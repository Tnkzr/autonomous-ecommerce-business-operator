"""TikTok Shop HTTP transport.

The single most important thing in this file: **TikTok returns HTTP 200 for
business failures.** A rejected product, an invalid shop_cipher, an expired
token, a rate limit — all arrive as `200 OK` with a non-zero `code` in the JSON
body. Any client that checks `response.status` and moves on will treat every
failure as a success, and an inventory sync built that way silently stops
syncing while reporting green.

So `request()` treats a non-zero `code` exactly as it would treat an HTTP error,
including for retry classification.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .signing import sign_request

# Business codes worth retrying. Everything else is deterministic — retrying a
# rejected payload just burns quota and looks like abuse.
RETRYABLE_CODES = {
    105000,   # internal error
    105002,   # service unavailable
    105003,   # timeout
    36004003, # rate limited
    90001,    # system busy
}
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Codes that mean "your token is dead" — re-auth then retry once.
AUTH_FAILURE_CODES = {105001, 36004004, 10005, 10006}

# TikTok publishes per-app QPS rather than per-operation buckets. 10 QPS is the
# common default; the limiter is deliberately conservative because a throttled
# app affects every endpoint at once, not just the noisy one.
DEFAULT_QPS = 8.0
DEFAULT_BURST = 8

MAX_ATTEMPTS = 5


class TikTokAPIError(RuntimeError):
    """A TikTok Shop call failed, by HTTP status or by business code."""

    def __init__(self, message: str, *, code: int | None = None,
                 status: int | None = None, endpoint: str = "",
                 request_id: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.endpoint = endpoint
        self.request_id = request_id

    @property
    def retryable(self) -> bool:
        return (self.code in RETRYABLE_CODES) or (self.status in RETRYABLE_STATUS)


class TikTokRateLimited(TikTokAPIError):
    """Throttled after exhausting retries."""


class TokenBucket:
    """Shared across all operations, because TikTok's limit is per app."""

    def __init__(self, rate: float = DEFAULT_QPS, burst: int = DEFAULT_BURST,
                 *, monotonic=time.monotonic) -> None:
        self.rate = max(rate, 0.0001)
        self.capacity = max(burst, 1)
        self._tokens = float(self.capacity)
        self._monotonic = monotonic
        self._last = monotonic()
        self._lock = threading.Lock()

    def acquire(self, *, sleep: Callable[[float], None] = time.sleep) -> float:
        waited = 0.0
        while True:
            with self._lock:
                now = self._monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._last) * self.rate
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return waited
                deficit = (1.0 - self._tokens) / self.rate
            sleep(deficit)
            waited += deficit


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: Any


class Transport:
    """Signs, sends, retries, and translates TikTok Shop responses."""

    def __init__(
        self,
        *,
        base_url: str,
        app_key: str,
        app_secret: str,
        token_provider,
        shop_cipher: str = "",
        send: Callable[..., Response] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        timeout: float = 30.0,
        qps: float = DEFAULT_QPS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.app_key = app_key
        self.app_secret = app_secret
        self.tokens = token_provider
        self.shop_cipher = shop_cipher
        self._send = send or self._urllib_send
        self._sleep = sleep
        self._bucket = TokenBucket(qps, monotonic=monotonic)
        self._timeout = timeout
        self.call_count = 0
        self.throttle_count = 0
        self.total_wait_seconds = 0.0

    def request(
        self,
        *,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: dict | None = None,
        needs_shop_cipher: bool = True,
        needs_auth: bool = True,
    ) -> dict:
        """Issue a signed request and return the `data` payload."""
        attempt = 0
        last_error: TikTokAPIError | None = None

        while attempt < MAX_ATTEMPTS:
            attempt += 1
            self.total_wait_seconds += self._bucket.acquire(sleep=self._sleep)

            params: dict[str, Any] = dict(query or {})
            params["app_key"] = self.app_key
            params["timestamp"] = int(time.time())
            if needs_shop_cipher and self.shop_cipher:
                params["shop_cipher"] = self.shop_cipher

            # Body must be serialised once and reused verbatim: signing one
            # string and sending another is an instant signature failure, and
            # dict ordering makes that trivially easy to do by accident.
            body_str = json.dumps(body, separators=(",", ":")) if body is not None else None

            params["sign"] = sign_request(
                path=path, params=params, app_secret=self.app_secret, body=body_str,
            )

            headers = {"Accept": "application/json"}
            if body_str is not None:
                headers["Content-Type"] = "application/json"
            if needs_auth:
                headers["x-tts-access-token"] = self.tokens.access_token()

            url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
            self.call_count += 1

            try:
                resp = self._send(method=method, url=url, headers=headers,
                                  body=body_str, timeout=self._timeout)
            except urllib.error.URLError as exc:  # pragma: no cover - network path
                last_error = TikTokAPIError(
                    f"Network failure calling {path}: {exc.reason}", endpoint=path)
                self._backoff(attempt)
                continue

            error = self._interpret(resp, path)
            if error is None:
                payload = resp.body if isinstance(resp.body, dict) else {}
                return payload.get("data", payload)

            last_error = error

            if error.code in AUTH_FAILURE_CODES or resp.status == 401:
                self.tokens.invalidate()
                if attempt < MAX_ATTEMPTS:
                    continue

            if error.retryable:
                if error.code == 36004003 or resp.status == 429:
                    self.throttle_count += 1
                self._backoff(attempt, retry_after=resp.headers.get("Retry-After"))
                continue

            raise error

        if last_error and (last_error.code == 36004003 or last_error.status == 429):
            raise TikTokRateLimited(
                f"{path} still throttled after {MAX_ATTEMPTS} attempts. TikTok limits "
                "QPS per application, so this affects every endpoint at once — reduce "
                "batch sizes or spread the run out rather than retrying harder.",
                code=36004003, endpoint=path,
            )
        raise last_error or TikTokAPIError(f"{path} failed with no response.", endpoint=path)

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _interpret(resp: Response, path: str) -> TikTokAPIError | None:
        """Return an error, or None when the call genuinely succeeded.

        Checks the business code as well as the HTTP status, because TikTok
        reports most failures as 200 with a non-zero code.
        """
        body = resp.body if isinstance(resp.body, dict) else {}
        request_id = str(body.get("request_id", "") or resp.headers.get("x-tt-logid", ""))

        if resp.status >= 400:
            return TikTokAPIError(
                f"{path} failed with HTTP {resp.status}: {str(resp.body)[:300]}",
                status=resp.status, endpoint=path, request_id=request_id,
            )

        code = body.get("code", 0)
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = -1

        if code == 0:
            return None

        message = body.get("message", "") or body.get("msg", "")
        hint = ""
        if code in AUTH_FAILURE_CODES:
            hint = (" — the access token was rejected. The app authorisation for this "
                    "shop may have been revoked.")
        elif "shop_cipher" in str(message).lower():
            hint = (" — shop_cipher is missing or wrong. Fetch it from "
                    "/authorization/{v}/shops; it is per-shop and not interchangeable.")
        elif "sign" in str(message).lower():
            hint = (" — signature rejected. Check that the signed body string is byte "
                    "identical to the one sent, and that access_token and sign are "
                    "excluded from the signature base.")

        return TikTokAPIError(
            f"{path} failed with code {code}: {message}{hint}",
            code=code, status=resp.status, endpoint=path, request_id=request_id,
        )

    def _backoff(self, attempt: int, *, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                self._sleep(float(retry_after))
                return
            except ValueError:
                pass
        self._sleep(min(2.0 ** attempt, 30.0))

    @staticmethod
    def _urllib_send(*, method: str, url: str, headers: dict[str, str],
                     body: str | None, timeout: float) -> Response:  # pragma: no cover
        data = body.encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
                parsed = json.loads(text) if text.strip() else {}
                return Response(resp.status, dict(resp.headers.items()), parsed)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(text) if text.strip() else {}
            except json.JSONDecodeError:
                parsed = {"raw": text[:1000]}
            return Response(exc.code, dict(exc.headers.items()) if exc.headers else {},
                            parsed)

    def stats(self) -> dict[str, Any]:
        return {
            "calls": self.call_count,
            "throttled": self.throttle_count,
            "seconds_waiting_on_rate_limits": round(self.total_wait_seconds, 2),
        }
