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
