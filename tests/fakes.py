"""Offline test doubles for the SP-API transport.

These let the whole request path — auth, rate limiting, retry, pagination,
error translation, model mapping — run in tests without network access or a
real Amazon application. Given that a bug in this layer means mis-priced
listings on a live account, testing it against a scripted server is the only
responsible option available before credentials exist.
"""

from __future__ import annotations

import json
from typing import Any

from connectors.amazon.transport import Response


class FakeClock:
    """Virtual time: sleeping advances the clock instead of blocking.

    Both `sleep` and `monotonic` come from the same source, so the rate limiter
    sees the time it was told to wait actually pass. Faking only `sleep` leaves
    the bucket spinning against a real clock.
    """

    def __init__(self) -> None:
        self.slept: list[float] = []
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now

    @property
    def total(self) -> float:
        return sum(self.slept)


class FakeTokenProvider:
    def __init__(self, token: str = "Atza|fake-access-token") -> None:
        self.token = token
        self.calls = 0
        self.invalidations = 0

    def access_token(self, *, scope: str | None = None, force_refresh: bool = False) -> str:
        self.calls += 1
        return self.token

    def invalidate(self, *, scope: str | None = None) -> None:
        self.invalidations += 1


class ScriptedSender:
    """Replays a queued list of responses and records what was requested.

    Each script entry is (status, body, headers). A callable entry is invoked
    with the request kwargs so a test can assert on, or vary by, the request.
    """

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, *, method: str, url: str, headers: dict[str, str],
                 body: dict | None, timeout: float) -> Response:
        self.requests.append({
            "method": method, "url": url, "headers": headers, "body": body,
        })
        if not self.script:
            raise AssertionError(
                f"ScriptedSender exhausted; unexpected {method} {url}"
            )
        entry = self.script.pop(0)
        if callable(entry):
            entry = entry(method=method, url=url, headers=headers, body=body)
        status, payload, resp_headers = entry
        return Response(status=status, headers=resp_headers or {}, body=payload)

    # -- assertions helpers ------------------------------------------------
    @property
    def last(self) -> dict[str, Any]:
        return self.requests[-1]

    def urls(self) -> list[str]:
        return [r["url"] for r in self.requests]


def ok(payload: dict, headers: dict | None = None) -> tuple:
    return (200, payload, headers or {})


def err(status: int, code: str, message: str, headers: dict | None = None) -> tuple:
    return (status, {"errors": [{"code": code, "message": message}]}, headers or {})


# ---------------------------------------------------------------------------
# Realistic response fixtures, shaped as Amazon actually returns them.
# ---------------------------------------------------------------------------
PARTICIPATIONS = {
    "payload": [
        {
            "marketplace": {"id": "ATVPDKIKX0DER", "name": "Amazon.com",
                            "countryCode": "US", "defaultCurrencyCode": "USD"},
            "participation": {"isParticipating": True, "hasSuspendedListings": False},
        }
    ]
}

CATALOG_SEARCH = {
    "numberOfResults": 2,
    "pagination": {},
    "items": [
        {
            "asin": "B08EXAMPLE1",
            "summaries": [{
                "marketplaceId": "ATVPDKIKX0DER",
                "itemName": "Bamboo Drawer Organizer, Expandable",
                "brand": "HomeNeat", "manufacturer": "HomeNeat",
                "productType": "HOME_ORGANIZER",
                "browseClassification": {"displayName": "Home & Kitchen"},
            }],
            "salesRanks": [{"classificationRanks": [{"rank": 4210}]}],
            "images": [{"images": [{"link": "https://m.media-amazon.com/x.jpg",
                                    "height": 500, "width": 500}]}],
            "dimensions": [{"package": {"weight": {"value": 2.4, "unit": "pounds"}}}],
        },
        {
            "asin": "B08EXAMPLE2",
            "summaries": [{"itemName": "Kitchen Drawer Divider Set",
                           "brand": "TidyCo", "productType": "HOME_ORGANIZER"}],
            "salesRanks": [{"displayGroupRanks": [{"rank": 18900}]}],
            "images": [],
        },
    ],
}

ITEM_OFFERS = {
    "payload": {
        "ASIN": "B08EXAMPLE1",
        "status": "Success",
        "Summary": {
            "TotalOfferCount": 4,
            "BuyBoxPrices": [{"LandedPrice": {"Amount": 32.99, "CurrencyCode": "USD"}}],
            "SalesRankings": [{"ProductCategoryId": "home_garden_display_on_website",
                               "Rank": 4210}],
        },
        "Offers": [
            {
                "SellerId": "A1RIVAL", "SubCondition": "new",
                "ListingPrice": {"Amount": 32.99, "CurrencyCode": "USD"},
                "Shipping": {"Amount": 0.0, "CurrencyCode": "USD"},
                "IsBuyBoxWinner": True, "IsFulfilledByAmazon": True,
                "PrimeInformation": {"IsPrime": True},
                "SellerFeedbackRating": {"SellerPositiveFeedbackRating": 96.0,
                                         "FeedbackCount": 1400},
            },
            {
                "SellerId": "A2CHEAP", "SubCondition": "new",
                "ListingPrice": {"Amount": 19.99, "CurrencyCode": "USD"},
                "Shipping": {"Amount": 4.99, "CurrencyCode": "USD"},
                "IsBuyBoxWinner": False, "IsFulfilledByAmazon": False,
                "SellerFeedbackRating": {"SellerPositiveFeedbackRating": 72.0,
                                         "FeedbackCount": 90},
            },
            {
                "MyOffer": True, "SubCondition": "new",
                "ListingPrice": {"Amount": 34.99, "CurrencyCode": "USD"},
                "Shipping": {"Amount": 0.0, "CurrencyCode": "USD"},
                "IsBuyBoxWinner": False, "IsFulfilledByAmazon": True,
            },
        ],
    }
}

FEES_ESTIMATE = {
    "payload": {
        "FeesEstimateResult": {
            "Status": "Success",
            "FeesEstimateIdentifier": {"IdType": "ASIN", "IdValue": "B08EXAMPLE1"},
            "FeesEstimate": {
                "TotalFeesEstimate": {"Amount": 10.22, "CurrencyCode": "USD"},
                "FeeDetailList": [
                    {"FeeType": "ReferralFee",
                     "FinalFee": {"Amount": 5.25, "CurrencyCode": "USD"}},
                    {"FeeType": "FBAFees",
                     "FinalFee": {"Amount": 4.97, "CurrencyCode": "USD"}},
                ],
            },
        }
    }
}

INVENTORY_SUMMARIES = {
    "payload": {
        "inventorySummaries": [
            {
                "sellerSku": "BAMBOO-ORG-01", "asin": "B08EXAMPLE1",
                "fnSku": "X001ABC", "condition": "NewItem",
                "productName": "Bamboo Drawer Organizer",
                "totalQuantity": 265,
                "inventoryDetails": {
                    "fulfillableQuantity": 240,
                    "inboundWorkingQuantity": 0,
                    "inboundShippedQuantity": 100,
                    "inboundReceivingQuantity": 20,
                    "reservedQuantity": {"totalReservedQuantity": 12},
                    "unfulfillableQuantity": {"totalUnfulfillableQuantity": 13},
                    "researchingQuantity": {"totalResearchingQuantity": 0},
                },
            }
        ]
    },
    "pagination": {},
}

ORDERS_PAGE_1 = {
    "payload": {
        "Orders": [
            {"AmazonOrderId": "111-0000001-0000001", "PurchaseDate": "2026-07-27T10:00:00Z",
             "OrderStatus": "Shipped", "FulfillmentChannel": "AFN",
             "NumberOfItemsShipped": 1, "NumberOfItemsUnshipped": 0,
             "OrderTotal": {"CurrencyCode": "USD", "Amount": "34.99"}},
            {"AmazonOrderId": "111-0000002-0000002", "PurchaseDate": "2026-07-27T11:00:00Z",
             "OrderStatus": "Pending", "FulfillmentChannel": "AFN",
             "NumberOfItemsShipped": 0, "NumberOfItemsUnshipped": 1},
        ],
        "NextToken": "PAGE2TOKEN",
    }
}

ORDERS_PAGE_2 = {
    "payload": {
        "Orders": [
            {"AmazonOrderId": "111-0000003-0000003", "PurchaseDate": "2026-07-27T12:00:00Z",
             "OrderStatus": "Shipped", "FulfillmentChannel": "MFN",
             "NumberOfItemsShipped": 2, "NumberOfItemsUnshipped": 0,
             "OrderTotal": {"CurrencyCode": "USD", "Amount": "49.98"}},
        ]
    }
}

LISTING_ITEM = {
    "sku": "BAMBOO-ORG-01",
    "summaries": [{"marketplaceId": "ATVPDKIKX0DER", "productType": "HOME_ORGANIZER",
                   "itemName": "Bamboo Drawer Organizer"}],
    "issues": [],
}

PATCH_ACCEPTED = {
    "sku": "BAMBOO-ORG-01",
    "status": "ACCEPTED",
    "submissionId": "sub-12345",
    "issues": [],
}


# ---------------------------------------------------------------------------
# TikTok Shop fixtures.
#
# Note the shape: TikTok wraps everything in {code, message, data, request_id}
# and returns HTTP 200 even for failures. The fixtures mirror that exactly,
# because a client that only checks HTTP status passes tests built on a
# friendlier shape and then silently swallows every real error.
# ---------------------------------------------------------------------------
from connectors.tiktok.transport import Response as TTResponse


class TikTokSender:
    """Replays queued TikTok responses and records the requests made."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, *, method: str, url: str, headers: dict[str, str],
                 body: str | None, timeout: float) -> TTResponse:
        self.requests.append({
            "method": method, "url": url, "headers": headers, "body": body,
        })
        if not self.script:
            raise AssertionError(f"TikTokSender exhausted; unexpected {method} {url}")
        entry = self.script.pop(0)
        if callable(entry):
            entry = entry(method=method, url=url, headers=headers, body=body)
        status, payload, resp_headers = entry
        return TTResponse(status=status, headers=resp_headers or {}, body=payload)

    @property
    def last(self) -> dict[str, Any]:
        return self.requests[-1]

    def urls(self) -> list[str]:
        return [r["url"] for r in self.requests]


class FakeTikTokTokens:
    def __init__(self, token: str = "tt-access-token") -> None:
        self.token = token
        self.calls = 0
        self.invalidations = 0
        self.warnings: list[str] = []

    def access_token(self, *, force_refresh: bool = False) -> str:
        self.calls += 1
        return self.token

    def invalidate(self) -> None:
        self.invalidations += 1


def tt_ok(data: dict, headers: dict | None = None) -> tuple:
    """A successful TikTok response: HTTP 200, code 0."""
    return (200, {"code": 0, "message": "Success", "data": data,
                  "request_id": "req-test"}, headers or {})


def tt_err(code: int, message: str, *, status: int = 200,
           headers: dict | None = None) -> tuple:
    """A TikTok failure. Note status defaults to 200 — that is the real trap."""
    return (status, {"code": code, "message": message, "data": {},
                     "request_id": "req-test"}, headers or {})


TT_SHOPS = {
    "shops": [
        {"id": "7000000000000000001", "name": "Test Shop US",
         "cipher": "ROW_CIPHER_ABC", "region": "US", "seller_type": "LOCAL"},
        {"id": "7000000000000000002", "name": "Other Shop",
         "cipher": "ROW_CIPHER_XYZ", "region": "GB", "seller_type": "LOCAL"},
    ]
}

TT_PRODUCTS = {
    "products": [
        {
            "id": "170000000001", "title": "Bamboo Drawer Organizer",
            "status": "ACTIVATE", "create_time": 1750000000, "update_time": 1753000000,
            "category_chains": [{"id": "601152", "local_name": "Home Storage"}],
            "skus": [
                {"id": "SKU-1", "seller_sku": "BAMBOO-ORG-01",
                 "price": {"sale_price": "34.99", "currency": "USD"},
                 "inventory": [{"warehouse_id": "WH1", "quantity": 240}]},
            ],
        },
        {
            "id": "170000000002", "title": "Pet Slicker Brush",
            "status": "ACTIVATE", "create_time": 1750000000, "update_time": 1753000000,
            "category_chains": [{"id": "601500", "local_name": "Pet Supplies"}],
            "skus": [
                {"id": "SKU-2", "seller_sku": "PETBRUSH-03",
                 "price": {"sale_price": "24.99", "currency": "USD"},
                 "inventory": [{"warehouse_id": "WH1", "quantity": 0},
                               {"warehouse_id": "WH2", "quantity": 12}]},
            ],
        },
    ],
    "next_page_token": "",
}

TT_ORDERS = {
    "orders": [
        {
            "id": "5770000000000001", "status": "COMPLETED", "create_time": 1753600000,
            "payment": {"total_amount": "34.99", "sub_total": "34.99",
                        "shipping_fee": "0.00", "seller_discount": "0.00",
                        "platform_discount": "0.00", "tax": "2.80", "currency": "USD"},
            "line_items": [
                {"sku_id": "SKU-1", "seller_sku": "BAMBOO-ORG-01",
                 "product_id": "170000000001", "product_name": "Bamboo Drawer Organizer",
                 "sale_price": "34.99", "platform_discount": "0.00",
                 "seller_discount": "0.00"},
            ],
        },
        {
            "id": "5770000000000002", "status": "UNPAID", "create_time": 1753610000,
            "payment": {"total_amount": "24.99", "sub_total": "24.99",
                        "currency": "USD"},
            "line_items": [],
        },
    ],
    "next_page_token": "",
}

TT_STATEMENTS = {
    "statements": [
        {"id": "STMT-1", "statement_time": 1753000000, "settlement_amount": "812.40",
         "revenue_amount": "1049.70", "fee_amount": "-237.30",
         "adjustment_amount": "0.00", "currency": "USD", "status": "PAID"},
    ],
    "next_page_token": "",
}

TT_PRODUCT_PERFORMANCE = {
    "products": [
        {"id": "170000000001", "title": "Bamboo Drawer Organizer",
         "gmv": {"amount": "3499.00", "currency": "USD"},
         "units_sold": 100, "orders": 96, "page_views": 8200,
         "unique_visitors": 6100, "click_through_rate": 0.031,
         "sku_orders_conversion_rate": 0.0117},
    ],
    "next_page_token": "",
}

TT_CREATE_OK = {
    "product_id": "170000000003",
    "skus": [{"id": "SKU-9", "seller_sku": "NEW-1"}],
    "warnings": [{"message": "Image resolution below recommended 800x800"}],
}


# ---------------------------------------------------------------------------
# Shopify Admin API fixtures.
#
# Shape note: Shopify returns HTTP 200 for both a rejected query (`errors`) and
# a rejected mutation (`data.<field>.userErrors`). The helpers below produce all
# three failure layers separately, because a test suite that only ever builds
# the happy shape will pass against a client that checks none of them.
# ---------------------------------------------------------------------------
from connectors.shopify.transport import Response as ShopResponse


class ShopifySender:
    """Replays queued Shopify responses and records the GraphQL sent."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, *, url: str, headers: dict[str, str], body: str,
                 timeout: float) -> ShopResponse:
        parsed = json.loads(body) if body else {}
        self.requests.append({
            "url": url, "headers": headers, "body": body,
            "query": parsed.get("query", ""),
            "variables": parsed.get("variables", {}),
        })
        if not self.script:
            raise AssertionError(
                f"ShopifySender exhausted; unexpected call #{len(self.requests)} "
                f"with variables {parsed.get('variables')}")
        entry = self.script.pop(0)
        if callable(entry):
            entry = entry(url=url, headers=headers, body=body)
        status, payload, resp_headers = entry
        return ShopResponse(status=status, headers=resp_headers or {}, body=payload)

    @property
    def last(self) -> dict[str, Any]:
        return self.requests[-1]

    def operations(self) -> list[str]:
        """First line of each query — enough to assert on call ordering."""
        return [r["query"].strip().splitlines()[0].strip() if r["query"] else ""
                for r in self.requests]


def _throttle(available: float = 900.0, maximum: float = 1000.0,
              restore: float = 100.0, cost: float = 10.0) -> dict:
    return {
        "cost": {
            "requestedQueryCost": cost,
            "actualQueryCost": cost,
            "throttleStatus": {
                "maximumAvailable": maximum,
                "currentlyAvailable": available,
                "restoreRate": restore,
            },
        }
    }


def sh_ok(data: dict, *, extensions: dict | None = None,
          headers: dict | None = None) -> tuple:
    """A successful GraphQL response."""
    return (200, {"data": data, "extensions": extensions or _throttle()},
            headers or {})


def sh_error(message: str, *, code: str = "", status: int = 200,
             available: float = 900.0) -> tuple:
    """Layer 2: query rejected. HTTP 200 unless told otherwise."""
    error: dict[str, Any] = {"message": message}
    if code:
        error["extensions"] = {"code": code}
    body: dict[str, Any] = {"errors": [error]}
    if code == "THROTTLED":
        body["extensions"] = _throttle(available=available)
    return (status, body, {})


def sh_user_error(field_name: str, errors: list[dict],
                  payload: dict | None = None) -> tuple:
    """Layer 3: mutation ran, business rejected it. HTTP 200, data populated."""
    block = dict(payload or {})
    block["userErrors"] = errors
    return (200, {"data": {field_name: block}, "extensions": _throttle()}, {})


def sh_http(status: int, message: str = "") -> tuple:
    """Layer 1: HTTP-level failure."""
    return (status, {"errors": message or f"HTTP {status}"}, {})


SH_SHOP = {
    "shop": {
        "id": "gid://shopify/Shop/1",
        "name": "Test Store",
        "myshopifyDomain": "test-store.myshopify.com",
        "primaryDomain": {"url": "https://test-store.com"},
        "currencyCode": "USD",
        "ianaTimezone": "America/New_York",
        "plan": {"displayName": "Basic", "partnerDevelopment": False,
                 "shopifyPlus": False},
        "billingAddress": {"countryCodeV2": "US"},
    }
}

SH_SCOPES = {
    "currentAppInstallation": {
        "accessScopes": [
            {"handle": "read_products"}, {"handle": "write_products"},
            {"handle": "read_inventory"}, {"handle": "write_inventory"},
            {"handle": "read_orders"}, {"handle": "read_customers"},
            {"handle": "read_price_rules"}, {"handle": "write_price_rules"},
            {"handle": "read_publications"}, {"handle": "write_publications"},
            {"handle": "read_locations"},
        ]
    }
}

SH_LOCATIONS = {
    "locations": {
        "nodes": [
            {"id": "gid://shopify/Location/1", "name": "Main Warehouse",
             "isActive": True, "fulfillsOnlineOrders": True,
             "address": {"countryCode": "US", "provinceCode": "NY", "city": "NYC"}},
        ],
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }
}

SH_PRODUCTS = {
    "products": {
        "nodes": [
            {
                "id": "gid://shopify/Product/1",
                "title": "Bamboo Drawer Organizer",
                "handle": "bamboo-drawer-organizer",
                "status": "ACTIVE",
                "vendor": "HomeNeat",
                "productType": "Home Storage",
                "tags": ["storage", "kitchen"],
                "totalInventory": 240,
                "onlineStoreUrl": "https://test-store.com/products/bamboo-drawer-organizer",
                "createdAt": "2026-06-01T00:00:00Z",
                "updatedAt": "2026-07-20T00:00:00Z",
                "publishedAt": "2026-06-02T00:00:00Z",
                "seo": {"title": "Bamboo Drawer Organizer", "description": "Expandable."},
                "featuredMedia": {"id": "gid://shopify/MediaImage/1", "alt": "organizer"},
                "variants": {"nodes": [
                    {"id": "gid://shopify/ProductVariant/11", "title": "Default",
                     "sku": "BAMBOO-ORG-01", "price": "34.99", "compareAtPrice": None,
                     "barcode": None, "inventoryQuantity": 240,
                     "inventoryItem": {"id": "gid://shopify/InventoryItem/21",
                                       "tracked": True,
                                       "unitCost": {"amount": "9.40", "currencyCode": "USD"},
                                       "measurement": {"weight": {"value": 2.4, "unit": "POUNDS"}}}},
                ]},
            },
            {
                "id": "gid://shopify/Product/2",
                "title": "Pet Slicker Brush",
                "handle": "pet-slicker-brush",
                "status": "DRAFT",
                "vendor": "PetCo",
                "productType": "Pet Supplies",
                "tags": [],
                "totalInventory": 12,
                "onlineStoreUrl": None,
                "createdAt": "2026-07-01T00:00:00Z",
                "updatedAt": "2026-07-21T00:00:00Z",
                "publishedAt": None,
                "seo": {"title": None, "description": None},
                "featuredMedia": None,
                "variants": {"nodes": [
                    {"id": "gid://shopify/ProductVariant/12", "title": "Default",
                     "sku": "PETBRUSH-03", "price": "24.99", "compareAtPrice": None,
                     "barcode": None, "inventoryQuantity": 12,
                     "inventoryItem": {"id": "gid://shopify/InventoryItem/22",
                                       "tracked": True, "unitCost": None,
                                       "measurement": None}},
                ]},
            },
        ],
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }
}


def sh_order(order_id: str, *, total: str = "34.99", journey: Any = None,
             created_at: str = "2026-07-27T10:00:00Z", order_count: int = 1,
             refunded: str = "", tax: str = "2.80", shipping: str = "0.00",
             sku: str = "BAMBOO-ORG-01", quantity: int = 1) -> dict:
    """One order node, with the journey shape Shopify actually returns."""
    return {
        "id": f"gid://shopify/Order/{order_id}",
        "name": f"#{order_id}",
        "createdAt": created_at,
        "processedAt": created_at,
        "displayFinancialStatus": "PAID",
        "displayFulfillmentStatus": "FULFILLED",
        "cancelledAt": None,
        "currentTotalPriceSet": {"shopMoney": {"amount": total, "currencyCode": "USD"}},
        "currentSubtotalPriceSet": {"shopMoney": {"amount": total}},
        "totalDiscountsSet": {"shopMoney": {"amount": "0.00"}},
        "totalShippingPriceSet": {"shopMoney": {"amount": shipping}},
        "totalTaxSet": {"shopMoney": {"amount": tax}},
        "refunds": ([{"totalRefundedSet": {"shopMoney": {"amount": refunded}}}]
                    if refunded else []),
        "customer": {"id": f"gid://shopify/Customer/{order_id}",
                     "numberOfOrders": order_count},
        "customerJourneySummary": journey,
        "lineItems": {"nodes": [{
            "id": "gid://shopify/LineItem/1", "quantity": quantity, "sku": sku,
            "title": "Bamboo Drawer Organizer",
            "originalTotalSet": {"shopMoney": {"amount": total}},
            "discountedTotalSet": {"shopMoney": {"amount": total}},
            "variant": {"id": "gid://shopify/ProductVariant/11",
                        "inventoryItem": {"unitCost": {"amount": "9.40"}}},
        }]},
    }


def sh_journey(source: str = "tiktok", *, source_type: str = "social",
               referrer: str = "https://www.tiktok.com/", landing: str = "/",
               utm: dict | None = None) -> dict:
    """A customer journey summary. Pass journey=None for an unattributed order."""
    return {
        "momentsCount": {"count": 2},
        "firstVisit": {"source": source, "sourceType": source_type,
                       "referrerUrl": referrer, "landingPage": landing,
                       "utmParameters": utm or {}},
        "lastVisit": {"source": source, "sourceType": source_type,
                      "referrerUrl": referrer, "landingPage": landing,
                      "utmParameters": utm or {}},
    }


# ---------------------------------------------------------------------------
# eBay fixtures.
#
# Shape note: unlike TikTok and Shopify, eBay uses honest HTTP status codes —
# a 2xx means it worked. Errors carry a stable numeric `errorId`, which is what
# code should branch on; the message text gets reworded between releases.
# ---------------------------------------------------------------------------
from connectors.ebay.transport import Response as EbayResponse


class EbaySender:
    """Replays queued eBay responses and records the requests made."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, *, method: str, url: str, headers: dict[str, str],
                 body: str | None, timeout: float) -> EbayResponse:
        self.requests.append({
            "method": method, "url": url, "headers": headers,
            "body": json.loads(body) if body else None,
            "marketplace": headers.get("X-EBAY-C-MARKETPLACE-ID", ""),
            "authorization": headers.get("Authorization", ""),
        })
        if not self.script:
            raise AssertionError(f"EbaySender exhausted; unexpected {method} {url}")
        entry = self.script.pop(0)
        if callable(entry):
            entry = entry(method=method, url=url, headers=headers, body=body)
        status, payload, resp_headers = entry
        return EbayResponse(status=status, headers=resp_headers or {}, body=payload)

    @property
    def last(self) -> dict[str, Any]:
        return self.requests[-1]

    def urls(self) -> list[str]:
        return [r["url"] for r in self.requests]


class FakeEbayTokens:
    """Records which token kind each call asked for."""

    def __init__(self) -> None:
        self.requested: list[str] = []
        self.invalidations: list[str] = []
        self.refresh_count = 0

    def access_token(self, kind: str = "user", *, force_refresh: bool = False) -> str:
        self.requested.append(kind)
        return f"token-{kind}"

    def invalidate(self, kind: str | None = None) -> None:
        self.invalidations.append(kind or "all")

    def grant_age_warning(self, **_kw) -> str:
        return ""


def eb_ok(payload: dict, *, status: int = 200, headers: dict | None = None) -> tuple:
    return (status, payload, headers or {})


def eb_error(status: int, error_id: int, message: str, *,
             long_message: str = "", parameters: list | None = None) -> tuple:
    """An eBay failure. Status is honest; errorId is the stable identifier."""
    error: dict[str, Any] = {"errorId": error_id, "domain": "API_FULFILLMENT",
                             "category": "REQUEST", "message": message}
    if long_message:
        error["longMessage"] = long_message
    if parameters:
        error["parameters"] = parameters
    return (status, {"errors": [error]}, {})


def eb_token(access_token: str = "v^1.1#i^1#access", expires_in: int = 7200) -> tuple:
    return (200, {"access_token": access_token, "expires_in": expires_in,
                  "token_type": "User Access Token"})


def eb_token_error(error: str, description: str = "", *, status: int = 400) -> tuple:
    return (status, {"error": error, "error_description": description})


EB_STANDARDS = {
    "standardsLevel": "TOP_RATED",
    "program": "PROGRAM_US",
    "cycle": {"cycleType": "CURRENT"},
    "username": "test_seller",
    "metrics": [
        {"metricKey": "DEFECTIVE_TRANSACTION_RATE",
         "value": {"value": "0.4"}, "lookbackStartDate": "2026-01-01"},
        {"metricKey": "SHIPPING_MISS_RATE", "value": {"value": "1.2"}},
        {"metricKey": "CASES_NOT_RESOLVED_RATE", "value": {"value": "0.0"}},
    ],
}


def eb_order(order_id: str = "12-34567-89012", *, total: str = "24.99",
             payment: str = "PAID", cancelled: bool = False,
             created: str = "2026-07-20T10:00:00.000Z",
             sku: str = "SKU-1", quantity: int = 1,
             buyer: str = "buyer_one") -> dict:
    return {
        "orderId": order_id,
        "creationDate": created,
        "orderPaymentStatus": payment,
        "orderFulfillmentStatus": "FULFILLED",
        "cancelStatus": {"cancelState": "CANCELED" if cancelled else "NONE_REQUESTED"},
        "pricingSummary": {"total": {"value": total, "currency": "USD"}},
        "buyer": {"username": buyer},
        "lineItems": [{
            "lineItemId": "1",
            "sku": sku,
            "title": "Self cleaning slicker brush",
            "quantity": quantity,
            "total": {"value": total, "currency": "USD"},
        }],
    }


def eb_browse_item(item_id: str, price: str, *, seller: str = "rival_seller",
                   shipping: str = "0.00", feedback_pct: str = "99.2",
                   feedback_score: int = 4210, condition: str = "New",
                   top_rated: bool = False) -> dict:
    return {
        "itemId": item_id,
        "title": "Self Cleaning Slicker Brush for Dogs",
        "price": {"value": price, "currency": "USD"},
        "condition": condition,
        "topRatedBuyingExperience": top_rated,
        "seller": {"username": seller, "feedbackPercentage": feedback_pct,
                   "feedbackScore": feedback_score},
        "shippingOptions": [{"shippingCost": {"value": shipping,
                                              "currency": "USD"}}],
    }
