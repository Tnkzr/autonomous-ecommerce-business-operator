"""HTTP transport: rate limiting, retry, and error translation.

SP-API rate limits are per-operation token buckets, not a global request count.
Exceeding them returns 429, and sustained 429s get an application flagged for
review — so the limiter is proactive (wait before sending) rather than reactive
(send, get rejected, back off).

The declared limits below are Amazon's documented defaults. The authoritative
values are the `x-amzn-RateLimit-Limit` response headers, which vary by seller
and change without notice, so the limiter updates itself from those headers as
responses arrive.
"""

from __future__ import annotations

import gzip
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

# operation -> (requests_per_second, burst)
# Conservative defaults from the SP-API usage-plan documentation.
RATE_LIMITS: dict[str, tuple[float, int]] = {
    "getMarketplaceParticipations": (0.016, 15),
    "searchCatalogItems": (2.0, 2),
    "getCatalogItem": (2.0, 2),
    "getCompetitivePricing": (0.5, 1),
    "getItemOffers": (0.5, 1),
    "getMyFeesEstimateForASIN": (1.0, 2),
    "getInventorySummaries": (2.0, 2),
    "getListingsItem": (5.0, 10),
    "putListingsItem": (5.0, 10),
    "patchListingsItem": (5.0, 5),
    "getOrders": (0.0167, 20),
    "getOrderItems": (0.5, 30),
    "createReport": (0.0167, 15),
    "getReport": (2.0, 15),
    "getReportDocument": (0.0167, 15),
    "createRestrictedDataToken": (1.0, 10),
    "_default": (1.0, 5),
}

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 5


class SPAPIError(RuntimeError):
    """An SP-API call failed. Carries enough context to act on."""

    def __init__(self, message: str, *, status: int | None = None,
                 operation: str = "", errors: list[dict] | None = None,
                 request_id: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.operation = operation
        self.errors = errors or []
        self.request_id = request_id

    @property
    def codes(self) -> list[str]:
        return [e.get("code", "") for e in self.errors]


class RateLimited(SPAPIError):
    """Throttled after exhausting retries."""


class TokenBucket:
    """Classic token bucket. Blocks until a token is available.

    `monotonic` is injectable alongside `sleep` so the two stay consistent. A
    fake sleep paired with a real clock makes the bucket busy-spin until wall
    time catches up, which turns a fast test into a slow flaky one.
    """

    def __init__(self, rate: float, burst: int, *, monotonic=time.monotonic) -> None:
        self.rate = max(rate, 0.0001)
        self.capacity = max(burst, 1)
        self._tokens = float(self.capacity)
        self._monotonic = monotonic
        self._last = monotonic()
        self._lock = threading.Lock()

    def update_limit(self, rate: float) -> None:
        """Adopt the rate Amazon reports for this seller."""
        with self._lock:
            if rate > 0:
                self.rate = rate

    def acquire(self, *, sleep: Callable[[float], None] = time.sleep) -> float:
        """Consume one token, waiting if necessary. Returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
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
    request_id: str = ""


class Transport:
    """Executes SP-API requests with limiting, retry, and error translation.

    `send` is injectable: tests drive the full retry and parsing path with a
    fake, which is the only responsible way to verify this logic when a bug
    means mis-priced listings on a live account.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        token_provider,
        send: Callable[..., Response] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        timeout: float = 30.0,
        user_agent: str = "AutonomousEcommerceOperator/0.2 (Language=Python/3.11)",
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.tokens = token_provider
        self._send = send or self._urllib_send
        self._sleep = sleep
        self._monotonic = monotonic
        self._timeout = timeout
        self._user_agent = user_agent
        self._buckets: dict[str, TokenBucket] = {}
        self._bucket_lock = threading.Lock()
        self.call_count = 0
        self.throttle_count = 0
        self.total_wait_seconds = 0.0

    # -- limiter -----------------------------------------------------------
    def _bucket(self, operation: str) -> TokenBucket:
        with self._bucket_lock:
            if operation not in self._buckets:
                rate, burst = RATE_LIMITS.get(operation, RATE_LIMITS["_default"])
                self._buckets[operation] = TokenBucket(
                    rate, burst, monotonic=self._monotonic
                )
            return self._buckets[operation]

    # -- request -----------------------------------------------------------
    def request(
        self,
        *,
        operation: str,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: dict | None = None,
        extra_headers: dict[str, str] | None = None,
        scope: str | None = None,
    ) -> Any:
        """Issue a request and return the parsed JSON payload."""
        bucket = self._bucket(operation)
        attempt = 0
        last_error: SPAPIError | None = None

        while attempt < MAX_ATTEMPTS:
            attempt += 1
            self.total_wait_seconds += bucket.acquire(sleep=self._sleep)

            token = self.tokens.access_token(scope=scope)
            headers = {
                "x-amz-access-token": token,
                "Accept": "application/json",
                "User-Agent": self._user_agent,
            }
            if body is not None:
                headers["Content-Type"] = "application/json"
            if extra_headers:
                headers.update(extra_headers)

            url = self._build_url(path, query)
            self.call_count += 1

            try:
                resp = self._send(
                    method=method, url=url, headers=headers, body=body,
                    timeout=self._timeout,
                )
            except urllib.error.URLError as exc:  # pragma: no cover - network path
                last_error = SPAPIError(
                    f"Network failure calling {operation}: {exc.reason}",
                    operation=operation,
                )
                self._backoff(attempt)
                continue

            self._adopt_reported_limit(bucket, resp)

            if 200 <= resp.status < 300:
                return resp.body

            last_error = self._translate(resp, operation)

            if resp.status == 403:
                # Usually an expired token; re-auth once before giving up.
                self.tokens.invalidate(scope=scope)
                if attempt < MAX_ATTEMPTS:
                    continue

            if resp.status in RETRYABLE_STATUS:
                if resp.status == 429:
                    self.throttle_count += 1
                self._backoff(attempt, retry_after=resp.headers.get("Retry-After"))
                continue

            raise last_error

        if last_error and last_error.status == 429:
            raise RateLimited(
                f"{operation} still throttled after {MAX_ATTEMPTS} attempts. "
                "The operation's rate limit is lower than this job's request "
                "pattern — reduce batch size or spread the run over time rather "
                "than retrying harder; sustained throttling gets applications "
                "flagged.",
                status=429, operation=operation,
            )
        raise last_error or SPAPIError(f"{operation} failed with no response.",
                                       operation=operation)

    # -- helpers -----------------------------------------------------------
    def _build_url(self, path: str, query: dict[str, Any] | None) -> str:
        url = f"{self.endpoint}{path}"
        if query:
            clean = {}
            for k, v in query.items():
                if v is None:
                    continue
                clean[k] = ",".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v)
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean)}"
        return url

    @staticmethod
    def _adopt_reported_limit(bucket: TokenBucket, resp: Response) -> None:
        reported = resp.headers.get("x-amzn-RateLimit-Limit")
        if reported:
            try:
                bucket.update_limit(float(reported))
            except ValueError:
                # A malformed rate-limit header is not worth failing a good
                # response over. Keep the documented default and carry on.
                pass

    def _backoff(self, attempt: int, *, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                self._sleep(float(retry_after))
                return
            except ValueError:
                # Retry-After can be an HTTP-date rather than seconds. Fall
                # through to exponential backoff rather than failing the call
                # over an unparseable header.
                pass
        # Exponential with a ceiling. No jitter needed for a single-tenant job.
        self._sleep(min(2.0 ** attempt, 30.0))

    @staticmethod
    def _translate(resp: Response, operation: str) -> SPAPIError:
        errors = []
        if isinstance(resp.body, dict):
            errors = resp.body.get("errors") or []
        detail = "; ".join(
            f"{e.get('code', '?')}: {e.get('message', '')}"
            + (f" ({e['details']})" if e.get("details") else "")
            for e in errors
        ) or str(resp.body)[:400]

        hint = ""
        if resp.status == 403:
            hint = (
                " — the access token was rejected. Check that the SP-API application "
                "is authorised for this seller and that the role includes the needed "
                "data-access permissions."
            )
        elif resp.status == 404:
            hint = " — resource not found. Verify the SKU/ASIN and marketplace ID."
        elif resp.status == 400:
            hint = (
                " — Amazon rejected the request payload. For listings calls this is "
                "usually a productType/attribute schema mismatch; the error details "
                "name the offending attribute."
            )

        cls = RateLimited if resp.status == 429 else SPAPIError
        return cls(
            f"{operation} failed with HTTP {resp.status}: {detail}{hint}",
            status=resp.status, operation=operation, errors=errors,
            request_id=resp.headers.get("x-amzn-RequestId", ""),
        )

    @staticmethod
    def _urllib_send(*, method: str, url: str, headers: dict[str, str],
                     body: dict | None, timeout: float) -> Response:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                text = raw.decode("utf-8", "replace")
                parsed = json.loads(text) if text.strip() else {}
                return Response(
                    status=resp.status,
                    headers={k: v for k, v in resp.headers.items()},
                    body=parsed,
                    request_id=resp.headers.get("x-amzn-RequestId", ""),
                )
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            text = raw.decode("utf-8", "replace")
            try:
                parsed = json.loads(text) if text.strip() else {}
            except json.JSONDecodeError:
                parsed = {"raw": text[:1000]}
            return Response(
                status=exc.code,
                headers={k: v for k, v in exc.headers.items()} if exc.headers else {},
                body=parsed,
                request_id=(exc.headers or {}).get("x-amzn-RequestId", ""),
            )

    def stats(self) -> dict[str, Any]:
        return {
            "calls": self.call_count,
            "throttled": self.throttle_count,
            "seconds_waiting_on_rate_limits": round(self.total_wait_seconds, 2),
        }
