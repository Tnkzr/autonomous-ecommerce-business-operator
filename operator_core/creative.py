"""Creative angle engine: ideas, hooks, captions, and CTAs at volume.

`content.py` builds one considered plan per product. This module builds the
*bank* — ten ideas, ten hooks, ten captions across ten named angles — because
the business model is organic reach, and organic reach is a hit-rate game. One
excellent video that the algorithm does not pick up earns nothing; ten decent
ones across different angles find the audience that exists.

Three rules run through everything here.

**Angles are structures, not scripts.** Each angle describes a shape, what it
is good for, and — more usefully — when it fails. A "before/after" on a product
with no visible change is not a weak video, it is a promise the footage breaks,
and the retention penalty carries to the next post.

**Originality is enforced, not requested.** "Do not copy other creators" is
worthless as a comment in a brief. Every idea here is generated from the
product's own attributes, and `check_originality` screens the output for the
signatures of copied work: named creators, references to recreating someone's
video, and specific sounds this system cannot legally verify.

**A trend we cannot see is left blank.** The viral-trend angle is the one that
cannot be generated honestly: TikTok publishes no API for trending sounds or
formats, and scraping the Creative Center breaches ToS. So the angle emits a
template with the trend slot unfilled and instructions for the human who can
open the app. An invented "trending sound" costs a real shoot day.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .content import (
    BANNED_CLAIM_PATTERNS,
    _derive_problem,
    _derive_transformation,
    build_hashtags,
    check_claims,
)
from .models import ProductCandidate

# A slot the operator refuses to fill because filling it would mean inventing a
# fact. The marker is deliberately loud and machine-detectable: `unfilled_slots`
# counts them, and the production package will not export while any remain.
SLOT = "[FILL: {}]"
SLOT_PATTERN = re.compile(r"\[FILL: ([^\]]+)\]")

# Signatures of copied work. Not a plagiarism detector — a screen against the
# specific ways generated briefs drift into "do what that account did".
COPY_SIGNATURES = (
    (r"\b(?:recreate|remake|copy|duplicate|replicate)\s+(?:the\s+)?"
     r"(?:this|that|their|his|her|@\w+)", "instructs recreating someone else's video"),
    (r"@[A-Za-z0-9_.]{3,}", "names a specific account"),
    (r"\blike\s+@[A-Za-z0-9_.]+", "references another creator's work as a model"),
    (r"\bviral video by\b", "references a specific existing video"),
    (r"\bsame (?:script|edit|format) as\b", "copies another creator's structure"),
)


@dataclass
class Angle:
    """One creative structure."""

    key: str
    name: str
    structure: str
    works_when: str
    fails_when: str
    requires: tuple[str, ...] = ()      # data this angle cannot be built without


# The ten angles. Ordered roughly by how reliably they work for a product with
# no audience yet — the first few need nothing but the product.
ANGLES: tuple[Angle, ...] = (
    Angle(
        key="problem_solution",
        name="Problem / solution",
        structure="Open on the pain in the viewer's own environment. Introduce "
                  "the product only after they have recognised themselves. End "
                  "on the pain being absent, not on the product.",
        works_when="The product solves one specific, nameable irritation.",
        fails_when="The problem has to be explained. If it needs a sentence of "
                   "setup, the viewer has already scrolled.",
    ),
    Angle(
        key="before_after",
        name="Before / after",
        structure="Identical framing, identical lighting, two states. The cut "
                  "between them is the whole video; everything else is context.",
        works_when="There is a genuine, photographable change of state.",
        fails_when="The change is marginal or has to be pointed at. A before/after "
                   "that needs an arrow graphic is a promise the footage breaks, "
                   "and the retention penalty carries to the next post.",
        requires=("visual_transformation",),
    ),
    Angle(
        key="demonstration",
        name="Demonstration with emotional payoff",
        structure="One unbroken take of the mechanism working, held long enough "
                  "to be believed, ending on a genuine human reaction rather "
                  "than a product beauty shot.",
        works_when="The mechanism is satisfying to watch and survives an uncut take.",
        fails_when="The demo needs a cut. A cut mid-mechanism reads as a hidden "
                   "edit and costs more trust than the time it saves.",
        requires=("visual_transformation",),
    ),
    Angle(
        key="comparison",
        name="Comparison",
        structure="The generic category version against this one, same task, "
                  "same conditions, timed or measured on camera.",
        works_when="The difference is observable within the length of a video.",
        fails_when="The comparison is against a named brand — that is a legal "
                   "problem and a platform problem at once. Compare against the "
                   "unbranded category only.",
    ),
    Angle(
        key="top_five",
        name="Top 5 / listicle",
        structure="Five items, the product placed third or fourth — never first "
                  "and never last. Each item gets one line and one shot.",
        works_when="The product sits naturally in a category a viewer is already "
                   "shopping.",
        fails_when="The other four are filler. A list where one entry is real "
                   "and four are padding is transparent, and the comments say so.",
    ),
    Angle(
        key="pov",
        name="POV",
        structure="Second person, present tense, camera as the viewer's eyes. "
                  "The product enters the frame the way it would enter their day.",
        works_when="There is a specific, recognisable moment the product belongs to.",
        fails_when="The POV is generic. 'POV: you found the perfect X' is a format "
                   "with no content and reads as an ad.",
    ),
    Angle(
        key="storytelling",
        name="Storytelling",
        structure="A first-person account with a turn in it. The product is the "
                  "hinge of the story, not its subject.",
        works_when="There is a true story to tell — a real failure the product fixed.",
        fails_when="The story is invented. A fabricated anecdote is the single "
                   "most damaging thing on this list: it is a false claim wearing "
                   "a narrative, and it is the one viewers punish permanently.",
        requires=("true_story",),
    ),
    Angle(
        key="gift",
        name="Gift ideas",
        structure="Framed around a recipient and an occasion, not around the "
                  "product. Price named early — gift viewers filter on it first.",
        works_when="The product is giftable and the occasion is close enough to "
                   "be on the viewer's mind.",
        fails_when="Run out of season. A gift video in the wrong month gets "
                   "reach and no conversion, which teaches the algorithm to send "
                   "the wrong audience.",
        requires=("seasonal_window",),
    ),
    Angle(
        key="satisfying",
        name="Satisfying demonstration",
        structure="No voiceover. Diegetic sound only, close and clean. The "
                  "mechanism repeats three times with slightly different framing.",
        works_when="The action has a texture or sound worth hearing.",
        fails_when="Nothing about the product is sensory. Silence over a static "
                   "object is not ASMR, it is a dead video.",
        requires=("visual_transformation",),
    ),
    Angle(
        key="trend",
        name="Trend participation",
        structure="An existing format or sound, used with the product as the "
                  "subject rather than as a placement.",
        works_when="The trend is live *now* and the product fits it without "
                   "being forced.",
        fails_when="The trend has peaked. Joining a format on its way down gets "
                   "the reach of an ad with the production cost of organic.",
        requires=("live_trend_data",),
    ),
)

ANGLES_BY_KEY = {a.key: a for a in ANGLES}

# CTA variations, weakest-sell to strongest. Organic reach is suppressed by a
# hard sell, so the default is the soft end; the strong variants are for
# audiences that already convert.
CTA_VARIATIONS = (
    ("passive", "It's in my bio if you want one.",
     "Lowest friction and lowest suppression. Default for a cold audience."),
    ("curiosity", "I left the link in my bio — the reviews are the interesting part.",
     "Redirects to social proof instead of asking for a sale."),
    ("utility", "Link's in my bio if you've got the same problem.",
     "Qualifies the click, which lifts landing-page conversion even though it "
     "lowers click volume."),
    ("scarcity_honest", "Link in bio. We restock slowly, that's the only catch.",
     "Only usable when restocking genuinely is slow. Manufactured scarcity is a "
     "consumer-protection problem, not a growth tactic."),
    ("question", "Would you use this? Link's in my bio either way.",
     "Comment bait plus a CTA. Comments lift distribution more than likes do."),
    ("direct", "Tap the link in my bio to get one.",
     "Highest intent, most suppression. Use on retargetable or warm audiences."),
    ("bundle", "Link in bio — it's cheaper with the set.",
     "Raises AOV. Requires the bundle to actually exist on the store."),
    ("objection", "Link in bio. Free returns, so it's a low-risk one to try.",
     "Only if returns genuinely are free. An unfunded promise here is a chargeback."),
)

# Caption shapes. Captions do two jobs — searchable text and a second hook for
# viewers who read before they listen — so half are keyword-bearing and half
# are conversational.
CAPTION_SHAPES = (
    ("hook_echo", "{hook}", "Repeats the hook for muted viewers."),
    ("problem", "If you also have {problem}, this is the fix.", "Keyword-bearing."),
    ("question", "Does anyone else have {problem}? Just me?", "Comment bait."),
    ("confession", "I did not expect this to work as well as it did.",
     "Conversational; pairs with a demo."),
    ("utility", "{title} — the version that actually fits.", "Search-oriented."),
    ("list", "Three things I stopped losing since I got this.", "Implies a list."),
    ("recommendation", "Sending this to the person who needs it most.",
     "Encourages shares, which weigh more than likes."),
    ("understated", "Small thing. Genuinely changed the drawer.",
     "Low-key tone tests better against ad-fatigued audiences."),
    ("timing", "Ordering a second one before I lose this one.", "Implies repeat value."),
    ("plain", "{title}.", "Control. Always keep one plain caption to test against."),
)


@dataclass
class VideoIdea:
    idea_id: str
    angle: str
    angle_name: str
    premise: str
    opening_shot: str
    payoff: str
    why_this_product: str
    risk: str
    unfilled_slots: list[str] = field(default_factory=list)
    blocked_reason: str = ""

    @property
    def ready_to_shoot(self) -> bool:
        return not self.unfilled_slots and not self.blocked_reason


@dataclass
class CreativeBank:
    """Everything generated for one product, plus what could not be."""

    sku: str
    title: str
    ideas: list[VideoIdea]
    hooks: list[dict[str, str]]
    captions: list[dict[str, str]]
    ctas: list[dict[str, str]]
    hashtags: list[str]
    blocked_angles: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def shootable_ideas(self) -> list[VideoIdea]:
        return [i for i in self.ideas if i.ready_to_shoot]

    def coverage(self) -> dict[str, Any]:
        """How much of the bank is usable without further input.

        Reported rather than hidden: a bank of ten ideas where six need a fact
        the operator does not have is a bank of four, and a content calendar
        built on the headline number will run dry mid-week.
        """
        ready = len(self.shootable_ideas)
        return {
            "ideas_generated": len(self.ideas),
            "ideas_ready_to_shoot": ready,
            "ideas_needing_input": len(self.ideas) - ready,
            "angles_blocked": len(self.blocked_angles),
            "angles_available": len(ANGLES) - len(self.blocked_angles),
        }


# ---------------------------------------------------------------------------
# Originality
# ---------------------------------------------------------------------------
def check_originality(text: str) -> list[str]:
    """Flag text that instructs copying another creator's work.

    Runs over generated ideas and briefs. "Never copy existing creators" only
    means something if something checks — and the realistic failure is not
    wholesale plagiarism, it is a brief that says "do the one @someone did".
    """
    found: list[str] = []
    for pattern, why in COPY_SIGNATURES:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            found.append(f"'{match.group(0).strip()}' — {why}.")
    return found


def unfilled_slots(text: str) -> list[str]:
    """The facts a piece of generated copy is deliberately missing."""
    return SLOT_PATTERN.findall(text)


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------
def _available_facts(candidate: ProductCandidate, *,
                     true_story: str | None,
                     seasonal_window: str | None,
                     live_trend: str | None) -> dict[str, Any]:
    """What is actually known, so angles requiring more can be blocked."""
    return {
        "visual_transformation": _derive_transformation(candidate),
        "true_story": true_story,
        "seasonal_window": seasonal_window,
        "live_trend_data": live_trend,
    }


BLOCK_EXPLANATIONS = {
    "visual_transformation": (
        "This product has no detectable visual transformation, so the angle "
        "would be promising a change the footage cannot deliver."),
    "true_story": (
        "No verified customer story was supplied. Generating one would be a "
        "fabricated anecdote — a false claim wearing a narrative, and the kind "
        "viewers punish permanently."),
    "seasonal_window": (
        "No occasion was supplied. A gift video pointed at no occasion gets "
        "reach without conversion, which trains the algorithm on the wrong "
        "audience."),
    "live_trend_data": (
        "No live trend feed is connected. TikTok publishes no API for trending "
        "sounds or formats and scraping the Creative Center breaches ToS, so "
        "this angle cannot be generated — only templated for a human who can "
        "open the app."),
}


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
def _idea_for_angle(angle: Angle, candidate: ProductCandidate,
                    facts: dict[str, Any], index: int) -> VideoIdea:
    problem = _derive_problem(candidate)
    transformation = facts.get("visual_transformation")
    name = candidate.title
    price = candidate.target_price

    builders = {
        "problem_solution": lambda: (
            f"Open mid-irritation: {problem}. The product does not appear until "
            "the viewer has recognised the moment as their own.",
            f"Handheld, waist height, already dealing with {problem}. No intro card.",
            "The final shot is the problem simply absent — no product in frame.",
        ),
        "before_after": lambda: (
            f"Two states of the same space, cut on the beat. {transformation}.",
            "Locked-off shot of the untouched 'before'. Mark the tripod position.",
            f"Identical framing after: {transformation}.",
        ),
        "demonstration": lambda: (
            f"One unbroken take of the mechanism: {transformation}.",
            "Tight on the mechanism, hands entering frame, camera already rolling.",
            "Hold on a real reaction for a full beat after the action finishes.",
        ),
        "comparison": lambda: (
            "The generic category version against this one, same task, timed on "
            "camera. No brand names.",
            "Both items in frame, unlabelled, side by side.",
            "The timer, then the result. Let the number do the work.",
        ),
        "top_five": lambda: (
            f"Five items for {candidate.category.lower() or 'this category'}, "
            f"with {name} placed fourth.",
            "Fast cuts, one shot per item, one line of on-screen text each.",
            "Land on the fourth item and stay there twice as long as the others.",
        ),
        "pov": lambda: (
            f"POV: the exact moment {problem} happens, and this is already there.",
            "Camera at eye level, moving as the viewer would move.",
            "The product resolves the moment without being announced.",
        ),
        "storytelling": lambda: (
            f"A true account: {facts.get('true_story') or SLOT.format('verified customer story')}",
            "Straight to camera, one take, no B-roll for the first eight seconds.",
            "The turn in the story is the product; the ending is the outcome.",
        ),
        "gift": lambda: (
            f"A gift for {facts.get('seasonal_window') or SLOT.format('recipient and occasion')} "
            f"at ${price:,.2f}.",
            "Product being wrapped or handed over, not on a shelf.",
            "Recipient's reaction. Price on screen within the first three seconds.",
        ),
        "satisfying": lambda: (
            f"No voiceover. The mechanism three times, close: {transformation}.",
            "Macro, shallow depth of field, clean diegetic audio.",
            "Third repetition is the longest. End on stillness, not a cut.",
        ),
        "trend": lambda: (
            f"{facts.get('live_trend_data') or SLOT.format('name the live trend or sound from the app')} "
            f"used with {name} as the subject.",
            (f"Open on the first beat of {facts['live_trend_data']}."
             if facts.get("live_trend_data")
             else SLOT.format("opening beat of the chosen format")),
            "Product is the subject of the format, never a placement inside it.",
        ),
    }

    premise, opening, payoff = builders[angle.key]()
    text = " ".join([premise, opening, payoff])

    return VideoIdea(
        idea_id=f"{candidate.sku}-A{index:02d}-{angle.key}",
        angle=angle.key,
        angle_name=angle.name,
        premise=premise,
        opening_shot=opening,
        payoff=payoff,
        why_this_product=angle.works_when,
        risk=angle.fails_when,
        unfilled_slots=unfilled_slots(text),
    )


def build_video_ideas(candidate: ProductCandidate, *, count: int = 10,
                      true_story: str | None = None,
                      seasonal_window: str | None = None,
                      live_trend: str | None = None
                      ) -> tuple[list[VideoIdea], list[dict[str, str]]]:
    """Generate ideas across the angle set.

    Returns the ideas and the angles that had to be blocked. Blocked angles are
    returned rather than dropped: "we generated six of ten" with the reasons is
    a usable answer, and silently returning six looks like the product only
    supports six.
    """
    facts = _available_facts(candidate, true_story=true_story,
                             seasonal_window=seasonal_window,
                             live_trend=live_trend)
    ideas: list[VideoIdea] = []
    blocked: list[dict[str, str]] = []

    for i, angle in enumerate(ANGLES, start=1):
        missing = [r for r in angle.requires if not facts.get(r)]
        if missing and angle.key != "trend":
            blocked.append({
                "angle": angle.key,
                "name": angle.name,
                "missing": ", ".join(missing),
                "reason": BLOCK_EXPLANATIONS.get(missing[0], "Required data absent."),
            })
            continue
        idea = _idea_for_angle(angle, candidate, facts, i)
        if missing:
            # The trend angle is templated rather than blocked: the shape is
            # still useful to whoever opens the app, and the unfilled slot
            # marks it as not shootable.
            idea.blocked_reason = BLOCK_EXPLANATIONS.get(missing[0], "")
        ideas.append(idea)

    # If fewer than requested, add second variants of the angles that worked
    # rather than padding with angles the product cannot support.
    variant = 0
    while len(ideas) < count and ideas:
        base = ideas[variant % max(len([i for i in ideas if i.ready_to_shoot]), 1)]
        variant += 1
        if not base.ready_to_shoot:
            break
        ideas.append(VideoIdea(
            idea_id=f"{base.idea_id}-v{variant + 1}",
            angle=base.angle,
            angle_name=base.angle_name,
            premise=f"Second cut of the {base.angle_name.lower()} angle, "
                    "different opening beat and a shorter payoff.",
            opening_shot=base.opening_shot,
            payoff=base.payoff,
            why_this_product=base.why_this_product,
            risk=("Two cuts of one angle test the hook, not the angle. Only "
                  "worth shooting once the angle itself has a result."),
        ))
        if variant > count * 2:  # pragma: no cover - guards a pathological loop
            break

    return ideas[:count], blocked


def build_hook_bank(candidate: ProductCandidate, *, count: int = 10,
                    review_objection: str | None = None,
                    verified_order_count: int | None = None,
                    live_trend: str | None = None) -> list[dict[str, str]]:
    """Ten hook lines spread across angles.

    Deliberately not ten variations of one line. The hook is the only part of
    the video most viewers see, so the useful test is between *kinds* of
    opening, not between synonyms.
    """
    problem = _derive_problem(candidate)
    transformation = _derive_transformation(candidate)
    category = candidate.category.lower() or "this"

    pool: list[tuple[str, str, str]] = [
        ("problem_solution", f"I can't believe I put up with {problem} for so long.",
         "Names the pain before the product exists. Highest completion for utility items."),
        ("problem_solution", f"Nobody told me {problem} was optional.",
         "Implies the viewer is missing something, without insulting them."),
        ("pov", f"POV: {problem}, and you already fixed it.",
         "Present tense puts the viewer inside the moment rather than watching one."),
        ("comparison", "The cheap version versus this one. Same job.",
         "Sets up an observable test in four words. Never name the rival brand."),
        ("top_five", f"Five things in my {category} I'd buy again.",
         "Borrows the credibility of a list. The product must earn its place in it."),
        ("understated", "This is a small thing and I think about it every day.",
         "Low-key openings test well against audiences fatigued by hard sells."),
        ("utility", "Three uses for this you probably didn't think of.",
         "Extends perceived value for products with one obvious use."),
        ("objection", "I thought this was a gimmick.",
         "Pre-empts the top comment, which is where the objection would land anyway."),
    ]

    if transformation:
        pool.insert(1, ("before_after", f"Wait for it… {transformation}",
                        "Only with a genuine visual change — a broken promise here "
                        "costs retention on the next post too."))
        pool.insert(4, ("satisfying", "No talking, just watch this bit.",
                        "Sets an expectation of sensory payoff. Needs real texture "
                        "or sound to land."))
    # Hooks backed by a real fact go to the front, not the back. They are the
    # strongest openings available and appending them means the `[:count]` slice
    # drops exactly the ones that took real data to earn.
    evidenced: list[tuple[str, str, str]] = []
    if verified_order_count:
        evidenced.append(
            ("social_proof", f"{verified_order_count:,} of these went out last month.",
             "Only usable because the count is verified. Never estimate it."))
    if review_objection:
        evidenced.append(
            ("objection", f"I thought {review_objection} — here's what happened.",
             "Built from an objection real reviews actually raise."))
    if live_trend:
        evidenced.append(("trend", f"{live_trend} — but with {candidate.title}.",
                          "Trend supplied by a human who checked the app."))
    pool = evidenced + pool

    hooks = [{"angle": angle, "line": line, "why_it_works": why,
              "compliance": "; ".join(check_claims(line)) or "clear"}
             for angle, line, why in pool[:count]]

    if len(hooks) < count:
        hooks.append({
            "angle": "unfilled",
            "line": SLOT.format(
                f"{count - len(hooks)} more hooks need a real detail about this "
                "product — a spec, a review quote, or a verified number"),
            "why_it_works": "Not generated. See the module docstring on fabrication.",
            "compliance": "n/a",
        })
    return hooks


def build_captions(candidate: ProductCandidate, *, count: int = 10,
                   hook_line: str = "") -> list[dict[str, str]]:
    """Ten captions across shapes, half keyword-bearing and half conversational."""
    problem = _derive_problem(candidate)
    hashtags = build_hashtags(candidate, limit=5)
    captions: list[dict[str, str]] = []

    for shape, template, purpose in CAPTION_SHAPES[:count]:
        text = template.format(hook=hook_line or f"About {candidate.title}",
                               problem=problem, title=candidate.title)
        tail = " ".join(hashtags[:3]) if shape != "plain" else ""
        full = f"{text} {tail}".strip()
        captions.append({
            "shape": shape,
            "text": full,
            "purpose": purpose,
            "compliance": "; ".join(check_claims(full)) or "clear",
        })
    return captions


def build_cta_variations(*, count: int = 8, has_bundle: bool = False,
                         free_returns: bool = False,
                         restock_is_slow: bool = False) -> list[dict[str, str]]:
    """CTA variants, filtered to the ones this business can honour.

    A CTA that promises free returns when returns are not free is not a
    conversion tactic, it is a chargeback. So the variants that make a factual
    promise are only offered when the caller confirms the fact.
    """
    gate = {
        "bundle": has_bundle,
        "objection": free_returns,
        "scarcity_honest": restock_is_slow,
    }
    out: list[dict[str, str]] = []
    for key, line, note in CTA_VARIATIONS:
        if key in gate and not gate[key]:
            continue
        out.append({"variant": key, "line": line, "note": note})
    return out[:count]


def build_creative_bank(candidate: ProductCandidate, *,
                        idea_count: int = 10, hook_count: int = 10,
                        caption_count: int = 10,
                        review_objection: str | None = None,
                        verified_order_count: int | None = None,
                        true_story: str | None = None,
                        seasonal_window: str | None = None,
                        live_trend: str | None = None,
                        has_bundle: bool = False,
                        free_returns: bool = False,
                        restock_is_slow: bool = False) -> CreativeBank:
    """The full creative bank for one product."""
    ideas, blocked = build_video_ideas(
        candidate, count=idea_count, true_story=true_story,
        seasonal_window=seasonal_window, live_trend=live_trend)
    hooks = build_hook_bank(candidate, count=hook_count,
                            review_objection=review_objection,
                            verified_order_count=verified_order_count,
                            live_trend=live_trend)
    captions = build_captions(candidate, count=caption_count,
                              hook_line=hooks[0]["line"] if hooks else "")
    ctas = build_cta_variations(has_bundle=has_bundle, free_returns=free_returns,
                                restock_is_slow=restock_is_slow)

    warnings: list[str] = []

    # Screen the operator's own output before it reaches a shoot day.
    for idea in ideas:
        text = " ".join([idea.premise, idea.opening_shot, idea.payoff])
        for issue in check_originality(text):
            warnings.append(f"{idea.idea_id}: originality — {issue}")
        for issue in check_claims(text):
            warnings.append(f"{idea.idea_id}: claim — {issue}")

    needing_input = [i for i in ideas if not i.ready_to_shoot]
    if needing_input:
        warnings.append(
            f"{len(needing_input)} of {len(ideas)} ideas are not shootable as "
            "generated — they contain slots the operator refuses to fill with a "
            "guess. Fill them or shoot the rest; do not treat the headline count "
            "as the usable count.")
    if blocked:
        names = ", ".join(b["name"] for b in blocked)
        warnings.append(
            f"Angles unavailable for this product: {names}. Each is blocked by a "
            "missing fact, not by a limitation of the product — supplying the "
            "fact unblocks it.")

    return CreativeBank(
        sku=candidate.sku,
        title=candidate.title,
        ideas=ideas,
        hooks=hooks,
        captions=captions,
        ctas=ctas,
        hashtags=build_hashtags(candidate),
        blocked_angles=blocked,
        warnings=warnings,
    )
