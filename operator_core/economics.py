"""Unit economics: the arithmetic every other decision rests on.

Deliberately boring and deterministic. If this module is wrong, everything
downstream is confidently wrong, so it is fully covered by tests and it never
guesses: an unknown fee raises rather than defaults to zero.
"""

from __future__ import annotations

from .config import Policy
from .models import ProductCandidate, Supplier, UnitEconomics, money


def compute_unit_economics(
    *,
    policy: Policy,
    sku: str,
    marketplace: str,
    sale_price: float,
    supplier: Supplier,
    duty_pct: float = 0.0,
    ad_cost_per_unit: float = 0.0,
    misc_cost: float = 0.0,
    units_for_amortisation: int = 0,
    months_of_storage: float = 1.0,
) -> UnitEconomics:
    """Build a full per-unit P&L.

    `return_cost` models the expected cost of returns, not a per-order refund:
    at a 5% return rate we lose the fulfillment fee and a refund-admin slice on
    that 5%, plus the goods when unsellable. Ignoring this is the most common
    way a "40% ROI" product turns out to make nothing.
    """
    fees = policy.fees_for(marketplace)

    landed = supplier.landed_unit_cost
    if supplier.tooling_fee and units_for_amortisation > 0:
        landed = money(landed + supplier.tooling_fee / units_for_amortisation)

    duty = money(landed * (duty_pct / 100.0))

    referral = money(sale_price * (float(fees.get("referral_pct", 0.0)) / 100.0))
    payment = money(
        sale_price * (float(fees.get("payment_pct", 0.0)) / 100.0)
        + float(fees.get("payment_flat", 0.0))
    )
    fulfillment = money(float(fees.get("fulfillment_flat", 0.0)) + float(fees.get("closing_fee", 0.0)))
    storage = money(float(fees.get("storage_per_unit_month", 0.0)) * months_of_storage)

    return_rate = float(fees.get("expected_return_rate_pct", 0.0)) / 100.0
    refund_admin_pct = float(fees.get("refund_admin_pct", 0.0)) / 100.0
    # Per returned unit we eat: outbound fulfillment (unrecoverable), refund
    # admin, and half the goods value (industry-typical unsellable share).
    cost_per_return = fulfillment + (sale_price * refund_admin_pct) + (landed + duty) * 0.5
    return_cost = money(cost_per_return * return_rate)

    return UnitEconomics(
        sku=sku,
        marketplace=marketplace,
        sale_price=money(sale_price),
        landed_cost=landed,
        duty=duty,
        referral_fee=referral,
        fulfillment_fee=fulfillment,
        payment_fee=payment,
        storage_fee=storage,
        return_cost=return_cost,
        ad_cost_per_unit=money(ad_cost_per_unit),
        misc_cost=money(misc_cost),
    )


def economics_for_candidate(
    policy: Policy,
    candidate: ProductCandidate,
    *,
    sale_price: float | None = None,
    ad_cost_per_unit: float | None = None,
) -> UnitEconomics:
    """Convenience wrapper: price a candidate at its target price.

    If no ad cost is supplied we assume a launch-phase spend of 10% of price.
    Assuming zero ad cost would flatter every candidate; new listings do not
    sell without traffic.
    """
    price = candidate.target_price if sale_price is None else sale_price
    ads = price * 0.10 if ad_cost_per_unit is None else ad_cost_per_unit
    return compute_unit_economics(
        policy=policy,
        sku=candidate.sku,
        marketplace=candidate.marketplace,
        sale_price=price,
        supplier=candidate.supplier,
        duty_pct=candidate.duty_pct,
        ad_cost_per_unit=ads,
        units_for_amortisation=candidate.supplier.moq,
    )


def price_for_target_margin(
    *,
    policy: Policy,
    marketplace: str,
    supplier: Supplier,
    target_margin_pct: float,
    duty_pct: float = 0.0,
    ad_pct_of_price: float = 10.0,
) -> float:
    """Solve for the price that yields a target margin.

    Percentage fees and ad spend scale with price, so this is solved
    algebraically rather than by iteration:

        P - (fixed + P*variable_rate) = P * margin
        P = fixed / (1 - variable_rate - margin)
    """
    fees = policy.fees_for(marketplace)

    landed = supplier.landed_unit_cost
    duty = landed * (duty_pct / 100.0)
    fulfillment = float(fees.get("fulfillment_flat", 0.0)) + float(fees.get("closing_fee", 0.0))
    storage = float(fees.get("storage_per_unit_month", 0.0))
    payment_flat = float(fees.get("payment_flat", 0.0))

    return_rate = float(fees.get("expected_return_rate_pct", 0.0)) / 100.0
    refund_admin_pct = float(fees.get("refund_admin_pct", 0.0)) / 100.0

    fixed = (
        landed + duty + fulfillment + storage + payment_flat
        + return_rate * (fulfillment + (landed + duty) * 0.5)
    )
    variable = (
        float(fees.get("referral_pct", 0.0)) / 100.0
        + float(fees.get("payment_pct", 0.0)) / 100.0
        + ad_pct_of_price / 100.0
        + return_rate * refund_admin_pct
    )

    denominator = 1.0 - variable - (target_margin_pct / 100.0)
    if denominator <= 0:
        raise ValueError(
            f"Target margin {target_margin_pct}% is unreachable on {marketplace}: "
            f"variable costs alone consume {variable * 100:.1f}% of every sale. "
            "No price satisfies this — the product or the target must change."
        )
    return money(fixed / denominator)


def break_even_price(
    *, policy: Policy, marketplace: str, supplier: Supplier, duty_pct: float = 0.0,
    ad_pct_of_price: float = 0.0,
) -> float:
    """The price at which this product makes exactly zero. Never sell below it."""
    return price_for_target_margin(
        policy=policy,
        marketplace=marketplace,
        supplier=supplier,
        target_margin_pct=0.0,
        duty_pct=duty_pct,
        ad_pct_of_price=ad_pct_of_price,
    )


def cash_cycle_days(supplier: Supplier, *, payment_terms_days: int = 0,
                    sell_through_days: int = 45) -> int:
    """How long a dollar is trapped before it comes back.

    Two products with identical ROI are not equal: the one that recycles cash
    in 60 days compounds roughly twice as fast as the one that takes 120.
    """
    return max(0, supplier.shipping_days + sell_through_days - payment_terms_days)


def annualised_roi_pct(unit: UnitEconomics, supplier: Supplier,
                       *, sell_through_days: int = 45) -> float:
    """ROI adjusted for how often the capital turns over in a year."""
    cycle = cash_cycle_days(supplier, sell_through_days=sell_through_days)
    if cycle <= 0:
        return unit.roi_pct
    turns = 365.0 / cycle
    return round(unit.roi_pct * turns, 2)
