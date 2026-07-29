"""Creative and production tests.

Weighted towards what the engine refuses to produce. Generating ten ideas is
easy; the value is that the eleventh is not invented, that a stage direction
never becomes an on-screen caption, and that a package with an unfilled slot
cannot be marked ready to shoot.
"""

from __future__ import annotations

import re
import unittest

from operator_core.config import load_policy
from operator_core.content import build_content_plan
from operator_core.creative import (
    ANGLES,
    ANGLES_BY_KEY,
    CTA_VARIATIONS,
    build_captions,
    build_creative_bank,
    build_cta_variations,
    build_hook_bank,
    build_video_ideas,
    check_originality,
    unfilled_slots,
)
from operator_core.models import ProductCandidate, Supplier
from operator_core.production import (
    CANVAS,
    MAX_CUE_CHARS,
    SAFE_AREA,
    _format_timecode,
    build_cues,
    build_production_package,
    build_scenes,
    extract_spoken,
    split_for_reading,
)

SUPPLIER = Supplier(supplier_id="S1", name="Test Supplier", country="CN",
                    rating=4.9, unit_cost=9.40, moq=50,
                    shipping_cost_per_unit=2.10, shipping_days=11)


def candidate(**overrides) -> ProductCandidate:
    base = dict(
        sku="BAMBOO-ORG-01", title="Expandable Bamboo Drawer Organizer",
        category="Home Storage", marketplace="tiktok", target_price=34.99,
        supplier=SUPPLIER, est_monthly_demand_units=900,
        keywords=["drawer organizer", "bamboo", "expandable"],
        description="Expandable bamboo drawer organizer for kitchen utensils",
    )
    base.update(overrides)
    return ProductCandidate(**base)


def flat(c: ProductCandidate = None) -> ProductCandidate:
    """A product with no visual transformation — blocks several angles."""
    return candidate(sku="PLAIN-01", title="Cotton Tea Towel Set",
                     keywords=["tea towel", "cotton"],
                     description="A set of plain cotton tea towels")


class TestAngles(unittest.TestCase):
    def test_ten_named_angles_exist(self):
        self.assertEqual(len(ANGLES), 10)

    def test_every_angle_says_when_it_fails(self):
        # The failure mode is the useful half. An angle brief that only says
        # when something works gets used everywhere.
        for angle in ANGLES:
            self.assertTrue(angle.fails_when.strip(), f"{angle.key} has no failure mode")
            self.assertTrue(angle.structure.strip())

    def test_named_angles_cover_the_brief(self):
        for key in ("problem_solution", "before_after", "demonstration",
                    "comparison", "top_five", "pov", "storytelling", "gift",
                    "satisfying", "trend"):
            self.assertIn(key, ANGLES_BY_KEY)


class TestIdeaGeneration(unittest.TestCase):
    def test_ten_ideas_for_a_rich_product(self):
        ideas, _blocked = build_video_ideas(candidate())
        self.assertEqual(len(ideas), 10)
        self.assertEqual(len({i.idea_id for i in ideas}), 10)

    def test_angles_needing_absent_facts_are_blocked_with_a_reason(self):
        _ideas, blocked = build_video_ideas(candidate())
        keys = {b["angle"] for b in blocked}
        # No verified story and no occasion were supplied.
        self.assertIn("storytelling", keys)
        self.assertIn("gift", keys)
        for entry in blocked:
            self.assertTrue(entry["reason"].strip())

    def test_supplying_the_fact_unblocks_the_angle(self):
        _ideas, blocked = build_video_ideas(
            candidate(), true_story="A customer wrote in that it survived a move.",
            seasonal_window="a housewarming")
        keys = {b["angle"] for b in blocked}
        self.assertNotIn("storytelling", keys)
        self.assertNotIn("gift", keys)

    def test_visual_angles_are_blocked_without_a_transformation(self):
        _ideas, blocked = build_video_ideas(flat())
        keys = {b["angle"] for b in blocked}
        self.assertIn("before_after", keys)
        self.assertIn("satisfying", keys)

    def test_trend_angle_is_templated_not_fabricated(self):
        ideas, _blocked = build_video_ideas(candidate())
        trend = next(i for i in ideas if i.angle == "trend")
        self.assertFalse(trend.ready_to_shoot)
        self.assertTrue(trend.unfilled_slots)
        self.assertIn("ToS", trend.blocked_reason)

    def test_supplied_trend_makes_the_angle_shootable(self):
        ideas, _blocked = build_video_ideas(
            candidate(), live_trend="the 'tell me without telling me' format")
        trend = next(i for i in ideas if i.angle == "trend")
        self.assertTrue(trend.ready_to_shoot)

    def test_shootable_count_is_reported_separately_from_generated(self):
        bank = build_creative_bank(candidate())
        coverage = bank.coverage()
        self.assertEqual(coverage["ideas_generated"], 10)
        self.assertLess(coverage["ideas_ready_to_shoot"], 10)
        self.assertEqual(
            coverage["ideas_ready_to_shoot"] + coverage["ideas_needing_input"], 10)


class TestOriginality(unittest.TestCase):
    def test_named_account_is_flagged(self):
        issues = check_originality("Shoot it the same way @homehacksguy did.")
        self.assertTrue(issues)

    def test_recreate_instruction_is_flagged(self):
        self.assertTrue(check_originality("Recreate that viral video from last week."))

    def test_original_brief_passes(self):
        self.assertEqual(
            check_originality("Open on the drawer, hands in frame, no intro card."),
            [])

    def test_generated_ideas_are_original(self):
        bank = build_creative_bank(candidate())
        for idea in bank.ideas:
            text = " ".join([idea.premise, idea.opening_shot, idea.payoff])
            self.assertEqual(check_originality(text), [], idea.idea_id)


class TestHooksAndCaptions(unittest.TestCase):
    def test_ten_hooks_across_different_angles(self):
        hooks = build_hook_bank(candidate())
        self.assertEqual(len(hooks), 10)
        # The point is variety of opening, not synonyms of one line.
        self.assertGreaterEqual(len({h["angle"] for h in hooks}), 5)

    def test_social_proof_hook_needs_a_verified_count(self):
        without = build_hook_bank(candidate())
        self.assertFalse(any(h["angle"] == "social_proof" for h in without))
        with_count = build_hook_bank(candidate(), verified_order_count=1840)
        self.assertTrue(any("1,840" in h["line"] for h in with_count))

    def test_visual_hook_needs_a_real_transformation(self):
        self.assertFalse(
            any(h["angle"] == "before_after" for h in build_hook_bank(flat())))
        self.assertTrue(
            any(h["angle"] == "before_after" for h in build_hook_bank(candidate())))

    def test_every_hook_is_claim_screened(self):
        for hook in build_hook_bank(candidate()):
            self.assertIn("compliance", hook)
            self.assertEqual(hook["compliance"], "clear", hook["line"])

    def test_ten_captions_including_a_plain_control(self):
        captions = build_captions(candidate())
        self.assertEqual(len(captions), 10)
        shapes = {c["shape"] for c in captions}
        self.assertIn("plain", shapes)

    def test_plain_control_carries_no_hashtags(self):
        # It is the control. Adding tags makes it a different test.
        plain = next(c for c in build_captions(candidate()) if c["shape"] == "plain")
        self.assertNotIn("#", plain["text"])


class TestCTAGuards(unittest.TestCase):
    def test_factual_promises_are_withheld_by_default(self):
        lines = {c["variant"] for c in build_cta_variations()}
        # These claim something about the business that may not be true.
        self.assertNotIn("bundle", lines)
        self.assertNotIn("objection", lines)
        self.assertNotIn("scarcity_honest", lines)

    def test_confirmed_facts_unlock_their_variants(self):
        lines = {c["variant"] for c in build_cta_variations(
            has_bundle=True, free_returns=True, restock_is_slow=True)}
        self.assertIn("bundle", lines)
        self.assertIn("objection", lines)
        self.assertIn("scarcity_honest", lines)

    def test_soft_cta_is_offered_first(self):
        # Organic reach is suppressed by a hard sell, so the default order runs
        # weakest-sell first.
        self.assertEqual(build_cta_variations()[0]["variant"], "passive")
        self.assertEqual(CTA_VARIATIONS[-1][0], "objection")


# ---------------------------------------------------------------------------
class TestTimecodes(unittest.TestCase):
    def test_srt_format(self):
        self.assertEqual(_format_timecode(0, srt=True), "00:00:00,000")
        self.assertEqual(_format_timecode(65.25, srt=True), "00:01:05,250")

    def test_millisecond_rounding_does_not_produce_1000(self):
        self.assertNotIn(",1000", _format_timecode(2.9999, srt=True))

    def test_human_format(self):
        self.assertEqual(_format_timecode(65.25), "01:05.2")


class TestSpokenVersusDirection(unittest.TestCase):
    """The distinction that stops a stage direction becoming a caption."""

    def test_quoted_audio_is_speech(self):
        text, is_direction = extract_spoken('VO: "It fits the whole drawer."')
        self.assertEqual(text, "It fits the whole drawer.")
        self.assertFalse(is_direction)

    def test_unquoted_vo_is_a_direction(self):
        text, is_direction = extract_spoken("VO: the specific thing it does differently.")
        self.assertTrue(is_direction)
        self.assertIn("specific thing", text)

    def test_diegetic_audio_produces_no_cue(self):
        text, is_direction = extract_spoken("Diegetic sound only. Let it be audible.")
        self.assertEqual(text, "")
        self.assertFalse(is_direction)

    def test_directions_become_blockers_not_captions(self):
        policy = load_policy()
        plan = build_content_plan(policy, candidate(), unit_margin=12.0)
        pkg = build_production_package(concept=plan.concepts[0],
                                       title="Bamboo Organizer")
        self.assertFalse(pkg.ready)
        self.assertTrue(any("direction, not a line" in b for b in pkg.blockers))
        # And crucially the direction text is nowhere in the subtitles.
        self.assertNotIn("the specific thing it does differently", pkg.srt())


class TestCaptionSplitting(unittest.TestCase):
    def test_short_line_is_one_chunk(self):
        self.assertEqual(split_for_reading("It fits the drawer."),
                         ["It fits the drawer."])

    def test_long_line_is_split_within_the_readable_width(self):
        long = ("I genuinely cannot believe how much room this freed up in the "
                "kitchen drawer, and it took about four seconds to set up.")
        for chunk in split_for_reading(long):
            self.assertLessEqual(len(chunk), MAX_CUE_CHARS)

    def test_splitting_prefers_clause_boundaries(self):
        chunks = split_for_reading(
            "It expands to fit the drawer, and it stays put when you open it.")
        self.assertTrue(chunks[0].endswith(","))

    def test_empty_input_produces_no_cues(self):
        self.assertEqual(split_for_reading("   "), [])


class TestPackageAssembly(unittest.TestCase):
    def _package(self, **kwargs):
        policy = load_policy()
        cand = kwargs.pop("cand", candidate())
        plan = build_content_plan(policy, cand, unit_margin=12.0)
        bank = build_creative_bank(cand)
        return build_production_package(
            concept=plan.concepts[0], idea=bank.ideas[0], title=cand.title,
            hashtags=bank.hashtags, **kwargs)

    def test_sku_survives_hyphens_in_the_utm_tag(self):
        # Splitting a concept id on the first hyphen turns BAMBOO-ORG-01 into
        # BAMBOO and silently mis-tags every video's attribution.
        pkg = self._package()
        self.assertEqual(pkg.sku, "BAMBOO-ORG-01")
        self.assertIn("utm_campaign=BAMBOO-ORG-01",
                      pkg.export_metadata["tracking_suffix"])

    def test_utm_content_identifies_the_individual_video(self):
        # Without this the journey says "tiktok" for every video ever posted.
        pkg = self._package()
        self.assertIn(f"utm_content={pkg.package_id}",
                      pkg.export_metadata["tracking_suffix"])

    def test_scenes_have_contiguous_timecodes(self):
        pkg = self._package()
        for earlier, later in zip(pkg.scenes, pkg.scenes[1:]):
            self.assertEqual(earlier.end, later.start)
        self.assertEqual(pkg.scenes[0].start, 0.0)

    def test_runtime_matches_the_last_scene(self):
        pkg = self._package()
        self.assertEqual(pkg.runtime, pkg.scenes[-1].end)

    def test_srt_is_well_formed(self):
        pkg = self._package()
        blocks = [b for b in pkg.srt().strip().split("\n\n") if b.strip()]
        self.assertTrue(blocks)
        for i, block in enumerate(blocks, start=1):
            lines = block.strip().splitlines()
            self.assertEqual(lines[0], str(i))
            self.assertRegex(
                lines[1],
                r"^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}$")
            self.assertTrue(lines[2].strip())

    def test_cues_never_run_past_their_scene(self):
        pkg = self._package()
        for cue in pkg.cues:
            scene = next(s for s in pkg.scenes
                         if s.start <= cue.start < s.end)
            self.assertLessEqual(cue.end, scene.end + 0.001)

    def test_cues_do_not_overlap(self):
        pkg = self._package()
        for earlier, later in zip(pkg.cues, pkg.cues[1:]):
            self.assertLessEqual(earlier.end, later.start + 0.001)

    def test_music_is_guidance_never_a_named_track(self):
        pkg = self._package()
        self.assertIn("Commercial Music Library", pkg.music["primary"])
        self.assertIn("Do not burn music into the export", pkg.music["trap"])

    def test_export_metadata_is_vertical(self):
        pkg = self._package()
        self.assertEqual(pkg.export_metadata["aspect"], "9:16")
        self.assertEqual(pkg.export_metadata["resolution"], "1080x1920")
        self.assertEqual(pkg.export_metadata["safe_area_px"], SAFE_AREA)

    def test_package_without_a_shot_list_is_not_ready(self):
        bank = build_creative_bank(candidate())
        pkg = build_production_package(idea=bank.ideas[0], sku="X", title="X")
        self.assertFalse(pkg.ready)
        self.assertTrue(any("No shot list" in b for b in pkg.blockers))

    def test_unfilled_slot_blocks_readiness(self):
        bank = build_creative_bank(candidate())
        trend_idea = next(i for i in bank.ideas if i.angle == "trend")
        policy = load_policy()
        plan = build_content_plan(policy, candidate(), unit_margin=12.0)
        pkg = build_production_package(concept=plan.concepts[0], idea=trend_idea,
                                       title="X")
        self.assertFalse(pkg.ready)
        self.assertTrue(any("unfilled" in b for b in pkg.blockers))

    def test_call_sheet_states_when_it_is_not_shootable(self):
        pkg = self._package()
        if not pkg.ready:
            self.assertIn("NOT READY TO SHOOT", pkg.call_sheet())

    def test_thumbnail_ideas_include_a_no_text_control(self):
        pkg = self._package()
        self.assertTrue(any(not t["text_overlay"] for t in pkg.thumbnail_ideas))

    def test_transformation_adds_a_split_cover(self):
        pkg = self._package(transformation="it expands to fit the whole drawer")
        self.assertTrue(any("before/after" in t["concept"].lower()
                            for t in pkg.thumbnail_ideas))


class TestRuntimeWarnings(unittest.TestCase):
    def _scenes(self, seconds: float):
        from operator_core.content import ShotListItem
        return build_scenes([ShotListItem(1, seconds, "shot", 'VO: "hi"', "", "why")],
                            "UGC")

    def test_overlong_cut_is_warned_about(self):
        from operator_core.content import Hook, ShotListItem, VideoConcept
        concept = VideoConcept(
            concept_id="X-C1", format="UGC",
            hook=Hook("a", "Line", "why"), premise="p",
            shot_list=[ShotListItem(1, 80.0, "shot", 'VO: "hello"', "", "why")],
            voiceover="v", caption="c", hashtags=[], call_to_action="cta",
            estimated_seconds=80.0)
        pkg = build_production_package(concept=concept, sku="X", title="X")
        self.assertTrue(any("Completion rate" in w for w in pkg.warnings))

    def test_very_short_cut_warns_about_loop_inflated_views(self):
        from operator_core.content import Hook, ShotListItem, VideoConcept
        concept = VideoConcept(
            concept_id="X-C1", format="UGC",
            hook=Hook("a", "Line", "why"), premise="p",
            shot_list=[ShotListItem(1, 3.0, "shot", 'VO: "hi"', "", "why")],
            voiceover="v", caption="c", hashtags=[], call_to_action="cta",
            estimated_seconds=3.0)
        pkg = build_production_package(concept=concept, sku="X", title="X")
        self.assertTrue(any("loop" in w for w in pkg.warnings))


if __name__ == "__main__":
    unittest.main()
