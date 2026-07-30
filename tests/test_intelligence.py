"""Learning, research, and dashboard tests.

The theme: these three modules are where a system talks itself into things. A
learning engine that always names a best hook, a research pipeline that always
has a top opportunity, and a dashboard that renders a missing number as zero
each produce a confident business that is wrong in a way nobody can see. Most
of what follows asserts the refusals.
"""

from __future__ import annotations

import unittest
from datetime import date

from operator_core.config import load_policy
from operator_core.dashboard import (
    PROVENANCE_BANNER,
    Tile,
    _pct_change,
    _worst_provenance,
    build_dashboard,
    render_html,
    render_terminal,
)
from operator_core.learning import (
    DIRECTIONAL,
    INSUFFICIENT,
    MIN_OBSERVATIONS_PER_GROUP,
    MIN_TOTAL_OBSERVATIONS,
    SUPPORTED,
    compare_groups,
    learning_report,
    median,
    what_works,
)
from operator_core.research import (
    RESEARCH_QUESTIONS,
    Opportunity,
    SourceReading,
    coverage_report,
    rank_pipeline,
    score_opportunity,
    unanswered_questions,
)

POLICY = load_policy()


def video(package_id: str, *, angle: str = "problem_solution", views: int = 40000,
          clicks: int | None = 400, hook: str = "problem_callout",
          fmt: str = "UGC", cta: str = "passive", weekday: int = 2,
          hour: int = 19) -> dict:
    return {"package_id": package_id, "angle": angle, "hook_archetype": hook,
            "format": fmt, "cta_variant": cta, "views": views,
            "link_clicks": clicks, "weekday": weekday, "hour": hour}


# ---------------------------------------------------------------------------
class TestGroupComparison(unittest.TestCase):
    def test_too_few_observations_overall(self):
        rows = [video(str(i)) for i in range(4)]
        finding = compare_groups(rows, dimension="angle", metric="views")
        self.assertEqual(finding.verdict, INSUFFICIENT)
        self.assertGreater(finding.observations_needed, 0)
        self.assertIn("invent a finding", finding.reason)

    def test_one_qualifying_group_cannot_be_compared(self):
        rows = [video(str(i), angle="problem_solution") for i in range(12)]
        rows += [video("odd", angle="pov")]
        finding = compare_groups(rows, dimension="angle", metric="views")
        self.assertEqual(finding.verdict, INSUFFICIENT)
        self.assertIn("Concentrate output", finding.reason)

    def test_small_groups_are_excluded_and_reported(self):
        rows = [video(str(i), angle="a") for i in range(6)]
        rows += [video(f"b{i}", angle="b") for i in range(6)]
        rows += [video("c1", angle="c")]
        finding = compare_groups(rows, dimension="angle", metric="views")
        excluded = {e["group"] for e in finding.excluded_groups}
        self.assertIn("c", excluded)

    def test_leader_inside_the_noise_is_directional_not_supported(self):
        # Group A: 1000..5000. Group B: 900..4900. A leads by 100 while each
        # group varies by 4000 internally. That is not a finding.
        rows = [video(f"a{i}", angle="a", views=v)
                for i, v in enumerate([1000, 2000, 3000, 4000, 5000])]
        rows += [video(f"b{i}", angle="b", views=v)
                 for i, v in enumerate([900, 1900, 2900, 3900, 4900])]
        rows += [video(f"c{i}", angle="a", views=3000) for i in range(2)]
        rows += [video(f"d{i}", angle="b", views=2900) for i in range(2)]
        finding = compare_groups(rows, dimension="angle", metric="views")
        self.assertEqual(finding.verdict, DIRECTIONAL)
        self.assertIn("not a basis for cutting", finding.reason)

    def test_clear_separation_is_supported(self):
        rows = [video(f"a{i}", angle="a", views=v)
                for i, v in enumerate([50000, 51000, 52000, 49000, 50500, 51500])]
        rows += [video(f"b{i}", angle="b", views=v)
                 for i, v in enumerate([1000, 1100, 900, 1200, 1050, 950])]
        finding = compare_groups(rows, dimension="angle", metric="views")
        self.assertEqual(finding.verdict, SUPPORTED)
        self.assertEqual(finding.leader, "a")

    def test_unmeasured_rows_are_skipped_not_counted_as_zero(self):
        # An unmeasured video is not a video that performed badly.
        rows = [video(f"a{i}", angle="a", clicks=None) for i in range(6)]
        rows += [video(f"b{i}", angle="b", clicks=400) for i in range(6)]
        finding = what_works(rows)["angle"]
        groups = {g.group for g in finding.groups}
        self.assertNotIn("a", groups)

    def test_median_resists_one_breakout(self):
        rows = [video(f"a{i}", angle="a", views=1000) for i in range(5)]
        rows += [video("a-viral", angle="a", views=10_000_000)]
        rows += [video(f"b{i}", angle="b", views=1100) for i in range(6)]
        finding = compare_groups(rows, dimension="angle", metric="views")
        group_a = next(g for g in finding.groups if g.group == "a")
        self.assertEqual(group_a.median, 1000.0)
        # And the breakout does not hand group a the win.
        self.assertEqual(finding.leader, "b")

    def test_median_helper(self):
        self.assertEqual(median([1, 2, 3]), 2)
        self.assertEqual(median([1, 2, 3, 4]), 2.5)


class TestLearningReport(unittest.TestCase):
    def test_new_account_gets_an_honest_headline(self):
        report = learning_report(video_rows=[video(str(i)) for i in range(3)])
        self.assertEqual(report["actionable_findings"], 0)
        self.assertIn("expected state for a new account", report["headline"])

    def test_actionable_findings_are_counted(self):
        rows = [video(f"a{i}", angle="a", views=50000, clicks=2000)
                for i in range(6)]
        rows += [video(f"b{i}", angle="b", views=50000, clicks=100)
                 for i in range(6)]
        report = learning_report(video_rows=rows)
        self.assertGreaterEqual(report["actionable_findings"], 1)
        self.assertEqual(report["findings"]["angle"]["leader"], "a")

    def test_reach_and_conversion_are_asked_separately(self):
        # An angle that gets reach and never gets clicks is an expensive kind
        # of success; ranking on views alone would promote it.
        report = learning_report(video_rows=[video(str(i)) for i in range(14)])
        self.assertIn("angle", report["findings"])
        self.assertIn("reach_by_angle", report["findings"])
        self.assertEqual(report["findings"]["angle"]["metric"], "click_rate_pct")
        self.assertEqual(report["findings"]["reach_by_angle"]["metric"], "views")


# ---------------------------------------------------------------------------
class TestOpportunityScoring(unittest.TestCase):
    def _opportunity(self, readings, **kw):
        base = dict(opportunity_id="O1", title="Test product", category="Home",
                    stage="SCORED", discovered_at="2026-07-25")
        base.update(kw)
        return Opportunity(readings=readings, **base)

    def test_absent_sources_contribute_nothing_not_a_neutral_value(self):
        # A neutral default drags every opportunity toward the same score and
        # makes the ranking a function of what is missing.
        strong = self._opportunity([
            SourceReading("tiktok_product_velocity", "positive", 1.0, origin="live")])
        scored = score_opportunity(strong, POLICY)
        self.assertGreater(scored.raw_score, 90)
        self.assertLess(scored.coverage_pct, 30)
        self.assertLess(scored.effective_score, scored.raw_score)

    def test_effective_score_ranks_knowledge_over_appearance(self):
        known = self._opportunity([
            SourceReading("tiktok_product_velocity", "positive", 0.7, origin="live"),
            SourceReading("amazon_sales_rank", "positive", 0.7, origin="live"),
        ])
        flashy = self._opportunity([
            SourceReading("amazon_sales_rank", "positive", 1.0, origin="live"),
        ], opportunity_id="O2")
        self.assertGreater(score_opportunity(known, POLICY).effective_score,
                           score_opportunity(flashy, POLICY).effective_score)

    def test_single_source_cannot_be_promoted(self):
        scored = score_opportunity(self._opportunity([
            SourceReading("amazon_sales_rank", "positive", 1.0, origin="live")]),
            POLICY)
        self.assertFalse(scored.promotable)
        self.assertTrue(any("anecdote" in b for b in scored.blockers))

    def test_only_negative_evidence_blocks_further_research(self):
        scored = score_opportunity(self._opportunity([
            SourceReading("amazon_sales_rank", "negative", 0.8, origin="live"),
            SourceReading("tiktok_product_velocity", "negative", 0.7, origin="live"),
        ]), POLICY)
        self.assertFalse(scored.promotable)
        self.assertTrue(any("does not need more research" in b
                            for b in scored.blockers))

    def test_unweighted_source_is_ignored_rather_than_scored(self):
        scored = score_opportunity(self._opportunity([
            SourceReading("a_source_nobody_configured", "positive", 1.0)]), POLICY)
        self.assertEqual(scored.coverage_pct, 0.0)
        self.assertEqual(scored.effective_score, 0.0)

    def test_low_coverage_carries_an_explanation(self):
        scored = score_opportunity(self._opportunity([
            SourceReading("amazon_sales_rank", "positive", 0.8, origin="live")]),
            POLICY)
        self.assertIn("contribute nothing", scored.note)

    def test_unanswered_questions_are_framed_as_questions(self):
        opportunity = self._opportunity([
            SourceReading("amazon_sales_rank", "positive", 0.8, origin="live")])
        questions = unanswered_questions(opportunity)
        self.assertTrue(questions)
        self.assertTrue(all(q["question"].endswith("?") for q in questions))
        self.assertTrue(any("Unanswerable" in q["status"] for q in questions))


class TestPipeline(unittest.TestCase):
    def test_stale_opportunities_are_retired_with_a_reason(self):
        stale = Opportunity(opportunity_id="OLD", title="Forgotten", category="Home",
                            stage="DISCOVERED", discovered_at="2026-05-01")
        result = rank_pipeline([stale], POLICY, today=date(2026, 7, 29))
        self.assertEqual(len(result["retired"]), 1)
        self.assertIn("demand it was scored on has moved",
                      result["retired"][0]["reason"])

    def test_terminal_stages_are_not_ranked(self):
        rejected = Opportunity(opportunity_id="R", title="No", category="Home",
                               stage="REJECTED", discovered_at="2026-07-28")
        result = rank_pipeline([rejected], POLICY, today=date(2026, 7, 29))
        self.assertEqual(result["active"], 0)

    def test_coverage_shortfall_is_always_stated(self):
        result = rank_pipeline([], POLICY, today=date(2026, 7, 29))
        self.assertTrue(any("no connector" in w for w in result["warnings"]))

    def test_ranking_is_by_effective_score(self):
        known = Opportunity(
            opportunity_id="A", title="Well researched", category="Home",
            stage="SCORED", discovered_at="2026-07-28", readings=[
                SourceReading("tiktok_product_velocity", "positive", 0.6, origin="live"),
                SourceReading("amazon_sales_rank", "positive", 0.6, origin="live")])
        flashy = Opportunity(
            opportunity_id="B", title="Looks amazing", category="Home",
            stage="SCORED", discovered_at="2026-07-28", readings=[
                SourceReading("amazon_sales_rank", "positive", 1.0, origin="live")])
        result = rank_pipeline([flashy, known], POLICY, today=date(2026, 7, 29))
        self.assertEqual(result["ranked"][0]["opportunity_id"], "A")


class TestCoverageReport(unittest.TestCase):
    def test_reports_both_halves(self):
        report = coverage_report()
        self.assertEqual(report["sources_connected"] + report["sources_missing"],
                         report["sources_total"])
        self.assertGreater(report["sources_missing"], 0)

    def test_every_missing_source_says_what_it_would_need(self):
        for entry in coverage_report()["missing"]:
            self.assertTrue(entry["would_need"].strip())

    def test_note_forbids_substituting_impressions(self):
        self.assertIn("Do not substitute", coverage_report()["note"])


# ---------------------------------------------------------------------------
class TestDashboard(unittest.TestCase):
    def _rows(self, **kw):
        base = {"metric_date": "2026-07-28", "channel": "tiktok", "sessions": None,
                "orders": 10, "revenue": 400.0, "refunds": 0.0,
                "new_customers": 10, "data_source": "live"}
        base.update(kw)
        return [base]

    def test_unknown_value_renders_as_unknown_not_zero(self):
        dashboard = build_dashboard(storefront_rows=self._rows())
        tile = dashboard.tile("Conversion rate")
        self.assertFalse(tile.known)
        self.assertEqual(tile.display(), "—")
        self.assertIn("back-computed", tile.reason_missing)

    def test_profit_is_unknown_without_cost_data(self):
        # Revenue is not profit and the difference is the entire business.
        dashboard = build_dashboard(storefront_rows=self._rows())
        tile = dashboard.tile("Estimated profit")
        self.assertFalse(tile.known)
        self.assertIn("Revenue is not profit", tile.reason_missing)

    def test_profit_is_computed_when_cost_data_exists(self):
        dashboard = build_dashboard(
            storefront_rows=self._rows(),
            product_rows=[{"sku": "A", "units": 10, "revenue": 400.0,
                           "net_profit": 120.0, "data_source": "live"}])
        self.assertEqual(dashboard.tile("Estimated profit").value, 120.0)

    def test_worst_provenance_sets_the_banner(self):
        self.assertEqual(_worst_provenance(["live", "seed", "live"]), "seed")
        self.assertEqual(_worst_provenance(["live", "import"]), "import")

    def test_no_inputs_is_its_own_state_not_unverified(self):
        # "nothing was synced" and "an input failed to declare itself" need
        # different responses; conflating them sends someone hunting a bug.
        self.assertEqual(_worst_provenance([]), "none")
        dashboard = build_dashboard(storefront_rows=[])
        self.assertIn("NO DATA", dashboard.banner)
        self.assertIn("not because the business produced zero", dashboard.banner)

    def test_seed_data_banner_cannot_be_absent(self):
        dashboard = build_dashboard(storefront_rows=self._rows(data_source="seed"))
        self.assertIn("NOT REAL BUSINESS NUMBERS", dashboard.banner)
        self.assertIn("NOT REAL BUSINESS NUMBERS", render_terminal(dashboard))
        self.assertIn("NOT REAL BUSINESS NUMBERS", render_html(dashboard))

    def test_live_data_has_no_banner(self):
        dashboard = build_dashboard(storefront_rows=self._rows())
        self.assertEqual(dashboard.banner, "")

    def test_imported_data_keeps_its_own_banner(self):
        dashboard = build_dashboard(storefront_rows=self._rows(data_source="import"))
        self.assertIn("as of the export date", dashboard.banner)

    def test_growth_from_zero_is_not_infinite(self):
        # A first sale is not spectacular growth.
        self.assertIsNone(_pct_change(400.0, 0.0))
        self.assertEqual(_pct_change(150.0, 100.0), 50.0)

    def test_high_unattributed_share_is_warned_about(self):
        rows = self._rows() + [{"metric_date": "2026-07-28",
                                "channel": "unattributed", "sessions": None,
                                "orders": 20, "revenue": 800.0, "refunds": 0.0,
                                "new_customers": 20, "data_source": "live"}]
        dashboard = build_dashboard(storefront_rows=rows)
        self.assertTrue(any("understate every channel" in w
                            for w in dashboard.warnings))

    def test_low_view_videos_are_excluded_from_top_videos(self):
        dashboard = build_dashboard(
            storefront_rows=self._rows(),
            video_rows=[video("v1", views=300, clicks=30)])
        panel = next(p for p in dashboard.panels if p.title == "Top videos")
        self.assertEqual(panel.rows, [])
        self.assertIn("nobody saw", panel.empty_message)

    def test_missing_video_data_becomes_a_task(self):
        dashboard = build_dashboard(storefront_rows=self._rows())
        self.assertTrue(any("video-metrics" in t for t in dashboard.tasks))

    def test_pending_approvals_become_tasks(self):
        dashboard = build_dashboard(
            storefront_rows=self._rows(),
            pending_approvals=[{"action": "publish_product", "sku": "A",
                                "action_id": "act-1"}])
        self.assertTrue(any("act-1" in t for t in dashboard.tasks))

    def test_terminal_render_includes_every_panel(self):
        dashboard = build_dashboard(storefront_rows=self._rows(),
                                    video_rows=[video("v1")])
        rendered = render_terminal(dashboard)
        for panel in dashboard.panels:
            self.assertIn(panel.title, rendered)

    def test_html_is_self_contained(self):
        # No network and no build step, same constraint as the rest of the system.
        markup = render_html(build_dashboard(storefront_rows=self._rows()))
        self.assertNotIn("http://", markup)
        self.assertNotIn("<script", markup)
        self.assertIn("<style>", markup)

    def test_html_escapes_untrusted_text(self):
        rows = self._rows(channel="<script>alert(1)</script>")
        markup = render_html(build_dashboard(storefront_rows=rows))
        self.assertNotIn("<script>alert(1)</script>", markup)
        self.assertIn("&lt;script&gt;", markup)

    def test_both_renderers_agree_on_missing_values(self):
        # Both surfaces must carry the *reason*, not just a dash. A dashboard
        # that explains itself in one medium and not the other lets the
        # unexplained one become the one people quote.
        import html as html_module

        dashboard = build_dashboard(storefront_rows=self._rows())
        # The terminal renderer wraps, so compare on normalised whitespace.
        terminal = " ".join(render_terminal(dashboard).split())
        markup = " ".join(html_module.unescape(render_html(dashboard)).split())
        unknown = [t for t in dashboard.tiles if not t.known and t.reason_missing]
        self.assertTrue(unknown, "expected at least one unknown tile")
        for tile in unknown:
            reason = " ".join(tile.reason_missing.split())
            self.assertIn(reason, terminal, tile.label)
            self.assertIn(reason, markup, tile.label)


if __name__ == "__main__":
    unittest.main()


class TestTopProductsAggregation(unittest.TestCase):
    """daily_metrics is one row per day per SKU. Rank products, not days."""

    def _rows(self):
        return [{"metric_date": "2026-07-28", "channel": "tiktok", "orders": 5,
                 "revenue": 200.0, "refunds": 0.0, "new_customers": 5,
                 "sessions": None, "data_source": "live"}]

    def test_the_same_sku_appears_once(self):
        products = [{"sku": "A", "units": 2, "revenue": 80.0, "net_profit": 30.0,
                     "data_source": "live"} for _ in range(6)]
        products.append({"sku": "B", "units": 1, "revenue": 40.0,
                         "net_profit": 20.0, "data_source": "live"})
        dashboard = build_dashboard(storefront_rows=self._rows(),
                                    product_rows=products)
        panel = next(p for p in dashboard.panels
                     if p.title == "Top products by profit")
        skus = [r["SKU"] for r in panel.rows]
        self.assertEqual(sorted(skus), ["A", "B"])
        self.assertEqual(len(skus), len(set(skus)))

    def test_totals_are_summed_across_days(self):
        products = [{"sku": "A", "units": 2, "revenue": 80.0, "net_profit": 30.0,
                     "data_source": "live"} for _ in range(3)]
        dashboard = build_dashboard(storefront_rows=self._rows(),
                                    product_rows=products)
        panel = next(p for p in dashboard.panels
                     if p.title == "Top products by profit")
        self.assertEqual(panel.rows[0]["Units"], 6)
        self.assertEqual(panel.rows[0]["Net profit"], "$90.00")

    def test_ranking_is_by_total_profit_not_best_day(self):
        # B has the single best day; A makes more money overall.
        products = [{"sku": "A", "units": 1, "revenue": 50.0, "net_profit": 20.0,
                     "data_source": "live"} for _ in range(5)]
        products.append({"sku": "B", "units": 1, "revenue": 90.0,
                         "net_profit": 60.0, "data_source": "live"})
        dashboard = build_dashboard(storefront_rows=self._rows(),
                                    product_rows=products)
        panel = next(p for p in dashboard.panels
                     if p.title == "Top products by profit")
        self.assertEqual(panel.rows[0]["SKU"], "A")
