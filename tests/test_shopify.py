"""Shopify connector tests.

Weighted towards refusal. The valuable assertions here are not that a product
can be created — they are that an unapproved publish is blocked, that a
`userErrors` rejection is not reported as a success, that a truncated page is
not treated as a whole catalogue, and that an order Shopify could not attribute
is never quietly credited to TikTok.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from connectors.base import ConnectorNotConfigured, WriteNotPermitted
from connectors.credentials import CredentialsUnavailable
from connectors.shopify import (
    CostLimiter,
    ShopifyAPIError,
    ShopifyClient,
    ShopifyConnector,
    ShopifyCredentials,
    ShopifyThrottled,
    ShopifyUserError,
    StaticCredentials,
    ThrottleStatus,
    Transport,
    UnavailableCredentials,
    classify_traffic_source,
    handleize,
    normalise_domain,
)
from tests.fakes import (
    SH_LOCATIONS,
    SH_PRODUCTS,
    SH_SCOPES,
    SH_SHOP,
    FakeClock,
    ShopifySender,
    sh_error,
    sh_http,
    sh_journey,
    sh_ok,
    sh_order,
    sh_user_error,
)

LIVE_ENV = {
    "SHOPIFY_SHOP_DOMAIN": "test-store.myshopify.com",
    "SHOPIFY_ADMIN_ACCESS_TOKEN": "shpat_testtoken",
}
CREDS = ShopifyCredentials("test-store.myshopify.com", "shpat_testtoken", "2025-07")


def build_transport(script: list) -> tuple[Transport, ShopifySender, FakeClock]:
    sender = ShopifySender(script)
    clock = FakeClock()
    transport = Transport(
        shop_domain="test-store.myshopify.com", access_token="shpat_testtoken",
        api_version="2025-07", send=sender, sleep=clock.sleep,
        monotonic=clock.monotonic)
    return transport, sender, clock


def build_client(script: list) -> tuple[ShopifyClient, ShopifySender, FakeClock]:
    transport, sender, clock = build_transport(script)
    return ShopifyClient(transport), sender, clock


def build_connector(script: list, *, allow_writes: bool = False):
    transport, sender, clock = build_transport(script)
    conn = ShopifyConnector(allow_writes=allow_writes, transport=transport,
                            credentials=StaticCredentials(CREDS))
    return conn, sender, clock


class _Authorisation:
    """Stands in for risk.AuthorisationResult."""

    def __init__(self, permitted: bool, reason: str = "") -> None:
        self.permitted = permitted
        self.reason = reason

    def explain(self) -> str:
        return self.reason


# ---------------------------------------------------------------------------
class TestDomainNormalisation(unittest.TestCase):
    def test_bare_handle_gets_myshopify_suffix(self):
        self.assertEqual(normalise_domain("my-store"), "my-store.myshopify.com")

    def test_pasted_admin_url_is_reduced(self):
        # This is how the value actually arrives — copied from the address bar.
        self.assertEqual(
            normalise_domain("https://my-store.myshopify.com/admin/products"),
            "my-store.myshopify.com")

    def test_case_is_normalised(self):
        self.assertEqual(normalise_domain("My-Store.MyShopify.com"),
                         "my-store.myshopify.com")

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            normalise_domain("   ")


class TestHandleize(unittest.TestCase):
    def test_matches_shopify_rules(self):
        self.assertEqual(handleize("Bamboo Drawer Organizer, Expandable!"),
                         "bamboo-drawer-organizer-expandable")

    def test_never_empty(self):
        self.assertEqual(handleize("!!!"), "product")


# ---------------------------------------------------------------------------
class TestThreeFailureLayers(unittest.TestCase):
    """Each layer must be caught. Shopify returns HTTP 200 for two of them."""

    def test_http_failure_is_caught(self):
        client, _s, _c = build_client([sh_http(401, "Invalid API key")])
        with self.assertRaises(ShopifyAPIError) as ctx:
            client.shop()
        self.assertEqual(ctx.exception.layer, "http")
        self.assertIn("revoked", str(ctx.exception))

    def test_frozen_shop_is_explained_and_not_retried(self):
        # 402 means an unpaid bill; retrying is pointless and looks abusive.
        client, sender, _c = build_client([sh_http(402)])
        with self.assertRaises(ShopifyAPIError) as ctx:
            client.shop()
        self.assertIn("frozen", str(ctx.exception))
        self.assertEqual(len(sender.requests), 1)

    def test_top_level_errors_at_http_200(self):
        client, _s, _c = build_client([sh_error("Field 'foo' doesn't exist on type 'Shop'")])
        with self.assertRaises(ShopifyAPIError) as ctx:
            client.shop()
        self.assertEqual(ctx.exception.layer, "errors")
        self.assertIn("API version", str(ctx.exception))

    def test_user_errors_at_http_200_with_data_present(self):
        # The subtlest failure: status 200, no `errors`, `data` populated, and
        # the write did not happen.
        client, _s, _c = build_client([
            sh_user_error("productCreate",
                          [{"field": ["handle"], "message": "Handle has already been taken"}],
                          {"product": None}),
        ])
        with self.assertRaises(ShopifyUserError) as ctx:
            client.create_product({"title": "X"})
        self.assertEqual(ctx.exception.layer, "user_errors")
        self.assertIn("Handle has already been taken", str(ctx.exception))

    def test_user_errors_are_never_retried(self):
        client, sender, _c = build_client([
            sh_user_error("productCreate", [{"field": ["title"], "message": "can't be blank"}]),
        ])
        with self.assertRaises(ShopifyUserError):
            client.create_product({"title": ""})
        self.assertEqual(len(sender.requests), 1)

    def test_missing_mutation_payload_is_an_error_not_a_success(self):
        # If the mutation field name and the checked field diverge, the
        # userErrors check silently stops running. That must fail loudly.
        client, _s, _c = build_client([sh_ok({"somethingElse": {}})])
        with self.assertRaises(ShopifyAPIError) as ctx:
            client.create_product({"title": "X"})
        self.assertIn("userErrors", str(ctx.exception))

    def test_clean_success_returns_data(self):
        client, _s, _c = build_client([
            sh_ok({"productCreate": {"product": {"id": "gid://shopify/Product/9",
                                                 "handle": "x", "status": "DRAFT",
                                                 "title": "X",
                                                 "variants": {"nodes": []}},
                                     "userErrors": []}}),
        ])
        product = client.create_product({"title": "X"})
        self.assertEqual(product["status"], "DRAFT")


# ---------------------------------------------------------------------------
class TestCostLimiter(unittest.TestCase):
    def test_adopts_reported_limits_over_defaults(self):
        transport, _s, _c = build_transport([sh_ok(SH_SHOP)])
        ShopifyClient(transport).shop()
        stats = transport.stats()
        self.assertTrue(stats["limits_adopted_from_api"])
        self.assertEqual(stats["maximum_points"], 1000.0)
        self.assertEqual(stats["restore_per_second"], 100.0)

    def test_waits_when_points_are_short(self):
        clock = FakeClock()
        limiter = CostLimiter(maximum=100, restore_rate=10, monotonic=clock.monotonic)
        limiter.acquire(100, sleep=clock.sleep)      # drains the bucket
        waited = limiter.acquire(50, sleep=clock.sleep)
        self.assertAlmostEqual(waited, 5.0, places=3)

    def test_query_larger_than_bucket_does_not_hang(self):
        # Waiting for more points than can ever exist is an infinite loop; let
        # it through and surface Shopify's own error instead.
        clock = FakeClock()
        limiter = CostLimiter(maximum=100, restore_rate=10, monotonic=clock.monotonic)
        self.assertEqual(limiter.acquire(5000, sleep=clock.sleep), 0.0)

    def test_throttled_waits_the_arithmetic_not_a_doubling(self):
        # 300 points short at 100/sec is a 3 second wait, computed — not a
        # blind 2^n backoff that either stalls or hammers.
        transport, _sender, clock = build_transport([
            sh_error("Throttled", code="THROTTLED", available=0.0),
            sh_ok(SH_SHOP),
        ])
        ShopifyClient(transport).shop()
        self.assertTrue(any(0.01 <= s <= 30.0 for s in clock.slept))
        self.assertEqual(transport.throttle_count, 1)

    def test_gives_up_with_a_specific_message(self):
        transport, _s, _c = build_transport(
            [sh_error("Throttled", code="THROTTLED", available=0.0) for _ in range(5)])
        with self.assertRaises(ShopifyThrottled) as ctx:
            ShopifyClient(transport).shop()
        self.assertIn("shared cost bucket", str(ctx.exception))

    def test_seconds_until_handles_zero_restore_rate(self):
        status = ThrottleStatus(maximum_available=100, currently_available=0,
                                restore_rate=0)
        self.assertGreater(status.seconds_until(10), 0)


# ---------------------------------------------------------------------------
class TestPagination(unittest.TestCase):
    def test_all_pages_are_walked(self):
        page1 = {"products": {"nodes": [{"id": "1", "variants": {"nodes": []}}],
                              "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}}
        page2 = {"products": {"nodes": [{"id": "2", "variants": {"nodes": []}}],
                              "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        client, sender, _c = build_client([sh_ok(page1), sh_ok(page2)])
        products = client.products()
        self.assertEqual(len(products), 2)
        self.assertEqual(sender.requests[1]["variables"]["after"], "c1")

    def test_next_page_without_cursor_raises_rather_than_looping(self):
        broken = {"products": {"nodes": [{"id": "1"}],
                               "pageInfo": {"hasNextPage": True, "endCursor": None}}}
        client, _s, _c = build_client([sh_ok(broken)])
        with self.assertRaises(ShopifyAPIError) as ctx:
            client.products()
        self.assertIn("incomplete", str(ctx.exception))

    def test_orders_use_a_smaller_page_size(self):
        # Order nodes carry line items and a journey; paging them like products
        # trips the cost bucket.
        empty = {"orders": {"nodes": [], "pageInfo": {"hasNextPage": False}}}
        client, sender, _c = build_client([sh_ok(empty)])
        client.orders()
        self.assertEqual(sender.last["variables"]["first"], 25)


# ---------------------------------------------------------------------------
class TestAttribution(unittest.TestCase):
    """The channel this whole business runs on. Guessing here is not free."""

    def test_tiktok_referrer_is_detected(self):
        attr = classify_traffic_source(sh_journey("tiktok"))
        self.assertTrue(attr.is_tiktok)
        self.assertTrue(attr.confident)

    def test_tiktok_detected_from_utm_when_source_is_generic(self):
        # In-app browser traffic often arrives with an unhelpful source and only
        # the utm tag identifies the channel.
        journey = sh_journey("unknown", source_type="unknown", referrer="",
                             utm={"source": "tiktok", "medium": "organic",
                                  "campaign": "aug-launch"})
        attr = classify_traffic_source(journey)
        self.assertTrue(attr.is_tiktok)
        self.assertEqual(attr.campaign, "aug-launch")

    def test_ttclid_is_detected(self):
        journey = sh_journey("unknown", source_type="unknown",
                             referrer="https://shop.com/?ttclid=abc123")
        self.assertTrue(classify_traffic_source(journey).is_tiktok)

    def test_missing_journey_is_unattributed_never_direct(self):
        attr = classify_traffic_source(None)
        self.assertEqual(attr.channel, "unattributed")
        self.assertFalse(attr.confident)

    def test_empty_source_is_unattributed_never_tiktok(self):
        journey = sh_journey("", source_type="", referrer="", landing="")
        attr = classify_traffic_source(journey)
        self.assertEqual(attr.channel, "unattributed")
        self.assertFalse(attr.confident)

    def test_other_social_is_not_counted_as_tiktok(self):
        attr = classify_traffic_source(
            sh_journey("instagram", referrer="https://instagram.com/"))
        self.assertEqual(attr.channel, "social")
        self.assertFalse(attr.is_tiktok)

    def test_search_and_direct_are_distinguished(self):
        self.assertEqual(
            classify_traffic_source(sh_journey("google", source_type="search",
                                               referrer="https://google.com/")).channel,
            "search")
        self.assertEqual(
            classify_traffic_source(sh_journey("direct", source_type="direct",
                                               referrer="")).channel,
            "direct")

    def test_unattributed_share_is_surfaced_as_a_warning(self):
        orders = {"orders": {
            "nodes": [sh_order("1", journey=sh_journey("tiktok")),
                      sh_order("2", journey=None)],
            "pageInfo": {"hasNextPage": False}}}
        conn, _s, _c = build_connector([sh_ok(orders)])
        env = conn.fetch_orders(since="2026-07-01")
        self.assertTrue(any("no resolvable traffic source" in w for w in env.warnings))
        self.assertTrue(any("understates" in w for w in env.warnings))


class TestOrderSummary(unittest.TestCase):
    def _one(self, **kwargs):
        orders = {"orders": {"nodes": [sh_order("1", **kwargs)],
                             "pageInfo": {"hasNextPage": False}}}
        conn, _s, _c = build_connector([sh_ok(orders)])
        return conn.fetch_orders(since="2026-07-01").payload[0]

    def test_net_revenue_excludes_tax_shipping_and_refunds(self):
        order = self._one(total="40.00", tax="3.00", shipping="5.00", refunded="10.00")
        # 40 - 10 refunded - 3 tax - 5 shipping
        self.assertEqual(order.net_revenue, 22.0)

    def test_repeat_customer_is_flagged(self):
        self.assertTrue(self._one(order_count=3).is_repeat)
        self.assertFalse(self._one(order_count=1).is_repeat)


# ---------------------------------------------------------------------------
class TestDraftFirstPublishing(unittest.TestCase):
    """Creating is reversible, publishing is not. Only the second is gated."""

    def test_created_products_are_always_draft(self):
        conn, sender, _c = build_connector([
            sh_ok({"productCreate": {"product": {"id": "gid://shopify/Product/9",
                                                 "handle": "widget", "status": "DRAFT",
                                                 "title": "Widget",
                                                 "variants": {"nodes": []}},
                                     "userErrors": []}}),
        ], allow_writes=True)
        env = conn.create_draft_product(title="Widget", description_html="<p>x</p>")
        self.assertEqual(sender.last["variables"]["product"]["status"], "DRAFT")
        self.assertEqual(env.payload["status"], "DRAFT")
        self.assertTrue(any("not visible to customers" in w for w in env.warnings))

    def test_create_is_blocked_without_write_permission(self):
        conn, _s, _c = build_connector([])
        with self.assertRaises(WriteNotPermitted):
            conn.create_draft_product(title="Widget", description_html="<p>x</p>")

    def test_publish_without_authorisation_is_refused(self):
        conn, sender, _c = build_connector([], allow_writes=True)
        with self.assertRaises(WriteNotPermitted) as ctx:
            conn.publish_product("gid://shopify/Product/9", authorisation=None)
        self.assertIn("irreversible", str(ctx.exception))
        # And crucially: nothing was sent.
        self.assertEqual(sender.requests, [])

    def test_publish_with_denied_authorisation_is_refused(self):
        conn, sender, _c = build_connector([], allow_writes=True)
        denied = _Authorisation(False, "daily spend cap exceeded")
        with self.assertRaises(WriteNotPermitted) as ctx:
            conn.publish_product("gid://shopify/Product/9", authorisation=denied)
        self.assertIn("daily spend cap exceeded", str(ctx.exception))
        self.assertEqual(sender.requests, [])

    def test_truthy_non_authorisation_object_is_still_refused(self):
        # A bare `True`-ish object must not pass for an authorisation result.
        conn, _s, _c = build_connector([], allow_writes=True)
        with self.assertRaises(WriteNotPermitted):
            conn.publish_product("gid://shopify/Product/9", authorisation="yes")

    def test_publish_with_authorisation_proceeds(self):
        conn, sender, _c = build_connector([
            sh_ok({"productUpdate": {"product": {"id": "gid://shopify/Product/9",
                                                 "handle": "widget", "status": "ACTIVE",
                                                 "title": "W",
                                                 "updatedAt": "2026-07-29T00:00:00Z"},
                                     "userErrors": []}}),
            sh_ok({"publishablePublish": {
                "publishable": {"availablePublicationsCount": {"count": 1}},
                "userErrors": []}}),
        ], allow_writes=True)
        env = conn.publish_product("gid://shopify/Product/9",
                                   authorisation=_Authorisation(True),
                                   publication_ids=["gid://shopify/Publication/1"])
        self.assertEqual(env.payload["status"], "ACTIVE")
        self.assertEqual(env.payload["publications"], 1)
        self.assertEqual(len(sender.requests), 2)

    def test_active_without_publication_warns_it_is_still_invisible(self):
        conn, _s, _c = build_connector([
            sh_ok({"productUpdate": {"product": {"id": "1", "handle": "w",
                                                 "status": "ACTIVE", "title": "W",
                                                 "updatedAt": ""},
                                     "userErrors": []}}),
        ], allow_writes=True)
        env = conn.publish_product("1", authorisation=_Authorisation(True))
        self.assertTrue(any("still invisible" in w for w in env.warnings))

    def test_unpublishing_needs_no_authorisation(self):
        # Taking a listing down is how you stop a problem. Gating it means a
        # compliance issue stays live overnight.
        conn, _s, _c = build_connector([
            sh_ok({"productUpdate": {"product": {"id": "1", "status": "DRAFT"},
                                     "userErrors": []}}),
        ], allow_writes=True)
        env = conn.unpublish_product("1")
        self.assertEqual(env.payload["status"], "DRAFT")


# ---------------------------------------------------------------------------
class TestPricingAndInventoryGuards(unittest.TestCase):
    def test_non_positive_price_is_refused(self):
        conn, sender, _c = build_connector([], allow_writes=True)
        with self.assertRaises(ValueError):
            conn.update_price("BAMBOO-ORG-01", 0.0)
        self.assertEqual(sender.requests, [])

    def test_unknown_sku_raises_rather_than_no_op(self):
        empty = {"products": {"nodes": [], "pageInfo": {"hasNextPage": False}}}
        conn, _s, _c = build_connector([sh_ok(empty)], allow_writes=True)
        with self.assertRaises(ShopifyAPIError) as ctx:
            conn.update_price("NOPE-01", 19.99)
        self.assertIn("nothing to reprice", str(ctx.exception))

    def test_ambiguous_sku_refuses_to_guess(self):
        dupes = {"products": {"nodes": [
            {"id": "gid://shopify/Product/1",
             "variants": {"nodes": [{"id": "v1", "sku": "DUP"}]}},
            {"id": "gid://shopify/Product/2",
             "variants": {"nodes": [{"id": "v2", "sku": "DUP"}]}},
        ], "pageInfo": {"hasNextPage": False}}}
        conn, _s, _c = build_connector([sh_ok(dupes)], allow_writes=True)
        with self.assertRaises(ShopifyAPIError) as ctx:
            conn.update_price("DUP", 19.99)
        self.assertIn("Refusing to guess", str(ctx.exception))

    def test_negative_inventory_is_refused(self):
        conn, _s, _c = build_connector([], allow_writes=True)
        with self.assertRaises(ValueError):
            conn.set_inventory(inventory_item_id="gid://shopify/InventoryItem/1",
                               quantity=-5, location_id="gid://shopify/Location/1")

    def test_multi_location_store_refuses_to_pick_one(self):
        two = {"locations": {"nodes": [
            {"id": "gid://shopify/Location/1", "name": "A", "isActive": True,
             "fulfillsOnlineOrders": True},
            {"id": "gid://shopify/Location/2", "name": "B", "isActive": True,
             "fulfillsOnlineOrders": True},
        ], "pageInfo": {"hasNextPage": False}}}
        conn, _s, _c = build_connector([sh_ok(two)], allow_writes=True)
        with self.assertRaises(ShopifyAPIError) as ctx:
            conn.default_location_id()
        self.assertIn("oversell", str(ctx.exception))

    def test_inventory_set_is_absolute_not_a_delta(self):
        # A delta double-counts when a daily run is retried.
        conn, sender, _c = build_connector([
            sh_ok({"inventorySetQuantities": {
                "inventoryAdjustmentGroup": {"createdAt": "", "reason": "correction",
                                             "changes": []},
                "userErrors": []}}),
        ], allow_writes=True)
        conn.set_inventory(inventory_item_id="gid://shopify/InventoryItem/1",
                           quantity=42, location_id="gid://shopify/Location/1")
        payload = sender.last["variables"]["input"]
        self.assertEqual(payload["quantities"][0]["quantity"], 42)
        self.assertEqual(payload["name"], "available")


class TestDiscountGuards(unittest.TestCase):
    def test_unbounded_code_requires_authorisation(self):
        conn, sender, _c = build_connector([], allow_writes=True)
        with self.assertRaises(WriteNotPermitted) as ctx:
            conn.create_discount_code(code="TIKTOK15", percentage=15,
                                      starts_at="2026-08-01T00:00:00Z")
        self.assertIn("usage limit", str(ctx.exception))
        self.assertEqual(sender.requests, [])

    def test_bounded_code_needs_no_authorisation(self):
        conn, sender, _c = build_connector([
            sh_ok({"discountCodeBasicCreate": {
                "codeDiscountNode": {"id": "gid://shopify/DiscountCodeNode/1"},
                "userErrors": []}}),
        ], allow_writes=True)
        env = conn.create_discount_code(code="TIKTOK15", percentage=15,
                                        starts_at="2026-08-01T00:00:00Z",
                                        ends_at="2026-08-31T00:00:00Z")
        self.assertEqual(env.payload["code"], "TIKTOK15")
        self.assertEqual(sender.last["variables"]["basicCodeDiscount"]
                         ["customerGets"]["value"]["percentage"], 0.15)

    def test_out_of_range_percentage_is_refused(self):
        conn, _s, _c = build_connector([], allow_writes=True)
        for bad in (0, 100, 150, -5):
            with self.assertRaises(ValueError):
                conn.create_discount_code(code="X", percentage=bad,
                                          starts_at="2026-08-01T00:00:00Z",
                                          ends_at="2026-08-31T00:00:00Z")


# ---------------------------------------------------------------------------
class TestUnavailableData(unittest.TestCase):
    """What Shopify cannot see must raise, not return an empty list."""

    def test_competitor_offers_raise_with_the_reason(self):
        conn, _s, _c = build_connector([])
        with self.assertRaises(NotImplementedError) as ctx:
            conn.fetch_competitor_offers("BAMBOO-ORG-01")
        self.assertIn("ToS", str(ctx.exception))

    def test_reviews_raise_rather_than_reporting_none(self):
        conn, _s, _c = build_connector([])
        with self.assertRaises(NotImplementedError):
            conn.fetch_reviews("BAMBOO-ORG-01")

    def test_ad_performance_raises(self):
        conn, _s, _c = build_connector([])
        with self.assertRaises(NotImplementedError):
            conn.fetch_ad_performance(since="2026-07-01")


class TestListingSummary(unittest.TestCase):
    def test_missing_unit_cost_is_none_not_zero(self):
        # A zero cost reports infinite margin on every product missing the field.
        conn, _s, _c = build_connector([sh_ok(SH_PRODUCTS)])
        listings = {l["handle"]: l for l in conn.fetch_listings().payload}
        self.assertEqual(listings["bamboo-drawer-organizer"]["avg_unit_cost"], 9.4)
        self.assertIsNone(listings["pet-slicker-brush"]["avg_unit_cost"])

    def test_drafts_are_flagged_as_earning_nothing(self):
        conn, _s, _c = build_connector([sh_ok(SH_PRODUCTS)])
        env = conn.fetch_listings()
        self.assertTrue(any("DRAFT" in w for w in env.warnings))

    def test_untracked_inventory_is_warned_about(self):
        rows = {"productVariants": {"nodes": [{
            "id": "v1", "sku": "X", "displayName": "X", "inventoryQuantity": 5,
            "inventoryItem": {"id": "i1", "tracked": False, "unitCost": None,
                              "inventoryLevels": {"nodes": []}},
        }], "pageInfo": {"hasNextPage": False}}}
        conn, _s, _c = build_connector([sh_ok(rows)])
        env = conn.fetch_inventory()
        self.assertTrue(any("tracking disabled" in w for w in env.warnings))
        self.assertTrue(any("without limit" in w for w in env.warnings))


# ---------------------------------------------------------------------------
class TestVerification(unittest.TestCase):
    def test_absent_credentials_report_setup_steps(self):
        conn = ShopifyConnector(credentials=UnavailableCredentials())
        result = conn.verify_connection()
        self.assertFalse(result["ok"])
        self.assertIn("Develop apps", result["remedy"])
        self.assertEqual(result["credential_source"], "unavailable")

    def test_missing_scope_is_reported_as_distinct_from_a_bad_token(self):
        partial = {"currentAppInstallation": {"accessScopes": [
            {"handle": "read_products"}, {"handle": "read_orders"}]}}
        conn, _s, _c = build_connector([sh_ok(SH_SHOP), sh_ok(partial)])
        result = conn.verify_connection()
        self.assertFalse(result["ok"])
        self.assertIn("write_products", result["missing_scopes"])
        self.assertTrue(any("reinstall" in w for w in result["warnings"]))

    def test_wrong_store_is_caught(self):
        # A token for a different store would send every write elsewhere.
        other = {"shop": dict(SH_SHOP["shop"], myshopifyDomain="other.myshopify.com")}
        conn, _s, _c = build_connector([sh_ok(other), sh_ok(SH_SCOPES)])
        result = conn.verify_connection()
        self.assertTrue(any("wrong store" in w for w in result["warnings"]))

    def test_successful_verification(self):
        conn, _s, _c = build_connector([sh_ok(SH_SHOP), sh_ok(SH_SCOPES)])
        result = conn.verify_connection()
        self.assertTrue(result["ok"])
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(result["domain"], "test-store.myshopify.com")

    def test_unconfigured_client_raises_connector_not_configured(self):
        conn = ShopifyConnector(credentials=UnavailableCredentials())
        with self.assertRaises(ConnectorNotConfigured):
            conn.client()

    def test_token_absent_from_repr(self):
        creds = ShopifyCredentials("s.myshopify.com", "shpat_SUPERSECRET")
        self.assertNotIn("SUPERSECRET", repr(creds))
        self.assertNotIn("SUPERSECRET", repr(StaticCredentials(creds)))


class TestCredentialSources(unittest.TestCase):
    def test_environment_source_reads_the_documented_variables(self):
        with mock.patch.dict(os.environ, LIVE_ENV, clear=True):
            from connectors.shopify import EnvCredentials
            creds = EnvCredentials().resolve()
        self.assertEqual(creds.shop_domain, "test-store.myshopify.com")
        self.assertEqual(creds.api_version, "2025-07")

    def test_missing_variables_are_named(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            from connectors.shopify import EnvCredentials
            status = EnvCredentials().status()
        self.assertFalse(status.available)
        self.assertIn("SHOPIFY_ADMIN_ACCESS_TOKEN",
                      " ".join(status.detail.split()))

    def test_unavailable_source_raises_with_a_remedy(self):
        with self.assertRaises(CredentialsUnavailable) as ctx:
            UnavailableCredentials().resolve()
        self.assertIn("Develop apps", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class TestStatusReporting(unittest.TestCase):
    def test_status_names_environment_variables_not_internal_fields(self):
        # A status command that reports `shop_domain` makes you read the source
        # to discover you set SHOPIFY_SHOP_DOMAIN.
        with mock.patch.dict(os.environ, {}, clear=True):
            status = ShopifyConnector().status()
        self.assertIn("SHOPIFY_SHOP_DOMAIN", status["missing_env"])
        self.assertIn("SHOPIFY_ADMIN_ACCESS_TOKEN", status["missing_env"])
        self.assertNotIn("shop_domain", status["missing_env"])

    def test_configured_connector_reports_nothing_missing(self):
        conn = ShopifyConnector(credentials=StaticCredentials(CREDS))
        self.assertTrue(conn.status()["configured"])
        self.assertEqual(conn.status()["missing_env"], [])

    def test_file_configured_connector_is_not_reported_unconfigured(self):
        # base.missing_credentials used to read os.environ directly, so a
        # connector configured from a file reported itself unconfigured while
        # working perfectly.
        with mock.patch.dict(os.environ, {}, clear=True):
            conn = ShopifyConnector(credentials=StaticCredentials(CREDS))
            self.assertTrue(conn.configured)
