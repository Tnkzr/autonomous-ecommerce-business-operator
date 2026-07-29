"""Tests for the charter engines: tax, capital, signals, account health.

Same bias as the rest of the suite — the tests that matter most assert that
the system refuses, holds, or reports ignorance rather than guessing. The
charter's most expensive failure mode is not a missed opportunity; it is a
confident number with nothing behind it.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from operator_core.account_health import assess, blocks_scaling
from operator_core.capital import (
    AllocationRequest,
    CapitalState,
    Position,
    allocate,
    annualised_roi,
    concentration_report,
    portfolio_turns,
    reserves,
    risk_adjusted_roi,
)
from operator_core.config import PolicyError, load_policy
from operator_core.economics import (
    after_tax_profit,
    after_tax_roi_pct,
    cash_flow_projection,
    compute_unit_economics,
)
from operator_core.models import Decision, Severity, Supplier
from operator_core.screening import screen_candidate
from operator_core.signals import (
    ConfidenceAssessment,
    Signal,
    SignalDirection,
    SourceState,
    assess_confidence,
    available_weight,
    signal_from_sales_rank,
    source_statuses,
    unavailable_sources_report,
)
from operator_core.suppliers import (
    backup_supplier_recommendation,
    detect_deterioration,
    score_suppliers,
)
from tests.test_operator import make_candidate, make_supplier

POLICY = load_policy()
TODAY = date(2026, 7, 28)


def sig(source: str, direction: str = "POSITIVE", strength: float = 70.0,
        days_ago: int = 1) -> Signal:
    observed = date.fromordinal(TODAY.toordinal() - days_ago).isoformat()
    return Signal(source=source, direction=SignalDirection(direction),
                  strength=strength, observed_at=observed, detail="test")


class TestTax(unittest.TestCase):
    def test_after_tax_reduces_profit(self):
        self.assertAlmostEqual(after_tax_profit(POLICY, 100.0), 79.0, places=2)

    def test_losses_are_not_refunded(self):
        # Assuming a tax refund on a loss would flatter a bad product.
        self.assertEqual(after_tax_profit(POLICY, -50.0), -50.0)

    def test_after_tax_roi_below_pre_tax(self):
        e = compute_unit_economics(
            policy=POLICY, sku="X", marketplace="amazon", sale_price=34.99,
            supplier=make_supplier(), duty_pct=5.0, ad_cost_per_unit=3.0,
        )
        self.assertLess(after_tax_roi_pct(POLICY, e), e.roi_pct)

    def test_impossible_tax_rate_rejected(self):
        import tempfile
        bad = Path(tempfile.mkdtemp()) / "bad.toml"
        bad.write_text(POLICY.source_path.read_text().replace(
            "income_tax_rate_pct = 21.0", "income_tax_rate_pct = 140.0"))
        with self.assertRaises(PolicyError):
            load_policy(bad)


class TestCashFlow(unittest.TestCase):
    def test_cash_out_precedes_cash_in(self):
        sup = make_supplier(shipping_days=30)
        e = compute_unit_economics(
            policy=POLICY, sku="X", marketplace="amazon", sale_price=34.99,
            supplier=sup, duty_pct=5.0,
        )
        cf = cash_flow_projection(policy=POLICY, unit=e, supplier=sup,
                                  order_units=500, monthly_demand_units=100)
        self.assertGreater(cf["cash_out_today"], 0)
        self.assertGreater(cf["days_to_first_cash"], 0)
        self.assertGreater(cf["days_to_full_recovery"], cf["days_to_first_cash"],
                           "Full recovery must lag the first dollar back.")

    def test_slower_sellthrough_lengthens_recovery(self):
        sup = make_supplier()
        e = compute_unit_economics(policy=POLICY, sku="X", marketplace="amazon",
                                   sale_price=30.0, supplier=sup)
        fast = cash_flow_projection(policy=POLICY, unit=e, supplier=sup,
                                    order_units=300, monthly_demand_units=300)
        slow = cash_flow_projection(policy=POLICY, unit=e, supplier=sup,
                                    order_units=300, monthly_demand_units=50)
        self.assertGreater(slow["days_to_full_recovery"], fast["days_to_full_recovery"])

    def test_payment_terms_shorten_the_cycle(self):
        sup = make_supplier()
        e = compute_unit_economics(policy=POLICY, sku="X", marketplace="amazon",
                                   sale_price=30.0, supplier=sup)
        none = cash_flow_projection(policy=POLICY, unit=e, supplier=sup,
                                    order_units=200, monthly_demand_units=100)
        net30 = cash_flow_projection(policy=POLICY, unit=e, supplier=sup,
                                     order_units=200, monthly_demand_units=100,
                                     payment_terms_days=30)
        self.assertLess(net30["days_to_full_recovery"], none["days_to_full_recovery"])


class TestSignals(unittest.TestCase):
    def test_unconnected_sources_are_reported_not_assumed(self):
        statuses = source_statuses(POLICY)
        unavailable = [s for s in statuses if s.state is SourceState.UNAVAILABLE]
        self.assertTrue(unavailable, "Most sources have no connector; that must show.")
        self.assertLess(available_weight(POLICY), 100.0)

    def test_unavailable_report_says_what_is_needed(self):
        lines = unavailable_sources_report(POLICY)
        self.assertTrue(lines)
        self.assertTrue(any("Brand Registry" in l for l in lines),
                        "The gap must be actionable, not just noted.")

    def test_no_signals_scores_zero_not_neutral(self):
        # "We know nothing" must not be recorded as "it is average".
        a = assess_confidence(POLICY, "X", [], today=TODAY)
        self.assertEqual(a.score, 0.0)
        self.assertEqual(a.coverage_pct, 0.0)
        self.assertFalse(a.sufficient)
        self.assertTrue(any("not because the product is bad" in w for w in a.warnings))

    def test_single_signal_fails_corroboration(self):
        a = assess_confidence(POLICY, "X", [sig("amazon_sales_rank")], today=TODAY)
        self.assertFalse(a.meets_signal_minimum)
        self.assertFalse(a.sufficient)

    def test_three_positive_signals_can_pass(self):
        a = assess_confidence(POLICY, "X", [
            sig("amazon_sales_rank", strength=80),
            sig("google_trends", strength=75),
            sig("social_reddit", strength=70),
        ], today=TODAY)
        self.assertTrue(a.meets_signal_minimum)
        self.assertGreater(a.score, 60)
        self.assertTrue(a.sufficient)

    def test_stale_signals_are_dropped(self):
        stale = sig("amazon_sales_rank", days_ago=400)
        a = assess_confidence(POLICY, "X", [stale], today=TODAY)
        self.assertEqual(a.positive_signals, 0)
        self.assertTrue(any("old" in w for w in a.warnings))

    def test_unknown_source_is_ignored_with_a_warning(self):
        a = assess_confidence(POLICY, "X", [sig("astrology")], today=TODAY)
        self.assertEqual(a.positive_signals, 0)
        self.assertTrue(any("unknown source" in w for w in a.warnings))

    def test_negative_signals_lower_the_score(self):
        pos = assess_confidence(POLICY, "X", [
            sig("amazon_sales_rank", strength=80), sig("google_trends", strength=80),
            sig("social_reddit", strength=80),
        ], today=TODAY)
        mixed = assess_confidence(POLICY, "X", [
            sig("amazon_sales_rank", strength=80), sig("google_trends", strength=80),
            sig("social_reddit", strength=80),
            sig("news_regulatory", direction="NEGATIVE", strength=90),
        ], today=TODAY)
        self.assertLess(mixed.score, pos.score)
        self.assertEqual(mixed.negative_signals, 1)

    def test_effective_score_discounts_thin_coverage(self):
        a = assess_confidence(POLICY, "X", [
            sig("social_pinterest", strength=100), sig("social_youtube", strength=100),
            sig("social_reddit", strength=100),
        ], today=TODAY)
        self.assertLess(a.effective_score, a.score,
                        "A high score from a sliver of sources is not a high score.")

    def test_sales_rank_maps_to_signal(self):
        strong = signal_from_sales_rank(500)
        weak = signal_from_sales_rank(800_000)
        self.assertEqual(strong.direction, SignalDirection.POSITIVE)
        self.assertEqual(weak.direction, SignalDirection.NEGATIVE)
        self.assertGreater(strong.strength, weak.strength)

    def test_missing_rank_is_neutral_not_negative(self):
        s = signal_from_sales_rank(0)
        self.assertEqual(s.direction, SignalDirection.NEUTRAL)


class TestScreeningWithEvidence(unittest.TestCase):
    def _clean(self):
        return make_candidate(target_price=34.99, supplier=make_supplier(unit_cost=4.0))

    def test_thin_evidence_holds_rather_than_approves(self):
        weak = assess_confidence(POLICY, "TEST-1", [sig("amazon_sales_rank")], today=TODAY)
        r = screen_candidate(POLICY, self._clean(), confidence=weak)
        self.assertEqual(r.decision, Decision.HOLD)
        self.assertTrue(any("do not know yet" in n for n in r.notes))

    def test_strong_evidence_reaches_approval(self):
        strong = assess_confidence(POLICY, "TEST-1", [
            sig("amazon_sales_rank", strength=85), sig("google_trends", strength=80),
            sig("social_reddit", strength=75),
        ], today=TODAY)
        r = screen_candidate(POLICY, self._clean(), confidence=strong)
        self.assertEqual(r.decision, Decision.NEEDS_HUMAN_APPROVAL)

    def test_evidence_never_rescues_a_compliance_failure(self):
        strong = assess_confidence(POLICY, "TEST-1", [
            sig("amazon_sales_rank", strength=95), sig("google_trends", strength=95),
            sig("social_reddit", strength=95),
        ], today=TODAY)
        hazmat = make_candidate(title="lantern with lithium battery",
                                supplier=make_supplier(unit_cost=2.0))
        r = screen_candidate(POLICY, hazmat, confidence=strong)
        self.assertEqual(r.decision, Decision.REJECT)

    def test_confidence_discounts_ranking_score(self):
        strong = assess_confidence(POLICY, "TEST-1", [
            sig("amazon_sales_rank", strength=85), sig("google_trends", strength=80),
            sig("social_reddit", strength=75),
        ], today=TODAY)
        with_evidence = screen_candidate(POLICY, self._clean(), confidence=strong)
        without = screen_candidate(POLICY, self._clean())
        self.assertLess(with_evidence.score, without.score,
                        "Evidence-weighted ranking must not exceed the raw score.")


class TestCapital(unittest.TestCase):
    def _state(self, **kw):
        base = dict(
            total_capital_usd=25000.0, cash_available_usd=15000.0,
            positions=[
                Position("A", "Home", "S1", 200, 0, 10.0, annual_units_sold=800),
                Position("B", "Pet", "S2", 100, 0, 5.0, annual_units_sold=400),
            ],
        )
        base.update(kw)
        return CapitalState(**base)

    def test_reserves_are_held_back(self):
        r = reserves(POLICY)
        self.assertGreater(r["total"], 0)
        self.assertLess(r["deployable"], float(POLICY.capital["total_capital_usd"]))

    def test_risk_adjusted_roi_punishes_low_confidence(self):
        confident = AllocationRequest("A", "C", "S", 1000, 100.0, 90.0, 90)
        unsure = AllocationRequest("B", "C", "S", 1000, 100.0, 30.0, 90)
        self.assertGreater(risk_adjusted_roi(confident), risk_adjusted_roi(unsure))

    def test_risk_adjusted_roi_punishes_slow_cash(self):
        fast = AllocationRequest("A", "C", "S", 1000, 80.0, 80.0, 60)
        slow = AllocationRequest("B", "C", "S", 1000, 80.0, 80.0, 240)
        self.assertGreater(risk_adjusted_roi(fast), risk_adjusted_roi(slow))

    def test_annualised_roi_rewards_fast_turns(self):
        fast = AllocationRequest("A", "C", "S", 1000, 40.0, 90.0, 60)
        slow = AllocationRequest("B", "C", "S", 1000, 40.0, 90.0, 365)
        self.assertGreater(annualised_roi(fast), annualised_roi(slow))

    def test_low_risk_adjusted_return_is_refused(self):
        req = AllocationRequest("A", "Home", "S1", 1000, 30.0, 20.0, 300)
        decisions, _ = allocate(POLICY, self._state(), [req])
        self.assertFalse(decisions[0].accepted)
        self.assertIn("under the", decisions[0].reasons[0])

    def test_concentration_partially_funds_a_good_opportunity(self):
        # The point of the limit: it binds even when the product is excellent.
        # In a diversified portfolio there is headroom, so the request is
        # trimmed to the limit rather than refused outright.
        state = self._state(positions=[
            Position("A", "Home", "S1", 200, 0, 10.0, annual_units_sold=800),
            Position("B", "Pet", "S2", 300, 0, 10.0, annual_units_sold=1200),
            Position("C", "Toys", "S3", 300, 0, 10.0, annual_units_sold=1200),
        ])
        req = AllocationRequest("A", "Home", "S1", 10000, 200.0, 95.0, 60)
        decisions, _ = allocate(POLICY, state, [req])
        d = decisions[0]
        self.assertGreater(d.approved_amount, 0, "Headroom exists; fund up to it.")
        self.assertLess(d.approved_amount, req.amount_usd)
        self.assertTrue(any("concentration limit" in r for r in d.reasons))

        # The funded amount must land exactly on the limit, not past it.
        limit = float(POLICY.capital["max_single_sku_share_pct"])
        new_a = 2000.0 + d.approved_amount
        new_total = state.deployed_usd + d.approved_amount
        self.assertLessEqual(new_a / new_total * 100, limit + 0.1)

    def test_already_over_limit_cannot_be_fixed_by_buying_more(self):
        # A SKU that is already the whole portfolio can never be brought under
        # the limit by adding to it — the fix is buying something else.
        state = self._state(positions=[
            Position("A", "Home", "S1", 500, 0, 10.0, annual_units_sold=2000),
        ])
        req = AllocationRequest("A", "Home", "S1", 10000, 200.0, 95.0, 60)
        decisions, _ = allocate(POLICY, state, [req])
        self.assertFalse(decisions[0].accepted)
        self.assertEqual(decisions[0].approved_amount, 0.0)
        self.assertTrue(any("over the 30% limit" in r for r in decisions[0].reasons))

    def test_unknown_grouping_is_flagged_not_enforced(self):
        # Everything landing in an "unknown" bucket is a bookkeeping gap, not a
        # real exposure — blocking on it would stop all allocation.
        state = self._state(positions=[
            Position("A", "uncategorised", "unknown", 100, 0, 10.0, annual_units_sold=400),
        ])
        req = AllocationRequest("B", "uncategorised", "unknown", 2000, 90.0, 90.0, 60)
        decisions, _ = allocate(POLICY, state, [req])
        self.assertTrue(decisions[0].accepted)
        self.assertTrue(any("could not be checked" in r for r in decisions[0].reasons))

    def test_allocation_respects_deployable_cash(self):
        state = self._state(cash_available_usd=1000.0)
        reqs = [
            AllocationRequest("A", "Home", "S1", 900, 120.0, 90.0, 60),
            AllocationRequest("C", "Toys", "S3", 900, 110.0, 90.0, 60),
        ]
        decisions, _ = allocate(POLICY, state, reqs)
        self.assertLessEqual(sum(d.approved_amount for d in decisions), 1000.0)

    def test_higher_risk_adjusted_return_ranks_first(self):
        reqs = [
            AllocationRequest("slow", "Home", "S1", 1000, 100.0, 50.0, 300),
            AllocationRequest("fast", "Pet", "S2", 1000, 100.0, 95.0, 60),
        ]
        decisions, _ = allocate(POLICY, self._state(), reqs)
        self.assertEqual(decisions[0].request.sku, "fast")

    def test_concentration_report_flags_breach(self):
        state = self._state(positions=[
            Position("BIG", "Home", "S1", 1000, 0, 10.0, annual_units_sold=100),
            Position("small", "Pet", "S2", 10, 0, 1.0, annual_units_sold=40),
        ])
        rep = concentration_report(POLICY, state)
        self.assertTrue(rep["breaches"])
        self.assertEqual(rep["breaches"][0]["value"], "BIG")

    def test_turns_use_held_units_not_half(self):
        p = Position("A", "C", "S", 100, 0, 10.0, annual_units_sold=400)
        self.assertEqual(p.turns_per_year(), 4.0,
                         "Halving inventory doubles reported turns and flatters "
                         "sleeping capital.")

    def test_dead_stock_is_identified(self):
        state = self._state(positions=[
            Position("DEAD", "Home", "S1", 300, 0, 6.0, annual_units_sold=0),
        ])
        t = portfolio_turns(POLICY, state)
        self.assertIn("DEAD", t["dead"])
        self.assertGreater(t["capital_in_dead"], 0)


class TestAccountHealth(unittest.TestCase):
    def test_missing_metrics_are_unknown_not_healthy(self):
        a = assess(POLICY, "amazon", {})
        self.assertEqual(a.coverage_pct, 0.0)
        self.assertTrue(a.unknown)
        self.assertTrue(any("not healthy" in x for x in a.actions))

    def test_breach_is_critical(self):
        a = assess(POLICY, "amazon", {"order_defect_rate_pct": 2.5})
        self.assertEqual(a.severity, Severity.CRITICAL)
        self.assertTrue(a.breaches)
        self.assertFalse(a.ok)

    def test_approaching_limit_warns_before_breach(self):
        # 0.8 of a 1.0 limit is 80%, over the 75% warn line.
        a = assess(POLICY, "amazon", {"order_defect_rate_pct": 0.8})
        self.assertEqual(a.severity, Severity.WARN)
        self.assertFalse(a.breaches)
        self.assertTrue(a.warnings)

    def test_healthy_metrics_pass(self):
        a = assess(POLICY, "amazon", {
            "order_defect_rate_pct": 0.2, "late_shipment_rate_pct": 0.5,
            "pre_fulfilment_cancel_rate_pct": 0.1, "invalid_tracking_rate_pct": 0.2,
            "return_dissatisfaction_rate_pct": 1.0, "account_health_rating": 900,
        })
        self.assertEqual(a.severity, Severity.INFO)
        self.assertTrue(a.ok)

    def test_ahr_is_higher_is_better(self):
        low = assess(POLICY, "amazon", {"account_health_rating": 100})
        self.assertTrue(low.breaches, "An AHR below the floor is a breach, not a pass.")

    def test_breach_blocks_scaling(self):
        a = assess(POLICY, "amazon", {"order_defect_rate_pct": 2.0})
        blocked, why = blocks_scaling(a)
        self.assertTrue(blocked)
        self.assertIn("more defects", why)

    def test_healthy_account_permits_scaling(self):
        a = assess(POLICY, "amazon", {
            "order_defect_rate_pct": 0.1, "late_shipment_rate_pct": 0.2,
            "pre_fulfilment_cancel_rate_pct": 0.1, "invalid_tracking_rate_pct": 0.1,
            "return_dissatisfaction_rate_pct": 1.0, "account_health_rating": 950,
        })
        blocked, _ = blocks_scaling(a)
        self.assertFalse(blocked)

    def test_remedy_is_specific_not_generic(self):
        a = assess(POLICY, "amazon", {"late_shipment_rate_pct": 5.0})
        self.assertTrue(any("handling time" in x for x in a.actions),
                        "Generic 'improve your metrics' advice is useless.")


class TestSupplierDeterioration(unittest.TestCase):
    def _history(self, scores, defects=None, on_time=None):
        return [
            {"supplier_id": "S1", "supplier_name": "Acme", "score": s,
             "defect_rate_pct": (defects[i] if defects else 1.0),
             "on_time_rate_pct": (on_time[i] if on_time else 95.0)}
            for i, s in enumerate(scores)
        ]

    def test_too_little_history_produces_no_alerts(self):
        self.assertEqual(detect_deterioration(self._history([80, 79])), [])

    def test_declining_score_alerts(self):
        alerts = detect_deterioration(self._history([85, 84, 70, 66]))
        self.assertTrue(any(a.metric == "scorecard" for a in alerts))

    def test_stable_supplier_is_quiet(self):
        self.assertEqual(detect_deterioration(self._history([80, 81, 80, 79])), [])

    def test_defect_ceiling_is_critical(self):
        alerts = detect_deterioration(
            self._history([80, 80, 80, 80], defects=[1, 1, 1, 8.0]))
        critical = [a for a in alerts if a.severity == "CRITICAL"]
        self.assertTrue(critical)
        self.assertIn("Hold the next order", critical[0].recommendation)

    def test_late_deliveries_alert(self):
        alerts = detect_deterioration(
            self._history([80, 80, 80, 80], on_time=[95, 93, 88, 70]))
        self.assertTrue(any(a.metric == "on_time_rate" for a in alerts))

    def test_backup_supplier_recommended(self):
        a = make_supplier(supplier_id="A", name="Primary", unit_cost=5.0)
        b = make_supplier(supplier_id="B", name="Second", unit_cost=6.0)
        scored = score_suppliers(POLICY, [a, b])
        backup, note = backup_supplier_recommendation(scored, chosen_id="A")
        self.assertIsNotNone(backup)
        self.assertEqual(backup.supplier.supplier_id, "B")

    def test_single_source_is_called_out(self):
        only = score_suppliers(POLICY, [make_supplier(supplier_id="A")])
        backup, note = backup_supplier_recommendation(only, chosen_id="A")
        self.assertIsNone(backup)
        self.assertIn("single-sourced", note)


class TestCharterPolicyValidation(unittest.TestCase):
    def _mutate(self, old: str, new: str):
        import tempfile
        p = Path(tempfile.mkdtemp()) / "p.toml"
        p.write_text(POLICY.source_path.read_text().replace(old, new))
        return p

    def test_reserves_cannot_consume_all_capital(self):
        with self.assertRaises(PolicyError):
            load_policy(self._mutate("reserve_advertising_pct = 15.0",
                                     "reserve_advertising_pct = 95.0"))

    def test_signal_weights_must_normalise(self):
        with self.assertRaises(PolicyError):
            load_policy(self._mutate("amazon_sales_rank = 0.30",
                                     "amazon_sales_rank = 0.90"))

    def test_min_signals_must_be_at_least_one(self):
        with self.assertRaises(PolicyError):
            load_policy(self._mutate("min_positive_signals = 3",
                                     "min_positive_signals = 0"))

    def test_warn_threshold_must_leave_reaction_time(self):
        with self.assertRaises(PolicyError):
            load_policy(self._mutate("warn_at_pct_of_limit = 75.0",
                                     "warn_at_pct_of_limit = 0.0"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
