"""Supplier comparison, scorecard, and negotiation preparation.

Negotiation here means *preparing a defensible position* — target price, walk-away
point, and the leverage that justifies them. The operator does not send binding
commercial commitments on its own; it drafts, a human sends.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Policy
from .models import Supplier, money


@dataclass
class SupplierScore:
    supplier: Supplier
    landed_cost_score: float
    shipping_score: float
    quality_score: float
    communication_score: float
    stability_score: float
    total: float
    disqualified: bool
    disqualification_reasons: list[str]

    @property
    def name(self) -> str:
        return self.supplier.name


def _normalise_inverse(value: float, best: float, worst: float) -> float:
    """Lower is better (cost, days) -> 0-100 where best maps to 100."""
    if worst <= best:
        return 100.0
    return max(0.0, min(100.0, (worst - value) / (worst - best) * 100.0))


def score_suppliers(policy: Policy, suppliers: list[Supplier]) -> list[SupplierScore]:
    """Rank suppliers on the policy-weighted scorecard.

    Scores are relative to the quote set, so they only mean something when at
    least two real quotes exist — which the policy requires anyway.
    """
    if not suppliers:
        return []

    weights = policy.suppliers["weights"]
    min_rating = float(policy.selection["min_supplier_rating"])
    max_days = int(policy.selection["max_shipping_days"])

    costs = [s.landed_unit_cost for s in suppliers]
    days = [s.shipping_days for s in suppliers]
    best_cost, worst_cost = min(costs), max(costs)
    best_days, worst_days = min(days), max(days)

    scored: list[SupplierScore] = []
    for s in suppliers:
        reasons: list[str] = []
        if s.rating < min_rating:
            reasons.append(f"rating {s.rating:.2f} below policy minimum {min_rating:.2f}")
        if not s.domestic_stock and s.shipping_days >= max_days:
            reasons.append(f"transit {s.shipping_days}d at/over limit {max_days}d")

        cost_score = _normalise_inverse(s.landed_unit_cost, best_cost, worst_cost)
        ship_score = 100.0 if s.domestic_stock else _normalise_inverse(
            float(s.shipping_days), float(best_days), float(worst_days)
        )
        # Blend the declared quality score with observed defect history; a low
        # defect rate is evidence, a self-reported score is a claim.
        quality = s.quality_score * 0.6 + max(0.0, 100.0 - s.defect_rate_pct * 10) * 0.4
        stability = s.inventory_stability * 0.7 + s.on_time_rate_pct * 0.3

        total = (
            cost_score * float(weights["landed_cost"])
            + ship_score * float(weights["shipping_speed"])
            + quality * float(weights["quality"])
            + s.communication_score * float(weights["communication"])
            + stability * float(weights["inventory_stability"])
        )

        scored.append(
            SupplierScore(
                supplier=s,
                landed_cost_score=round(cost_score, 1),
                shipping_score=round(ship_score, 1),
                quality_score=round(quality, 1),
                communication_score=round(s.communication_score, 1),
                stability_score=round(stability, 1),
                total=round(total, 1),
                disqualified=bool(reasons),
                disqualification_reasons=reasons,
            )
        )

    scored.sort(key=lambda x: (x.disqualified, -x.total))
    return scored


def select_supplier(policy: Policy, suppliers: list[Supplier]) -> tuple[SupplierScore | None, list[str]]:
    """Pick the winner, or explain why we cannot pick one yet."""
    notes: list[str] = []
    min_quotes = int(policy.suppliers.get("min_quote_count", 2))

    if len(suppliers) < min_quotes:
        notes.append(
            f"Only {len(suppliers)} quote(s) available; policy requires {min_quotes} "
            "before selection. Holding — a single quote is a price, not a market."
        )
        return None, notes

    scored = score_suppliers(policy, suppliers)
    eligible = [s for s in scored if not s.disqualified]
    if not eligible:
        notes.append("Every quoted supplier failed a hard policy gate. No selection made.")
        for s in scored:
            notes.append(f"  - {s.name}: {'; '.join(s.disqualification_reasons)}")
        return None, notes

    winner = eligible[0]
    notes.append(f"Selected {winner.name} (score {winner.total}).")
    if len(eligible) > 1:
        runner_up = eligible[1]
        gap = round(winner.total - runner_up.total, 1)
        notes.append(f"Runner-up {runner_up.name} (score {runner_up.total}, gap {gap}).")
        if gap < 5.0:
            notes.append(
                "Gap under 5 points — effectively a tie. Recommend splitting the first "
                "order across both to build a second qualified source before scale."
            )
    if policy.suppliers.get("sample_required_before_first_po", True):
        notes.append("Policy: physical sample required before the first purchase order.")
    return winner, notes


@dataclass
class NegotiationBrief:
    supplier_name: str
    current_unit_cost: float
    target_unit_cost: float
    walk_away_cost: float
    leverage_points: list[str]
    asks: list[str]
    draft_message: str


def build_negotiation_brief(
    *,
    winner: SupplierScore,
    alternatives: list[SupplierScore],
    annual_volume_units: int,
    max_acceptable_cost: float,
) -> NegotiationBrief:
    """Prepare a negotiation position grounded in actual leverage.

    `max_acceptable_cost` should come from the economics layer — the unit cost
    above which the product stops clearing the ROI gate. Walking away is only
    credible if you know where the wall is.

    Only *eligible* alternatives count as leverage. Quoting a price from a
    supplier we have already disqualified is a bluff: if they call it, we have
    nowhere to go, and a supplier who catches you bluffing prices you worse next
    time.
    """
    s = winner.supplier
    current = s.unit_cost

    # Anchor on the best genuinely-available competing quote; otherwise ask for
    # a conventional volume break.
    # A quote only becomes leverage if it is both attainable (eligible) and
    # cheaper. Waving a more expensive alternative in front of a supplier argues
    # their case for them.
    competing = [
        a.supplier.unit_cost for a in alternatives
        if a.supplier.supplier_id != s.supplier_id
        and not a.disqualified
        and a.supplier.unit_cost < current
    ]
    if competing:
        best_rival = min(competing)
        target = money(min(current * 0.88, best_rival * 0.97))
    else:
        target = money(current * 0.90)
    target = max(target, 0.01)

    leverage: list[str] = []
    if competing:
        leverage.append(
            f"Competing quote at ${min(competing):.2f}/unit from a qualified alternate source."
        )
    if annual_volume_units > 0:
        leverage.append(
            f"Projected {annual_volume_units:,} units/year — worth "
            f"${current * annual_volume_units:,.0f} at current pricing."
        )
    if s.moq and annual_volume_units > s.moq * 4:
        leverage.append(
            f"Repeat-order cadence well above their {s.moq}-unit MOQ; predictable "
            "reorders reduce their planning cost."
        )
    if not leverage:
        leverage.append("Limited leverage: single quote, unproven volume. Expect little movement.")

    asks = [
        f"Unit price ${target:.2f} at {max(s.moq, 1)}+ units (from ${current:.2f}).",
        "Net-30 terms after two successful orders.",
        "Free or credited samples against the first production order.",
        "Tooling/setup fees amortised or waived at agreed annual volume.",
        "Written defect-rate guarantee with replacement-or-credit remedy.",
        "Inventory held for 30 days against a rolling forecast.",
    ]

    draft = (
        f"Hello {s.name} team,\n\n"
        "Thank you for the quote. We are ready to move forward and expect a recurring "
        f"programme of roughly {annual_volume_units:,} units per year.\n\n"
        f"To make the economics work on our side we need ${target:.2f}/unit at "
        f"{max(s.moq, 1)}+ units. "
        + (f"We do hold a comparable quote at ${min(competing):.2f}, but we would rather "
           "build one long-term relationship than chase the lowest price.\n\n"
           if competing else "\n")
        + "We would also like to discuss:\n"
        + "\n".join(f"  - {a}" for a in asks[1:])
        + "\n\nIf you can meet us on price we will place the opening order this week.\n\n"
        "Best regards"
    )

    return NegotiationBrief(
        supplier_name=s.name,
        current_unit_cost=money(current),
        target_unit_cost=target,
        walk_away_cost=money(max_acceptable_cost),
        leverage_points=leverage,
        asks=asks,
        draft_message=draft,
    )
