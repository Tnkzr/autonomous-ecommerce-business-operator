"""SP-API operations.

Each method wraps one Amazon operation, handles its pagination, and returns
parsed JSON. Mapping into the operator's domain models happens one layer up in
`connector.py`, so this file stays a faithful mirror of the API and is easy to
check against Amazon's docs.

Pagination is handled internally with a page cap. An unbounded `while nextToken`
loop against a large catalogue is how a "quick sync" turns into a six-hour
throttled crawl.
"""

from __future__ import annotations

import csv
import gzip
import io
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from .transport import SPAPIError, Transport

MAX_PAGES_DEFAULT = 50


def _iso(dt: datetime) -> str:
    """SP-API wants ISO 8601 with an explicit offset."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class FeeBreakdown:
    """Real Amazon fees for one ASIN at one price. Replaces our estimates."""

    asin: str
    price: float
    currency: str
    referral_fee: float
    fba_fee: float
    variable_closing_fee: float
    per_item_fee: float
    total_fees: float
    is_fba: bool

    @property
    def referral_pct(self) -> float:
        return round(self.referral_fee / self.price * 100, 2) if self.price else 0.0


class SPAPIClient:
    """Typed access to the SP-API operations the operator needs."""

    def __init__(self, transport: Transport, *, marketplace_id: str, seller_id: str) -> None:
        self.t = transport
        self.marketplace_id = marketplace_id
        self.seller_id = seller_id

    # ------------------------------------------------------------------
    # Connection verification
    # ------------------------------------------------------------------
    def get_marketplace_participations(self) -> list[dict]:
        """Sellers API. The cheapest call that proves auth end to end.

        It exercises the full chain — refresh token, LWA exchange, application
        authorisation, region routing — and needs no special role permission,
        which makes it the right probe for `status`.
        """
        data = self.t.request(
            operation="getMarketplaceParticipations",
            method="GET",
            path="/sellers/v1/marketplaceParticipations",
        )
        return data.get("payload", data.get("marketplaceParticipations", []))

    # ------------------------------------------------------------------
    # Catalog / product research
    # ------------------------------------------------------------------
    def search_catalog_items(
        self,
        *,
        keywords: list[str] | None = None,
        identifiers: list[str] | None = None,
        identifiers_type: str = "ASIN",
        brand_names: list[str] | None = None,
        page_size: int = 20,
        max_pages: int = 5,
        included_data: tuple[str, ...] = (
            "summaries", "attributes", "salesRanks", "images", "identifiers",
        ),
    ) -> list[dict]:
        """Catalog Items 2022-04-01 search.

        Either `keywords` (discovery) or `identifiers` (lookup) — Amazon rejects
        both together.
        """
        if keywords and identifiers:
            raise ValueError(
                "searchCatalogItems accepts keywords or identifiers, not both. "
                "Use keywords for discovery and identifiers for lookup."
            )
        if not keywords and not identifiers:
            raise ValueError("searchCatalogItems needs keywords or identifiers.")

        query: dict[str, Any] = {
            "marketplaceIds": self.marketplace_id,
            "includedData": ",".join(included_data),
            "pageSize": min(page_size, 20),
        }
        if keywords:
            query["keywords"] = ",".join(keywords)
        if identifiers:
            query["identifiers"] = ",".join(identifiers)
            query["identifiersType"] = identifiers_type
        if brand_names:
            query["brandNames"] = ",".join(brand_names)

        items: list[dict] = []
        token: str | None = None
        for _ in range(max_pages):
            if token:
                query["pageToken"] = token
            data = self.t.request(
                operation="searchCatalogItems", method="GET",
                path="/catalog/2022-04-01/items", query=query,
            )
            items.extend(data.get("items", []))
            token = (data.get("pagination") or {}).get("nextToken")
            if not token:
                break
        return items

    def get_catalog_item(self, asin: str, *, included_data: tuple[str, ...] = (
        "summaries", "attributes", "salesRanks", "images", "productTypes",
        "dimensions", "relationships",
    )) -> dict:
        return self.t.request(
            operation="getCatalogItem", method="GET",
            path=f"/catalog/2022-04-01/items/{urllib.parse.quote(asin)}",
            query={
                "marketplaceIds": self.marketplace_id,
                "includedData": ",".join(included_data),
            },
        )

    # ------------------------------------------------------------------
    # Competitive pricing
    # ------------------------------------------------------------------
    def get_competitive_pricing(self, asins: list[str]) -> list[dict]:
        """Product Pricing v0. Batches of 20 ASINs maximum."""
        out: list[dict] = []
        for i in range(0, len(asins), 20):
            batch = asins[i:i + 20]
            data = self.t.request(
                operation="getCompetitivePricing", method="GET",
                path="/products/pricing/v0/competitivePrice",
                query={
                    "MarketplaceId": self.marketplace_id,
                    "Asins": ",".join(batch),
                    "ItemType": "Asin",
                },
            )
            out.extend(data.get("payload", []))
        return out

    def get_item_offers(self, asin: str, *, condition: str = "New") -> dict:
        """Full offer list for one ASIN — the input to the repricer."""
        data = self.t.request(
            operation="getItemOffers", method="GET",
            path=f"/products/pricing/v0/items/{urllib.parse.quote(asin)}/offers",
            query={"MarketplaceId": self.marketplace_id, "ItemCondition": condition},
        )
        return data.get("payload", data)

    # ------------------------------------------------------------------
    # Fees — the highest-value calibration data in the system
    # ------------------------------------------------------------------
    def get_fees_estimate(self, asin: str, price: float, *, currency: str = "USD",
                          is_fba: bool = True) -> FeeBreakdown:
        """Product Fees v0. Amazon's own fee calculation for a given price.

        This is what replaces the estimated `[fees.amazon]` values in policy:
        the referral percentage and FBA fee here are the actual ones Amazon will
        charge, per ASIN, at that price point.
        """
        body = {
            "FeesEstimateRequest": {
                "MarketplaceId": self.marketplace_id,
                "IsAmazonFulfilled": is_fba,
                "Identifier": f"fees-{asin}-{int(price * 100)}",
                "PriceToEstimateFees": {
                    "ListingPrice": {"CurrencyCode": currency, "Amount": price},
                },
            }
        }
        data = self.t.request(
            operation="getMyFeesEstimateForASIN", method="POST",
            path=f"/products/fees/v0/items/{urllib.parse.quote(asin)}/feesEstimate",
            body=body,
        )
        payload = data.get("payload", data)
        result = payload.get("FeesEstimateResult", payload)

        status = result.get("Status")
        if status and status != "Success":
            err = result.get("Error", {})
            raise SPAPIError(
                f"Fee estimate for {asin} returned status {status}: "
                f"{err.get('Code', '')} {err.get('Message', '')}. "
                "Do not substitute an estimate here — a wrong fee silently "
                "changes every downstream profit calculation.",
                operation="getMyFeesEstimateForASIN",
            )

        estimate = result.get("FeesEstimate", {})
        details = estimate.get("FeeDetailList", []) or []
        by_type = {d.get("FeeType"): d for d in details}

        def amount(fee_type: str) -> float:
            entry = by_type.get(fee_type) or {}
            return float((entry.get("FinalFee") or {}).get("Amount", 0.0))

        total = float((estimate.get("TotalFeesEstimate") or {}).get("Amount", 0.0))
        return FeeBreakdown(
            asin=asin,
            price=price,
            currency=currency,
            referral_fee=amount("ReferralFee"),
            fba_fee=amount("FBAFees") or amount("FulfillmentFees"),
            variable_closing_fee=amount("VariableClosingFee"),
            per_item_fee=amount("PerItemFee"),
            total_fees=total,
            is_fba=is_fba,
        )

    # ------------------------------------------------------------------
    # Inventory
    # ------------------------------------------------------------------
    def get_inventory_summaries(self, *, skus: list[str] | None = None,
                                max_pages: int = MAX_PAGES_DEFAULT) -> list[dict]:
        """FBA Inventory v1, with details so reserved/inbound are populated."""
        query: dict[str, Any] = {
            "details": "true",
            "granularityType": "Marketplace",
            "granularityId": self.marketplace_id,
            "marketplaceIds": self.marketplace_id,
        }
        if skus:
            query["sellerSkus"] = ",".join(skus[:50])

        out: list[dict] = []
        token: str | None = None
        for _ in range(max_pages):
            if token:
                query["nextToken"] = token
            data = self.t.request(
                operation="getInventorySummaries", method="GET",
                path="/fba/inventory/v1/summaries", query=query,
            )
            payload = data.get("payload", {})
            out.extend(payload.get("inventorySummaries", []))
            token = (data.get("pagination") or {}).get("nextToken")
            if not token:
                break
        return out

    # ------------------------------------------------------------------
    # Listings
    # ------------------------------------------------------------------
    def get_listings_item(self, sku: str, *, included_data: tuple[str, ...] = (
        "summaries", "attributes", "issues", "offers", "fulfillmentAvailability",
    )) -> dict:
        return self.t.request(
            operation="getListingsItem", method="GET",
            path=f"/listings/2021-08-01/items/{self.seller_id}/{urllib.parse.quote(sku, safe='')}",
            query={
                "marketplaceIds": self.marketplace_id,
                "includedData": ",".join(included_data),
            },
        )

    def put_listings_item(self, sku: str, *, product_type: str, attributes: dict,
                          requirements: str = "LISTING",
                          issue_locale: str = "en_US") -> dict:
        """Create or fully replace a listing.

        PUT is a full replace: attributes omitted here are removed. Use
        `patch_listings_item` for targeted edits such as price.
        """
        return self.t.request(
            operation="putListingsItem", method="PUT",
            path=f"/listings/2021-08-01/items/{self.seller_id}/{urllib.parse.quote(sku, safe='')}",
            query={"marketplaceIds": self.marketplace_id, "issueLocale": issue_locale},
            body={
                "productType": product_type,
                "requirements": requirements,
                "attributes": attributes,
            },
        )

    def patch_listings_item(self, sku: str, *, product_type: str,
                            patches: list[dict], issue_locale: str = "en_US") -> dict:
        return self.t.request(
            operation="patchListingsItem", method="PATCH",
            path=f"/listings/2021-08-01/items/{self.seller_id}/{urllib.parse.quote(sku, safe='')}",
            query={"marketplaceIds": self.marketplace_id, "issueLocale": issue_locale},
            body={"productType": product_type, "patches": patches},
        )

    def update_price(self, sku: str, price: float, *, product_type: str,
                     currency: str = "USD") -> dict:
        """Patch only the offer price.

        `purchasable_offer` is the correct attribute for the seller's price on
        the 2021-08-01 Listings API. Patching `list_price` instead is a common
        and expensive mistake — that field is the manufacturer's list price and
        changing it does not move what customers pay.
        """
        patches = [{
            "op": "replace",
            "path": "/attributes/purchasable_offer",
            "value": [{
                "marketplace_id": self.marketplace_id,
                "currency": currency,
                "our_price": [{"schedule": [{"value_with_tax": round(price, 2)}]}],
            }],
        }]
        return self.patch_listings_item(sku, product_type=product_type, patches=patches)

    def update_quantity(self, sku: str, quantity: int, *, product_type: str) -> dict:
        patches = [{
            "op": "replace",
            "path": "/attributes/fulfillment_availability",
            "value": [{
                "fulfillment_channel_code": "DEFAULT",
                "quantity": int(quantity),
            }],
        }]
        return self.patch_listings_item(sku, product_type=product_type, patches=patches)

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    def get_orders(self, *, created_after: datetime, created_before: datetime | None = None,
                   statuses: list[str] | None = None,
                   max_pages: int = MAX_PAGES_DEFAULT,
                   restricted_data_token: str | None = None) -> list[dict]:
        """Orders v0.

        Buyer PII (name, address, email) is omitted unless a Restricted Data
        Token is supplied. The operator does not need PII for any decision it
        makes, so the default is to run without one — the less customer data
        this system touches, the smaller the breach surface.
        """
        query: dict[str, Any] = {
            "MarketplaceIds": self.marketplace_id,
            "CreatedAfter": _iso(created_after),
        }
        if created_before:
            query["CreatedBefore"] = _iso(created_before)
        if statuses:
            query["OrderStatuses"] = ",".join(statuses)

        headers = {"x-amz-access-token": restricted_data_token} if restricted_data_token else None

        out: list[dict] = []
        token: str | None = None
        for _ in range(max_pages):
            if token:
                query = {"MarketplaceIds": self.marketplace_id, "NextToken": token}
            data = self.t.request(
                operation="getOrders", method="GET", path="/orders/v0/orders",
                query=query, extra_headers=headers,
            )
            payload = data.get("payload", {})
            out.extend(payload.get("Orders", []))
            token = payload.get("NextToken")
            if not token:
                break
        return out

    def get_order_items(self, order_id: str, *,
                        max_pages: int = MAX_PAGES_DEFAULT) -> list[dict]:
        out: list[dict] = []
        query: dict[str, Any] = {}
        token: str | None = None
        for _ in range(max_pages):
            if token:
                query["NextToken"] = token
            data = self.t.request(
                operation="getOrderItems", method="GET",
                path=f"/orders/v0/orders/{urllib.parse.quote(order_id)}/orderItems",
                query=query or None,
            )
            payload = data.get("payload", {})
            out.extend(payload.get("OrderItems", []))
            token = payload.get("NextToken")
            if not token:
                break
        return out

    def create_restricted_data_token(self, *, path: str, data_elements: list[str],
                                     method: str = "GET") -> str:
        """Mint an RDT for a PII-bearing operation. Valid for one hour."""
        data = self.t.request(
            operation="createRestrictedDataToken", method="POST",
            path="/tokens/2021-03-01/restrictedDataToken",
            body={"restrictedResources": [
                {"method": method, "path": path, "dataElements": data_elements}
            ]},
        )
        token = data.get("restrictedDataToken")
        if not token:
            raise SPAPIError("RDT request returned no token.",
                             operation="createRestrictedDataToken")
        return token

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------
    def create_report(self, report_type: str, *, start: datetime | None = None,
                      end: datetime | None = None,
                      options: dict | None = None) -> str:
        body: dict[str, Any] = {
            "reportType": report_type,
            "marketplaceIds": [self.marketplace_id],
        }
        if start:
            body["dataStartTime"] = _iso(start)
        if end:
            body["dataEndTime"] = _iso(end)
        if options:
            body["reportOptions"] = options

        data = self.t.request(
            operation="createReport", method="POST",
            path="/reports/2021-06-30/reports", body=body,
        )
        report_id = data.get("reportId")
        if not report_id:
            raise SPAPIError(f"createReport returned no reportId: {data}",
                             operation="createReport")
        return report_id

    def get_report(self, report_id: str) -> dict:
        return self.t.request(
            operation="getReport", method="GET",
            path=f"/reports/2021-06-30/reports/{urllib.parse.quote(report_id)}",
        )

    def wait_for_report(self, report_id: str, *, timeout_seconds: float = 900,
                        poll_seconds: float = 30,
                        sleep=time.sleep, monotonic=time.monotonic) -> dict:
        """Poll until the report is done.

        Report generation is asynchronous and can take minutes; there is no
        webhook for it. FATAL and CANCELLED are terminal and must not be
        retried — a FATAL report usually means the request parameters were
        invalid, so retrying just burns the createReport rate limit.
        """
        deadline = monotonic() + timeout_seconds
        while True:
            meta = self.get_report(report_id)
            status = meta.get("processingStatus")
            if status == "DONE":
                return meta
            if status in ("FATAL", "CANCELLED"):
                raise SPAPIError(
                    f"Report {report_id} finished with status {status}. "
                    "This is terminal — check the report type, date range, and "
                    "that the seller has data for the requested period.",
                    operation="getReport",
                )
            if monotonic() > deadline:
                raise SPAPIError(
                    f"Report {report_id} still {status} after {timeout_seconds:.0f}s. "
                    "Large date ranges can exceed this; increase the timeout or "
                    "narrow the range.",
                    operation="getReport",
                )
            sleep(poll_seconds)

    def get_report_document(self, document_id: str, *, fetch=None) -> str:
        """Download and decompress a report document.

        The document URL is a pre-signed S3 link, valid ~5 minutes, and is
        fetched without SP-API auth headers. Sending the access token to S3
        would leak the credential to a third-party host.
        """
        meta = self.t.request(
            operation="getReportDocument", method="GET",
            path=f"/reports/2021-06-30/documents/{urllib.parse.quote(document_id)}",
        )
        url = meta.get("url")
        if not url:
            raise SPAPIError(f"Report document {document_id} had no URL.",
                             operation="getReportDocument")

        fetcher = fetch or self._fetch_document
        raw = fetcher(url)
        if meta.get("compressionAlgorithm") == "GZIP":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "replace")

    @staticmethod
    def _fetch_document(url: str) -> bytes:  # pragma: no cover - network path
        with urllib.request.urlopen(url, timeout=120) as resp:
            return resp.read()

    @staticmethod
    def parse_tab_report(text: str) -> list[dict[str, str]]:
        """Most Amazon flat-file reports are tab-separated with a header row."""
        if not text.strip():
            return []
        reader = csv.DictReader(io.StringIO(text), delimiter="\t")
        return [dict(row) for row in reader]

    def fetch_report(self, report_type: str, *, start: datetime | None = None,
                     end: datetime | None = None, options: dict | None = None,
                     sleep=time.sleep) -> list[dict[str, str]]:
        """Create → wait → download → parse, in one call."""
        report_id = self.create_report(report_type, start=start, end=end, options=options)
        meta = self.wait_for_report(report_id, sleep=sleep)
        document_id = meta.get("reportDocumentId")
        if not document_id:
            raise SPAPIError(f"Report {report_id} completed without a document id.",
                             operation="getReport")
        return self.parse_tab_report(self.get_report_document(document_id))


# Report types the operator uses.
REPORT_SALES_AND_TRAFFIC = "GET_SALES_AND_TRAFFIC_REPORT"
REPORT_ALL_ORDERS = "GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL"
REPORT_FBA_INVENTORY = "GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA"
REPORT_MERCHANT_LISTINGS = "GET_MERCHANT_LISTINGS_ALL_DATA"
REPORT_FEE_PREVIEW = "GET_FBA_ESTIMATED_FBA_FEES_TXT_DATA"
REPORT_RETURNS = "GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA"
REPORT_SETTLEMENT = "GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE"
