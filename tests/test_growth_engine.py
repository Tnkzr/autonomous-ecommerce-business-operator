"""Publishing, conversion, and experiment tests.

The assertions that matter are the ones proving these engines can return "we do
not know". A funnel diagnosis that always names a leak, a posting-time
recommender that always has an opinion, and a test framework that always picks
a winner are all worse than useless — they are confident, and wrong at a rate
nobody measures.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from operator_core.config import load_policy
from operator_core.conversion import (
    FunnelMetrics,
    aggregate_funnel,
    bundle_opportunities,
    diagnose_funnel,
    locate_leak,
)
from operator_core.experiments import (
    ArmResult,
    compare_arms,
    evaluate,
    portfolio_view,
    required_sample_per_arm,
)
from operator_core.publishing import (
    JUDGEMENT_HOURS,
    MIN_POSTS_FOR_TIMING,
    MIN_VIEWS_FOR_RATES,
    build_publishing_plan,
    cadence_report,
    channel_summary,
    follower_growth,
    recommend_posting_times,
    summarise_video,
)
from operator_core.store import Store

POLICY = load_policy()


class _Package:
    """Stands in for a ProductionPackage."""

    def __init__(self, package_id: str, ready: bool = True, blockers=None):
        self.package_id = package_id
        self.sku = "SKU-1"
        self.angle = "Problem / solution"
        self.ready = ready
        self.blockers = blockers or []


def video_row(**kw):
    base = {
        "package_id": "P1", "sku": "SKU-1", "angle": "problem_solution",
        "hook_archetype": "problem_callout", "published_at": "2026-07-20T19:00:00+00:00",
        "weekday": 0, "hour": 19, "hours_since_post": 72.0, "views": 40000,
        "likes": 3000, "comments": 200, "shares": 400, "saves": 800,
        "avg_watch_pct": 45.0, "link_clicks": 600,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
class TestPublishingPlan(unittest.TestCase):
    def test_schedule_covers_the_window(self):
        plan = build_publishing_plan([_Package(f"P{i}") for i in range(6)],
                                     days=3, posts_per_day=2,
                                     start=date(2026, 8, 1))
        self.assertEqual(len(plan.posts), 6)
        self.assertEqual(plan.starts, "2026-08-01")
        self.assertEqual(plan.ends, "2026-08-03")

    def test_blocked_packages_are_scheduled_and_flagged_not_dropped(self):
        # Dropping them silently shrinks the calendar and hides a short pipeline.
        plan = build_publishing_plan(
            [_Package("P1"), _Package("P2", ready=False, blockers=["unfilled slot"])],
            days=1, posts_per_day=2, start=date(2026, 8, 1))
        self.assertEqual(len(plan.posts), 2)
        self.assertEqual(len(plan.shootable), 1)
        self.assertTrue(any("not shootable" in w for w in plan.warnings))

    def test_thin_pipeline_warns_about_reposting(self):
        plan = build_publishing_plan([_Package("P1")], days=7, posts_per_day=2,
                                     start=date(2026, 8, 1))
        self.assertTrue(any("Re-posting" in w for w in plan.warnings))

    def test_empty_pipeline_is_an_explicit_warning(self):
        plan = build_publishing_plan([], days=7)
        self.assertEqual(plan.posts, [])
        self.assertTrue(any("nothing to schedule" in w for w in plan.warnings))


class TestVideoPerformance(unittest.TestCase):
    def test_low_view_video_gets_no_rates(self):
        # A handful of interactions on a video nobody saw produces a percentage
        # that means nothing.
        perf = summarise_video(video_row(views=300, likes=30))
        self.assertFalse(perf.rateable)
        self.assertIsNone(perf.engagement_rate_pct)
        self.assertIn("below the", perf.note)

    def test_fresh_video_is_marked_provisional(self):
        perf = summarise_video(video_row(hours_since_post=4.0))
        self.assertFalse(perf.mature)
        self.assertIn("provisional", perf.note)

    def test_rates_are_computed_when_the_sample_supports_it(self):
        perf = summarise_video(video_row())
        self.assertTrue(perf.rateable)
        self.assertAlmostEqual(perf.click_rate_pct, 1.5, places=2)
        self.assertAlmostEqual(perf.engagement_rate_pct, 11.0, places=2)

    def test_missing_click_data_is_none_not_zero(self):
        perf = summarise_video(video_row(link_clicks=None))
        self.assertIsNone(perf.click_rate_pct)

    def test_averages_exclude_unjudgeable_videos(self):
        rows = [video_row(package_id="A"),
                video_row(package_id="B", views=200, likes=100),      # below floor
                video_row(package_id="C", hours_since_post=2.0)]       # too fresh
        summary = channel_summary(rows)
        self.assertEqual(summary["videos_published"], 3)
        self.assertEqual(summary["videos_judgeable"], 1)
        self.assertEqual(summary["videos_below_view_floor"], 1)
        self.assertEqual(summary["videos_too_fresh"], 1)

    def test_views_are_reported_as_a_median(self):
        # One breakout drags a mean above every other post and describes a
        # channel that does not exist.
        rows = [video_row(package_id=str(i), views=1000) for i in range(4)]
        rows.append(video_row(package_id="viral", views=5_000_000))
        self.assertEqual(channel_summary(rows)["median_views"], 1000.0)


class TestPostingTimes(unittest.TestCase):
    """No built-in 'best time to post' table. This is the point."""

    def test_refuses_without_enough_history(self):
        result = recommend_posting_times([video_row() for _ in range(5)])
        self.assertFalse(result["confident"])
        self.assertEqual(result["recommendations"], [])
        self.assertIn("someone else's audience", result["note"])

    def test_refuses_when_no_slot_has_enough_posts(self):
        # 20 posts spread one per slot: an apparent winner exists and is noise.
        rows = [video_row(package_id=str(i), weekday=i % 7, hour=i)
                for i in range(MIN_POSTS_FOR_TIMING + 5)]
        result = recommend_posting_times(rows)
        self.assertFalse(result["confident"])
        self.assertIn("168 possible slots", result["note"])

    def test_recommends_from_the_accounts_own_data(self):
        rows = []
        for i in range(12):        # concentrated in one slot, higher views
            rows.append(video_row(package_id=f"good{i}", weekday=2, hour=19,
                                  views=50000))
        for i in range(8):
            rows.append(video_row(package_id=f"bad{i}", weekday=4, hour=8,
                                  views=5000))
        result = recommend_posting_times(rows)
        self.assertTrue(result["confident"])
        best = result["recommendations"][0]
        self.assertEqual(best["weekday"], "Wednesday")
        self.assertEqual(best["hour"], "19:00")
        self.assertIn("own posts only", result["note"])

    def test_immature_posts_are_excluded_from_timing(self):
        rows = [video_row(package_id=str(i), hours_since_post=2.0)
                for i in range(30)]
        self.assertFalse(recommend_posting_times(rows)["confident"])


class TestCadence(unittest.TestCase):
    def test_gap_is_reported(self):
        today = date(2026, 8, 28)
        rows = [{"published_at": "2026-08-01T10:00:00+00:00"},
                {"published_at": "2026-08-28T10:00:00+00:00"}]
        report = cadence_report(rows, days=28, today=today)
        self.assertGreaterEqual(report["longest_gap_days"], 4)
        self.assertTrue(any("silence" in w for w in report["warnings"]))

    def test_batching_into_one_day_is_flagged(self):
        today = date(2026, 8, 28)
        rows = [{"published_at": "2026-08-27T10:00:00+00:00"} for _ in range(6)]
        report = cadence_report(rows, days=28, today=today)
        self.assertTrue(any("compete with each other" in w for w in report["warnings"]))

    def test_posts_outside_the_window_are_ignored(self):
        report = cadence_report([{"published_at": "2020-01-01T10:00:00+00:00"}],
                                days=28, today=date(2026, 8, 28))
        self.assertEqual(report["posts"], 0)


class TestFollowerGrowth(unittest.TestCase):
    def test_one_reading_is_a_level_not_a_rate(self):
        result = follower_growth([{"observed_at": "2026-08-01T00:00:00+00:00",
                                   "followers": 1000}])
        self.assertIsNone(result["growth_per_day"])
        self.assertIn("level, not a rate", result["note"])

    def test_two_readings_produce_a_rate(self):
        result = follower_growth([
            {"observed_at": "2026-08-01T00:00:00+00:00", "followers": 1000},
            {"observed_at": "2026-08-11T00:00:00+00:00", "followers": 1500},
        ])
        self.assertEqual(result["growth_per_day"], 50.0)


# ---------------------------------------------------------------------------
class TestFunnelMetrics(unittest.TestCase):
    def _metrics(self, **kw):
        base = dict(period="2026-08", channel="tiktok", views=180000, clicks=2100,
                    sessions=1900, orders=40, revenue=1600.0, refunds=0.0,
                    new_customers=36)
        base.update(kw)
        return FunnelMetrics(**base)

    def test_conversion_rate_is_none_when_sessions_are_unknown(self):
        # Back-computing it from orders is the most confidently wrong number a
        # store can produce.
        self.assertIsNone(self._metrics(sessions=None).conversion_rate_pct)
        self.assertIsNone(self._metrics(sessions=None).revenue_per_visitor)

    def test_zero_sessions_is_also_none_not_a_division(self):
        self.assertIsNone(self._metrics(sessions=0).conversion_rate_pct)

    def test_rates_compute_when_denominators_exist(self):
        m = self._metrics()
        self.assertAlmostEqual(m.click_rate_pct, 1.167, places=2)
        self.assertAlmostEqual(m.conversion_rate_pct, 2.105, places=2)
        self.assertEqual(m.aov, 40.0)

    def test_aov_is_none_with_no_orders(self):
        self.assertIsNone(self._metrics(orders=0, revenue=0).aov)

    def test_repeat_share(self):
        self.assertEqual(self._metrics(orders=40, new_customers=30).repeat_share_pct,
                         25.0)


class TestLeakLocation(unittest.TestCase):
    """Earliest failing stage, not worst — a funnel is sequential."""

    def _metrics(self, **kw):
        base = dict(period="p", channel="tiktok", views=180000, clicks=2100,
                    sessions=1900, orders=40, revenue=1600.0, refunds=0.0,
                    new_customers=36)
        base.update(kw)
        return FunnelMetrics(**base)

    def test_reach_leak_short_circuits_the_rest(self):
        leak, _u = locate_leak(self._metrics(views=200), POLICY)
        self.assertEqual(leak, "reach")

    def test_click_leak_is_found_before_landing(self):
        # Both stages are bad; the click stage is upstream and must win.
        leak, _u = locate_leak(self._metrics(clicks=100, orders=1, revenue=40),
                               POLICY)
        self.assertEqual(leak, "click")

    def test_landing_leak_when_clicks_are_healthy(self):
        leak, _u = locate_leak(self._metrics(orders=5, revenue=200), POLICY)
        self.assertEqual(leak, "landing")

    def test_healthy_funnel_has_no_leak(self):
        leak, unmeasurable = locate_leak(self._metrics(), POLICY)
        self.assertIsNone(leak)
        self.assertEqual(unmeasurable, [])

    def test_unmeasured_stages_are_listed_not_assumed_healthy(self):
        leak, unmeasurable = locate_leak(
            self._metrics(views=None, clicks=None, sessions=None), POLICY)
        self.assertIsNone(leak)
        self.assertEqual(len(unmeasurable), 3)
        self.assertTrue(any("back-computed" in u for u in unmeasurable))


class TestRecommendations(unittest.TestCase):
    def _diagnose(self, **kw):
        base = dict(period="p", channel="tiktok", views=180000, clicks=2100,
                    sessions=1900, orders=40, revenue=1600.0, refunds=0.0,
                    new_customers=36)
        base.update(kw)
        return diagnose_funnel(FunnelMetrics(**base), POLICY)

    def test_click_leak_recommends_creative_not_the_store(self):
        diagnosis = self._diagnose(clicks=100)
        self.assertEqual(diagnosis.leak_stage, "click")
        self.assertTrue(all(r.stage != "landing" or r.priority != "HIGH"
                            for r in diagnosis.recommendations))
        self.assertTrue(any("creative problem" in r.rationale
                            for r in diagnosis.recommendations))

    def test_low_traffic_marks_store_recommendations_unconfident(self):
        # 1 order in 200 sessions is a 0.5% conversion rate, under the
        # benchmark — so the landing recommendations fire, on 200 sessions.
        diagnosis = self._diagnose(sessions=200, orders=1, revenue=40)
        store_recs = [r for r in diagnosis.recommendations
                      if r.stage in ("landing", "checkout")]
        self.assertTrue(store_recs)
        self.assertTrue(all(not r.confident for r in store_recs))
        self.assertTrue(any("destroys the baseline" in w for w in diagnosis.warnings))

    def test_healthy_funnel_redirects_to_order_value(self):
        diagnosis = self._diagnose(orders=200, revenue=9000, new_customers=100)
        self.assertIsNone(diagnosis.leak_stage)
        self.assertTrue(any("repeat purchase" in w for w in diagnosis.warnings))

    def test_high_refund_rate_recommends_fixing_the_page_not_the_video(self):
        diagnosis = self._diagnose(orders=100, revenue=4000, refunds=600)
        rec = next(r for r in diagnosis.recommendations if "over-promising" in r.action)
        self.assertIn("not the videos", rec.action)
        self.assertIn("that fall is the point", rec.expected_effect)

    def test_low_aov_recommends_a_bundle(self):
        diagnosis = self._diagnose(orders=100, revenue=2000)   # AOV 20 vs target 45
        self.assertTrue(any("bundle" in r.action for r in diagnosis.recommendations))


class TestAggregation(unittest.TestCase):
    def test_sessions_stay_none_when_no_row_reports_them(self):
        rows = [{"channel": "tiktok", "sessions": None, "orders": 3, "revenue": 120.0}]
        metrics = aggregate_funnel(period="p", channel="tiktok", storefront_rows=rows)
        self.assertIsNone(metrics.sessions)
        self.assertEqual(metrics.orders, 3)

    def test_channel_filter_applies(self):
        rows = [{"channel": "tiktok", "orders": 3, "revenue": 120.0},
                {"channel": "search", "orders": 9, "revenue": 400.0}]
        metrics = aggregate_funnel(period="p", channel="tiktok", storefront_rows=rows)
        self.assertEqual(metrics.orders, 3)


class TestBundles(unittest.TestCase):
    def test_pairs_below_the_floor_are_not_suggested(self):
        lines = [{"order_id": "1", "sku": "A"}, {"order_id": "1", "sku": "B"}]
        self.assertEqual(bundle_opportunities(lines, min_co_occurrences=10), [])

    def test_frequent_pair_is_surfaced_with_both_directions(self):
        lines = []
        for i in range(12):
            lines.append({"order_id": str(i), "sku": "A"})
            lines.append({"order_id": str(i), "sku": "B"})
        for i in range(100, 130):     # A also sells alone a lot
            lines.append({"order_id": str(i), "sku": "A"})
        pair = bundle_opportunities(lines, min_co_occurrences=10)[0]
        self.assertEqual(pair["skus"], ["A", "B"])
        # B pulls A far more than A pulls B — the bundle should lead with B.
        self.assertGreater(pair["confidence_right_to_left_pct"],
                           pair["confidence_left_to_right_pct"])


# ---------------------------------------------------------------------------
class TestSampleSizing(unittest.TestCase):
    def test_smaller_effects_need_larger_samples(self):
        big = required_sample_per_arm(baseline_rate=0.02, minimum_detectable_effect=0.5)
        small = required_sample_per_arm(baseline_rate=0.02, minimum_detectable_effect=0.1)
        self.assertGreater(small, big)

    def test_percentage_baseline_is_rejected(self):
        # 2 instead of 0.02 under-sizes the test by a hundredfold.
        with self.assertRaises(ValueError):
            required_sample_per_arm(baseline_rate=2.0, minimum_detectable_effect=0.3)

    def test_unsupported_power_is_rejected(self):
        with self.assertRaises(ValueError):
            required_sample_per_arm(baseline_rate=0.02,
                                    minimum_detectable_effect=0.3, power=0.77)


class TestExperimentEvaluation(unittest.TestCase):
    def _record(self, arms, **kw):
        base = {"experiment_id": "E1", "sku": "SKU-1", "hypothesis": "h",
                "success_metric": "conversion_rate", "success_threshold": 0.02,
                "min_sample": 500, "max_spend_usd": 150.0, "deadline": "",
                "arms": arms}
        base.update(kw)
        return base

    def test_below_sample_floor_is_inconclusive_not_a_winner(self):
        result = evaluate(self._record([
            {"arm": "A", "exposures": 40, "conversions": 4, "revenue": 160},
            {"arm": "B", "exposures": 40, "conversions": 1, "revenue": 40},
        ]), POLICY)
        self.assertEqual(result.verdict, "INCONCLUSIVE")
        self.assertFalse(result.sample_reached)
        self.assertTrue(any("noise" in r for r in result.reasons))

    def test_overlapping_interval_is_inconclusive(self):
        result = evaluate(self._record([
            {"arm": "A", "exposures": 600, "conversions": 18, "revenue": 720},
            {"arm": "B", "exposures": 600, "conversions": 15, "revenue": 600},
        ]), POLICY)
        self.assertEqual(result.verdict, "INCONCLUSIVE")
        self.assertFalse(result.significant)
        self.assertTrue(any("includes zero" in r for r in result.reasons))

    def test_clear_winner_scales(self):
        result = evaluate(self._record([
            {"arm": "A", "exposures": 3000, "conversions": 240, "revenue": 9600},
            {"arm": "B", "exposures": 3000, "conversions": 90, "revenue": 3600},
        ]), POLICY)
        self.assertEqual(result.verdict, "SCALE")
        self.assertTrue(result.significant)
        self.assertEqual(result.leader, "A")

    def test_winner_below_its_registered_threshold_is_archived(self):
        # It won its comparison and failed its criterion. That is the criterion
        # doing its job.
        result = evaluate(self._record([
            {"arm": "A", "exposures": 3000, "conversions": 30, "revenue": 1200},
            {"arm": "B", "exposures": 3000, "conversions": 3, "revenue": 120},
        ], success_threshold=0.05), POLICY)
        self.assertEqual(result.verdict, "ARCHIVE")
        self.assertTrue(any("failed its criterion" in r for r in result.reasons))

    def test_unprofitable_winner_is_archived_not_scaled(self):
        import copy
        policy = copy.deepcopy(POLICY.raw)
        policy["experiments"]["min_contribution_per_exposure_usd"] = 1.0
        mutated = type(POLICY)(raw=policy, source_path=POLICY.source_path)
        self.assertEqual(
            mutated.raw["experiments"]["min_contribution_per_exposure_usd"], 1.0,
            "policy mutation did not apply — the test would pass vacuously")
        result = evaluate(self._record([
            {"arm": "A", "exposures": 3000, "conversions": 240, "revenue": 9600,
             "spend": 9000},
            {"arm": "B", "exposures": 3000, "conversions": 90, "revenue": 3600},
        ]), mutated)
        self.assertEqual(result.verdict, "ARCHIVE")
        self.assertTrue(any("scales a loss" in r for r in result.reasons))

    def test_spend_over_cap_is_warned_about(self):
        result = evaluate(self._record([
            {"arm": "A", "exposures": 3000, "conversions": 240, "revenue": 9600,
             "spend": 200},
            {"arm": "B", "exposures": 3000, "conversions": 90, "revenue": 3600},
        ]), POLICY)
        self.assertTrue(any("cap" in w for w in result.warnings))

    def test_expired_test_below_sample_is_abandoned(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        result = evaluate(self._record([
            {"arm": "A", "exposures": 40, "conversions": 4, "revenue": 160},
            {"arm": "B", "exposures": 40, "conversions": 1, "revenue": 40},
        ], deadline=past), POLICY)
        self.assertEqual(result.verdict, "ABANDONED")

    def test_no_arms_is_inconclusive_not_a_crash(self):
        self.assertEqual(evaluate(self._record([]), POLICY).verdict, "INCONCLUSIVE")


class TestPortfolio(unittest.TestCase):
    def test_mostly_unresolved_programme_is_called_out(self):
        evaluations = [evaluate({"experiment_id": f"E{i}", "sku": "S",
                                 "hypothesis": "h", "success_metric": "m",
                                 "success_threshold": 0.02, "min_sample": 5000,
                                 "arms": [
                                     {"arm": "A", "exposures": 100, "conversions": 3},
                                     {"arm": "B", "exposures": 100, "conversions": 2}]},
                                POLICY) for i in range(5)]
        view = portfolio_view(evaluations)
        self.assertGreater(view["inconclusive_share_pct"], 60)
        self.assertIn("underpowered", view["note"])

    def test_empty_programme(self):
        self.assertEqual(portfolio_view([])["experiments"], 0)


# ---------------------------------------------------------------------------
class TestStorePersistence(unittest.TestCase):
    def setUp(self):
        self._dir = TemporaryDirectory()
        self.store = Store(Path(self._dir.name) / "t.db")

    def tearDown(self):
        self._dir.cleanup()

    def test_metrics_for_an_unposted_video_are_refused(self):
        # An unjoinable row would quietly skew every average computed over it.
        with self.assertRaises(ValueError) as ctx:
            self.store.record_video_metrics(package_id="GHOST",
                                            measured_at="2026-08-01T00:00:00+00:00")
        self.assertIn("unjoinable", str(ctx.exception))

    def test_hours_since_post_is_derived_not_supplied(self):
        self.store.record_published_video(
            package_id="P1", sku="S1", published_at="2026-08-01T10:00:00+00:00")
        self.store.record_video_metrics(package_id="P1", views=1000,
                                        measured_at="2026-08-03T10:00:00+00:00")
        self.assertEqual(self.store.latest_video_metrics()[0]["hours_since_post"], 48.0)

    def test_latest_reading_wins_not_the_first(self):
        # Engagement accrues for days; an early reading understates a late riser.
        self.store.record_published_video(
            package_id="P1", sku="S1", published_at="2026-08-01T10:00:00+00:00")
        self.store.record_video_metrics(package_id="P1", views=500,
                                        measured_at="2026-08-01T12:00:00+00:00")
        self.store.record_video_metrics(package_id="P1", views=90000,
                                        measured_at="2026-08-05T12:00:00+00:00")
        rows = self.store.latest_video_metrics()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["views"], 90000)

    def test_single_arm_experiment_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self.store.register_experiment(
                sku="S1", hypothesis="h", variable="hook", success_metric="cvr",
                success_threshold=0.02, min_sample=500, arms={"only": "one"})
        self.assertIn("launch with a hopeful name", str(ctx.exception))

    def test_zero_sample_floor_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.register_experiment(
                sku="S1", hypothesis="h", variable="hook", success_metric="cvr",
                success_threshold=0.02, min_sample=0, arms={"a": "1", "b": "2"})

    def test_result_against_an_unregistered_arm_is_refused(self):
        eid = self.store.register_experiment(
            sku="S1", hypothesis="h", variable="hook", success_metric="cvr",
            success_threshold=0.02, min_sample=500, arms={"a": "1", "b": "2"})
        with self.assertRaises(ValueError) as ctx:
            self.store.record_arm_result(eid, "c", exposures=10)
        self.assertIn("nobody designed", str(ctx.exception))

    def test_conclusion_is_idempotent(self):
        eid = self.store.register_experiment(
            sku="S1", hypothesis="h", variable="hook", success_metric="cvr",
            success_threshold=0.02, min_sample=500, arms={"a": "1", "b": "2"})
        self.assertTrue(self.store.conclude_experiment(eid, outcome="SCALE",
                                                       detail={}))
        self.assertFalse(self.store.conclude_experiment(eid, outcome="ARCHIVE",
                                                        detail={}))

    def test_unknown_outcome_is_refused(self):
        eid = self.store.register_experiment(
            sku="S1", hypothesis="h", variable="hook", success_metric="cvr",
            success_threshold=0.02, min_sample=500, arms={"a": "1", "b": "2"})
        with self.assertRaises(ValueError):
            self.store.conclude_experiment(eid, outcome="WINNER", detail={})

    def test_storefront_rows_keep_null_sessions(self):
        self.store.upsert_storefront_daily(
            metric_date="2026-08-01", channel="tiktok", sessions=None,
            orders=4, revenue=160.0, data_source="live")
        row = self.store.storefront_range("2026-08-01", "2026-08-01")[0]
        self.assertIsNone(row["sessions"])


if __name__ == "__main__":
    unittest.main()
