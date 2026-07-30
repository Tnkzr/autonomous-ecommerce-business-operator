"""Growth loop tests.

The loop's job is to run engines in order, carry uncertainty forward, and stop
where a human has to decide. So the assertions are about ordering, idempotency,
and the boundary between "proposed" and "done" — a loop that quietly acts is
the failure this whole system is arranged to prevent.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from operator_core.config import load_policy
from operator_core.growth_pipeline import (
    GrowthRunResult,
    render_growth_run,
    run_growth_cycle,
)
from operator_core.models import Decision, ProductCandidate, Supplier
from operator_core.research import Opportunity, SourceReading
from operator_core.store import Store

POLICY = load_policy()

GOOD_SUPPLIER = Supplier(supplier_id="S1", name="Good Supplier", country="CN",
                         rating=4.9, unit_cost=4.10, moq=50,
                         shipping_cost_per_unit=1.20, shipping_days=9,
                         quality_score=88, communication_score=85,
                         inventory_stability=90, defect_rate_pct=0.7,
                         on_time_rate_pct=97)

BAD_SUPPLIER = Supplier(supplier_id="S2", name="Weak Supplier", country="CN",
                        rating=4.1, unit_cost=9.00, moq=500,
                        shipping_cost_per_unit=4.00, shipping_days=28,
                        defect_rate_pct=6.0, on_time_rate_pct=70)


def passing_candidate(sku: str = "GOOD-01") -> ProductCandidate:
    return ProductCandidate(
        sku=sku, title="Self cleaning slicker brush for dogs",
        category="Pet Supplies", marketplace="tiktok", target_price=24.99,
        supplier=GOOD_SUPPLIER, est_monthly_demand_units=1200,
        competitor_count=8, top_rival_review_count=900, avg_rival_price=27.0,
        avg_rival_rating=4.3, weight_lb=0.6,
        keywords=["slicker brush", "self cleaning", "dog grooming"],
        description="Self cleaning slicker brush that retracts the bristles")


def failing_candidate(sku: str = "BAD-01") -> ProductCandidate:
    return ProductCandidate(
        sku=sku, title="Lithium battery LED strip",
        category="Home", marketplace="tiktok", target_price=29.99,
        supplier=BAD_SUPPLIER, est_monthly_demand_units=200,
        competitor_count=90, top_rival_review_count=50000,
        keywords=["lithium battery", "led strip"],
        description="LED strip with a built in lithium battery")


class GrowthTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = TemporaryDirectory()
        self.store = Store(Path(self._dir.name) / "growth.db")

    def tearDown(self):
        self._dir.cleanup()

    def run_cycle(self, **kwargs) -> GrowthRunResult:
        kwargs.setdefault("run_date", "2026-08-01")
        return run_growth_cycle(POLICY, self.store, **kwargs)


class TestStageOrdering(GrowthTestCase):
    def test_all_stages_are_reported_even_when_skipped(self):
        result = self.run_cycle()
        names = [s.name for s in result.stages]
        self.assertEqual(names, ["research", "screen", "listing", "creative",
                                 "publishing", "measure", "learn"])

    def test_skipped_stages_carry_a_reason(self):
        result = self.run_cycle()
        for stage in result.stages:
            if not stage.ran:
                self.assertTrue(stage.skipped_reason.strip(), stage.name)

    def test_did_not_look_differs_from_looked_and_found_nothing(self):
        # The distinction the whole system is built around.
        empty = self.run_cycle()
        self.assertFalse(empty.stage("measure").ran)
        self.assertIn("not a funnel with a problem",
                      empty.stage("measure").skipped_reason)

        self.store.upsert_storefront_daily(
            metric_date="2026-08-01", channel="tiktok", orders=0, revenue=0.0,
            data_source="live")
        with_data = self.run_cycle()
        self.assertTrue(with_data.stage("measure").ran)

    def test_coverage_shortfall_is_always_warned(self):
        result = self.run_cycle()
        self.assertTrue(any("signal sources connected" in w
                            for w in result.warnings))


class TestScreening(GrowthTestCase):
    def test_failing_candidates_are_rejected_and_journaled(self):
        result = self.run_cycle(candidates=[failing_candidate()])
        stage = result.stage("screen")
        self.assertTrue(stage.ran)
        self.assertEqual(stage.payload["selected"], [])
        self.assertEqual(len(stage.payload["rejected"]), 1)
        journal = self.store.decisions_for_sku("BAD-01")
        self.assertTrue(journal)
        self.assertEqual(journal[0]["action"], "reject_candidate")

    def test_passing_candidate_proceeds_to_creative(self):
        result = self.run_cycle(candidates=[passing_candidate()])
        self.assertEqual(result.stage("screen").payload["selected"], ["GOOD-01"])
        self.assertTrue(result.stage("creative").ran)
        self.assertGreater(result.stage("creative").payload["packages"], 0)

    def test_needs_approval_still_gets_creative_built(self):
        # A candidate that passes every gate but routes the *spend* to a human
        # must still have creative generated — generating it is free and
        # reversible, and coupling the two makes the loop look inert.
        result = self.run_cycle(candidates=[passing_candidate()])
        self.assertTrue(result.stage("creative").ran)
        self.assertTrue(result.proposals)


class TestProposals(GrowthTestCase):
    def test_publishing_is_proposed_never_performed(self):
        result = self.run_cycle(candidates=[passing_candidate()])
        proposal = result.proposals[0]
        self.assertEqual(proposal["action"], "publish_product")
        self.assertTrue(proposal["requires_approval"])
        # It is in the journal awaiting approval, not executed.
        pending = self.store.pending_approvals()
        self.assertTrue(any(p["action_id"] == proposal["action_id"]
                            for p in pending))

    def test_proposal_is_made_even_with_no_shootable_creative(self):
        # Publishing a listing and finishing a video are independent tasks.
        result = self.run_cycle(candidates=[passing_candidate()])
        self.assertTrue(result.proposals)
        journal = [d for d in self.store.decisions_for_sku("GOOD-01")
                   if d["action"] == "publish_product"]
        self.assertTrue(journal)

    def test_proposal_rationale_names_the_irreversibility(self):
        self.run_cycle(candidates=[passing_candidate()])
        entry = next(d for d in self.store.decisions_for_sku("GOOD-01")
                     if d["action"] == "publish_product")
        self.assertIn("irreversible", entry["rationale"])

    def test_no_candidates_means_no_proposals(self):
        self.assertEqual(self.run_cycle().proposals, [])


class TestIdempotency(GrowthTestCase):
    def test_rerunning_the_same_day_does_not_duplicate_the_journal(self):
        # A loop that duplicates its own journal teaches the learning engine
        # that it does twice as much as it does.
        candidates = [passing_candidate(), failing_candidate()]
        self.run_cycle(candidates=candidates)
        first = len(self.store.recent_decisions(limit=1000))
        self.run_cycle(candidates=candidates)
        self.assertEqual(len(self.store.recent_decisions(limit=1000)), first)

    def test_a_different_day_does_journal_again(self):
        candidates = [passing_candidate()]
        self.run_cycle(candidates=candidates, run_date="2026-08-01")
        first = len(self.store.recent_decisions(limit=1000))
        self.run_cycle(candidates=candidates, run_date="2026-08-02")
        self.assertGreater(len(self.store.recent_decisions(limit=1000)), first)


class TestResearchStage(GrowthTestCase):
    def test_opportunities_are_ranked_when_supplied(self):
        opportunity = Opportunity(
            opportunity_id="O1", title="Magnetic organiser", category="Home",
            stage="SCORED", discovered_at="2026-07-30",
            readings=[
                SourceReading("tiktok_product_velocity", "positive", 0.8,
                              origin="live"),
                SourceReading("amazon_sales_rank", "positive", 0.7, origin="live"),
            ])
        result = self.run_cycle(opportunities=[opportunity])
        stage = result.stage("research")
        self.assertTrue(stage.ran)
        self.assertEqual(stage.payload["active"], 1)

    def test_empty_pipeline_is_not_the_same_as_exhausted(self):
        stage = self.run_cycle().stage("research")
        self.assertIn("empty rather than exhausted", stage.skipped_reason)


class TestRendering(GrowthTestCase):
    def test_report_names_every_stage(self):
        rendered = render_growth_run(self.run_cycle(
            candidates=[passing_candidate()]))
        for name in ("RESEARCH", "SCREEN", "CREATIVE", "PUBLISHING", "MEASURE",
                     "LEARN"):
            self.assertIn(name, rendered)

    def test_proposals_are_marked_as_needing_approval(self):
        rendered = render_growth_run(self.run_cycle(
            candidates=[passing_candidate()]))
        self.assertIn("NEEDS APPROVAL", rendered)

    def test_duplicate_warnings_are_collapsed(self):
        result = self.run_cycle(candidates=[passing_candidate()])
        result.warnings.extend(["duplicate warning"] * 3)
        self.assertEqual(render_growth_run(result).count("duplicate warning"), 1)

    def test_summary_counts_are_consistent(self):
        result = self.run_cycle(candidates=[passing_candidate()])
        summary = result.summary()
        self.assertEqual(summary["stages_run"] + summary["stages_skipped"],
                         summary["stages_total"])
        self.assertEqual(summary["proposals"], len(result.proposals))


if __name__ == "__main__":
    unittest.main()


class TestListingStage(GrowthTestCase):
    """The `select → list on Shopify` step. A plan is not a write."""

    def test_a_shopify_product_plan_is_built_for_each_selection(self):
        result = self.run_cycle(candidates=[passing_candidate()])
        stage = result.stage("listing")
        self.assertTrue(stage.ran)
        plans = stage.payload["plans"]
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["sku"], "GOOD-01")

    def test_the_plan_is_always_a_draft(self):
        # Creating is only safe to do autonomously because a draft is
        # invisible and reversible.
        plan = self.run_cycle(
            candidates=[passing_candidate()]).stage("listing").payload["plans"][0]
        self.assertEqual(plan["product"]["status"], "DRAFT")

    def test_the_handle_is_pinned_across_runs(self):
        # The handle is the product URL. Every video points at it, so a handle
        # that changes between runs costs every view those videos earned.
        first = self.run_cycle(candidates=[passing_candidate()],
                               run_date="2026-08-01")
        second = self.run_cycle(candidates=[passing_candidate()],
                                run_date="2026-08-02")
        self.assertEqual(first.stage("listing").payload["plans"][0]["handle"],
                         second.stage("listing").payload["plans"][0]["handle"])

    def test_the_plan_carries_price_and_cost(self):
        plan = self.run_cycle(
            candidates=[passing_candidate()]).stage("listing").payload["plans"][0]
        self.assertEqual(plan["variant"]["price"], "24.99")
        self.assertIn("cost", plan["variant"]["inventoryItem"])

    def test_no_selection_means_no_listing_stage_run(self):
        stage = self.run_cycle(candidates=[failing_candidate()]).stage("listing")
        self.assertFalse(stage.ran)
        self.assertIn("per selected product", stage.skipped_reason)

    def test_handle_appears_in_the_publish_proposal(self):
        # Approving a publish should show which URL is about to go live.
        self.run_cycle(candidates=[passing_candidate()])
        entry = next(d for d in self.store.decisions_for_sku("GOOD-01")
                     if d["action"] == "publish_product")
        self.assertIn("handle", entry["inputs_json"])
