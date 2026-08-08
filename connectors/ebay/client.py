"""eBay Sell and Buy API client — mirrors the API, no business logic.

Five APIs, each metered separately by eBay and so each named as its own quota
bucket:

- **Fulfillment** — orders. The seller's actual sales.
- **Inventory** — inventory items, offers, publishing. The modern listing path;
  the older Trading API XML flow is deliberately not used.
- **Finances** — real transaction fees and payouts. This is the only honest
  source for what eBay actually charged, and is what makes the estimated
  `[fees.ebay]` schedule replaceable with measured numbers.
- **Analytics** — seller standards, traffic. Also where the real call quotas
  come from.
- **Browse** — public catalogue search. The legal source for competitor
  pricing, and the reason this connector can answer a question Shopify and
  TikTok both have to refuse.

Two conventions worth keeping.

**Pagination is offset-based and must be exhausted.** eBay caps a page and
reports `total`; a caller that reads the first page and stops sees part of the
catalogue and treats it as all of it. Every list method walks to the end.

**Browse takes an application token, everything else takes a user token.** It
is passed explicitly on each call rather than inferred, because inferring it is
exactly how a Sell call goes out with the wrong credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from .auth import APPLICATION, USER
from .transport import EbayAPIError, Transport

PAGE_SIZE = 100
# Browse caps at 200 and charges the same quota either way, so take the max —
# fewer calls against a daily budget is the whole game here.
BROWSE_PAGE_SIZE = 200
MAX_PAGES = 200


@dataclass
class SellerProfile:
    username: str
    marketplace: str
    standards_level: str
    is_top_rated: bool
    defect_rate_pct: float | None
    late_shipment_rate_pct: float | None
    cases_not_resolved_pct: float | None
    transaction_count: int | None


class EbayClient:
    """One method per API operation. Returns eBay's shapes, not ours."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    # -- paging ------------------------------------------------------------
    def _paginate(self, path: str, collection: str, *, api: str,
                  query: dict[str, Any] | None = None,
                  page_size: int = PAGE_SIZE,
                  token_kind: str = USER) -> Iterator[dict[str, Any]]:
        """Walk an offset-paginated collection to exhaustion."""
        offset = 0
        for page in range(MAX_PAGES):
            params = dict(query or {})
            params.update({"limit": page_size, "offset": offset})
            body = self.transport.request(method="GET", path=path, query=params,
                                          api=api, token_kind=token_kind)
            items = body.get(collection) or []
            for item in items:
                yield item

            total = body.get("total")
            offset += len(items)
            if not items:
                return
            if total is not None:
                try:
                    if offset >= int(total):
                        return
                except (TypeError, ValueError):
                    pass
            elif len(items) < page_size:
                return
            if not body.get("next") and total is None:
                return
        raise EbayAPIError(
            f"{path} exceeded {MAX_PAGES} pages. Stopping rather than paging "
            "forever — narrow the filter.", endpoint=path)

    # -- identity / quota --------------------------------------------------
    def seller_standards(self, *, program: str = "PROGRAM_US",
                         cycle: str = "CURRENT") -> SellerProfile:
        """Seller standards profile. Proves the user token works.

        Used for verification because it is cheap, read-only, and requires the
        seller scope — so a success here means the whole Sell path is wired,
        not merely that a token was minted.
        """
        body = self.transport.request(
            method="GET",
            path=f"/sell/analytics/v1/seller_standards_profile/{program}/{cycle}",
            api="analytics", token_kind=USER)
        metrics = {m.get("metricKey"): m for m in (body.get("metrics") or [])
                   if isinstance(m, dict)}

        def rate(key: str) -> float | None:
            entry = metrics.get(key) or {}
            value = (entry.get("value") or {})
            raw = value.get("value") if isinstance(value, dict) else value
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None

        return SellerProfile(
            username=str(body.get("username", "")),
            marketplace=str(body.get("program", "")),
            standards_level=str(body.get("standardsLevel", "")),
            is_top_rated=str(body.get("standardsLevel", "")).upper() == "TOP_RATED",
            defect_rate_pct=rate("DEFECTIVE_TRANSACTION_RATE"),
            late_shipment_rate_pct=rate("SHIPPING_MISS_RATE"),
            cases_not_resolved_pct=rate("CASES_NOT_RESOLVED_RATE"),
            transaction_count=None,
        )

    def rate_limits(self) -> list[dict[str, Any]]:
        """Real call quotas from the Developer Analytics API.

        The only way to know a seller's actual entitlement. Without it the
        transport runs on documented defaults, which are a guess about somebody
        else's application.
        """
        body = self.transport.request(
            method="GET", path="/developer/analytics/v1_beta/rate_limit",
            api="analytics", token_kind=APPLICATION)
        return body.get("rateLimits") or []

    # -- orders ------------------------------------------------------------
    def orders(self, *, since: str = "", order_filter: str = "") -> list[dict[str, Any]]:
        """Orders from the Fulfillment API.

        `since` is an ISO timestamp; eBay wants it inside a `filter` expression
        rather than as a plain parameter, which is easy to get wrong and fails
        by returning everything rather than by erroring.
        """
        filters = []
        if order_filter:
            filters.append(order_filter)
        elif since:
            filters.append(f"creationdate:[{_ebay_timestamp(since)}..]")
        query = {"filter": ",".join(filters) or None}
        return list(self._paginate("/sell/fulfillment/v1/order", "orders",
                                   api="fulfillment", query=query, page_size=50))

    # -- inventory / listings ----------------------------------------------
    def inventory_items(self) -> list[dict[str, Any]]:
        return list(self._paginate("/sell/inventory/v1/inventory_item",
                                   "inventoryItems", api="inventory"))

    def offers(self, sku: str) -> list[dict[str, Any]]:
        body = self.transport.request(
            method="GET", path="/sell/inventory/v1/offer",
            query={"sku": sku}, api="inventory", token_kind=USER)
        return body.get("offers") or []

    def create_or_replace_inventory_item(self, sku: str,
                                         payload: dict[str, Any]) -> dict[str, Any]:
        """PUT an inventory item. Idempotent by SKU, which is why it is a PUT.

        Creating the item does not list anything — an inventory item with no
        published offer is invisible to buyers. That separation is what makes
        it safe to run without approval.
        """
        return self.transport.request(
            method="PUT", path=f"/sell/inventory/v1/inventory_item/{sku}",
            body=payload, api="inventory", token_kind=USER,
            extra_headers={"Content-Language": "en-US"})

    def create_offer(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.transport.request(
            method="POST", path="/sell/inventory/v1/offer", body=payload,
            api="inventory", token_kind=USER,
            extra_headers={"Content-Language": "en-US"})

    def update_offer(self, offer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.transport.request(
            method="PUT", path=f"/sell/inventory/v1/offer/{offer_id}",
            body=payload, api="inventory", token_kind=USER,
            extra_headers={"Content-Language": "en-US"})

    def publish_offer(self, offer_id: str) -> dict[str, Any]:
        """Publish an offer. This is the irreversible step — it goes live."""
        return self.transport.request(
            method="POST", path=f"/sell/inventory/v1/offer/{offer_id}/publish",
            api="inventory", token_kind=USER)

    def withdraw_offer(self, offer_id: str) -> dict[str, Any]:
        """End the listing but keep the offer. The reverse of publish."""
        return self.transport.request(
            method="POST", path=f"/sell/inventory/v1/offer/{offer_id}/withdraw",
            api="inventory", token_kind=USER)

    # -- finances ----------------------------------------------------------
    def transactions(self, *, since: str = "",
                     transaction_type: str = "") -> list[dict[str, Any]]:
        """Real fees and payouts. The source of truth for what eBay charged.

        This is what turns the estimated `[fees.ebay]` schedule into measured
        numbers — the single highest-value calibration available, because every
        margin, floor price and screening verdict is computed from it.
        """
        filters = []
        if since:
            filters.append(f"transactionDate:[{_ebay_timestamp(since)}..]")
        if transaction_type:
            filters.append(f"transactionType:{{{transaction_type}}}")
        query = {"filter": ",".join(filters) or None}
        return list(self._paginate("/sell/finances/v1/transaction", "transactions",
                                   api="finances", query=query, page_size=200))

    # -- browse (competitor data) ------------------------------------------
    def search_items(self, query_text: str, *, limit: int = BROWSE_PAGE_SIZE,
                     category_ids: str = "",
                     filters: str = "") -> list[dict[str, Any]]:
        """Search the public catalogue. The legal source for competitor prices.

        Uses an application token: Browse is public data and does not need — or
        want — the seller's credentials. Deliberately not paginated to
        exhaustion: a competitor scan is a sample, and walking every result of
        a broad query would spend a day's quota on one lookup.
        """
        params: dict[str, Any] = {"q": query_text,
                                  "limit": min(limit, BROWSE_PAGE_SIZE)}
        if category_ids:
            params["category_ids"] = category_ids
        if filters:
            params["filter"] = filters
        body = self.transport.request(
            method="GET", path="/buy/browse/v1/item_summary/search",
            query=params, api="browse", token_kind=APPLICATION)
        return body.get("itemSummaries") or []

    def item(self, item_id: str) -> dict[str, Any]:
        return self.transport.request(
            method="GET", path=f"/buy/browse/v1/item/{item_id}",
            api="browse", token_kind=APPLICATION)


def _ebay_timestamp(value: str) -> str:
    """Normalise a date or datetime to the UTC form eBay's filters expect.

    A bare date is accepted and widened to midnight UTC, because that is how
    callers naturally express "since the first of the month" and eBay rejects
    it without the time component.
    """
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) == 10:
        return f"{text}T00:00:00.000Z"
    if text.endswith("Z"):
        return text
    if "+" in text[10:]:
        return text.split("+")[0] + "Z"
    return text if text.endswith("Z") else f"{text}Z"
