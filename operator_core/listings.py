"""Listing generation: SEO title, bullets, description, backend keywords,
image briefs, comparison chart, FAQ, and an A+ content draft.

Deterministic and constraint-aware. Marketplace limits (title length, backend
keyword bytes) are enforced structurally rather than hoped for, because a
truncated title silently loses indexed keywords.

Output is a *draft for human review*. Publishing is an approval-gated action —
a live listing is a public commitment and a compliance surface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import ProductCandidate

# Marketplace title limits (characters).
TITLE_LIMITS = {
    "amazon": 200,
    "shopify": 70,      # SEO best practice, not a hard cap
    "walmart": 100,
    "ebay": 80,
    "tiktok": 255,
}
# Amazon indexes backend search terms by bytes, not characters.
BACKEND_KEYWORD_BYTE_LIMIT = 249

STOPWORDS = {
    "a", "an", "the", "and", "or", "for", "with", "of", "to", "in", "on", "at",
    "by", "from", "is", "it", "this", "that", "your", "you", "our", "we",
}


@dataclass
class ListingDraft:
    sku: str
    marketplace: str
    title: str
    bullets: list[str]
    description: str
    backend_keywords: str
    image_briefs: list[dict[str, str]]
    comparison_chart: dict[str, list[str]]
    faq: list[dict[str, str]]
    aplus_modules: list[dict[str, str]]
    warnings: list[str] = field(default_factory=list)
    keyword_coverage: dict[str, bool] = field(default_factory=dict)


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9+]+", text.lower()) if t and t not in STOPWORDS]


def _dedupe_preserve(items: list[str]) -> list[str]:
    seen, out = set(), []
    for i in items:
        k = i.lower()
        if k not in seen:
            seen.add(k)
            out.append(i)
    return out


def _titlecase(s: str) -> str:
    small = {"and", "or", "for", "with", "of", "the", "a", "an", "in", "on", "to"}
    words = s.split()
    return " ".join(
        w.capitalize() if (i == 0 or w.lower() not in small) else w.lower()
        for i, w in enumerate(words)
    )


def build_seo_title(candidate: ProductCandidate, marketplace: str,
                    features: list[str]) -> tuple[str, list[str]]:
    """Front-load the highest-volume keywords; fill remaining space by priority.

    Marketplace relevance weighting decays left-to-right, so the primary keyword
    goes first and brand goes last unless the brand is itself the search term.
    """
    limit = TITLE_LIMITS.get(marketplace, 150)
    warnings: list[str] = []

    primary = candidate.keywords[0] if candidate.keywords else candidate.title
    secondary = candidate.keywords[1:4]

    segments: list[str] = [_titlecase(primary)]
    if candidate.brand:
        segments.insert(0, candidate.brand)
    for kw in secondary:
        segments.append(_titlecase(kw))
    for f in features[:3]:
        segments.append(_titlecase(f))

    title = ""
    for seg in _dedupe_preserve(segments):
        candidate_title = f"{title} - {seg}" if title else seg
        if len(candidate_title) <= limit:
            title = candidate_title
        else:
            break

    if not title:
        title = candidate.title[:limit]
    if len(title) < limit * 0.5:
        warnings.append(
            f"Title uses only {len(title)}/{limit} characters. Unused title space is "
            "unindexed keyword real estate — add qualifiers (size, material, use case)."
        )
    return title, warnings


def build_bullets(candidate: ProductCandidate, features: list[str],
                  benefits: list[str]) -> list[str]:
    """Benefit-led bullets. Each opens with a capitalised hook, then the proof.

    Buyers scan hooks; the detail after the dash is for the ones already sold.
    """
    bullets: list[str] = []
    pairs = list(zip(features, benefits))
    for feature, benefit in pairs[:5]:
        hook = feature.upper()[:48]
        bullets.append(f"{hook} - {benefit}")

    while len(bullets) < 5 and candidate.keywords:
        kw = candidate.keywords[len(bullets) % len(candidate.keywords)]
        bullets.append(
            f"DESIGNED FOR {kw.upper()[:40]} - Built to hold up to daily use, so it keeps "
            "doing the job long after the cheap alternative has been replaced."
        )
    return bullets[:5]


def build_description(candidate: ProductCandidate, features: list[str],
                      benefits: list[str]) -> str:
    kw = candidate.keywords[:5]
    feature_lines = "\n".join(f"• {f} — {b}" for f, b in zip(features, benefits))
    return (
        f"{_titlecase(candidate.title)}\n\n"
        f"{candidate.description.strip() or 'Built for people who want this done properly the first time.'}\n\n"
        "WHAT YOU GET\n"
        f"{feature_lines}\n\n"
        "WHY IT WORKS\n"
        f"Most {kw[0] if kw else 'products in this category'} cut corners where it matters. "
        "This one is specified around the failure points customers actually complain "
        "about — the parts that wear first, the sizing that never quite fits, and the "
        "finish that stops looking new after a month.\n\n"
        "WHAT'S INCLUDED\n"
        f"• 1x {_titlecase(candidate.title)}\n"
        "• Quick-start guide\n"
        "• Responsive support if anything is not right\n\n"
        "Order today and see the difference in person."
    )


def build_backend_keywords(candidate: ProductCandidate, title: str,
                           bullets: list[str]) -> tuple[str, list[str]]:
    """Backend terms must not repeat the title — duplicates waste indexed bytes.

    Amazon indexes the union of visible and backend text, so a repeated word
    buys nothing while consuming part of a hard byte budget.
    """
    warnings: list[str] = []
    used = set(_tokens(title)) | set(_tokens(" ".join(bullets)))

    pool: list[str] = []
    for kw in candidate.keywords:
        pool.extend(_tokens(kw))
    pool.extend(_tokens(candidate.category))
    pool.extend(_tokens(candidate.description))

    fresh = _dedupe_preserve([t for t in pool if t not in used and len(t) > 2])

    out, size = [], 0
    for term in fresh:
        add = len(term.encode("utf-8")) + (1 if out else 0)
        if size + add > BACKEND_KEYWORD_BYTE_LIMIT:
            break
        out.append(term)
        size += add

    if size < BACKEND_KEYWORD_BYTE_LIMIT * 0.6:
        warnings.append(
            f"Backend keywords use {size}/{BACKEND_KEYWORD_BYTE_LIMIT} bytes. "
            "Add synonyms, common misspellings, and Spanish-language terms."
        )
    return " ".join(out), warnings


def build_image_briefs(candidate: ProductCandidate, features: list[str]) -> list[dict[str, str]]:
    """Image concepts, in the order that actually drives conversion.

    These are production briefs, not generated pixels — see README on why the
    operator does not fabricate product photography.
    """
    name = _titlecase(candidate.title)
    briefs = [
        {
            "slot": "1 - MAIN",
            "concept": f"{name} straight-on, pure white (RGB 255,255,255) background",
            "spec": "2000x2000px min, product fills 85% of frame, no props, no text, no logos",
            "why": "Marketplace TOS requires a clean main image; it also sets CTR in search.",
        },
        {
            "slot": "2 - SCALE",
            "concept": f"{name} held in hand or beside a common object",
            "spec": "Lifestyle lighting, neutral background, dimensions annotated subtly",
            "why": "Wrong-size expectations are a top driver of returns. Kill it here.",
        },
        {
            "slot": "3 - FEATURE CALLOUT",
            "concept": f"Close-up macro of the {features[0].lower() if features else 'key feature'}",
            "spec": "Short callout text, max 6 words, high contrast, readable at thumbnail size",
            "why": "Answers the single most common pre-purchase objection.",
        },
        {
            "slot": "4 - IN USE",
            "concept": f"Target customer using {name} in its natural setting",
            "spec": "Real environment, natural light, model hands visible for relatability",
            "why": "Buyers need to picture themselves owning it before they add to cart.",
        },
        {
            "slot": "5 - COMPARISON",
            "concept": "Side-by-side vs the generic alternative",
            "spec": "Two-column layout, checkmarks vs crosses, honest and verifiable claims",
            "why": "Frames the purchase against rivals without naming a competitor brand.",
        },
        {
            "slot": "6 - SPECS",
            "concept": "Dimensioned technical drawing with materials list",
            "spec": "Clean line art, metric and imperial units",
            "why": "Pre-empts the sizing and material questions that create support load.",
        },
        {
            "slot": "7 - TRUST",
            "concept": "Packaging, warranty card, and what's-in-the-box flat lay",
            "spec": "Overhead shot, all included items labelled",
            "why": "Reduces 'is this the full kit?' hesitation at checkout.",
        },
    ]
    return briefs


def build_comparison_chart(candidate: ProductCandidate,
                           features: list[str]) -> dict[str, list[str]]:
    rows = features[:5] or ["Build quality", "Warranty", "Materials"]
    return {
        "columns": ["Feature", "This Product", "Typical Alternative"],
        "rows": [f"{r} | Yes | Varies" for r in rows],
        "note": (
            "Every claim in this chart must be verifiable against the actual product "
            "spec sheet before publishing. Unverifiable comparison claims are a "
            "marketplace policy violation and an FTC exposure."
        ),
    }


def build_faq(candidate: ProductCandidate, features: list[str]) -> list[dict[str, str]]:
    kw = candidate.keywords[0] if candidate.keywords else candidate.title
    return [
        {"q": f"What size is the {_titlecase(candidate.title)}?",
         "a": "Full dimensions are in image 6. Measure your space before ordering — "
              "sizing is the number one reason products in this category get returned."},
        {"q": "What is it made from?",
         "a": f"See the materials list in the specification image. {features[0] if features else 'Built for durability'}."},
        {"q": "How long does shipping take?",
         "a": "Standard delivery is shown at checkout for your address. Expedited "
              "options are available on most orders."},
        {"q": "What if it arrives damaged or is not right?",
         "a": "Contact us through the marketplace message centre and we will replace "
              "or refund it. We would rather fix it than argue about it."},
        {"q": "Is there a warranty?",
         "a": "Yes — details are on the warranty card included in the box."},
    ]


def build_aplus_modules(candidate: ProductCandidate, features: list[str],
                        benefits: list[str]) -> list[dict[str, str]]:
    name = _titlecase(candidate.title)
    return [
        {"module": "Standard Company Logo",
         "content": f"{candidate.brand or 'Brand'} logo, 600x180px transparent PNG"},
        {"module": "Standard Four Image & Text",
         "content": " | ".join(f"{f}: {b}" for f, b in list(zip(features, benefits))[:4])},
        {"module": "Standard Comparison Chart",
         "content": f"{name} vs 2 alternates across {min(len(features), 5)} attributes"},
        {"module": "Standard Image Header With Text",
         "content": f"Hero banner 1464x600px. Headline: 'Made for people who are tired of "
                    f"replacing their {candidate.keywords[0] if candidate.keywords else 'gear'}.'"},
        {"module": "Standard Three Image & Text",
         "content": "Use cases: everyday use, gifting, travel"},
        {"module": "Standard Single Image & Specs",
         "content": "Full technical specification table alongside the product hero shot"},
    ]


def generate_listing(
    candidate: ProductCandidate,
    *,
    features: list[str] | None = None,
    benefits: list[str] | None = None,
    marketplace: str | None = None,
) -> ListingDraft:
    mp = (marketplace or candidate.marketplace).lower()
    features = features or ["Durable construction", "Easy to use", "Fits most setups",
                            "Simple to clean", "Backed by support"]
    benefits = benefits or [
        "Holds up to daily use instead of failing in a few months.",
        "Works out of the box with no fiddly setup or extra parts to buy.",
        "Standard sizing means it fits what you already own.",
        "Wipes clean in seconds, so maintenance never becomes a chore.",
        "Real support from a seller who answers messages.",
    ]

    warnings: list[str] = []
    title, w1 = build_seo_title(candidate, mp, features)
    warnings += w1

    bullets = build_bullets(candidate, features, benefits)
    description = build_description(candidate, features, benefits)
    backend, w2 = build_backend_keywords(candidate, title, bullets)
    warnings += w2

    # Coverage check: every target keyword should be indexed somewhere.
    haystack = " ".join([title, *bullets, description, backend]).lower()
    coverage = {kw: kw.lower() in haystack for kw in candidate.keywords}
    missing = [k for k, v in coverage.items() if not v]
    if missing:
        warnings.append(
            f"Target keywords absent from all indexed fields: {', '.join(missing)}. "
            "These will not rank."
        )

    if len(title) > TITLE_LIMITS.get(mp, 200):
        warnings.append(f"Title exceeds the {mp} limit and will be truncated.")

    return ListingDraft(
        sku=candidate.sku,
        marketplace=mp,
        title=title,
        bullets=bullets,
        description=description,
        backend_keywords=backend,
        image_briefs=build_image_briefs(candidate, features),
        comparison_chart=build_comparison_chart(candidate, features),
        faq=build_faq(candidate, features),
        aplus_modules=build_aplus_modules(candidate, features, benefits),
        warnings=warnings,
        keyword_coverage=coverage,
    )
