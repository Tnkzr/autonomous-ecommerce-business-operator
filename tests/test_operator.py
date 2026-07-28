"""Test suite.

Bias: the tests that matter most are the ones asserting the system *refuses*
to do something. A repricer that prices well is nice; a repricer that cannot be
dragged below cost is the one that keeps the business solvent.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from operator_core.advertising import KeywordStat, review_campaign, review_keywords
from operator_core.config import PolicyError, load_policy
from operator_core.economics import (
    break_even_price,
    compute_unit_economics,
    price_for_target_margin,
)
from operator_core.inventory import plan_item, safety_stock_units
from operator_core.listings import BACKEND_KEYWORD_BYTE_LIMIT, TITLE_LIMITS, generate_listing
from operator_core.models import (
    Campaign,
    CompetitorOffer,
    Decision,
    InventoryItem,
    ProductCandidate,
    Review,
    Supplier,
)
from operator_core.pricing import recommend_price
from operator_core.reviews import analyse_reviews
from operator_core.risk import SpendState, authorise
from operator_core.screening import competition_score, screen_candidate
from operator_core.store import Store, learning_summary
from operator_core.suppliers import score_suppliers, select_supplier

POLICY = load_policy()


def make_supplier(**kw) -> Supplier:
    base = dict(
        supplier_id="S1", name="Test Supplier", country="CN", rating=4.9,
        unit_cost=5.00, moq=100, shipping_cost_per_unit=1.50, shipping_days=10,
        quality_score=85, communication_score=85, inventory_stability=85,
        defect_rate_pct=1.0, on_time_rate_pct=95,
    )
    base.update(kw)
    return Supplier(**base)


def make_candidate(**kw) -> ProductCandidate:
    base = dict(
        sku="TEST-1", title="stainless steel water bottle", category="Sports",
        marketplace="amazon", target_price=29.99, supplier=make_supplier(),
        est_monthly_demand_units=500, competitor_count=10,
        top_rival_review_count=500, avg_rival_price=28.0, avg_rival_rating=4.2,
        duty_pct=5.0, keywords=["water bottle", "insulated bottle"],
        description="Vacuum insulated bottle.",
    )
    base.update(kw)
    return ProductCandidate(**base)


class TestPolicy(unittest.TestCase):
    def test_loads(self):
        self.assertEqual(POLICY.selection["min_roi_pct"], 40.0)
        self.assertEqual(POLICY.selection["min_margin_pct"], 30.0)
        self.assertEqual(POLICY.selection["min_supplier_rating"], 4.8)

    def test_unknown_marketplace_raises_rather_than_defaulting(self):
        # Silently returning zero fees would make every product look profitable.
        with self.assertRaises(PolicyError):
            POLICY.fees_for("etsy")

    def test_supplier_weights_must_sum_to_one(self):
        total = sum(POLICY.suppliers["weights"].values())
        self.assertAlmostEqual(total, 1.0, places=6)

    def test_rejects_unnormalised_weights(self):
        bad = Path(tempfile.mkdtemp()) / "bad.toml"
        good = POLICY.source_path.read_text()
        bad.write_text(good.replace("landed_cost = 0.30", "landed_cost = 0.90"))
        with self.assertRaises(PolicyError):
            load_policy(bad)

    def test_never_autonomous_is_top_level_not_nested(self):
        # A TOML sub-table mistake here would silently disable the hard blocks.
        self.assertIn("never_autonomous", POLICY.risk)
        self.assertIn("transfer_funds", POLICY.risk["never_autonomous"])
        self.assertNotIn("never_autonomous", POLICY.risk["approval_thresholds"])


class TestEconomics(unittest.TestCase):
    def test_all_costs_are_counted(self):
        e = compute_unit_economics(
            policy=POLICY, sku="X", marketplace="amazon", sale_price=30.0,
            supplier=make_supplier(), duty_pct=5.0, ad_cost_per_unit=3.0,
        )
        # Sum of parts must equal total cost — no silent omissions.
        parts = (e.landed_cost + e.duty + e.referral_fee + e.fulfillment_fee
                 + e.payment_fee + e.storage_fee + e.return_cost
                 + e.ad_cost_per_unit + e.misc_cost)
        self.assertAlmostEqual(parts, e.total_cost, places=2)
        self.assertAlmostEqual(e.net_profit, 30.0 - e.total_cost, places=2)

    def test_returns_are_charged(self):
        e = compute_unit_economics(
            policy=POLICY, sku="X", marketplace="amazon", sale_price=30.0,
            supplier=make_supplier(),
        )
        self.assertGreater(e.return_cost, 0.0,
                           "Return cost must be modelled; ignoring it overstates margin.")

    def test_price_for_target_margin_round_trips(self):
        for mp in ("amazon", "shopify", "walmart", "ebay", "tiktok"):
            with self.subTest(marketplace=mp):
                target = 30.0
                price = price_for_target_margin(
                    policy=POLICY, marketplace=mp, supplier=make_supplier(),
                    target_margin_pct=target, duty_pct=5.0, ad_pct_of_price=10.0,
                )
                e = compute_unit_economics(
                    policy=POLICY, sku="X", marketplace=mp, sale_price=price,
                    supplier=make_supplier(), duty_pct=5.0,
                    ad_cost_per_unit=price * 0.10,
                )
                self.assertAlmostEqual(e.margin_pct, target, delta=0.6)

    def test_break_even_price_yields_zero_profit(self):
        p = break_even_price(policy=POLICY, marketplace="amazon",
                             supplier=make_supplier(), duty_pct=5.0)
        e = compute_unit_economics(
            policy=POLICY, sku="X", marketplace="amazon", sale_price=p,
            supplier=make_supplier(), duty_pct=5.0,
        )
        self.assertAlmostEqual(e.net_profit, 0.0, delta=0.10)

    def test_impossible_margin_raises(self):
        with self.assertRaises(ValueError):
            price_for_target_margin(
                policy=POLICY, marketplace="amazon", supplier=make_supplier(),
                target_margin_pct=95.0, ad_pct_of_price=10.0,
            )

    def test_break_even_acos_equals_pre_ad_margin(self):
        e = compute_unit_economics(
            policy=POLICY, sku="X", marketplace="amazon", sale_price=40.0,
            supplier=make_supplier(), ad_cost_per_unit=4.0,
        )
        expected = (e.net_profit + 4.0) / 40.0 * 100
        self.assertAlmostEqual(e.break_even_acos_pct, expected, places=1)


class TestScreeningGates(unittest.TestCase):
    def test_hazmat_blocks_regardless_of_profit(self):
        c = make_candidate(
            sku="HAZ", title="camping lantern with lithium battery",
            target_price=99.0, supplier=make_supplier(unit_cost=2.0),
        )
        r = screen_candidate(POLICY, c)
        self.assertEqual(r.decision, Decision.REJECT)
        self.assertIn("hazmat", [g.name for g in r.blocking_failures])
        self.assertGreater(r.economics.roi_pct, 40,
                           "Fixture must be highly profitable to prove profit cannot override.")

    def test_medical_claim_blocks(self):
        c = make_candidate(description="Clinically proven to cure joint pain.")
        r = screen_candidate(POLICY, c)
        self.assertEqual(r.decision, Decision.REJECT)
        self.assertIn("medical_claims", [g.name for g in r.blocking_failures])

    def test_trademark_blocks(self):
        c = make_candidate(title="nike inspired running belt", brand="Nike")
        r = screen_candidate(POLICY, c)
        self.assertIn("trademark", [g.name for g in r.blocking_failures])

    def test_word_boundary_prevents_false_positive(self):
        # "cure" must not fire inside "manicure" / "obscure".
        c = make_candidate(title="manicure kit", description="Obscure travel size set.")
        r = screen_candidate(POLICY, c)
        med = next(g for g in r.gates if g.name == "medical_claims")
        self.assertTrue(med.passed, f"False positive: {med.detail}")

    def test_gated_category_blocks_without_approval(self):
        c = make_candidate(category="Jewelry")
        r = screen_candidate(POLICY, c)
        self.assertIn("restricted_category", [g.name for g in r.blocking_failures])

    def test_gated_category_passes_with_approval_on_file(self):
        c = make_candidate(category="Jewelry", gated_approval_on_file=True)
        r = screen_candidate(POLICY, c)
        gate = next(g for g in r.gates if g.name == "restricted_category")
        self.assertTrue(gate.passed)

    def test_low_supplier_rating_fails(self):
        c = make_candidate(supplier=make_supplier(rating=4.7))
        r = screen_candidate(POLICY, c)
        self.assertFalse(next(g for g in r.gates if g.name == "supplier_rating").passed)

    def test_slow_shipping_fails_but_domestic_is_waived(self):
        slow = make_candidate(supplier=make_supplier(shipping_days=20))
        self.assertFalse(
            next(g for g in screen_candidate(POLICY, slow).gates
                 if g.name == "shipping_time").passed
        )
        domestic = make_candidate(
            supplier=make_supplier(shipping_days=20, domestic_stock=True)
        )
        self.assertTrue(
            next(g for g in screen_candidate(POLICY, domestic).gates
                 if g.name == "shipping_time").passed
        )

    def test_roi_and_margin_thresholds_enforced(self):
        thin = make_candidate(target_price=14.0, supplier=make_supplier(unit_cost=6.0))
        r = screen_candidate(POLICY, thin)
        names = [g.name for g in r.blocking_failures]
        self.assertTrue({"roi", "margin"} & set(names))
        self.assertEqual(r.decision, Decision.REJECT)

    def test_clean_candidate_routes_to_approval_not_auto_approve(self):
        c = make_candidate(target_price=34.99, supplier=make_supplier(unit_cost=4.0))
        r = screen_candidate(POLICY, c)
        self.assertEqual(r.decision, Decision.NEEDS_HUMAN_APPROVAL)
        self.assertEqual(r.blocking_failures, [])

    def test_competition_score_monotonic(self):
        low = competition_score(make_candidate(competitor_count=2, top_rival_review_count=50))
        high = competition_score(make_candidate(competitor_count=80, top_rival_review_count=40000))
        self.assertLess(low, high)
        self.assertLessEqual(high, 100.0)


class TestRiskGate(unittest.TestCase):
    def setUp(self):
        self.state = SpendState(spent_today_usd=0, open_po_exposure_usd=0,
                                cash_available_usd=50000, new_skus_this_week=0)

    def test_never_autonomous_is_absolute(self):
        r = authorise(policy=POLICY, action_type="transfer_funds", amount_usd=1.0,
                      state=self.state)
        self.assertFalse(r.permitted)
        self.assertTrue(r.approval_required_from_human)

    def test_advisory_mode_blocks_writes(self):
        self.assertFalse(POLICY.live_trading_enabled)
        r = authorise(policy=POLICY, action_type="place_purchase_order",
                      amount_usd=100.0, state=self.state)
        self.assertFalse(r.permitted)
        self.assertIn("live_trading_enabled", r.explain())

    def test_negative_profit_never_authorised(self):
        r = authorise(policy=POLICY, action_type="place_purchase_order", amount_usd=100.0,
                      projected_profit_usd=-0.01, state=self.state)
        self.assertFalse(r.permitted)
        self.assertEqual(r.decision, Decision.REJECT)

    def test_single_po_cap(self):
        r = authorise(policy=POLICY, action_type="place_purchase_order",
                      amount_usd=2000.01, projected_profit_usd=500.0, state=self.state)
        self.assertFalse(r.permitted)
        self.assertIn("single-transaction cap", r.explain())

    def test_daily_cap(self):
        state = SpendState(spent_today_usd=4900, cash_available_usd=50000)
        r = authorise(policy=POLICY, action_type="place_purchase_order", amount_usd=200.0,
                      projected_profit_usd=100.0, state=state)
        self.assertFalse(r.permitted)
        self.assertIn("daily cap", r.explain())

    def test_cash_buffer_is_not_spendable(self):
        state = SpendState(cash_available_usd=3100)
        r = authorise(policy=POLICY, action_type="place_purchase_order", amount_usd=200.0,
                      projected_profit_usd=100.0, state=state)
        self.assertFalse(r.permitted)
        self.assertIn("cash reserve", r.explain())

    def test_open_exposure_ceiling(self):
        state = SpendState(open_po_exposure_usd=14900, cash_available_usd=50000)
        r = authorise(policy=POLICY, action_type="place_purchase_order", amount_usd=500.0,
                      projected_profit_usd=100.0, state=state)
        self.assertFalse(r.permitted)
        self.assertIn("exposure", r.explain())

    def test_weekly_sku_launch_cap(self):
        state = SpendState(cash_available_usd=50000, new_skus_this_week=3)
        r = authorise(policy=POLICY, action_type="analyse", is_new_sku=True, state=state)
        self.assertFalse(r.permitted)

    def test_new_supplier_always_needs_approval(self):
        r = authorise(policy=POLICY, action_type="analyse", amount_usd=1.0,
                      projected_profit_usd=50.0, state=self.state, is_new_supplier=True)
        self.assertTrue(r.approval_required_from_human)

    def test_small_read_only_action_is_permitted(self):
        r = authorise(policy=POLICY, action_type="analyse", amount_usd=0.0,
                      state=self.state)
        self.assertTrue(r.permitted)
        self.assertEqual(r.decision, Decision.APPROVE)


class TestPricing(unittest.TestCase):
    def test_never_follows_a_rival_below_the_floor(self):
        rec = recommend_price(
            policy=POLICY, sku="P", marketplace="amazon", current_price=29.99,
            supplier=make_supplier(unit_cost=8.0), duty_pct=5.0,
            competitors=[CompetitorOffer(seller="Dumper", price=9.99, in_stock=True)],
            ad_cost_per_unit=3.0,
        )
        self.assertGreaterEqual(rec.recommended_price, rec.floor_price)
        self.assertIn("below our floor", " ".join(rec.rationale))

    def test_sub_floor_rival_is_treated_as_an_outlier_not_the_market(self):
        rec = recommend_price(
            policy=POLICY, sku="P", marketplace="amazon", current_price=34.99,
            supplier=make_supplier(unit_cost=8.20), duty_pct=5.0,
            ad_cost_per_unit=3.50,
            competitors=[
                CompetitorOffer(seller="Dumper", price=15.00, in_stock=True),
                CompetitorOffer(seller="Real1", price=32.99, in_stock=True),
                CompetitorOffer(seller="Real2", price=33.50, in_stock=True),
            ],
        )
        self.assertGreaterEqual(rec.recommended_price, rec.floor_price)
        joined = " ".join(rec.rationale)
        self.assertIn("below our floor", joined)
        self.assertIn("credible field", joined,
                      "It must explain why it still moved, not just why it refused.")

    def test_daily_change_is_clamped(self):
        rec = recommend_price(
            policy=POLICY, sku="P", marketplace="amazon", current_price=20.00,
            supplier=make_supplier(unit_cost=3.0),
            competitors=[CompetitorOffer(seller="R", price=60.0, in_stock=True)],
        )
        limit = float(POLICY.pricing["max_daily_price_change_pct"])
        self.assertLessEqual(abs(rec.change_pct), limit + 0.01)

    def test_cooldown_defers_change(self):
        rec = recommend_price(
            policy=POLICY, sku="P", marketplace="amazon", current_price=29.99,
            supplier=make_supplier(unit_cost=5.0),
            competitors=[CompetitorOffer(seller="R", price=24.99, in_stock=True)],
            hours_since_last_change=1.0,
        )
        self.assertEqual(rec.action, "HOLD")
        self.assertIn("Cooldown", " ".join(rec.rationale))

    def test_undercuts_when_it_can_afford_to(self):
        rec = recommend_price(
            policy=POLICY, sku="P", marketplace="amazon", current_price=29.99,
            supplier=make_supplier(unit_cost=3.0),
            competitors=[CompetitorOffer(seller="R", price=28.50, in_stock=True)],
        )
        self.assertEqual(rec.action, "LOWER")
        self.assertLess(rec.recommended_price, 29.99)

    def test_out_of_stock_rivals_ignored(self):
        rec = recommend_price(
            policy=POLICY, sku="P", marketplace="amazon", current_price=29.99,
            supplier=make_supplier(unit_cost=5.0),
            competitors=[CompetitorOffer(seller="Ghost", price=5.0, in_stock=False)],
        )
        self.assertIn("No in-stock competitors", " ".join(rec.rationale))

    def test_large_change_requires_approval(self):
        rec = recommend_price(
            policy=POLICY, sku="P", marketplace="amazon", current_price=10.00,
            supplier=make_supplier(unit_cost=6.0),
            competitors=[CompetitorOffer(seller="R", price=40.0, in_stock=True)],
        )
        self.assertTrue(rec.requires_approval)


class TestInventory(unittest.TestCase):
    def test_safety_stock_grows_with_volatility(self):
        steady = InventoryItem(sku="A", marketplace="amazon", on_hand_units=100,
                               inbound_units=0, daily_velocity=10, velocity_stddev=0.5)
        spiky = InventoryItem(sku="A", marketplace="amazon", on_hand_units=100,
                              inbound_units=0, daily_velocity=10, velocity_stddev=8.0)
        self.assertLess(safety_stock_units(steady, POLICY),
                        safety_stock_units(spiky, POLICY))

    def test_stockout_inside_lead_time_is_critical(self):
        item = InventoryItem(sku="A", marketplace="amazon", on_hand_units=50,
                             inbound_units=0, daily_velocity=10, velocity_stddev=2,
                             lead_time_days=30, unit_cost=5.0)
        plan = plan_item(POLICY, item)
        self.assertEqual(plan.status, "CRITICAL")
        self.assertGreater(plan.recommended_order_units, 0)

    def test_zero_velocity_never_reorders(self):
        item = InventoryItem(sku="A", marketplace="amazon", on_hand_units=500,
                             inbound_units=0, daily_velocity=0.0, unit_cost=5.0)
        plan = plan_item(POLICY, item)
        self.assertEqual(plan.status, "STALLED")
        self.assertEqual(plan.recommended_order_units, 0)

    def test_overstock_detected(self):
        item = InventoryItem(sku="A", marketplace="amazon", on_hand_units=5000,
                             inbound_units=0, daily_velocity=5, velocity_stddev=1,
                             unit_cost=5.0)
        self.assertEqual(plan_item(POLICY, item).status, "OVERSTOCK")

    def test_inbound_counts_toward_cover(self):
        base = dict(sku="A", marketplace="amazon", on_hand_units=100,
                    daily_velocity=10, velocity_stddev=1, unit_cost=5.0)
        without = plan_item(POLICY, InventoryItem(inbound_units=0, **base))
        with_in = plan_item(POLICY, InventoryItem(inbound_units=400, **base))
        self.assertGreater(with_in.days_of_cover, without.days_of_cover)

    def test_expensive_reorder_flags_approval(self):
        item = InventoryItem(sku="A", marketplace="amazon", on_hand_units=10,
                             inbound_units=0, daily_velocity=20, velocity_stddev=3,
                             unit_cost=25.0, moq=100)
        plan = plan_item(POLICY, item)
        self.assertTrue(plan.requires_approval)


class TestAdvertising(unittest.TestCase):
    def _camp(self, **kw) -> Campaign:
        base = dict(campaign_id="C1", name="c", sku="S", marketplace="amazon",
                    spend=100.0, sales=400.0, clicks=100, impressions=10000,
                    orders=10, daily_budget=20.0, period_days=30)
        base.update(kw)
        return Campaign(**base)

    def test_insufficient_data_holds(self):
        r = review_campaign(POLICY, self._camp(clicks=3, spend=2.0, sales=0, orders=0),
                            product_margin_pct=35.0)
        self.assertEqual(r.verdict, "INSUFFICIENT_DATA")
        self.assertEqual(r.actions, [])

    def test_zero_orders_with_many_clicks_pauses(self):
        r = review_campaign(POLICY, self._camp(clicks=200, spend=180.0, sales=0, orders=0),
                            product_margin_pct=35.0)
        self.assertEqual(r.verdict, "PAUSE")

    def test_break_even_is_per_product_not_global(self):
        camp = self._camp(spend=120.0, sales=400.0)   # 30% ACOS
        rich = review_campaign(POLICY, camp, product_margin_pct=45.0)
        poor = review_campaign(POLICY, camp, product_margin_pct=18.0)
        self.assertEqual(rich.break_even_acos_pct, 45.0)
        self.assertEqual(poor.break_even_acos_pct, 18.0)
        self.assertEqual(poor.verdict, "PAUSE")
        self.assertNotEqual(rich.verdict, "PAUSE")

    def test_profitable_campaign_scales(self):
        r = review_campaign(POLICY, self._camp(spend=60.0, sales=600.0),
                            product_margin_pct=35.0)
        self.assertEqual(r.verdict, "SCALE")
        self.assertEqual(r.actions[0].action, "INCREASE_BUDGET")

    def test_weekly_scaling_cap_respected(self):
        r = review_campaign(POLICY, self._camp(spend=60.0, sales=600.0),
                            product_margin_pct=35.0,
                            weekly_budget_increase_so_far_pct=50.0)
        self.assertEqual(r.verdict, "HEALTHY")
        self.assertEqual(r.actions, [])

    def test_wasteful_keyword_negated(self):
        kws = [KeywordStat("junk term", "broad", 40, 5000, 40.0, 0.0, 0, 0.9)]
        actions = review_keywords(POLICY, kws, break_even_acos_pct=30.0)
        self.assertEqual(actions[0].action, "NEGATE")

    def test_winning_keyword_harvested(self):
        kws = [KeywordStat("great term", "broad", 50, 3000, 30.0, 400.0, 12, 0.9)]
        actions = review_keywords(POLICY, kws, break_even_acos_pct=30.0)
        self.assertEqual(actions[0].action, "HARVEST")

    def test_converting_but_expensive_keyword_bid_down_not_cut(self):
        kws = [KeywordStat("pricey", "broad", 50, 3000, 90.0, 200.0, 5, 1.00)]
        actions = review_keywords(POLICY, kws, break_even_acos_pct=30.0)
        self.assertEqual(actions[0].action, "DECREASE_BID")
        self.assertLess(actions[0].proposed_value, 1.00)


class TestListings(unittest.TestCase):
    def test_title_respects_marketplace_limit(self):
        for mp in TITLE_LIMITS:
            with self.subTest(marketplace=mp):
                d = generate_listing(make_candidate(marketplace=mp), marketplace=mp)
                self.assertLessEqual(len(d.title), TITLE_LIMITS[mp])

    def test_backend_keywords_within_byte_budget(self):
        d = generate_listing(make_candidate())
        self.assertLessEqual(len(d.backend_keywords.encode("utf-8")),
                             BACKEND_KEYWORD_BYTE_LIMIT)

    def test_backend_keywords_do_not_repeat_title(self):
        d = generate_listing(make_candidate())
        title_words = set(d.title.lower().split())
        for term in d.backend_keywords.split():
            self.assertNotIn(term, title_words,
                             "Repeating a title word in backend terms wastes indexed bytes.")

    def test_full_asset_set_produced(self):
        d = generate_listing(make_candidate())
        self.assertEqual(len(d.bullets), 5)
        self.assertGreaterEqual(len(d.image_briefs), 7)
        self.assertGreaterEqual(len(d.faq), 5)
        self.assertGreaterEqual(len(d.aplus_modules), 6)
        self.assertTrue(d.description)

    def test_normal_candidate_gets_full_keyword_coverage(self):
        # The generator must place every target keyword in an indexed field.
        d = generate_listing(make_candidate())
        self.assertTrue(all(d.keyword_coverage.values()),
                        f"Uncovered: {[k for k, v in d.keyword_coverage.items() if not v]}")

    def test_uncoverable_keywords_are_warned_not_silently_dropped(self):
        # More keywords than the title and backend byte budget can hold.
        overflow = [f"extraordinarily descriptive longtail keyword phrase {i}"
                    for i in range(12)]
        d = generate_listing(make_candidate(keywords=["water bottle", *overflow]))
        self.assertFalse(all(d.keyword_coverage.values()))
        self.assertTrue(any("not rank" in w for w in d.warnings),
                        "Dropped keywords must surface a warning, not fail silently.")


class TestReviews(unittest.TestCase):
    def _rev(self, rid, rating, title, body) -> Review:
        return Review(review_id=rid, sku="S", marketplace="amazon", rating=rating,
                      title=title, body=body, created_at="2026-01-01")

    def test_safety_language_escalates_regardless_of_rating(self):
        revs = [self._rev("1", 5, "Great", "It caught fire but I love it")]
        ins = analyse_reviews("S", "amazon", revs)
        self.assertEqual(ins.severity.value, "CRITICAL")
        self.assertTrue(ins.account_risk_hits)

    def test_recurring_theme_detected(self):
        revs = [
            self._rev("1", 2, "Bad", "runs small and does not fit"),
            self._rev("2", 1, "Too small", "way too small"),
            self._rev("3", 2, "Sizing", "wrong size entirely"),
            self._rev("4", 5, "Great", "perfect"),
        ]
        ins = analyse_reviews("S", "amazon", revs)
        self.assertEqual(ins.top_complaint, "sizing")

    def test_no_reviews_is_handled(self):
        ins = analyse_reviews("S", "amazon", [])
        self.assertEqual(ins.total_reviews, 0)
        self.assertTrue(ins.recommended_actions)


class TestSuppliers(unittest.TestCase):
    def test_single_quote_blocks_selection(self):
        winner, notes = select_supplier(POLICY, [make_supplier()])
        self.assertIsNone(winner)
        self.assertIn("policy requires 2", " ".join(notes))

    def test_disqualified_supplier_cannot_win(self):
        good = make_supplier(supplier_id="G", name="Good", rating=4.9, unit_cost=9.0)
        cheap_bad = make_supplier(supplier_id="B", name="Cheap", rating=4.0, unit_cost=2.0)
        winner, _ = select_supplier(POLICY, [good, cheap_bad])
        self.assertIsNotNone(winner)
        self.assertEqual(winner.supplier.supplier_id, "G",
                         "A cheaper supplier below the rating gate must never win.")

    def test_disqualified_quote_is_not_used_as_leverage(self):
        from operator_core.suppliers import build_negotiation_brief
        good = make_supplier(supplier_id="G", name="Good", rating=4.9, unit_cost=9.0)
        cheap_bad = make_supplier(supplier_id="B", name="TooSlow", rating=4.9,
                                  unit_cost=2.0, shipping_days=40)
        scored = score_suppliers(POLICY, [good, cheap_bad])
        winner = next(s for s in scored if not s.disqualified)
        brief = build_negotiation_brief(
            winner=winner, alternatives=scored, annual_volume_units=1000,
            max_acceptable_cost=10.0,
        )
        joined = " ".join(brief.leverage_points) + brief.draft_message
        self.assertNotIn("2.00", joined,
                         "Citing a disqualified supplier's price is a bluff we cannot execute.")

    def test_more_expensive_quote_is_not_leverage(self):
        from operator_core.suppliers import build_negotiation_brief
        cheap = make_supplier(supplier_id="C", name="Cheap", rating=4.9, unit_cost=8.0)
        pricey = make_supplier(supplier_id="P", name="Pricey", rating=4.9, unit_cost=15.0)
        scored = score_suppliers(POLICY, [cheap, pricey])
        winner = next(s for s in scored if s.supplier.supplier_id == "C")
        brief = build_negotiation_brief(
            winner=winner, alternatives=scored, annual_volume_units=1000,
            max_acceptable_cost=10.0,
        )
        joined = " ".join(brief.leverage_points) + brief.draft_message
        self.assertNotIn("15.00", joined,
                         "A dearer alternative argues the supplier's case, not ours.")

    def test_scores_are_ordered(self):
        a = make_supplier(supplier_id="A", unit_cost=5.0, quality_score=95,
                          communication_score=95, inventory_stability=95)
        b = make_supplier(supplier_id="B", unit_cost=9.0, quality_score=50,
                          communication_score=50, inventory_stability=50)
        scored = score_suppliers(POLICY, [b, a])
        self.assertEqual(scored[0].supplier.supplier_id, "A")


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(Path(self.tmp) / "t.db")

    def test_journal_roundtrip(self):
        aid = self.store.record_decision(
            domain="pricing", sku="S", action="raise", rationale="r",
            decision="APPROVE", expected_outcome="more profit",
        )
        self.assertTrue(self.store.decisions_for_sku("S"))
        self.assertTrue(self.store.record_outcome(aid, {"met_expectation": True}))
        summary = learning_summary(self.store)
        self.assertEqual(summary["resolved_decisions"], 1)
        self.assertEqual(summary["by_domain"]["pricing"]["hit_rate_pct"], 100.0)

    def test_dedupe_key_makes_reruns_idempotent(self):
        kw = dict(domain="inventory", sku="S", action="reorder", rationale="r",
                  decision="APPROVE", dedupe_key="2026-01-01|inventory|S|reorder")
        first = self.store.record_decision(**kw)
        second = self.store.record_decision(**kw)
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.recent_decisions()), 1)

    def test_pending_approvals_clear_once_approved(self):
        aid = self.store.record_decision(
            domain="inventory", sku="S", action="buy", rationale="r",
            decision="NEEDS_HUMAN_APPROVAL", requires_approval=True,
        )
        self.assertEqual(len(self.store.pending_approvals()), 1)
        self.store.approve(aid, "Owner")
        self.assertEqual(len(self.store.pending_approvals()), 0)

    def test_unresolved_decisions_are_not_counted_as_wins(self):
        self.store.record_decision(domain="ads", sku="S", action="a", rationale="r",
                                   decision="APPROVE")
        summary = learning_summary(self.store)
        self.assertEqual(summary["resolved_decisions"], 0)
        self.assertEqual(summary["by_domain"], {})


class TestConnectors(unittest.TestCase):
    def test_missing_credentials_raise_not_return_empty(self):
        from connectors import ConnectorNotConfigured, get_connector
        c = get_connector("amazon")
        self.assertFalse(c.configured)
        with self.assertRaises(ConnectorNotConfigured):
            c.fetch_orders(since="2026-01-01")

    def test_writes_blocked_in_read_only_mode(self):
        from connectors import WriteNotPermitted, get_connector
        c = get_connector("shopify", allow_writes=False)
        with self.assertRaises(WriteNotPermitted):
            c.update_price("SKU", 10.0)

    def test_all_five_marketplaces_present(self):
        from connectors import CONNECTOR_REGISTRY
        self.assertEqual(set(CONNECTOR_REGISTRY),
                         {"amazon", "shopify", "walmart", "ebay", "tiktok"})


class TestPipelineEndToEnd(unittest.TestCase):
    def test_daily_run_labels_seed_data(self):
        from operator_core.pipeline import run_daily
        tmp = Path(tempfile.mkdtemp())
        store = Store(tmp / "t.db")
        content, path = run_daily(POLICY, store, report_date="2026-01-15")
        self.assertIn("NOT REAL BUSINESS NUMBERS", content)
        self.assertIn("ADVISORY ONLY", content)
        self.assertTrue(path.exists())

    def test_daily_run_is_idempotent(self):
        from operator_core.pipeline import run_daily
        tmp = Path(tempfile.mkdtemp())
        store = Store(tmp / "t.db")
        run_daily(POLICY, store, report_date="2026-01-15")
        n1 = len(store.recent_decisions(limit=1000))
        run_daily(POLICY, store, report_date="2026-01-15")
        n2 = len(store.recent_decisions(limit=1000))
        self.assertEqual(n1, n2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
