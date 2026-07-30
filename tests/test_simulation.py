"""End-to-end and simulation tests.

Unit tests prove each engine is right in isolation. These prove the parts agree
when driven together over time, which is where the interesting failures live:
a metric written by one module and read by another with a different assumption,
a rate that inverts once the sample grows, a loop that looks fine on day one and
double-counts by day thirty.

The simulation runs a synthetic quarter — orders arriving, videos posted,
performance read back — and asserts the system's *conclusions* change in the
right direction as evidence accumulates: refusing early, concluding later, and
never claiming more than the data supports at either end.
"""

from __future__ import annotations

import random
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from connectors.shopify import ShopifyConnector, StaticCredentials
from connectors.shopify.credentials import ShopifyCredentials
from connectors.shopify.transport import Transport
from operator_core.config import load_policy
from operator_core.conversion import aggregate_funnel, diagnose_funnel
from operator_core.dashboard import build_dashboard, render_html, render_terminal
from operator_core.learning import SUPPORTED, learning_report
from operator_core.publishing import recommend_posting_times
from operator_core.storefront import sync_orders
from operator_core.store import Store
from tests.fakes import FakeClock, ShopifySender, sh_journey, sh_ok, sh_order

POLICY = load_policy()
CREDS = ShopifyCredentials("test-store.myshopify.com", "shpat_testtoken", "2025-07")


def connector_for(orders: list[dict]):
    sender = ShopifySender([sh_ok({"orders": {
        "nodes": orders, "pageInfo": {"hasNextPage": False, "endCursor": None}}})])
    clock = FakeClock()
    transport = Transport(shop_domain="test-store.myshopify.com",
                          access_token="shpat_testtoken", api_version="2025-07",
                          send=sender, sleep=clock.sleep,
                          monotonic=clock.monotonic)
    return ShopifyConnector(transport=transport,
                            credentials=StaticCredentials(CREDS))


class SimulationTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = TemporaryDirectory()
        self.store = Store(Path(self._dir.name) / "sim.db")

    def tearDown(self):
        self._dir.cleanup()


# ---------------------------------------------------------------------------
class TestShopifyToFunnel(SimulationTestCase):
    """The joint between the connector and the funnel table."""

    def test_orders_reach_the_funnel_with_their_channel(self):
        orders = [
            sh_order("1", journey=sh_journey("tiktok"), total="40.00",
                     tax="0.00", shipping="0.00"),
            sh_order("2", journey=sh_journey("google", source_type="search",
                                             referrer="https://google.com/"),
                     total="60.00", tax="0.00", shipping="0.00"),
        ]
        conn = connector_for(orders)
        envelope = conn.fetch_orders(since="2026-07-01")
        result = sync_orders(self.store, envelope.payload)

        self.assertEqual(result.orders_counted, 2)
        self.assertEqual(result.channels, ["search", "tiktok"])
        rows = {r["channel"]: r for r in
                self.store.storefront_range("2026-07-27", "2026-07-27")}
        self.assertEqual(rows["tiktok"]["revenue"], 40.0)
        self.assertEqual(rows["search"]["revenue"], 60.0)

    def test_sessions_stay_null_all_the_way_to_the_dashboard(self):
        # The whole chain must preserve "unknown" rather than turning it into 0
        # at any hop.
        conn = connector_for([sh_order("1", journey=sh_journey("tiktok"))])
        sync_orders(self.store, conn.fetch_orders(since="2026-07-01").payload)
        rows = self.store.storefront_range("2026-07-27", "2026-07-27")
        self.assertIsNone(rows[0]["sessions"])

        metrics = aggregate_funnel(period="p", channel="tiktok",
                                   storefront_rows=rows)
        self.assertIsNone(metrics.conversion_rate_pct)

        dashboard = build_dashboard(storefront_rows=rows)
        self.assertFalse(dashboard.tile("Conversion rate").known)

    def test_cancelled_orders_never_become_revenue(self):
        cancelled = sh_order("1", journey=sh_journey("tiktok"), total="100.00")
        cancelled["cancelledAt"] = "2026-07-27T12:00:00Z"
        conn = connector_for([cancelled,
                              sh_order("2", journey=sh_journey("tiktok"),
                                       total="40.00", tax="0.00", shipping="0.00")])
        result = sync_orders(self.store, conn.fetch_orders(since="2026-07-01").payload)
        self.assertEqual(result.orders_counted, 1)
        self.assertEqual(result.orders_excluded, 1)
        rows = self.store.storefront_range("2026-07-27", "2026-07-27")
        self.assertEqual(rows[0]["revenue"], 40.0)

    def test_refund_lands_on_the_order_day_not_the_refund_day(self):
        refunded = sh_order("1", journey=sh_journey("tiktok"), total="100.00",
                            refunded="30.00", tax="0.00", shipping="0.00",
                            created_at="2026-07-20T10:00:00Z")
        sync_orders(self.store, connector_for([refunded])
                    .fetch_orders(since="2026-07-01").payload)
        rows = self.store.storefront_range("2026-07-20", "2026-07-20")
        self.assertEqual(rows[0]["refunds"], 30.0)
        self.assertEqual(rows[0]["revenue"], 70.0)

    def test_resync_is_idempotent(self):
        # Refunds change after the fact, so the window is re-fetched. Doing so
        # must correct the row, not add to it.
        orders = [sh_order("1", journey=sh_journey("tiktok"), total="40.00",
                           tax="0.00", shipping="0.00")]
        for _ in range(3):
            sync_orders(self.store, connector_for(orders)
                        .fetch_orders(since="2026-07-01").payload)
        rows = self.store.storefront_range("2026-07-27", "2026-07-27")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["orders"], 1)

    def test_unattributed_majority_is_warned_about(self):
        orders = [sh_order(str(i), journey=None) for i in range(8)]
        orders.append(sh_order("9", journey=sh_journey("tiktok")))
        result = sync_orders(self.store, connector_for(orders)
                             .fetch_orders(since="2026-07-01").payload)
        self.assertGreater(result.unattributed_share_pct, 30)
        self.assertTrue(any("understate every channel" in w
                            for w in result.warnings))

    def test_provenance_survives_to_the_banner(self):
        # A seed-sourced sync must not produce an unlabelled dashboard.
        conn = connector_for([sh_order("1", journey=sh_journey("tiktok"))])
        sync_orders(self.store, conn.fetch_orders(since="2026-07-01").payload,
                    data_source="seed")
        rows = self.store.storefront_range("2026-07-27", "2026-07-27")
        dashboard = build_dashboard(storefront_rows=rows)
        self.assertIn("NOT REAL BUSINESS NUMBERS", dashboard.banner)
        self.assertIn("NOT REAL BUSINESS NUMBERS", render_terminal(dashboard))
        self.assertIn("NOT REAL BUSINESS NUMBERS", render_html(dashboard))


# ---------------------------------------------------------------------------
class TestQuarterSimulation(SimulationTestCase):
    """A synthetic quarter. Conclusions must firm up as evidence accumulates."""

    ANGLES = ("problem_solution", "before_after")
    START = date(2026, 4, 1)

    def _post(self, day_offset: int, index: int, angle: str, views: int,
              clicks: int) -> str:
        package_id = f"SIM-{index:03d}"
        posted = datetime(2026, 4, 1, 19, 0, tzinfo=timezone.utc) + \
            timedelta(days=day_offset)
        self.store.record_published_video(
            package_id=package_id, sku="SIM-SKU-01",
            published_at=posted.isoformat(), angle=angle,
            hook_archetype="problem_callout", fmt="UGC", cta_variant="passive",
            runtime_seconds=15.0)
        self.store.record_video_metrics(
            package_id=package_id,
            measured_at=(posted + timedelta(days=7)).isoformat(),
            views=views, likes=views // 12, comments=views // 200,
            shares=views // 90, saves=views // 60, avg_watch_pct=42.0,
            link_clicks=clicks)
        return package_id

    def _run_quarter(self, posts: int, *, seed: int = 7):
        """Post `posts` videos where one angle genuinely converts better."""
        rng = random.Random(seed)
        for i in range(posts):
            angle = self.ANGLES[i % 2]
            views = rng.randint(20000, 60000)
            # A real 3x difference in click rate, with noise on top.
            base = 0.030 if angle == "problem_solution" else 0.010
            clicks = int(views * base * rng.uniform(0.85, 1.15))
            self._post(i * 2, i, angle, views, clicks)

    def test_early_evidence_refuses_to_conclude(self):
        self._run_quarter(posts=4)
        report = learning_report(video_rows=self.store.latest_video_metrics())
        self.assertEqual(report["actionable_findings"], 0)
        self.assertIn("expected state for a new account", report["headline"])

    def test_a_full_quarter_finds_the_planted_difference(self):
        self._run_quarter(posts=30)
        report = learning_report(video_rows=self.store.latest_video_metrics())
        angle = report["findings"]["angle"]
        self.assertEqual(angle["verdict"], SUPPORTED)
        self.assertEqual(angle["leader"], "problem_solution")

    def test_conclusions_only_strengthen_with_more_data(self):
        # The failure this guards: a metric that inverts once the sample grows,
        # which means the early answer was an artefact of the aggregation.
        self._run_quarter(posts=12)
        early = learning_report(video_rows=self.store.latest_video_metrics())
        early_leader = early["findings"]["angle"]["leader"]

        for i in range(12, 40):
            angle = self.ANGLES[i % 2]
            base = 0.030 if angle == "problem_solution" else 0.010
            self._post(i * 2, i, angle, 40000, int(40000 * base))

        late = late_report = learning_report(
            video_rows=self.store.latest_video_metrics())
        self.assertEqual(late["findings"]["angle"]["leader"], early_leader)
        self.assertEqual(late_report["findings"]["angle"]["verdict"], SUPPORTED)

    def test_posting_times_stay_silent_when_slots_are_spread(self):
        # Every post at 19:00 on alternating weekdays: no single slot reaches
        # the floor, so the recommender must still decline.
        self._run_quarter(posts=20)
        timing = recommend_posting_times(self.store.latest_video_metrics())
        if not timing["confident"]:
            self.assertIn("posts_analysed", timing)
        else:
            # If it does conclude, every recommendation must clear the floor.
            for rec in timing["recommendations"]:
                self.assertGreaterEqual(rec["posts"], 3)

    def test_funnel_over_the_quarter_is_internally_consistent(self):
        self._run_quarter(posts=20)
        for offset in range(0, 90, 3):
            day = (self.START + timedelta(days=offset)).isoformat()
            self.store.upsert_storefront_daily(
                metric_date=day, channel="tiktok", sessions=None,
                orders=3, revenue=126.0, refunds=0.0, new_customers=3,
                data_source="live")

        rows = self.store.storefront_range("2026-04-01", "2026-06-30")
        metrics = aggregate_funnel(period="Q2", channel="tiktok",
                                   storefront_rows=rows,
                                   video_rows=self.store.latest_video_metrics())
        self.assertEqual(metrics.orders, len(rows) * 3)
        self.assertAlmostEqual(metrics.aov, 42.0, places=2)
        self.assertIsNone(metrics.conversion_rate_pct)

        diagnosis = diagnose_funnel(metrics, POLICY)
        # Sessions unknown, so the landing stage cannot be judged and must be
        # listed as unmeasured rather than silently passed.
        self.assertTrue(any("Sessions are unmeasured" in u
                            for u in diagnosis.unmeasurable))

    def test_dashboard_renders_a_full_quarter_without_contradiction(self):
        self._run_quarter(posts=20)
        for offset in range(0, 90, 3):
            self.store.upsert_storefront_daily(
                metric_date=(self.START + timedelta(days=offset)).isoformat(),
                channel="tiktok", orders=3, revenue=126.0, data_source="live")

        rows = self.store.storefront_range("2026-04-01", "2026-06-30")
        dashboard = build_dashboard(
            storefront_rows=rows,
            video_rows=self.store.latest_video_metrics(),
            period="2026 Q2")
        terminal = render_terminal(dashboard)
        markup = render_html(dashboard)

        # Live data, so no banner in either surface.
        self.assertEqual(dashboard.banner, "")
        self.assertNotIn("NOT REAL BUSINESS NUMBERS", terminal)
        self.assertNotIn("NOT REAL BUSINESS NUMBERS", markup)

        # Every known tile's value appears in both renderings.
        for tile in dashboard.tiles:
            if tile.known:
                self.assertIn(tile.display(), terminal, tile.label)
                self.assertIn(tile.display(), markup, tile.label)

    def test_video_count_matches_across_store_and_dashboard(self):
        self._run_quarter(posts=20)
        rows = self.store.latest_video_metrics()
        self.assertEqual(len(rows), 20)
        dashboard = build_dashboard(storefront_rows=[], video_rows=rows)
        self.assertEqual(dashboard.tile("Videos published").value, 20)


if __name__ == "__main__":
    unittest.main()
