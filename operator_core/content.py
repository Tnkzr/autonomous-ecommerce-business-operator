"""Video and content strategy for TikTok Shop.

On TikTok the video *is* the storefront. A product with a mediocre listing and
a good demo outsells the reverse, so content is not marketing support here — it
is the primary conversion surface, and it belongs in the operating system rather
than in someone's head.

What this module does and does not do:

**Does**: generate concepts, hooks, scripts, shot lists, captions, and a
publishing calendar from the product's actual attributes, and structure creator
outreach.

**Does not**: invent product specifications, claim results the product has not
demonstrated, or supply "trending" hashtags and sounds. Trend data needs a feed
that does not exist here, and a fabricated trending sound is worse than none —
it sends real production budget at a guess. Hashtags are generated from the
product and category, and clearly labelled as evergreen rather than trending.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .config import Policy
from .models import ProductCandidate

# Claim language that must never appear in generated copy. TikTok enforces
# these harder than other platforms and the penalty lands on the shop.
BANNED_CLAIM_PATTERNS = (
    r"\bcure[sd]?\b", r"\btreats?\b", r"\bheals?\b", r"\bfda[- ]approved\b",
    r"\bclinically proven\b", r"\bdoctor[- ]recommended\b",
    r"\bguaranteed\b", r"\b100% effective\b", r"\bmiracle\b",
    r"\bbest in the world\b", r"\bnumber one\b", r"\b#1\b",
    r"\blose \d+ (?:pounds|lbs|kg)\b", r"\bdetox\b", r"\banti[- ]aging\b",
)

# Hook archetypes that work on short-form commerce video. Each is a *shape*,
# filled from real product attributes — not a script to be used verbatim.
HOOK_ARCHETYPES = [
    ("problem_callout", "I can't believe I put up with {problem} for so long.",
     "Opens on the pain, not the product. Highest completion rate for utility "
     "items because the viewer recognises themselves in the first second."),
    ("visual_shock", "Wait for it… {transformation}",
     "Only use when there is a genuine visual change. Promising a payoff the "
     "product cannot deliver reads as bait and tanks retention on the next post."),
    ("objection_first", "I thought {objection} — here's what actually happened.",
     "Pre-empts the top comment. Works when reviews show one repeated doubt."),
    ("comparison", "{alternative} vs this. Same job, {difference}.",
     "Do not name a competitor brand. Compare against the generic category."),
    ("use_case_reveal", "Three ways to use this you probably didn't think of.",
     "Extends perceived value; strong for products with a single obvious use."),
    ("social_proof", "{count} people bought this last month. Here's why.",
     "Only usable once the number is real. Never fabricate a count."),
]

# Evergreen hashtag stems, generated from product and category. NOT trending
# data — see the module docstring.
EVERGREEN_HASHTAG_STEMS = (
    "tiktokmademebuyit", "amazonfinds", "homefinds", "musthaves",
    "organization", "cleantok", "homehacks", "giftideas", "smallbusiness",
)


@dataclass
class Hook:
    archetype: str
    line: str
    why_it_works: str
    seconds: float = 2.0


@dataclass
class ShotListItem:
    order: int
    duration_seconds: float
    shot: str
    audio: str
    on_screen_text: str
    purpose: str


@dataclass
class VideoConcept:
    concept_id: str
    format: str                  # UGC | DEMO | UNBOXING | COMPARISON | TUTORIAL
    hook: Hook
    premise: str
    shot_list: list[ShotListItem]
    voiceover: str
    caption: str
    hashtags: list[str]
    call_to_action: str
    estimated_seconds: float
    production_notes: list[str] = field(default_factory=list)
    compliance_warnings: list[str] = field(default_factory=list)


@dataclass
class CreatorBrief:
    tier: str                    # NANO | MICRO | MID
    follower_range: str
    commission_pct: float
    sample_policy: str
    outreach_message: str
    deliverables: list[str]
    rationale: str


@dataclass
class ContentCalendarEntry:
    day: str
    slot: str
    concept_id: str
    format: str
    objective: str


@dataclass
class ContentPlan:
    sku: str
    title: str
    concepts: list[VideoConcept]
    creator_briefs: list[CreatorBrief]
    calendar: list[ContentCalendarEntry]
    hashtag_strategy: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Compliance
# ---------------------------------------------------------------------------
def check_claims(text: str) -> list[str]:
    """Flag claim language that violates TikTok's content policy.

    Runs over every generated string. Generating copy that gets the shop
    penalised is worse than generating none, and the operator is the one
    producing this text, so it screens its own output.
    """
    found: list[str] = []
    lowered = text.lower()
    for pattern in BANNED_CLAIM_PATTERNS:
        match = re.search(pattern, lowered)
        if match:
            found.append(
                f"'{match.group(0)}' — efficacy/superlative claim; TikTok "
                "enforces these against the shop, not just the video."
            )
    return found


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------
def _derive_problem(candidate: ProductCandidate) -> str:
    """Infer the problem from the product's own words, never invented."""
    text = candidate.searchable_text()
    known = {
        "organiz": "a drawer I couldn't find anything in",
        "organis": "a drawer I couldn't find anything in",
        "storage": "stuff with nowhere to live",
        "tangle": "cables in a knot",
        "cable": "cables everywhere",
        "brush": "hair on everything I own",
        "clean": "a job that took twice as long as it should",
        "holder": "putting it down and losing it",
        "protect": "replacing the same thing again",
    }
    for key, problem in known.items():
        if key in text:
            return problem
    return "the thing this product fixes"


def _derive_transformation(candidate: ProductCandidate) -> str | None:
    """Only returns something when the product genuinely transforms visually."""
    text = candidate.searchable_text()
    from .scoring import VISUAL_TRANSFORMATION_TERMS

    hits = [t for t in VISUAL_TRANSFORMATION_TERMS if t in text]
    if not hits:
        return None
    if "expand" in text or "collapsible" in text or "foldable" in text:
        return "it expands to fit the whole drawer"
    if "self-cleaning" in text or "self cleaning" in text:
        return "one press and all the hair comes off"
    if "magnetic" in text:
        return "it just snaps into place"
    return f"the {hits[0]} moment"


def build_hooks(candidate: ProductCandidate, *, review_objection: str | None = None,
                verified_order_count: int | None = None) -> list[Hook]:
    """Fill hook archetypes from real product attributes.

    Archetypes needing data we do not have are skipped rather than filled with
    a plausible invention — an unfilled hook costs nothing, a fabricated
    social-proof number is a false advertising claim.
    """
    problem = _derive_problem(candidate)
    transformation = _derive_transformation(candidate)
    hooks: list[Hook] = []

    for archetype, template, why in HOOK_ARCHETYPES:
        if archetype == "visual_shock":
            if not transformation:
                continue
            line = template.format(transformation=transformation)
        elif archetype == "problem_callout":
            line = template.format(problem=problem)
        elif archetype == "objection_first":
            if not review_objection:
                continue
            line = template.format(objection=review_objection)
        elif archetype == "comparison":
            line = template.format(
                alternative="The cheap version",
                difference="one lasts and one doesn't",
            )
        elif archetype == "social_proof":
            if not verified_order_count:
                continue
            line = template.format(count=f"{verified_order_count:,}")
        else:
            line = template

        hooks.append(Hook(archetype=archetype, line=line, why_it_works=why))
    return hooks


def build_shot_list(candidate: ProductCandidate, hook: Hook,
                    fmt: str) -> list[ShotListItem]:
    """A second-by-second shot list. Retention is won in the first three."""
    name = candidate.title
    transformation = _derive_transformation(candidate)

    shots = [
        ShotListItem(
            order=1, duration_seconds=2.0,
            shot="Handheld, eye level, subject already mid-action. No logo, no intro.",
            audio=f'VO: "{hook.line}"',
            on_screen_text=hook.line[:40],
            purpose="Stop the scroll. A branded intro card loses half the audience.",
        ),
        ShotListItem(
            order=2, duration_seconds=3.0,
            shot="Close-up of the problem in the viewer's own environment.",
            audio="VO continues, natural room tone underneath.",
            on_screen_text="",
            purpose="Make the pain concrete before the product appears.",
        ),
    ]

    if fmt == "DEMO" and transformation:
        shots.append(ShotListItem(
            order=3, duration_seconds=5.0,
            shot=f"Unbroken single take of the key action — {transformation}. No cut.",
            audio="Diegetic sound only. Let the mechanism be audible.",
            on_screen_text="",
            purpose=("A cut here reads as a hidden edit and kills trust. The "
                     "single take is the whole reason this format works."),
        ))
    else:
        shots.append(ShotListItem(
            order=3, duration_seconds=5.0,
            shot=f"{name} in use, hands visible, real setting.",
            audio="VO: the specific thing it does differently.",
            on_screen_text="",
            purpose="Show, do not describe.",
        ))

    shots.extend([
        ShotListItem(
            order=4, duration_seconds=3.0,
            shot="Result held next to the 'before' state, same framing.",
            audio="VO: one concrete detail, no superlatives.",
            on_screen_text="",
            purpose="Same framing makes the difference legible in a glance.",
        ),
        ShotListItem(
            order=5, duration_seconds=2.0,
            shot="Product resting in the finished scene. Slow push in.",
            audio='VO: "It\'s in my bio if you want one."',
            on_screen_text="Tap the yellow basket",
            purpose=("Soft CTA. A hard sell at the end suppresses reach on "
                     "organic posts."),
        ),
    ])
    return shots


def build_voiceover(candidate: ProductCandidate, hook: Hook) -> str:
    """A spoken script — sentences, not bullet points.

    Deliberately free of specification claims: the operator does not know the
    material, dimensions, or durability of a product it has not received, and
    a script that invents them becomes a false advertising claim the moment it
    is filmed.
    """
    problem = _derive_problem(candidate)
    transformation = _derive_transformation(candidate)

    lines = [
        hook.line,
        f"I had {problem}, and I'd basically stopped noticing it.",
    ]
    if transformation:
        lines.append(f"Then I tried this — {transformation}.")
    else:
        lines.append("Then I tried this.")
    lines.extend([
        "[FILL FROM PRODUCT SPEC: one concrete, verifiable thing it does. "
        "Do not write this line until you have the product in hand.]",
        "That's it. No setup, no extra parts.",
        "It's in my bio if you want one.",
    ])
    return "\n".join(lines)


def build_hashtags(candidate: ProductCandidate, *, limit: int = 8) -> list[str]:
    """Evergreen, product-derived hashtags.

    Explicitly NOT trending tags: trend data needs a feed this system does not
    have, and a fabricated trending tag sends real production budget at a guess.
    """
    tags: list[str] = []
    for kw in candidate.keywords[:3]:
        slug = re.sub(r"[^a-z0-9]", "", kw.lower())
        if slug and len(slug) > 3:
            tags.append(f"#{slug}")
    category_slug = re.sub(r"[^a-z0-9]", "", candidate.category.lower())
    if category_slug:
        tags.append(f"#{category_slug}")
    for stem in EVERGREEN_HASHTAG_STEMS:
        if len(tags) >= limit:
            break
        tags.append(f"#{stem}")
    seen, out = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:limit]


def build_creator_briefs(policy: Policy, candidate: ProductCandidate,
                         unit_margin: float) -> list[CreatorBrief]:
    """Creator tiers with commission that the product's margin can survive.

    The affiliate commission comes out of the same margin as everything else,
    so the ceiling is set by unit economics rather than by what creators ask
    for. Offering a rate the product cannot fund buys volume at a loss.
    """
    price = candidate.target_price
    margin_pct = (unit_margin / price * 100) if price else 0.0
    # Leave at least a third of the margin after commission, or scaling makes
    # the product less profitable the more it sells.
    max_commission = max(0.0, min(margin_pct * 0.6, 30.0))

    tiers = [
        ("NANO", "1k–10k followers", 0.5,
         "Send a free sample, no fee.",
         "Cheapest volume and the most authentic footage. Expect a low hit rate "
         "and treat it as a portfolio: twenty creators, two work."),
        ("MICRO", "10k–100k followers", 0.75,
         "Free sample plus commission; small flat fee only after one performing video.",
         "The efficient tier for TikTok Shop. Enough reach to matter, still "
         "priced on performance rather than on audience size."),
        ("MID", "100k–500k followers", 1.0,
         "Sample, commission, and a negotiated flat fee against exclusivity.",
         "Only once the product has proven conversion with smaller creators. "
         "Paying for reach before the video converts is buying an audience for "
         "a video that does not sell."),
    ]

    briefs: list[CreatorBrief] = []
    for tier, followers, share, sample_policy, rationale in tiers:
        commission = round(max_commission * share, 1)
        message = (
            f"Hi — we make {candidate.title}. "
            f"We think it would suit your audience because it solves "
            f"{_derive_problem(candidate)} in one shot, which films well.\n\n"
            f"We'd send one free, no obligation to post. If you do post and it "
            f"performs, our affiliate rate is {commission:.0f}%.\n\n"
            "No script and no approval process — your framing will outperform "
            "anything we write. The only thing we ask is that you don't claim "
            "results the product hasn't given you.\n\n"
            "Interested?"
        )
        briefs.append(CreatorBrief(
            tier=tier, follower_range=followers, commission_pct=commission,
            sample_policy=sample_policy, outreach_message=message,
            deliverables=["1 organic video", "Spark Ads usage rights, 30 days",
                          "Raw footage if the video performs"],
            rationale=rationale,
        ))
    return briefs


def build_calendar(concepts: list[VideoConcept], *, days: int = 14,
                   posts_per_day: int = 2,
                   start: date | None = None) -> list[ContentCalendarEntry]:
    """A publishing schedule that rotates formats.

    Rotation is the point: posting the same format repeatedly trains the
    algorithm on one audience and caps reach. Testing formats against each
    other is how you find which one converts before spending on ads.
    """
    if not concepts:
        return []
    start = start or date.today()
    slots = ["11:00", "19:00", "15:00"][:max(posts_per_day, 1)]
    objectives = ["Test hook retention", "Test format conversion",
                  "Re-cut of the best performer", "Creator-sourced UGC"]

    calendar: list[ContentCalendarEntry] = []
    i = 0
    for day_offset in range(days):
        day = (start + timedelta(days=day_offset)).isoformat()
        for slot in slots:
            concept = concepts[i % len(concepts)]
            calendar.append(ContentCalendarEntry(
                day=day, slot=slot, concept_id=concept.concept_id,
                format=concept.format,
                objective=objectives[i % len(objectives)],
            ))
            i += 1
    return calendar


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def build_content_plan(
    policy: Policy,
    candidate: ProductCandidate,
    *,
    unit_margin: float,
    review_objection: str | None = None,
    verified_order_count: int | None = None,
    calendar_days: int = 14,
) -> ContentPlan:
    """Produce the full content plan for one product."""
    hooks = build_hooks(candidate, review_objection=review_objection,
                        verified_order_count=verified_order_count)
    hashtags = build_hashtags(candidate)
    transformation = _derive_transformation(candidate)

    formats = ["UGC", "DEMO", "COMPARISON", "TUTORIAL", "UNBOXING"]
    concepts: list[VideoConcept] = []
    warnings: list[str] = []

    for i, hook in enumerate(hooks):
        fmt = "DEMO" if (hook.archetype == "visual_shock" and transformation) \
            else formats[i % len(formats)]
        shots = build_shot_list(candidate, hook, fmt)
        voiceover = build_voiceover(candidate, hook)
        caption = (
            f"{hook.line} {' '.join(hashtags[:4])}"
        )
        concept = VideoConcept(
            concept_id=f"{candidate.sku}-C{i + 1}",
            format=fmt,
            hook=hook,
            premise=hook.why_it_works,
            shot_list=shots,
            voiceover=voiceover,
            caption=caption,
            hashtags=hashtags,
            call_to_action="Soft — product link in bio, no hard sell on organic posts.",
            estimated_seconds=round(sum(s.duration_seconds for s in shots), 1),
            production_notes=[
                "Vertical 9:16, 1080x1920 minimum.",
                "Shoot in a real room, not a studio. Studio lighting reads as an "
                "ad and suppresses organic reach.",
                "Captions burned in — most viewing is muted.",
                "No licensed music in the export; add sound in-app so the "
                "commercial library applies.",
            ],
        )

        # Screen the operator's own output before anyone films it.
        for field_name, text in (("hook", hook.line), ("voiceover", voiceover),
                                 ("caption", caption)):
            for issue in check_claims(text):
                concept.compliance_warnings.append(f"{field_name}: {issue}")
        concepts.append(concept)

    if not concepts:
        warnings.append(
            "No hook archetype could be filled from this product's own "
            "attributes. Rather than inventing one, add real detail to the "
            "product record — keywords, description, a genuine transformation."
        )

    if not transformation:
        warnings.append(
            "No visual transformation detected. This product will be harder to "
            "sell on short-form video regardless of its margins — plan for paid "
            "traffic rather than organic reach, and price that in."
        )

    if verified_order_count is None:
        warnings.append(
            "Social-proof hooks are omitted because no verified order count is "
            "available. Do not fill in a number — an invented one is a false "
            "advertising claim, and TikTok penalises the shop for it."
        )

    calendar = build_calendar(concepts, days=calendar_days)
    briefs = build_creator_briefs(policy, candidate, unit_margin)

    return ContentPlan(
        sku=candidate.sku,
        title=candidate.title,
        concepts=concepts,
        creator_briefs=briefs,
        calendar=calendar,
        hashtag_strategy={
            "tags": hashtags,
            "kind": "evergreen",
            "note": (
                "These are evergreen and product-derived, NOT trending. Trending "
                "hashtags and sounds need a data feed this system does not have. "
                "Check the app before posting and swap two or three for whatever "
                "is genuinely live in the category — a fabricated trending tag "
                "sends real production budget at a guess."
            ),
        },
        warnings=warnings,
    )
