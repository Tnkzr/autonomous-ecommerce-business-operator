"""Connector registry.

Amazon (`connectors.amazon`) and TikTok Shop (`connectors.tiktok`) are fully
implemented. Shopify, Walmart, and eBay are still declarations: they name the
exact credentials their real API needs, so `operator status` reports precisely
what to provision, and their reads fail loudly rather than fabricating data.
Each documents the endpoints to implement and the gotcha that matters most.
"""

from __future__ import annotations

from typing import Any

from .amazon import AmazonConnector
from .tiktok import TikTokShopConnector
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
