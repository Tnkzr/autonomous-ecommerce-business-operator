"""Amazon SP-API integration tests, run entirely offline.

Every request the operator can make is exercised here against scripted
responses shaped like Amazon's real ones. The tests that matter most are, as
elsewhere, the ones asserting refusal and correct failure: that a throttle is
retried rather than dropped, that a fee error is not papered over with an
estimate, that a write still needs authorisation, and that a marketplace
mismatch is reported instead of surfacing as "no sales".
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.amazon import (
    AmazonConnector,
    LWACredentials,
    RateLimited,
    SPAPIClient,
    SPAPIError,
    TokenBucket,
    TokenProvider,
    Transport,
    UnknownMarketplace,
    resolve,
)
from connectors.base import ConnectorNotConfigured, WriteNotPermitted
from tests.fakes import (
    CATALOG_SEARCH,
    FEES_ESTIMATE,
    INVENTORY_SUMMARIES,
    ITEM_OFFERS,
    LISTING_ITEM,
    ORDERS_PAGE_1,
    ORDERS_PAGE_2,
    PARTICIPATIONS,
    PATCH_ACCEPTED,
    FakeClock,
    FakeTokenProvider,
    ScriptedSender,
    err,
    ok,
)

LIVE_ENV = {
    "AMZ_LWA_CLIENT_ID": "amzn1.application-oa2-client.test",
    "AMZ_LWA_CLIENT_SECRET": "secret",
    "AMZ_REFRESH_TOKEN": "Atzr|refresh",
    "AMZ_SELLER_ID": "A1SELLER",
    "AMZ_MARKETPLACE_ID": "ATVPDKIKX0DER",
}


def build_client(script: list) -> tuple[SPAPIClient, ScriptedSender, FakeClock]:
    sender = ScriptedSender(script)
    clock = FakeClock()
    transport = Transport(
        endpoint="https://sellingpartnerapi-na.amazon.com",
        token_provider=FakeTokenProvider(),
        send=sender, sleep=clock.sleep, monotonic=clock.monotonic,
    )
    client = SPAPIClient(transport, marketplace_id="ATVPDKIKX0DER", seller_id="A1SELLER")
    return client, sender, clock


class TestRegions(unittest.TestCase):
    def test_resolves_us_to_na(self):
        endpoint, country, currency = resolve("ATVPDKIKX0DER")
        self.assertIn("na", endpoint)
        self.assertEqual(country, "US")
        self.assertEqual(currency, "USD")

    def test_resolves_eu_and_fe(self):
        self.assertIn("eu", resolve("A1F83G8C2ARO7P")[0])   # UK
        self.assertIn("fe", resolve("A1VC38T7YXB528")[0])   # JP

    def test_unknown_marketplace_raises_not_defaults(self):
        # Defaulting to NA would authenticate fine and return empty data,
        # which reads as "no sales" rather than "wrong region".
        with self.assertRaises(UnknownMarketplace):
            resolve("NOTAREALID")

    def test_sandbox_endpoint_is_distinct(self):
        self.assertNotEqual(resolve("ATVPDKIKX0DER")[0],
                            resolve("ATVPDKIKX0DER", sandbox=True)[0])


class TestAuth(unittest.TestCase):
    def test_token_is_cached_between_calls(self):
        calls = []

        def fake_post(url, payload, timeout):
            calls.append(payload)
            return '{"access_token": "Atza|tok", "expires_in": 3600}'

        p = TokenProvider(LWACredentials("id", "secret", "refresh"), http_post=fake_post)
        self.assertEqual(p.access_token(), "Atza|tok")
        self.assertEqual(p.access_token(), "Atza|tok")
        self.assertEqual(len(calls), 1, "Second call must hit the cache, not LWA.")

    def test_refresh_grant_shape(self):
        captured = {}

        def fake_post(url, payload, timeout):
            captured.update(payload)
            return '{"access_token": "t", "expires_in": 3600}'

        TokenProvider(LWACredentials("id", "sec", "ref"), http_post=fake_post).access_token()
        self.assertEqual(captured["grant_type"], "refresh_token")
        self.assertEqual(captured["refresh_token"], "ref")

    def test_invalidate_forces_new_exchange(self):
        calls = []

        def fake_post(url, payload, timeout):
            calls.append(1)
            return '{"access_token": "t", "expires_in": 3600}'

        p = TokenProvider(LWACredentials("id", "sec", "ref"), http_post=fake_post)
        p.access_token()
        p.invalidate()
        p.access_token()
        self.assertEqual(len(calls), 2)

    def test_incomplete_credentials_raise(self):
        p = TokenProvider(LWACredentials("id", "", "ref"), http_post=lambda *a: "{}")
        with self.assertRaises(Exception) as ctx:
            p.access_token()
        self.assertIn("AMZ_LWA_CLIENT_SECRET", str(ctx.exception))

    def test_secrets_are_not_in_repr(self):
        creds = LWACredentials("amzn1.app.client", "super-secret", "Atzr|refresh-token")
        text = repr(creds)
        self.assertNotIn("super-secret", text)
        self.assertNotIn("Atzr|refresh-token", text)


class TestTokenBucket(unittest.TestCase):
    def test_burst_then_throttle(self):
        clock = FakeClock()
        bucket = TokenBucket(rate=1.0, burst=3, monotonic=clock.monotonic)
        for _ in range(3):
            bucket.acquire(sleep=clock.sleep)
        self.assertEqual(clock.total, 0.0, "Burst capacity must not sleep.")
        bucket.acquire(sleep=clock.sleep)
        self.assertGreater(clock.total, 0.0, "Beyond burst, it must wait.")

    def test_adopts_reported_limit(self):
        bucket = TokenBucket(rate=0.5, burst=1)
        bucket.update_limit(5.0)
        self.assertEqual(bucket.rate, 5.0)


class TestTransportRetry(unittest.TestCase):
    def test_throttle_is_retried_then_succeeds(self):
        client, sender, clock = build_client([
            err(429, "QuotaExceeded", "Too many requests"),
            ok(PARTICIPATIONS),
        ])
        result = client.get_marketplace_participations()
        self.assertEqual(len(sender.requests), 2)
        self.assertGreater(clock.total, 0, "A 429 must back off before retrying.")
        self.assertEqual(result[0]["marketplace"]["id"], "ATVPDKIKX0DER")

    def test_retry_after_header_is_honoured(self):
        client, _sender, clock = build_client([
            err(429, "QuotaExceeded", "slow down", {"Retry-After": "7"}),
            ok(PARTICIPATIONS),
        ])
        client.get_marketplace_participations()
        self.assertIn(7.0, clock.slept,
                      "Amazon's Retry-After must win over our own backoff curve.")

    def test_persistent_throttle_raises_rate_limited(self):
        client, _s, _c = build_client([err(429, "QuotaExceeded", "no")] * 6)
        with self.assertRaises(RateLimited):
            client.get_marketplace_participations()

    def test_403_reauthenticates_once(self):
        sender = ScriptedSender([err(403, "Unauthorized", "token expired"),
                                 ok(PARTICIPATIONS)])
        tokens = FakeTokenProvider()
        clock = FakeClock()
        transport = Transport(endpoint="https://x", token_provider=tokens,
                              send=sender, sleep=clock.sleep,
                              monotonic=clock.monotonic)
        client = SPAPIClient(transport, marketplace_id="ATVPDKIKX0DER", seller_id="S")
        client.get_marketplace_participations()
        self.assertEqual(tokens.invalidations, 1,
                         "A 403 should drop the cached token before retrying.")

    def test_400_is_not_retried(self):
        client, sender, _c = build_client([
            err(400, "InvalidInput", "bad productType"),
        ])
        with self.assertRaises(SPAPIError):
            client.get_marketplace_participations()
        self.assertEqual(len(sender.requests), 1,
                         "A client error is deterministic; retrying wastes quota.")

    def test_error_message_carries_amazon_code(self):
        client, _s, _c = build_client([err(400, "InvalidInput", "attribute missing")])
        with self.assertRaises(SPAPIError) as ctx:
            client.get_marketplace_participations()
        self.assertIn("InvalidInput", str(ctx.exception))
        self.assertIn("attribute missing", str(ctx.exception))

    def test_server_error_is_retried(self):
        client, sender, _c = build_client([
            (503, {"errors": [{"code": "ServiceUnavailable", "message": "x"}]}, {}),
            ok(PARTICIPATIONS),
        ])
        client.get_marketplace_participations()
        self.assertEqual(len(sender.requests), 2)


class TestCatalog(unittest.TestCase):
    def test_search_maps_results(self):
        client, sender, _c = build_client([ok(CATALOG_SEARCH)])
        items = client.search_catalog_items(keywords=["bamboo drawer organizer"])
        self.assertEqual(len(items), 2)
        self.assertIn("keywords=bamboo", sender.last["url"])
        self.assertIn("marketplaceIds=ATVPDKIKX0DER", sender.last["url"])

    def test_keywords_and_identifiers_are_mutually_exclusive(self):
        client, _s, _c = build_client([])
        with self.assertRaises(ValueError):
            client.search_catalog_items(keywords=["a"], identifiers=["B01"])

    def test_search_needs_some_criteria(self):
        client, _s, _c = build_client([])
        with self.assertRaises(ValueError):
            client.search_catalog_items()

    def test_pagination_follows_next_token(self):
        page1 = dict(CATALOG_SEARCH, pagination={"nextToken": "T2"})
        page2 = dict(CATALOG_SEARCH, pagination={})
        client, sender, _c = build_client([ok(page1), ok(page2)])
        items = client.search_catalog_items(keywords=["x"], max_pages=5)
        self.assertEqual(len(items), 4)
        self.assertIn("pageToken=T2", sender.urls()[1])

    def test_page_cap_is_respected(self):
        endless = dict(CATALOG_SEARCH, pagination={"nextToken": "MORE"})
        client, sender, _c = build_client([ok(endless)] * 10)
        client.search_catalog_items(keywords=["x"], max_pages=3)
        self.assertEqual(len(sender.requests), 3,
                         "An uncapped crawl is how a sync becomes a throttled six-hour job.")


class TestPricingAndFees(unittest.TestCase):
    def test_offers_map_to_repricer_shape(self):
        conn = AmazonConnector()
        client, _s, _c = build_client([ok(ITEM_OFFERS)])
        conn._client = client
        env = conn.fetch_competitor_offers("B08EXAMPLE1")

        self.assertEqual(env.source, "live")
        offers = env.payload["offers"]
        self.assertEqual(len(offers), 3)

        cheap = next(o for o in offers if o["listing_price"] == 19.99)
        self.assertAlmostEqual(cheap["price"], 24.98,
                               msg="Landed price must include shipping, or an MFN "
                                   "rival looks cheaper than they are.")
        self.assertAlmostEqual(cheap["rating"], 3.6, places=1,
                               msg="Feedback % must convert to the 5-point scale.")
        self.assertTrue(any(o["seller"] == "self" for o in offers))

    def test_buybox_suppression_is_surfaced(self):
        payload = {"payload": {"Summary": {}, "Offers": [
            {"SellerId": "A", "ListingPrice": {"Amount": 10.0},
             "Shipping": {"Amount": 0.0}, "IsBuyBoxWinner": False},
        ]}}
        conn = AmazonConnector()
        client, _s, _c = build_client([ok(payload)])
        conn._client = client
        env = conn.fetch_competitor_offers("B0X")
        self.assertTrue(any("suppressed" in w for w in env.warnings))

    def test_fee_estimate_parsed(self):
        client, sender, _c = build_client([ok(FEES_ESTIMATE)])
        fees = client.get_fees_estimate("B08EXAMPLE1", 34.99)
        self.assertEqual(fees.referral_fee, 5.25)
        self.assertEqual(fees.fba_fee, 4.97)
        self.assertEqual(fees.total_fees, 10.22)
        self.assertAlmostEqual(fees.referral_pct, 15.0, places=1)
        self.assertEqual(sender.last["method"], "POST")

    def test_failed_fee_estimate_raises_rather_than_estimating(self):
        # Substituting a guess here would silently corrupt every downstream
        # profit calculation for that product.
        bad = {"payload": {"FeesEstimateResult": {
            "Status": "ClientError",
            "Error": {"Code": "InvalidInput", "Message": "unknown ASIN"},
        }}}
        client, _s, _c = build_client([ok(bad)])
        with self.assertRaises(SPAPIError) as ctx:
            client.get_fees_estimate("BADASIN", 10.0)
        self.assertIn("InvalidInput", str(ctx.exception))

    def test_competitive_pricing_batches_by_twenty(self):
        client, sender, _c = build_client([ok({"payload": []}), ok({"payload": []})])
        client.get_competitive_pricing([f"B{i:09d}" for i in range(25)])
        self.assertEqual(len(sender.requests), 2,
                         "25 ASINs must split into 20 + 5, not one oversized call.")


class TestInventoryAndOrders(unittest.TestCase):
    def test_inventory_sums_inbound_states(self):
        conn = AmazonConnector()
        client, _s, _c = build_client([ok(INVENTORY_SUMMARIES)])
        conn._client = client
        env = conn.fetch_inventory()
        row = env.payload[0]
        self.assertEqual(row["on_hand_units"], 240)
        self.assertEqual(row["inbound_units"], 120,
                         "working + shipped + receiving must all count as inbound.")
        self.assertEqual(row["unfulfillable_units"], 13)

    def test_unfulfillable_stock_warns(self):
        conn = AmazonConnector()
        client, _s, _c = build_client([ok(INVENTORY_SUMMARIES)])
        conn._client = client
        env = conn.fetch_inventory()
        self.assertTrue(any("unfulfillable" in w for w in env.warnings))

    def test_orders_paginate_and_flag_pending(self):
        conn = AmazonConnector()
        client, sender, _c = build_client([ok(ORDERS_PAGE_1), ok(ORDERS_PAGE_2)])
        conn._client = client
        env = conn.fetch_orders(since="2026-07-27")

        self.assertEqual(len(env.payload), 3)
        self.assertIn("NextToken=PAGE2TOKEN", sender.urls()[1])
        self.assertTrue(any("no OrderTotal" in w for w in env.warnings),
                        "A pending order with no total must not be summed as $0 revenue.")

    def test_since_must_be_parseable(self):
        conn = AmazonConnector()
        conn._client = object()
        with self.assertRaises(ValueError):
            conn.fetch_orders(since="last tuesday")

    def test_orders_query_uses_iso_utc(self):
        conn = AmazonConnector()
        client, sender, _c = build_client([ok({"payload": {"Orders": []}})])
        conn._client = client
        conn.fetch_orders(since="2026-07-01")
        self.assertIn("CreatedAfter=2026-07-01T00%3A00%3A00Z", sender.last["url"])


class TestWrites(unittest.TestCase):
    def test_price_update_blocked_without_write_permission(self):
        conn = AmazonConnector(allow_writes=False)
        with self.assertRaises(WriteNotPermitted):
            conn.update_price("SKU-1", 29.99)

    def test_price_update_patches_purchasable_offer(self):
        conn = AmazonConnector(allow_writes=True)
        client, sender, _c = build_client([ok(LISTING_ITEM), ok(PATCH_ACCEPTED)])
        conn._client = client
        with mock.patch.dict(os.environ, LIVE_ENV):
            env = conn.update_price("BAMBOO-ORG-01", 32.49)

        patch_req = sender.requests[-1]
        self.assertEqual(patch_req["method"], "PATCH")
        path = patch_req["body"]["patches"][0]["path"]
        self.assertEqual(path, "/attributes/purchasable_offer",
                         "list_price is the manufacturer's price; patching it does "
                         "not change what customers pay.")
        value = patch_req["body"]["patches"][0]["value"][0]
        self.assertEqual(value["our_price"][0]["schedule"][0]["value_with_tax"], 32.49)
        self.assertEqual(env.payload["status"], "ACCEPTED")

    def test_product_type_is_read_not_guessed(self):
        conn = AmazonConnector(allow_writes=True)
        client, sender, _c = build_client([ok(LISTING_ITEM), ok(PATCH_ACCEPTED)])
        conn._client = client
        with mock.patch.dict(os.environ, LIVE_ENV):
            conn.update_price("BAMBOO-ORG-01", 30.0)
        self.assertEqual(sender.requests[0]["method"], "GET")
        self.assertEqual(sender.requests[-1]["body"]["productType"], "HOME_ORGANIZER")

    def test_missing_product_type_raises_rather_than_defaulting(self):
        conn = AmazonConnector(allow_writes=True)
        client, _s, _c = build_client([ok({"sku": "X", "summaries": []})])
        conn._client = client
        with mock.patch.dict(os.environ, LIVE_ENV):
            with self.assertRaises(SPAPIError):
                conn.update_price("X", 10.0)

    def test_accepted_status_warns_it_is_not_yet_live(self):
        conn = AmazonConnector(allow_writes=True)
        client, _s, _c = build_client([ok(LISTING_ITEM), ok(PATCH_ACCEPTED)])
        conn._client = client
        with mock.patch.dict(os.environ, LIVE_ENV):
            env = conn.update_price("BAMBOO-ORG-01", 31.0)
        self.assertTrue(any("not that the change is live" in w for w in env.warnings))

    def test_publish_listing_requires_full_payload(self):
        conn = AmazonConnector(allow_writes=True)
        conn._client = object()
        with mock.patch.dict(os.environ, LIVE_ENV):
            with self.assertRaises(ValueError):
                conn.publish_listing({"sku": "X"})

    def test_publish_listing_blocked_without_permission(self):
        conn = AmazonConnector(allow_writes=False)
        with self.assertRaises(WriteNotPermitted):
            conn.publish_listing({"sku": "X", "product_type": "T", "attributes": {}})

    def test_put_listing_sends_expected_body(self):
        conn = AmazonConnector(allow_writes=True)
        client, sender, _c = build_client([ok(PATCH_ACCEPTED)])
        conn._client = client
        with mock.patch.dict(os.environ, LIVE_ENV):
            conn.publish_listing({
                "sku": "NEW-1", "product_type": "HOME_ORGANIZER",
                "attributes": {"item_name": [{"value": "Test"}]},
            })
        self.assertEqual(sender.last["method"], "PUT")
        self.assertEqual(sender.last["body"]["productType"], "HOME_ORGANIZER")


class TestReports(unittest.TestCase):
    def test_report_lifecycle_polls_until_done(self):
        client, sender, clock = build_client([
            ok({"reportId": "R1"}),
            ok({"processingStatus": "IN_PROGRESS"}),
            ok({"processingStatus": "DONE", "reportDocumentId": "D1"}),
            ok({"url": "https://s3.example/doc"}),
        ])
        rid = client.create_report("GET_MERCHANT_LISTINGS_ALL_DATA")
        self.assertEqual(rid, "R1")

        meta = client.wait_for_report(rid, sleep=clock.sleep,
                                      monotonic=clock.monotonic)
        self.assertEqual(meta["reportDocumentId"], "D1")
        self.assertGreater(clock.total, 0, "An IN_PROGRESS report must be waited on.")

        text = client.get_report_document(
            meta["reportDocumentId"],
            fetch=lambda url: b"sku\tprice\nA1\t10.00\nA2\t20.00\n",
        )
        rows = client.parse_tab_report(text)
        self.assertEqual(rows[0]["sku"], "A1")
        self.assertEqual(rows[1]["price"], "20.00")

    def test_report_timeout_raises(self):
        client, _s, clock = build_client([ok({"processingStatus": "IN_PROGRESS"})] * 40)
        with self.assertRaises(SPAPIError) as ctx:
            client.wait_for_report("R1", timeout_seconds=60, poll_seconds=30,
                                   sleep=clock.sleep, monotonic=clock.monotonic)
        self.assertIn("still IN_PROGRESS", str(ctx.exception))

    def test_fatal_report_is_terminal(self):
        client, _s, clock = build_client([ok({"processingStatus": "FATAL"})])
        with self.assertRaises(SPAPIError) as ctx:
            client.wait_for_report("R1", sleep=clock.sleep,
                                   monotonic=clock.monotonic)
        self.assertIn("terminal", str(ctx.exception))

    def test_gzip_document_is_decompressed(self):
        import gzip
        client, _s, _c = build_client([ok({"url": "https://s3/x",
                                           "compressionAlgorithm": "GZIP"})])
        text = client.get_report_document(
            "D1", fetch=lambda url: gzip.compress(b"sku\tqty\nA\t5\n"),
        )
        self.assertIn("sku", text)
        self.assertEqual(client.parse_tab_report(text)[0]["qty"], "5")

    def test_report_document_url_is_fetched_without_auth_header(self):
        # The URL is a pre-signed S3 link; sending the SP-API token there would
        # leak a live credential to a third-party host.
        seen = {}

        def fetcher(url):
            seen["url"] = url
            return b"a\tb\n1\t2\n"

        client, _s, _c = build_client([ok({"url": "https://s3.example/presigned"})])
        client.get_report_document("D1", fetch=fetcher)
        self.assertEqual(seen["url"], "https://s3.example/presigned")


class TestListingsEnumeration(unittest.TestCase):
    def test_uses_merchant_listings_report_not_fba(self):
        # The FBA report omits merchant-fulfilled SKUs, which would hide part
        # of the catalogue from every downstream engine.
        conn = AmazonConnector()
        client, sender, clock = build_client([
            ok({"reportId": "R1"}),
            ok({"processingStatus": "DONE", "reportDocumentId": "D1"}),
            ok({"url": "https://s3/x"}),
        ])
        conn._client = client
        client.get_report_document = lambda doc_id, fetch=None: (
            "seller-sku\tasin1\titem-name\tprice\tquantity\tstatus\tfulfillment-channel\n"
            "SKU-A\tB01\tWidget\t19.99\t10\tActive\tDEFAULT\n"
            "SKU-B\tB02\tGadget\t29.99\t0\tIncomplete\tAMAZON_NA\n"
        )
        env = conn.fetch_listings()

        body = sender.requests[0]["body"]
        self.assertEqual(body["reportType"], "GET_MERCHANT_LISTINGS_ALL_DATA")
        self.assertEqual(env.payload[0]["sku"], "SKU-A")
        self.assertEqual(env.payload[0]["asin"], "B01")
        self.assertEqual(env.payload[0]["price"], 19.99)
        self.assertTrue(any("not Active" in w for w in env.warnings))


class TestConnectionVerification(unittest.TestCase):
    def test_missing_credentials_reported_not_raised(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            result = AmazonConnector().verify_connection()
        self.assertFalse(result["ok"])
        self.assertIn("Missing credentials", result["detail"])

    def test_successful_verification(self):
        conn = AmazonConnector()
        client, _s, _c = build_client([ok(PARTICIPATIONS)])
        conn._client = client
        with mock.patch.dict(os.environ, LIVE_ENV):
            result = conn.verify_connection()
        self.assertTrue(result["ok"])
        self.assertEqual(result["country"], "US")
        self.assertIn("ATVPDKIKX0DER", result["seller_marketplaces"])

    def test_marketplace_mismatch_is_caught(self):
        # Authenticating against a marketplace the seller does not sell in
        # returns empty data everywhere, which reads as a dead business.
        conn = AmazonConnector()
        client, _s, _c = build_client([ok(PARTICIPATIONS)])
        conn._client = client
        env = dict(LIVE_ENV, AMZ_MARKETPLACE_ID="A1F83G8C2ARO7P")  # UK
        with mock.patch.dict(os.environ, env):
            result = conn.verify_connection()
        self.assertFalse(result["ok"])
        self.assertIn("not among this seller's marketplaces", result["detail"])

    def test_auth_failure_is_diagnosed_not_crashed(self):
        conn = AmazonConnector()
        client, _s, _c = build_client([err(403, "Unauthorized", "bad token")] * 6)
        conn._client = client
        with mock.patch.dict(os.environ, LIVE_ENV):
            result = conn.verify_connection()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 403)
        self.assertIn("authorised", result["detail"])

    def test_bad_marketplace_id_diagnosed(self):
        conn = AmazonConnector()
        with mock.patch.dict(os.environ, dict(LIVE_ENV, AMZ_MARKETPLACE_ID="XYZ")):
            result = conn.verify_connection()
        self.assertFalse(result["ok"])
        self.assertIn("not recognised", result["detail"])


class TestUnavailableSurfaces(unittest.TestCase):
    def test_reviews_refuse_rather_than_return_empty(self):
        # An empty review list would report a badly-reviewed product as clean.
        conn = AmazonConnector()
        with mock.patch.dict(os.environ, LIVE_ENV):
            with self.assertRaises(ConnectorNotConfigured) as ctx:
                conn.fetch_reviews("SKU-1")
        self.assertIn("Conditions of Use", str(ctx.exception))

    def test_ads_direct_to_the_right_api(self):
        conn = AmazonConnector()
        with mock.patch.dict(os.environ, LIVE_ENV):
            with self.assertRaises(ConnectorNotConfigured) as ctx:
                conn.fetch_ad_performance(since="2026-07-01")
        self.assertIn("Amazon Ads API", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
