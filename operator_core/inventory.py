"""Inventory: stockout prediction, safety stock, and reorder recommendations.

Uses the standard reorder-point model with demand *and* lead-time variance.
Lead-time variance is the term most small operators drop, and it is usually the
one that causes the stockout: a 30-day supplier that occasionally takes 45 will
empty the shelf even when average demand was forecast perfectly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Policy
from .models import InventoryItem, money


@dataclass
class InventoryPlan:
    sku: str
    marketplace: str
    on_hand_units: int
    inbound_units: int
    daily_velocity: float
    days_of_cover: float
    stockout_date_days: float | None
    safety_stock_units: int
    reorder_point_units: int
    recommended_order_units: int
    estimated_order_cost: float
    status: str  # HEALTHY | REORDER_NOW | CRITICAL | OVERSTOCK | STALLED
    urgency: int  # 0-100
    rationale: list[str]
    requires_approval: bool = False


def safety_stock_units(item: InventoryItem, policy: Policy) -> int:
    """Safety stock covering both demand and lead-time uncertainty.

        SS = z * sqrt( LT * sigma_d^2  +  d^2 * sigma_LT^2 )
    """
    cfg = policy.inventory
    z = float(cfg["service_level_z"])
    lt = float(item.lead_time_days or cfg["default_lead_time_days"])
    sigma_d = float(item.velocity_stddev)
    sigma_lt = float(cfg["lead_time_variance_days"])
    d = float(item.daily_velocity)

    variance = lt * (sigma_d ** 2) + (d ** 2) * (sigma_lt ** 2)
    return int(math.ceil(z * math.sqrt(max(variance, 0.0))))


def plan_item(policy: Policy, item: InventoryItem) -> InventoryPlan:
    cfg = policy.inventory
    rationale: list[str] = []

    available = item.on_hand_units + item.inbound_units
    velocity = max(item.daily_velocity, 0.0)

    if velocity <= 0:
        # No sales is a merchandising problem, not a replenishment one.
        return InventoryPlan(
            sku=item.sku,
            marketplace=item.marketplace,
            on_hand_units=item.on_hand_units,
            inbound_units=item.inbound_units,
            daily_velocity=0.0,
            days_of_cover=float("inf"),
            stockout_date_days=None,
            safety_stock_units=0,
            reorder_point_units=0,
            recommended_order_units=0,
            estimated_order_cost=0.0,
            status="STALLED",
            urgency=0,
            rationale=[
                "Zero trailing velocity. Do not reorder. This is a demand problem — "
                "check listing visibility, price, and ad delivery before buying more units."
            ],
        )

    days_cover = round(available / velocity, 1)
    stockout_days = round(item.on_hand_units / velocity, 1)

    ss = safety_stock_units(item, policy)
    lt = int(item.lead_time_days or cfg["default_lead_time_days"])
    review_days = int(cfg["review_period_days"])
    reorder_point = int(math.ceil(velocity * (lt + review_days) + ss))

    rationale.append(
        f"Velocity {velocity:.2f} u/day; {item.on_hand_units} on hand, "
        f"{item.inbound_units} inbound = {days_cover:.0f} days of cover."
    )
    rationale.append(
        f"Safety stock {ss} u covers {lt}d lead time (±{cfg['lead_time_variance_days']}d) "
        f"at {float(cfg['service_level_z'])} z-score."
    )

    target_days = int(cfg["target_days_of_cover"])
    critical_days = int(cfg["critical_days_of_cover"])
    overstock_days = int(cfg["overstock_days_of_cover"])

    order_units = 0
    if days_cover > overstock_days:
        status = "OVERSTOCK"
        urgency = 10
        rationale.append(
            f"{days_cover:.0f} days of cover exceeds the {overstock_days}-day overstock "
            "line. Capital is sitting idle and storage fees accrue — promote, bundle, "
            "or discount before ordering anything further."
        )
    elif available <= reorder_point:
        # Order back up to target cover, netting off what is already inbound.
        desired = math.ceil(velocity * (target_days + lt) + ss)
        order_units = max(0, desired - available)
        order_units = max(order_units, int(item.moq or cfg["min_order_quantity_default"]))

        if stockout_days <= lt:
            status = "CRITICAL"
            urgency = 95
            rationale.append(
                f"Projected stockout in {stockout_days:.0f} days, inside the {lt}-day "
                "lead time. A stockout here also costs ranking and Buy Box share, "
                "which is more expensive than the lost units. Order immediately or "
                "arrange air freight on a partial quantity."
            )
        else:
            status = "REORDER_NOW"
            urgency = 70
            rationale.append(
                f"Available {available} u is at/below the {reorder_point} u reorder point. "
                f"Order now to land stock before cover runs out in {days_cover:.0f} days."
            )
    elif days_cover < critical_days:
        status = "CRITICAL"
        urgency = 90
        order_units = max(
            int(math.ceil(velocity * (target_days + lt) + ss)) - available,
            int(item.moq or cfg["min_order_quantity_default"]),
        )
        rationale.append(
            f"Cover {days_cover:.0f}d is under the {critical_days}d critical line."
        )
    else:
        status = "HEALTHY"
        urgency = max(0, int(100 - (days_cover / max(target_days, 1)) * 50))
        days_until_reorder = round((available - reorder_point) / velocity, 1)
        rationale.append(
            f"Healthy. Reorder point ({reorder_point} u) reached in ~{days_until_reorder:.0f} days."
        )

    cost = money(order_units * item.unit_cost)
    approval_at = float(policy.approval_threshold("purchase_order_usd") or 0.0)
    max_po = float(policy.risk["max_single_po_usd"])

    requires_approval = order_units > 0 and cost >= approval_at
    if order_units > 0:
        if cost > max_po:
            rationale.append(
                f"Order value ${cost:,.2f} exceeds the ${max_po:,.2f} single-PO cap. "
                "Split into staged orders or raise the cap deliberately — the operator "
                "will not place it."
            )
        elif requires_approval:
            rationale.append(
                f"Order value ${cost:,.2f} meets the ${approval_at:,.2f} approval "
                "threshold — queued for sign-off, not placed."
            )

    return InventoryPlan(
        sku=item.sku,
        marketplace=item.marketplace,
        on_hand_units=item.on_hand_units,
        inbound_units=item.inbound_units,
        daily_velocity=round(velocity, 2),
        days_of_cover=days_cover,
        stockout_date_days=stockout_days,
        safety_stock_units=ss,
        reorder_point_units=reorder_point,
        recommended_order_units=int(order_units),
        estimated_order_cost=cost,
        status=status,
        urgency=urgency,
        rationale=rationale,
        requires_approval=requires_approval,
    )


def plan_all(policy: Policy, items: list[InventoryItem]) -> list[InventoryPlan]:
    plans = [plan_item(policy, i) for i in items]
    plans.sort(key=lambda p: -p.urgency)
    return plans


def portfolio_health(plans: list[InventoryPlan]) -> dict[str, float | int]:
    """Roll-up used by the daily report."""
    if not plans:
        return {"skus": 0, "health_score": 0.0}

    counts = {"HEALTHY": 0, "REORDER_NOW": 0, "CRITICAL": 0, "OVERSTOCK": 0, "STALLED": 0}
    for p in plans:
        counts[p.status] = counts.get(p.status, 0) + 1

    n = len(plans)
    # Weighted so critical/stalled SKUs drag the score hard — they should.
    score = (
        counts["HEALTHY"] * 100
        + counts["REORDER_NOW"] * 65
        + counts["OVERSTOCK"] * 45
        + counts["CRITICAL"] * 10
        + counts["STALLED"] * 20
    ) / n

    return {
        "skus": n,
        "healthy": counts["HEALTHY"],
        "reorder_now": counts["REORDER_NOW"],
        "critical": counts["CRITICAL"],
        "overstock": counts["OVERSTOCK"],
        "stalled": counts["STALLED"],
        "health_score": round(score, 1),
        "capital_to_deploy": money(sum(p.estimated_order_cost for p in plans)),
    }
