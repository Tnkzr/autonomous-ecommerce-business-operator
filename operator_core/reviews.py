"""Customer review monitoring and defect clustering.

The value of review data is not the star average — it is the *recurring
complaint*. One person calling a product flimsy is noise; eleven people using
the word "flimsy" is a supplier conversation and a listing change.

Also watches for review-velocity collapse and account-safety signals (safety
complaints, counterfeit accusations) that need a human immediately.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .models import Review, Severity

# Complaint themes -> the words customers actually use for them.
DEFECT_THEMES = {
    "durability": ["broke", "broken", "snapped", "cracked", "flimsy", "fell apart",
                   "stopped working", "died", "tore", "ripped", "bent"],
    "sizing": ["too small", "too big", "runs small", "runs large", "wrong size",
               "doesn't fit", "does not fit", "tight", "loose"],
    "quality": ["cheap", "cheaply made", "poor quality", "shoddy", "thin", "flimsy",
                "not as described", "looks fake"],
    "shipping": ["late", "damaged in transit", "arrived damaged", "crushed box",
                 "never arrived", "slow shipping", "delayed"],
    "missing_parts": ["missing", "didn't include", "did not include", "no instructions",
                      "incomplete", "parts missing"],
    "functionality": ["doesn't work", "does not work", "stopped charging", "leaks",
                      "won't turn on", "defective", "malfunction"],
    "expectation_gap": ["not as pictured", "different color", "misleading", "smaller than expected",
                        "looks nothing like"],
}

# Any hit here is an escalation, regardless of star rating.
ACCOUNT_RISK_TERMS = [
    "counterfeit", "fake", "knockoff", "replica", "not authentic",
    "caught fire", "burned", "smoke", "shock", "electrocuted", "injury",
    "injured", "hurt my", "rash", "allergic reaction", "sick", "hospital",
    "child swallowed", "choking hazard", "lawsuit", "attorney", "reported to",
]


@dataclass
class ReviewInsight:
    sku: str
    marketplace: str
    total_reviews: int
    average_rating: float
    negative_count: int
    negative_pct: float
    theme_counts: dict[str, int]
    top_complaint: str | None
    account_risk_hits: list[dict[str, str]] = field(default_factory=list)
    recommended_actions: list[str] = field(default_factory=list)
    severity: Severity = Severity.INFO


def _find_terms(text: str, terms: list[str]) -> list[str]:
    lowered = text.lower()
    return [t for t in terms if re.search(re.escape(t), lowered)]


def analyse_reviews(sku: str, marketplace: str, reviews: list[Review]) -> ReviewInsight:
    if not reviews:
        return ReviewInsight(
            sku=sku, marketplace=marketplace, total_reviews=0, average_rating=0.0,
            negative_count=0, negative_pct=0.0, theme_counts={}, top_complaint=None,
            recommended_actions=["No reviews yet. Prioritise getting the first 15 — "
                                 "conversion rate climbs steeply over that range."],
        )

    avg = round(sum(r.rating for r in reviews) / len(reviews), 2)
    negatives = [r for r in reviews if r.rating <= 3]

    themes: Counter[str] = Counter()
    for r in negatives:
        blob = f"{r.title} {r.body}"
        for theme, terms in DEFECT_THEMES.items():
            if _find_terms(blob, terms):
                themes[theme] += 1

    risk_hits: list[dict[str, str]] = []
    for r in reviews:
        hits = _find_terms(f"{r.title} {r.body}", ACCOUNT_RISK_TERMS)
        if hits:
            risk_hits.append({
                "review_id": r.review_id,
                "rating": str(r.rating),
                "terms": ", ".join(hits),
                "excerpt": r.body[:180],
            })

    neg_pct = round(len(negatives) / len(reviews) * 100, 1)
    top = themes.most_common(1)[0][0] if themes else None

    actions: list[str] = []
    severity = Severity.INFO

    if risk_hits:
        severity = Severity.CRITICAL
        actions.append(
            f"ESCALATE NOW: {len(risk_hits)} review(s) contain safety or authenticity "
            "language. These precede listing suspension and, in the safety case, "
            "liability. A human must read these today and decide on a recall or "
            "delisting — the operator will not resolve this autonomously."
        )

    if avg < 3.5 and len(reviews) >= 5:
        severity = Severity.CRITICAL if severity != Severity.CRITICAL else severity
        actions.append(
            f"Rating {avg} is below the 3.5 line where conversion falls off a cliff and "
            "Amazon begins suppressing. Fix the root cause before spending another "
            "dollar on ads for this SKU."
        )
    elif avg < 4.2 and len(reviews) >= 10:
        severity = Severity.WARN if severity == Severity.INFO else severity
        actions.append(f"Rating {avg} is soft. Address the top complaint before scaling.")

    for theme, count in themes.most_common(3):
        share = round(count / max(len(negatives), 1) * 100)
        if theme == "sizing":
            actions.append(
                f"'{theme}' appears in {count} negative reviews ({share}% of them). "
                "This is fixable without touching the product: add a dimensioned "
                "sizing image and put measurements in bullet 1. Expect the return "
                "rate to drop within a cycle."
            )
        elif theme in ("durability", "quality", "functionality"):
            actions.append(
                f"'{theme}' appears in {count} negative reviews ({share}%). This is a "
                "supplier quality issue, not a listing issue. Raise it with the "
                "supplier with review excerpts attached, request a corrective action "
                "plan, and hold the next PO until they respond."
            )
        elif theme == "shipping":
            actions.append(
                f"'{theme}' appears in {count} negative reviews ({share}%). Review "
                "packaging spec and carrier performance — this is usually cheaper to "
                "fix than the reviews cost you."
            )
        elif theme == "expectation_gap":
            actions.append(
                f"'{theme}' appears in {count} negative reviews ({share}%). The listing "
                "images are overselling relative to the physical product. Correct the "
                "images — this is also a policy risk, not just a rating problem."
            )
        elif theme == "missing_parts":
            actions.append(
                f"'{theme}' appears in {count} negative reviews ({share}%). Audit the "
                "supplier's packing checklist; this is a fulfilment discipline problem."
            )

    if not actions:
        actions.append("No systemic complaint pattern detected. Continue monitoring.")

    return ReviewInsight(
        sku=sku,
        marketplace=marketplace,
        total_reviews=len(reviews),
        average_rating=avg,
        negative_count=len(negatives),
        negative_pct=neg_pct,
        theme_counts=dict(themes),
        top_complaint=top,
        account_risk_hits=risk_hits,
        recommended_actions=actions,
        severity=severity,
    )
