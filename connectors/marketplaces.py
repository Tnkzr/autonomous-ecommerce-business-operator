"""Concrete connectors for the five marketplaces.

Each declares the exact credentials its real API needs, so `operator status`
tells you precisely what to provision. The request/response plumbing is left
unimplemented on purpose: writing untested API calls against live seller
accounts — where a malformed call can delist products or mis-price inventory —
is not something to do speculatively. Each class documents the endpoint to
implement and the gotcha that matters most for it.
"""

from __future__ import annotations

from typing import Any

from .base import DataEnvelope, MarketplaceConnector


class _UnimplementedReads(MarketplaceConnector):
    """Shared read stubs that fail loudly rather than fabricating data."""

    def _reads(self, what: str) -> DataEnvelope:
        self.require_credentials()   # raises with a precise, actionable message
        raise NotImplementedError(
            f"{self.name}.{what} needs implementing against the live API. "
            f"Credentials are present; wire the endpoint noted in the class docstring."
        )

    def fetch_orders(self, *, since: str) -> DataEnvelope:
        return self._reads("fetch_orders")

    def fetch_inventory(self) -> DataEnvelope:
        return self._reads("fetch_inventory")

    def fetch_listings(self) -> DataEnvelope:
        return self._reads("fetch_listings")

    def fetch_competitor_offers(self, identifier: str) -> DataEnvelope:
        return self._reads("fetch_competitor_offers")

    def fetch_reviews(self, sku: str) -> DataEnvelope:
        return self._reads("fetch_reviews")

    def fetch_ad_performance(self, *, since: str) -> DataEnvelope:
        return self._reads("fetch_ad_performance")


class AmazonConnector(_UnimplementedReads):
    """Amazon SP-API + Amazon Ads API.

    Endpoints: Orders v0, FBA Inventory v1, Listings Items 2021-08-01,
    Product Pricing v0 (competitive offers), Ads v2 reporting.

    Gotcha: SP-API rate limits are per-operation and burst-bucketed. Reporting
    endpoints are asynchronous — you request a report, poll for it, then
    download. Treat 429s as normal and back off; hammering them gets the
    application flagged.
    """

    name = "amazon"
    required_env = (
        "AMZ_LWA_CLIENT_ID", "AMZ_LWA_CLIENT_SECRET", "AMZ_REFRESH_TOKEN",
        "AMZ_SELLER_ID", "AMZ_MARKETPLACE_ID",
    )
    docs_url = "https://developer-docs.amazon.com/sp-api/"


class ShopifyConnector(_UnimplementedReads):
    """Shopify Admin GraphQL API (2024-10+).

    Endpoints: orders, productVariants, inventoryLevels, publications.

    Gotcha: the GraphQL API is cost-throttled, not request-throttled. Ask for
    fewer fields to buy more calls. REST is deprecated for new work.
    """

    name = "shopify"
    required_env = ("SHOPIFY_STORE_DOMAIN", "SHOPIFY_ADMIN_ACCESS_TOKEN")
    docs_url = "https://shopify.dev/docs/api/admin-graphql"


class WalmartConnector(_UnimplementedReads):
    """Walmart Marketplace API.

    Endpoints: /v3/orders, /v3/inventory, /v3/items, /v3/price.

    Gotcha: auth is a signed token exchange with a short TTL, and Walmart
    enforces item-setup schema validation strictly — a listing that fails
    validation is rejected wholesale, not partially.
    """

    name = "walmart"
    required_env = ("WALMART_CLIENT_ID", "WALMART_CLIENT_SECRET")
    docs_url = "https://developer.walmart.com/doc/us/mp/us-mp-getting-started/"


class EbayConnector(_UnimplementedReads):
    """eBay Sell APIs.

    Endpoints: Fulfillment (orders), Inventory, Marketing, Browse (rival offers).

    Gotcha: OAuth user tokens expire in ~2 hours; the refresh token is the one
    to persist. The Browse API is the correct source for competitor pricing —
    scraping listing pages violates the site terms.
    """

    name = "ebay"
    required_env = ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_REFRESH_TOKEN")
    docs_url = "https://developer.ebay.com/api-docs/sell/static/oauth/oauth-tokens.html"


class TikTokShopConnector(_UnimplementedReads):
    """TikTok Shop Partner API.

    Endpoints: /order/202309/orders/search, /product/202312/products/search,
    /logistics, ads via the TikTok Marketing API.

    Gotcha: every request needs an HMAC-SHA256 signature over sorted query
    params plus the body. Sign incorrectly and you get an opaque error, so
    build and test the signer before anything else.
    """

    name = "tiktok"
    required_env = ("TIKTOK_APP_KEY", "TIKTOK_APP_SECRET", "TIKTOK_SHOP_ACCESS_TOKEN",
                    "TIKTOK_SHOP_ID")
    docs_url = "https://partner.tiktokshop.com/docv2/page/api-overview"


CONNECTOR_REGISTRY: dict[str, type[MarketplaceConnector]] = {
    "amazon": AmazonConnector,
    "shopify": ShopifyConnector,
    "walmart": WalmartConnector,
    "ebay": EbayConnector,
    "tiktok": TikTokShopConnector,
}


def get_connector(marketplace: str, *, allow_writes: bool = False) -> MarketplaceConnector:
    key = marketplace.strip().lower()
    if key not in CONNECTOR_REGISTRY:
        raise KeyError(f"Unknown marketplace {marketplace!r}. Known: {sorted(CONNECTOR_REGISTRY)}")
    return CONNECTOR_REGISTRY[key](allow_writes=allow_writes)


def all_status(*, allow_writes: bool = False) -> list[dict[str, Any]]:
    return [
        cls(allow_writes=allow_writes).status()
        for cls in CONNECTOR_REGISTRY.values()
    ]
