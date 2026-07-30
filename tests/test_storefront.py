"""Storefront sync and Shopify product-plan tests.

Both modules are bridges between systems with different assumptions, which is
where quiet corruption lives: an order counted twice, a refund landing on the
wrong day, a cost guessed rather than read, a product URL that changes after
the videos linking to it are published.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from operator_core.config import load_policy
from operator_core.listings import generate_listing
from operator_core.models import ProductCandidate, Supplier
from operator_core.shopify_listing import (
    SEO_DESCRIPTION_MAX,
    SEO_TITLE_MAX,
    build_body_html,
    build_product_plan,
    build_tags,
    handleize,
    publish_plan,
    trim_at_word,
)
from operator_core.store import Store
from operator_core.storefront import (
    _estimate_fees,
    sync_line_items,
    sync_orders,
)

POLICY = load_policy()

SUPPLIER = Supplier(supplier_id="S1", name="Supplier", country="CN", rating=4.9,
                    unit_cost=4.10, moq=50, shipping_cost_per_unit=1.20,
                    shipping_days=9)


def candidate(**kw) -> ProductCandidate:
    base = dict(sku="SKU-1", title="Self cleaning slicker brush for dogs",
                category="Pet Supplies", marketplace="shopify",
                target_price=24.99, supplier=SUPPLIER,
                est_monthly_demand_units=1200,
                keywords=["slicker brush", "self cleaning"],
                description="A self cleaning slicker brush")
    base.update(kw)
    return ProductCandidate(**base)


class _Attribution:
    def __init__(self, channel: str = "tiktok") -> None:
        self.channel = channel


class _Order:
    """Stands in for a ShopifyOrderSummary."""

    def __init__(self, *, created_at="2026-08-01T10:00:00Z", channel="tiktok",
                 net_revenue=40.0, refunded=0.0, cancelled=False,
                 financial_status="PAID", order_count=1, lines=None,
                 name="#1"):
        self.created_at = created_at
        self.attribution = _Attribution(channel)
        self.net_revenue = net_revenue
        self.refunded = refunded
        self.cancelled = cancelled
        self.financial_status = financial_status
        self.customer_order_count = order_count
        self.name = name
        self.line_items = lines if lines is not None else [
            {"sku": "SKU-1", "quantity": 1, "revenue": net_revenue,
             "unit_cost": 5.30}]


class StorefrontTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = TemporaryDirectory()
        self.store = Store(Path(self._dir.name) / "s.db")

    def tearDown(self):
        self._dir.cleanup()


class TestOrderAggregation(StorefrontTestCase):
    def test_unreadable_timestamp_is_excluded_not_bucketed_into_today(self):
        # Bucketing it into today would silently move revenue between days.
        result = sync_orders(self.store, [_Order(created_at="not a date")])
        self.assertEqual(result.orders_counted, 0)
        self.assertEqual(result.orders_excluded, 1)
        self.assertTrue(any("excluded rather than bucketed" in w
                            for w in result.warnings))

    def test_voided_orders_are_not_revenue(self):
        result = sync_orders(self.store, [_Order(financial_status="VOIDED")])
        self.assertEqual(result.orders_counted, 0)

    def test_repeat_customers_do_not_count_as_new(self):
        sync_orders(self.store, [_Order(order_count=1), _Order(order_count=4)])
        row = self.store.storefront_range("2026-08-01", "2026-08-01")[0]
        self.assertEqual(row["orders"], 2)
        self.assertEqual(row["new_customers"], 1)

    def test_utc_day_boundary_is_consistent(self):
        # Mixing shop-local and UTC days produces overlapping days, and a funnel
        # over overlapping days double-counts the orders in the seam.
        sync_orders(self.store, [
            _Order(created_at="2026-08-01T23:30:00+00:00"),
            _Order(created_at="2026-08-02T00:30:00+00:00"),
        ])
        days = {r["metric_date"] for r
                in self.store.storefront_range("2026-08-01", "2026-08-02")}
        self.assertEqual(days, {"2026-08-01", "2026-08-02"})

    def test_channels_are_kept_separate(self):
        sync_orders(self.store, [_Order(channel="tiktok", net_revenue=40.0),
                                 _Order(channel="search", net_revenue=60.0)])
        rows = {r["channel"]: r for r
                in self.store.storefront_range("2026-08-01", "2026-08-01")}
        self.assertEqual(rows["tiktok"]["revenue"], 40.0)
        self.assertEqual(rows["search"]["revenue"], 60.0)


class TestLineItemAggregation(StorefrontTestCase):
    def test_per_sku_profit_is_written(self):
        result = sync_line_items(self.store, [_Order()], POLICY)
        self.assertEqual(result.orders_counted, 1)
        rows = self.store.metrics_range("2026-08-01", "2026-08-01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku"], "SKU-1")
        self.assertEqual(rows[0]["cogs"], 5.30)
        self.assertGreater(rows[0]["fees"], 0)

    def test_missing_cost_leaves_profit_at_zero_and_says_so(self):
        # A guessed cost produces a confident margin, and a confident margin on
        # a guessed cost is how a loss-making product gets scaled.
        order = _Order(lines=[{"sku": "SKU-9", "quantity": 1, "revenue": 40.0,
                               "unit_cost": None}])
        result = sync_line_items(self.store, [order], POLICY)
        row = self.store.metrics_range("2026-08-01", "2026-08-01")[0]
        self.assertEqual(row["cogs"], 0.0)
        self.assertEqual(row["net_profit"], 0.0)
        self.assertTrue(any("no cost per item" in w for w in result.warnings))

    def test_blank_sku_is_excluded_not_bucketed_under_empty_string(self):
        # A blank-SKU bucket becomes a phantom best-seller.
        order = _Order(lines=[{"sku": "", "quantity": 1, "revenue": 40.0,
                               "unit_cost": 5.0}])
        result = sync_line_items(self.store, [order], POLICY)
        self.assertEqual(result.orders_counted, 0)
        self.assertEqual(self.store.metrics_range("2026-08-01", "2026-08-01"), [])

    def test_refunds_are_apportioned_across_lines_by_revenue_share(self):
        order = _Order(net_revenue=100.0, refunded=20.0, lines=[
            {"sku": "A", "quantity": 1, "revenue": 75.0, "unit_cost": 10.0},
            {"sku": "B", "quantity": 1, "revenue": 25.0, "unit_cost": 5.0},
        ])
        sync_line_items(self.store, [order], POLICY)
        rows = {r["sku"]: r for r
                in self.store.metrics_range("2026-08-01", "2026-08-01")}
        self.assertAlmostEqual(rows["A"]["refunds"], 15.0, places=2)
        self.assertAlmostEqual(rows["B"]["refunds"], 5.0, places=2)

    def test_two_sku_order_is_one_order_and_two_product_rows(self):
        # Forcing both through one table makes every per-product rate wrong by
        # the basket size.
        order = _Order(lines=[
            {"sku": "A", "quantity": 1, "revenue": 30.0, "unit_cost": 5.0},
            {"sku": "B", "quantity": 2, "revenue": 20.0, "unit_cost": 3.0},
        ])
        sync_orders(self.store, [order])
        sync_line_items(self.store, [order], POLICY)
        self.assertEqual(
            self.store.storefront_range("2026-08-01", "2026-08-01")[0]["orders"], 1)
        self.assertEqual(
            len(self.store.metrics_range("2026-08-01", "2026-08-01")), 2)

    def test_resync_is_idempotent(self):
        for _ in range(3):
            sync_line_items(self.store, [_Order()], POLICY)
        rows = self.store.metrics_range("2026-08-01", "2026-08-01")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["units"], 1)

    def test_fees_are_labelled_as_estimates(self):
        result = sync_line_items(self.store, [_Order()], POLICY)
        self.assertTrue(any("Reconcile them against a real payout" in w
                            for w in result.warnings))

    def test_fee_estimate_uses_the_policy_schedule(self):
        cfg = {"referral_pct": 10.0, "payment_pct": 2.0, "payment_flat": 0.30,
               "fulfillment_flat": 5.00}
        # 100 * 10% + 100 * 2% + 0.30 + 5.00
        self.assertAlmostEqual(_estimate_fees(100.0, 1, cfg), 17.30, places=2)

    def test_fee_estimate_on_zero_revenue_charges_no_flat_payment_fee(self):
        cfg = {"payment_flat": 0.30, "fulfillment_flat": 0.0}
        self.assertEqual(_estimate_fees(0.0, 0, cfg), 0.0)


# ---------------------------------------------------------------------------
class TestProductPlan(unittest.TestCase):
    def _plan(self, cand=None, **kw):
        cand = cand or candidate()
        draft = generate_listing(cand, marketplace="shopify")
        return build_product_plan(POLICY, cand, draft, **kw)

    def test_status_is_always_draft(self):
        self.assertEqual(self._plan().product_input["status"], "DRAFT")

    def test_handle_is_deterministic(self):
        # The handle is the product URL. Two runs must agree or every video
        # linking to it breaks.
        self.assertEqual(self._plan().handle, self._plan().handle)

    def test_seo_fields_respect_search_engine_limits(self):
        plan = self._plan()
        self.assertLessEqual(len(plan.product_input["seo"]["title"]),
                             SEO_TITLE_MAX)
        self.assertLessEqual(len(plan.product_input["seo"]["description"]),
                             SEO_DESCRIPTION_MAX)

    def test_trimming_never_cuts_a_word_in_half(self):
        trimmed = trim_at_word("the quick brown fox jumps over the lazy dog", 20)
        self.assertLessEqual(len(trimmed), 20)
        self.assertTrue(trimmed.split()[-1] in
                        "the quick brown fox jumps over the lazy dog".split())

    def test_body_html_escapes_generated_copy(self):
        # An unescaped ampersand is broken markup; an angle bracket is worse.
        cand = candidate(title="Brush <b>Pro</b> & Comb",
                         description="Cats & dogs <3")
        draft = generate_listing(cand, marketplace="shopify")
        body = build_body_html(draft, cand)
        self.assertNotIn("<b>Pro</b>", body)
        self.assertIn("&amp;", body)

    def test_cost_comes_from_the_supplier_when_not_supplied(self):
        variant = self._plan().variant_input
        self.assertEqual(variant["inventoryItem"]["cost"], "5.30")

    def test_missing_cost_is_warned_about_not_guessed(self):
        cand = candidate(supplier=Supplier(
            supplier_id="S", name="S", country="CN", rating=4.9, unit_cost=0.0,
            moq=1, shipping_cost_per_unit=0.0, shipping_days=5))
        plan = self._plan(cand)
        self.assertNotIn("cost", plan.variant_input["inventoryItem"])
        self.assertTrue(any("cost per item is left unset" in w
                            for w in plan.warnings))

    def test_fake_discount_is_blocked(self):
        # A struck-through price that was never charged is a misleading-pricing
        # claim, not a promotion.
        plan = self._plan(compare_at_price=20.00)
        self.assertFalse(plan.ready)
        self.assertTrue(any("misleading-pricing" in b for b in plan.blockers))

    def test_equal_compare_at_is_also_blocked(self):
        self.assertFalse(self._plan(compare_at_price=24.99).ready)

    def test_genuine_compare_at_is_accepted(self):
        plan = self._plan(compare_at_price=34.99)
        self.assertTrue(plan.ready)
        self.assertEqual(plan.variant_input["compareAtPrice"], "34.99")

    def test_deep_discount_over_the_policy_ceiling_warns(self):
        plan = self._plan(compare_at_price=99.99)   # ~75% implied
        self.assertTrue(any("ceiling in [shopify]" in w for w in plan.warnings))

    def test_non_positive_price_is_blocked(self):
        plan = self._plan(candidate(target_price=0.0))
        self.assertFalse(plan.ready)

    def test_tags_are_deduplicated_case_insensitively(self):
        # Shopify treats Storage and storage as two tags and shows both.
        cand = candidate(keywords=["Storage", "storage", "STORAGE"])
        tags = build_tags(cand, generate_listing(cand, marketplace="shopify"))
        self.assertEqual(len([t for t in tags if t.lower() == "storage"]), 1)

    def test_handleize_rules(self):
        self.assertEqual(handleize("Bamboo Organizer, Expandable!"),
                         "bamboo-organizer-expandable")
        self.assertEqual(handleize("!!!"), "product")

    def test_sku_is_carried_onto_the_variant(self):
        self.assertEqual(
            self._plan().variant_input["inventoryItem"]["sku"], "SKU-1")


class TestPublishPlan(unittest.TestCase):
    def test_blocked_plan_refuses_to_create(self):
        cand = candidate()
        draft = generate_listing(cand, marketplace="shopify")
        plan = build_product_plan(POLICY, cand, draft, compare_at_price=1.0)
        with self.assertRaises(ValueError) as ctx:
            publish_plan(object(), plan)
        self.assertIn("Refusing to create", str(ctx.exception))

    def test_creation_is_two_calls_not_one(self):
        # Since 2024-10 productCreate does not accept variants; collapsing the
        # two back into one call is how this breaks on the next version bump.
        calls: list[str] = []

        class _Envelope:
            def __init__(self, payload):
                self.payload = payload
                self.warnings = []

        class _Connector:
            def create_draft_product(self, **kw):
                calls.append("create_draft_product")
                return _Envelope({"product_id": "gid://shopify/Product/1",
                                  "handle": kw["handle"], "status": "DRAFT"})

            def add_variants(self, product_id, variants):
                calls.append("add_variants")
                return _Envelope([{"variant_id": "v1"}])

        cand = candidate()
        plan = build_product_plan(POLICY, cand,
                                  generate_listing(cand, marketplace="shopify"))
        result = publish_plan(_Connector(), plan)
        self.assertEqual(calls, ["create_draft_product", "add_variants"])
        self.assertEqual(result["status"], "DRAFT")


if __name__ == "__main__":
    unittest.main()
