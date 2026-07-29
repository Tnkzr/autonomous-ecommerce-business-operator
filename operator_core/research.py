"""Research engine: a ranked pipeline of product opportunities.

The charter asks for continuous monitoring of TikTok trends and search, Google
Trends, Amazon Best Sellers, Reddit, Pinterest, YouTube, seasonality, news,
emerging interests, competitor stores, and supplier catalogues. Two of those
have a connected feed. This module is built around that fact rather than around
the wish.

**Opportunities are scored only on sources that reported.** An unconnected
source contributes nothing — not a neutral middle value, which would quietly
drag every opportunity toward the same score and make the ranking a function of
how many sources are missing rather than of the product. Every opportunity
therefore carries its own coverage figure, and `effective_score` is the raw
score multiplied by it. A brilliant product scored on one source ranks below a
good one scored on four, which is the correct ordering when the question is
"where should the next thousand dollars go".

**The pipeline is a memory, not a report.** An opportunity that was scored six
weeks ago and never acted on is not still a good idea — the demand it was
scored on has moved and nobody checked. `stale_after_days` in `[research]`
retires them, and a retired opportunity is reported as retired rather than
silently dropped, because "we looked at this and let it rot" is the lesson.

**Nothing here estimates demand.** There is no function that turns a category
and a hunch into a monthly unit figure. Where a source is absent, the gap is
named along with what would connect it. A confident guess about demand is the
most expensive fabrication available to this system, because it survives into a
purchase order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .config import Policy
from .signals import SOURCE_CONNECTORS, SOURCE_REQUIREMENTS

# Pipeline stages, in the order an opportunity moves through them. Backwards
# movement is allowed and recorded — an opportunity that fails screening after
# looking good is the most informative thing in the table.
STAGES = ("DISCOVERED", "SCREENING", "SCORED", "VALIDATED", "LAUNCHED",
          "REJECTED", "RETIRED")

TERMINAL_STAGES = ("REJECTED", "RETIRED")

# What each research question needs before it can be answered, and what would
# answer it. Keyed to `signals.SOURCE_CONNECTORS` so there is one register of
# what this system can and cannot see.
RESEARCH_QUESTIONS = {
    "is_demand_growing": ("google_trends", "tiktok_hashtag_momentum"),
    "is_it_saturated": ("amazon_search_volume", "tiktok_creator_adoption"),
    "is_it_already_selling": ("amazon_sales_rank", "tiktok_product_velocity"),
    "is_it_seasonal": ("seasonality",),
    "is_it_being_discussed": ("social_reddit", "social_youtube", "social_pinterest"),
    "is_it_about_to_be_regulated": ("news_regulatory",),
}


@dataclass
class SourceReading:
    """One source's contribution to one opportunity."""

    source: str
    direction: str              # positive | negative | neutral
    strength: float             # 0-1
    detail: str = ""
    observed_at: str = ""
    origin: str = "manual"      # manual | live | import

    @property
    def connected(self) -> bool:
        return SOURCE_CONNECTORS.get(self.source) is not None


@dataclass
class Opportunity:
    opportunity_id: str
    title: str
    category: str
    stage: str
    discovered_at: str
    readings: list[SourceReading] = field(default_factory=list)
    sku: str = ""
    notes: str = ""
    rejected_reason: str = ""
    last_moved_at: str = ""

    @property
    def sources_reporting(self) -> int:
        return len({r.source for r in self.readings})

    @property
    def live_sources(self) -> int:
        return len({r.source for r in self.readings if r.origin == "live"})


@dataclass
class ScoredOpportunity:
    opportunity: Opportunity
    raw_score: float
    coverage_pct: float
    effective_score: float
    positive_sources: int
    negative_sources: int
    unanswered_questions: list[str]
    blockers: list[str] = field(default_factory=list)
    promotable: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity.opportunity_id,
            "title": self.opportunity.title,
            "category": self.opportunity.category,
            "stage": self.opportunity.stage,
            "raw_score": self.raw_score,
            "coverage_pct": self.coverage_pct,
            "effective_score": self.effective_score,
            "sources_reporting": self.opportunity.sources_reporting,
            "positive_sources": self.positive_sources,
            "negative_sources": self.negative_sources,
            "unanswered_questions": self.unanswered_questions,
            "promotable": self.promotable,
            "blockers": self.blockers,
            "note": self.note,
        }


def _research_cfg(policy: Policy) -> dict[str, Any]:
    return policy.raw.get("research", {})


def _signal_weights(policy: Policy) -> dict[str, float]:
    return {k: float(v) for k, v in policy.raw.get("signals", {})
            .get("weights", {}).items()}


# ---------------------------------------------------------------------------
def score_opportunity(opportunity: Opportunity, policy: Policy
                      ) -> ScoredOpportunity:
    """Score on reporting sources only, then scale by coverage.

    The two numbers are kept separate on purpose. `raw_score` answers "how good
    does this look on what we know"; `coverage_pct` answers "how much do we
    know". Collapsing them into one number hides the second, and the second is
    what determines whether the first is worth anything.
    """
    weights = _signal_weights(policy)
    cfg = _research_cfg(policy)

    total_weight = sum(weights.values()) or 1.0
    reported_weight = 0.0
    weighted_sum = 0.0
    positive = negative = 0
    seen: set[str] = set()

    for reading in opportunity.readings:
        weight = weights.get(reading.source)
        if weight is None:
            # A source with no weight in policy is not scored. Adding one is a
            # policy change, not a code change.
            continue
        if reading.source not in seen:
            reported_weight += weight
            seen.add(reading.source)
        sign = {"positive": 1.0, "negative": -1.0}.get(reading.direction, 0.0)
        weighted_sum += weight * sign * max(0.0, min(reading.strength, 1.0))
        if sign > 0:
            positive += 1
        elif sign < 0:
            negative += 1

    coverage = reported_weight / total_weight if total_weight else 0.0
    # Map the signed weighted sum onto 0-100 across the weight that reported.
    raw = (50.0 + (weighted_sum / reported_weight) * 50.0) if reported_weight else 0.0
    raw = round(max(0.0, min(raw, 100.0)), 1)
    effective = round(raw * coverage, 1)

    unanswered = [
        question for question, sources in RESEARCH_QUESTIONS.items()
        if not any(s in seen for s in sources)
    ]

    blockers: list[str] = []
    min_sources = int(cfg.get("min_sources_to_promote", 2))
    min_confidence = float(cfg.get("min_confidence_to_promote", 40.0))

    if opportunity.sources_reporting < min_sources:
        blockers.append(
            f"{opportunity.sources_reporting} source(s) reporting, "
            f"{min_sources} required. One strong signal is an anecdote — the "
            "multi-signal rule exists because single-source enthusiasm is what "
            "buys inventory nobody wants.")
    if effective < min_confidence:
        blockers.append(
            f"Effective score {effective} is below the {min_confidence} floor. "
            f"The raw score is {raw} but only {coverage * 100:.0f}% of the "
            "weighted signal set reported, so most of that score is untested.")
    if negative and not positive:
        blockers.append(
            f"{negative} source(s) report negatively and none positively. "
            "A product whose only evidence is against it does not need more "
            "research.")

    note = ""
    if coverage < 0.5:
        note = (f"Scored on {coverage * 100:.0f}% of the weighted signal set. "
                "Unconnected sources contribute nothing rather than a neutral "
                "middle value — a neutral default would drag every opportunity "
                "toward the same score and make this ranking a function of what "
                "is missing rather than of the product.")

    return ScoredOpportunity(
        opportunity=opportunity, raw_score=raw,
        coverage_pct=round(coverage * 100, 1), effective_score=effective,
        positive_sources=positive, negative_sources=negative,
        unanswered_questions=unanswered, blockers=blockers,
        promotable=not blockers, note=note)


def rank_pipeline(opportunities: list[Opportunity], policy: Policy,
                  *, today: date | None = None) -> dict[str, Any]:
    """Rank the pipeline and retire what has gone stale.

    Ranked on effective score, so an opportunity we know a lot about outranks
    one that merely looks good. Ties break on how many sources reported, for the
    same reason.
    """
    cfg = _research_cfg(policy)
    today = today or datetime.now(timezone.utc).date()
    stale_days = int(cfg.get("stale_after_days", 30))
    max_size = int(cfg.get("max_pipeline_size", 50))
    cutoff = today - timedelta(days=stale_days)

    active: list[ScoredOpportunity] = []
    retired: list[dict[str, Any]] = []

    for opportunity in opportunities:
        if opportunity.stage in TERMINAL_STAGES:
            continue
        moved = opportunity.last_moved_at or opportunity.discovered_at
        try:
            moved_date = datetime.fromisoformat(
                str(moved).replace("Z", "+00:00")).date()
        except (ValueError, TypeError):
            moved_date = today
        if moved_date < cutoff:
            retired.append({
                "opportunity_id": opportunity.opportunity_id,
                "title": opportunity.title,
                "days_idle": (today - moved_date).days,
                "reason": (f"No movement for {(today - moved_date).days} days. "
                           "The demand it was scored on has moved and nobody "
                           "checked; re-research it rather than acting on the "
                           "old score."),
            })
            continue
        active.append(score_opportunity(opportunity, policy))

    active.sort(key=lambda s: (s.effective_score, s.opportunity.sources_reporting),
                reverse=True)
    promotable = [s for s in active if s.promotable]

    warnings: list[str] = []
    if len(active) > max_size:
        warnings.append(
            f"{len(active)} active opportunities against a {max_size} cap. A "
            "pipeline larger than the team can research is a list, not a "
            "pipeline — reject the bottom rather than carrying them.")
    if active and not promotable:
        warnings.append(
            "No opportunity clears promotion. That is a signal about coverage, "
            "not about the products: with two of eleven sources connected, most "
            "things will look unproven because most things are unproven here.")
    unconnected = [s for s, c in SOURCE_CONNECTORS.items() if c is None]
    if unconnected:
        warnings.append(
            f"{len(unconnected)} of {len(SOURCE_CONNECTORS)} signal sources have "
            "no connector. Every score below is capped by that, and no amount of "
            "research effort changes it — only wiring a feed does.")

    return {
        "generated_for": today.isoformat(),
        "active": len(active),
        "promotable": len(promotable),
        "retired": retired,
        "ranked": [s.to_dict() for s in active[:max_size]],
        "top": active[0].to_dict() if active else None,
        "warnings": warnings,
    }


def coverage_report() -> dict[str, Any]:
    """What the research engine can and cannot see, with the remedy for each.

    Reported on every pipeline run rather than buried in documentation. A
    limitation nobody is reminded of gets designed around instead of fixed.
    """
    connected, missing = [], []
    for source, connector in sorted(SOURCE_CONNECTORS.items()):
        if connector:
            connected.append({"source": source, "connector": connector})
        else:
            missing.append({
                "source": source,
                "would_need": SOURCE_REQUIREMENTS.get(
                    source, "No connector implemented and no route documented."),
            })
    total = len(SOURCE_CONNECTORS)
    return {
        "sources_total": total,
        "sources_connected": len(connected),
        "sources_missing": len(missing),
        "connected_pct": round(len(connected) / total * 100, 1) if total else 0.0,
        "connected": connected,
        "missing": missing,
        "note": (
            "Every unconnected source is a question this system cannot answer. "
            "It contributes nothing to a score rather than a neutral value, and "
            "the resulting coverage figure travels with every recommendation. "
            "Do not substitute an impression of what is trending for a feed."),
    }


def unanswered_questions(opportunity: Opportunity) -> list[dict[str, str]]:
    """The research questions this opportunity has no evidence for.

    Framed as questions rather than as missing fields, because the useful
    output is "we do not know whether demand is growing", not "google_trends is
    null".
    """
    reporting = {r.source for r in opportunity.readings}
    out = []
    for question, sources in RESEARCH_QUESTIONS.items():
        if any(s in reporting for s in sources):
            continue
        connectable = [s for s in sources if SOURCE_CONNECTORS.get(s)]
        out.append({
            "question": question.replace("_", " ").capitalize() + "?",
            "answerable_by": ", ".join(sources),
            "status": ("Unanswered — a connected source exists but has not "
                       f"reported: {', '.join(connectable)}."
                       if connectable else
                       "Unanswerable — none of these sources has a connector."),
        })
    return out
