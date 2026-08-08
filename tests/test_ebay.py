"""eBay connector tests.

Three things here are unlike the other connectors and carry most of the
assertions: two token kinds that must not be swapped, a daily quota that
refuses rather than waits, and a refresh token that dies on a calendar rather
than on an error. Plus the one thing eBay can do that the others cannot —
return competitor prices legally.
"""

from __future__ import annotations

import os
import unittest
from datetime import date
from unittest import mock

from connectors.base import ConnectorNotConfigured, WriteNotPermitted
from connectors.credentials import CredentialsUnavailable
from connectors.ebay import (
    APPLICATION,
    USER,
    DailyQuota,
    EbayAPIError,
    EbayAuthError,
    EbayClient,
    EbayConnector,
    EbayCredentials,
    EbayQuotaExhausted,
    StaticCredentials,
    TokenProvider,
    Transport,
    UnavailableCredentials,
)
from tests.fakes import (
    EB_STANDARDS,
    EbaySender,
    FakeClock,
    FakeEbayTokens,
    eb_browse_item,
    eb_error,
    eb_ok,
    eb_order,
    eb_token,
    eb_token_error,
)

CREDS = EbayCredentials("client-id", "client-secret", "refresh-token",
                        environment="production", marketplace_id="EBAY_US",
                        granted_at="2026-01-01")


class _Authorisation:
    def __init__(self, permitted: bool, reason: str = "") -> None:
        self.permitted = permitted
        self.reason = reason

    def explain(self) -> str:
        return self.reason


def build_transport(script: list, *, credentials: EbayCredentials = CREDS):
    sender = EbaySender(script)
    clock = FakeClock()
    tokens = FakeEbayTokens()
    transport = Transport(credentials=credentials, token_provider=tokens,
                          send=sender, sleep=clock.sleep, clock=lambda: 0.0)
    return transport, sender, tokens


def build_client(script: list):
    transport, sender, tokens = build_transport(script)
    return EbayClient(transport), sender, tokens


def build_connector(script: list, *, allow_writes: bool = False):
    transport, sender, tokens = build_transport(script)
    conn = EbayConnector(allow_writes=allow_writes, transport=transport,
                         credentials=StaticCredentials(CREDS))
    return conn, sender, tokens


# ---------------------------------------------------------------------------
class TestCredentials(unittest.TestCase):
    def test_secrets_absent_from_repr(self):
        creds = EbayCredentials("id", "SUPERSECRET", "REFRESHSECRET")
        self.assertNotIn("SUPERSECRET", repr(creds))
        self.assertNotIn("REFRESHSECRET", repr(creds))

    def test_unknown_environment_is_refused(self):
        with self.assertRaises(ValueError):
            EbayCredentials("id", "secret", "refresh", environment="staging")

    def test_sandbox_uses_different_hosts(self):
        sandbox = EbayCredentials("id", "secret", "refresh", environment="sandbox")
        self.assertIn("sandbox", sandbox.api_base)
        self.assertIn("sandbox", sandbox.auth_url)
        self.assertTrue(sandbox.is_sandbox)

    def test_marketplace_is_normalised(self):
        self.assertEqual(
            EbayCredentials("i", "s", "r", marketplace_id="ebay_gb").marketplace_id,
            "EBAY_GB")

    def test_setup_remedy_names_the_console_fields(self):
        result = EbayConnector(credentials=UnavailableCredentials()).verify_connection()
        self.assertFalse(result["ok"])
        self.assertIn("App ID (Client ID)", result["remedy"])
        self.assertIn("Cert ID (Client Secret)", result["remedy"])

    def test_env_variables_are_named_in_status(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            status = EbayConnector().status()
        self.assertIn("EBAY_CLIENT_ID", status["missing_env"])
        self.assertNotIn("client_id", status["missing_env"])


class TestTwoTokenKinds(unittest.TestCase):
    """Application and user tokens are not interchangeable."""

    def _provider(self, script):
        sent = []

        def send(*, url, headers, body):
            sent.append({"url": url, "body": body, "headers": headers})
            return script.pop(0)

        return TokenProvider(CREDS, send=send, clock=lambda: 1000.0), sent

    def test_user_token_uses_the_refresh_grant(self):
        provider, sent = self._provider([eb_token()])
        provider.access_token(USER)
        self.assertIn("grant_type=refresh_token", sent[0]["body"])
        self.assertIn("refresh_token=refresh-token", sent[0]["body"])

    def test_application_token_uses_client_credentials(self):
        provider, sent = self._provider([eb_token()])
        provider.access_token(APPLICATION)
        self.assertIn("grant_type=client_credentials", sent[0]["body"])
        self.assertNotIn("refresh_token", sent[0]["body"])

    def test_the_two_are_cached_separately(self):
        provider, sent = self._provider([eb_token("user-tok"), eb_token("app-tok")])
        self.assertEqual(provider.access_token(USER), "user-tok")
        self.assertEqual(provider.access_token(APPLICATION), "app-tok")
        # Both cached now; no further mints.
        self.assertEqual(provider.access_token(USER), "user-tok")
        self.assertEqual(provider.access_token(APPLICATION), "app-tok")
        self.assertEqual(len(sent), 2)

    def test_unknown_kind_is_refused(self):
        provider, _sent = self._provider([])
        with self.assertRaises(ValueError) as ctx:
            provider.access_token("seller")
        self.assertIn("Sell APIs", str(ctx.exception))

    def test_token_is_retired_before_it_expires(self):
        # Reactive refresh means every expiry costs a failed call.
        now = [1000.0]
        script = [eb_token("first", expires_in=200), eb_token("second", expires_in=200)]

        def send(**_kw):
            return script.pop(0)

        provider = TokenProvider(CREDS, send=send, clock=lambda: now[0])
        self.assertEqual(provider.access_token(USER), "first")
        now[0] += 100          # 100s left, inside the 120s skew
        self.assertEqual(provider.access_token(USER), "second")

    def test_missing_expiry_does_not_cache_forever(self):
        provider, _sent = self._provider([(200, {"access_token": "t"})])
        provider.access_token(USER)
        self.assertLess(provider._tokens[USER].expires_at - 1000.0, 7201)


class TestAuthFailures(unittest.TestCase):
    def _provider(self, response):
        return TokenProvider(CREDS, send=lambda **_kw: response, clock=lambda: 0.0)

    def test_invalid_grant_is_fatal_and_explains_the_18_month_life(self):
        provider = self._provider(eb_token_error("invalid_grant", "token expired"))
        with self.assertRaises(EbayAuthError) as ctx:
            provider.access_token(USER)
        self.assertFalse(ctx.exception.retryable)
        self.assertIn("18 months", str(ctx.exception))
        self.assertIn("retrying will not help", str(ctx.exception).lower())

    def test_invalid_client_points_at_the_sandbox_mixup(self):
        provider = self._provider(eb_token_error("invalid_client", "bad creds"))
        with self.assertRaises(EbayAuthError) as ctx:
            provider.access_token(USER)
        self.assertIn("sandbox keyset against a production host",
                      str(ctx.exception))

    def test_server_error_is_retryable(self):
        provider = self._provider(eb_token_error("server_error", "oops", status=503))
        with self.assertRaises(EbayAuthError) as ctx:
            provider.access_token(USER)
        self.assertTrue(ctx.exception.retryable)


class TestRefreshTokenClock(unittest.TestCase):
    """The failure that arrives on a calendar, not on an error."""

    def _provider(self, granted_at: str):
        creds = EbayCredentials("i", "s", "r", granted_at=granted_at)
        return TokenProvider(creds, send=lambda **_kw: eb_token(), clock=lambda: 0.0)

    def test_fresh_grant_is_silent(self):
        provider = self._provider("2026-01-01")
        self.assertEqual(provider.grant_age_warning(today=date(2026, 3, 1)), "")

    def test_warns_around_seventeen_months(self):
        provider = self._provider("2025-01-01")
        warning = provider.grant_age_warning(today=date(2026, 6, 15))
        self.assertIn("cannot be renewed in software", warning)

    def test_urgent_inside_the_last_fortnight(self):
        provider = self._provider("2025-01-01")
        warning = provider.grant_age_warning(today=date(2026, 6, 25))
        self.assertIn("schedule it now", warning)

    def test_expired_grant_is_stated_plainly(self):
        provider = self._provider("2024-01-01")
        self.assertIn("past its", provider.grant_age_warning(today=date(2026, 6, 1)))

    def test_unknown_grant_date_is_itself_reported(self):
        # A silent unknown here becomes a surprise outage in 18 months.
        provider = self._provider("")
        self.assertIn("cannot be tracked", provider.grant_age_warning())

    def test_malformed_grant_date_is_reported(self):
        provider = self._provider("last January")
        self.assertIn("not a valid date", provider.grant_age_warning())


# ---------------------------------------------------------------------------
class TestDailyQuota(unittest.TestCase):
    """A daily budget is spent, not borrowed. Waiting does not refill it."""

    def test_refuses_rather_than_blocking(self):
        quota = DailyQuota(default_limit=10, clock=lambda: 0.0)
        for _ in range(8):          # soft limit is 80% of 10
            quota.spend("browse")
        with self.assertRaises(EbayQuotaExhausted) as ctx:
            quota.spend("browse")
        self.assertIn("midnight UTC", str(ctx.exception))
        self.assertIn("waiting does not restore", str(ctx.exception))

    def test_a_reserve_is_held_back_for_urgent_work(self):
        quota = DailyQuota(default_limit=10, clock=lambda: 0.0)
        for _ in range(8):
            quota.spend("browse")
        # Soft limit reached, but the reserve is still there.
        quota.spend("browse", allow_reserve=True)
        quota.spend("browse", allow_reserve=True)
        with self.assertRaises(EbayQuotaExhausted):
            quota.spend("browse", allow_reserve=True)

    def test_apis_are_metered_separately(self):
        # Exhausting Browse must not stop Fulfillment.
        quota = DailyQuota(default_limit=10, clock=lambda: 0.0)
        for _ in range(8):
            quota.spend("browse")
        quota.spend("fulfillment")   # unaffected

    def test_budget_resets_on_a_new_utc_day(self):
        now = [0.0]
        quota = DailyQuota(default_limit=10, clock=lambda: now[0])
        for _ in range(8):
            quota.spend("browse")
        now[0] += 86400
        quota.spend("browse")        # new day, new budget
        self.assertEqual(quota.snapshot()["browse"]["used"], 1)

    def test_real_limits_are_adopted_over_defaults(self):
        quota = DailyQuota(default_limit=10, clock=lambda: 0.0)
        quota.adopt("browse", limit=5000, used=12)
        snapshot = quota.snapshot()["browse"]
        self.assertEqual(snapshot["limit"], 5000)
        self.assertEqual(snapshot["used"], 12)
        self.assertTrue(snapshot["limit_adopted_from_api"])

    def test_transport_refuses_without_sending(self):
        transport, sender, _tokens = build_transport([])
        transport.quota.adopt("fulfillment", limit=1, used=1)
        with self.assertRaises(EbayQuotaExhausted):
            transport.request(method="GET", path="/sell/fulfillment/v1/order",
                              api="fulfillment")
        self.assertEqual(sender.requests, [])
        self.assertEqual(transport.stats()["refused_on_quota"], 1)


class TestTransport(unittest.TestCase):
    def test_marketplace_header_is_always_sent(self):
        # Omitting it does not fail — it silently returns another country's data.
        transport, sender, _t = build_transport([eb_ok({"orders": []})])
        transport.request(method="GET", path="/sell/fulfillment/v1/order",
                          api="fulfillment")
        self.assertEqual(sender.last["marketplace"], "EBAY_US")

    def test_token_kind_is_explicit_per_call(self):
        transport, _s, tokens = build_transport(
            [eb_ok({"itemSummaries": []}), eb_ok({"orders": []})])
        transport.request(method="GET", path="/buy/browse/v1/item_summary/search",
                          api="browse", token_kind=APPLICATION)
        transport.request(method="GET", path="/sell/fulfillment/v1/order",
                          api="fulfillment", token_kind=USER)
        self.assertEqual(tokens.requested, [APPLICATION, USER])

    def test_401_refreshes_once_then_gives_up(self):
        transport, sender, tokens = build_transport([
            eb_error(401, 1001, "Invalid access token"),
            eb_error(401, 1001, "Invalid access token"),
        ])
        with self.assertRaises(EbayAPIError):
            transport.request(method="GET", path="/sell/fulfillment/v1/order",
                              api="fulfillment")
        self.assertEqual(tokens.invalidations, [USER])
        self.assertEqual(len(sender.requests), 2)

    def test_401_then_success(self):
        transport, _s, _t = build_transport([
            eb_error(401, 1001, "Invalid access token"),
            eb_ok({"orders": []}),
        ])
        self.assertEqual(
            transport.request(method="GET", path="/sell/fulfillment/v1/order",
                              api="fulfillment"), {"orders": []})

    def test_error_id_hint_is_attached(self):
        transport, _s, _t = build_transport([
            eb_error(403, 1002, "Insufficient permissions")])
        with self.assertRaises(EbayAPIError) as ctx:
            transport.request(method="GET", path="/sell/finances/v1/transaction",
                              api="finances")
        self.assertIn("fixed at consent", str(ctx.exception))
        self.assertEqual(ctx.exception.error_id, 1002)

    def test_user_error_is_not_retried(self):
        transport, sender, _t = build_transport([
            eb_error(400, 25002, "Invalid request payload")])
        with self.assertRaises(EbayAPIError):
            transport.request(method="POST", path="/sell/inventory/v1/offer",
                              api="inventory", body={"sku": "X"})
        self.assertEqual(len(sender.requests), 1)

    def test_429_becomes_a_quota_error(self):
        transport, _s, _t = build_transport([
            eb_error(429, 2001, "Too many requests")])
        with self.assertRaises(EbayQuotaExhausted):
            transport.request(method="GET", path="/buy/browse/v1/item_summary/search",
                              api="browse", token_kind=APPLICATION)

    def test_error_parameters_are_surfaced(self):
        transport, _s, _t = build_transport([
            eb_error(400, 25710, "Not found",
                     parameters=[{"name": "sku", "value": "MISSING-1"}])])
        with self.assertRaises(EbayAPIError) as ctx:
            transport.request(method="GET", path="/sell/inventory/v1/offer",
                              api="inventory")
        self.assertIn("sku=MISSING-1", str(ctx.exception))

    def test_2xx_is_trusted(self):
        # Unlike TikTok and Shopify, a 200 here genuinely means success.
        transport, _s, _t = build_transport([eb_ok({"orders": [], "total": 0})])
        self.assertEqual(
            transport.request(method="GET", path="/sell/fulfillment/v1/order",
                              api="fulfillment"), {"orders": [], "total": 0})


class TestPagination(unittest.TestCase):
    def test_all_pages_are_walked(self):
        client, sender, _t = build_client([
            eb_ok({"orders": [eb_order("1"), eb_order("2")], "total": 3,
                   "next": "..."}),
            eb_ok({"orders": [eb_order("3")], "total": 3}),
        ])
        self.assertEqual(len(client.orders(since="2026-07-01")), 3)
        self.assertIn("offset=2", sender.urls()[1])

    def test_stops_when_total_is_reached(self):
        client, sender, _t = build_client([
            eb_ok({"orders": [eb_order("1")], "total": 1})])
        client.orders(since="2026-07-01")
        self.assertEqual(len(sender.requests), 1)

    def test_since_is_converted_to_an_ebay_filter(self):
        # A bare date is rejected by eBay without the time component.
        client, sender, _t = build_client([eb_ok({"orders": [], "total": 0})])
        client.orders(since="2026-07-01")
        self.assertIn("creationdate", sender.last["url"])
        self.assertIn("2026-07-01T00%3A00%3A00.000Z", sender.last["url"])


# ---------------------------------------------------------------------------
class TestCompetitorOffers(unittest.TestCase):
    """The question only eBay can answer legally."""

    def test_browse_returns_rival_prices(self):
        conn, _s, _t = build_connector([eb_ok({"itemSummaries": [
            eb_browse_item("v1|1|0", "19.99", seller="cheap_seller"),
            eb_browse_item("v1|2|0", "27.50", seller="premium_seller"),
        ]})])
        env = conn.fetch_competitor_offers("slicker brush")
        self.assertEqual(len(env.payload), 2)
        self.assertEqual(env.payload[0]["price"], 19.99)
        self.assertEqual(env.payload[0]["seller"], "cheap_seller")

    def test_uses_the_application_token_not_the_sellers(self):
        # Browse is public data and does not want seller credentials.
        conn, _s, tokens = build_connector([eb_ok({"itemSummaries": []})])
        conn.fetch_competitor_offers("brush")
        self.assertEqual(tokens.requested, [APPLICATION])

    def test_shipping_is_added_into_a_landed_price(self):
        # A $15 item with $9 shipping does not undercut a $20 item with free
        # shipping, and a repricer comparing headline prices would think it did.
        conn, _s, _t = build_connector([eb_ok({"itemSummaries": [
            eb_browse_item("v1|1|0", "15.00", shipping="9.00")]})])
        offer = conn.fetch_competitor_offers("brush").payload[0]
        self.assertEqual(offer["price"], 15.00)
        self.assertEqual(offer["landed_price"], 24.00)

    def test_results_are_labelled_a_sample_not_the_market(self):
        conn, _s, _t = build_connector([eb_ok({"itemSummaries": [
            eb_browse_item("v1|1|0", "19.99")]})])
        env = conn.fetch_competitor_offers("brush")
        self.assertTrue(any("sample" in w for w in env.warnings))
        self.assertTrue(any("true market floor" in w for w in env.warnings))

    def test_empty_results_are_not_reported_as_an_empty_category(self):
        conn, _s, _t = build_connector([eb_ok({"itemSummaries": []})])
        env = conn.fetch_competitor_offers("extremely specific query")
        self.assertEqual(env.payload, [])
        self.assertTrue(any("not proof the category is empty" in w
                            for w in env.warnings))

    def test_unpriced_items_are_dropped(self):
        conn, _s, _t = build_connector([eb_ok({"itemSummaries": [
            {"itemId": "v1|9|0", "title": "No price"},
            eb_browse_item("v1|1|0", "19.99"),
        ]})])
        self.assertEqual(len(conn.fetch_competitor_offers("brush").payload), 1)


class TestOrders(unittest.TestCase):
    def test_unpaid_orders_are_returned_but_not_revenue(self):
        conn, _s, _t = build_connector([eb_ok({
            "orders": [eb_order("1", payment="PAID"),
                       eb_order("2", payment="PENDING")], "total": 2})])
        env = conn.fetch_orders(since="2026-07-01")
        self.assertEqual(len(env.payload), 2)
        self.assertTrue(env.payload[0].is_revenue)
        self.assertFalse(env.payload[1].is_revenue)
        self.assertTrue(any("overstate sales and conversion" in w
                            for w in env.warnings))

    def test_cancelled_orders_are_flagged(self):
        conn, _s, _t = build_connector([eb_ok({
            "orders": [eb_order("1", cancelled=True)], "total": 1})])
        env = conn.fetch_orders(since="2026-07-01")
        self.assertTrue(env.payload[0].cancelled)
        self.assertFalse(env.payload[0].is_revenue)

    def test_units_come_from_line_items(self):
        conn, _s, _t = build_connector([eb_ok({
            "orders": [eb_order("1", quantity=3)], "total": 1})])
        self.assertEqual(conn.fetch_orders(since="2026-07-01").payload[0].units, 3)


class TestUnpublishedFirstListing(unittest.TestCase):
    """Item and offer are invisible; only publishing is gated."""

    def test_inventory_item_needs_no_authorisation(self):
        conn, _s, _t = build_connector([eb_ok({})], allow_writes=True)
        env = conn.upsert_inventory_item(
            sku="SKU-1", title="Brush", description="<p>x</p>", quantity=5)
        self.assertTrue(any("invisible to buyers" in w for w in env.warnings))

    def test_title_is_truncated_at_eighty_deliberately(self):
        conn, sender, _t = build_connector([eb_ok({})], allow_writes=True)
        long_title = "A" * 120
        env = conn.upsert_inventory_item(
            sku="SKU-1", title=long_title, description="x", quantity=1)
        self.assertEqual(len(sender.last["body"]["product"]["title"]), 80)
        self.assertTrue(any("cut to 80" in w for w in env.warnings))

    def test_negative_quantity_is_refused(self):
        conn, sender, _t = build_connector([], allow_writes=True)
        with self.assertRaises(ValueError):
            conn.upsert_inventory_item(sku="S", title="t", description="d",
                                       quantity=-1)
        self.assertEqual(sender.requests, [])

    def test_offer_creation_says_it_is_not_published(self):
        conn, _s, _t = build_connector([eb_ok({"offerId": "off-1"})],
                                       allow_writes=True)
        env = conn.create_offer(sku="SKU-1", price=24.99, category_id="1281",
                                merchant_location_key="LOC-1")
        self.assertTrue(any("NOT published" in w for w in env.warnings))

    def test_publish_without_authorisation_is_refused(self):
        conn, sender, _t = build_connector([], allow_writes=True)
        with self.assertRaises(WriteNotPermitted) as ctx:
            conn.publish_offer("off-1", authorisation=None)
        self.assertIn("binding obligation to sell", str(ctx.exception))
        self.assertEqual(sender.requests, [])

    def test_publish_with_denied_authorisation_is_refused(self):
        conn, sender, _t = build_connector([], allow_writes=True)
        with self.assertRaises(WriteNotPermitted) as ctx:
            conn.publish_offer("off-1",
                               authorisation=_Authorisation(False, "cap exceeded"))
        self.assertIn("cap exceeded", str(ctx.exception))
        self.assertEqual(sender.requests, [])

    def test_truthy_non_authorisation_is_still_refused(self):
        conn, _s, _t = build_connector([], allow_writes=True)
        with self.assertRaises(WriteNotPermitted):
            conn.publish_offer("off-1", authorisation="yes")

    def test_publish_with_authorisation_proceeds(self):
        conn, _s, _t = build_connector([eb_ok({"listingId": "1122334455"})],
                                       allow_writes=True)
        env = conn.publish_offer("off-1", authorisation=_Authorisation(True))
        self.assertEqual(env.payload["listing_id"], "1122334455")

    def test_withdraw_needs_no_authorisation(self):
        # Taking a listing down is how you stop a problem.
        conn, _s, _t = build_connector([eb_ok({"listingId": "1122334455"})],
                                       allow_writes=True)
        self.assertEqual(conn.withdraw_offer("off-1").payload["offer_id"], "off-1")

    def test_writes_blocked_without_permission(self):
        conn, _s, _t = build_connector([])
        with self.assertRaises(WriteNotPermitted):
            conn.upsert_inventory_item(sku="S", title="t", description="d",
                                       quantity=1)


class TestPricing(unittest.TestCase):
    def test_non_positive_price_is_refused(self):
        conn, sender, _t = build_connector([], allow_writes=True)
        with self.assertRaises(ValueError):
            conn.update_price("SKU-1", 0.0)
        self.assertEqual(sender.requests, [])

    def test_missing_offer_raises_rather_than_no_op(self):
        conn, _s, _t = build_connector([eb_ok({"offers": []})], allow_writes=True)
        with self.assertRaises(EbayAPIError) as ctx:
            conn.update_price("SKU-1", 19.99)
        self.assertIn("nothing to reprice", str(ctx.exception))


class TestUnavailableData(unittest.TestCase):
    def test_reviews_raise_and_explain_ebays_feedback_model(self):
        conn, _s, _t = build_connector([])
        with self.assertRaises(NotImplementedError) as ctx:
            conn.fetch_reviews("SKU-1")
        self.assertIn("attaches", str(ctx.exception))

    def test_ad_performance_raises_as_a_build_not_a_credential(self):
        conn, _s, _t = build_connector([])
        with self.assertRaises(NotImplementedError) as ctx:
            conn.fetch_ad_performance(since="2026-07-01")
        self.assertIn("not a missing", str(ctx.exception))


class TestVerification(unittest.TestCase):
    def test_successful_verification_reports_the_seller(self):
        conn, _s, _t = build_connector([eb_ok(EB_STANDARDS)])
        result = conn.verify_connection()
        self.assertTrue(result["ok"])
        self.assertEqual(result["seller"], "test_seller")
        self.assertTrue(result["top_rated"])
        self.assertEqual(result["defect_rate_pct"], 0.4)

    def test_sandbox_is_called_out(self):
        sandbox = EbayCredentials("i", "s", "r", environment="sandbox")
        transport, _s, tokens = build_transport([eb_ok(EB_STANDARDS)],
                                                credentials=sandbox)
        conn = EbayConnector(transport=transport,
                             credentials=StaticCredentials(sandbox))
        result = conn.verify_connection()
        self.assertTrue(any("SANDBOX" in w for w in result["warnings"]))

    def test_unknown_marketplace_is_warned_about(self):
        odd = EbayCredentials("i", "s", "r", marketplace_id="EBAY_ATLANTIS")
        transport, _s, _t = build_transport([eb_ok(EB_STANDARDS)], credentials=odd)
        conn = EbayConnector(transport=transport,
                             credentials=StaticCredentials(odd))
        self.assertTrue(any("not one this connector recognises" in w
                            for w in conn.verify_connection()["warnings"]))

    def test_unconfigured_client_raises(self):
        conn = EbayConnector(credentials=UnavailableCredentials())
        with self.assertRaises(ConnectorNotConfigured):
            conn.client()

    def test_unavailable_source_raises_with_a_remedy(self):
        with self.assertRaises(CredentialsUnavailable) as ctx:
            UnavailableCredentials().resolve()
        self.assertIn("developer.ebay.com", str(ctx.exception))


class TestRegistry(unittest.TestCase):
    def test_registry_hands_out_the_real_connector(self):
        from connectors import get_connector
        self.assertIsInstance(get_connector("ebay"), EbayConnector)


if __name__ == "__main__":
    unittest.main()


class TestQuotaErrorIsNeverRetried(unittest.TestCase):
    def test_429_does_not_burn_the_remaining_attempts(self):
        # On a per-second bucket a 429 means "try again shortly". On a daily
        # quota it means "you are done until midnight UTC" — retrying makes the
        # app look like it is hammering a limit it was already told about.
        transport, sender, _t = build_transport([
            eb_error(429, 2001, "Too many requests")])
        with self.assertRaises(EbayQuotaExhausted):
            transport.request(method="GET", path="/buy/browse/v1/item_summary/search",
                              api="browse", token_kind=APPLICATION)
        self.assertEqual(len(sender.requests), 1)

    def test_a_500_still_retries(self):
        transport, sender, _t = build_transport([
            eb_error(503, 25001, "Service unavailable"),
            eb_ok({"orders": [], "total": 0}),
        ])
        transport.request(method="GET", path="/sell/fulfillment/v1/order",
                          api="fulfillment")
        self.assertEqual(len(sender.requests), 2)
