"""ShopifyConnector: the Admin API wired into the operator's models.

Shopify is the destination in this business, not a marketplace we compete in.
That changes what the connector is for, and two consequences run through the
whole file.

**Draft-first is the safety model.** Every product this operator creates is
created with `status: DRAFT`. A draft is invisible to customers, so creating one
is reversible and needs no approval — which is what makes it safe to let the
operator generate listings autonomously. Moving a product to ACTIVE is the
irreversible step (it can be indexed, linked, and bought within seconds), so
that transition, and only that transition, goes through `risk.authorise()`. This
is why `create_product` refuses to accept ACTIVE at all rather than gating it:
an approval check that can be passed the wrong argument is a check that will
eventually be passed the wrong argument.

**Attribution is the point.** The business runs on organic TikTok traffic, so
"did this order come from TikTok" is the single most valuable field the API
returns. `customerJourneySummary` is first-party and free; without it the
content engine optimises blind. `classify_traffic_source` is deliberately
conservative — an order whose journey Shopify could not resolve is reported as
`unattributed`, never silently folded into direct or into TikTok. Inflating the
channel you are trying to evaluate is the fastest way to keep funding a channel
that does not work.

What Shopify genuinely cannot tell us is stated rather than approximated:
there are no competitor offers and no reviews in the Admin API, so those calls
raise instead of returning an empty list that reads as "no competitors".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..base import (
    ConnectorNotConfigured,
    DataEnvelope,
    MarketplaceConnector,
    MarketplaceNotImplemented,
    WriteNotPermitted,
)
from ..credentials import CredentialSource, CredentialsUnavailable
from .client import ShopifyClient, ShopInfo
from .credentials import REQUIRED_SCOPES, ShopifyCredentials, default_source
from .transport import ShopifyAPIError, Transport, normalise_domain

# Referrer and UTM fragments that identify TikTok traffic. Matched against the
# journey's source, source type, referrer URL and utm_source — TikTok appears
# in different fields depending on whether the visit came from the in-app
# browser, a bio link, or a tagged link, and matching only one field
# under-counts the channel the whole business depends on.
TIKTOK_MARKERS = ("tiktok", "tik tok", "ttclid", "bytedance", "musical.ly")

# Sources Shopify reports that mean "we could not tell". Treating these as
# direct traffic is the standard analytics lie: it silently credits the channel
# that gets the blame for everything and none of the budget.
UNKNOWN_SOURCES = ("", "unknown", "unavailable", "null", "none")

SOCIAL_MARKERS = ("instagram", "facebook", "pinterest", "youtube", "reddit",
                  "snapchat", "twitter", "x.com", "threads", "linkedin")
SEARCH_MARKERS = ("google", "bing", "duckduckgo", "yahoo", "ecosia", "baidu")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _money_of(node: Any) -> float:
    """Pull an amount out of Shopify's nested MoneyBag shape."""
    if not isinstance(node, dict):
        return 0.0
    shop_money = node.get("shopMoney")
    if isinstance(shop_money, dict):
        return _f(shop_money.get("amount"))
    return _f(node.get("amount"))


def handleize(text: str) -> str:
    """Shopify's own handle rules: lowercase, non-alphanumerics to hyphens.

    Generated here rather than left to Shopify because the handle is the
    product URL, and a URL that changes after the videos linking to it are
    published costs every view those videos earned.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return slug[:255] or "product"


@dataclass
class TrafficAttribution:
    """Where one order came from, and how sure we are."""

    channel: str            # tiktok | social | search | direct | referral | unattributed
    detail: str = ""
    campaign: str = ""
    confident: bool = True

    @property
    def is_tiktok(self) -> bool:
        return self.channel == "tiktok"


@dataclass
class ShopifyOrderSummary:
    order_id: str
    name: str
    created_at: str
    gross_revenue: float
    discounts: float
    shipping: float
    tax: float
    refunded: float
    currency: str
    financial_status: str
    fulfillment_status: str
    cancelled: bool
    customer_id: str
    customer_order_count: int
    attribution: TrafficAttribution
    line_items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def net_revenue(self) -> float:
        """Revenue actually kept: gross less refunds, excluding tax and shipping.

        Tax is collected on behalf of a government and shipping is usually a
        pass-through; counting either as revenue overstates margin on every
        product, which then overstates how much can be paid to acquire a
        customer.
        """
        return round(self.gross_revenue - self.refunded - self.tax - self.shipping, 2)

    @property
    def is_repeat(self) -> bool:
        return self.customer_order_count > 1


def classify_traffic_source(journey: Any) -> TrafficAttribution:
    """Classify an order's traffic source from its customer journey.

    Conservative by construction: anything Shopify could not resolve comes back
    as `unattributed` with `confident=False` rather than being assigned to a
    channel. The content engine's entire feedback loop reads this field, so a
    guess here becomes a spending decision later.
    """
    if not isinstance(journey, dict) or not journey:
        return TrafficAttribution(
            "unattributed",
            "Shopify returned no customer journey for this order. Journeys are "
            "absent for orders created outside the online store and for visitors "
            "who blocked tracking.",
            confident=False)

    visit = journey.get("lastVisit") or journey.get("firstVisit") or {}
    if not isinstance(visit, dict) or not visit:
        return TrafficAttribution(
            "unattributed", "Journey present but carried no visit record.",
            confident=False)

    utm = visit.get("utmParameters") or {}
    campaign = str(utm.get("campaign") or "")
    haystack = " ".join(str(part or "").lower() for part in (
        visit.get("source"), visit.get("sourceType"), visit.get("referrerUrl"),
        visit.get("landingPage"), utm.get("source"), utm.get("medium"),
        campaign,
    ))

    if any(marker in haystack for marker in TIKTOK_MARKERS):
        return TrafficAttribution("tiktok", str(visit.get("source") or "tiktok"),
                                  campaign)

    source = str(visit.get("source") or "").strip().lower()
    source_type = str(visit.get("sourceType") or "").strip().lower()

    if any(marker in haystack for marker in SOCIAL_MARKERS):
        return TrafficAttribution("social", source or source_type, campaign)
    if any(marker in haystack for marker in SEARCH_MARKERS):
        return TrafficAttribution("search", source or source_type, campaign)
    if source_type == "direct" or source == "direct":
        return TrafficAttribution("direct", "direct", campaign)
    if source in UNKNOWN_SOURCES and source_type in UNKNOWN_SOURCES:
        return TrafficAttribution(
            "unattributed", "Visit recorded but the source was empty.",
            campaign, confident=False)
    return TrafficAttribution("referral", source or source_type, campaign)


class ShopifyConnector(MarketplaceConnector):
    """Shopify Admin API (GraphQL).

    Reads return `DataEnvelope` tagged `live`. Writes require write permission;
    the ACTIVE transition additionally requires an authorised action, which the
    caller supplies — this class refuses rather than deciding for itself.
    """

    name = "shopify"
    required_env = ("SHOPIFY_SHOP_DOMAIN", "SHOPIFY_ADMIN_ACCESS_TOKEN")
    docs_url = "https://shopify.dev/docs/api/admin-graphql"

    def __init__(self, *, allow_writes: bool = False,
                 transport: Transport | None = None,
                 client: ShopifyClient | None = None,
                 credentials: CredentialSource | None = None) -> None:
        super().__init__(allow_writes=allow_writes)
        self._client = client
        self._transport = transport
        self._credentials = credentials or default_source()
        self._shop: ShopInfo | None = None
        self._locations: list[dict[str, Any]] = []

    # -- wiring ------------------------------------------------------------
    def client(self) -> ShopifyClient:
        if self._client is not None:
            return self._client
        if self._transport is None:
            try:
                creds: ShopifyCredentials = self._credentials.resolve()
            except CredentialsUnavailable as exc:
                raise ConnectorNotConfigured(str(exc)) from exc
            self._transport = Transport(
                shop_domain=creds.shop_domain,
                access_token=creds.access_token,
                api_version=creds.api_version,
            )
        self._client = ShopifyClient(self._transport)
        return self._client

    def shop(self) -> ShopInfo:
        if self._shop is None:
            self._shop = self.client().shop()
        return self._shop

    def default_location_id(self) -> str:
        """The location inventory is written to.

        Picked once and cached. A store with several locations that writes to
        an arbitrary one splits its stock across them, and the resulting
        oversell looks like a forecasting failure rather than a wiring bug — so
        an ambiguous multi-location store raises instead of choosing.
        """
        if not self._locations:
            self._locations = self.client().locations()
        usable = [loc for loc in self._locations
                  if loc.get("isActive") and loc.get("fulfillsOnlineOrders")]
        if not usable:
            raise ShopifyAPIError(
                "No active location fulfils online orders, so there is nowhere "
                "to write inventory. Set one in Settings > Locations.",
                layer="errors")
        if len(usable) > 1:
            names = ", ".join(f"{l.get('name')} ({l.get('id')})" for l in usable)
            raise ShopifyAPIError(
                f"{len(usable)} locations fulfil online orders ({names}). Refusing "
                "to pick one — writing stock to the wrong location silently splits "
                "inventory and causes oversells. Pass location_id explicitly.",
                layer="errors")
        return str(usable[0]["id"])

    # -- reads -------------------------------------------------------------
    def fetch_orders(self, *, since: str) -> DataEnvelope:
        query = f"created_at:>={since}" if since else None
        raw = self.client().orders(query=query)
        summaries = [self._summarise_order(o) for o in raw]
        warnings: list[str] = []
        unattributed = sum(1 for s in summaries if not s.attribution.confident)
        if summaries and unattributed:
            share = unattributed / len(summaries) * 100
            warnings.append(
                f"{unattributed} of {len(summaries)} orders ({share:.0f}%) have no "
                "resolvable traffic source. Channel performance below is computed "
                "on the attributable remainder and understates every channel.")
        return DataEnvelope("live", self.name, _now(), summaries, warnings)

    def fetch_inventory(self) -> DataEnvelope:
        rows = self.client().inventory_levels()
        items: list[dict[str, Any]] = []
        warnings: list[str] = []
        untracked = 0
        for row in rows:
            inv = row.get("inventoryItem") or {}
            if not inv.get("tracked"):
                untracked += 1
            levels = (inv.get("inventoryLevels") or {}).get("nodes") or []
            by_location = []
            for level in levels:
                quantities = {q.get("name"): q.get("quantity")
                              for q in (level.get("quantities") or [])}
                by_location.append({
                    "location_id": str((level.get("location") or {}).get("id", "")),
                    "location_name": str((level.get("location") or {}).get("name", "")),
                    "available": quantities.get("available"),
                    "on_hand": quantities.get("on_hand"),
                    "committed": quantities.get("committed"),
                })
            items.append({
                "variant_id": str(row.get("id", "")),
                "sku": str(row.get("sku") or ""),
                "title": str(row.get("displayName") or ""),
                "total_quantity": row.get("inventoryQuantity"),
                "tracked": bool(inv.get("tracked")),
                "unit_cost": _f((inv.get("unitCost") or {}).get("amount"))
                             if inv.get("unitCost") else None,
                "inventory_item_id": str(inv.get("id", "")),
                "locations": by_location,
            })
        if untracked:
            warnings.append(
                f"{untracked} variant(s) have inventory tracking disabled. Their "
                "quantities are not a stock level and must not be fed to the "
                "reorder forecast — Shopify will sell them without limit.")
        missing_cost = sum(1 for i in items if i["unit_cost"] is None)
        if missing_cost:
            warnings.append(
                f"{missing_cost} variant(s) have no unit cost set, so margin "
                "cannot be computed for them. Set cost per item on the variant.")
        return DataEnvelope("live", self.name, _now(), items, warnings)

    def fetch_listings(self) -> DataEnvelope:
        products = self.client().products()
        listings = [self._summarise_product(p) for p in products]
        warnings = []
        drafts = sum(1 for l in listings if l["status"] == "DRAFT")
        if drafts:
            warnings.append(
                f"{drafts} product(s) are in DRAFT and are not visible to "
                "customers. Drafts earn nothing until published.")
        return DataEnvelope("live", self.name, _now(), listings, warnings)

    def fetch_customers(self, *, since: str = "") -> DataEnvelope:
        query = f"created_at:>={since}" if since else None
        rows = self.client().customers(query=query)
        customers = [{
            "customer_id": str(c.get("id", "")),
            "created_at": str(c.get("createdAt", "")),
            "order_count": int(c.get("numberOfOrders") or 0),
            "lifetime_spend": _f((c.get("amountSpent") or {}).get("amount")),
            "last_order_at": str((c.get("lastOrder") or {}).get("createdAt", "")),
            "tags": c.get("tags") or [],
        } for c in rows]
        return DataEnvelope("live", self.name, _now(), customers)

    def fetch_collections(self) -> DataEnvelope:
        rows = self.client().collections()
        return DataEnvelope("live", self.name, _now(), [{
            "collection_id": str(c.get("id", "")),
            "title": str(c.get("title", "")),
            "handle": str(c.get("handle", "")),
            "product_count": int((c.get("productsCount") or {}).get("count") or 0),
        } for c in rows])

    def fetch_competitor_offers(self, identifier: str) -> DataEnvelope:
        raise MarketplaceNotImplemented(
            self.name, "fetch_competitor_offers",
            endpoints="none — the Admin API only sees your own store",
            docs=("Competitor pricing is not obtainable from Shopify. Scraping "
                  "rival storefronts is the usual workaround and is both a ToS "
                  "breach and legally exposed; a paid market-data feed is the "
                  "supportable route. Reporting an empty list here would read "
                  "as 'no competitors', which is never true."))

    def fetch_reviews(self, sku: str) -> DataEnvelope:
        raise MarketplaceNotImplemented(
            self.name, "fetch_reviews",
            endpoints="none — reviews live in a third-party app, not the Admin API",
            docs=("Shopify has no native product reviews. If a reviews app is "
                  "installed, read it through that app's own API and register it "
                  "as a separate connector so its provenance stays visible."))

    def fetch_ad_performance(self, *, since: str) -> DataEnvelope:
        raise MarketplaceNotImplemented(
            self.name, "fetch_ad_performance",
            endpoints="none — ad spend lives in the ad platform",
            docs=("This business runs on organic traffic and has no ad spend to "
                  "report. Shopify sees the resulting sessions and orders, not "
                  "cost; joining spend to revenue needs the ad platform's own API."))

    # -- writes ------------------------------------------------------------
    def create_draft_product(self, *, title: str, description_html: str,
                             vendor: str = "", product_type: str = "",
                             tags: list[str] | None = None,
                             seo_title: str = "", seo_description: str = "",
                             handle: str = "") -> DataEnvelope:
        """Create a product as DRAFT. Never ACTIVE — see the module docstring."""
        self.require_write_permission("create_draft_product")
        product_input: dict[str, Any] = {
            "title": title,
            "descriptionHtml": description_html,
            "status": "DRAFT",
            "handle": handle or handleize(title),
        }
        if vendor:
            product_input["vendor"] = vendor
        if product_type:
            product_input["productType"] = product_type
        if tags:
            product_input["tags"] = tags
        seo = {}
        if seo_title:
            seo["title"] = seo_title
        if seo_description:
            seo["description"] = seo_description
        if seo:
            product_input["seo"] = seo

        product = self.client().create_product(product_input)
        return DataEnvelope("live", self.name, _now(), {
            "product_id": str(product.get("id", "")),
            "handle": str(product.get("handle", "")),
            "status": str(product.get("status", "")),
            "title": str(product.get("title", "")),
        }, ["Created as DRAFT. It is not visible to customers and earns nothing "
            "until `publish_product` is called with an approved action."])

    def add_variants(self, product_id: str,
                     variants: list[dict[str, Any]]) -> DataEnvelope:
        self.require_write_permission("add_variants")
        created = self.client().create_variants(product_id, variants)
        return DataEnvelope("live", self.name, _now(), [{
            "variant_id": str(v.get("id", "")),
            "sku": str(v.get("sku") or ""),
            "price": _f(v.get("price")),
            "inventory_item_id": str((v.get("inventoryItem") or {}).get("id", "")),
        } for v in created])

    def publish_product(self, product_id: str, *, authorisation: Any,
                        publication_ids: list[str] | None = None) -> DataEnvelope:
        """Move a product to ACTIVE and publish it to sales channels.

        The irreversible step: once live the URL can be indexed, shared, and
        bought. `authorisation` must be a permitted `risk.AuthorisationResult`.
        It is required rather than computed here so the decision, its reasons,
        and its journal entry all live in one place — a connector that decides
        its own permissions is a connector that can be talked into anything.
        """
        self.require_write_permission("publish_product")
        permitted = getattr(authorisation, "permitted", None)
        if permitted is not True:
            reason = getattr(authorisation, "explain", lambda: "")() or \
                "no authorisation was supplied"
            raise WriteNotPermitted(
                f"Refusing to publish {product_id}: {reason}. Publishing is "
                "irreversible in the way that matters — the page can be indexed "
                "and bought before it can be taken down.")

        product = self.client().update_product({"id": product_id, "status": "ACTIVE"})
        warnings: list[str] = []
        published_to = 0
        if publication_ids:
            result = self.client().publish(product_id, publication_ids)
            published_to = int((result.get("availablePublicationsCount") or {})
                               .get("count") or 0)
        else:
            warnings.append(
                "Status set to ACTIVE but no sales channel publication was "
                "requested. An ACTIVE product that is not published to the Online "
                "Store is still invisible — pass publication_ids to finish.")
        return DataEnvelope("live", self.name, _now(), {
            "product_id": str(product.get("id", "")),
            "status": str(product.get("status", "")),
            "handle": str(product.get("handle", "")),
            "publications": published_to,
        }, warnings)

    def unpublish_product(self, product_id: str) -> DataEnvelope:
        """Return a product to DRAFT. The reverse of the gated step, ungated.

        Deliberately asymmetric: taking a listing down is how you stop a
        problem, so it must never be blocked by the approval that publishing
        needs. Requiring sign-off to undo is how a compliance issue stays live
        overnight.
        """
        self.require_write_permission("unpublish_product")
        product = self.client().update_product({"id": product_id, "status": "DRAFT"})
        return DataEnvelope("live", self.name, _now(), {
            "product_id": str(product.get("id", "")),
            "status": str(product.get("status", "")),
        })

    def update_price(self, sku: str, price: float) -> DataEnvelope:
        """Update a variant price, located by SKU.

        The lookup is by SKU because that is the operator's identifier
        everywhere else; a mismatch is an error rather than a no-op, since a
        silent no-op leaves the repricer believing it acted.
        """
        self.require_write_permission("update_price")
        if price <= 0:
            raise ValueError(f"Refusing to set a non-positive price ({price}) on {sku}.")
        matches = self._find_variants_by_sku(sku)
        if not matches:
            raise ShopifyAPIError(
                f"No variant with SKU {sku!r} exists in this store, so there is "
                "nothing to reprice.", layer="errors")
        if len(matches) > 1:
            raise ShopifyAPIError(
                f"SKU {sku!r} is used by {len(matches)} variants. Refusing to "
                "guess which one to reprice.", layer="errors")
        product_id, variant_id = matches[0]
        updated = self.client().update_variants(
            product_id, [{"id": variant_id, "price": f"{price:.2f}"}])
        return DataEnvelope("live", self.name, _now(), {
            "sku": sku,
            "variant_id": variant_id,
            "price": _f(updated[0].get("price")) if updated else price,
        })

    def set_inventory(self, *, inventory_item_id: str, quantity: int,
                      location_id: str = "", reason: str = "correction",
                      reference_uri: str = "") -> DataEnvelope:
        self.require_write_permission("set_inventory")
        if quantity < 0:
            raise ValueError(f"Refusing to set negative inventory ({quantity}).")
        target = location_id or self.default_location_id()
        group = self.client().set_inventory(
            inventory_item_id=inventory_item_id, location_id=target,
            quantity=quantity, reason=reason, reference_uri=reference_uri)
        return DataEnvelope("live", self.name, _now(), {
            "inventory_item_id": inventory_item_id,
            "location_id": target,
            "quantity": quantity,
            "changes": group.get("changes") or [],
        })

    def create_collection(self, *, title: str, description_html: str = "",
                          handle: str = "") -> DataEnvelope:
        self.require_write_permission("create_collection")
        payload: dict[str, Any] = {"title": title,
                                   "handle": handle or handleize(title)}
        if description_html:
            payload["descriptionHtml"] = description_html
        collection = self.client().create_collection(payload)
        return DataEnvelope("live", self.name, _now(), {
            "collection_id": str(collection.get("id", "")),
            "title": str(collection.get("title", "")),
            "handle": str(collection.get("handle", "")),
        })

    def add_to_collection(self, collection_id: str,
                          product_ids: list[str]) -> DataEnvelope:
        self.require_write_permission("add_to_collection")
        result = self.client().add_products_to_collection(collection_id, product_ids)
        return DataEnvelope("live", self.name, _now(), {
            "collection_id": collection_id,
            "product_count": int((result.get("productsCount") or {}).get("count") or 0),
        })

    def create_discount_code(self, *, code: str, percentage: float,
                             starts_at: str, ends_at: str = "",
                             usage_limit: int | None = None,
                             authorisation: Any = None) -> DataEnvelope:
        """Create a percentage discount code.

        A discount is a permanent, unbounded giveaway if it has no end date and
        no usage cap: codes get shared, and one posted to a deals forum can
        outrun the margin it was meant to test. So an unbounded code requires an
        explicit authorisation, and the discount depth is capped by the caller's
        own policy rather than by anything decided here.
        """
        self.require_write_permission("create_discount_code")
        if not 0 < percentage < 100:
            raise ValueError(
                f"Discount must be between 0 and 100 percent, got {percentage}.")
        unbounded = not ends_at and usage_limit is None
        if unbounded and getattr(authorisation, "permitted", None) is not True:
            raise WriteNotPermitted(
                f"Refusing to create discount {code!r} with no end date and no "
                "usage limit. A shared code with neither runs until someone "
                "notices the margin. Set ends_at or usage_limit, or supply an "
                "authorised action.")
        payload: dict[str, Any] = {
            "title": code,
            "code": code,
            "startsAt": starts_at,
            "customerSelection": {"all": True},
            "customerGets": {
                "value": {"percentage": round(percentage / 100.0, 4)},
                "items": {"all": True},
            },
        }
        if ends_at:
            payload["endsAt"] = ends_at
        if usage_limit is not None:
            payload["usageLimit"] = int(usage_limit)
        node = self.client().create_discount_code(payload)
        return DataEnvelope("live", self.name, _now(), {
            "discount_id": str(node.get("id", "")),
            "code": code,
            "percentage": percentage,
            "ends_at": ends_at,
            "usage_limit": usage_limit,
        })

    # -- helpers -----------------------------------------------------------
    def _find_variants_by_sku(self, sku: str) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for product in self.client().products(query=f"sku:{sku}"):
            for variant in (product.get("variants") or {}).get("nodes") or []:
                if str(variant.get("sku") or "") == sku:
                    found.append((str(product.get("id", "")), str(variant.get("id", ""))))
        return found

    def _summarise_product(self, product: dict[str, Any]) -> dict[str, Any]:
        variants = (product.get("variants") or {}).get("nodes") or []
        prices = [_f(v.get("price")) for v in variants if v.get("price") is not None]
        costs = [_f((v.get("inventoryItem") or {}).get("unitCost", {}).get("amount"))
                 for v in variants
                 if (v.get("inventoryItem") or {}).get("unitCost")]
        return {
            "product_id": str(product.get("id", "")),
            "title": str(product.get("title", "")),
            "handle": str(product.get("handle", "")),
            "status": str(product.get("status", "")),
            "url": product.get("onlineStoreUrl"),
            "tags": product.get("tags") or [],
            "total_inventory": product.get("totalInventory"),
            "variant_count": len(variants),
            "skus": [str(v.get("sku") or "") for v in variants if v.get("sku")],
            "min_price": min(prices) if prices else None,
            "max_price": max(prices) if prices else None,
            # None, not 0.0: an unset cost is unknown, and a zero cost would
            # report infinite margin on every product missing the field.
            "avg_unit_cost": round(sum(costs) / len(costs), 2) if costs else None,
            "seo_title": (product.get("seo") or {}).get("title"),
            "seo_description": (product.get("seo") or {}).get("description"),
            "published_at": product.get("publishedAt"),
            "updated_at": product.get("updatedAt"),
        }

    def _summarise_order(self, order: dict[str, Any]) -> ShopifyOrderSummary:
        total_node = order.get("currentTotalPriceSet") or {}
        currency = str(((total_node.get("shopMoney") or {}).get("currencyCode")) or "")
        refunded = sum(_money_of(r.get("totalRefundedSet"))
                       for r in (order.get("refunds") or []))
        customer = order.get("customer") or {}
        line_items = [{
            "sku": str(li.get("sku") or ""),
            "title": str(li.get("title") or ""),
            "quantity": int(li.get("quantity") or 0),
            "revenue": _money_of(li.get("discountedTotalSet")),
            "unit_cost": _f(((li.get("variant") or {}).get("inventoryItem") or {})
                            .get("unitCost", {}).get("amount"))
                         if ((li.get("variant") or {}).get("inventoryItem") or {})
                            .get("unitCost") else None,
        } for li in ((order.get("lineItems") or {}).get("nodes") or [])]

        return ShopifyOrderSummary(
            order_id=str(order.get("id", "")),
            name=str(order.get("name", "")),
            created_at=str(order.get("createdAt", "")),
            gross_revenue=_money_of(total_node),
            discounts=_money_of(order.get("totalDiscountsSet")),
            shipping=_money_of(order.get("totalShippingPriceSet")),
            tax=_money_of(order.get("totalTaxSet")),
            refunded=round(refunded, 2),
            currency=currency,
            financial_status=str(order.get("displayFinancialStatus", "")),
            fulfillment_status=str(order.get("displayFulfillmentStatus", "")),
            cancelled=bool(order.get("cancelledAt")),
            customer_id=str(customer.get("id", "")),
            customer_order_count=int(customer.get("numberOfOrders") or 0),
            attribution=classify_traffic_source(order.get("customerJourneySummary")),
            line_items=line_items,
        )

    # -- verification ------------------------------------------------------
    def verify_connection(self) -> dict[str, Any]:
        """Prove the token works, and check it has the scopes we will need.

        Returns structured detail rather than raising so a status sweep can
        report every marketplace. Scope checking is included because a missing
        scope produces a 403 at the moment of first use — typically mid-publish,
        which is the worst possible time to discover it.
        """
        status = self._credentials.status()
        if not status.available:
            return {"marketplace": self.name, "ok": False, "detail": status.detail,
                    "credential_source": status.source_name,
                    "remedy": status.remedy}
        try:
            shop = self.shop()
            scopes = set(self.client().access_scopes())
        except (ShopifyAPIError, ConnectorNotConfigured) as exc:
            return {"marketplace": self.name, "ok": False, "detail": str(exc),
                    "credential_source": status.source_name,
                    "remedy": self._credentials.spec.remedy}

        missing_scopes = [s for s in REQUIRED_SCOPES if s not in scopes]
        warnings: list[str] = []
        if missing_scopes:
            warnings.append(
                "Token is valid but missing scope(s): "
                f"{', '.join(missing_scopes)}. Calls needing them will fail with "
                "403 at the point of use. Scopes are fixed at install — add them "
                "in the app configuration and reinstall.")

        creds: ShopifyCredentials = self._credentials.resolve()
        configured = normalise_domain(creds.shop_domain)
        if shop.domain and shop.domain.lower() != configured:
            warnings.append(
                f"Token belongs to {shop.domain}, not the configured "
                f"{configured}. Every write would land in the wrong store.")

        return {
            "marketplace": self.name,
            "ok": not missing_scopes,
            "detail": f"Connected to {shop.name} ({shop.domain}).",
            "credential_source": status.source_name,
            "shop": shop.name,
            "domain": shop.domain,
            "currency": shop.currency,
            "timezone": shop.timezone,
            "plan": shop.plan,
            "api_version": creds.api_version,
            "missing_scopes": missing_scopes,
            "warnings": warnings,
        }

    def status(self) -> dict[str, Any]:
        cred = self._credentials.status()
        return {
            "marketplace": self.name,
            "configured": cred.available,
            "credential_source": cred.source_name,
            # Environment variable names, not internal field names. A status
            # command that reports `shop_domain` makes you go read the source
            # to find out that you set SHOPIFY_SHOP_DOMAIN.
            "missing_env": self.missing_credentials(),
            "writes_allowed": self.allow_writes,
            "docs": self.docs_url,
            "remedy": cred.remedy,
        }
