"""eBay OAuth: two token types from one endpoint.

`TokenProvider` mints and caches both, keyed by kind, because they are not
interchangeable and the failure when they are swapped is misleading:

- `application` — client-credentials grant. Authenticates the *app*. This is
  what the Browse API accepts for public catalogue and competitor data.
- `user` — refresh-token grant. Authenticates the *seller*. Every Sell API
  (orders, inventory, finances, analytics) requires it.

Calling a Sell API with an application token returns 403 with a message about
insufficient permissions, which reads as a missing scope and sends people back
to the consent screen for an hour. Keeping the two in separate cache slots and
selecting by call site is the only reliable fix.

**Refresh early, not on failure.** Access tokens are valid for roughly two
hours. Refreshing reactively on the first 401 means every token expiry costs a
failed call, and under concurrency it costs several. `SKEW_SECONDS` retires a
token before eBay does.

**The 18-month cliff.** The refresh token does not rotate — using it does not
extend it. It simply stops working about 18 months after consent, and the only
fix is a human repeating the browser flow. Nothing in software prevents that,
so `grant_age_warning()` reports the approach instead of letting it arrive as a
mystery outage.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable

from .credentials import EbayCredentials

# Retire a token this long before eBay would. Two minutes is comfortably longer
# than any single call plus a retry, so a token never expires mid-flight.
SKEW_SECONDS = 120

# eBay's refresh tokens are documented at 18 months. Warn from 17 so there is a
# month to act, and treat the last fortnight as urgent — re-consent needs a
# human at a browser, which is not always same-day.
REFRESH_TOKEN_DAYS = 547          # ~18 months
WARN_AFTER_DAYS = 517             # ~17 months
URGENT_AFTER_DAYS = 533           # ~2 weeks left

APPLICATION = "application"
USER = "user"

# eBay's OAuth errors are a small, stable set. `invalid_grant` is the one that
# matters: it means the refresh token is dead, and no retry will revive it.
FATAL_GRANT_ERRORS = {"invalid_grant", "invalid_client", "unauthorized_client"}


class EbayAuthError(RuntimeError):
    """Token acquisition failed."""

    def __init__(self, message: str, *, error_code: str = "",
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


@dataclass
class Token:
    value: str
    expires_at: float
    kind: str

    def valid(self, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return bool(self.value) and now < (self.expires_at - SKEW_SECONDS)


class TokenProvider:
    """Mints and caches eBay access tokens of both kinds."""

    def __init__(self, credentials: EbayCredentials, *,
                 send: Callable[..., tuple[int, dict[str, Any]]] | None = None,
                 scopes: tuple[str, ...] = (),
                 clock: Callable[[], float] = time.time) -> None:
        self.credentials = credentials
        self._send = send or self._urllib_send
        self._scopes = scopes
        self._clock = clock
        self._tokens: dict[str, Token] = {}
        self._lock = threading.Lock()
        self.refresh_count = 0

    # -- public ------------------------------------------------------------
    def access_token(self, kind: str = USER, *, force_refresh: bool = False) -> str:
        """Return a valid token of the requested kind, refreshing if needed."""
        if kind not in (APPLICATION, USER):
            raise ValueError(
                f"Unknown eBay token kind {kind!r}. Expected "
                f"{APPLICATION!r} (Browse, public data) or {USER!r} (Sell APIs).")
        with self._lock:
            cached = self._tokens.get(kind)
            if cached is not None and cached.valid(now=self._clock()) \
                    and not force_refresh:
                return cached.value
            token = self._mint(kind)
            self._tokens[kind] = token
            return token.value

    def invalidate(self, kind: str | None = None) -> None:
        """Drop cached tokens. `None` drops both."""
        with self._lock:
            if kind is None:
                self._tokens.clear()
            else:
                self._tokens.pop(kind, None)

    def grant_age_warning(self, *, today: date | None = None) -> str:
        """Warn as the non-renewable refresh token approaches expiry.

        Empty when there is nothing to say. Requires `granted_at` — without it
        the age is unknowable, and that gap is itself reported, because a
        silent unknown here becomes a surprise outage in a year and a half.
        """
        raw = (self.credentials.granted_at or "").strip()
        if not raw:
            return ("Refresh-token grant date is unknown, so its expiry cannot "
                    "be tracked. eBay refresh tokens stop working about 18 "
                    "months after consent and cannot be renewed in software. "
                    "Set EBAY_REFRESH_TOKEN_GRANTED_AT to the date you "
                    "authorised the app.")
        try:
            granted = date.fromisoformat(raw[:10])
        except ValueError:
            return (f"Refresh-token grant date {raw!r} is not a valid date "
                    "(YYYY-MM-DD), so expiry cannot be tracked.")

        today = today or datetime.now(timezone.utc).date()
        age = (today - granted).days
        remaining = REFRESH_TOKEN_DAYS - age
        if age >= REFRESH_TOKEN_DAYS:
            return (f"Refresh token was granted {age} days ago and is past its "
                    "~18-month life. Sell API calls will fail with "
                    "invalid_grant until a human re-runs the consent flow.")
        if age >= URGENT_AFTER_DAYS:
            return (f"Refresh token expires in about {remaining} day(s). "
                    "Renewal needs a human at a browser — schedule it now.")
        if age >= WARN_AFTER_DAYS:
            return (f"Refresh token expires in about {remaining} day(s). It "
                    "cannot be renewed in software; plan the re-consent.")
        return ""

    # -- minting -----------------------------------------------------------
    def _mint(self, kind: str) -> Token:
        if kind == USER:
            payload = {
                "grant_type": "refresh_token",
                "refresh_token": self.credentials.refresh_token,
            }
            if self._scopes:
                payload["scope"] = " ".join(self._scopes)
        else:
            payload = {"grant_type": "client_credentials",
                       "scope": "https://api.ebay.com/oauth/api_scope"}

        basic = base64.b64encode(
            f"{self.credentials.client_id}:{self.credentials.client_secret}"
            .encode("utf-8")).decode("ascii")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
            "Accept": "application/json",
        }

        status, body = self._send(
            url=self.credentials.auth_url, headers=headers,
            body=urllib.parse.urlencode(payload))
        self.refresh_count += 1

        if status >= 400 or "access_token" not in body:
            raise self._interpret_failure(status, body, kind)

        expires_in = body.get("expires_in")
        try:
            lifetime = float(expires_in)
        except (TypeError, ValueError):
            # A missing lifetime is not a reason to cache forever. Two hours is
            # eBay's documented user-token life and the safe assumption.
            lifetime = 7200.0
        return Token(value=str(body["access_token"]),
                     expires_at=self._clock() + lifetime, kind=kind)

    def _interpret_failure(self, status: int, body: dict[str, Any],
                           kind: str) -> EbayAuthError:
        code = str(body.get("error", "") or "")
        description = str(body.get("error_description", "") or body)[:300]

        if code == "invalid_grant" and kind == USER:
            return EbayAuthError(
                f"eBay rejected the refresh token ({code}): {description}. "
                "This is not transient — the token has expired (they last about "
                "18 months and do not renew on use) or the app's authorisation "
                "was revoked. A human must repeat the consent flow; retrying "
                "will not help.\n" + self.grant_age_warning(),
                error_code=code, retryable=False)
        if code in FATAL_GRANT_ERRORS:
            return EbayAuthError(
                f"eBay rejected the credentials ({code}): {description}. Check "
                "that the client id and secret are from the same keyset, and "
                f"that both are {self.credentials.environment} keys — a sandbox "
                "keyset against a production host fails exactly like this.",
                error_code=code, retryable=False)
        if status >= 500 or status == 429:
            return EbayAuthError(
                f"eBay token endpoint returned HTTP {status}: {description}",
                error_code=code, retryable=True)
        return EbayAuthError(
            f"eBay token request failed with HTTP {status} ({code}): "
            f"{description}", error_code=code, retryable=False)

    @staticmethod
    def _urllib_send(*, url: str, headers: dict[str, str],
                     body: str) -> tuple[int, dict[str, Any]]:  # pragma: no cover
        request = urllib.request.Request(
            url, data=body.encode("utf-8"), method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                text = response.read().decode("utf-8", "replace")
                return response.status, (json.loads(text) if text.strip() else {})
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(text) if text.strip() else {}
            except json.JSONDecodeError:
                parsed = {"error_description": text[:500]}
            return exc.code, parsed
