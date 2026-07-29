"""TikTok Shop API operations.

One method per endpoint, mirroring the API. Domain mapping lives in
`connector.py`.

Pagination on TikTok is cursor-based (`next_page_token`) with a `page_size` cap
of 100 on most search endpoints. As with Amazon, every loop has a page cap — an
unbounded crawl against a large catalogue is how a sync becomes a throttled
multi-hour job that starves every other endpoint, since the QPS limit is shared
across the whole application.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .regions import (
    V_ANALYTICS,
    V_AUTH,
    V_FINANCE,
    V_FULFILMENT,
    V_ORDER,
    V_PRODUCT,
    V_PRODUCT_CATEGORY,
)
from .transport import TikTokAPIError, Transport

MAX_PAGES_DEFAULT = 30
PAGE_SIZE_MAX = 100


def _epoch(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


@dataclass
class ShopInfo:
    shop_id: str
    shop_name: str
    shop_cipher: str
    region: str
    seller_type: str = ""


class TikTokShopClient:
    """Typed access to the TikTok Shop operations the operator needs."""

    def __init__(self, transport: Transport, *, shop_id: str = "") -> None:
        self.t = transport
        self.shop_id = shop_id

    # ------------------------------------------------------------------
    # Authorisation / verification
    # ------------------------------------------------------------------
    def get_authorized_shops(self) -> list[ShopInfo]:
        """The cheapest call that proves the whole auth chain works.

        It also returns `shop_cipher`, which nearly every other endpoint
        requires — so this is both the health check and a required bootstrap
        step. The cipher is per-shop and not interchangeable between shops.
        """
        data = self.t.request(
            method="GET",
            path=f"/authorization/{V_AUTH}/shops",
            needs_shop_cipher=False,
        )
        shops = []
        for s in data.get("shops", []) or []:
            shops.append(ShopInfo(
                shop_id=str(s.get("id", "")),
                shop_name=s.get("name", ""),
                shop_cipher=s.get("cipher", ""),
                region=s.get("region", ""),
                seller_type=s.get("seller_type", ""),
            ))
        return shops

    # ------------------------------------------------------------------
    # Catalogue
    # ------------------------------------------------------------------
    def search_products(
        self,
        *,
        status: str | None = None,
        seller_skus: list[str] | None = None,
        page_size: int = 50,
        max_pages: int = MAX_PAGES_DEFAULT,
    ) -> list[dict]:
        """List the shop's own products. TikTok has no cross-seller search."""
        body: dict[str, Any] = {}
        if status:
            body["status"] = status
        if seller_skus:
            body["seller_skus"] = seller_skus

        out: list[dict] = []
        token: str | None = None
        for _ in range(max_pages):
            query = {"page_size": min(page_size, PAGE_SIZE_MAX)}
            if token:
                query["page_token"] = token
            data = self.t.request(
                method="POST",
                path=f"/product/{V_PRODUCT}/products/search",
                query=query,
                body=body,
            )
            out.extend(data.get("products", []) or [])
            token = data.get("next_page_token")
            if not token:
                break
        return out

    def get_product(self, product_id: str) -> dict:
        return self.t.request(
            method="GET",
            path=f"/product/{V_PRODUCT}/products/{product_id}",
        )

    def get_categories(self) -> list[dict]:
        data = self.t.request(
            method="GET",
            path=f"/product/{V_PRODUCT_CATEGORY}/categories",
        )
        return data.get("categories", []) or []

    def get_category_rules(self, category_id: str) -> dict:
        """Category-specific listing requirements.

        Worth calling before any create: categories differ in required
        attributes, size-chart obligations, and whether a qualification
        document (for example a cosmetics licence) must be attached. A create
        that omits one is rejected wholesale.
        """
        return self.t.request(
            method="GET",
            path=f"/product/{V_PRODUCT_CATEGORY}/categories/{category_id}/rules",
        )

    def get_brands(self, *, page_size: int = 50) -> list[dict]:
        data = self.t.request(
            method="GET",
            path=f"/product/{V_PRODUCT}/brands",
            query={"page_size": min(page_size, PAGE_SIZE_MAX)},
        )
        return data.get("brands", []) or []

    def create_product(self, payload: dict) -> dict:
        """Create a product. Returns product_id and any warnings.

        `payload` must satisfy the category's rules — see `get_category_rules`.
        TikTok validates the whole payload atomically; a single bad attribute
        rejects the entire product rather than creating a partial one.
        """
        self._validate_product_payload(payload)
        return self.t.request(
            method="POST",
            path=f"/product/{V_PRODUCT}/products",
            body=payload,
        )

    def update_product(self, product_id: str, payload: dict) -> dict:
        """Full update. Like the create, this replaces rather than merges."""
        self._validate_product_payload(payload, updating=True)
        return self.t.request(
            method="PUT",
            path=f"/product/{V_PRODUCT}/products/{product_id}",
            body=payload,
        )

    def update_inventory(self, product_id: str, skus: list[dict]) -> dict:
        """Set stock levels.

        `skus` is `[{"id": sku_id, "inventory": [{"warehouse_id": w, "quantity": n}]}]`.
        Warehouse ID is mandatory for shops with more than one warehouse and
        omitting it does not error — it updates the default, which may not be
        the one holding the stock.
        """
        if not skus:
            raise ValueError("update_inventory called with no SKUs.")
        return self.t.request(
            method="POST",
            path=f"/product/{V_PRODUCT}/products/{product_id}/inventory/update",
            body={"skus": skus},
        )

    def update_price(self, product_id: str, skus: list[dict]) -> dict:
        """Set prices. `skus` is `[{"id": sku_id, "price": {"amount": "12.34",
        "currency": "USD"}}]`.

        Amounts are strings on this endpoint. Sending a float works until a
        value like 12.30 serialises as 12.3 and TikTok rejects it, so the
        connector formats them.
        """
        if not skus:
            raise ValueError("update_price called with no SKUs.")
        return self.t.request(
            method="POST",
            path=f"/product/{V_PRODUCT}/products/{product_id}/prices/update",
            body={"skus": skus},
        )

    def activate_product(self, product_ids: list[str]) -> dict:
        return self.t.request(
            method="POST",
            path=f"/product/{V_PRODUCT}/products/activate",
            body={"product_ids": product_ids},
        )

    def deactivate_product(self, product_ids: list[str]) -> dict:
        return self.t.request(
            method="POST",
            path=f"/product/{V_PRODUCT}/products/deactivate",
            body={"product_ids": product_ids},
        )

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    def search_orders(
        self,
        *,
        create_time_from: datetime | None = None,
        create_time_to: datetime | None = None,
        order_status: str | None = None,
        page_size: int = 50,
        max_pages: int = MAX_PAGES_DEFAULT,
    ) -> list[dict]:
        body: dict[str, Any] = {}
        if create_time_from:
            body["create_time_ge"] = _epoch(create_time_from)
        if create_time_to:
            body["create_time_lt"] = _epoch(create_time_to)
        if order_status:
            body["order_status"] = order_status

        out: list[dict] = []
        token: str | None = None
        for _ in range(max_pages):
            query = {"page_size": min(page_size, PAGE_SIZE_MAX), "sort_field": "create_time"}
            if token:
                query["page_token"] = token
            data = self.t.request(
                method="POST",
                path=f"/order/{V_ORDER}/orders/search",
                query=query,
                body=body,
            )
            out.extend(data.get("orders", []) or [])
            token = data.get("next_page_token")
            if not token:
                break
        return out

    def get_order_detail(self, order_ids: list[str]) -> list[dict]:
        """Batch detail lookup. TikTok caps this at 50 ids per call."""
        out: list[dict] = []
        for i in range(0, len(order_ids), 50):
            batch = order_ids[i:i + 50]
            data = self.t.request(
                method="GET",
                path=f"/order/{V_ORDER}/orders",
                query={"ids": ",".join(batch)},
            )
            out.extend(data.get("orders", []) or [])
        return out

    # ------------------------------------------------------------------
    # Finance — the only trustworthy source of realised revenue
    # ------------------------------------------------------------------
    def get_statements(
        self,
        *,
        statement_time_from: datetime | None = None,
        statement_time_to: datetime | None = None,
        page_size: int = 50,
        max_pages: int = MAX_PAGES_DEFAULT,
    ) -> list[dict]:
        """Settlement statements.

        Order totals are what the buyer was charged. Statements are what TikTok
        actually paid after commission, transaction fees, affiliate payouts, and
        seller-funded promotions. The gap between the two is often 15-25%, so
        profit must be computed from statements, never from order value.
        """
        out: list[dict] = []
        token: str | None = None
        for _ in range(max_pages):
            query: dict[str, Any] = {
                "page_size": min(page_size, PAGE_SIZE_MAX),
                "sort_field": "statement_time",
            }
            if statement_time_from:
                query["statement_time_ge"] = _epoch(statement_time_from)
            if statement_time_to:
                query["statement_time_lt"] = _epoch(statement_time_to)
            if token:
                query["page_token"] = token
            data = self.t.request(
                method="GET",
                path=f"/finance/{V_FINANCE}/statements",
                query=query,
            )
            out.extend(data.get("statements", []) or [])
            token = data.get("next_page_token")
            if not token:
                break
        return out

    def get_statement_transactions(self, statement_id: str,
                                   *, page_size: int = 50,
                                   max_pages: int = MAX_PAGES_DEFAULT) -> list[dict]:
        """Line-item breakdown for one statement — the real fee detail."""
        out: list[dict] = []
        token: str | None = None
        for _ in range(max_pages):
            query: dict[str, Any] = {"page_size": min(page_size, PAGE_SIZE_MAX)}
            if token:
                query["page_token"] = token
            data = self.t.request(
                method="GET",
                path=f"/finance/{V_FINANCE}/statements/{statement_id}/statement_transactions",
                query=query,
            )
            out.extend(data.get("statement_transactions", []) or [])
            token = data.get("next_page_token")
            if not token:
                break
        return out

    # ------------------------------------------------------------------
    # Analytics — the shop's own performance, not the market's
    # ------------------------------------------------------------------
    def get_shop_performance(self, *, start: datetime, end: datetime,
                             granularity: str = "1D") -> dict:
        return self.t.request(
            method="GET",
            path=f"/analytics/{V_ANALYTICS}/shop/performance",
            query={
                "start_date_ge": start.strftime("%Y-%m-%d"),
                "end_date_lt": end.strftime("%Y-%m-%d"),
                "granularity": granularity,
            },
        )

    def get_product_performance(self, *, start: datetime, end: datetime,
                                page_size: int = 50,
                                max_pages: int = MAX_PAGES_DEFAULT) -> list[dict]:
        """Per-product views, clicks, orders, GMV over a window.

        This is the input to trend analysis. It describes *our* products, which
        is a real and useful signal — but it is not market demand, and it says
        nothing about what competitors are doing.
        """
        out: list[dict] = []
        token: str | None = None
        for _ in range(max_pages):
            query: dict[str, Any] = {
                "start_date_ge": start.strftime("%Y-%m-%d"),
                "end_date_lt": end.strftime("%Y-%m-%d"),
                "page_size": min(page_size, PAGE_SIZE_MAX),
                "sort_field": "gmv",
                "sort_order": "DESC",
            }
            if token:
                query["page_token"] = token
            data = self.t.request(
                method="GET",
                path=f"/analytics/{V_ANALYTICS}/shop_products/performance",
                query=query,
            )
            out.extend(data.get("products", []) or [])
            token = data.get("next_page_token")
            if not token:
                break
        return out

    # ------------------------------------------------------------------
    # Fulfilment
    # ------------------------------------------------------------------
    def get_warehouses(self) -> list[dict]:
        data = self.t.request(
            method="GET",
            path=f"/logistics/{V_FULFILMENT}/warehouses",
        )
        return data.get("warehouses", []) or []

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_product_payload(payload: dict, *, updating: bool = False) -> None:
        """Catch the payload mistakes that produce unhelpful server errors.

        TikTok's rejection messages for structural problems are vague, and each
        round trip costs a rate-limit slot, so the obvious ones are caught here
        with an actionable message instead.
        """
        required = {"title", "description", "category_id", "package_weight", "skus"}
        missing = required - set(payload)
        if missing:
            raise ValueError(
                f"TikTok product payload is missing {sorted(missing)}. Required: "
                f"{sorted(required)}. Fetch the category's rules first — required "
                "attributes vary by category."
            )

        skus = payload.get("skus") or []
        if not skus:
            raise ValueError("A TikTok product needs at least one SKU.")

        for i, sku in enumerate(skus):
            if "price" not in sku:
                raise ValueError(f"SKU {i} has no price.")
            price = sku["price"]
            if not isinstance(price, dict) or "amount" not in price:
                raise ValueError(
                    f"SKU {i} price must be {{'amount': '12.34', 'currency': 'USD'}}. "
                    "TikTok expects the amount as a string; a float that serialises "
                    "as 12.3 is rejected."
                )
            if not isinstance(price["amount"], str):
                raise ValueError(
                    f"SKU {i} price amount must be a string, got "
                    f"{type(price['amount']).__name__}."
                )

        title = payload.get("title", "")
        if len(title) > 255:
            raise ValueError(f"Title is {len(title)} chars; TikTok's limit is 255.")
