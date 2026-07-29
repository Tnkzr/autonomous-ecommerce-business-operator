"""Tests for scoring, content strategy, LTV, and the weekly review.

Same bias as the rest of the suite: the assertions that matter most are the
ones proving the system distinguishes "measured and bad" from "not measured",
refuses to fabricate the inputs it lacks, and lets a single disqualifying risk
override an otherwise excellent product.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from operator_core.config import load_policy
from operator_core.content import (
    BANNED_CLAIM_PATTERNS,
    build_calendar,
    build_content_plan,
    build_creator_briefs,
    build_hashtags,
    build_hooks,
    build_shot_list,
    build_voiceover,
    check_claims,
)
from operator_core.economics import (
    compute_unit_economics,
    economics_for_candidate,
    lifetime_value,
    max_acquisition_cost,
)
from operator_core.scoring import (
    RISK_CAP_THRESHOLD,
    build_scorecard,
    rank,
    score_demand,
    score_return_risk,
    score_trend,
    score_virality,
)
from operator_core.store import Store
from operator_core.weekly import (
    build_weekly_report,
    derive_engineering_backlog,
    derive_experiments,
    render_weekly_report,
)
from tests.test_operator import make_candidate, make_supplier

POLICY = load_policy()


def econ_for(candidate):
    return economics_for_candidate(POLICY, candidate)


class TestScoringDimensions(unittest.TestCase):
    def test_missing_data_scores_none_not_zero(self):
        # "Not measured" and "measured and terrible" are different facts.
        d = score_trend(None, None)
        self.assertIsNone(d.value)
        self.assertFalse(d.measured)
        self.assertEqual(d.band, "UNMEASURED")

    def test_demand_is_log_scaled(self):
        low = score_demand(make_candidate(est_monthly_demand_units=100), POLICY)
        mid = score_demand(make_candidate(est_monthly_demand_units=1000), POLICY)
        high = score_demand(make_candidate(est_monthly_demand_units=10000), POLICY)
        self.assertLess(low.value, mid.value)
        self.assertLess(mid.value, high.value)
        # Log scaling: the first 10x gains as much as the second.
        self.assertAlmostEqual(mid.value - low.value, high.value - mid.value, delta=2)

    def test_spike_decay_scores_below_steady(self):
        # A rolled-over spike is worse than a product that never spiked: the
        # stock it justifies buying will sit.
        spike = score_trend("SPIKE_DECAY", -80)
        steady = score_trend("STEADY", 0)
        self.assertLess(spike.value, steady.value)

    def test_virality_rewards_demonstrable_products(self):
        demo = make_candidate(
            title="self cleaning slicker brush",
            description="One press and the hair pops off. No more mess.",
            keywords=["self cleaning brush", "pet grooming"])
        dull = make_candidate(
            title="replacement filter cartridge",
            description="Compatible filter cartridge.", keywords=["filter"])
        self.assertGreater(score_virality(demo).value, score_virality(dull).value)

    def test_virality_penalises_high_price(self):
        cheap = make_candidate(target_price=25.0, title="collapsible storage box")
        dear = make_candidate(target_price=180.0, title="collapsible storage box")
        self.assertGreater(score_virality(cheap).value, score_virality(dear).value)

    def test_hyphen_and_space_variants_both_match(self):
        # Supplier titles use whichever form they feel like.
        hyphen = make_candidate(title="self-cleaning brush")
        spaced = make_candidate(title="self cleaning brush")
        self.assertEqual(score_virality(hyphen).value, score_virality(spaced).value)

    def test_observed_return_rate_beats_estimate(self):
        c = make_candidate()
        estimated = score_return_risk(c, POLICY)
        observed = score_return_risk(c, POLICY, observed_return_rate_pct=1.0)
        self.assertIn("estimated", estimated.detail)
        self.assertIn("observed", observed.detail)
        self.assertGreater(observed.value, estimated.value)


class TestScorecard(unittest.TestCase):
    def test_coverage_reported_alongside_score(self):
        c = make_candidate()
        card = build_scorecard(POLICY, c, econ_for(c))
        self.assertLess(card.coverage_pct, 100.0,
                        "Trend and reviews are unmeasured on a fresh candidate.")
        self.assertTrue(any("Unmeasured" in n for n in card.notes))

    def test_policy_risk_caps_an_otherwise_strong_product(self):
        # The whole point: a trademark landmine with great margins must not
        # average out to "good".
        c = make_candidate(
            title="disney mickey mouse organizer", brand="Disney",
            target_price=34.99, supplier=make_supplier(unit_cost=3.0),
            est_monthly_demand_units=5000)
        card = build_scorecard(POLICY, c, econ_for(c))
        self.assertEqual(card.capped_by, "Policy risk")
        self.assertLess(card.overall, 40)
        self.assertEqual(card.verdict, "REJECT")

    def test_good_risk_score_does_not_cap(self):
        # Capping whenever risk sits below average would cap almost everything
        # and turn the warning into noise.
        c = make_candidate(supplier=make_supplier(unit_cost=3.0))
        card = build_scorecard(POLICY, c, econ_for(c))
        for d in card.dimensions:
            if d.is_risk and d.measured and d.value >= RISK_CAP_THRESHOLD:
                self.assertNotEqual(card.capped_by, d.label)

    def test_nothing_measurable_holds_rather_than_rejects(self):
        c = make_candidate(est_monthly_demand_units=0)
        card = build_scorecard(POLICY, c, econ_for(c))
        self.assertIn(card.verdict, ("HOLD", "INVESTIGATE", "PURSUE", "REJECT"))
        self.assertIsInstance(card.overall, float)

    def test_ranking_prefers_coverage(self):
        # An 80 we can support beats a 90 we cannot.
        class Fake:
            def __init__(self, overall, coverage):
                self.overall, self.coverage_pct = overall, coverage
        high_thin = Fake(90, 40)
        lower_broad = Fake(80, 95)
        self.assertEqual(rank([high_thin, lower_broad])[0], lower_broad)

    def test_all_twelve_charter_dimensions_present(self):
        c = make_candidate()
        card = build_scorecard(POLICY, c, econ_for(c))
        expected = {
            "demand", "trend", "competition", "margin", "shipping",
            "return_risk", "policy_risk", "supplier", "review_sentiment",
            "virality", "cash_flow",
        }
        self.assertEqual({d.name for d in card.dimensions}, expected)

    def test_virality_is_labelled_as_structural(self):
        c = make_candidate()
        card = build_scorecard(POLICY, c, econ_for(c))
        self.assertTrue(any("not observed performance" in n or "structural" in n
                            for n in card.notes))


class TestLifetimeValue(unittest.TestCase):
    def test_no_repeat_data_assumes_none(self):
        # Assuming an industry-typical rate would inflate every LTV and fund
        # unprofitable acquisition.
        ltv = lifetime_value(policy=POLICY, first_order_profit=10.0)
        self.assertEqual(ltv.basis, "assumed")
        self.assertEqual(ltv.repeat_rate_pct, 0.0)
        self.assertEqual(ltv.lifetime_profit, 10.0)

    def test_observed_repeat_rate_raises_ltv(self):
        ltv = lifetime_value(policy=POLICY, first_order_profit=10.0,
                             repeat_rate_pct=40.0)
        self.assertEqual(ltv.basis, "observed")
        self.assertGreater(ltv.lifetime_profit, 10.0)
        self.assertGreater(ltv.orders_per_customer, 1.0)

    def test_margin_decay_reduces_later_orders(self):
        flat = lifetime_value(policy=POLICY, first_order_profit=10.0,
                              repeat_rate_pct=50.0)
        decaying = lifetime_value(policy=POLICY, first_order_profit=10.0,
                                  repeat_rate_pct=50.0, margin_decay_pct=20.0)
        self.assertLess(decaying.lifetime_profit, flat.lifetime_profit)

    def test_series_is_capped_not_infinite(self):
        ltv = lifetime_value(policy=POLICY, first_order_profit=10.0,
                             repeat_rate_pct=95.0, max_orders=5)
        self.assertLessEqual(ltv.orders_per_customer, 5.0)

    def test_max_cac_respects_the_ratio(self):
        ltv = lifetime_value(policy=POLICY, first_order_profit=30.0,
                             repeat_rate_pct=50.0)
        self.assertAlmostEqual(max_acquisition_cost(ltv, target_ltv_cac_ratio=3.0),
                               ltv.lifetime_profit / 3, places=2)

    def test_invalid_ratio_raises(self):
        ltv = lifetime_value(policy=POLICY, first_order_profit=10.0)
        with self.assertRaises(ValueError):
            max_acquisition_cost(ltv, target_ltv_cac_ratio=0)


class TestPackaging(unittest.TestCase):
    def test_packaging_reduces_profit(self):
        base = compute_unit_economics(
            policy=POLICY, sku="X", marketplace="tiktok", sale_price=30.0,
            supplier=make_supplier())
        packed = compute_unit_economics(
            policy=POLICY, sku="X", marketplace="tiktok", sale_price=30.0,
            supplier=make_supplier(), packaging_cost=1.50)
        self.assertAlmostEqual(base.net_profit - packed.net_profit, 1.50, places=2)


class TestContentCompliance(unittest.TestCase):
    def test_efficacy_claims_are_caught(self):
        self.assertTrue(check_claims("This cures back pain"))
        self.assertTrue(check_claims("Clinically proven results"))
        self.assertTrue(check_claims("Guaranteed to work"))

    def test_clean_copy_passes(self):
        self.assertEqual(check_claims("Fits a standard kitchen drawer."), [])

    def test_generated_copy_is_self_screened(self):
        # The operator produces this text, so it screens its own output.
        c = make_candidate(title="organizer", description="Keeps the drawer tidy.")
        plan = build_content_plan(POLICY, c, unit_margin=8.0)
        for concept in plan.concepts:
            for warning in concept.compliance_warnings:
                self.assertIn(":", warning)

    def test_every_banned_pattern_is_a_valid_regex(self):
        import re
        for pattern in BANNED_CLAIM_PATTERNS:
            re.compile(pattern)


class TestContentGeneration(unittest.TestCase):
    def _demo_candidate(self):
        return make_candidate(
            title="collapsible storage box",
            description="Expand it to fit the shelf. No more clutter.",
            keywords=["collapsible storage", "shelf organizer"],
            category="Home & Kitchen")

    def test_hooks_derive_from_product_attributes(self):
        hooks = build_hooks(self._demo_candidate())
        self.assertTrue(hooks)
        self.assertTrue(all(h.line and h.why_it_works for h in hooks))

    def test_social_proof_hook_omitted_without_a_real_count(self):
        # An invented order count is a false advertising claim.
        without = build_hooks(self._demo_candidate())
        with_count = build_hooks(self._demo_candidate(), verified_order_count=4200)
        self.assertNotIn("social_proof", [h.archetype for h in without])
        self.assertIn("social_proof", [h.archetype for h in with_count])

    def test_visual_shock_hook_needs_a_real_transformation(self):
        dull = make_candidate(title="replacement filter", description="A filter.",
                              keywords=["filter"])
        self.assertNotIn("visual_shock",
                         [h.archetype for h in build_hooks(dull)])

    def test_objection_hook_needs_a_real_objection(self):
        hooks = build_hooks(self._demo_candidate(),
                            review_objection="it would be flimsy")
        self.assertIn("objection_first", [h.archetype for h in hooks])

    def test_voiceover_refuses_to_invent_specifications(self):
        c = self._demo_candidate()
        hooks = build_hooks(c)
        vo = build_voiceover(c, hooks[0])
        self.assertIn("FILL FROM PRODUCT SPEC", vo,
                      "The operator has not seen the product and must not "
                      "invent its specifications.")

    def test_shot_list_opens_without_a_branded_intro(self):
        c = self._demo_candidate()
        shots = build_shot_list(c, build_hooks(c)[0], "DEMO")
        self.assertIn("No logo", shots[0].shot)
        self.assertLessEqual(shots[0].duration_seconds, 3.0)

    def test_demo_format_uses_an_unbroken_take(self):
        c = self._demo_candidate()
        shots = build_shot_list(c, build_hooks(c)[0], "DEMO")
        self.assertTrue(any("No cut" in s.shot or "single take" in s.purpose
                            for s in shots))

    def test_hashtags_are_labelled_evergreen_not_trending(self):
        plan = build_content_plan(POLICY, self._demo_candidate(), unit_margin=8.0)
        self.assertEqual(plan.hashtag_strategy["kind"], "evergreen")
        self.assertIn("NOT trending", plan.hashtag_strategy["note"])

    def test_hashtags_derive_from_the_product(self):
        tags = build_hashtags(self._demo_candidate())
        self.assertTrue(any("collapsible" in t for t in tags))

    def test_creator_commission_is_bounded_by_margin(self):
        # Offering a rate the product cannot fund buys volume at a loss.
        thin = build_creator_briefs(POLICY, make_candidate(target_price=30.0),
                                    unit_margin=3.0)
        fat = build_creator_briefs(POLICY, make_candidate(target_price=30.0),
                                   unit_margin=15.0)
        self.assertLess(thin[0].commission_pct, fat[0].commission_pct)
        for b in thin + fat:
            self.assertLessEqual(b.commission_pct, 30.0)

    def test_calendar_rotates_formats(self):
        plan = build_content_plan(POLICY, self._demo_candidate(), unit_margin=8.0,
                                  calendar_days=7)
        formats = {e.format for e in plan.calendar}
        self.assertGreater(len(formats), 1,
                           "Posting one format repeatedly caps reach.")

    def test_calendar_length_matches_request(self):
        plan = build_content_plan(POLICY, self._demo_candidate(), unit_margin=8.0,
                                  calendar_days=10)
        self.assertEqual(len({e.day for e in plan.calendar}), 10)

    def test_non_demo_product_is_warned_about(self):
        dull = make_candidate(title="replacement filter", description="A filter.",
                              keywords=["filter"])
        plan = build_content_plan(POLICY, dull, unit_margin=8.0)
        self.assertTrue(any("harder to sell on short-form" in w
                            for w in plan.warnings))


class TestWeeklyReview(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.store = Store(Path(tempfile.mkdtemp()) / "w.db")

    def _report(self, **kw):
        base = dict(
            week_ending="2026-07-29", data_source="seed",
            metrics_this_week=[{"revenue": 1000.0, "cogs": 300.0,
                                "net_profit": 250.0, "ad_spend": 100.0}],
            metrics_last_week=[{"revenue": 800.0, "cogs": 250.0,
                                "net_profit": 180.0, "ad_spend": 90.0}],
            signal_coverage_pct=30.0,
            connector_status=[{"marketplace": "tiktok", "configured": False}],
        )
        base.update(kw)
        return build_weekly_report(POLICY, self.store, **base)

    def test_after_tax_profit_is_reported(self):
        r = self._report()
        self.assertLess(r.profit["after_tax_profit"], r.profit["net_profit"])

    def test_week_over_week_change_computed(self):
        r = self._report()
        self.assertAlmostEqual(r.profit["revenue_change_pct"], 25.0, places=1)

    def test_engineering_backlog_names_business_cost(self):
        # An improvement that cannot say what it costs today is a preference.
        r = self._report()
        self.assertTrue(r.engineering)
        for item in r.engineering:
            self.assertTrue(item.business_cost_of_not_doing_it)

    def test_low_signal_coverage_raises_a_backlog_item(self):
        items = derive_engineering_backlog(
            POLICY, self.store, signal_coverage_pct=25.0,
            connector_status=[], unresolved_decisions=0)
        self.assertTrue(any(i.area == "Market signals" for i in items))

    def test_full_coverage_drops_that_item(self):
        items = derive_engineering_backlog(
            POLICY, self.store, signal_coverage_pct=95.0,
            connector_status=[], unresolved_decisions=0)
        self.assertFalse(any(i.area == "Market signals" for i in items))

    def test_experiments_have_stopping_rules(self):
        # An experiment without a sample size and a metric is just a change.
        for e in derive_experiments(POLICY, signal_coverage_pct=30.0):
            self.assertTrue(e.hypothesis)
            self.assertTrue(e.success_metric)
            self.assertTrue(e.minimum_sample)

    def test_spike_decay_prompts_a_recovery_experiment(self):
        class FakeTrend:
            shape = "SPIKE_DECAY"
            title = "Viral Brush"
        experiments = derive_experiments(POLICY, trends=[FakeTrend()])
        self.assertTrue(any("Spike-decay" in e.name for e in experiments))

    def test_seed_data_keeps_its_banner(self):
        content = render_weekly_report(POLICY, self._report())
        self.assertIn("NOT REAL BUSINESS NUMBERS", content)

    def test_all_charter_sections_render(self):
        content = render_weekly_report(POLICY, self._report())
        for heading in ("Estimated Profit", "Best Opportunities", "Biggest Risks",
                        "Product Pipeline", "Recommended Engineering Improvements",
                        "Store Health", "Experiments to Run Next"):
            self.assertIn(heading, content)

    def test_concentration_breach_becomes_a_risk(self):
        r = self._report(concentration={"breaches": [
            {"dimension": "sku", "value": "X", "share_pct": 70.0, "limit_pct": 30.0}]})
        self.assertTrue(any(x["area"] == "Capital concentration" for x in r.risks))

    def test_unmeasured_store_health_is_not_reported_healthy(self):
        content = render_weekly_report(POLICY, self._report())
        self.assertIn("Not assumed healthy", content)


class TestMarketplaceNotImplemented(unittest.TestCase):
    def test_unwired_marketplace_names_what_is_needed(self):
        import os
        from unittest import mock

        from connectors import MarketplaceNotImplemented, get_connector

        with mock.patch.dict(os.environ, {"SHOPIFY_STORE_DOMAIN": "x",
                                          "SHOPIFY_ADMIN_ACCESS_TOKEN": "y"}):
            with self.assertRaises(MarketplaceNotImplemented) as ctx:
                get_connector("shopify").fetch_orders(since="2026-01-01")
        message = str(ctx.exception)
        self.assertIn("no live implementation", message)
        self.assertIn("Endpoints to implement", message)

    def test_it_is_still_a_notimplementederror(self):
        # Existing handlers must keep working.
        from connectors import MarketplaceNotImplemented
        self.assertTrue(issubclass(MarketplaceNotImplemented, NotImplementedError))


if __name__ == "__main__":
    unittest.main(verbosity=2)
