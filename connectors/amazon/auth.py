"""Login with Amazon (LWA) token management.

SP-API no longer requires AWS SigV4 request signing for standard operations —
the LWA access token in `x-amz-access-token` is the credential. This module
exchanges the long-lived refresh token for short-lived access tokens and caches
them.

Two things here are load-bearing:

1. **Refresh early, not on failure.** Tokens live an hour. Refreshing at expiry
   means every long-running job hits a 403 mid-flight and has to unwind. We
   refresh at 80% of the lifetime instead.

2. **Never log the token.** `__repr__` is overridden on the credential holder
   because an access token in a traceback or a log line is a live credential
   for the next hour.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .regions import LWA_TOKEN_URL

# Refresh at 80% of lifetime so no request is issued with a nearly-dead token.
REFRESH_AT_FRACTION = 0.8


class LWAError(RuntimeError):
    """Raised when the token exchange fails."""


@dataclass
class _CachedToken:
    value: str
    expires_at: float

    def __repr__(self) -> str:  # pragma: no cover - safety, not logic
        return f"<_CachedToken expires_at={self.expires_at:.0f} value=***redacted***>"

    @property
    def usable(self) -> bool:
        return bool(self.value) and time.time() < self.expires_at


@dataclass
class LWACredentials:
    client_id: str
    client_secret: str
    refresh_token: str

    def __repr__(self) -> str:  # pragma: no cover - safety, not logic
        return f"<LWACredentials client_id={self.client_id[:12]}... secrets=***redacted***>"

    def validate(self) -> None:
        missing = [
            name for name, val in (
                ("AMZ_LWA_CLIENT_ID", self.client_id),
                ("AMZ_LWA_CLIENT_SECRET", self.client_secret),
                ("AMZ_REFRESH_TOKEN", self.refresh_token),
            ) if not val
        ]
        if missing:
            raise LWAError(f"Incomplete LWA credentials; missing {', '.join(missing)}.")


class TokenProvider:
    """Thread-safe LWA access-token cache.

    `http_post` is injectable so the token path is testable without network
    access and without a real Amazon application.
    """

    def __init__(
        self,
        credentials: LWACredentials,
        *,
        http_post=None,
        timeout: float = 20.0,
    ) -> None:
        self.credentials = credentials
        self._timeout = timeout
        self._http_post = http_post or self._urllib_post
        self._lock = threading.Lock()
        self._cache: dict[str, _CachedToken] = {}

    # -- public ------------------------------------------------------------
    def access_token(self, *, scope: str | None = None, force_refresh: bool = False) -> str:
        """Return a valid access token, refreshing when needed.

        `scope` selects a grantless token (used by a few operations such as
        notifications); omit it for ordinary seller-authorised calls.
        """
        self.credentials.validate()
        key = scope or "_seller"
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached.usable and not force_refresh:
                return cached.value

            payload = self._build_payload(scope)
            data = self._exchange(payload)

            token = data.get("access_token")
            if not token:
                raise LWAError(
                    "LWA response contained no access_token. "
                    f"Keys returned: {sorted(data)}."
                )
            lifetime = float(data.get("expires_in", 3600))
            self._cache[key] = _CachedToken(
                value=token,
                expires_at=time.time() + lifetime * REFRESH_AT_FRACTION,
            )
            return token

    def invalidate(self, *, scope: str | None = None) -> None:
        """Drop a cached token. Called after a 403 so the next call re-auths."""
        with self._lock:
            self._cache.pop(scope or "_seller", None)

    # -- internals ---------------------------------------------------------
    def _build_payload(self, scope: str | None) -> dict[str, str]:
        if scope:
            return {
                "grant_type": "client_credentials",
                "scope": scope,
                "client_id": self.credentials.client_id,
                "client_secret": self.credentials.client_secret,
            }
        return {
            "grant_type": "refresh_token",
            "refresh_token": self.credentials.refresh_token,
            "client_id": self.credentials.client_id,
            "client_secret": self.credentials.client_secret,
        }

    def _exchange(self, payload: dict[str, str]) -> dict:
        try:
            raw = self._http_post(LWA_TOKEN_URL, payload, self._timeout)
        except urllib.error.HTTPError as exc:  # pragma: no cover - network path
            body = exc.read().decode("utf-8", "replace")[:500]
            raise LWAError(
                f"LWA token exchange failed with HTTP {exc.code}. Response: {body}\n"
                "Common causes: the refresh token was issued for a different "
                "application, the app was reauthorised (which invalidates old refresh "
                "tokens), or client_id/client_secret belong to different apps."
            ) from exc
        except urllib.error.URLError as exc:  # pragma: no cover - network path
            raise LWAError(f"Could not reach the LWA token endpoint: {exc.reason}") from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LWAError(f"LWA returned non-JSON: {raw[:200]!r}") from exc

    @staticmethod
    def _urllib_post(url: str, payload: dict[str, str], timeout: float) -> str:
        body = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
