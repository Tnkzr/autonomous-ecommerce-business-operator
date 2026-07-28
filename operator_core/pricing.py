"""Competitive repricing with a hard profit floor.

The floor is not advisory. A repricer that can be dragged below cost by a
competitor with different economics is a machine for losing money quickly, so
the floor is computed from our own P&L and enforced last, after every other
rule has had its say.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Policy
from .economics import compute_unit_economics, price_for_target_margin
from .models import CompetitorOffer, Supplier, money


@dataclass
class PriceRecommendation:
    sku: str
    marketplace: str
    current_price: float
    recommended_price: float
    floor_price: float
    target_price: float
    change_pct: float
    rationale: list[str]
    requires_approval: bool
    projected_margin_pct: float
    projected_unit_profit: float
    action: str  # HOLD | RAISE | LOWER | BLOCKED_BY_FLOOR


def _margin_at(policy: Policy, marketplace: str, supplier: Supplier, price: float,
               duty_pct: float, ad_cost_per_unit: float):
    return compute_unit_economics(
        policy=policy,
        sku="_probe",
        marketplace=marketplace,
        sale_price=price,
        supplier=supplier,
        duty_pct=duty_pct,
        ad_cost_per_unit=ad_cost_per_unit,
    )


def recommend_price(
    *,
    policy: Policy,
    sku: str,
    marketplace: str,
    current_price: float,
    supplier: Supplier,
    competitors: list[CompetitorOffer],
    duty_pct: float = 0.0,
    ad_cost_per_unit: float = 0.0,
    hours_since_last_change: float = 999.0,
    our_rating: float = 4.5,
    our_review_count: int = 0,
) -> PriceRecommendation:
    """Recommend a price given the live competitive set.

    Strategy, in order of precedence:
      1. Never below the absolute floor margin. Non-negotiable.
      2. If we are materially stronger than rivals (rating + review moat), hold
         a premium rather than racing down — the Buy Box is not won on price alone.
      3. If a rival undercuts and we can still clear the floor, undercut back by
         the configured increment.
      4. If rivals sit above us and we are leaving money on the table, raise.
      5. Clamp every move to the daily change limit and the cooldown window.
    """
    pcfg = policy.pricing
    rationale: list[str] = []

    floor = price_for_target_margin(
        policy=policy,
        marketplace=marketplace,
        supplier=supplier,
        target_margin_pct=float(pcfg["absolute_floor_margin_pct"]),
        duty_pct=duty_pct,
        ad_pct_of_price=(ad_cost_per_unit / current_price * 100.0) if current_price > 0 else 0.0,
    )
    target = price_for_target_margin(
        policy=policy,
        marketplace=marketplace,
        supplier=supplier,
        target_margin_pct=float(pcfg["target_margin_pct"]),
        duty_pct=duty_pct,
        ad_pct_of_price=(ad_cost_per_unit / current_price * 100.0) if current_price > 0 else 0.0,
    )

    in_stock = [c for c in competitors if c.in_stock]
    proposed = current_price

    if not in_stock:
        rationale.append("No in-stock competitors found — pricing to target margin.")
        proposed = target
    else:
        lowest = min(in_stock, key=lambda c: c.price)
        rival_prices = sorted(c.price for c in in_stock)
        median_rival = rival_prices[len(rival_prices) // 2]
        rationale.append(
            f"{len(in_stock)} in-stock rivals: low ${lowest.price:.2f}, "
            f"median ${median_rival:.2f}, high ${rival_prices[-1]:.2f}."
        )

        strongest_rival_reviews = max((c.review_count for c in in_stock), default=0)
        best_rival_rating = max((c.rating for c in in_stock), default=0.0)
        we_are_stronger = (
            our_review_count >= strongest_rival_reviews * 1.2 and our_rating >= best_rival_rating
        )

        if lowest.price < floor:
            rationale.append(
                f"Lowest rival ${lowest.price:.2f} sits below our floor ${floor:.2f}. "
                "Not following — their cost base is not ours, and matching would sell "
                "at a loss. Competing on content and reviews instead."
            )
            proposed = max(floor, min(current_price, median_rival))
        elif we_are_stronger:
            premium = money(median_rival * 1.03)
            rationale.append(
                f"Our rating {our_rating:.1f}/{our_review_count} reviews beats the field "
                f"({best_rival_rating:.1f}/{strongest_rival_reviews}). Holding a ~3% premium "
                "rather than discounting into strength."
            )
            proposed = max(premium, floor)
        elif lowest.price < current_price:
            undercut = money(lowest.price - float(pcfg["undercut_amount"]))
            if undercut >= floor:
                rationale.append(
                    f"Undercutting lowest rival by ${float(pcfg['undercut_amount']):.2f} "
                    f"to ${undercut:.2f}; still ${undercut - floor:.2f} above floor."
                )
                proposed = undercut
            else:
                rationale.append(
                    f"Cannot undercut ${lowest.price:.2f} without breaching floor "
                    f"${floor:.2f}. Holding at floor."
                )
                proposed = floor
        elif current_price < median_rival * 0.95:
            rationale.append(
                f"We are {(1 - current_price / median_rival) * 100:.1f}% under the median "
                "rival with no need to be — recovering margin."
            )
            proposed = min(money(median_rival * 0.98), max(target, current_price))
        else:
            rationale.append("Priced competitively within the field — no change indicated.")
            proposed = current_price

    # --- guard rails -----------------------------------------------------
    if not bool(pcfg.get("allow_loss_leader", False)):
        if proposed < floor:
            rationale.append(f"Clamped up to floor ${floor:.2f} (loss leaders disabled).")
            proposed = floor

    max_move = float(pcfg["max_daily_price_change_pct"]) / 100.0
    if current_price > 0:
        upper = current_price * (1 + max_move)
        lower = current_price * (1 - max_move)
        if proposed > upper:
            rationale.append(
                f"Clamped to +{float(pcfg['max_daily_price_change_pct']):.0f}% daily move limit."
            )
            proposed = upper
        elif proposed < lower:
            rationale.append(
                f"Clamped to -{float(pcfg['max_daily_price_change_pct']):.0f}% daily move limit."
            )
            proposed = lower
    proposed = money(max(proposed, floor))

    cooldown = float(pcfg["min_hours_between_changes"])
    if hours_since_last_change < cooldown and abs(proposed - current_price) > 0.001:
        rationale.append(
            f"Cooldown active ({hours_since_last_change:.0f}h of {cooldown:.0f}h). "
            "Deferring the change to avoid whipsawing the listing."
        )
        proposed = current_price

    change_pct = round((proposed - current_price) / current_price * 100, 2) if current_price else 0.0
    econ = _margin_at(policy, marketplace, supplier, proposed, duty_pct, ad_cost_per_unit)

    if abs(change_pct) < 0.01:
        action = "HOLD"
    elif proposed <= floor + 0.001 and change_pct < 0:
        action = "BLOCKED_BY_FLOOR"
    else:
        action = "RAISE" if change_pct > 0 else "LOWER"

    approval_pct = float(policy.approval_threshold("price_change_pct") or 100.0)
    requires_approval = abs(change_pct) >= approval_pct
    if requires_approval:
        rationale.append(
            f"Change of {change_pct:+.1f}% meets the {approval_pct:.0f}% approval "
            "threshold — queued for human sign-off, not applied."
        )

    return PriceRecommendation(
        sku=sku,
        marketplace=marketplace,
        current_price=money(current_price),
        recommended_price=proposed,
        floor_price=floor,
        target_price=target,
        change_pct=change_pct,
        rationale=rationale,
        requires_approval=requires_approval,
        projected_margin_pct=econ.margin_pct,
        projected_unit_profit=econ.net_profit,
        action=action,
    )
