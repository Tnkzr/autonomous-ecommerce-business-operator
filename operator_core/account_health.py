"""Marketplace account health.

Account suspension is the largest existential risk this business carries. Every
other failure mode — a bad product, a lost price war, an overspent ad budget —
costs money. Suspension costs the business. So this module warns well before a
metric reaches its limit, not at it: performance metrics are trailing windows,
and by the time the number crosses the line the orders that caused it are
already counted and cannot be undone.

Amazon's published targets are the defaults in `[account_health]`. The same
shape applies to the other marketplaces, which have their own names for the
same handful of measurements.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Policy
from .models import Severity


@dataclass
class HealthMetric:
    name: str
    label: str
    value: float | None
    limit: float
    lower_is_better: bool = True
    unit: str = "%"

    @property
    def known(self) -> bool:
        return self.value is not None

    @property
    def utilisation_pct(self) -> float | None:
        """How much of the allowed budget this metric has consumed."""
        if self.value is None or self.limit == 0:
            return None
        if self.lower_is_better:
            return round(self.value / self.limit * 100, 1)
        return round(self.limit / self.value * 100, 1) if self.value else None

    @property
    def breached(self) -> bool:
        if self.value is None:
            return False
        return self.value > self.limit if self.lower_is_better else self.value < self.limit


@dataclass
class HealthAssessment:
    marketplace: str
    metrics: list[HealthMetric]
    severity: Severity
    breaches: list[HealthMetric] = field(default_factory=list)
    warnings: list[HealthMetric] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.breaches and self.severity is not Severity.CRITICAL

    @property
    def coverage_pct(self) -> float:
        if not self.metrics:
            return 0.0
        known = sum(1 for m in self.metrics if m.known)
        return round(known / len(self.metrics) * 100, 1)


# Metric key -> (policy limit key, human label, lower_is_better)
METRIC_SPEC = {
    "order_defect_rate_pct": ("max_order_defect_rate_pct", "Order Defect Rate", True),
    "late_shipment_rate_pct": ("max_late_shipment_rate_pct", "Late Shipment Rate", True),
    "pre_fulfilment_cancel_rate_pct": (
        "max_pre_fulfilment_cancel_rate_pct", "Pre-Fulfilment Cancel Rate", True),
    "invalid_tracking_rate_pct": (
        "max_invalid_tracking_rate_pct", "Invalid Tracking Rate", True),
    "return_dissatisfaction_rate_pct": (
        "max_return_dissatisfaction_rate_pct", "Return Dissatisfaction Rate", True),
    "account_health_rating": ("min_account_health_rating", "Account Health Rating", False),
}

# What to actually do about each metric. Generic "improve your metrics" advice
# is useless; each of these has a specific operational cause.
REMEDIES = {
    "order_defect_rate_pct": (
        "ODR is the metric that suspends accounts. Read the defective orders "
        "individually — it is usually one SKU or one fulfilment failure, not a "
        "general decline. Pause ads on the offending SKU while you fix it."
    ),
    "late_shipment_rate_pct": (
        "Lengthen handling time in Seller Central today. A longer stated handling "
        "time costs a little conversion; late shipments cost account standing."
    ),
    "pre_fulfilment_cancel_rate_pct": (
        "This is almost always overselling. Reconcile listed quantity against "
        "actual on-hand and add a safety buffer to the quantity you publish."
    ),
    "invalid_tracking_rate_pct": (
        "Check the carrier mapping — this is usually a data problem, not a "
        "shipping problem, and it is the cheapest of these to fix."
    ),
    "return_dissatisfaction_rate_pct": (
        "Buyers are unhappy with how returns were handled. Approve returns "
        "faster and respond within 24 hours; the cost of a return is far below "
        "the cost of the metric."
    ),
    "account_health_rating": (
        "AHR aggregates policy violations and performance. Open the Account "
        "Health page and resolve every listed violation — each unresolved one "
        "keeps pulling the score down."
    ),
}


def assess(policy: Policy, marketplace: str, metrics: dict[str, Any]) -> HealthAssessment:
    """Evaluate account health from whatever metrics were supplied.

    Missing metrics are reported as unknown rather than assumed healthy. An
    unmeasured defect rate is not a passing defect rate, and reporting it as
    green is the failure mode that lets a suspension arrive unannounced.
    """
    cfg = policy.raw["account_health"]
    warn_at = float(cfg.get("warn_at_pct_of_limit", 75.0))

    built: list[HealthMetric] = []
    unknown: list[str] = []

    for key, (limit_key, label, lower_better) in METRIC_SPEC.items():
        value = metrics.get(key)
        limit = float(cfg[limit_key])
        unit = "" if key == "account_health_rating" else "%"
        if value is None:
            unknown.append(label)
        built.append(HealthMetric(
            name=key, label=label,
            value=float(value) if value is not None else None,
            limit=limit, lower_is_better=lower_better, unit=unit,
        ))

    breaches = [m for m in built if m.breached]
    warnings = [
        m for m in built
        if not m.breached and m.known
        and (m.utilisation_pct or 0) >= warn_at
    ]

    if breaches:
        severity = Severity.CRITICAL
    elif warnings:
        severity = Severity.WARN
    else:
        severity = Severity.INFO

    actions: list[str] = []
    for m in breaches:
        actions.append(
            f"BREACH — {m.label} at {m.value:g}{m.unit} against a "
            f"{m.limit:g}{m.unit} limit. {REMEDIES.get(m.name, '')}"
        )
    for m in warnings:
        actions.append(
            f"APPROACHING — {m.label} at {m.value:g}{m.unit} is "
            f"{m.utilisation_pct:.0f}% of the {m.limit:g}{m.unit} limit. "
            f"{REMEDIES.get(m.name, '')}"
        )

    if unknown:
        actions.append(
            f"{len(unknown)} metric(s) not supplied: {', '.join(unknown)}. "
            "These are reported as unknown, not healthy — an unmeasured defect "
            "rate is exactly how a suspension arrives without warning."
        )

    if not breaches and not warnings and not unknown:
        actions.append("All account health metrics within safe range.")

    return HealthAssessment(
        marketplace=marketplace, metrics=built, severity=severity,
        breaches=breaches, warnings=warnings, unknown=unknown, actions=actions,
    )


def blocks_scaling(assessment: HealthAssessment) -> tuple[bool, str]:
    """Whether account health should stop us pouring money into growth.

    Scaling ad spend or launching SKUs while account health is deteriorating
    multiplies the exposure: more orders through a broken process means more
    defects, faster. Growth waits until the account is stable.
    """
    if assessment.breaches:
        names = ", ".join(m.label for m in assessment.breaches)
        return True, (
            f"Account health breached ({names}). Hold all scaling — more volume "
            "through a failing process produces more defects, not more profit. "
            "Fix the metric, then scale."
        )
    if assessment.severity is Severity.WARN:
        names = ", ".join(m.label for m in assessment.warnings)
        return True, (
            f"Account health approaching limits ({names}). Pause budget increases "
            "and new launches until the trailing window recovers."
        )
    return False, "Account health permits scaling."
