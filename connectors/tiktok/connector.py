"""TikTokShopConnector: the TikTok Shop API wired into the operator's models.

Reads return `DataEnvelope` tagged `live`. Writes are implemented and still
pass `require_write_permission()` — on TikTok this matters more than elsewhere,
because a rejected product edit can deactivate a listing rather than leaving the
previous version in place.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from ..base import ConnectorNotConfigured, DataEnvelope, MarketplaceConnector
from .auth import TikTokCredentials, TokenProvider
from .client import ShopInfo, TikTokShopClient
from .credentials import (
    CredentialSource,
    CredentialsUnavailable,
    FileCredentials,
    default_source,
)
from .regions import resolve
from .transport import TikTokAPIError, Transport

# TikTok Shop's prohibited-products policy is stricter than Amazon's in several
# categories and enforcement is faster. These are screened before any listing
# write so the operator cannot publish something that gets the shop suspended.
TIKTOK_PROHIBITED_TERMS = (
    "weight loss", "slimming", "detox", "appetite suppressant",
    "cbd", "hemp", "kratom", "nicotine", "vape", "e-liquid",
    "prescription", "antibiotic", "vaccine", "covid",
    "whitening injection", "breast enlargement", "male enhancement",
    "counterfeit", "replica", "knockoff", "firearm", "ammunition",
    "pepper spray", "taser", "lock pick", "jammer",
    "live animal", "endangered", "ivory", "shark fin",
    "lottery", "gambling", "casino", "crypto", "nft",
    "recall", "used underwear", "contact lens",
)

# Claims that are ordinary marketing elsewhere and are policy violations here.
TIKTOK_RESTRICTED_CLAIMS = (
    "cure", "cures", "treat", "treats", "heal", "heals", "fda approved",
    "clinically proven", "doctor recommended", "100% effective",
    "guaranteed results", "lose weight fast", "anti-aging", "reverses",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class TikTokShopConnector(MarketplaceConnector):
    """TikTok Shop Partner API.

    Endpoints used: Authorization 202309, Product 202309, Order 202309,
    Finance 202309, Analytics 202405, Logistics 202309.
    """

    name = "tiktok"
    required_env = (
        "TIKTOK_APP_KEY", "TIKTOK_APP_SECRET", "TIKTOK_REFRESH_TOKEN", "TIKTOK_SHOP_ID",
    )
    docs_url = "https://partner.tiktokshop.com/docv2/page/api-overview"

    def __init__(self, *, allow_writes: bool = False, transport: Transport | None = None,
                 client: TikTokShopClient | None = None,
                 credentials: CredentialSource | None = None) -> None:
        super().__init__(allow_writes=allow_writes)
        self._client = client
        self._transport = transport
        # Where credentials come from is injected, so the rest of this class
        # never learns whether they live in the environment, a file, or a
        # secrets manager. Swapping the source changes nothing below.
        self._credentials = credentials or default_source()
        self._shop: ShopInfo | None = None
        self._shops: list[ShopInfo] = []
        self.auth_warnings: list[str] = []

    # ------------------------------------------------------------------
    # wiring
    # ------------------------------------------------------------------
    @property
    def shop_id(self) -> str:
        return os.environ.get("TIKTOK_SHOP_ID", "")

    @property
    def region(self) -> str:
        return os.environ.get("TIKTOK_REGION", "US")

    @property
    def sandbox(self) -> bool:
        return os.environ.get("TIKTOK_SANDBOX", "").lower() in ("1", "true", "yes")

    @property
    def currency(self) -> str:
        try:
            return resolve(self.region, sandbox=self.sandbox)[1]
        except Exception:
            return "USD"

    @property
    def settlement_lag_days(self) -> int:
        try:
            return resolve(self.region, sandbox=self.sandbox)[2]
        except Exception:
            return 15

    def client(self) -> TikTokShopClient:
        """Build (and cache) the API client, resolving shop_cipher on the way.

        The cipher is not a credential you configure — it is returned by the
        authorisation endpoint and required by nearly every other call, so it
        has to be fetched once during construction.
        """
        if self._client is not None:
            return self._client

        creds = self._credentials.resolve()   # raises CredentialsUnavailable
        base_url, _currency, _lag = resolve(self.region, sandbox=self.sandbox)

        # A file source can absorb TikTok's refresh-token rotation; an
        # environment variable cannot, so the callback is wired only when the
        # source can actually persist.
        on_rotated = (self._credentials.persist_refresh_token
                      if isinstance(self._credentials, FileCredentials) else None)
        tokens = TokenProvider(creds, on_refresh_token_rotated=on_rotated)

        transport = self._transport or Transport(
            base_url=base_url,
            app_key=creds.app_key,
            app_secret=creds.app_secret,
            token_provider=tokens,
        )
        client = TikTokShopClient(transport, shop_id=self.shop_id)

        shops = client.get_authorized_shops()
        shop = next((s for s in shops if s.shop_id == self.shop_id), None)
        if shop is None:
            available = ", ".join(f"{s.shop_id} ({s.shop_name})" for s in shops) or "none"
            raise ConnectorNotConfigured(
                f"TIKTOK_SHOP_ID={self.shop_id} is not among the shops this app is "
                f"authorised for. Authorised: {available}. Every subsequent call "
                "would fail or return another shop's data."
            )
        transport.shop_cipher = shop.shop_cipher
        self._shop = shop
        self._shops = shops
        self.auth_warnings = list(getattr(tokens, "warnings", []))
        self._client = client
        return client

    # ------------------------------------------------------------------
    # verification
    # ------------------------------------------------------------------
    def verify_connection(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "marketplace": self.name,
            "ok": False,
            "checked_at": _now(),
            "detail": "",
            "sandbox": self.sandbox,
        }

        cred_status = self._credentials.status()
        result["credential_source"] = cred_status.source_name
        if not cred_status.available:
            result["detail"] = cred_status.detail
            result["remedy"] = cred_status.remedy
            return result

        try:
            base_url, currency, lag = resolve(self.region, sandbox=self.sandbox)
        except ValueError as exc:
            result["detail"] = str(exc)
            return result
        result.update(endpoint=base_url, region=self.region, currency=currency,
                      settlement_lag_days=lag)

        try:
            # client() already fetches the shop list to resolve shop_cipher;
            # reuse it rather than spending a second rate-limit slot on the
            # same data.
            self.client()
            shops = self._shops
        except ConnectorNotConfigured as exc:
            result["detail"] = str(exc)
            return result
        except TikTokAPIError as exc:
            result["detail"] = str(exc)
            result["code"] = exc.code
            result["status_code"] = exc.status
            return result
        except Exception as exc:
            result["detail"] = f"{type(exc).__name__}: {exc}"
            return result

        result["authorised_shops"] = [f"{s.shop_id} ({s.shop_name})" for s in shops]
        if self._shop:
            result["shop_name"] = self._shop.shop_name
            result["shop_region"] = self._shop.region
            # A cipher resolving means the bootstrap worked end to end.
            result["shop_cipher_resolved"] = bool(self._shop.shop_cipher)
            if self._shop.region and self._shop.region.upper() != self.region.upper():
                result["detail"] = (
                    f"Authenticated, but the shop's region is {self._shop.region} while "
                    f"TIKTOK_REGION={self.region}. Currency and settlement lag would be "
                    "wrong, which silently corrupts every profit calculation."
                )
                return result

        result["ok"] = True
        result["detail"] = (
            f"Authenticated to shop {self.shop_id}"
            + (f" ({self._shop.shop_name})" if self._shop else "")
            + (" [SANDBOX]" if self.sandbox else "")
        )
        result["warnings"] = self.auth_warnings
        return result

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def fetch_orders(self, *, since: str) -> DataEnvelope:
        c = self.client()
        start = self._parse_since(since)
        orders = c.search_orders(create_time_from=start)

        rows: list[dict[str, Any]] = []
        for o in orders:
            payment = o.get("payment") or {}
            rows.append({
                "order_id": o.get("id"),
                "status": o.get("status"),
                "created_at": self._epoch_to_iso(o.get("create_time")),
                "buyer_total": _f(payment.get("total_amount")),
                "sub_total": _f(payment.get("sub_total")),
                "shipping_fee": _f(payment.get("shipping_fee")),
                "seller_discount": _f(payment.get("seller_discount")),
                "platform_discount": _f(payment.get("platform_discount")),
                "tax": _f(payment.get("tax")),
                "currency": payment.get("currency", self.currency),
                "line_items": [
                    {
                        "sku_id": li.get("sku_id"),
                        "seller_sku": li.get("seller_sku"),
                        "product_id": li.get("product_id"),
                        "product_name": li.get("product_name"),
                        "sale_price": _f(li.get("sale_price")),
                        "platform_discount": _f(li.get("platform_discount")),
                        "seller_discount": _f(li.get("seller_discount")),
                    }
                    for li in (o.get("line_items") or [])
                ],
            })

        warnings = [
            "Order totals are what the buyer paid, not what TikTok will pay you. "
            "Commission, transaction fees, affiliate payouts, and seller-funded "
            "promotions come out of this — often 15-25%. Use fetch_settlements "
            "for realised revenue."
        ]
        unpaid = sum(1 for o in orders if (o.get("status") or "").upper() in
                     ("UNPAID", "ON_HOLD"))
        if unpaid:
            warnings.append(
                f"{unpaid} order(s) are UNPAID or ON_HOLD and may never settle. "
                "They are listed but should not be counted as revenue."
            )

        return DataEnvelope(source="live", marketplace=self.name, fetched_at=_now(),
                            payload=rows, warnings=warnings)

    def fetch_inventory(self) -> DataEnvelope:
        products = self.client().search_products()
        rows: list[dict[str, Any]] = []
        for p in products:
            for sku in p.get("skus", []) or []:
                stock = sum(
                    int(_f(inv.get("quantity")))
                    for inv in (sku.get("inventory") or [])
                )
                rows.append({
                    "product_id": p.get("id"),
                    "sku_id": sku.get("id"),
                    "sku": sku.get("seller_sku"),
                    "title": p.get("title"),
                    "status": p.get("status"),
                    "on_hand_units": stock,
                    "inbound_units": 0,   # TikTok exposes no inbound concept here
                    "price": _f((sku.get("price") or {}).get("sale_price")),
                    "currency": (sku.get("price") or {}).get("currency", self.currency),
                    "warehouses": [inv.get("warehouse_id")
                                   for inv in (sku.get("inventory") or [])],
                })

        warnings = []
        oos = [r["sku"] for r in rows if r["on_hand_units"] == 0]
        if oos:
            warnings.append(
                f"{len(oos)} SKU(s) at zero stock: {', '.join(str(s) for s in oos[:5])}"
                + ("…" if len(oos) > 5 else "")
                + ". TikTok suppresses out-of-stock products from the feed, and "
                "recovering that placement takes longer than the stockout itself."
            )
        multi = [r["sku"] for r in rows if len(r["warehouses"]) > 1]
        if multi:
            warnings.append(
                f"{len(multi)} SKU(s) span multiple warehouses. Inventory writes must "
                "name the warehouse explicitly — omitting it updates the default, "
                "which may not be the one holding the stock."
            )
        return DataEnvelope(source="live", marketplace=self.name, fetched_at=_now(),
                            payload=rows, warnings=warnings)

    def fetch_listings(self) -> DataEnvelope:
        products = self.client().search_products()
        rows = [{
            "product_id": p.get("id"),
            "title": p.get("title"),
            "status": p.get("status"),
            "category_id": (p.get("category_chains") or [{}])[-1].get("id"),
            "created_at": self._epoch_to_iso(p.get("create_time")),
            "updated_at": self._epoch_to_iso(p.get("update_time")),
            "sku_count": len(p.get("skus") or []),
            "min_price": min(
                (_f((s.get("price") or {}).get("sale_price"))
                 for s in (p.get("skus") or [])), default=0.0),
        } for p in products]

        warnings = []
        inactive = [r["product_id"] for r in rows
                    if (r["status"] or "").upper() not in ("ACTIVATE", "LIVE", "")]
        if inactive:
            warnings.append(
                f"{len(inactive)} product(s) are not live (status: "
                f"{', '.join(sorted({str(r['status']) for r in rows if r['product_id'] in inactive}))}). "
                "TikTok deactivates on policy review as well as on seller action — "
                "check the reason rather than simply reactivating."
            )
        return DataEnvelope(source="live", marketplace=self.name, fetched_at=_now(),
                            payload=rows, warnings=warnings)

    def fetch_settlements(self, *, days: int = 30) -> DataEnvelope:
        """Realised revenue after all platform deductions."""
        c = self.client()
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        statements = c.get_statements(statement_time_from=start, statement_time_to=end)

        rows = [{
            "statement_id": s.get("id"),
            "statement_time": self._epoch_to_iso(s.get("statement_time")),
            "settlement_amount": _f(s.get("settlement_amount")),
            "revenue_amount": _f(s.get("revenue_amount")),
            "fee_amount": _f(s.get("fee_amount")),
            "adjustment_amount": _f(s.get("adjustment_amount")),
            "currency": s.get("currency", self.currency),
            "status": s.get("status"),
        } for s in statements]

        total_revenue = sum(r["revenue_amount"] for r in rows)
        total_fees = sum(abs(r["fee_amount"]) for r in rows)
        take_rate = (total_fees / total_revenue * 100) if total_revenue else 0.0

        warnings = []
        if take_rate:
            warnings.append(
                f"Platform take rate over this window: {take_rate:.1f}% of revenue. "
                "Reconcile [fees.tiktok] in policy.toml against this — every profit "
                "figure and price floor is computed from those estimates."
            )
        return DataEnvelope(source="live", marketplace=self.name, fetched_at=_now(),
                            payload={"statements": rows,
                                     "total_revenue": round(total_revenue, 2),
                                     "total_fees": round(total_fees, 2),
                                     "take_rate_pct": round(take_rate, 2)},
                            warnings=warnings)

    def fetch_product_performance(self, *, days: int = 30) -> DataEnvelope:
        """Per-product traffic and conversion — the input to trend analysis."""
        c = self.client()
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        products = c.get_product_performance(start=start, end=end)

        rows = [{
            "product_id": p.get("id"),
            "title": p.get("title"),
            "gmv": _f((p.get("gmv") or {}).get("amount")),
            "units_sold": int(_f(p.get("units_sold"))),
            "orders": int(_f(p.get("orders"))),
            "page_views": int(_f(p.get("page_views"))),
            "unique_visitors": int(_f(p.get("unique_visitors"))),
            "click_through_rate": _f(p.get("click_through_rate")),
            "conversion_rate": _f(p.get("sku_orders_conversion_rate")),
        } for p in products]
        return DataEnvelope(source="live", marketplace=self.name, fetched_at=_now(),
                            payload=rows)

    def fetch_shop_performance(self, *, days: int = 30) -> DataEnvelope:
        c = self.client()
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        return DataEnvelope(
            source="live", marketplace=self.name, fetched_at=_now(),
            payload=c.get_shop_performance(start=start, end=end),
        )

    def fetch_competitor_offers(self, identifier: str) -> DataEnvelope:
        """TikTok Shop exposes no competitor data to sellers.

        Unlike Amazon's Product Pricing API, there is no endpoint that returns
        other sellers' listings or prices. Scraping the app or web storefront
        breaches TikTok's Terms of Service and puts the shop at risk — which is
        precisely the asset this system exists to protect.
        """
        raise ConnectorNotConfigured(
            "The TikTok Shop Partner API has no competitor-pricing endpoint — there "
            "is no equivalent of Amazon's Product Pricing API. Scraping the "
            "storefront breaches TikTok's Terms of Service and risks the shop that "
            "this system exists to protect.\n\n"
            "Legitimate alternatives: a licensed market-intelligence provider with "
            "its own TikTok data agreement, or manual spot checks recorded through "
            "the signals interface. Returning fabricated competitor prices here "
            "would feed the repricer directly and mis-price live listings."
        )

    def fetch_reviews(self, sku: str) -> DataEnvelope:
        raise ConnectorNotConfigured(
            "TikTok Shop's Partner API does not expose product reviews to sellers "
            "in a retrievable form. Export them from Seller Center and feed "
            "operator_core.reviews, or connect an authorised provider. An empty "
            "list here would report a badly-reviewed product as clean."
        )

    def fetch_ad_performance(self, *, since: str) -> DataEnvelope:
        raise ConnectorNotConfigured(
            "TikTok ad data comes from the TikTok Marketing API "
            "(business-api.tiktok.com), a separate application with its own "
            "credentials and advertiser_id. Shop API credentials cannot reach it."
        )

    # ------------------------------------------------------------------
    # compliance screen — runs before any write
    # ------------------------------------------------------------------
    @staticmethod
    def screen_for_policy(payload: dict) -> list[str]:
        """Check a product payload against TikTok's stricter content policy.

        TikTok enforces faster and more aggressively than the other
        marketplaces, and a violation can deactivate the whole shop rather than
        one listing. Screening before submission is cheaper than appealing after.
        """
        text = " ".join(str(payload.get(k, "")) for k in
                        ("title", "description", "brand")).lower()
        violations: list[str] = []

        for term in TIKTOK_PROHIBITED_TERMS:
            if term in text:
                violations.append(
                    f"Prohibited-category term '{term}' present. TikTok Shop's "
                    "restricted-products policy is stricter than other marketplaces "
                    "and enforcement can suspend the whole shop, not one listing."
                )
        for claim in TIKTOK_RESTRICTED_CLAIMS:
            if claim in text:
                violations.append(
                    f"Restricted claim '{claim}' present. Efficacy and health claims "
                    "draw enforcement on TikTok even where they would pass elsewhere."
                )
        return violations

    # ------------------------------------------------------------------
    # writes — implemented, and still gated
    # ------------------------------------------------------------------
    def publish_listing(self, listing: dict[str, Any]) -> DataEnvelope:
        self.require_write_permission("publish_listing")

        violations = self.screen_for_policy(listing)
        if violations:
            # Compliance is checked before the API call, not after a rejection.
            raise ValueError(
                "Refusing to publish: TikTok Shop policy screen failed.\n  - "
                + "\n  - ".join(violations)
            )

        result = self.client().create_product(listing)
        return DataEnvelope(
            source="live", marketplace=self.name, fetched_at=_now(),
            payload={"product_id": result.get("product_id"),
                     "skus": result.get("skus", []),
                     "warnings": result.get("warnings", [])},
            warnings=self._result_warnings(result),
        )

    def update_listing(self, product_id: str, listing: dict[str, Any]) -> DataEnvelope:
        self.require_write_permission("update_listing")

        violations = self.screen_for_policy(listing)
        if violations:
            raise ValueError(
                "Refusing to update: TikTok Shop policy screen failed.\n  - "
                + "\n  - ".join(violations)
            )

        result = self.client().update_product(product_id, listing)
        return DataEnvelope(
            source="live", marketplace=self.name, fetched_at=_now(),
            payload={"product_id": product_id, "skus": result.get("skus", [])},
            warnings=self._result_warnings(result) + [
                "A TikTok product update replaces the listing rather than merging. "
                "Any attribute omitted from this payload is cleared."
            ],
        )

    def update_price(self, sku: str, price: float, *,
                     product_id: str | None = None,
                     sku_id: str | None = None) -> DataEnvelope:
        self.require_write_permission("update_price")
        if not product_id or not sku_id:
            raise ValueError(
                "TikTok price updates need both product_id and sku_id — the seller "
                "SKU string alone does not identify the record."
            )
        # Amounts are strings on this endpoint; formatting is not cosmetic.
        result = self.client().update_price(product_id, [{
            "id": sku_id,
            "price": {"amount": f"{price:.2f}", "currency": self.currency},
        }])
        return DataEnvelope(
            source="live", marketplace=self.name, fetched_at=_now(),
            payload={"product_id": product_id, "sku_id": sku_id, "price": price,
                     "currency": self.currency},
            warnings=self._result_warnings(result),
        )

    def update_inventory(self, product_id: str, sku_id: str, quantity: int,
                         *, warehouse_id: str | None = None) -> DataEnvelope:
        self.require_write_permission("update_inventory")
        entry: dict[str, Any] = {"quantity": int(quantity)}
        if warehouse_id:
            entry["warehouse_id"] = warehouse_id
        result = self.client().update_inventory(product_id, [
            {"id": sku_id, "inventory": [entry]}
        ])
        warnings = self._result_warnings(result)
        if not warehouse_id:
            warnings.append(
                "No warehouse_id supplied — this updated the default warehouse. "
                "If the shop has more than one, the stock may not be where you "
                "think it is."
            )
        return DataEnvelope(
            source="live", marketplace=self.name, fetched_at=_now(),
            payload={"product_id": product_id, "sku_id": sku_id, "quantity": quantity},
            warnings=warnings,
        )

    def update_ad_budget(self, campaign_id: str, budget: float) -> DataEnvelope:
        self.require_write_permission("update_ad_budget")
        raise ConnectorNotConfigured(
            "Ad budgets live in the TikTok Marketing API, a separate application "
            "from the Shop API. These credentials cannot reach it."
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _result_warnings(result: dict) -> list[str]:
        out = []
        for w in result.get("warnings", []) or []:
            message = w.get("message") if isinstance(w, dict) else str(w)
            out.append(f"TikTok warning: {message}")
        return out

    @staticmethod
    def _epoch_to_iso(value: Any) -> str:
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat(
                timespec="seconds")
        except (TypeError, ValueError, OSError):
            return ""

    @staticmethod
    def _parse_since(since: str) -> datetime:
        try:
            dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"Could not parse `since`={since!r}. Use ISO 8601, e.g. 2026-07-01."
            ) from exc
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def status(self) -> dict[str, Any]:
        base = super().status()
        base.update({
            "shop_id": self.shop_id or None,
            "region": self.region,
            "sandbox": self.sandbox,
            "currency": self.currency,
        })
        if self._transport is not None:
            base["transport"] = self._transport.stats()
        return base
