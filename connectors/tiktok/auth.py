"""TikTok Shop authentication.

The flow differs from Amazon's in one way that matters operationally: TikTok
refresh tokens themselves expire (typically a year) and are *rotated* on every
refresh. If you refresh and discard the new refresh token, the old one still
works until it doesn't, and then the integration dies silently at some point
months later with no deploy to correlate it against.

So `TokenProvider` surfaces the rotated refresh token through a callback and
warns loudly when the refresh token is nearing expiry.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from .regions import AUTH_BASE, REFRESH_PATH, TOKEN_PATH

# Refresh the access token at 80% of life, as with Amazon.
REFRESH_AT_FRACTION = 0.8
# Warn when the refresh token has under this many days left.
REFRESH_TOKEN_WARN_DAYS = 30


class TikTokAuthError(RuntimeError):
    """Raised when the token exchange or refresh fails."""


@dataclass
class TikTokCredentials:
    app_key: str
    app_secret: str
    refresh_token: str
    shop_id: str = ""

    def __repr__(self) -> str:  # pragma: no cover - safety, not logic
        return f"<TikTokCredentials app_key={self.app_key[:8]}... secrets=***redacted***>"

    def validate(self) -> None:
        missing = [
            name for name, val in (
                ("TIKTOK_APP_KEY", self.app_key),
                ("TIKTOK_APP_SECRET", self.app_secret),
                ("TIKTOK_REFRESH_TOKEN", self.refresh_token),
            ) if not val
        ]
        if missing:
            raise TikTokAuthError(
                f"Incomplete TikTok Shop credentials; missing {', '.join(missing)}."
            )


@dataclass
class _CachedToken:
    value: str
    expires_at: float

    def __repr__(self) -> str:  # pragma: no cover - safety, not logic
        return f"<_CachedToken expires_at={self.expires_at:.0f} value=***redacted***>"

    @property
    def usable(self) -> bool:
        return bool(self.value) and time.time() < self.expires_at


class TokenProvider:
    """Access-token cache with refresh-token rotation handling."""

    def __init__(
        self,
        credentials: TikTokCredentials,
        *,
        http_get: Callable[..., str] | None = None,
        on_refresh_token_rotated: Callable[[str], None] | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.credentials = credentials
        self._timeout = timeout
        self._http_get = http_get or self._urllib_get
        self._on_rotated = on_refresh_token_rotated
        self._lock = threading.Lock()
        self._cache: _CachedToken | None = None
        self.warnings: list[str] = []
        self.refresh_token_expires_at: float | None = None

    def access_token(self, *, force_refresh: bool = False) -> str:
        self.credentials.validate()
        with self._lock:
            if self._cache and self._cache.usable and not force_refresh:
                return self._cache.value

            params = {
                "app_key": self.credentials.app_key,
                "app_secret": self.credentials.app_secret,
                "refresh_token": self.credentials.refresh_token,
                "grant_type": "refresh_token",
            }
            data = self._exchange(f"{AUTH_BASE}{REFRESH_PATH}", params)

            token = data.get("access_token")
            if not token:
                raise TikTokAuthError(
                    f"TikTok token refresh returned no access_token. Payload keys: "
                    f"{sorted(data)}. A common cause is an app_key/app_secret pair "
                    "from different applications."
                )

            lifetime = float(data.get("access_token_expire_in", 0) or 0)
            if lifetime <= 0:
                # Absolute epoch seconds are returned by some versions.
                expire_at = float(data.get("access_token_expire_in", 0) or 0)
                lifetime = max(expire_at - time.time(), 3600.0)
            self._cache = _CachedToken(token, time.time() + lifetime * REFRESH_AT_FRACTION)

            # Refresh tokens rotate. Losing the new one strands the integration.
            new_refresh = data.get("refresh_token")
            if new_refresh and new_refresh != self.credentials.refresh_token:
                self.credentials.refresh_token = new_refresh
                if self._on_rotated:
                    self._on_rotated(new_refresh)
                else:
                    self.warnings.append(
                        "TikTok rotated the refresh token and no persistence callback "
                        "is configured. Update TIKTOK_REFRESH_TOKEN with the new value "
                        "or the integration will stop working when the old one expires."
                    )

            expire_in = data.get("refresh_token_expire_in")
            if expire_in:
                self.refresh_token_expires_at = float(expire_in)
                days_left = (float(expire_in) - time.time()) / 86400
                if 0 < days_left < REFRESH_TOKEN_WARN_DAYS:
                    self.warnings.append(
                        f"TikTok refresh token expires in {days_left:.0f} days. "
                        "Re-authorise the app before then — once it lapses the "
                        "integration needs a full manual reauthorisation."
                    )
            return token

    def invalidate(self) -> None:
        with self._lock:
            self._cache = None

    def exchange_auth_code(self, auth_code: str) -> dict:
        """One-time exchange of an authorisation code for the first token pair."""
        self.credentials.validate()
        params = {
            "app_key": self.credentials.app_key,
            "app_secret": self.credentials.app_secret,
            "auth_code": auth_code,
            "grant_type": "authorized_code",
        }
        return self._exchange(f"{AUTH_BASE}{TOKEN_PATH}", params)

    # -- internals ---------------------------------------------------------
    def _exchange(self, url: str, params: dict) -> dict:
        query = urllib.parse.urlencode(params)
        try:
            raw = self._http_get(f"{url}?{query}", self._timeout)
        except urllib.error.HTTPError as exc:  # pragma: no cover - network path
            body = exc.read().decode("utf-8", "replace")[:500]
            raise TikTokAuthError(
                f"TikTok token endpoint returned HTTP {exc.code}: {body}"
            ) from exc
        except urllib.error.URLError as exc:  # pragma: no cover - network path
            raise TikTokAuthError(f"Could not reach TikTok auth: {exc.reason}") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TikTokAuthError(f"TikTok auth returned non-JSON: {raw[:200]!r}") from exc

        # The auth service, like the main API, signals failure inside a 200.
        code = payload.get("code", 0)
        if code != 0:
            raise TikTokAuthError(
                f"TikTok auth failed with code {code}: {payload.get('message', '')}. "
                "Check that the app is approved and authorised for this shop."
            )
        return payload.get("data", {})

    @staticmethod
    def _urllib_get(url: str, timeout: float) -> str:  # pragma: no cover - network
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
