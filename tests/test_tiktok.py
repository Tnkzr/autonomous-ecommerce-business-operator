"""TikTok Shop integration tests, run entirely offline.

The highest-value assertions here are about TikTok's specific traps:

  - HTTP 200 with a non-zero code is a FAILURE, and a client that misses this
    reports every error as success.
  - The signature excludes `sign` and `access_token`, and the signed body must
    be byte-identical to the body sent.
  - Order value is not revenue; settlements are.
  - A spike that has already decayed looks identical to growth in a 30-day
    total and calls for the opposite inventory decision.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.base import ConnectorNotConfigured, WriteNotPermitted
from connectors.tiktok import (
    TikTokAPIError,
    TikTokCredentials,
    TikTokRateLimited,
    TikTokShopClient,
    TikTokShopConnector,
    TokenBucket,
    TokenProvider,
    Transport,
    UnknownRegion,
    canonical_string,
    resolve,
    sign_request,
)
from operator_core.config import load_policy
from operator_core.tiktok import (
    TREND_DEAD,
    TREND_DECAYING,
    TREND_GROWING,
    TREND_INSUFFICIENT,
    TREND_SPIKE_DECAY,
    TREND_STEADY,
    DailyPoint,
    analyse_trend,
    build_optimisation_report,
    compute_profit,
)
from tests.fakes import (
    TT_CREATE_OK,
    TT_ORDERS,
    TT_PRODUCT_PERFORMANCE,
    TT_PRODUCTS,
    TT_SHOPS,
    TT_STATEMENTS,
    FakeClock,
    FakeTikTokTokens,
    TikTokSender,
    tt_err,
    tt_ok,
)

POLICY = load_policy()

LIVE_ENV = {
    "TIKTOK_APP_KEY": "abc123",
    "TIKTOK_APP_SECRET": "test_secret",
    "TIKTOK_REFRESH_TOKEN": "rt-test",
    "TIKTOK_SHOP_ID": "7000000000000000001",
    "TIKTOK_REGION": "US",
}


def build_client(script: list, *, shop_cipher: str = "ROW_CIPHER_ABC"):
    sender = TikTokSender(script)
    clock = FakeClock()
    transport = Transport(
        base_url="https://open-api.tiktokglobalshop.com",
        app_key="abc123", app_secret="test_secret",
        token_provider=FakeTikTokTokens(), shop_cipher=shop_cipher,
        send=sender, sleep=clock.sleep, monotonic=clock.monotonic,
    )
    return TikTokShopClient(transport, shop_id="7000000000000000001"), sender, clock


def build_connector(script: list):
    client, sender, clock = build_client(script)
    conn = TikTokShopConnector()
    conn._client = client
    return conn, sender, clock


class TestSigning(unittest.TestCase):
    def test_matches_hand_computed_vector(self):
        # Asserted against arithmetic done independently, not against whatever
        # the implementation happens to emit.
        params = {"app_key": "abc123", "timestamp": 1700000000,
                  "shop_cipher": "CIPHER1", "page_size": 50,
                  "sign": "IGNORED", "access_token": "IGNORED"}
        body = '{"status":"ACTIVATE"}'
        expected = (
            "46e1264ef62ab7ebdb5daabaf91328508211b1aaa7932e281911ac49d62eb167"
        )
        got = sign_request(path="/product/202309/products/search", params=params,
                           app_secret="test_secret", body=body)
        self.assertEqual(got, expected)

    def test_sign_and_access_token_are_excluded(self):
        base = {"app_key": "k", "timestamp": 1}
        with_extras = dict(base, sign="xxx", access_token="yyy")
        self.assertEqual(
            sign_request(path="/p", params=base, app_secret="s"),
            sign_request(path="/p", params=with_extras, app_secret="s"),
            "Including sign or access_token produces a signature that always fails.",
        )

    def test_params_are_sorted_not_insertion_ordered(self):
        a = {"zebra": 1, "alpha": 2}
        b = {"alpha": 2, "zebra": 1}
        self.assertEqual(sign_request(path="/p", params=a, app_secret="s"),
                         sign_request(path="/p", params=b, app_secret="s"))

    def test_secret_wraps_both_ends(self):
        base = canonical_string(path="/p", params={"a": 1}, app_secret="SEC")
        self.assertTrue(base.startswith("SEC"))
        self.assertTrue(base.endswith("SEC"))

    def test_body_participates_in_signature(self):
        without = sign_request(path="/p", params={"a": 1}, app_secret="s")
        with_body = sign_request(path="/p", params={"a": 1}, app_secret="s",
                                 body='{"x":1}')
        self.assertNotEqual(without, with_body)

    def test_booleans_serialise_as_json_not_python(self):
        # str(True) is "True"; every HTTP API expects "true".
        base = canonical_string(path="/p", params={"flag": True}, app_secret="s")
        self.assertIn("flagtrue", base)
        self.assertNotIn("flagTrue", base)

    def test_none_values_are_dropped(self):
        with_none = canonical_string(path="/p", params={"a": 1, "b": None},
                                     app_secret="s")
        without = canonical_string(path="/p", params={"a": 1}, app_secret="s")
        self.assertEqual(with_none, without)

    def test_missing_secret_raises(self):
        with self.assertRaises(ValueError):
            sign_request(path="/p", params={}, app_secret="")


class TestRegions(unittest.TestCase):
    def test_known_region_resolves(self):
        base, currency, lag = resolve("US")
        self.assertIn("tiktokglobalshop", base)
        self.assertEqual(currency, "USD")
        self.assertGreater(lag, 0)

    def test_unknown_region_raises_not_defaults(self):
        with self.assertRaises(UnknownRegion):
            resolve("ATLANTIS")

    def test_sandbox_is_distinct(self):
        self.assertNotEqual(resolve("US")[0], resolve("US", sandbox=True)[0])

    def test_currency_varies_by_region(self):
        self.assertEqual(resolve("GB")[1], "GBP")
        self.assertEqual(resolve("ID")[1], "IDR")


class TestTransportErrorModel(unittest.TestCase):
    def test_http_200_with_error_code_is_a_failure(self):
        # The single most important behaviour in the whole integration.
        client, _s, _c = build_client([tt_err(105002, "service unavailable")] * 6)
        with self.assertRaises(TikTokAPIError):
            client.get_authorized_shops()

    def test_success_requires_code_zero(self):
        client, _s, _c = build_client([tt_ok(TT_SHOPS)])
        shops = client.get_authorized_shops()
        self.assertEqual(len(shops), 2)
        self.assertEqual(shops[0].shop_cipher, "ROW_CIPHER_ABC")

    def test_retryable_code_is_retried(self):
        client, sender, clock = build_client([
            tt_err(105000, "internal error"),
            tt_ok(TT_SHOPS),
        ])
        client.get_authorized_shops()
        self.assertEqual(len(sender.requests), 2)
        self.assertGreater(clock.total, 0)

    def test_deterministic_error_is_not_retried(self):
        client, sender, _c = build_client([tt_err(11000, "invalid parameter")])
        with self.assertRaises(TikTokAPIError):
            client.get_authorized_shops()
        self.assertEqual(len(sender.requests), 1,
                         "Retrying a rejected payload burns quota and looks like abuse.")

    def test_auth_failure_reauthenticates(self):
        sender = TikTokSender([tt_err(105001, "token invalid"), tt_ok(TT_SHOPS)])
        tokens = FakeTikTokTokens()
        clock = FakeClock()
        transport = Transport(
            base_url="https://x", app_key="k", app_secret="s",
            token_provider=tokens, shop_cipher="C",
            send=sender, sleep=clock.sleep, monotonic=clock.monotonic,
        )
        TikTokShopClient(transport).get_authorized_shops()
        self.assertEqual(tokens.invalidations, 1)

    def test_persistent_throttle_raises_rate_limited(self):
        client, _s, _c = build_client([tt_err(36004003, "rate limited")] * 6)
        with self.assertRaises(TikTokRateLimited):
            client.get_authorized_shops()

    def test_error_message_includes_signature_hint(self):
        client, _s, _c = build_client([tt_err(11001, "invalid sign")])
        with self.assertRaises(TikTokAPIError) as ctx:
            client.get_authorized_shops()
        self.assertIn("signature rejected", str(ctx.exception).lower())

    def test_shop_cipher_error_is_explained(self):
        client, _s, _c = build_client([tt_err(11002, "shop_cipher is invalid")])
        with self.assertRaises(TikTokAPIError) as ctx:
            client.get_authorized_shops()
        self.assertIn("/authorization/", str(ctx.exception))

    def test_http_error_still_raises(self):
        client, _s, _c = build_client([(500, {"code": 0}, {})] * 6)
        with self.assertRaises(TikTokAPIError):
            client.get_authorized_shops()

    def test_signed_body_matches_sent_body(self):
        # Signing one serialisation and sending another is an instant failure.
        client, sender, _c = build_client([tt_ok(TT_PRODUCTS)])
        client.search_products(status="ACTIVATE")
        sent = sender.last["body"]
        self.assertEqual(sent, json.dumps({"status": "ACTIVATE"}, separators=(",", ":")))

    def test_shop_cipher_is_attached_to_shop_scoped_calls(self):
        client, sender, _c = build_client([tt_ok(TT_PRODUCTS)])
        client.search_products()
        self.assertIn("shop_cipher=ROW_CIPHER_ABC", sender.last["url"])

    def test_authorization_call_omits_shop_cipher(self):
        # It is the call that *fetches* the cipher; sending one is circular.
        client, sender, _c = build_client([tt_ok(TT_SHOPS)])
        client.get_authorized_shops()
        self.assertNotIn("shop_cipher", sender.last["url"])


class TestRateLimiter(unittest.TestCase):
    def test_shared_bucket_throttles_after_burst(self):
        clock = FakeClock()
        bucket = TokenBucket(rate=2.0, burst=2, monotonic=clock.monotonic)
        bucket.acquire(sleep=clock.sleep)
        bucket.acquire(sleep=clock.sleep)
        self.assertEqual(clock.total, 0.0)
        bucket.acquire(sleep=clock.sleep)
        self.assertGreater(clock.total, 0.0)


class TestAuth(unittest.TestCase):
    def test_token_is_cached(self):
        calls = []

        def fake_get(url, timeout):
            calls.append(url)
            return json.dumps({"code": 0, "data": {
                "access_token": "at", "access_token_expire_in": 3600,
                "refresh_token": "rt-test"}})

        p = TokenProvider(TikTokCredentials("k", "s", "rt-test"), http_get=fake_get)
        p.access_token()
        p.access_token()
        self.assertEqual(len(calls), 1)

    def test_rotated_refresh_token_is_surfaced(self):
        # Losing the rotated token strands the integration months later.
        rotated = []

        def fake_get(url, timeout):
            return json.dumps({"code": 0, "data": {
                "access_token": "at", "access_token_expire_in": 3600,
                "refresh_token": "rt-NEW"}})

        p = TokenProvider(TikTokCredentials("k", "s", "rt-OLD"), http_get=fake_get,
                          on_refresh_token_rotated=rotated.append)
        p.access_token()
        self.assertEqual(rotated, ["rt-NEW"])
        self.assertEqual(p.credentials.refresh_token, "rt-NEW")

    def test_rotation_without_callback_warns(self):
        def fake_get(url, timeout):
            return json.dumps({"code": 0, "data": {
                "access_token": "at", "access_token_expire_in": 3600,
                "refresh_token": "rt-NEW"}})

        p = TokenProvider(TikTokCredentials("k", "s", "rt-OLD"), http_get=fake_get)
        p.access_token()
        self.assertTrue(any("rotated" in w for w in p.warnings))

    def test_auth_failure_inside_200_raises(self):
        def fake_get(url, timeout):
            return json.dumps({"code": 105001, "message": "app not authorised"})

        p = TokenProvider(TikTokCredentials("k", "s", "rt"), http_get=fake_get)
        with self.assertRaises(Exception) as ctx:
            p.access_token()
        self.assertIn("105001", str(ctx.exception))

    def test_secrets_not_in_repr(self):
        c = TikTokCredentials("app-key-123", "super-secret", "refresh-secret")
        self.assertNotIn("super-secret", repr(c))
        self.assertNotIn("refresh-secret", repr(c))


class TestReads(unittest.TestCase):
    def test_orders_flag_that_totals_are_not_revenue(self):
        conn, _s, _c = build_connector([tt_ok(TT_ORDERS)])
        env = conn.fetch_orders(since="2026-07-01")
        self.assertEqual(env.source, "live")
        self.assertEqual(len(env.payload), 2)
        self.assertTrue(any("not what TikTok will pay you" in w for w in env.warnings))

    def test_unpaid_orders_are_flagged(self):
        conn, _s, _c = build_connector([tt_ok(TT_ORDERS)])
        env = conn.fetch_orders(since="2026-07-01")
        self.assertTrue(any("UNPAID" in w for w in env.warnings))

    def test_inventory_sums_across_warehouses(self):
        conn, _s, _c = build_connector([tt_ok(TT_PRODUCTS)])
        env = conn.fetch_inventory()
        brush = next(r for r in env.payload if r["sku"] == "PETBRUSH-03")
        self.assertEqual(brush["on_hand_units"], 12)
        self.assertTrue(any("multiple warehouses" in w for w in env.warnings))

    def test_zero_stock_warns_about_feed_suppression(self):
        out_of_stock = {"products": [{
            "id": "170000000009", "title": "Sold Out Item", "status": "ACTIVATE",
            "category_chains": [{"id": "1"}],
            "skus": [{"id": "SKU-9", "seller_sku": "GONE-01",
                      "price": {"sale_price": "19.99", "currency": "USD"},
                      "inventory": [{"warehouse_id": "WH1", "quantity": 0}]}],
        }], "next_page_token": ""}
        conn, _s, _c = build_connector([tt_ok(out_of_stock)])
        env = conn.fetch_inventory()
        self.assertEqual(env.payload[0]["on_hand_units"], 0)
        self.assertTrue(any("suppresses out-of-stock" in w for w in env.warnings))

    def test_settlements_expose_real_take_rate(self):
        conn, _s, _c = build_connector([tt_ok(TT_STATEMENTS)])
        env = conn.fetch_settlements(days=30)
        self.assertAlmostEqual(env.payload["take_rate_pct"], 22.61, places=1)
        self.assertTrue(any("Reconcile [fees.tiktok]" in w for w in env.warnings))

    def test_product_performance_maps(self):
        conn, _s, _c = build_connector([tt_ok(TT_PRODUCT_PERFORMANCE)])
        env = conn.fetch_product_performance(days=30)
        self.assertEqual(env.payload[0]["units_sold"], 100)
        self.assertEqual(env.payload[0]["page_views"], 8200)

    def test_pagination_follows_token(self):
        page1 = dict(TT_PRODUCTS, next_page_token="T2")
        page2 = dict(TT_PRODUCTS, next_page_token="")
        client, sender, _c = build_client([tt_ok(page1), tt_ok(page2)])
        products = client.search_products()
        self.assertEqual(len(products), 4)
        self.assertIn("page_token=T2", sender.urls()[1])

    def test_page_cap_bounds_the_crawl(self):
        endless = dict(TT_PRODUCTS, next_page_token="MORE")
        client, sender, _c = build_client([tt_ok(endless)] * 10)
        client.search_products(max_pages=3)
        self.assertEqual(len(sender.requests), 3)


class TestUnavailableSurfaces(unittest.TestCase):
    def test_competitor_data_refuses_and_explains(self):
        # TikTok exposes no competitor pricing; scraping breaches ToS and
        # risks the shop this system exists to protect.
        conn, _s, _c = build_connector([])
        with mock.patch.dict(os.environ, LIVE_ENV):
            with self.assertRaises(ConnectorNotConfigured) as ctx:
                conn.fetch_competitor_offers("170000000001")
        message = str(ctx.exception)
        self.assertIn("Terms of Service", message)
        self.assertIn("fabricated competitor prices", message)

    def test_reviews_refuse(self):
        conn, _s, _c = build_connector([])
        with mock.patch.dict(os.environ, LIVE_ENV):
            with self.assertRaises(ConnectorNotConfigured):
                conn.fetch_reviews("SKU-1")

    def test_ads_point_to_the_marketing_api(self):
        conn, _s, _c = build_connector([])
        with mock.patch.dict(os.environ, LIVE_ENV):
            with self.assertRaises(ConnectorNotConfigured) as ctx:
                conn.fetch_ad_performance(since="2026-07-01")
        self.assertIn("Marketing API", str(ctx.exception))


class TestWrites(unittest.TestCase):
    def test_writes_blocked_without_permission(self):
        conn, _s, _c = build_connector([])
        with self.assertRaises(WriteNotPermitted):
            conn.publish_listing({"title": "x"})
        with self.assertRaises(WriteNotPermitted):
            conn.update_price("SKU", 10.0, product_id="1", sku_id="2")

    def test_policy_screen_blocks_prohibited_product(self):
        conn, _s, _c = build_connector([])
        conn.allow_writes = True
        with mock.patch.dict(os.environ, LIVE_ENV):
            with self.assertRaises(ValueError) as ctx:
                conn.publish_listing({
                    "title": "Detox Weight Loss Tea", "description": "Slimming blend",
                    "category_id": "1", "package_weight": {"value": "1"},
                    "skus": [{"price": {"amount": "9.99", "currency": "USD"}}],
                })
        self.assertIn("policy screen failed", str(ctx.exception))

    def test_policy_screen_blocks_efficacy_claims(self):
        violations = TikTokShopConnector.screen_for_policy({
            "title": "Joint Cream", "description": "Clinically proven to cure pain",
        })
        self.assertTrue(violations)

    def test_clean_product_passes_screen(self):
        violations = TikTokShopConnector.screen_for_policy({
            "title": "Bamboo Drawer Organizer",
            "description": "Expandable bamboo organiser for kitchen drawers.",
        })
        self.assertEqual(violations, [])

    def test_create_sends_expected_payload(self):
        conn, sender, _c = build_connector([tt_ok(TT_CREATE_OK)])
        conn.allow_writes = True
        payload = {
            "title": "Bamboo Drawer Organizer",
            "description": "Expandable bamboo organiser.",
            "category_id": "601152",
            "package_weight": {"value": "2.4", "unit": "POUND"},
            "skus": [{"price": {"amount": "34.99", "currency": "USD"}}],
        }
        with mock.patch.dict(os.environ, LIVE_ENV):
            env = conn.publish_listing(payload)
        self.assertEqual(sender.last["method"], "POST")
        self.assertEqual(env.payload["product_id"], "170000000003")
        self.assertTrue(any("Image resolution" in w for w in env.warnings))

    def test_create_validates_before_calling_api(self):
        conn, sender, _c = build_connector([])
        conn.allow_writes = True
        with mock.patch.dict(os.environ, LIVE_ENV):
            with self.assertRaises(ValueError) as ctx:
                conn.publish_listing({"title": "x", "description": "y"})
        self.assertIn("missing", str(ctx.exception))
        self.assertEqual(len(sender.requests), 0,
                         "Validation must precede the call, not waste a rate slot.")

    def test_float_price_is_rejected(self):
        conn, _s, _c = build_connector([])
        conn.allow_writes = True
        with mock.patch.dict(os.environ, LIVE_ENV):
            with self.assertRaises(ValueError) as ctx:
                conn.publish_listing({
                    "title": "T", "description": "D", "category_id": "1",
                    "package_weight": {"value": "1"},
                    "skus": [{"price": {"amount": 34.99, "currency": "USD"}}],
                })
        self.assertIn("string", str(ctx.exception))

    def test_price_update_formats_amount_as_string(self):
        conn, sender, _c = build_connector([tt_ok({})])
        conn.allow_writes = True
        with mock.patch.dict(os.environ, LIVE_ENV):
            conn.update_price("BAMBOO", 32.30, product_id="P1", sku_id="S1")
        body = json.loads(sender.last["body"])
        self.assertEqual(body["skus"][0]["price"]["amount"], "32.30",
                         "12.30 serialised as a float becomes 12.3 and is rejected.")

    def test_price_update_needs_ids(self):
        conn, _s, _c = build_connector([])
        conn.allow_writes = True
        with mock.patch.dict(os.environ, LIVE_ENV):
            with self.assertRaises(ValueError):
                conn.update_price("SELLER-SKU", 10.0)

    def test_update_warns_that_it_replaces(self):
        conn, _s, _c = build_connector([tt_ok({"skus": []})])
        conn.allow_writes = True
        with mock.patch.dict(os.environ, LIVE_ENV):
            env = conn.update_listing("P1", {
                "title": "T", "description": "D", "category_id": "1",
                "package_weight": {"value": "1"},
                "skus": [{"price": {"amount": "9.99", "currency": "USD"}}],
            })
        self.assertTrue(any("replaces the listing" in w for w in env.warnings))

    def test_inventory_without_warehouse_warns(self):
        conn, _s, _c = build_connector([tt_ok({})])
        conn.allow_writes = True
        with mock.patch.dict(os.environ, LIVE_ENV):
            env = conn.update_inventory("P1", "S1", 100)
        self.assertTrue(any("default warehouse" in w for w in env.warnings))


class TestVerification(unittest.TestCase):
    def test_missing_credentials_reported(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            r = TikTokShopConnector().verify_connection()
        self.assertFalse(r["ok"])
        self.assertIn("Missing credentials", r["detail"])

    def test_successful_verification(self):
        conn, _s, _c = build_connector([tt_ok(TT_SHOPS)])
        with mock.patch.dict(os.environ, LIVE_ENV):
            r = conn.verify_connection()
        self.assertTrue(r["ok"])
        self.assertEqual(r["currency"], "USD")

    def test_region_mismatch_is_caught(self):
        # Wrong region means wrong currency and settlement lag, which silently
        # corrupts every profit calculation. The transport is injected rather
        # than a built client so the shop bootstrap actually runs.
        client, _s, _c = build_client([tt_ok(TT_SHOPS)])
        conn = TikTokShopConnector()
        conn._transport = client.t
        with mock.patch.dict(os.environ, dict(LIVE_ENV, TIKTOK_REGION="GB")):
            r = conn.verify_connection()
        self.assertFalse(r["ok"])
        self.assertIn("region", r["detail"].lower())

    def test_verification_does_not_double_fetch_shops(self):
        # The shop list is needed for shop_cipher anyway; fetching it twice
        # wastes a slot on a shared per-app QPS budget.
        client, sender, _c = build_client([tt_ok(TT_SHOPS)])
        conn = TikTokShopConnector()
        conn._transport = client.t
        with mock.patch.dict(os.environ, LIVE_ENV):
            r = conn.verify_connection()
        self.assertTrue(r["ok"])
        self.assertEqual(len(sender.requests), 1)

    def test_bad_region_diagnosed(self):
        conn, _s, _c = build_connector([])
        with mock.patch.dict(os.environ, dict(LIVE_ENV, TIKTOK_REGION="ATLANTIS")):
            r = conn.verify_connection()
        self.assertFalse(r["ok"])
        self.assertIn("not a known TikTok Shop market", r["detail"])

    def test_unauthorised_shop_id_is_caught(self):
        client, _s, _c = build_client([tt_ok(TT_SHOPS)])
        conn = TikTokShopConnector()
        conn._transport = client.t
        with mock.patch.dict(os.environ, dict(LIVE_ENV,
                                              TIKTOK_SHOP_ID="9999999999")):
            r = conn.verify_connection()
        self.assertFalse(r["ok"])
        self.assertIn("not among the shops", r["detail"])


class TestProfit(unittest.TestCase):
    def test_settlement_beats_estimate(self):
        estimated = compute_profit(
            policy=POLICY, product_id="P1", units=100, gross_revenue=3499.0,
            cogs_per_unit=10.30,
        )
        settled = compute_profit(
            policy=POLICY, product_id="P1", units=100, gross_revenue=3499.0,
            cogs_per_unit=10.30,
            settlement={"revenue_amount": 2700.0, "fee_amount": -799.0},
        )
        self.assertEqual(estimated.revenue_basis, "estimated")
        self.assertEqual(settled.revenue_basis, "settlement")
        self.assertNotEqual(estimated.net_revenue, settled.net_revenue)

    def test_take_rate_is_computed(self):
        p = compute_profit(
            policy=POLICY, product_id="P1", units=100, gross_revenue=1000.0,
            cogs_per_unit=3.0, affiliate_rate_pct=10.0, seller_promotion_pct=5.0,
        )
        self.assertGreater(p.take_rate_pct, 20.0,
                           "Commission + payment + affiliate + promo must all count.")

    def test_returns_are_charged(self):
        p = compute_profit(policy=POLICY, product_id="P1", units=100,
                           gross_revenue=1000.0, cogs_per_unit=3.0)
        self.assertGreater(p.return_cost, 0.0)

    def test_after_tax_below_pre_tax_when_profitable(self):
        p = compute_profit(policy=POLICY, product_id="P1", units=100,
                           gross_revenue=3000.0, cogs_per_unit=3.0)
        self.assertGreater(p.pre_tax_profit, 0)
        self.assertLess(p.after_tax_profit, p.pre_tax_profit)

    def test_affiliate_commission_can_flip_a_thin_product(self):
        without = compute_profit(policy=POLICY, product_id="P1", units=100,
                                 gross_revenue=1000.0, cogs_per_unit=6.5)
        with_aff = compute_profit(policy=POLICY, product_id="P1", units=100,
                                  gross_revenue=1000.0, cogs_per_unit=6.5,
                                  affiliate_rate_pct=20.0)
        self.assertGreater(without.pre_tax_profit, with_aff.pre_tax_profit)


class TestTrends(unittest.TestCase):
    def _series(self, units: list[int]) -> list[DailyPoint]:
        return [
            DailyPoint(day=f"2026-07-{i + 1:02d}", units=u, gmv=u * 30.0,
                       page_views=u * 60, orders=u)
            for i, u in enumerate(units)
        ]

    def test_short_history_is_insufficient(self):
        t = analyse_trend(product_id="P", title="T", history=self._series([5] * 5))
        self.assertEqual(t.shape, TREND_INSUFFICIENT)
        self.assertIn("Hold", t.inventory_guidance)

    def test_growth_is_detected(self):
        t = analyse_trend(product_id="P", title="T",
                          history=self._series([2, 2, 3, 3, 4, 4, 5,
                                                8, 9, 10, 11, 12, 12, 13]))
        self.assertEqual(t.shape, TREND_GROWING)
        self.assertGreater(t.change_pct, 25)

    def test_spike_decay_is_not_mistaken_for_growth(self):
        # The whole point: this has a strong 30-day total and must not be
        # reordered on the peak.
        units = [1, 1, 2, 60, 80, 40, 20, 10, 5, 3, 2, 2, 1, 1]
        t = analyse_trend(product_id="P", title="T", history=self._series(units))
        self.assertEqual(t.shape, TREND_SPIKE_DECAY)
        self.assertIn("tail", t.inventory_guidance)

    def test_decay_is_detected(self):
        t = analyse_trend(product_id="P", title="T",
                          history=self._series([20, 19, 18, 17, 16, 15, 14,
                                                8, 7, 6, 5, 4, 3, 2]))
        self.assertEqual(t.shape, TREND_DECAYING)

    def test_steady_is_detected(self):
        t = analyse_trend(product_id="P", title="T", history=self._series([10] * 14))
        self.assertEqual(t.shape, TREND_STEADY)

    def test_zero_sales_is_dead(self):
        t = analyse_trend(product_id="P", title="T", history=self._series([0] * 14))
        self.assertEqual(t.shape, TREND_DEAD)
        self.assertIn("cannot fix", t.inventory_guidance)

    def test_high_volatility_is_called_out(self):
        t = analyse_trend(product_id="P", title="T",
                          history=self._series([0, 40, 0, 35, 0, 38, 0,
                                                42, 0, 36, 0, 39, 0, 41]))
        self.assertGreater(t.volatility, 0.9)
        self.assertIn("safety stock", t.confidence_note)


class TestOptimisationReport(unittest.TestCase):
    def _trend(self, shape_units, **kw):
        history = [DailyPoint(day=f"2026-07-{i + 1:02d}", units=u, gmv=u * 30,
                              page_views=u * 60, orders=u)
                   for i, u in enumerate(shape_units)]
        return analyse_trend(product_id=kw.get("pid", "P1"),
                             title=kw.get("title", "Product"), history=history)

    def test_loss_making_product_is_critical(self):
        t = self._trend([10] * 14)
        losing = compute_profit(policy=POLICY, product_id="P1", units=100,
                                gross_revenue=500.0, cogs_per_unit=8.0)
        actions, _ = build_optimisation_report(
            policy=POLICY, trends=[t], profits={"P1": losing},
            inventory=[{"product_id": "P1", "on_hand_units": 100}],
        )
        self.assertEqual(actions[0].priority, "CRITICAL")
        self.assertIn("Losing", actions[0].rationale)

    def test_growing_product_low_on_stock_is_critical(self):
        t = self._trend([2, 2, 3, 3, 4, 4, 5, 8, 9, 10, 11, 12, 12, 13])
        good = compute_profit(policy=POLICY, product_id="P1", units=100,
                              gross_revenue=3000.0, cogs_per_unit=3.0)
        actions, _ = build_optimisation_report(
            policy=POLICY, trends=[t], profits={"P1": good},
            inventory=[{"product_id": "P1", "on_hand_units": 50}],
        )
        self.assertEqual(actions[0].priority, "CRITICAL")
        self.assertIn("Reorder", actions[0].action)

    def test_spike_decay_gets_do_not_extrapolate_advice(self):
        t = self._trend([1, 1, 2, 60, 80, 40, 20, 10, 5, 3, 2, 2, 1, 1])
        good = compute_profit(policy=POLICY, product_id="P1", units=100,
                              gross_revenue=3000.0, cogs_per_unit=3.0)
        actions, _ = build_optimisation_report(
            policy=POLICY, trends=[t], profits={"P1": good},
            inventory=[{"product_id": "P1", "on_hand_units": 500}],
        )
        texts = " ".join(a.action for a in actions)
        self.assertIn("tail", texts)

    def test_fee_drift_is_reported(self):
        t = self._trend([10] * 14)
        good = compute_profit(policy=POLICY, product_id="P1", units=100,
                              gross_revenue=3000.0, cogs_per_unit=3.0)
        _actions, notes = build_optimisation_report(
            policy=POLICY, trends=[t], profits={"P1": good},
            inventory=[], take_rate_pct=24.0,
        )
        self.assertTrue(any("take rate" in n for n in notes))

    def test_poor_conversion_targets_the_listing(self):
        history = [DailyPoint(day=f"2026-07-{i + 1:02d}", units=1, gmv=30,
                              page_views=5000, orders=1) for i in range(14)]
        t = analyse_trend(product_id="P1", title="Product", history=history)
        good = compute_profit(policy=POLICY, product_id="P1", units=14,
                              gross_revenue=420.0, cogs_per_unit=3.0)
        actions, _ = build_optimisation_report(
            policy=POLICY, trends=[t], profits={"P1": good},
            inventory=[{"product_id": "P1", "on_hand_units": 200}],
        )
        self.assertTrue(any("Fix the listing" in a.action for a in actions))


if __name__ == "__main__":
    unittest.main(verbosity=2)
