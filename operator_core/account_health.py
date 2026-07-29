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


# ---------------------------------------------------------------------------
# TikTok Shop store health
#
# TikTok is the primary marketplace and its metrics are not Amazon's. The names
# differ, the thresholds are tighter, and the enforcement mechanism is different
# in a way that matters operationally: TikTok accumulates *violation points*,
# and it suppresses a shop's reach well before it suspends the account. By the
# time a metric hits its limit, the traffic loss has already happened — which is
# why the warning line here sits lower than Amazon's.
# ---------------------------------------------------------------------------
TIKTOK_METRIC_SPEC = {
    "seller_violation_points": (
        "max_seller_violation_points", "Seller violation points", True, ""),
    "late_dispatch_rate_pct": (
        "max_late_dispatch_rate_pct", "Late dispatch rate", True, "%"),
    "cancellation_rate_pct": (
        "max_cancellation_rate_pct", "Seller cancellation rate", True, "%"),
    "return_refund_rate_pct": (
        "max_return_refund_rate_pct", "Return & refund rate", True, "%"),
    "negative_review_rate_pct": (
        "max_negative_review_rate_pct", "Negative review rate", True, "%"),
    "shop_rating": ("min_shop_rating", "Shop rating", False, "★"),
    "fulfilment_rate_pct": (
        "min_fulfilment_rate_pct", "Fulfilment rate", False, "%"),
    "avg_dispatch_hours": (
        "max_avg_dispatch_hours", "Average dispatch time", True, "h"),
}

TIKTOK_REMEDIES = {
    "seller_violation_points": (
        "Points are the mechanism that actually suspends a shop. Open Seller "
        "Center > Compliance and appeal anything appealable — points expire, but "
        "only after 90 days, and they stack in the meantime."
    ),
    "late_dispatch_rate_pct": (
        "Extend the stated handling time today. A longer handling time costs a "
        "little conversion; late dispatch costs reach, and reach does not come "
        "back when the metric recovers."
    ),
    "cancellation_rate_pct": (
        "Seller-initiated cancellations are almost always overselling. Reconcile "
        "published quantity against real stock and publish less than you hold."
    ),
    "return_refund_rate_pct": (
        "Look at which SKU drives it before touching anything else — on TikTok "
        "this is usually one product whose video oversold what arrives."
    ),
    "negative_review_rate_pct": (
        "Read the actual reviews for a recurring theme. A rate is a symptom; the "
        "theme is the defect."
    ),
    "shop_rating": (
        "Below 4.4 TikTok suppresses traffic to the whole shop, not just the "
        "product that caused it. Treat this as shop-wide, not per-listing."
    ),
    "fulfilment_rate_pct": (
        "Orders accepted but not fulfilled. Check stock accuracy and warehouse "
        "mapping — a SKU split across warehouses can show stock it cannot ship."
    ),
    "avg_dispatch_hours": (
        "Dispatch speed feeds both late-dispatch rate and search ranking. Batch "
        "picking earlier in the day is usually the cheapest fix."
    ),
}


def assess_tiktok(policy: Policy, metrics: dict[str, Any]) -> HealthAssessment:
    """Evaluate TikTok Shop health against `[tiktok_health]`.

    Same contract as `assess`: unsupplied metrics are reported as unknown, never
    assumed healthy. An unmeasured violation-point count is exactly how a
    suspension arrives without warning.
    """
    cfg = policy.raw["tiktok_health"]
    warn_at = float(cfg.get("warn_at_pct_of_limit", 70.0))

    built: list[HealthMetric] = []
    unknown: list[str] = []

    for key, (limit_key, label, lower_better, unit) in TIKTOK_METRIC_SPEC.items():
        value = metrics.get(key)
        if value is None:
            unknown.append(label)
        built.append(HealthMetric(
            name=key, label=label,
            value=float(value) if value is not None else None,
            limit=float(cfg[limit_key]), lower_is_better=lower_better, unit=unit,
        ))

    breaches = [m for m in built if m.breached]
    warnings = [m for m in built
                if not m.breached and m.known and (m.utilisation_pct or 0) >= warn_at]

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
            f"{m.limit:g}{m.unit} limit. {TIKTOK_REMEDIES.get(m.name, '')}"
        )
    for m in warnings:
        actions.append(
            f"APPROACHING — {m.label} at {m.value:g}{m.unit} is "
            f"{m.utilisation_pct:.0f}% of the {m.limit:g}{m.unit} limit. "
            f"{TIKTOK_REMEDIES.get(m.name, '')}"
        )

    points = metrics.get("seller_violation_points")
    if points is not None:
        warn_points = float(cfg["warn_violation_points"])
        if float(points) >= warn_points:
            actions.append(
                f"{points:g} violation points accumulated (suspension review at "
                f"{cfg['max_seller_violation_points']:g}). Points decay after 90 "
                "days but stack until then, so a second violation now is far more "
                "expensive than the first was."
            )

    if unknown:
        actions.append(
            f"{len(unknown)} metric(s) not supplied: {', '.join(unknown)}. "
            "Reported as unknown, not healthy — TikTok suppresses reach before it "
            "suspends, so an unmeasured metric hides the warning as well as the "
            "breach."
        )

    if not breaches and not warnings and not unknown:
        actions.append("All TikTok Shop health metrics within safe range.")

    return HealthAssessment(
        marketplace="tiktok", metrics=built, severity=severity,
        breaches=breaches, warnings=warnings, unknown=unknown, actions=actions,
    )
