"""Shopify Admin API transport (GraphQL).

GraphQL only, deliberately. Shopify moved the Admin API to GraphQL-first and
marked the REST product endpoints legacy; building new work on REST buys a
rewrite. GraphQL also solves a problem this operator has repeatedly: REST would
need five round trips to assemble a product with variants, inventory levels and
publication state, and each of those is a chance to half-fail.

**Three layers of failure, and all three must be checked.** This is the trap
here, the same shape as TikTok's "HTTP 200 for business failures" but with an
extra floor:

1. `HTTP status` — network, auth, and shop-level failures (401, 402 for a
   frozen shop, 423 for a locked one, 5xx).
2. `errors[]` at the top level — the query was rejected: bad syntax, a field
   that does not exist in this API version, or `THROTTLED`. **HTTP 200.**
3. `data.<mutation>.userErrors[]` — the query ran and the business rejected it:
   a duplicate handle, an invalid price, a missing required option. **HTTP 200,
   no top-level errors, and `data` is populated.** Code that checks only the
   first two treats "Shopify refused to create your product" as success, and
   the operator then reports a listing that does not exist.

`_interpret` is the single place that decides success, and it checks all three.

**Rate limiting is cost-based, not request-based.** Shopify computes a point
cost per query and runs a leaky bucket. Every response carries the live bucket
state in `extensions.cost.throttleStatus`, so the limiter never has to guess:
the defaults below are a conservative starting point for the first request only
and are replaced by the real numbers as soon as one comes back. That also means
a `THROTTLED` error is not a case for blind exponential backoff — the response
says how many points you have and how fast they restore, so the wait is
arithmetic. Backing off blindly either stalls far too long or hammers a bucket
that was never going to be full yet.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

# Conservative starting point for the very first request of a session. The
# authoritative numbers arrive in `extensions.cost.throttleStatus` on every
# response and are adopted at runtime — a seller's real plan limits are not
# knowable from here and must never be hardcoded as if they were.
DEFAULT_BUCKET_POINTS = 100.0
DEFAULT_RESTORE_RATE = 50.0
DEFAULT_QUERY_COST = 10.0

MAX_ATTEMPTS = 5
MAX_BACKOFF_SECONDS = 30.0

# HTTP statuses worth another attempt. 402/423 are excluded on purpose: a
# frozen or locked shop will still be frozen in four seconds, and retrying
# reads as a misbehaving app.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Shopify's machine-readable rejection codes that a retry can actually fix.
RETRYABLE_ERROR_CODES = {"THROTTLED", "INTERNAL_SERVER_ERROR", "TIMEOUT"}

SHOP_STATE_HINTS = {
    401: ("the Admin API access token was rejected. A custom app token is "
          "revoked when the app is uninstalled, and it is store-specific."),
    402: ("the shop is frozen — usually an unpaid bill. No API call will "
          "succeed until the merchant settles it; this is not retryable."),
    403: ("the token is valid but lacks the required access scope. Scopes are "
          "granted at install time; adding one requires reinstalling the app."),
    404: ("the endpoint does not exist in this API version. Shopify expires a "
          "version after roughly 12 months — check the pinned api_version."),
    423: "the shop is locked and rejecting API traffic. Not retryable.",
}


class ShopifyAPIError(RuntimeError):
    """A Shopify call failed, at any of the three layers."""

    def __init__(self, message: str, *, status: int | None = None,
                 code: str = "", layer: str = "", request_id: str = "",
                 user_errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.layer = layer          # http | errors | user_errors
        self.request_id = request_id
        self.user_errors = user_errors or []

    @property
    def retryable(self) -> bool:
        # A userErrors rejection is a business decision, never retryable — the
        # same payload will be refused identically every time.
        if self.layer == "user_errors":
            return False
        return self.status in RETRYABLE_STATUS or self.code in RETRYABLE_ERROR_CODES


class ShopifyThrottled(ShopifyAPIError):
    """Still throttled after exhausting attempts."""


class ShopifyUserError(ShopifyAPIError):
    """The mutation ran and Shopify's business rules rejected it."""


@dataclass
class ThrottleStatus:
    """Live bucket state as reported by Shopify."""

    maximum_available: float = DEFAULT_BUCKET_POINTS
    currently_available: float = DEFAULT_BUCKET_POINTS
    restore_rate: float = DEFAULT_RESTORE_RATE

    def seconds_until(self, points: float) -> float:
        if self.currently_available >= points:
            return 0.0
        if self.restore_rate <= 0:
            return MAX_BACKOFF_SECONDS
        return (points - self.currently_available) / self.restore_rate


class CostLimiter:
    """Leaky bucket in *query cost points*, synchronised to Shopify's own state.

    Locally predicted between calls, corrected by `adopt` after each response.
    Prediction alone drifts (other processes share the bucket) and adoption
    alone is blind to the request in flight; together they track.
    """

    def __init__(self, *, maximum: float = DEFAULT_BUCKET_POINTS,
                 restore_rate: float = DEFAULT_RESTORE_RATE,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.maximum = max(maximum, 1.0)
        self.restore_rate = max(restore_rate, 0.0001)
        self._available = self.maximum
        self._monotonic = monotonic
        self._last = monotonic()
        self._lock = threading.Lock()
        self.adopted = False

    def _replenish_locked(self) -> None:
        now = self._monotonic()
        self._available = min(
            self.maximum, self._available + (now - self._last) * self.restore_rate)
        self._last = now

    def acquire(self, cost: float, *,
                sleep: Callable[[float], None] = time.sleep) -> float:
        """Block until `cost` points are available. Returns seconds waited."""
        waited = 0.0
        # A query costing more than the whole bucket can never run; waiting
        # would hang forever, so let it through and surface Shopify's own error.
        cost = min(cost, self.maximum)
        while True:
            with self._lock:
                self._replenish_locked()
                if self._available >= cost:
                    self._available -= cost
                    return waited
                deficit = (cost - self._available) / self.restore_rate
            sleep(deficit)
            waited += deficit

    def adopt(self, status: ThrottleStatus) -> None:
        """Replace predicted state with what Shopify actually reports."""
        with self._lock:
            self.maximum = max(status.maximum_available, 1.0)
            self.restore_rate = max(status.restore_rate, 0.0001)
            self._available = max(status.currently_available, 0.0)
            self._last = self._monotonic()
            self.adopted = True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._replenish_locked()
            return {
                "available_points": round(self._available, 1),
                "maximum_points": self.maximum,
                "restore_per_second": self.restore_rate,
                "limits_adopted_from_api": self.adopted,
            }


@dataclass
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None


class Transport:
    """Sends, throttles, retries, and interprets Shopify GraphQL calls."""

    def __init__(
        self,
        *,
        shop_domain: str,
        access_token: str,
        api_version: str,
        send: Callable[..., Response] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        timeout: float = 30.0,
    ) -> None:
        self.shop_domain = normalise_domain(shop_domain)
        self.access_token = access_token
        self.api_version = api_version
        self._send = send or self._urllib_send
        self._sleep = sleep
        self._limiter = CostLimiter(monotonic=monotonic)
        self._timeout = timeout
        self.call_count = 0
        self.throttle_count = 0
        self.total_wait_seconds = 0.0
        self.points_spent = 0.0

    @property
    def endpoint(self) -> str:
        return f"https://{self.shop_domain}/admin/api/{self.api_version}/graphql.json"

    def execute(self, query: str, variables: dict[str, Any] | None = None, *,
                operation: str = "", mutation_field: str = "",
                estimated_cost: float = DEFAULT_QUERY_COST) -> dict[str, Any]:
        """Run one GraphQL document and return its `data`.

        `mutation_field` names the field whose `userErrors` must be empty for
        the call to count as successful. Omitting it on a mutation is how a
        rejected write gets reported as a success, so the client always passes
        it.
        """
        label = operation or mutation_field or "graphql"
        payload = json.dumps({"query": query, "variables": variables or {}},
                             separators=(",", ":"))
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Shopify-Access-Token": self.access_token,
        }

        last_error: ShopifyAPIError | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.total_wait_seconds += self._limiter.acquire(
                estimated_cost, sleep=self._sleep)
            self.call_count += 1

            try:
                resp = self._send(url=self.endpoint, headers=headers,
                                  body=payload, timeout=self._timeout)
            except urllib.error.URLError as exc:  # pragma: no cover - network
                last_error = ShopifyAPIError(
                    f"Network failure calling {label}: {exc.reason}", layer="http")
                self._backoff(attempt)
                continue

            body = resp.body if isinstance(resp.body, dict) else {}
            status = self._read_throttle_status(body)
            if status is not None:
                self._limiter.adopt(status)
            self.points_spent += self._read_actual_cost(body)

            error = self._interpret(resp, label, mutation_field)
            if error is None:
                return body.get("data") or {}

            last_error = error
            if not error.retryable:
                raise error
            if attempt == MAX_ATTEMPTS:
                # Fall out of the loop rather than raising here, so exhaustion
                # gets the specific diagnosis below instead of repeating the
                # last generic error.
                break

            if error.code == "THROTTLED" or resp.status == 429:
                self.throttle_count += 1
                # Cost-based limiter: the response says exactly how long the
                # points take to come back, so wait that long rather than
                # doubling blindly.
                wait = (status.seconds_until(estimated_cost)
                        if status is not None else min(2.0 ** attempt, MAX_BACKOFF_SECONDS))
                self._sleep(min(max(wait, 0.1), MAX_BACKOFF_SECONDS))
                self.total_wait_seconds += wait
            else:
                self._backoff(attempt, retry_after=resp.headers.get("Retry-After"))

        if last_error and (last_error.code == "THROTTLED" or last_error.status == 429):
            raise ShopifyThrottled(
                f"{label} still throttled after {MAX_ATTEMPTS} attempts. Shopify's "
                "limit is a shared cost bucket, so this is the whole app being "
                "slowed, not one noisy query — request fewer fields or smaller "
                "pages rather than retrying harder.",
                code="THROTTLED", layer="errors",
            )
        raise last_error or ShopifyAPIError(f"{label} produced no response.",
                                            layer="http")

    # -- interpretation ----------------------------------------------------
    @staticmethod
    def _interpret(resp: Response, label: str,
                   mutation_field: str) -> ShopifyAPIError | None:
        """Return an error, or None when the call genuinely succeeded."""
        body = resp.body if isinstance(resp.body, dict) else {}
        request_id = str(resp.headers.get("X-Request-Id", "")
                         or resp.headers.get("x-request-id", ""))

        # Layer 1: HTTP.
        if resp.status >= 400:
            hint = SHOP_STATE_HINTS.get(resp.status, "")
            detail = str(resp.body)[:300]
            return ShopifyAPIError(
                f"{label} failed with HTTP {resp.status}: {detail}"
                + (f" — {hint}" if hint else ""),
                status=resp.status, layer="http", request_id=request_id,
            )

        # Layer 2: top-level errors. HTTP 200.
        errors = body.get("errors")
        if errors:
            code, message = _first_error(errors)
            hint = ""
            if code == "THROTTLED":
                hint = " — query cost exceeded the available points in the bucket."
            elif "doesn't exist on type" in message or "Field '" in message:
                hint = (" — this field does not exist in API version "
                        "in use. Shopify expires versions after roughly 12 "
                        "months and moves fields between them.")
            return ShopifyAPIError(
                f"{label} was rejected: {message}{hint}",
                status=resp.status, code=code, layer="errors",
                request_id=request_id,
            )

        # Layer 3: userErrors. HTTP 200, no top-level errors, data populated.
        if mutation_field:
            data = body.get("data") or {}
            result = data.get(mutation_field)
            if result is None:
                return ShopifyAPIError(
                    f"{label} returned no '{mutation_field}' payload. The mutation "
                    "name and the field checked for userErrors must match, or a "
                    "rejected write reads as a success.",
                    status=resp.status, layer="errors", request_id=request_id,
                )
            user_errors = result.get("userErrors") or []
            if user_errors:
                rendered = "; ".join(
                    f"{'.'.join(str(p) for p in (e.get('field') or []))}: {e.get('message', '')}"
                    .lstrip(": ")
                    for e in user_errors
                )
                return ShopifyUserError(
                    f"{label} was refused by Shopify: {rendered}",
                    status=resp.status, layer="user_errors",
                    request_id=request_id, user_errors=user_errors,
                )
        return None

    @staticmethod
    def _read_throttle_status(body: dict[str, Any]) -> ThrottleStatus | None:
        cost = ((body.get("extensions") or {}).get("cost") or {})
        raw = cost.get("throttleStatus")
        if not isinstance(raw, dict):
            return None
        try:
            return ThrottleStatus(
                maximum_available=float(raw.get("maximumAvailable",
                                                DEFAULT_BUCKET_POINTS)),
                currently_available=float(raw.get("currentlyAvailable", 0)),
                restore_rate=float(raw.get("restoreRate", DEFAULT_RESTORE_RATE)),
            )
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _read_actual_cost(body: dict[str, Any]) -> float:
        cost = ((body.get("extensions") or {}).get("cost") or {})
        try:
            return float(cost.get("actualQueryCost") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _backoff(self, attempt: int, *, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                self._sleep(float(retry_after))
                return
            except ValueError:
                pass
        self._sleep(min(2.0 ** attempt, MAX_BACKOFF_SECONDS))

    @staticmethod
    def _urllib_send(*, url: str, headers: dict[str, str], body: str,
                     timeout: float) -> Response:  # pragma: no cover - network
        req = urllib.request.Request(url, data=body.encode("utf-8"),
                                     method="POST", headers=headers)
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
            "query_points_spent": round(self.points_spent, 1),
            "seconds_waiting_on_rate_limits": round(self.total_wait_seconds, 2),
            **self._limiter.snapshot(),
        }


def _first_error(errors: Any) -> tuple[str, str]:
    """Pull a code and message out of Shopify's top-level error array."""
    if isinstance(errors, list) and errors:
        first = errors[0] if isinstance(errors[0], dict) else {}
        code = str((first.get("extensions") or {}).get("code", ""))
        message = str(first.get("message", "")) or json.dumps(first)[:200]
        return code, message
    return "", str(errors)[:200]


def normalise_domain(domain: str) -> str:
    """Accept a bare handle, a full domain, or a pasted admin URL.

    The value is copied out of a browser as often as it is typed, and a scheme
    or trailing path silently produces an unreachable endpoint that looks like
    an auth failure.
    """
    text = (domain or "").strip()
    if not text:
        raise ValueError("Shopify shop domain is empty.")
    if "://" in text:
        text = urllib.parse.urlsplit(text).netloc or text.split("://", 1)[1]
    text = text.split("/")[0].strip().lower()
    if not text:
        raise ValueError(f"Could not read a shop domain out of {domain!r}.")
    if "." not in text:
        text = f"{text}.myshopify.com"
    return text
