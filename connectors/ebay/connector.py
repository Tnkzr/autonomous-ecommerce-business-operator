"""EbayConnector: the Sell and Buy APIs wired into the operator's models.

eBay is a **search** marketplace, not a discovery one, and that changes what the
connector is for. Nobody scrolls past an eBay listing — they went looking. So
the levers are title, price, and seller standing, and the connector's job is to
supply the three things the rest of the system needs to work those levers:

**Competitor prices, legally.** `fetch_competitor_offers` actually returns data
here. On Shopify and TikTok it raises, because the only way to get rival prices
is scraping and that is a ToS breach with legal exposure. eBay's Browse API
publishes them. This un-blocks the repricer and the competition dimensions of
the scorecard, which have been reporting `None` since they were written.

**Real fees.** The Finances API reports what eBay actually charged per
transaction, so `[fees.ebay]` stops being an estimate. Every margin, floor
price and screening verdict downstream is computed from that schedule, which
makes this the highest-value number in the integration.

**Unpublished-first listing.** eBay separates an inventory item (a record), an
offer (a priced intent), and publishing (a live listing). The first two are
invisible to buyers and reversible, so the operator creates them freely; only
`publish_offer` is gated by `risk.authorise()`. Same shape as the Shopify
draft-first model, for the same reason — and withdrawing is ungated, because
requiring sign-off to take a listing down is how a problem stays live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..base import (
    ConnectorNotConfigured,
    DataEnvelope,
    MarketplaceConnector,
    MarketplaceNotImplemented,
    WriteNotPermitted,
)
from ..credentials import CredentialSource, CredentialsUnavailable
from .auth import TokenProvider
from .client import EbayClient, SellerProfile
from .credentials import (
    KNOWN_MARKETPLACES,
    REQUIRED_SCOPES,
    EbayCredentials,
    default_source,
)
from .transport import EbayAPIError, EbayQuotaExhausted, Transport

# eBay order states where money has not settled. Counting them as revenue
# overstates both sales and conversion, and the correction lands weeks later.
UNPAID_STATES = frozenset({"NOT_STARTED", "PENDING", "IN_PROGRESS"})
CANCELLED_STATES = frozenset({"CANCELLED", "CANCELED"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _amount(node: Any) -> float:
    """Pull a value out of eBay's {value, currency} shape."""
    if isinstance(node, dict):
        return _f(node.get("value"))
    return _f(node)


@dataclass
class EbayOrderSummary:
    order_id: str
    created_at: str
    status: str
    payment_status: str
    cancelled: bool
    gross_total: float
    currency: str
    buyer_key: str
    line_items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def units(self) -> int:
        return sum(int(li.get("quantity") or 0) for li in self.line_items)

    @property
    def is_revenue(self) -> bool:
        """Whether this order represents money that actually arrived."""
        return not self.cancelled and self.payment_status.upper() == "PAID"


class EbayConnector(MarketplaceConnector):
    """eBay Sell APIs (Fulfillment, Inventory, Finances, Analytics) + Browse."""

    name = "ebay"
    required_env = ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_REFRESH_TOKEN")
    docs_url = "https://developer.ebay.com/api-docs/sell/static/overview.html"

    def __init__(self, *, allow_writes: bool = False,
                 transport: Transport | None = None,
                 client: EbayClient | None = None,
                 credentials: CredentialSource | None = None) -> None:
        super().__init__(allow_writes=allow_writes)
        self._client = client
        self._transport = transport
        self._credentials = credentials or default_source()
        self._tokens: TokenProvider | None = None

    # -- wiring ------------------------------------------------------------
    def client(self) -> EbayClient:
        if self._client is not None:
            return self._client
        if self._transport is None:
            try:
                creds: EbayCredentials = self._credentials.resolve()
            except CredentialsUnavailable as exc:
                raise ConnectorNotConfigured(str(exc)) from exc
            self._tokens = TokenProvider(creds, scopes=REQUIRED_SCOPES)
            self._transport = Transport(credentials=creds,
                                        token_provider=self._tokens)
        self._client = EbayClient(self._transport)
        return self._client

    # -- reads -------------------------------------------------------------
    def fetch_orders(self, *, since: str) -> DataEnvelope:
        raw = self.client().orders(since=since)
        summaries = [self._summarise_order(o) for o in raw]
        warnings: list[str] = []

        unpaid = sum(1 for s in summaries if not s.is_revenue and not s.cancelled)
        cancelled = sum(1 for s in summaries if s.cancelled)
        if unpaid:
            warnings.append(
                f"{unpaid} order(s) are placed but unpaid. They are returned "
                "here and excluded from revenue — counting them would overstate "
                "sales and conversion at once.")
        if cancelled:
            warnings.append(f"{cancelled} order(s) are cancelled and are not revenue.")
        return DataEnvelope("live", self.name, _now(), summaries, warnings)

    def fetch_inventory(self) -> DataEnvelope:
        items = self.client().inventory_items()
        rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        uncosted = 0

        for item in items:
            availability = ((item.get("availability") or {})
                            .get("shipToLocationAvailability") or {})
            cost = (item.get("cost") or {}).get("value")
            if cost is None:
                uncosted += 1
            rows.append({
                "sku": str(item.get("sku", "")),
                "available": availability.get("quantity"),
                "condition": str(item.get("condition", "")),
                "title": str((item.get("product") or {}).get("title", "")),
                "unit_cost": _f(cost) if cost is not None else None,
            })
        if uncosted:
            warnings.append(
                f"{uncosted} inventory item(s) carry no cost, so margin cannot "
                "be computed for them. Cost is not an eBay-required field — it "
                "has to come from the purchase order.")
        return DataEnvelope("live", self.name, _now(), rows, warnings)

    def fetch_listings(self) -> DataEnvelope:
        listings: list[dict[str, Any]] = []
        warnings: list[str] = []
        unpublished = 0

        for item in self.client().inventory_items():
            sku = str(item.get("sku", ""))
            if not sku:
                continue
            for offer in self.client().offers(sku):
                status = str(offer.get("status", ""))
                if status.upper() != "PUBLISHED":
                    unpublished += 1
                listings.append({
                    "sku": sku,
                    "offer_id": str(offer.get("offerId", "")),
                    "listing_id": str((offer.get("listing") or {}).get("listingId", "")),
                    "status": status,
                    "price": _amount((offer.get("pricingSummary") or {}).get("price")),
                    "quantity": offer.get("availableQuantity"),
                    "category_id": str(offer.get("categoryId", "")),
                    "format": str(offer.get("format", "")),
                })
        if unpublished:
            warnings.append(
                f"{unpublished} offer(s) exist but are not published. An "
                "unpublished offer is invisible to buyers and earns nothing.")
        return DataEnvelope("live", self.name, _now(), listings, warnings)

    def fetch_competitor_offers(self, identifier: str) -> DataEnvelope:
        """Rival offers from the Browse API.

        The one marketplace here where this question has an answer. Results are
        a *sample* of the search page, not the whole market — reporting them as
        exhaustive would overstate how well the competitive set is understood,
        so the count and the cap travel with the data.
        """
        items = self.client().search_items(identifier)
        offers = []
        for item in items:
            price = _amount(item.get("price"))
            if price <= 0:
                continue
            shipping_options = item.get("shippingOptions") or []
            shipping = _amount((shipping_options[0] or {}).get("shippingCost")) \
                if shipping_options else 0.0
            seller = item.get("seller") or {}
            offers.append({
                "seller": str(seller.get("username", "")),
                "price": round(price, 2),
                "shipping": round(shipping, 2),
                "landed_price": round(price + shipping, 2),
                "feedback_pct": _f(seller.get("feedbackPercentage")) or None,
                "feedback_score": seller.get("feedbackScore"),
                "condition": str(item.get("condition", "")),
                "item_id": str(item.get("itemId", "")),
                "title": str(item.get("title", "")),
                "is_top_rated": bool(item.get("topRatedBuyingExperience")),
            })

        warnings = [
            f"{len(offers)} offer(s) from a Browse search for {identifier!r}. "
            "This is the first page of a search, not the entire competitive set "
            "— treat it as a sample and do not read the minimum as the true "
            "market floor.",
        ]
        if not offers:
            warnings.append(
                "No priced offers came back. That is not proof the category is "
                "empty; it usually means the query is too specific.")
        return DataEnvelope("live", self.name, _now(), offers, warnings)

    def fetch_transactions(self, *, since: str) -> DataEnvelope:
        """Real fees and payouts from the Finances API."""
        raw = self.client().transactions(since=since)
        rows = [{
            "transaction_id": str(t.get("transactionId", "")),
            "order_id": str(t.get("orderId", "")),
            "type": str(t.get("transactionType", "")),
            "date": str(t.get("transactionDate", "")),
            "amount": _amount(t.get("amount")),
            "fee_type": str(t.get("feeType", "")),
            "booking_entry": str(t.get("bookingEntry", "")),
        } for t in raw]
        return DataEnvelope("live", self.name, _now(), rows, [
            "These are eBay's actual charges. Reconcile [fees.ebay] against "
            "them — every margin and floor price in the system is computed "
            "from that schedule.",
        ])

    def fetch_reviews(self, sku: str) -> DataEnvelope:
        raise MarketplaceNotImplemented(
            self.name, "fetch_reviews",
            endpoints="none — eBay feedback is per seller and per transaction",
            docs=("eBay has no product-review corpus to read. Feedback attaches "
                  "to the seller and the transaction, not to the item, so there "
                  "is nothing to cluster for defect themes. Seller-level "
                  "standing is available through `seller_standards` instead."))

    def fetch_ad_performance(self, *, since: str) -> DataEnvelope:
        raise MarketplaceNotImplemented(
            self.name, "fetch_ad_performance",
            endpoints="sell/marketing/v1/ad_campaign (not implemented)",
            docs=("Promoted Listings runs through the Marketing API, which is "
                  "not wired up. It is a separate build, not a missing "
                  "credential — this connector does not read ad spend."))

    # -- writes ------------------------------------------------------------
    def upsert_inventory_item(self, *, sku: str, title: str, description: str,
                              quantity: int, condition: str = "NEW",
                              image_urls: list[str] | None = None,
                              aspects: dict[str, list[str]] | None = None,
                              unit_cost: float | None = None) -> DataEnvelope:
        """Create or replace an inventory item. Invisible to buyers.

        Safe to run autonomously: an inventory item with no published offer is
        a private record. Nothing is for sale until an offer is published.
        """
        self.require_write_permission("upsert_inventory_item")
        if quantity < 0:
            raise ValueError(f"Refusing to set negative quantity ({quantity}).")

        payload: dict[str, Any] = {
            "availability": {"shipToLocationAvailability": {"quantity": int(quantity)}},
            "condition": condition,
            "product": {
                "title": title[:80],   # eBay truncates at 80; do it deliberately.
                "description": description,
            },
        }
        if image_urls:
            payload["product"]["imageUrls"] = image_urls
        if aspects:
            payload["product"]["aspects"] = aspects
        if unit_cost is not None and unit_cost > 0:
            payload["cost"] = {"value": f"{unit_cost:.2f}",
                               "currency": "USD"}

        self.client().create_or_replace_inventory_item(sku, payload)
        warnings = ["Inventory item saved. It is invisible to buyers until an "
                    "offer is created and published."]
        if len(title) > 80:
            warnings.append(
                f"Title was {len(title)} characters and was cut to 80 — eBay's "
                "hard limit. Truncating here rather than letting eBay do it "
                "keeps the important words at the front.")
        return DataEnvelope("live", self.name, _now(), {"sku": sku}, warnings)

    def create_offer(self, *, sku: str, price: float, category_id: str,
                     merchant_location_key: str, quantity: int = 1,
                     listing_policies: dict[str, str] | None = None,
                     listing_description: str = "") -> DataEnvelope:
        """Create an unpublished offer. Still invisible; still reversible."""
        self.require_write_permission("create_offer")
        if price <= 0:
            raise ValueError(f"Refusing to create an offer at {price}.")

        payload: dict[str, Any] = {
            "sku": sku,
            "marketplaceId": self._credentials.resolve().marketplace_id,
            "format": "FIXED_PRICE",
            "availableQuantity": int(quantity),
            "categoryId": category_id,
            "merchantLocationKey": merchant_location_key,
            "pricingSummary": {"price": {"value": f"{price:.2f}",
                                         "currency": "USD"}},
        }
        if listing_description:
            payload["listingDescription"] = listing_description
        if listing_policies:
            payload["listingPolicies"] = listing_policies

        offer = self.client().create_offer(payload)
        return DataEnvelope("live", self.name, _now(), {
            "offer_id": str(offer.get("offerId", "")), "sku": sku, "price": price,
        }, ["Offer created but NOT published. It earns nothing until "
            "`publish_offer` runs with an approved action."])

    def publish_offer(self, offer_id: str, *, authorisation: Any) -> DataEnvelope:
        """Publish an offer. The irreversible step — the listing goes live.

        `authorisation` must be a permitted `risk.AuthorisationResult`. It is
        required rather than computed here so the decision, its reasons, and
        its journal entry all live in one place.
        """
        self.require_write_permission("publish_offer")
        if getattr(authorisation, "permitted", None) is not True:
            reason = getattr(authorisation, "explain", lambda: "")() or \
                "no authorisation was supplied"
            raise WriteNotPermitted(
                f"Refusing to publish offer {offer_id}: {reason}. Publishing "
                "creates a live listing with a binding obligation to sell at "
                "that price, and eBay charges an insertion fee for it.")

        result = self.client().publish_offer(offer_id)
        return DataEnvelope("live", self.name, _now(), {
            "offer_id": offer_id,
            "listing_id": str(result.get("listingId", "")),
        }, ["Listing is live and buyable."])

    def withdraw_offer(self, offer_id: str) -> DataEnvelope:
        """End a live listing. Deliberately ungated, like Shopify unpublish."""
        self.require_write_permission("withdraw_offer")
        result = self.client().withdraw_offer(offer_id)
        return DataEnvelope("live", self.name, _now(), {
            "offer_id": offer_id,
            "listing_id": str(result.get("listingId", "")),
        })

    def update_price(self, sku: str, price: float) -> DataEnvelope:
        """Reprice every offer on a SKU."""
        self.require_write_permission("update_price")
        if price <= 0:
            raise ValueError(f"Refusing to set a non-positive price ({price}) on {sku}.")

        offers = self.client().offers(sku)
        if not offers:
            raise EbayAPIError(
                f"No offer exists for SKU {sku!r}, so there is nothing to "
                "reprice.", endpoint="update_price")

        updated = []
        for offer in offers:
            offer_id = str(offer.get("offerId", ""))
            self.client().update_offer(offer_id, {
                "availableQuantity": offer.get("availableQuantity"),
                "categoryId": offer.get("categoryId"),
                "merchantLocationKey": offer.get("merchantLocationKey"),
                "pricingSummary": {"price": {"value": f"{price:.2f}",
                                             "currency": "USD"}},
            })
            updated.append(offer_id)
        return DataEnvelope("live", self.name, _now(),
                            {"sku": sku, "offers": updated, "price": price})

    # -- helpers -----------------------------------------------------------
    def _summarise_order(self, order: dict[str, Any]) -> EbayOrderSummary:
        payment = order.get("orderPaymentStatus") or ""
        fulfilment = order.get("orderFulfillmentStatus") or ""
        pricing = order.get("pricingSummary") or {}
        buyer = order.get("buyer") or {}

        line_items = [{
            "sku": str(li.get("sku") or ""),
            "title": str(li.get("title") or ""),
            "quantity": int(li.get("quantity") or 0),
            "revenue": _amount(li.get("total")),
            "line_item_id": str(li.get("lineItemId", "")),
        } for li in (order.get("lineItems") or [])]

        return EbayOrderSummary(
            order_id=str(order.get("orderId", "")),
            created_at=str(order.get("creationDate", "")),
            status=str(fulfilment),
            payment_status=str(payment),
            cancelled=str(order.get("cancelStatus", {}).get("cancelState", ""))
                      .upper() in CANCELLED_STATES,
            gross_total=_amount(pricing.get("total")),
            currency=str((pricing.get("total") or {}).get("currency", "")),
            buyer_key=str(buyer.get("username", "")),
            line_items=line_items,
        )

    # -- verification ------------------------------------------------------
    def verify_connection(self) -> dict[str, Any]:
        """Prove both token types work and report the refresh-token clock."""
        status = self._credentials.status()
        if not status.available:
            return {"marketplace": self.name, "ok": False, "detail": status.detail,
                    "credential_source": status.source_name,
                    "remedy": status.remedy}

        creds: EbayCredentials = self._credentials.resolve()
        warnings: list[str] = []
        if creds.marketplace_id not in KNOWN_MARKETPLACES:
            warnings.append(
                f"Marketplace {creds.marketplace_id!r} is not one this connector "
                f"recognises. Known: {', '.join(KNOWN_MARKETPLACES[:6])}…")
        if creds.is_sandbox:
            warnings.append(
                "Connected to SANDBOX. Orders, listings and fees here are "
                "eBay's test fixtures, not your business.")

        try:
            profile: SellerProfile = self.client().seller_standards()
        except (EbayAPIError, ConnectorNotConfigured) as exc:
            return {"marketplace": self.name, "ok": False, "detail": str(exc),
                    "credential_source": status.source_name,
                    "warnings": warnings,
                    "remedy": self._credentials.spec.remedy}

        if self._tokens is not None:
            grant_warning = self._tokens.grant_age_warning()
            if grant_warning:
                warnings.append(grant_warning)

        return {
            "marketplace": self.name,
            "ok": True,
            "detail": f"Connected as {profile.username or 'unknown seller'}.",
            "credential_source": status.source_name,
            "environment": creds.environment,
            "marketplace_id": creds.marketplace_id,
            "seller": profile.username,
            "standards_level": profile.standards_level,
            "top_rated": profile.is_top_rated,
            "defect_rate_pct": profile.defect_rate_pct,
            "late_shipment_rate_pct": profile.late_shipment_rate_pct,
            "warnings": warnings,
        }

    def status(self) -> dict[str, Any]:
        cred = self._credentials.status()
        return {
            "marketplace": self.name,
            "configured": cred.available,
            "credential_source": cred.source_name,
            "missing_env": self.missing_credentials(),
            "writes_allowed": self.allow_writes,
            "docs": self.docs_url,
            "remedy": cred.remedy,
        }
