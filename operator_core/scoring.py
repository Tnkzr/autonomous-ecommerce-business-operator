"""The product scorecard: twelve dimensions, one verdict.

Screening answers "may we sell this?" — a set of hard gates with no scoring.
This module answers "how good is it, and where is it weak?", which is a
different question and needs a different shape.

Two design rules that matter:

**A dimension with no data scores `None`, not zero.** Zero means "measured and
bad"; `None` means "not measured". Collapsing them makes an unresearched
product look identical to a researched terrible one, and the overall score
reports its own coverage so a high number built on three dimensions cannot
masquerade as a high number built on twelve.

**Risk dimensions cap only when they are genuinely bad.** Policy risk and
return risk are scored so that high is safe, and they participate in the
weighted average like everything else. But below `RISK_CAP_THRESHOLD` they stop
averaging and start capping: a product can be outstanding on eleven dimensions
and still be uninvestable because of one, and averaging is exactly how a
trademark landmine with great margins gets funded.

The threshold matters. Capping whenever a risk score merely sits below the
average would cap almost every product and turn a meaningful warning into
noise, so the cap fires only for risk scores that are actually weak.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from .config import Policy
from .models import ProductCandidate, UnitEconomics

# Products that demonstrate well on video sell on TikTok; products that need
# explaining do not. These are structural predictors of demo-ability, not
# observed virality — see VIRALITY_NOTE.
# Both hyphenated and spaced variants: product titles are written by suppliers
# and use whichever they feel like, so matching only one form silently scores a
# classic demo product as having no visual hook at all.
VISUAL_TRANSFORMATION_TERMS = (
    "before and after", "transform", "instantly", "watch", "reveal",
    "satisfying", "peel", "pop", "fold", "expand", "collapsible", "foldable",
    "self-cleaning", "self cleaning", "selfcleaning",
    "colour change", "color change", "glow", "light up",
    "magnetic", "hidden", "compartment", "stackable", "portable",
    "retractable", "one press", "one click", "no mess", "detachable",
)
PROBLEM_SOLUTION_TERMS = (
    "no more", "stop", "prevent", "fix", "solve", "tangle", "mess", "clutter",
    "spill", "leak", "scratch", "slip", "organiz", "organis", "storage",
    "saver", "protector", "holder", "hack",
)
# Categories where a demo lands and repeat purchase is plausible.
HIGH_DEMO_CATEGORIES = (
    "home", "kitchen", "storage", "organiz", "organis", "pet", "beauty tool",
    "cleaning", "car accessor", "phone accessor", "craft", "toy", "gadget",
)

# Below this, a risk dimension stops averaging and starts capping the overall.
# 60 is the boundary between "GOOD" and "WEAK" in the band scale.
RISK_CAP_THRESHOLD = 60.0

VIRALITY_NOTE = (
    "Virality here is a structural estimate — how well the product is likely to "
    "demonstrate on video — not observed performance. Real virality needs "
    "hashtag, sound, and creator data that no connector currently supplies. "
    "Treat it as a prior to be replaced by measurement, not as a measurement."
)


@dataclass
class Dimension:
    """One scored axis. `value=None` means not measured."""

    name: str
    label: str
    value: float | None
    weight: float
    detail: str
    is_risk: bool = False       # high score = safe, and caps the overall

    @property
    def measured(self) -> bool:
        return self.value is not None

    @property
    def band(self) -> str:
        if self.value is None:
            return "UNMEASURED"
        if self.value >= 80:
            return "STRONG"
        if self.value >= 60:
            return "GOOD"
        if self.value >= 40:
            return "WEAK"
        return "POOR"


@dataclass
class Scorecard:
    sku: str
    title: str
    dimensions: list[Dimension]
    overall: float
    coverage_pct: float
    capped_by: str | None
    verdict: str                # PURSUE | INVESTIGATE | HOLD | REJECT
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def get(self, name: str) -> Dimension | None:
        return next((d for d in self.dimensions if d.name == name), None)

    def summary(self) -> str:
        return (
            f"{self.overall:.0f}/100 across {self.coverage_pct:.0f}% of dimensions"
            + (f", capped by {self.capped_by}" if self.capped_by else "")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sku": self.sku, "overall": self.overall,
            "coverage_pct": self.coverage_pct, "verdict": self.verdict,
            "capped_by": self.capped_by,
            "dimensions": {d.name: d.value for d in self.dimensions},
        }


def _clamp(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 1)


def _text_of(candidate: ProductCandidate) -> str:
    return candidate.searchable_text()


def _count_hits(text: str, terms: tuple[str, ...]) -> int:
    return sum(1 for t in terms if t in text)


# ---------------------------------------------------------------------------
# Individual dimensions
# ---------------------------------------------------------------------------
def score_demand(candidate: ProductCandidate, policy: Policy) -> Dimension:
    """Monthly unit demand against the policy floor, log-scaled.

    Log rather than linear because the difference between 100 and 500 units a
    month matters far more than between 5,000 and 5,400.
    """
    units = candidate.est_monthly_demand_units
    floor = float(policy.selection.get("min_monthly_demand_units", 100)) or 100
    if units <= 0:
        return Dimension("demand", "Demand", None, 0.15,
                         "No demand estimate available.")
    ratio = units / floor
    # Calibrated so the floor scores 50 and 100x the floor scores 100. A
    # narrower scale saturates almost immediately — most viable products clear
    # 10x the floor, and a dimension that always reads 100 carries no
    # information and quietly stops contributing to the ranking.
    value = _clamp(50 + math.log10(max(ratio, 0.01)) * 25)
    return Dimension("demand", "Demand", value, 0.15,
                     f"{units:,} units/mo against a {floor:.0f} floor "
                     f"({ratio:.1f}x).")


def score_trend(trend_shape: str | None, change_pct: float | None) -> Dimension:
    """Demand direction, from the TikTok trend classifier.

    Spike-decay is deliberately scored *below* steady: a product whose curve has
    already rolled over is worse than one that never spiked, because the stock
    it justifies buying will sit.
    """
    if trend_shape is None:
        return Dimension("trend", "Trend", None, 0.15,
                         "No trend history — needs 14+ days of daily data.")
    mapping = {
        "GROWING": 85.0,
        "STEADY": 60.0,
        "SPIKE_DECAY": 25.0,
        "DECAYING": 20.0,
        "DEAD": 5.0,
        "INSUFFICIENT_DATA": None,
    }
    value = mapping.get(trend_shape)
    if value is None:
        return Dimension("trend", "Trend", None, 0.15,
                         f"Trend shape {trend_shape} carries no score.")
    if trend_shape == "GROWING" and change_pct:
        value = _clamp(value + min(change_pct / 10, 15))
    detail = f"{trend_shape}" + (f" ({change_pct:+.0f}%)" if change_pct else "")
    return Dimension("trend", "Trend", _clamp(value), 0.15, detail)


def score_competition(candidate: ProductCandidate) -> Dimension:
    """Inverted crowding — high score means room to compete."""
    from .screening import competition_score

    crowding = competition_score(candidate)
    return Dimension(
        "competition", "Competition", _clamp(100 - crowding), 0.12,
        f"Crowding {crowding:.0f}/100 ({candidate.competitor_count} sellers, "
        f"top rival {candidate.top_rival_review_count:,} reviews).",
    )


def score_margin(econ: UnitEconomics, policy: Policy) -> Dimension:
    """Net margin against the policy minimum, with headroom rewarded."""
    minimum = float(policy.selection["min_margin_pct"])
    margin = econ.margin_pct
    if margin <= 0:
        return Dimension("margin", "Margin", 0.0, 0.15,
                         f"Negative margin ({margin:.1f}%).")
    # At the minimum, score 60. Double the minimum, score 100.
    value = _clamp(60 + (margin - minimum) / max(minimum, 1) * 40)
    return Dimension("margin", "Margin", value, 0.15,
                     f"{margin:.1f}% net margin vs {minimum:.0f}% minimum.")


def score_shipping(candidate: ProductCandidate, policy: Policy) -> Dimension:
    """Speed to customer. Domestic stock scores full marks."""
    s = candidate.supplier
    limit = int(policy.selection["max_shipping_days"])
    if s.domestic_stock:
        return Dimension("shipping", "Shipping", 100.0, 0.08,
                         f"Domestic stock ({s.shipping_days}d).")
    days = s.shipping_days
    value = _clamp((1 - days / (limit * 2)) * 100)
    return Dimension("shipping", "Shipping", value, 0.08,
                     f"{days}d transit vs {limit}d limit.")


def score_return_risk(candidate: ProductCandidate, policy: Policy,
                      observed_return_rate_pct: float | None = None) -> Dimension:
    """Inverted return risk — high score means low returns expected.

    Sizing-sensitive and fragile categories return far more, and on TikTok the
    baseline is already high because video-driven purchases are impulsive.
    """
    if observed_return_rate_pct is not None:
        rate = observed_return_rate_pct
        detail = f"{rate:.1f}% observed return rate."
    else:
        rate = float(policy.fees_for(candidate.marketplace)
                     .get("expected_return_rate_pct", 5.0))
        text = _text_of(candidate)
        risky = ("size", "fit", "apparel", "shoe", "clothing", "glass",
                 "ceramic", "fragile", "electronic")
        bumps = _count_hits(text, risky)
        rate += bumps * 2.0
        detail = (f"{rate:.1f}% estimated ({bumps} risk term(s) in the listing). "
                  "No observed rate yet.")
    value = _clamp(100 - rate * 6)
    return Dimension("return_risk", "Return risk", value, 0.08, detail, is_risk=True)


def score_policy_risk(candidate: ProductCandidate, policy: Policy,
                      marketplace: str = "tiktok") -> Dimension:
    """Inverted compliance risk. This one can cap everything else.

    Screening already hard-rejects clear violations. This scores the residual:
    near-miss language, restricted-adjacent categories, and TikTok's stricter
    content rules.
    """
    from .screening import _matches

    text = _text_of(candidate)
    prohibited = policy.prohibited
    hits: list[str] = []
    for key in ("hazmat_terms", "medical_claim_terms", "trademark_terms",
                "copyright_terms", "patent_risk_terms"):
        hits.extend(_matches(text, prohibited.get(key, [])))

    tiktok_hits: list[str] = []
    if marketplace == "tiktok":
        from connectors.tiktok.connector import (
            TIKTOK_PROHIBITED_TERMS,
            TIKTOK_RESTRICTED_CLAIMS,
        )
        tiktok_hits = [t for t in TIKTOK_PROHIBITED_TERMS + TIKTOK_RESTRICTED_CLAIMS
                       if t in text]

    total = len(hits) + len(tiktok_hits)
    if total == 0:
        return Dimension("policy_risk", "Policy risk", 95.0, 0.12,
                         "No prohibited or restricted terms detected.",
                         is_risk=True)
    # Any hit is serious; several is disqualifying. Account suspension is not
    # a gradient.
    value = _clamp(60 - total * 25)
    all_hits = ", ".join((hits + tiktok_hits)[:4])
    return Dimension("policy_risk", "Policy risk", value, 0.12,
                     f"{total} flagged term(s): {all_hits}.", is_risk=True)


def score_supplier(candidate: ProductCandidate, policy: Policy) -> Dimension:
    s = candidate.supplier
    minimum = float(policy.selection["min_supplier_rating"])
    parts = []
    # Rating relative to the 4.0-5.0 band that matters in practice.
    rating_component = _clamp((s.rating - 4.0) / 1.0 * 100)
    parts.append(rating_component)
    if s.quality_score:
        parts.append(s.quality_score)
    if s.on_time_rate_pct:
        parts.append(s.on_time_rate_pct)
    if s.defect_rate_pct is not None:
        parts.append(_clamp(100 - s.defect_rate_pct * 10))
    value = _clamp(sum(parts) / len(parts))
    detail = (f"Rating {s.rating:.2f} (min {minimum:.2f}), "
              f"{s.defect_rate_pct:.1f}% defects, {s.on_time_rate_pct:.0f}% on time.")
    return Dimension("supplier", "Supplier", value, 0.10, detail)


def score_review_sentiment(avg_rating: float | None,
                           review_count: int = 0) -> Dimension:
    """Own-product sentiment. Unmeasured until reviews exist."""
    if avg_rating is None or review_count == 0:
        return Dimension("review_sentiment", "Review sentiment", None, 0.05,
                         "No reviews yet — unmeasured, not good.")
    # 3.0 is the floor where conversion collapses; 5.0 is full marks.
    value = _clamp((avg_rating - 3.0) / 2.0 * 100)
    return Dimension("review_sentiment", "Review sentiment", value, 0.05,
                     f"{avg_rating:.2f}★ across {review_count} reviews.")


def score_virality(candidate: ProductCandidate) -> Dimension:
    """How well this product is likely to demonstrate on video.

    Structural, not observed. A product with a visible transformation and an
    obvious problem it solves is demonstrable in six seconds; a product whose
    value needs a paragraph is not, whatever its margins. On TikTok that
    distinction decides whether paid traffic ever becomes organic.
    """
    text = _text_of(candidate)
    transformation = _count_hits(text, VISUAL_TRANSFORMATION_TERMS)
    problem = _count_hits(text, PROBLEM_SOLUTION_TERMS)
    category_fit = any(c in text for c in HIGH_DEMO_CATEGORIES)

    value = 25.0
    value += min(transformation, 3) * 15
    value += min(problem, 3) * 10
    if category_fit:
        value += 15
    # A high price point suppresses impulse conversion regardless of demo value.
    if candidate.target_price > 60:
        value -= 15

    detail = (f"{transformation} transformation cue(s), {problem} problem-solution "
              f"cue(s)"
              + (", demo-friendly category" if category_fit else "")
              + (f", ${candidate.target_price:.0f} price point" if candidate.target_price > 60 else ""))
    return Dimension("virality", "Virality (structural)", _clamp(value), 0.10, detail)


def score_cash_flow(econ: UnitEconomics, candidate: ProductCandidate,
                    settlement_lag_days: int = 15) -> Dimension:
    """How fast a dollar comes back. Fast money is worth more than slow money."""
    from .economics import cash_cycle_days

    cycle = cash_cycle_days(candidate.supplier, sell_through_days=45) + settlement_lag_days
    # 60 days is excellent, 180 is poor.
    value = _clamp((1 - (cycle - 60) / 120) * 100)
    return Dimension("cash_flow", "Cash flow", value, 0.10,
                     f"~{cycle}d from cash out to cash back "
                     f"(incl. {settlement_lag_days}d settlement lag).")


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def build_scorecard(
    policy: Policy,
    candidate: ProductCandidate,
    econ: UnitEconomics,
    *,
    trend_shape: str | None = None,
    trend_change_pct: float | None = None,
    avg_rating: float | None = None,
    review_count: int = 0,
    observed_return_rate_pct: float | None = None,
    settlement_lag_days: int = 15,
    marketplace: str = "tiktok",
) -> Scorecard:
    """Score a candidate across every dimension the charter names."""
    dimensions = [
        score_demand(candidate, policy),
        score_trend(trend_shape, trend_change_pct),
        score_competition(candidate),
        score_margin(econ, policy),
        score_shipping(candidate, policy),
        score_return_risk(candidate, policy, observed_return_rate_pct),
        score_policy_risk(candidate, policy, marketplace),
        score_supplier(candidate, policy),
        score_review_sentiment(avg_rating, review_count),
        score_virality(candidate),
        score_cash_flow(econ, candidate, settlement_lag_days),
    ]

    measured = [d for d in dimensions if d.measured]
    total_weight = sum(d.weight for d in dimensions) or 1.0
    measured_weight = sum(d.weight for d in measured)
    coverage = round(measured_weight / total_weight * 100, 1)

    if not measured:
        return Scorecard(
            sku=candidate.sku, title=candidate.title, dimensions=dimensions,
            overall=0.0, coverage_pct=0.0, capped_by=None, verdict="HOLD",
            notes=["Nothing measurable about this candidate yet. Not a rejection — "
                   "an absence of evidence."],
        )

    # Weighted mean over measured dimensions only, so a missing dimension
    # reduces coverage rather than dragging the score toward the middle.
    weighted = sum((d.value or 0) * d.weight for d in measured) / measured_weight

    # Risk dimensions cap only when genuinely weak. A trademark landmine with
    # great margins must not average out to "good" — but a merely-below-average
    # risk score is not a landmine, and treating it as one makes the cap
    # meaningless.
    capped_by = None
    overall = weighted
    for d in dimensions:
        if not (d.is_risk and d.measured and d.value is not None):
            continue
        if d.value < RISK_CAP_THRESHOLD and d.value < overall:
            overall = d.value
            capped_by = d.label

    overall = _clamp(overall)

    strengths = [f"{d.label}: {d.value:.0f} — {d.detail}"
                 for d in measured if (d.value or 0) >= 75]
    weaknesses = [f"{d.label}: {d.value:.0f} — {d.detail}"
                  for d in measured if (d.value or 0) < 50]

    notes: list[str] = []
    unmeasured = [d.label for d in dimensions if not d.measured]
    if unmeasured:
        notes.append(
            f"Unmeasured: {', '.join(unmeasured)}. These are absent, not bad — "
            "the overall score covers only what was measurable."
        )
    if coverage < 60:
        notes.append(
            f"Only {coverage:.0f}% of the weighted scorecard is measurable. Rank "
            "provisionally; a score from half the dimensions is half a score."
        )
    if any(d.name == "virality" and d.measured for d in dimensions):
        notes.append(VIRALITY_NOTE)

    if capped_by:
        verdict = "REJECT" if overall < 40 else "INVESTIGATE"
        notes.append(
            f"Overall capped by {capped_by} rather than averaged. A single "
            "disqualifying risk is not offset by strength elsewhere."
        )
    elif overall >= 70 and coverage >= 60:
        verdict = "PURSUE"
    elif overall >= 55:
        verdict = "INVESTIGATE"
    elif overall >= 40:
        verdict = "HOLD"
    else:
        verdict = "REJECT"

    return Scorecard(
        sku=candidate.sku, title=candidate.title, dimensions=dimensions,
        overall=overall, coverage_pct=coverage, capped_by=capped_by,
        verdict=verdict, strengths=strengths, weaknesses=weaknesses, notes=notes,
    )


def rank(scorecards: list[Scorecard]) -> list[Scorecard]:
    """Rank by coverage-adjusted score.

    A 90 from 40% coverage ranks below an 80 from 90% coverage: the second is
    a claim we can actually support.
    """
    return sorted(
        scorecards,
        key=lambda s: -(s.overall * (s.coverage_pct / 100.0)),
    )
