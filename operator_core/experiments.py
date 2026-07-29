"""Product and creative testing: launch, measure, scale winners, archive losers.

"Test products and scale the winners" is easy to say and is usually implemented
as: launch several, look at the numbers, keep the one that looks best. That
procedure has no failure mode — it always produces a winner, including when
every product is bad and when the difference is noise. This module exists to
give it one.

Three rules, each of which makes it possible for a test to *fail*:

**The success criterion is registered before the test runs.** `store.register_
experiment` writes the metric, the threshold and the minimum sample at
registration. A threshold chosen after seeing the result is not a threshold.

**A result below the sample floor concludes INCONCLUSIVE, not "the best one".**
The floor is computed from the baseline rate and the effect worth detecting,
not picked to be reachable. Most small tests genuinely cannot resolve, and
saying so is the finding.

**Scaling requires profitability, not just a winning arm.** An arm can beat its
sibling and still lose money. `evaluate` checks the arm against the success
threshold *and* against contribution per exposure, because scaling a loss is
the one mistake in this business that compounds.

The comparison uses a normal approximation to the difference in two
proportions. That is deliberately modest arithmetic: the sample sizes here are
hundreds, the effects are large or not worth having, and a more elaborate model
would imply a precision the data does not carry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import Policy

# Two-sided z for a 95% interval. Not configurable: a per-test confidence level
# is a knob that only ever gets turned until the answer is the desired one.
Z_95 = 1.96

OUTCOMES = ("SCALE", "ARCHIVE", "INCONCLUSIVE", "ABANDONED")


@dataclass
class ArmResult:
    arm: str
    description: str
    exposures: int
    conversions: int
    revenue: float
    spend: float

    @property
    def rate(self) -> float | None:
        return self.conversions / self.exposures if self.exposures else None

    @property
    def revenue_per_exposure(self) -> float | None:
        return self.revenue / self.exposures if self.exposures else None

    @property
    def contribution(self) -> float:
        """Revenue less direct spend. Not profit — cost of goods is not here."""
        return round(self.revenue - self.spend, 2)

    def standard_error(self) -> float | None:
        rate = self.rate
        if rate is None or self.exposures <= 0:
            return None
        return math.sqrt(max(rate * (1 - rate), 0.0) / self.exposures)


@dataclass
class Evaluation:
    experiment_id: str
    sku: str
    hypothesis: str
    success_metric: str
    success_threshold: float
    min_sample: int
    arms: list[ArmResult]
    leader: str | None
    runner_up: str | None
    absolute_lift: float | None
    relative_lift_pct: float | None
    confidence_interval: tuple[float, float] | None
    significant: bool
    sample_reached: bool
    verdict: str
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "sku": self.sku,
            "hypothesis": self.hypothesis,
            "verdict": self.verdict,
            "leader": self.leader,
            "runner_up": self.runner_up,
            "absolute_lift": self.absolute_lift,
            "relative_lift_pct": self.relative_lift_pct,
            "confidence_interval": list(self.confidence_interval)
                                   if self.confidence_interval else None,
            "significant": self.significant,
            "sample_reached": self.sample_reached,
            "success_metric": self.success_metric,
            "success_threshold": self.success_threshold,
            "arms": [
                {"arm": a.arm, "exposures": a.exposures,
                 "conversions": a.conversions, "rate": a.rate,
                 "revenue": round(a.revenue, 2), "spend": round(a.spend, 2),
                 "contribution": a.contribution,
                 "revenue_per_exposure": a.revenue_per_exposure}
                for a in self.arms
            ],
            "reasons": self.reasons,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
def required_sample_per_arm(*, baseline_rate: float,
                            minimum_detectable_effect: float,
                            power: float = 0.8) -> int:
    """Sample per arm needed to detect a relative lift of the given size.

    Standard two-proportion sizing at 95% confidence. Included because the
    alternative — picking a round number that feels achievable — produces tests
    that were never capable of answering their own question, which is worse
    than not running them: they cost the same and yield a confident wrong
    answer.
    """
    if not 0 < baseline_rate < 1:
        raise ValueError(
            f"baseline_rate must be a proportion between 0 and 1, got "
            f"{baseline_rate}. A rate expressed as a percentage here "
            "under-sizes the test by a hundredfold.")
    if minimum_detectable_effect <= 0:
        raise ValueError("minimum_detectable_effect must be positive.")

    # z for the requested power, one-sided. Table lookup rather than an inverse
    # normal, since only a few power levels are ever used.
    z_power = {0.8: 0.8416, 0.9: 1.2816, 0.95: 1.6449}.get(round(power, 2))
    if z_power is None:
        raise ValueError(
            f"Unsupported power {power}. Use 0.8, 0.9, or 0.95 — anything else "
            "implies a precision this sample size formula does not have.")

    treated = baseline_rate * (1 + minimum_detectable_effect)
    if treated >= 1:
        raise ValueError(
            f"A {minimum_detectable_effect:.0%} lift on a {baseline_rate:.1%} "
            "baseline exceeds 100%. Pick a smaller effect.")
    pooled = (baseline_rate + treated) / 2
    numerator = (Z_95 * math.sqrt(2 * pooled * (1 - pooled))
                 + z_power * math.sqrt(baseline_rate * (1 - baseline_rate)
                                       + treated * (1 - treated))) ** 2
    denominator = (treated - baseline_rate) ** 2
    return int(math.ceil(numerator / denominator))


def compare_arms(leader: ArmResult, control: ArmResult
                 ) -> tuple[float | None, float | None, tuple[float, float] | None, bool]:
    """Difference in conversion rate with a 95% interval.

    Significance is "the interval excludes zero". Reported alongside the
    interval rather than as a bare boolean, because the width is the useful
    part: an interval spanning -1% to +40% is not a result, however it is
    labelled.
    """
    lead_rate, control_rate = leader.rate, control.rate
    if lead_rate is None or control_rate is None:
        return None, None, None, False

    absolute = lead_rate - control_rate
    relative = (absolute / control_rate * 100) if control_rate else None

    se_lead = leader.standard_error() or 0.0
    se_control = control.standard_error() or 0.0
    se_diff = math.sqrt(se_lead ** 2 + se_control ** 2)
    if se_diff <= 0:
        return absolute, relative, None, False

    margin = Z_95 * se_diff
    interval = (round(absolute - margin, 5), round(absolute + margin, 5))
    significant = interval[0] > 0 or interval[1] < 0
    return absolute, relative, interval, significant


def evaluate(record: dict[str, Any], policy: Policy) -> Evaluation:
    """Judge a registered experiment against the criteria it was registered with."""
    cfg = policy.raw.get("experiments", {})
    arms = [ArmResult(
        arm=str(a.get("arm", "")), description=str(a.get("description", "")),
        exposures=int(a.get("exposures") or 0),
        conversions=int(a.get("conversions") or 0),
        revenue=float(a.get("revenue") or 0.0),
        spend=float(a.get("spend") or 0.0),
    ) for a in record.get("arms", [])]

    min_sample = int(record.get("min_sample") or 0)
    threshold = float(record.get("success_threshold") or 0.0)
    reasons: list[str] = []
    warnings: list[str] = []

    ranked = sorted((a for a in arms if a.rate is not None),
                    key=lambda a: a.rate or 0.0, reverse=True)
    leader = ranked[0] if ranked else None
    runner_up = ranked[1] if len(ranked) > 1 else None

    sample_reached = all(a.exposures >= min_sample for a in arms) and bool(arms)
    absolute = relative = interval = None
    significant = False
    if leader and runner_up:
        absolute, relative, interval, significant = compare_arms(leader, runner_up)

    # --- verdict ---------------------------------------------------------
    if not arms:
        return Evaluation(
            experiment_id=str(record.get("experiment_id", "")),
            sku=str(record.get("sku", "")),
            hypothesis=str(record.get("hypothesis", "")),
            success_metric=str(record.get("success_metric", "")),
            success_threshold=threshold, min_sample=min_sample, arms=[],
            leader=None, runner_up=None, absolute_lift=None,
            relative_lift_pct=None, confidence_interval=None, significant=False,
            sample_reached=False, verdict="INCONCLUSIVE",
            reasons=["The experiment has no arms recorded."])

    max_spend = float(record.get("max_spend_usd") or 0.0)
    total_spend = sum(a.spend for a in arms)
    if max_spend and total_spend > max_spend:
        warnings.append(
            f"Spend on this test is ${total_spend:,.2f} against a ${max_spend:,.2f} "
            "cap. The cap is the point of a test — stop it rather than letting "
            "it find the answer eventually.")

    deadline = str(record.get("deadline") or "")
    expired = False
    if deadline:
        try:
            expired = datetime.fromisoformat(deadline.replace("Z", "+00:00")) < \
                datetime.now(timezone.utc)
        except ValueError:
            warnings.append(f"Deadline {deadline!r} is not a valid timestamp.")

    if not sample_reached:
        short = [f"{a.arm} ({a.exposures:,}/{min_sample:,})"
                 for a in arms if a.exposures < min_sample]
        reasons.append(
            f"Below the registered sample floor: {', '.join(short)}. A "
            "difference at this size is one or two conversions of noise — the "
            "registered floor exists so the test can fail rather than always "
            "producing a winner.")
        verdict = "ABANDONED" if expired else "INCONCLUSIVE"
        if expired:
            reasons.append(
                "The deadline passed without reaching sample. Running longer "
                "at this traffic would take more time than the answer is worth.")
        return Evaluation(
            experiment_id=str(record.get("experiment_id", "")),
            sku=str(record.get("sku", "")), hypothesis=str(record.get("hypothesis", "")),
            success_metric=str(record.get("success_metric", "")),
            success_threshold=threshold, min_sample=min_sample, arms=arms,
            leader=leader.arm if leader else None,
            runner_up=runner_up.arm if runner_up else None,
            absolute_lift=absolute, relative_lift_pct=relative,
            confidence_interval=interval, significant=significant,
            sample_reached=False, verdict=verdict, reasons=reasons,
            warnings=warnings)

    assert leader is not None
    leader_rate = leader.rate or 0.0
    meets_threshold = leader_rate >= threshold
    contribution_per_exposure = (leader.contribution / leader.exposures
                                 if leader.exposures else 0.0)
    min_contribution = float(cfg.get("min_contribution_per_exposure_usd", 0.0))
    profitable = contribution_per_exposure >= min_contribution

    if not meets_threshold:
        verdict = "ARCHIVE"
        reasons.append(
            f"Best arm '{leader.arm}' converts at {leader_rate:.2%}, under the "
            f"{threshold:.2%} registered as success. It won its comparison and "
            "still failed its criterion — which is the criterion doing its job.")
    elif not profitable:
        verdict = "ARCHIVE"
        reasons.append(
            f"'{leader.arm}' beats the threshold but contributes "
            f"${contribution_per_exposure:,.4f} per exposure against a required "
            f"${min_contribution:,.4f}. Scaling this scales a loss, and a loss "
            "is the one thing in this business that compounds reliably.")
    elif runner_up is not None and not significant:
        verdict = "INCONCLUSIVE"
        span = (f"{interval[0]:+.2%} to {interval[1]:+.2%}"
                if interval else "unavailable")
        reasons.append(
            f"'{leader.arm}' leads but the 95% interval on the difference spans "
            f"{span}, which includes zero. The arms are not distinguishable; "
            "picking the higher number here is picking noise.")
        warnings.append(
            "Both arms cleared the success threshold, so either can be run — "
            "the test failed to separate them, not to find something workable.")
    else:
        verdict = "SCALE"
        reasons.append(
            f"'{leader.arm}' converts at {leader_rate:.2%} against a "
            f"{threshold:.2%} threshold, contributes "
            f"${contribution_per_exposure:,.4f} per exposure, and the "
            "difference from the next arm excludes zero.")

    if interval and verdict == "SCALE" and interval[0] > 0:
        width = interval[1] - interval[0]
        if absolute and width > abs(absolute) * 2:
            warnings.append(
                f"The interval is wide relative to the effect ({interval[0]:+.2%} "
                f"to {interval[1]:+.2%}). The direction is reliable; the size is "
                "not. Scale, but do not forecast off this number.")

    return Evaluation(
        experiment_id=str(record.get("experiment_id", "")),
        sku=str(record.get("sku", "")), hypothesis=str(record.get("hypothesis", "")),
        success_metric=str(record.get("success_metric", "")),
        success_threshold=threshold, min_sample=min_sample, arms=arms,
        leader=leader.arm, runner_up=runner_up.arm if runner_up else None,
        absolute_lift=absolute, relative_lift_pct=relative,
        confidence_interval=interval, significant=significant,
        sample_reached=True, verdict=verdict, reasons=reasons, warnings=warnings)


def portfolio_view(evaluations: list[Evaluation]) -> dict[str, Any]:
    """The whole test programme at a glance.

    `inconclusive_share` is the number to watch. A programme where most tests
    resolve is usually testing changes too large to be interesting; one where
    most do not is running underpowered tests and learning nothing from either.
    """
    if not evaluations:
        return {"experiments": 0,
                "note": "No experiments registered. A business that ships without "
                        "testing learns only from its failures, and slowly."}
    counts: dict[str, int] = {}
    for evaluation in evaluations:
        counts[evaluation.verdict] = counts.get(evaluation.verdict, 0) + 1
    total = len(evaluations)
    inconclusive = counts.get("INCONCLUSIVE", 0) + counts.get("ABANDONED", 0)
    share = round(inconclusive / total * 100, 1)

    note = "Test programme is resolving normally."
    if share > 60:
        note = (f"{share}% of tests did not resolve. The tests are underpowered "
                "for the traffic available — test fewer, larger changes rather "
                "than many small ones.")
    elif share < 10 and total >= 5:
        note = (f"Only {share}% of tests were inconclusive, which is suspiciously "
                "clean at this traffic level. Check that the sample floors are "
                "being enforced rather than met by construction.")
    return {
        "experiments": total,
        "by_verdict": counts,
        "scaled": counts.get("SCALE", 0),
        "archived": counts.get("ARCHIVE", 0),
        "inconclusive_share_pct": share,
        "note": note,
    }
