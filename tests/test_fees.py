"""Fee reconciliation tests.

`[fees.*]` decides which products clear the margin gate and where price floors
sit, so the assertions here are mostly about restraint: not concluding from a
thin sample, not churning the policy over noise, and never writing the file
automatically.
"""

from __future__ import annotations

import unittest

from operator_core.config import load_policy
from operator_core.fees import (
    MATERIAL_DRIFT_PCT,
    MIN_ORDERS,
    propose_policy_patch,
    reconcile_ebay,
    render,
)

POLICY = load_policy()


def sale(order_id: str, revenue: float = 24.99, *, fvf_pct: float = 13.25,
         fixed: float = 0.30, ad: float = 0.0,
         broken_out: bool = True) -> dict:
    txn: dict = {"transactionType": "SALE", "orderId": order_id,
                 "amount": {"value": f"{revenue:.2f}"}}
    if not broken_out:
        txn["totalFeeAmount"] = {"value": f"{revenue * fvf_pct / 100 + fixed:.2f}"}
        return txn
    fees = [
        {"feeType": "FINAL_VALUE_FEE",
         "amount": {"value": f"{revenue * fvf_pct / 100:.2f}"}},
        {"feeType": "FINAL_VALUE_FEE_FIXED_PER_ORDER",
         "amount": {"value": f"{fixed:.2f}"}},
    ]
    if ad:
        fees.append({"feeType": "AD_FEE", "amount": {"value": f"{ad:.2f}"}})
    txn["orderLineItems"] = [{"marketplaceFees": fees}]
    return txn


def many(count: int, **kw) -> list[dict]:
    return [sale(f"O{i}", **kw) for i in range(count)]


class TestReconciliation(unittest.TestCase):
    def test_take_rate_is_fees_over_revenue(self):
        result = reconcile_ebay(many(30), POLICY)
        self.assertEqual(result.orders, 30)
        self.assertAlmostEqual(result.realised_take_rate_pct, 14.45, places=1)

    def test_fee_types_are_broken_out(self):
        result = reconcile_ebay(many(30, ad=0.55), POLICY)
        types = {c.fee_type for c in result.components}
        self.assertIn("FINAL_VALUE_FEE", types)
        self.assertIn("AD_FEE", types)

    def test_components_are_ordered_by_size(self):
        result = reconcile_ebay(many(30, ad=0.55), POLICY)
        totals = [c.total for c in result.components]
        self.assertEqual(totals, sorted(totals, reverse=True))

    def test_every_component_explains_what_it_is(self):
        # "13.4% overall" hides that some of it is a choice and some a penalty.
        for component in reconcile_ebay(many(30, ad=0.55), POLICY).components:
            self.assertTrue(component.meaning, component.fee_type)

    def test_duplicate_order_ids_count_once(self):
        # Two transactions against one order are one order, and a per-order fee
        # divided by an inflated count understates it.
        txns = [sale("O1"), sale("O1"), sale("O2")]
        self.assertEqual(reconcile_ebay(txns, POLICY).orders, 2)

    def test_refunds_are_not_netted_off_revenue(self):
        txns = many(30) + [{"transactionType": "REFUND",
                            "amount": {"value": "24.99"}}]
        result = reconcile_ebay(txns, POLICY)
        self.assertAlmostEqual(result.gross_revenue, 24.99 * 30, places=2)
        self.assertAlmostEqual(result.refunds, 24.99, places=2)
        self.assertAlmostEqual(result.refund_rate_pct, 3.33, places=1)

    def test_non_sale_charges_are_counted_as_real_money(self):
        # Insertion fees are omitted from every take rate that only looks at
        # sales, which is how a catalogue of dead listings quietly costs money.
        txns = many(30) + [{"transactionType": "NON_SALE_CHARGE",
                            "feeType": "INSERTION_FEE",
                            "amount": {"value": "8.75"}}]
        result = reconcile_ebay(txns, POLICY)
        self.assertTrue(any(c.fee_type == "INSERTION_FEE"
                            for c in result.components))

    def test_rolled_up_fees_still_produce_a_correct_rate(self):
        result = reconcile_ebay(many(30, broken_out=False), POLICY)
        self.assertGreater(result.total_fees, 0)
        self.assertTrue(any("UNBROKEN_TOTAL" == c.fee_type
                            for c in result.components))
        self.assertTrue(any("attribution to specific fee types is not" in w
                            for w in result.warnings))

    def test_no_revenue_yields_none_not_zero(self):
        result = reconcile_ebay([], POLICY)
        self.assertIsNone(result.realised_take_rate_pct)
        self.assertIsNone(result.drift_pct)
        self.assertIsNone(result.refund_rate_pct)

    def test_ad_spend_is_called_out_as_a_choice(self):
        result = reconcile_ebay(many(30, ad=0.55), POLICY)
        self.assertTrue(any("choice, not a cost of trading" in w
                            for w in result.warnings))

    def test_penalty_fees_are_called_out_as_removable(self):
        txns = many(30) + [{"transactionType": "NON_SALE_CHARGE",
                            "feeType": "BELOW_STANDARD_FEE",
                            "amount": {"value": "40.00"}}]
        result = reconcile_ebay(txns, POLICY)
        self.assertTrue(any("removable" in w for w in result.warnings))


class TestSampleGating(unittest.TestCase):
    def test_thin_sample_is_reported_but_not_recommended(self):
        result = reconcile_ebay(many(4), POLICY)
        self.assertFalse(result.sufficient)
        # Still reports what it saw — "we looked and it is early" is useful.
        self.assertEqual(result.orders, 4)
        self.assertIsNotNone(result.realised_take_rate_pct)
        self.assertFalse(propose_policy_patch(result)["recommended"])

    def test_thin_sample_warning_explains_why(self):
        result = reconcile_ebay(many(4), POLICY)
        self.assertTrue(any("not yet what to expect" in w
                            for w in result.warnings))

    def test_sufficient_at_the_floor(self):
        self.assertTrue(reconcile_ebay(many(MIN_ORDERS), POLICY).sufficient)


class TestPolicyProposal(unittest.TestCase):
    def test_material_drift_produces_a_patch(self):
        # Real FVF well above the 13.25% estimate.
        result = reconcile_ebay(many(30, fvf_pct=16.0), POLICY)
        patch = propose_policy_patch(result)
        self.assertTrue(patch["recommended"])
        self.assertAlmostEqual(patch["changes"]["referral_pct"], 16.0, places=1)

    def test_small_drift_is_left_alone(self):
        # Churning the policy file without changing a decision is noise.
        result = reconcile_ebay(many(30, fvf_pct=13.3, fixed=0.0), POLICY)
        patch = propose_policy_patch(result)
        self.assertFalse(patch["recommended"])
        self.assertIn("noise band", patch["reason"])

    def test_the_patch_is_never_applied_automatically(self):
        # A fee change moves every margin gate; it needs a commit message.
        result = reconcile_ebay(many(30, fvf_pct=16.0), POLICY)
        patch = propose_policy_patch(result)
        self.assertIn("apply_by_hand", patch)
        self.assertIn("commit message", patch["apply_by_hand"])
        # And the live policy is untouched.
        self.assertEqual(load_policy().raw["fees"]["ebay"]["referral_pct"], 13.25)

    def test_refund_rate_is_carried_into_the_proposal(self):
        txns = many(30, fvf_pct=16.0) + [
            {"transactionType": "REFUND", "amount": {"value": "24.99"}}]
        patch = propose_policy_patch(reconcile_ebay(txns, POLICY))
        self.assertIn("expected_return_rate_pct", patch["changes"])

    def test_empty_window_recommends_nothing(self):
        # Zero orders fails the sample gate before the revenue check, which is
        # the right order — sample size is the more fundamental objection.
        patch = propose_policy_patch(reconcile_ebay([], POLICY))
        self.assertFalse(patch["recommended"])
        self.assertIn("0 order(s)", patch["reason"])

    def test_orders_without_revenue_recommend_nothing(self):
        # The odd edge: enough orders to clear the sample gate, but every one
        # fully refunded or zero-valued, so there is no base to divide by.
        txns = [{"transactionType": "SALE", "orderId": f"O{i}",
                 "amount": {"value": "0.00"}} for i in range(MIN_ORDERS)]
        result = reconcile_ebay(txns, POLICY)
        self.assertTrue(result.sufficient)
        self.assertIsNone(result.realised_take_rate_pct)
        patch = propose_policy_patch(result)
        self.assertFalse(patch["recommended"])
        self.assertIn("No revenue", patch["reason"])


class TestRendering(unittest.TestCase):
    def test_report_shows_the_drift_and_the_components(self):
        result = reconcile_ebay(many(30, fvf_pct=16.0, ad=0.55), POLICY)
        text = render(result, propose_policy_patch(result))
        self.assertIn("FEE RECONCILIATION", text)
        self.assertIn("FINAL_VALUE_FEE", text)
        self.assertIn("Drift", text)
        self.assertIn("Suggested [fees.ebay] changes", text)

    def test_columns_do_not_collide(self):
        result = reconcile_ebay(many(30, ad=0.55), POLICY)
        for line in render(result, propose_policy_patch(result)).splitlines():
            self.assertNotIn("%$", line)

    def test_thin_sample_report_says_so(self):
        result = reconcile_ebay(many(4), POLICY)
        self.assertIn("below the", render(result, propose_policy_patch(result)))


if __name__ == "__main__":
    unittest.main()
