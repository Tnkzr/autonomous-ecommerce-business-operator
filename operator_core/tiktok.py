"""TikTok Shop business logic: profit, trends, and the daily optimisation pass.

TikTok differs from Amazon in three ways that change the arithmetic, and each
one is handled explicitly here rather than being folded into the generic model:

1. **Realised revenue ≠ order value.** Commission, transaction fees, affiliate
   payouts, and seller-funded promotions come out between the two, typically
   15-25%. Profit is computed from settlements where available.

2. **Returns run much higher.** Impulse purchases from a video convert well and
   come back often. The policy default is 9% against Amazon's 5%.

3. **Demand is spiky and decays.** A product carried by one video can do a
   month of volume in three days and then stop. Trend analysis therefore looks
   at the *shape* of the curve, not just its level — a decaying spike and
   steady growth look identical in a 30-day total and call for opposite
   inventory decisions.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .config import Policy
from .economics import after_tax_profit
from .models import money


# Plain string constants rather than an Enum: these are compared and printed as
# strings throughout, and the indirection bought nothing.
TREND_GROWING = "GROWING"
TREND_STEADY = "STEADY"
TREND_DECAYING = "DECAYING"
TREND_SPIKE_DECAY = "SPIKE_DECAY"
TREND_DEAD = "DEAD"
TREND_INSUFFICIENT = "INSUFFICIENT_DATA"


@dataclass
class DailyPoint:
    """One day of a product's performance."""

    day: str
    units: int
    gmv: float
    page_views: int = 0
    orders: int = 0


@dataclass
class TrendAnalysis:
    product_id: str
    title: str
    shape: str
    recent_daily_units: float
    prior_daily_units: float
    change_pct: float
    peak_day: str
    days_since_peak: int
    volatility: float
    conversion_rate_pct: float
    interpretation: str
    inventory_guidance: str
    confidence_note: str


@dataclass
class TikTokProfit:
    """Per-product profit on TikTok, from realised or estimated revenue."""

    product_id: str
    units: int
    gross_revenue: float
    platform_fees: float
    affiliate_commission: float
    seller_promotions: float
    net_revenue: float
    cogs: float
    shipping_cost: float
    return_cost: float
    ad_cost: float
    pre_tax_profit: float
    after_tax_profit: float
    margin_pct: float
    revenue_basis: str          # "settlement" | "estimated"

    @property
    def take_rate_pct(self) -> float:
        if self.gross_revenue <= 0:
            return 0.0
        deducted = self.platform_fees + self.affiliate_commission + self.seller_promotions
        return round(deducted / self.gross_revenue * 100, 2)


# ---------------------------------------------------------------------------
# Profit
# ---------------------------------------------------------------------------
def compute_profit(
    *,
    policy: Policy,
    product_id: str,
    units: int,
    gross_revenue: float,
    cogs_per_unit: float,
    shipping_per_unit: float = 0.0,
    ad_cost: float = 0.0,
    settlement: dict[str, float] | None = None,
    affiliate_rate_pct: float | None = None,
    seller_promotion_pct: float = 0.0,
) -> TikTokProfit:
    """Compute profit, preferring settlement data over fee estimates.

    When a settlement is supplied its numbers win outright. Estimated fees are
    a planning tool; settlements are what actually landed in the account, and
    on TikTok the gap between them is wide enough to flip a product from
    profitable to not.
    """
    fees_cfg = policy.fees_for("tiktok")

    if settlement:
        net_revenue = float(settlement.get("revenue_amount", 0.0))
        platform_fees = abs(float(settlement.get("fee_amount", 0.0)))
        affiliate = abs(float(settlement.get("affiliate_commission", 0.0)))
        promotions = abs(float(settlement.get("seller_promotion", 0.0)))
        basis = "settlement"
    else:
        commission = gross_revenue * float(fees_cfg.get("referral_pct", 0.0)) / 100.0
        payment = gross_revenue * float(fees_cfg.get("payment_pct", 0.0)) / 100.0
        platform_fees = commission + payment
        # Affiliate is optional per product; it only applies if the seller
        # enrolled the product in the creator programme.
        rate = (affiliate_rate_pct
                if affiliate_rate_pct is not None
                else float(fees_cfg.get("affiliate_commission_pct", 0.0)))
        affiliate = gross_revenue * rate / 100.0
        promotions = gross_revenue * seller_promotion_pct / 100.0
        net_revenue = gross_revenue - platform_fees - affiliate - promotions
        basis = "estimated"

    cogs = cogs_per_unit * units
    shipping = shipping_per_unit * units

    return_rate = float(fees_cfg.get("expected_return_rate_pct", 0.0)) / 100.0
    fulfilment_flat = float(fees_cfg.get("fulfillment_flat", 0.0))
    # A return costs the outbound fulfilment plus roughly half the goods, same
    # model as the core economics module.
    return_cost = units * return_rate * (fulfilment_flat + cogs_per_unit * 0.5)

    pre_tax = net_revenue - cogs - shipping - return_cost - ad_cost
    post_tax = after_tax_profit(policy, pre_tax)

    return TikTokProfit(
        product_id=product_id,
        units=units,
        gross_revenue=money(gross_revenue),
        platform_fees=money(platform_fees),
        affiliate_commission=money(affiliate),
        seller_promotions=money(promotions),
        net_revenue=money(net_revenue),
        cogs=money(cogs),
        shipping_cost=money(shipping),
        return_cost=money(return_cost),
        ad_cost=money(ad_cost),
        pre_tax_profit=money(pre_tax),
        after_tax_profit=money(post_tax),
        margin_pct=round(pre_tax / gross_revenue * 100, 2) if gross_revenue else 0.0,
        revenue_basis=basis,
    )


# ---------------------------------------------------------------------------
# Trends
# ---------------------------------------------------------------------------
def analyse_trend(
    *,
    product_id: str,
    title: str,
    history: list[DailyPoint],
    min_days: int = 14,
) -> TrendAnalysis:
    """Classify a product's demand curve.

    The distinction that matters on TikTok is between *growing* and
    *spike-then-decay*. Both show strong 30-day totals. One justifies a reorder;
    the other means the video that drove it has stopped circulating and the
    inventory you buy will sit. Telling them apart requires the shape, which is
    why a 30-day sum is not enough.
    """
    if len(history) < min_days:
        return TrendAnalysis(
            product_id=product_id, title=title, shape=TREND_INSUFFICIENT,
            recent_daily_units=0.0, prior_daily_units=0.0, change_pct=0.0,
            peak_day="", days_since_peak=0, volatility=0.0, conversion_rate_pct=0.0,
            interpretation=(
                f"Only {len(history)} days of history against a {min_days}-day "
                "minimum. Not enough to tell growth from a decaying spike, and those "
                "call for opposite inventory decisions."
            ),
            inventory_guidance="Hold. Do not reorder on a curve you cannot yet see.",
            confidence_note="Insufficient data.",
        )

    ordered = sorted(history, key=lambda p: p.day)
    half = len(ordered) // 2
    prior, recent = ordered[:half], ordered[half:]

    prior_daily = statistics.mean(p.units for p in prior) if prior else 0.0
    recent_daily = statistics.mean(p.units for p in recent) if recent else 0.0
    change = ((recent_daily - prior_daily) / prior_daily * 100) if prior_daily else (
        100.0 if recent_daily else 0.0)

    peak = max(ordered, key=lambda p: p.units)
    days_since_peak = (
        date.fromisoformat(ordered[-1].day).toordinal()
        - date.fromisoformat(peak.day).toordinal()
    ) if peak.day and ordered[-1].day else 0

    units_series = [p.units for p in ordered]
    mean_units = statistics.mean(units_series) if units_series else 0.0
    volatility = (statistics.pstdev(units_series) / mean_units) if mean_units else 0.0

    total_views = sum(p.page_views for p in ordered)
    total_orders = sum(p.orders for p in ordered)
    conversion = (total_orders / total_views * 100) if total_views else 0.0

    # Classification. Order matters: a decaying spike must be caught before the
    # 30-day total lets it masquerade as healthy demand.
    tail = ordered[-3:]
    tail_daily = statistics.mean(p.units for p in tail) if tail else 0.0

    if mean_units <= 0:
        shape = TREND_DEAD
        interpretation = "No sales in the window at all."
        guidance = ("Do not reorder. This is a demand or visibility problem; buying "
                    "more units cannot fix either.")
    elif peak.units > mean_units * 3 and days_since_peak >= 5 and tail_daily < peak.units * 0.25:
        shape = TREND_SPIKE_DECAY
        interpretation = (
            f"Spike then decay: peaked at {peak.units} units on {peak.day}, "
            f"{days_since_peak} days ago, now running {tail_daily:.1f}/day. "
            "The 30-day total looks healthy but the curve has already rolled over — "
            "this is the pattern that fills warehouses."
        )
        guidance = (
            "Do not extrapolate the peak. Size any reorder to the current tail rate, "
            "not the window average, and only if the tail itself is profitable."
        )
    elif change >= 25 and tail_daily >= recent_daily * 0.8:
        shape = TREND_GROWING
        interpretation = (
            f"Growing: {prior_daily:.1f} -> {recent_daily:.1f} units/day "
            f"({change:+.0f}%), and the last three days are holding."
        )
        guidance = (
            "Reorder is justified. Confirm the growth is not a single creator's "
            "video before scaling the order size — creator-driven demand ends when "
            "the creator moves on."
        )
    elif change <= -25:
        shape = TREND_DECAYING
        interpretation = (
            f"Decaying: {prior_daily:.1f} -> {recent_daily:.1f} units/day ({change:+.0f}%)."
        )
        guidance = ("Do not reorder yet. Establish whether this is seasonality, lost "
                    "placement, or genuine demand loss before committing capital.")
    else:
        shape = TREND_STEADY
        interpretation = (
            f"Steady at roughly {recent_daily:.1f} units/day ({change:+.0f}% vs prior)."
        )
        guidance = "Normal replenishment applies."

    confidence = f"{len(ordered)} days of history."
    if volatility > 1.0:
        confidence += (
            f" Volatility is high (coefficient of variation {volatility:.2f}) — daily "
            "demand swings more than its own average, so any forecast from it is wide. "
            "Carry more safety stock than the number alone suggests."
        )

    return TrendAnalysis(
        product_id=product_id, title=title, shape=shape,
        recent_daily_units=round(recent_daily, 2),
        prior_daily_units=round(prior_daily, 2),
        change_pct=round(change, 1),
        peak_day=peak.day, days_since_peak=days_since_peak,
        volatility=round(volatility, 2),
        conversion_rate_pct=round(conversion, 2),
        interpretation=interpretation,
        inventory_guidance=guidance,
        confidence_note=confidence,
    )


# ---------------------------------------------------------------------------
# Daily optimisation
# ---------------------------------------------------------------------------
@dataclass
class OptimisationAction:
    product_id: str
    title: str
    priority: str          # CRITICAL | HIGH | MEDIUM | LOW
    action: str
    rationale: str
    requires_approval: bool = False
    estimated_impact_usd: float = 0.0


def build_optimisation_report(
    *,
    policy: Policy,
    trends: list[TrendAnalysis],
    profits: dict[str, TikTokProfit],
    inventory: list[dict[str, Any]],
    take_rate_pct: float | None = None,
) -> tuple[list[OptimisationAction], list[str]]:
    """Turn the day's TikTok data into ranked actions.

    Ranking is by what it costs to ignore, not by how easy it is to do.
    """
    actions: list[OptimisationAction] = []
    notes: list[str] = []

    inv_by_product = {str(i.get("product_id")): i for i in inventory}
    approval_at = float(policy.approval_threshold("purchase_order_usd") or 0.0)

    # Fee drift first: it silently invalidates every other number below.
    if take_rate_pct is not None:
        fees = policy.fees_for("tiktok")
        baseline = (float(fees.get("referral_pct", 0.0))
                    + float(fees.get("payment_pct", 0.0))
                    + float(fees.get("affiliate_commission_pct", 0.0)))
        drift = take_rate_pct - baseline
        # Products enrolled in the creator programme carry their own affiliate
        # rate, which the shop-wide baseline does not include. Saying so keeps
        # the comparison honest rather than implying the whole gap is drift.
        enrolled = sorted({
            p.product_id for p in profits.values() if p.affiliate_commission > 0
        })
        if abs(drift) > 3.0:
            note = (
                f"Settled take rate is {take_rate_pct:.1f}% against a "
                f"{baseline:.1f}% policy baseline ({drift:+.1f}pp). Every margin, "
                "price floor, and break-even below is computed from the baseline."
            )
            if enrolled:
                note += (
                    f" Part of that gap is per-product affiliate commission on "
                    f"{len(enrolled)} enrolled product(s), which the shop-wide "
                    "baseline excludes by design — compare per product before "
                    "concluding the whole gap is drift."
                )
            else:
                note += " Update [fees.tiktok] before acting on any of them."
            notes.append(note)

    for t in trends:
        p = profits.get(t.product_id)
        inv = inv_by_product.get(t.product_id, {})
        stock = int(inv.get("on_hand_units", 0) or 0)

        # Losing money is the most expensive thing to leave running.
        if p and p.pre_tax_profit < 0:
            actions.append(OptimisationAction(
                product_id=t.product_id, title=t.title, priority="CRITICAL",
                action="Stop promoting and reprice or delist",
                rationale=(
                    f"Losing ${abs(p.pre_tax_profit):,.2f} across {p.units} units "
                    f"({p.margin_pct:.1f}% margin, {p.take_rate_pct:.1f}% platform take). "
                    "Volume makes this worse, not better."
                ),
                estimated_impact_usd=abs(p.pre_tax_profit),
                requires_approval=True,
            ))
            continue

        if t.shape == TREND_SPIKE_DECAY:
            actions.append(OptimisationAction(
                product_id=t.product_id, title=t.title, priority="HIGH",
                action="Do not reorder on the peak; size to the current tail",
                rationale=t.interpretation,
            ))
            if stock > t.recent_daily_units * 90 and t.recent_daily_units > 0:
                actions.append(OptimisationAction(
                    product_id=t.product_id, title=t.title, priority="HIGH",
                    action="Discount or bundle to clear ageing stock",
                    rationale=(
                        f"{stock} units against {t.recent_daily_units:.1f}/day is over "
                        "90 days of cover on a product whose demand has already rolled "
                        "over. That capital is not coming back on its own."
                    ),
                ))

        elif t.shape == TREND_GROWING:
            days_cover = (stock / t.recent_daily_units) if t.recent_daily_units else 999
            if days_cover < 21:
                impact = (p.pre_tax_profit / max(p.units, 1) * t.recent_daily_units * 30
                          if p else 0.0)
                actions.append(OptimisationAction(
                    product_id=t.product_id, title=t.title, priority="CRITICAL",
                    action=f"Reorder — only {days_cover:.0f} days of cover on a growing product",
                    rationale=(
                        f"{t.interpretation} A stockout here loses the placement that "
                        "the growth is built on, and TikTok suppresses out-of-stock "
                        "products from the feed."
                    ),
                    estimated_impact_usd=round(impact, 2),
                    requires_approval=True,
                ))
            else:
                actions.append(OptimisationAction(
                    product_id=t.product_id, title=t.title, priority="MEDIUM",
                    action="Scale creator/affiliate promotion",
                    rationale=(
                        f"{t.interpretation} Cover is adequate at {days_cover:.0f} days, "
                        "so growth can be pushed without a stockout risk."
                    ),
                ))

        elif t.shape == TREND_DEAD:
            actions.append(OptimisationAction(
                product_id=t.product_id, title=t.title, priority="MEDIUM",
                action="Diagnose visibility before writing off",
                rationale=(
                    f"No sales in the window with {stock} units on hand. "
                    "Check listing status first — TikTok deactivates on policy review "
                    "as well as on seller action, and a deactivated listing looks "
                    "exactly like dead demand."
                ),
            ))

        elif t.shape == TREND_DECAYING and stock > 0:
            actions.append(OptimisationAction(
                product_id=t.product_id, title=t.title, priority="MEDIUM",
                action="Hold reorder; investigate cause",
                rationale=t.interpretation,
            ))

        if t.volatility > 1.0 and t.shape in (TREND_GROWING, TREND_STEADY):
            notes.append(
                f"{t.title}: demand swings more than its own average "
                f"(CV {t.volatility:.2f}). Reorder points computed from mean velocity "
                "will understate the safety stock this product needs."
            )

        if 0 < t.conversion_rate_pct < 1.0:
            actions.append(OptimisationAction(
                product_id=t.product_id, title=t.title, priority="MEDIUM",
                action="Fix the listing, not the traffic",
                rationale=(
                    f"{t.conversion_rate_pct:.2f}% conversion — traffic arrives and does "
                    "not buy. More promotion multiplies the leak rather than the sales."
                ),
            ))

    for a in actions:
        if a.estimated_impact_usd >= approval_at and approval_at > 0:
            a.requires_approval = True

    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    actions.sort(key=lambda a: (order.get(a.priority, 9), -a.estimated_impact_usd))
    return actions, notes


def daily_points_from_performance(rows: list[dict[str, Any]]) -> dict[str, list[DailyPoint]]:
    """Group per-day analytics rows by product."""
    grouped: dict[str, list[DailyPoint]] = {}
    for r in rows:
        pid = str(r.get("product_id", ""))
        if not pid:
            continue
        grouped.setdefault(pid, []).append(DailyPoint(
            day=str(r.get("day", "")),
            units=int(r.get("units_sold", 0) or 0),
            gmv=float(r.get("gmv", 0.0) or 0.0),
            page_views=int(r.get("page_views", 0) or 0),
            orders=int(r.get("orders", 0) or 0),
        ))
    return grouped
