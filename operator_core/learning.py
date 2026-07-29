"""Learning engine: what the history actually supports concluding.

The brief for this module is "learn which hooks perform best, which products
convert, which posting times work, which video styles succeed, which suppliers
perform". Every one of those is a grouped comparison over a small sample, and
grouped comparisons over small samples are where operating systems go wrong:
with six angles and twenty videos, one angle always looks best, and the system
that reports it has invented a finding.

So the primary output of every function here is not a ranking. It is a
*verdict* on whether a ranking is supportable, and the ranking is attached to
it. Three states:

- `SUPPORTED` — enough observations per group, and the leader separates from
  the field by more than the spread within it.
- `DIRECTIONAL` — enough observations to point, not enough to act. Reported,
  labelled, and explicitly not a basis for cutting a variant.
- `INSUFFICIENT` — cannot conclude. Says how many more observations are needed.

Two design choices carry most of the weight.

**Medians, not means.** Organic view counts are heavily skewed: one breakout
video in a group of five will make that group's mean beat every other group,
regardless of the creative. The median asks the question we actually care
about — does this angle reliably do better — rather than "did this angle
contain the lucky one".

**Separation is measured against within-group spread.** A leader whose margin
over second place is smaller than the variation inside its own group has not
separated from it. That single check is what stops a rotating cast of
"best-performing hooks" from being reported week after week.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# Below this many observations a group is not a group. Set deliberately low
# enough to be reachable by a working account and high enough that a single
# video cannot elect its own category.
MIN_OBSERVATIONS_PER_GROUP = 5
MIN_GROUPS_TO_COMPARE = 2
# Observations needed across all groups before any comparison runs at all.
MIN_TOTAL_OBSERVATIONS = 12

# A leader must beat the runner-up by more than this fraction of the field's
# own spread to count as separated. 1.0 means "the gap must exceed the typical
# within-group variation" — the weakest defensible bar, chosen because a
# stricter one would never fire at this business's data volumes.
SEPARATION_RATIO = 1.0

SUPPORTED = "SUPPORTED"
DIRECTIONAL = "DIRECTIONAL"
INSUFFICIENT = "INSUFFICIENT"


@dataclass
class GroupResult:
    group: str
    observations: int
    median: float
    spread: float               # interquartile-ish: half the min-max range
    best: float
    worst: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": self.group, "observations": self.observations,
            "median": self.median, "spread": self.spread,
            "best": self.best, "worst": self.worst,
        }


@dataclass
class Finding:
    """What can be concluded about one dimension, and how confidently."""

    dimension: str
    metric: str
    verdict: str
    leader: str | None
    groups: list[GroupResult]
    excluded_groups: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    observations_needed: int = 0

    @property
    def actionable(self) -> bool:
        return self.verdict == SUPPORTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "metric": self.metric,
            "verdict": self.verdict,
            "actionable": self.actionable,
            "leader": self.leader,
            "reason": self.reason,
            "observations_needed": self.observations_needed,
            "groups": [g.to_dict() for g in self.groups],
            "excluded_groups": self.excluded_groups,
        }


def median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


def _spread(values: list[float]) -> float:
    """Half the range. Crude, and deliberately so.

    A standard deviation on five skewed observations implies a distribution the
    data does not have. Half-range answers the only question being asked —
    how much do videos in this group differ from each other — without pretending
    to more.
    """
    if len(values) < 2:
        return 0.0
    return (max(values) - min(values)) / 2.0


# ---------------------------------------------------------------------------
def compare_groups(rows: Iterable[dict[str, Any]], *, dimension: str,
                   metric: str,
                   value_of: Callable[[dict[str, Any]], float | None] | None = None,
                   min_per_group: int = MIN_OBSERVATIONS_PER_GROUP,
                   higher_is_better: bool = True) -> Finding:
    """Group rows by one attribute and decide whether the ranking is supportable.

    `value_of` extracts the metric; rows returning None are skipped rather than
    counted as zero, since an unmeasured video is not a video that performed
    badly.
    """
    extract = value_of or (lambda r: r.get(metric))

    buckets: dict[str, list[float]] = {}
    for row in rows:
        key = str(row.get(dimension) or "").strip()
        if not key:
            continue
        value = extract(row)
        if value is None:
            continue
        buckets.setdefault(key, []).append(float(value))

    total = sum(len(v) for v in buckets.values())
    qualifying = {k: v for k, v in buckets.items() if len(v) >= min_per_group}
    excluded = [{"group": k, "observations": len(v),
                 "needed": min_per_group - len(v)}
                for k, v in buckets.items() if len(v) < min_per_group]

    if total < MIN_TOTAL_OBSERVATIONS:
        return Finding(
            dimension=dimension, metric=metric, verdict=INSUFFICIENT, leader=None,
            groups=[], excluded_groups=excluded,
            observations_needed=MIN_TOTAL_OBSERVATIONS - total,
            reason=(f"{total} usable observation(s) across all {dimension} "
                    f"values. Below {MIN_TOTAL_OBSERVATIONS} nothing can be "
                    "compared — with a handful of videos one group always "
                    "looks best, and reporting it would invent a finding."))

    if len(qualifying) < MIN_GROUPS_TO_COMPARE:
        needed = min(
            (min_per_group - len(v) for k, v in buckets.items()
             if len(v) < min_per_group), default=min_per_group)
        return Finding(
            dimension=dimension, metric=metric, verdict=INSUFFICIENT, leader=None,
            groups=[], excluded_groups=excluded, observations_needed=needed,
            reason=(f"Only {len(qualifying)} {dimension} value(s) have the "
                    f"{min_per_group} observations needed to be compared. "
                    "Concentrate output on fewer variants to learn faster — "
                    "spreading thin produces many groups and no answers."))

    results = []
    for group, values in qualifying.items():
        results.append(GroupResult(
            group=group, observations=len(values),
            median=round(median(values), 3), spread=round(_spread(values), 3),
            best=round(max(values), 3), worst=round(min(values), 3)))
    results.sort(key=lambda r: r.median, reverse=higher_is_better)

    leader, runner_up = results[0], results[1]
    gap = abs(leader.median - runner_up.median)
    typical_spread = median([r.spread for r in results]) or 0.0

    if typical_spread > 0 and gap < typical_spread * SEPARATION_RATIO:
        return Finding(
            dimension=dimension, metric=metric, verdict=DIRECTIONAL,
            leader=leader.group, groups=results, excluded_groups=excluded,
            reason=(f"'{leader.group}' leads on median {metric} "
                    f"({leader.median:,.3f} vs {runner_up.median:,.3f}), but the "
                    f"gap of {gap:,.3f} is smaller than the typical variation "
                    f"within a group ({typical_spread:,.3f}). Directionally "
                    "useful; not a basis for cutting the other variants."))

    return Finding(
        dimension=dimension, metric=metric, verdict=SUPPORTED,
        leader=leader.group, groups=results, excluded_groups=excluded,
        reason=(f"'{leader.group}' leads on median {metric} at "
                f"{leader.median:,.3f} against {runner_up.median:,.3f}, a gap "
                f"of {gap:,.3f} that exceeds the typical within-group spread of "
                f"{typical_spread:,.3f}. Supported by {leader.observations} "
                "observations."))


# ---------------------------------------------------------------------------
# Named dimensions
# ---------------------------------------------------------------------------
def _click_rate(row: dict[str, Any]) -> float | None:
    """Click-through as a fraction of views. None when either side is missing."""
    views = int(row.get("views") or 0)
    clicks = row.get("link_clicks")
    if views <= 0 or clicks is None:
        return None
    return float(clicks) / views * 100.0


def what_works(video_rows: list[dict[str, Any]]) -> dict[str, Finding]:
    """The four creative questions, each answered or explicitly declined.

    Click rate rather than views for the creative dimensions: views measure how
    well a video was distributed, clicks measure whether it sold. An angle that
    reliably gets reach and never gets clicks is an expensive kind of success,
    and ranking on views would promote it.
    """
    return {
        "angle": compare_groups(video_rows, dimension="angle",
                                metric="click_rate_pct", value_of=_click_rate),
        "hook": compare_groups(video_rows, dimension="hook_archetype",
                               metric="click_rate_pct", value_of=_click_rate),
        "format": compare_groups(video_rows, dimension="format",
                                 metric="click_rate_pct", value_of=_click_rate),
        "cta": compare_groups(video_rows, dimension="cta_variant",
                              metric="click_rate_pct", value_of=_click_rate),
        # Reach is a separate question from conversion and is asked separately.
        "reach_by_angle": compare_groups(video_rows, dimension="angle",
                                         metric="views"),
    }


def best_posting_slots(video_rows: list[dict[str, Any]]) -> Finding:
    """Which hour of which day earns reach — from our own posts only."""
    tagged = []
    for row in video_rows:
        if row.get("weekday") is None or row.get("hour") is None:
            continue
        enriched = dict(row)
        enriched["slot"] = f"{int(row['weekday'])}-{int(row['hour']):02d}"
        tagged.append(enriched)
    return compare_groups(tagged, dimension="slot", metric="views")


def product_performance(metric_rows: list[dict[str, Any]]) -> Finding:
    """Which products actually convert, by net profit per unit sold."""
    rows = []
    for row in metric_rows:
        units = int(row.get("units") or 0)
        if units <= 0:
            continue
        enriched = dict(row)
        enriched["profit_per_unit"] = float(row.get("net_profit") or 0.0) / units
        rows.append(enriched)
    return compare_groups(rows, dimension="sku", metric="profit_per_unit")


def supplier_performance(history_rows: list[dict[str, Any]]) -> Finding:
    """Which suppliers hold up over time, on their recorded scorecard history."""
    return compare_groups(history_rows, dimension="supplier_name", metric="score")


# ---------------------------------------------------------------------------
def learning_report(*, video_rows: list[dict[str, Any]],
                    metric_rows: list[dict[str, Any]] | None = None,
                    supplier_rows: list[dict[str, Any]] | None = None
                    ) -> dict[str, Any]:
    """Everything the history supports, and everything it does not.

    The headline is `actionable_findings` over `total_questions`. A low ratio is
    the expected state for a young account and is reported as such rather than
    padded with directional results dressed up as conclusions.
    """
    findings: dict[str, Finding] = dict(what_works(video_rows))
    findings["posting_slot"] = best_posting_slots(video_rows)
    if metric_rows:
        findings["product"] = product_performance(metric_rows)
    if supplier_rows:
        findings["supplier"] = supplier_performance(supplier_rows)

    actionable = [k for k, f in findings.items() if f.actionable]
    directional = [k for k, f in findings.items() if f.verdict == DIRECTIONAL]
    insufficient = [k for k, f in findings.items() if f.verdict == INSUFFICIENT]

    if actionable:
        headline = (f"{len(actionable)} of {len(findings)} questions can be "
                    f"answered from the data so far: {', '.join(actionable)}.")
    elif directional:
        headline = ("Nothing is conclusive yet, but "
                    f"{len(directional)} question(s) point somewhere. Keep "
                    "posting the same variants rather than adding new ones — "
                    "adding variants restarts the count.")
    else:
        headline = ("Not enough history to answer any question yet. This is the "
                    "expected state for a new account; the fastest route out of "
                    "it is consistent output on a small number of variants, not "
                    "more variants.")

    return {
        "questions_asked": len(findings),
        "actionable_findings": len(actionable),
        "directional_findings": len(directional),
        "insufficient": len(insufficient),
        "headline": headline,
        "findings": {k: f.to_dict() for k, f in findings.items()},
        "note": (
            "Rankings are reported with a verdict, never alone. A ranking whose "
            "leader has not separated from the field is labelled DIRECTIONAL and "
            "is not a basis for cutting variants — that is what stops a rotating "
            "cast of 'best performing' results being reported every week."),
    }
