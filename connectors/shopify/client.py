"""Shopify Admin API client — mirrors the API, no business logic.

The split matters and is the same one the Amazon connector uses: this file
knows GraphQL field names, cursors and page sizes; `connector.py` knows what a
product means to the business. Mixing them produces a client that cannot be
tested without a policy and a domain layer that breaks on an API rename.

The one piece of judgement that does live here is **page size**, because it is
an API concern rather than a business one. Shopify's cost limiter charges by
returned node count, so a large page is not free — it is the same points spent
sooner, and a page big enough to exceed the bucket fails outright no matter how
long you wait. `PAGE_SIZE` is tuned to stay well inside a default bucket while
still exhausting a mid-size catalogue in a handful of calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from . import queries
from .transport import ShopifyAPIError, Transport

PAGE_SIZE = 50
# Orders carry line items and a customer journey, so each node is far more
# expensive than a product node. Paging them at 50 routinely trips the bucket.
ORDER_PAGE_SIZE = 25
MAX_PAGES = 400  # ~20k products. A run that hits this has a query bug, not a big catalogue.


@dataclass
class ShopInfo:
    id: str
    name: str
    domain: str
    currency: str
    timezone: str
    plan: str
    country: str


class ShopifyClient:
    """One method per API operation. Returns Shopify's shapes, not ours."""

    def __init__(self, transport: Transport) -> None:
        self.transport = transport

    # -- paging ------------------------------------------------------------
    def _paginate(self, query: str, connection: str, *, operation: str,
                  page_size: int = PAGE_SIZE,
                  variables: dict[str, Any] | None = None,
                  cost: float = 20.0) -> Iterator[dict[str, Any]]:
        """Walk a cursor connection to exhaustion.

        Deliberately a generator over *all* pages. Returning the first page and
        letting callers opt into more is how a truncated catalogue gets treated
        as the whole catalogue.
        """
        cursor: str | None = None
        for page in range(MAX_PAGES):
            payload = dict(variables or {})
            payload.update({"first": page_size, "after": cursor})
            data = self.transport.execute(
                query, payload, operation=f"{operation} p{page + 1}",
                estimated_cost=cost)
            block = data.get(connection) or {}
            for node in block.get("nodes") or []:
                yield node
            info = block.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return
            cursor = info.get("endCursor")
            if not cursor:
                # hasNextPage true with no cursor would loop forever on page 1.
                raise ShopifyAPIError(
                    f"{operation} reported another page but returned no cursor. "
                    "Refusing to loop; the result so far is incomplete.",
                    layer="errors")
        raise ShopifyAPIError(
            f"{operation} exceeded {MAX_PAGES} pages. Stopping rather than "
            "paging forever — narrow the query filter.", layer="errors")

    # -- shop --------------------------------------------------------------
    def shop(self) -> ShopInfo:
        data = self.transport.execute(queries.SHOP, operation="shop", estimated_cost=2)
        raw = data.get("shop") or {}
        if not raw:
            raise ShopifyAPIError("Shop query returned no shop.", layer="errors")
        return ShopInfo(
            id=str(raw.get("id", "")),
            name=str(raw.get("name", "")),
            domain=str(raw.get("myshopifyDomain", "")),
            currency=str(raw.get("currencyCode", "")),
            timezone=str(raw.get("ianaTimezone", "")),
            plan=str((raw.get("plan") or {}).get("displayName", "")),
            country=str((raw.get("billingAddress") or {}).get("countryCodeV2", "")),
        )

    def access_scopes(self) -> list[str]:
        data = self.transport.execute(queries.ACCESS_SCOPES, operation="accessScopes",
                                      estimated_cost=2)
        install = data.get("currentAppInstallation") or {}
        return [str(s.get("handle", "")) for s in (install.get("accessScopes") or [])]

    def locations(self) -> list[dict[str, Any]]:
        return list(self._paginate(queries.LOCATIONS, "locations",
                                   operation="locations", page_size=20, cost=10))

    def publications(self) -> list[dict[str, Any]]:
        data = self.transport.execute(queries.PUBLICATIONS, {"first": 20},
                                      operation="publications", estimated_cost=5)
        return (data.get("publications") or {}).get("nodes") or []

    # -- products ----------------------------------------------------------
    def products(self, *, query: str | None = None) -> list[dict[str, Any]]:
        return list(self._paginate(queries.PRODUCTS, "products", operation="products",
                                   variables={"query": query}, cost=40))

    def product(self, product_id: str) -> dict[str, Any]:
        data = self.transport.execute(queries.PRODUCT_BY_ID, {"id": product_id},
                                      operation="product", estimated_cost=10)
        product = data.get("product")
        if product is None:
            raise ShopifyAPIError(
                f"No product with id {product_id}. A null product is Shopify's "
                "answer for both 'deleted' and 'not visible to this token'.",
                layer="errors")
        return product

    def create_product(self, product_input: dict[str, Any]) -> dict[str, Any]:
        data = self.transport.execute(
            queries.PRODUCT_CREATE, {"product": product_input},
            operation="productCreate", mutation_field="productCreate",
            estimated_cost=20)
        return (data.get("productCreate") or {}).get("product") or {}

    def update_product(self, product_input: dict[str, Any]) -> dict[str, Any]:
        data = self.transport.execute(
            queries.PRODUCT_UPDATE, {"product": product_input},
            operation="productUpdate", mutation_field="productUpdate",
            estimated_cost=20)
        return (data.get("productUpdate") or {}).get("product") or {}

    def create_variants(self, product_id: str,
                        variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
        data = self.transport.execute(
            queries.VARIANTS_BULK_CREATE,
            {"productId": product_id, "variants": variants},
            operation="productVariantsBulkCreate",
            mutation_field="productVariantsBulkCreate", estimated_cost=20)
        return (data.get("productVariantsBulkCreate") or {}).get("productVariants") or []

    def update_variants(self, product_id: str,
                        variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
        data = self.transport.execute(
            queries.VARIANTS_BULK_UPDATE,
            {"productId": product_id, "variants": variants},
            operation="productVariantsBulkUpdate",
            mutation_field="productVariantsBulkUpdate", estimated_cost=20)
        return (data.get("productVariantsBulkUpdate") or {}).get("productVariants") or []

    def publish(self, publishable_id: str,
                publication_ids: list[str]) -> dict[str, Any]:
        payload = [{"publicationId": pid} for pid in publication_ids]
        data = self.transport.execute(
            queries.PUBLISHABLE_PUBLISH, {"id": publishable_id, "input": payload},
            operation="publishablePublish", mutation_field="publishablePublish",
            estimated_cost=15)
        return (data.get("publishablePublish") or {}).get("publishable") or {}

    # -- inventory ---------------------------------------------------------
    def set_inventory(self, *, inventory_item_id: str, location_id: str,
                      quantity: int, reason: str = "correction",
                      reference_uri: str = "") -> dict[str, Any]:
        quantity_entry: dict[str, Any] = {
            "inventoryItemId": inventory_item_id,
            "locationId": location_id,
            "quantity": int(quantity),
        }
        payload: dict[str, Any] = {
            "name": "available",
            "reason": reason,
            "ignoreCompareQuantity": True,
            "quantities": [quantity_entry],
        }
        if reference_uri:
            payload["referenceDocumentUri"] = reference_uri
        data = self.transport.execute(
            queries.INVENTORY_SET, {"input": payload},
            operation="inventorySetQuantities",
            mutation_field="inventorySetQuantities", estimated_cost=15)
        return (data.get("inventorySetQuantities") or {}).get(
            "inventoryAdjustmentGroup") or {}

    def inventory_levels(self) -> list[dict[str, Any]]:
        return list(self._paginate(queries.INVENTORY_LEVELS, "productVariants",
                                   operation="inventoryLevels", page_size=25,
                                   cost=40))

    # -- orders / customers ------------------------------------------------
    def orders(self, *, query: str | None = None) -> list[dict[str, Any]]:
        return list(self._paginate(queries.ORDERS, "orders", operation="orders",
                                   page_size=ORDER_PAGE_SIZE,
                                   variables={"query": query}, cost=60))

    def customers(self, *, query: str | None = None) -> list[dict[str, Any]]:
        return list(self._paginate(queries.CUSTOMERS, "customers",
                                   operation="customers",
                                   variables={"query": query}, cost=20))

    # -- collections / discounts -------------------------------------------
    def collections(self) -> list[dict[str, Any]]:
        return list(self._paginate(queries.COLLECTIONS, "collections",
                                   operation="collections", cost=15))

    def create_collection(self, collection_input: dict[str, Any]) -> dict[str, Any]:
        data = self.transport.execute(
            queries.COLLECTION_CREATE, {"input": collection_input},
            operation="collectionCreate", mutation_field="collectionCreate",
            estimated_cost=15)
        return (data.get("collectionCreate") or {}).get("collection") or {}

    def add_products_to_collection(self, collection_id: str,
                                   product_ids: list[str]) -> dict[str, Any]:
        data = self.transport.execute(
            queries.COLLECTION_ADD_PRODUCTS,
            {"id": collection_id, "productIds": product_ids},
            operation="collectionAddProducts",
            mutation_field="collectionAddProducts", estimated_cost=15)
        return (data.get("collectionAddProducts") or {}).get("collection") or {}

    def create_discount_code(self, discount_input: dict[str, Any]) -> dict[str, Any]:
        data = self.transport.execute(
            queries.DISCOUNT_CODE_CREATE, {"basicCodeDiscount": discount_input},
            operation="discountCodeBasicCreate",
            mutation_field="discountCodeBasicCreate", estimated_cost=15)
        return (data.get("discountCodeBasicCreate") or {}).get("codeDiscountNode") or {}
