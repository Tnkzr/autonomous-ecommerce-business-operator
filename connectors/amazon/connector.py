"""AmazonConnector: SP-API wired into the operator's domain models.

Every read returns a `DataEnvelope` tagged `live`, because it genuinely is.
Every write still passes `require_write_permission()` first — the API calls
below are real and will change a live account, which is exactly why the gate
stays. Implementing a write and authorising a write are different things.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from ..base import ConnectorNotConfigured, DataEnvelope, MarketplaceConnector
from .auth import LWACredentials, TokenProvider
from .client import (
    REPORT_MERCHANT_LISTINGS,
    REPORT_RETURNS,
    REPORT_SALES_AND_TRAFFIC,
    FeeBreakdown,
    SPAPIClient,
)
from .regions import resolve
from .transport import SPAPIError, Transport


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class AmazonConnector(MarketplaceConnector):
    """Amazon Seller Central via SP-API.

    Endpoints used: Sellers v1, Catalog Items 2022-04-01, Product Pricing v0,
    Product Fees v0, FBA Inventory v1, Listings Items 2021-08-01, Orders v0,
    Reports 2021-06-30, Tokens 2021-03-01.
    """

    name = "amazon"
    required_env = (
        "AMZ_LWA_CLIENT_ID", "AMZ_LWA_CLIENT_SECRET", "AMZ_REFRESH_TOKEN",
        "AMZ_SELLER_ID", "AMZ_MARKETPLACE_ID",
    )
    docs_url = "https://developer-docs.amazon.com/sp-api/"

    def __init__(self, *, allow_writes: bool = False, transport: Transport | None = None,
                 client: SPAPIClient | None = None) -> None:
        super().__init__(allow_writes=allow_writes)
        self._client = client
        self._transport = transport
        self._verified: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # wiring
    # ------------------------------------------------------------------
    @property
    def marketplace_id(self) -> str:
        return os.environ.get("AMZ_MARKETPLACE_ID", "")

    @property
    def seller_id(self) -> str:
        return os.environ.get("AMZ_SELLER_ID", "")

    @property
    def sandbox(self) -> bool:
        return os.environ.get("AMZ_SANDBOX", "").lower() in ("1", "true", "yes")

    def client(self) -> SPAPIClient:
        """Build (and cache) the API client. Raises if credentials are absent."""
        if self._client is not None:
            return self._client

        self.require_credentials()
        endpoint, _country, _currency = resolve(self.marketplace_id, sandbox=self.sandbox)

        tokens = TokenProvider(LWACredentials(
            client_id=os.environ["AMZ_LWA_CLIENT_ID"],
            client_secret=os.environ["AMZ_LWA_CLIENT_SECRET"],
            refresh_token=os.environ["AMZ_REFRESH_TOKEN"],
        ))
        transport = self._transport or Transport(endpoint=endpoint, token_provider=tokens)
        self._client = SPAPIClient(
            transport,
            marketplace_id=self.marketplace_id,
            seller_id=self.seller_id,
        )
        return self._client

    @property
    def currency(self) -> str:
        try:
            return resolve(self.marketplace_id, sandbox=self.sandbox)[2]
        except Exception:
            return "USD"

    # ------------------------------------------------------------------
    # connection verification
    # ------------------------------------------------------------------
    def verify_connection(self) -> dict[str, Any]:
        """Prove the credentials work, end to end.

        Returns a structured result rather than raising, so `status` can print a
        diagnosis for every marketplace instead of dying on the first one.
        """
        result: dict[str, Any] = {
            "marketplace": self.name,
            "ok": False,
            "checked_at": _now(),
            "detail": "",
            "seller_marketplaces": [],
            "sandbox": self.sandbox,
        }

        missing = self.missing_credentials()
        if missing:
            result["detail"] = f"Missing credentials: {', '.join(missing)}"
            return result

        try:
            endpoint, country, currency = resolve(self.marketplace_id, sandbox=self.sandbox)
        except ValueError as exc:
            result["detail"] = str(exc)
            return result
        result["endpoint"] = endpoint
        result["country"] = country
        result["currency"] = currency

        try:
            participations = self.client().get_marketplace_participations()
        except SPAPIError as exc:
            result["detail"] = str(exc)
            result["status_code"] = exc.status
            return result
        except Exception as exc:  # auth/network/config failures
            result["detail"] = f"{type(exc).__name__}: {exc}"
            return result

        ids = []
        for entry in participations:
            mp = entry.get("marketplace", {}) if isinstance(entry, dict) else {}
            mid = mp.get("id")
            if mid:
                ids.append(mid)
        result["seller_marketplaces"] = ids

        if ids and self.marketplace_id not in ids:
            # Authenticated, but pointed at a marketplace this seller does not
            # sell in — every subsequent read would return empty and look like
            # a dead business rather than a misconfiguration.
            result["detail"] = (
                f"Authenticated, but AMZ_MARKETPLACE_ID={self.marketplace_id} is not "
                f"among this seller's marketplaces ({', '.join(ids)}). Reads would "
                "return empty results that look like zero sales."
            )
            return result

        result["ok"] = True
        result["detail"] = (
            f"Authenticated to {country} ({self.marketplace_id})"
            + (" [SANDBOX]" if self.sandbox else "")
        )
        self._verified = result
        return result

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def fetch_orders(self, *, since: str) -> DataEnvelope:
        c = self.client()
        created_after = self._parse_since(since)
        orders = c.get_orders(created_after=created_after)

        rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        for o in orders:
            total = (o.get("OrderTotal") or {})
            rows.append({
                "order_id": o.get("AmazonOrderId"),
                "purchase_date": o.get("PurchaseDate"),
                "status": o.get("OrderStatus"),
                "channel": o.get("FulfillmentChannel"),
                "items_shipped": o.get("NumberOfItemsShipped", 0),
                "items_unshipped": o.get("NumberOfItemsUnshipped", 0),
                "order_total": _f(total.get("Amount")),
                "currency": total.get("CurrencyCode", self.currency),
                "is_business": o.get("IsBusinessOrder", False),
                "is_replacement": o.get("IsReplacementOrder", False),
            })

        # OrderTotal is absent on pending orders; treating that as zero revenue
        # understates the day, so it is surfaced rather than silently summed.
        pending = sum(1 for o in orders if not o.get("OrderTotal"))
        if pending:
            warnings.append(
                f"{pending} order(s) have no OrderTotal yet (typically Pending). "
                "Their revenue is not counted until they settle."
            )

        return DataEnvelope(source="live", marketplace=self.name, fetched_at=_now(),
                            payload=rows, warnings=warnings)

    def fetch_order_items(self, order_id: str) -> DataEnvelope:
        items = self.client().get_order_items(order_id)
        rows = [{
            "order_id": order_id,
            "sku": i.get("SellerSKU"),
            "asin": i.get("ASIN"),
            "title": i.get("Title"),
            "quantity_ordered": i.get("QuantityOrdered", 0),
            "quantity_shipped": i.get("QuantityShipped", 0),
            "item_price": _f((i.get("ItemPrice") or {}).get("Amount")),
            "item_tax": _f((i.get("ItemTax") or {}).get("Amount")),
            "promotion_discount": _f((i.get("PromotionDiscount") or {}).get("Amount")),
        } for i in items]
        return DataEnvelope(source="live", marketplace=self.name, fetched_at=_now(),
                            payload=rows)

    def fetch_inventory(self) -> DataEnvelope:
        summaries = self.client().get_inventory_summaries()
        rows: list[dict[str, Any]] = []
        warnings: list[str] = []

        for s in summaries:
            details = s.get("inventoryDetails", {}) or {}
            reserved = details.get("reservedQuantity", {}) or {}
            researching = details.get("researchingQuantity", {}) or {}
            unfulfillable = details.get("unfulfillableQuantity", {}) or {}

            inbound = (
                int(details.get("inboundWorkingQuantity", 0) or 0)
                + int(details.get("inboundShippedQuantity", 0) or 0)
                + int(details.get("inboundReceivingQuantity", 0) or 0)
            )
            fulfillable = int(details.get("fulfillableQuantity", 0) or 0)

            rows.append({
                "sku": s.get("sellerSku"),
                "asin": s.get("asin"),
                "fnsku": s.get("fnSku"),
                "condition": s.get("condition"),
                "on_hand_units": fulfillable,
                "inbound_units": inbound,
                "reserved_units": int(reserved.get("totalReservedQuantity", 0) or 0),
                "unfulfillable_units": int(
                    unfulfillable.get("totalUnfulfillableQuantity", 0) or 0),
                "researching_units": int(
                    researching.get("totalResearchingQuantity", 0) or 0),
                "total_units": int(s.get("totalQuantity", 0) or 0),
                "product_name": s.get("productName"),
            })

        stranded = [r["sku"] for r in rows if r["unfulfillable_units"] > 0]
        if stranded:
            warnings.append(
                f"{len(stranded)} SKU(s) hold unfulfillable stock: {', '.join(stranded[:5])}"
                + ("…" if len(stranded) > 5 else "")
                + ". Unfulfillable units accrue storage fees while being unsellable — "
                "create a removal order or fix the listing defect."
            )

        return DataEnvelope(source="live", marketplace=self.name, fetched_at=_now(),
                            payload=rows, warnings=warnings)

    def fetch_listings(self) -> DataEnvelope:
        """Enumerate the catalogue via the merchant listings report.

        Listings Items has no bulk 'list everything' operation — it is per-SKU —
        so the report is the only way to enumerate. It must be the *merchant*
        listings report, not the FBA inventory one: the FBA report omits
        merchant-fulfilled SKUs entirely, which would silently hide part of the
        catalogue from every downstream engine.
        """
        rows = self.client().fetch_report(REPORT_MERCHANT_LISTINGS)
        listings = [{
            "sku": r.get("seller-sku"),
            "asin": r.get("asin1") or r.get("product-id"),
            "listing_id": r.get("listing-id"),
            "title": r.get("item-name"),
            "price": _f(r.get("price")),
            "quantity": int(_f(r.get("quantity"))),
            "status": r.get("status"),
            "fulfillment_channel": r.get("fulfillment-channel"),
            "open_date": r.get("open-date"),
        } for r in rows]

        warnings = []
        inactive = [x["sku"] for x in listings
                    if (x["status"] or "").lower() not in ("active", "")]
        if inactive:
            warnings.append(
                f"{len(inactive)} listing(s) are not Active: {', '.join(inactive[:5])}"
                + ("…" if len(inactive) > 5 else "")
                + ". Inactive listings make no sales but still hold inventory."
            )
        return DataEnvelope(source="live", marketplace=self.name, fetched_at=_now(),
                            payload=listings, warnings=warnings)

    def fetch_listing(self, sku: str) -> DataEnvelope:
        data = self.client().get_listings_item(sku)
        issues = data.get("issues", []) or []
        warnings = [
            f"{i.get('severity', 'INFO')}: {i.get('message', '')}"
            for i in issues if i.get("severity") in ("ERROR", "WARNING")
        ]
        return DataEnvelope(source="live", marketplace=self.name, fetched_at=_now(),
                            payload=data, warnings=warnings)

    def fetch_competitor_offers(self, identifier: str) -> DataEnvelope:
        """Live offer list for an ASIN, shaped for the repricer."""
        payload = self.client().get_item_offers(identifier)
        offers = payload.get("Offers", []) or []
        summary = payload.get("Summary", {}) or {}

        rows: list[dict[str, Any]] = []
        for o in offers:
            listing = (o.get("ListingPrice") or {})
            shipping = (o.get("Shipping") or {})
            rows.append({
                "seller": "self" if o.get("MyOffer") else (o.get("SellerId") or "unknown"),
                "price": _f(listing.get("Amount")) + _f(shipping.get("Amount")),
                "listing_price": _f(listing.get("Amount")),
                "shipping": _f(shipping.get("Amount")),
                "is_buybox": bool(o.get("IsBuyBoxWinner")),
                "is_fba": bool(o.get("IsFulfilledByAmazon")),
                "is_prime": bool((o.get("PrimeInformation") or {}).get("IsPrime")),
                "rating": _f((o.get("SellerFeedbackRating") or {}).get(
                    "SellerPositiveFeedbackRating")) / 20.0,  # % -> 5-point scale
                "review_count": int(_f((o.get("SellerFeedbackRating") or {}).get(
                    "FeedbackCount"))),
                "condition": o.get("SubCondition"),
                "in_stock": True,
            })

        warnings = []
        if not any(r["is_buybox"] for r in rows) and rows:
            warnings.append(
                "No Buy Box winner in the offer list — the Buy Box is likely "
                "suppressed on this ASIN (often a pricing-policy trigger). "
                "Repricing will not restore it; investigate the suppression."
            )

        return DataEnvelope(
            source="live", marketplace=self.name, fetched_at=_now(),
            payload={
                "asin": identifier,
                "offers": rows,
                "offer_count": summary.get("TotalOfferCount", len(rows)),
                "buybox_prices": summary.get("BuyBoxPrices", []),
                "sales_rankings": summary.get("SalesRankings", []),
            },
            warnings=warnings,
        )

    def fetch_reviews(self, sku: str) -> DataEnvelope:
        """Customer reviews are not exposed by SP-API.

        Amazon provides no review-retrieval API to sellers. Scraping product
        pages violates the Conditions of Use and risks the seller account, so
        this raises rather than quietly returning nothing — the review engine
        should be fed from an authorised third-party source or manual export.
        """
        raise ConnectorNotConfigured(
            "SP-API exposes no customer-review endpoint, and scraping reviews "
            "breaches Amazon's Conditions of Use — putting the selling account at "
            "risk to obtain them. Feed operator_core.reviews from an authorised "
            "provider or a Brand Analytics export instead. Returning an empty list "
            "here would misreport a product with bad reviews as having none."
        )

    def fetch_ad_performance(self, *, since: str) -> DataEnvelope:
        """Advertising lives on a separate API with its own authorisation.

        The Amazon Ads API is a different product from SP-API: different host,
        different scopes, different profile IDs. It is not reachable with these
        credentials.
        """
        raise ConnectorNotConfigured(
            "Advertising data comes from the Amazon Ads API (advertising-api.amazon.com), "
            "not SP-API. It needs its own application, its own LWA scopes, and a "
            "profileId. Set AMZ_ADS_* credentials and implement the Ads connector; "
            "SP-API credentials cannot reach it."
        )

    # ------------------------------------------------------------------
    # research / economics
    # ------------------------------------------------------------------
    def search_products(self, keywords: list[str], *, page_size: int = 20,
                        max_pages: int = 3) -> DataEnvelope:
        items = self.client().search_catalog_items(
            keywords=keywords, page_size=page_size, max_pages=max_pages,
        )
        rows: list[dict[str, Any]] = []
        for item in items:
            summaries = item.get("summaries") or [{}]
            s = summaries[0]
            ranks = item.get("salesRanks") or []
            best_rank = None
            for group in ranks:
                for rank in group.get("classificationRanks", []) + group.get("displayGroupRanks", []):
                    value = rank.get("rank")
                    if value and (best_rank is None or value < best_rank):
                        best_rank = value
            images = (item.get("images") or [{}])[0].get("images", [])
            rows.append({
                "asin": item.get("asin"),
                "title": s.get("itemName"),
                "brand": s.get("brand"),
                "manufacturer": s.get("manufacturer"),
                "product_type": s.get("productType"),
                "category": s.get("browseClassification", {}).get("displayName"),
                "best_sales_rank": best_rank,
                "image_url": images[0].get("link") if images else None,
                "package_weight": (item.get("dimensions") or [{}])[0]
                                  .get("package", {}).get("weight", {}).get("value"),
            })
        return DataEnvelope(source="live", marketplace=self.name, fetched_at=_now(),
                            payload=rows)

    def fetch_fee_breakdown(self, asin: str, price: float,
                            *, is_fba: bool = True) -> DataEnvelope:
        """Amazon's own fee calculation — the ground truth for `[fees.amazon]`."""
        fees: FeeBreakdown = self.client().get_fees_estimate(
            asin, price, currency=self.currency, is_fba=is_fba,
        )
        return DataEnvelope(
            source="live", marketplace=self.name, fetched_at=_now(),
            payload={
                "asin": fees.asin,
                "price": fees.price,
                "referral_fee": fees.referral_fee,
                "referral_pct": fees.referral_pct,
                "fba_fee": fees.fba_fee,
                "variable_closing_fee": fees.variable_closing_fee,
                "per_item_fee": fees.per_item_fee,
                "total_fees": fees.total_fees,
                "is_fba": fees.is_fba,
                "currency": fees.currency,
            },
        )

    def fetch_sales_and_traffic(self, *, days: int = 30) -> DataEnvelope:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        rows = self.client().fetch_report(
            REPORT_SALES_AND_TRAFFIC, start=start, end=end,
            options={"dateGranularity": "DAY", "asinGranularity": "CHILD"},
        )
        return DataEnvelope(source="live", marketplace=self.name, fetched_at=_now(),
                            payload=rows)

    def fetch_returns(self, *, days: int = 30) -> DataEnvelope:
        """Actual return rate per SKU — replaces the policy's flat estimate."""
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        rows = self.client().fetch_report(REPORT_RETURNS, start=start, end=end)
        return DataEnvelope(source="live", marketplace=self.name, fetched_at=_now(),
                            payload=rows)

    # ------------------------------------------------------------------
    # writes — implemented, and still gated
    # ------------------------------------------------------------------
    def update_price(self, sku: str, price: float, *,
                     product_type: str | None = None) -> DataEnvelope:
        self.require_write_permission("update_price")
        c = self.client()
        pt = product_type or self._product_type_for(sku)
        result = c.update_price(sku, price, product_type=pt, currency=self.currency)
        return DataEnvelope(
            source="live", marketplace=self.name, fetched_at=_now(),
            payload={"sku": sku, "price": price, "status": result.get("status"),
                     "submission_id": result.get("submissionId"),
                     "issues": result.get("issues", [])},
            warnings=self._issue_warnings(result),
        )

    def update_quantity(self, sku: str, quantity: int, *,
                        product_type: str | None = None) -> DataEnvelope:
        self.require_write_permission("update_quantity")
        c = self.client()
        pt = product_type or self._product_type_for(sku)
        result = c.update_quantity(sku, quantity, product_type=pt)
        return DataEnvelope(
            source="live", marketplace=self.name, fetched_at=_now(),
            payload={"sku": sku, "quantity": quantity, "status": result.get("status"),
                     "submission_id": result.get("submissionId")},
            warnings=self._issue_warnings(result),
        )

    def publish_listing(self, listing: dict[str, Any]) -> DataEnvelope:
        """Create or replace a listing.

        `listing` must carry `sku`, `product_type`, and `attributes` in the
        schema Amazon expects for that product type. The schema is per-type and
        per-marketplace; fetch it from the Product Type Definitions API rather
        than guessing, because a rejected submission is silent until you read
        the issues array.
        """
        self.require_write_permission("publish_listing")

        required = {"sku", "product_type", "attributes"}
        missing = required - set(listing)
        if missing:
            raise ValueError(
                f"publish_listing needs {sorted(missing)}. Attributes must match the "
                "Product Type Definitions schema for this product type and marketplace."
            )

        result = self.client().put_listings_item(
            listing["sku"],
            product_type=listing["product_type"],
            attributes=listing["attributes"],
            requirements=listing.get("requirements", "LISTING"),
        )
        return DataEnvelope(
            source="live", marketplace=self.name, fetched_at=_now(),
            payload={"sku": listing["sku"], "status": result.get("status"),
                     "submission_id": result.get("submissionId"),
                     "issues": result.get("issues", [])},
            warnings=self._issue_warnings(result),
        )

    def update_ad_budget(self, campaign_id: str, budget: float) -> DataEnvelope:
        self.require_write_permission("update_ad_budget")
        raise ConnectorNotConfigured(
            "Ad budgets are managed through the Amazon Ads API, which is a separate "
            "application from SP-API and cannot be reached with these credentials."
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _product_type_for(self, sku: str) -> str:
        """Look up the product type Amazon has on file for a SKU.

        Patches are rejected if the productType does not match, and the value is
        not knowable from the SKU alone, so it is read rather than assumed.
        """
        data = self.client().get_listings_item(sku, included_data=("summaries",))
        summaries = data.get("summaries") or []
        for s in summaries:
            pt = s.get("productType")
            if pt:
                return pt
        raise SPAPIError(
            f"Could not determine productType for SKU {sku!r}. A patch with the wrong "
            "productType is rejected, so the operator will not guess one.",
            operation="getListingsItem",
        )

    @staticmethod
    def _issue_warnings(result: dict[str, Any]) -> list[str]:
        out = []
        for issue in result.get("issues", []) or []:
            out.append(
                f"{issue.get('severity', 'INFO')} [{issue.get('code', '')}]: "
                f"{issue.get('message', '')}"
            )
        if result.get("status") == "ACCEPTED" and not out:
            out.append(
                "Amazon ACCEPTED the submission. Acceptance means the payload was "
                "well-formed, not that the change is live — poll getListingsItem to "
                "confirm it applied."
            )
        return out

    @staticmethod
    def _parse_since(since: str) -> datetime:
        try:
            dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"Could not parse `since`={since!r}. Use ISO 8601, e.g. 2026-07-01 "
                "or 2026-07-01T00:00:00Z."
            ) from exc
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def status(self) -> dict[str, Any]:
        base = super().status()
        base.update({
            "marketplace_id": self.marketplace_id or None,
            "seller_id": self.seller_id or None,
            "sandbox": self.sandbox,
        })
        if self._transport is not None:
            base["transport"] = self._transport.stats()
        return base
