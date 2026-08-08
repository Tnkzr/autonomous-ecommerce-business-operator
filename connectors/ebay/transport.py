"""eBay HTTP transport.

Three things here differ from the other connectors and drive the design.

**Rate limits are a daily quota, not a per-second bucket.** Amazon, Shopify and
TikTok all refill continuously, so the right response to a limit is to wait —
seconds, and the call succeeds. eBay grants each application a fixed number of
calls per API per *day*, resetting at midnight UTC. Waiting is useless: if the
quota is gone at 2pm, sleeping does not get it back, and a limiter that blocks
would hang for ten hours. So `DailyQuota` **refuses** instead, and refuses
early enough to keep a reserve for the calls that matter. Budget is spent, not
borrowed.

**eBay uses honest HTTP status codes.** After TikTok (200 for failures) and
Shopify (200 for two of three failure layers), this is a relief and worth
stating so nobody adds a defensive body-check that never fires. A 200 here
means it worked. Errors arrive as `{"errors": [...]}` with a stable numeric
`errorId`, which is the thing to branch on — messages get reworded, ids do not.

**The marketplace header decides which country's data you get.** Omitting
`X-EBAY-C-MARKETPLACE-ID` does not fail; it silently defaults, and a repricer
comparing US listings against UK prices produces confident nonsense. It is set
on every call from the credentials rather than passed per call.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .auth import APPLICATION, USER, EbayAuthError

# Documented defaults for a new application. eBay publishes actual per-API
# quotas through the Developer Analytics API, and `DailyQuota.adopt` takes them
# — these are a starting point for the first run of the day, never a seller's
# real entitlement.
DEFAULT_DAILY_QUOTA = 5000

# Stop spending at this fraction so a scheduled sync cannot consume the whole
# day's budget and leave nothing for an urgent repricing or a stock correction.
SOFT_LIMIT_FRACTION = 0.80

MAX_ATTEMPTS = 4
MAX_BACKOFF_SECONDS = 20.0

RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# eBay error ids worth knowing by number. The message text is reworded between
# releases; these are stable.
ERROR_HINTS = {
    1001: ("the token is invalid or expired. The transport refreshes and "
           "retries once; seeing this after that means the grant is dead."),
    1002: ("the token does not carry the scope this call needs. Scopes are "
           "fixed at consent — adding one means re-running the consent flow."),
    1100: ("the application is not authorised for this API. Check the keyset "
           "has the right API access enabled in the developer console."),
    2001: "too many requests — the daily quota for this API is exhausted.",
    # Inventory API specifics that are easy to misread.
    25001: "an internal eBay error; retry is appropriate.",
    25002: "a user error in the payload — the request will fail identically on retry.",
    25710: "the resource was not found; check the SKU or offer id.",
}


class EbayAPIError(RuntimeError):
    """An eBay call failed."""

    def __init__(self, message: str, *, status: int | None = None,
                 error_id: int | None = None, endpoint: str = "",
                 errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.error_id = error_id
        self.endpoint = endpoint
        self.errors = errors or []

    @property
    def retryable(self) -> bool:
        if self.error_id in (25002, 25710):
            return False
        return self.status in RETRYABLE_STATUS


class EbayQuotaExhausted(EbayAPIError):
    """The daily call budget is spent. Waiting does not help before midnight UTC."""

    @property
    def retryable(self) -> bool:
        # Never. On a per-second bucket a 429 means "try again shortly"; on a
        # daily quota it means "you are done until midnight UTC". Inheriting
        # the status-based rule here would burn the remaining attempts on calls
        # that cannot succeed, and make the app look like it is hammering a
        # limit it has already been told about.
        return False


@dataclass
class QuotaState:
    limit: int = DEFAULT_DAILY_QUOTA
    used: int = 0
    day: str = ""
    adopted: bool = False

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)

    @property
    def soft_limit(self) -> int:
        return int(self.limit * SOFT_LIMIT_FRACTION)


class DailyQuota:
    """Per-API daily call budget that refuses rather than blocks.

    Keyed by API name because eBay meters each one separately: exhausting
    Browse does not stop Fulfillment, and a single shared counter would either
    stop the wrong calls or fail to stop the right ones.
    """

    def __init__(self, *, default_limit: int = DEFAULT_DAILY_QUOTA,
                 clock: Callable[[], float] = time.time) -> None:
        self.default_limit = default_limit
        self._clock = clock
        self._apis: dict[str, QuotaState] = {}
        self._lock = threading.Lock()

    def _today(self) -> str:
        return datetime.fromtimestamp(self._clock(), tz=timezone.utc).date().isoformat()

    def _state(self, api: str) -> QuotaState:
        today = self._today()
        state = self._apis.get(api)
        if state is None or state.day != today:
            # A new UTC day resets the budget. Adoption does not survive it —
            # the real limit is re-read from the API rather than assumed.
            state = QuotaState(limit=self.default_limit, day=today)
            self._apis[api] = state
        return state

    def spend(self, api: str, *, cost: int = 1, allow_reserve: bool = False) -> None:
        """Record a call, or raise if the budget will not cover it."""
        with self._lock:
            state = self._state(api)
            ceiling = state.limit if allow_reserve else state.soft_limit
            if state.used + cost > ceiling:
                reserve_note = (
                    "" if allow_reserve else
                    f" A reserve of {state.limit - state.soft_limit} call(s) is "
                    "held back for urgent work; pass allow_reserve to use it.")
                raise EbayQuotaExhausted(
                    f"Daily quota for the {api} API is spent: {state.used} of "
                    f"{state.limit} used. eBay resets these at midnight UTC and "
                    "waiting does not restore them, so this run should stop "
                    f"rather than retry.{reserve_note}",
                    error_id=2001, endpoint=api)
            state.used += cost

    def adopt(self, api: str, *, limit: int, used: int | None = None) -> None:
        """Take the real quota reported by eBay's Developer Analytics API."""
        with self._lock:
            state = self._state(api)
            state.limit = max(int(limit), 1)
            if used is not None:
                state.used = max(int(used), 0)
            state.adopted = True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                api: {
                    "limit": state.limit, "used": state.used,
                    "remaining": state.remaining,
                    "soft_limit": state.soft_limit,
                    "limit_adopted_from_api": state.adopted,
                }
                for api, state in self._apis.items()
            }


@dataclass
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None


class Transport:
    """Signs, sends, meters, and interprets eBay REST calls."""

    def __init__(self, *, credentials, token_provider,
                 send: Callable[..., Response] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time,
                 timeout: float = 30.0,
                 quota: DailyQuota | None = None) -> None:
        self.credentials = credentials
        self.tokens = token_provider
        self._send = send or self._urllib_send
        self._sleep = sleep
        self._timeout = timeout
        self.quota = quota or DailyQuota(clock=clock)
        self.call_count = 0
        self.refused_count = 0

    @property
    def base_url(self) -> str:
        return self.credentials.api_base

    def request(self, *, method: str, path: str,
                query: dict[str, Any] | None = None,
                body: dict[str, Any] | None = None,
                api: str = "sell", token_kind: str = USER,
                allow_reserve: bool = False,
                extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
        """Issue one call and return the parsed body.

        `api` names the quota bucket, `token_kind` selects application vs user
        credentials. Both are explicit at every call site rather than inferred
        from the path, because inferring them is how a Sell call quietly goes
        out with a Browse token.
        """
        try:
            self.quota.spend(api, allow_reserve=allow_reserve)
        except EbayQuotaExhausted:
            self.refused_count += 1
            raise

        url = f"{self.base_url}{path}"
        if query:
            cleaned = {k: v for k, v in query.items() if v is not None}
            if cleaned:
                url += "?" + urllib.parse.urlencode(cleaned)

        payload = json.dumps(body, separators=(",", ":")) if body is not None else None
        last_error: EbayAPIError | None = None
        refreshed = False

        for attempt in range(1, MAX_ATTEMPTS + 1):
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {self.tokens.access_token(token_kind)}",
                # Set on every call. Omitting it does not fail — it silently
                # returns another country's data.
                "X-EBAY-C-MARKETPLACE-ID": self.credentials.marketplace_id,
            }
            if payload is not None:
                headers["Content-Type"] = "application/json"
            if extra_headers:
                headers.update(extra_headers)

            self.call_count += 1
            try:
                response = self._send(method=method, url=url, headers=headers,
                                      body=payload, timeout=self._timeout)
            except urllib.error.URLError as exc:  # pragma: no cover - network
                last_error = EbayAPIError(
                    f"Network failure calling {path}: {exc.reason}", endpoint=path)
                self._backoff(attempt)
                continue

            error = self._interpret(response, path)
            if error is None:
                if isinstance(response.body, dict):
                    return response.body
                return {} if response.body is None else {"data": response.body}

            last_error = error

            # A 401 on the first attempt is usually a token that expired
            # between mint and use. Refresh once and retry; a second 401 means
            # the grant itself is bad and retrying is noise.
            if response.status == 401 and not refreshed:
                refreshed = True
                self.tokens.invalidate(token_kind)
                continue

            if error.retryable and attempt < MAX_ATTEMPTS:
                self._backoff(attempt, retry_after=response.headers.get("Retry-After"))
                continue
            raise error

        raise last_error or EbayAPIError(f"{path} produced no response.",
                                         endpoint=path)

    # -- interpretation ----------------------------------------------------
    @staticmethod
    def _interpret(response: Response, path: str) -> EbayAPIError | None:
        """Return an error, or None when the call succeeded.

        Unlike TikTok and Shopify, the HTTP status is trustworthy here — a 2xx
        with an `errors` array does not happen, so this checks status first and
        only reads the body to explain what went wrong.
        """
        if 200 <= response.status < 300:
            return None

        body = response.body if isinstance(response.body, dict) else {}
        errors = body.get("errors") or []
        first = errors[0] if errors and isinstance(errors[0], dict) else {}

        try:
            error_id = int(first.get("errorId")) if first.get("errorId") is not None else None
        except (TypeError, ValueError):
            error_id = None

        message = str(first.get("longMessage") or first.get("message")
                      or body or response.body)[:300]
        hint = ERROR_HINTS.get(error_id or -1, "")

        parameters = first.get("parameters") or []
        if parameters:
            rendered = ", ".join(
                f"{p.get('name')}={p.get('value')}" for p in parameters
                if isinstance(p, dict))
            if rendered:
                message += f" [{rendered}]"

        if response.status == 429 or error_id == 2001:
            return EbayQuotaExhausted(
                f"{path} was rate limited (HTTP {response.status}): {message}. "
                "eBay meters per API per day and resets at midnight UTC.",
                status=response.status, error_id=error_id, endpoint=path,
                errors=errors)

        return EbayAPIError(
            f"{path} failed with HTTP {response.status}"
            + (f" (errorId {error_id})" if error_id else "")
            + f": {message}" + (f" — {hint}" if hint else ""),
            status=response.status, error_id=error_id, endpoint=path,
            errors=errors)

    def _backoff(self, attempt: int, *, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                self._sleep(min(float(retry_after), MAX_BACKOFF_SECONDS))
                return
            except ValueError:
                # Retry-After may be an HTTP-date. Fall through to exponential
                # backoff rather than failing over an unparseable header.
                pass
        self._sleep(min(2.0 ** attempt, MAX_BACKOFF_SECONDS))

    @staticmethod
    def _urllib_send(*, method: str, url: str, headers: dict[str, str],
                     body: str | None, timeout: float) -> Response:  # pragma: no cover
        data = body.encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method,
                                         headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read().decode("utf-8", "replace")
                parsed = json.loads(text) if text.strip() else {}
                return Response(response.status, dict(response.headers.items()),
                                parsed)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(text) if text.strip() else {}
            except json.JSONDecodeError:
                parsed = {"raw": text[:1000]}
            return Response(exc.code,
                            dict(exc.headers.items()) if exc.headers else {},
                            parsed)

    def stats(self) -> dict[str, Any]:
        return {
            "calls": self.call_count,
            "refused_on_quota": self.refused_count,
            "token_refreshes": getattr(self.tokens, "refresh_count", 0),
            "quota": self.quota.snapshot(),
        }
