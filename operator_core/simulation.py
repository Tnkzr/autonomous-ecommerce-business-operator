"""Cash-flow simulation. Explicitly not a forecast.

This module exists because "what happens if I start with $200" is a reasonable
question that the rest of the system refuses to answer — and refusing is
correct, because the honest answer needs demand data the operator does not
have. A simulation is the legitimate middle: run the *real* engines (economics,
screening, risk, the fee model) against *stated* assumptions, and report what
the system decides.

Three rules keep this from becoming the fabrication the charter forbids.

**Every number that is not derived is a named assumption with a stated source.**
`Assumptions` carries them, `report()` prints them all, and none of them are
hidden inside a formula. A simulation whose inputs are invisible is a forecast
wearing a lab coat.

**Output provenance is `seed`.** Every figure produced here is synthetic. Feed
one into `store` and the dashboard banner fires; that is intended.

**Results come as a range, never a point.** A single number is read as a
prediction no matter how it is labelled, so `run_scenarios` produces
pessimistic / base / optimistic and the spread between them is the finding. If
the spread is wide, that *is* the answer: the plan is not yet decidable.

What the simulation is genuinely good for is finding the **binding constraint**
— the thing that stops the plan regardless of how the demand assumptions land.
That answer does not depend on guessing conversion rates, which is exactly why
it is worth computing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .config import Policy
from .models import ProductCandidate

# Sourcing models available to a business with very little capital. The
# distinction matters more than any demand assumption at this scale: it decides
# whether inventory is reachable at all.
SOURCING_MODELS = {
    "stocked": (
        "Buy inventory up front at supplier MOQ. Lowest unit cost, highest "
        "capital requirement, and the cash is committed before any demand is "
        "proven."),
    "dropship": (
        "Supplier ships per order. No inventory outlay and no MOQ, but a "
        "materially higher unit cost, slower delivery, and no control over "
        "packaging or defects — the refund rate is usually worse."),
    "sample_then_stock": (
        "Buy a handful of units at sample pricing to film with, sell nothing "
        "until the content works, then stock. Slowest, and the only one that "
        "does not commit capital to unproven demand."),
}


@dataclass
class Assumption:
    """One input the simulation cannot derive, with where it came from."""

    name: str
    value: Any
    unit: str
    source: str

    def render(self) -> str:
        shown = (f"{self.value:,.2f}" if isinstance(self.value, float)
                 else f"{self.value:,}" if isinstance(self.value, int)
                 else str(self.value))
        return f"{self.name:<34} {shown:>12} {self.unit:<10} {self.source}"


@dataclass
class Assumptions:
    """Everything the simulation is told rather than computes.

    Defaults are conservative placeholders, not estimates of this business.
    Each carries its own `source` string and every one is printed with the
    result — an assumption the reader cannot see is an assumption they cannot
    disagree with.
    """

    starting_cash: float = 200.0
    days: int = 30
    sourcing_model: str = "stocked"

    # Fixed costs. These are the ones that hurt at this scale and the ones
    # people leave out of back-of-envelope plans.
    shopify_monthly_usd: float = 39.00
    domain_monthly_usd: float = 1.25
    other_fixed_monthly_usd: float = 0.00

    # Content production. Organic acquisition costs time, not media spend.
    videos_per_day: float = 2.0
    props_and_samples_usd: float = 0.00

    # Demand. THE assumptions the operator cannot verify — 2 of 12 signal
    # sources are connected, so these are stated, not known.
    median_views_per_video: int = 1200
    click_rate_pct: float = 1.2
    session_to_order_pct: float = 1.5
    dropship_cost_multiplier: float = 1.9

    def as_list(self) -> list[Assumption]:
        return [
            Assumption("starting_cash", self.starting_cash, "USD",
                       "given by the operator"),
            Assumption("days", self.days, "days", "given by the operator"),
            Assumption("sourcing_model", self.sourcing_model, "",
                       "chosen scenario"),
            Assumption("shopify_monthly", self.shopify_monthly_usd, "USD/mo",
                       "ASSUMED — check your own plan's current price"),
            Assumption("domain_monthly", self.domain_monthly_usd, "USD/mo",
                       "ASSUMED — a ~$15/yr domain amortised"),
            Assumption("other_fixed_monthly", self.other_fixed_monthly_usd,
                       "USD/mo", "given by the operator"),
            Assumption("videos_per_day", self.videos_per_day, "videos",
                       "planned cadence, costs time not money"),
            Assumption("props_and_samples", self.props_and_samples_usd, "USD",
                       "given by the operator"),
            Assumption("median_views_per_video", self.median_views_per_video,
                       "views",
                       "ASSUMED — no organic-analytics API; unknowable in advance"),
            Assumption("click_rate", self.click_rate_pct, "%",
                       "ASSUMED — no history for this account yet"),
            Assumption("session_to_order", self.session_to_order_pct, "%",
                       "ASSUMED — Shopify exposes no sessions; unverifiable"),
            Assumption("dropship_cost_multiplier", self.dropship_cost_multiplier,
                       "x", "ASSUMED — dropship unit cost vs bulk landed cost"),
        ]

    @property
    def fixed_monthly(self) -> float:
        return (self.shopify_monthly_usd + self.domain_monthly_usd
                + self.other_fixed_monthly_usd)


@dataclass
class DayRecord:
    day: int
    date: str
    cash_open: float
    inventory_units: int
    videos_posted: int
    views: int
    clicks: int
    orders: int
    revenue: float
    variable_cost: float
    fixed_cost: float
    cash_close: float
    note: str = ""


@dataclass
class SimulationResult:
    scenario: str
    sku: str
    assumptions: Assumptions
    days: list[DayRecord] = field(default_factory=list)
    blocking_constraint: str = ""
    feasible: bool = True
    warnings: list[str] = field(default_factory=list)

    @property
    def ending_cash(self) -> float:
        return round(self.days[-1].cash_close, 2) if self.days else \
            self.assumptions.starting_cash

    @property
    def total_orders(self) -> int:
        return sum(d.orders for d in self.days)

    @property
    def total_revenue(self) -> float:
        return round(sum(d.revenue for d in self.days), 2)

    @property
    def total_costs(self) -> float:
        return round(sum(d.variable_cost + d.fixed_cost for d in self.days), 2)

    @property
    def net_change(self) -> float:
        return round(self.ending_cash - self.assumptions.starting_cash, 2)

    @property
    def lowest_cash(self) -> float:
        return round(min((d.cash_close for d in self.days),
                         default=self.assumptions.starting_cash), 2)

    def summary(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "sku": self.sku,
            "feasible": self.feasible,
            "blocking_constraint": self.blocking_constraint,
            "starting_cash": self.assumptions.starting_cash,
            "ending_cash": self.ending_cash,
            "net_change": self.net_change,
            "lowest_cash": self.lowest_cash,
            "orders": self.total_orders,
            "revenue": self.total_revenue,
            "costs": self.total_costs,
            # Provenance is not optional. Everything here is synthetic.
            "data_source": "seed",
        }


# ---------------------------------------------------------------------------
def check_feasibility(policy: Policy, candidate: ProductCandidate,
                      assumptions: Assumptions) -> tuple[bool, str, list[str]]:
    """Can this plan start at all? Answered before any demand is modelled.

    Deliberately first. If capital cannot clear the supplier's minimum, no
    conversion-rate assumption changes the outcome, and computing a revenue
    figure on top of an impossible start is the most misleading thing this
    module could do.
    """
    warnings: list[str] = []
    supplier = candidate.supplier
    cash = assumptions.starting_cash
    fixed = assumptions.fixed_monthly

    runway = cash - fixed - assumptions.props_and_samples_usd
    if runway <= 0:
        return False, (
            f"Fixed costs of ${fixed:,.2f}/month exceed the ${cash:,.2f} "
            "starting cash before a single unit is bought. The store cannot be "
            "kept open for the month."), warnings

    if assumptions.sourcing_model == "stocked":
        first_order = supplier.landed_unit_cost * supplier.moq
        if first_order > runway:
            shortfall = first_order - runway
            return False, (
                f"Supplier MOQ is {supplier.moq:,} units at "
                f"${supplier.landed_unit_cost:,.2f} landed = "
                f"${first_order:,.2f}. After ${fixed:,.2f} of fixed costs, "
                f"${runway:,.2f} is available — short by ${shortfall:,.2f}. "
                "This binds regardless of how well the content performs."), warnings

    if assumptions.sourcing_model == "dropship":
        warnings.append(
            "Dropship removes the MOQ constraint and replaces it with a margin "
            f"one: unit cost is assumed {assumptions.dropship_cost_multiplier}x "
            "the bulk landed cost, and delivery times on this model routinely "
            "breach the 12-day shipping gate in [selection].")

    reserve = float(policy.risk.get("min_cash_reserve_usd", 0) or 0)
    if reserve and cash < reserve:
        warnings.append(
            f"Starting cash ${cash:,.2f} is below the ${reserve:,.2f} minimum "
            "cash reserve in [risk]. The reserve exists so one bad month is not "
            "terminal; below it, every purchase is being made out of the buffer.")

    return True, "", warnings


def unit_contribution(policy: Policy, candidate: ProductCandidate,
                      assumptions: Assumptions) -> tuple[float, float]:
    """Revenue and variable cost per unit sold, on the policy's fee schedule.

    Uses `[fees.shopify]` because the sale happens on the storefront — the
    TikTok schedule would apply only to a TikTok Shop checkout, and using the
    wrong one silently mis-states margin on every unit.
    """
    fees = policy.raw.get("fees", {}).get("shopify", {})
    price = float(candidate.target_price)

    unit_cost = candidate.supplier.landed_unit_cost
    if assumptions.sourcing_model == "dropship":
        unit_cost *= assumptions.dropship_cost_multiplier

    variable = unit_cost
    variable += price * float(fees.get("referral_pct", 0.0)) / 100.0
    variable += price * float(fees.get("payment_pct", 0.0)) / 100.0
    variable += float(fees.get("payment_flat", 0.0))
    variable += float(fees.get("fulfillment_flat", 0.0))
    # Returns cost the goods and the outbound shipping; the 0.5 factor is the
    # share of returned stock that cannot be resold, used elsewhere in the
    # system for the same reason.
    return_rate = float(fees.get("expected_return_rate_pct", 0.0)) / 100.0
    variable += return_rate * (price + unit_cost * 0.5)
    return price, round(variable, 4)


def run_month(policy: Policy, candidate: ProductCandidate,
              assumptions: Assumptions, *, scenario: str = "base",
              demand_multiplier: float = 1.0,
              start: date | None = None) -> SimulationResult:
    """Day-by-day cash ledger for one month.

    Demand is modelled as a ramp rather than a flat rate, because a new account
    has no distribution on day one — assuming steady-state views from the start
    is the single most common way these plans overstate the first month.
    """
    start = start or date.today()
    result = SimulationResult(scenario=scenario, sku=candidate.sku,
                              assumptions=assumptions)

    feasible, blocker, warnings = check_feasibility(policy, candidate, assumptions)
    result.warnings.extend(warnings)
    if not feasible:
        result.feasible = False
        result.blocking_constraint = blocker
        return result

    price, variable = unit_contribution(policy, candidate, assumptions)
    if price - variable <= 0:
        result.feasible = False
        result.blocking_constraint = (
            f"Contribution per unit is ${price - variable:,.2f} — every sale "
            "loses money before a single fixed cost. No volume fixes this.")
        return result

    cash = assumptions.starting_cash
    inventory = 0
    daily_fixed = assumptions.fixed_monthly / 30.0

    # Up-front outlay.
    if assumptions.sourcing_model == "stocked":
        units = candidate.supplier.moq
        outlay = candidate.supplier.landed_unit_cost * units
        cash -= outlay
        inventory = units
    cash -= assumptions.props_and_samples_usd

    for day_index in range(assumptions.days):
        day_date = start + timedelta(days=day_index)
        cash_open = cash

        # Distribution ramps. A new account reaches roughly its steady state
        # over several weeks; front-loading it is how a first month gets
        # overstated.
        ramp = min(1.0, (day_index + 1) / 21.0)
        videos = assumptions.videos_per_day
        views = int(assumptions.median_views_per_video * videos * ramp
                    * demand_multiplier)
        clicks = int(views * assumptions.click_rate_pct / 100.0)
        # Every click is treated as a session. Real click-to-session loss is
        # material and unmeasurable here, so this is optimistic by construction.
        orders = int(clicks * assumptions.session_to_order_pct / 100.0)

        note = ""
        if assumptions.sourcing_model == "stocked" and orders > inventory:
            note = (f"Demand of {orders} exceeded {inventory} units in stock; "
                    "the rest is lost, not backordered.")
            orders = max(inventory, 0)

        revenue = orders * price
        variable_cost = orders * variable
        if assumptions.sourcing_model == "stocked":
            # Goods were already paid for at purchase; do not charge again.
            variable_cost -= orders * candidate.supplier.landed_unit_cost
            inventory -= orders

        cash += revenue - variable_cost - daily_fixed
        result.days.append(DayRecord(
            day=day_index + 1, date=day_date.isoformat(), cash_open=round(cash_open, 2),
            inventory_units=inventory, videos_posted=int(videos), views=views,
            clicks=clicks, orders=orders, revenue=round(revenue, 2),
            variable_cost=round(variable_cost, 2), fixed_cost=round(daily_fixed, 2),
            cash_close=round(cash, 2), note=note))

        if cash < 0 and not result.blocking_constraint:
            result.blocking_constraint = (
                f"Cash went negative on day {day_index + 1}. The plan needs "
                "more starting capital or lower fixed costs, not more videos.")

    if result.lowest_cash < 0:
        result.warnings.append(
            f"Cash bottomed at ${result.lowest_cash:,.2f}. A plan that dips "
            "below zero has already failed — there is no overdraft here.")
    stockouts = [d for d in result.days if d.note]
    if stockouts:
        result.warnings.append(
            f"Stocked out on {len(stockouts)} day(s). Demand above stock is "
            "lost, not deferred; the modelled revenue is capped by inventory "
            "rather than by demand.")
    return result


def run_scenarios(policy: Policy, candidate: ProductCandidate,
                  assumptions: Assumptions,
                  start: date | None = None) -> dict[str, SimulationResult]:
    """Pessimistic / base / optimistic.

    A range rather than a point, because a single number is read as a
    prediction no matter how it is labelled. The multipliers are deliberately
    wide: with two of twelve signal sources connected, the honest uncertainty
    on demand is large, and a narrow band would imply knowledge that does not
    exist.
    """
    return {
        name: run_month(policy, candidate, assumptions, scenario=name,
                        demand_multiplier=multiplier, start=start)
        for name, multiplier in
        (("pessimistic", 0.25), ("base", 1.0), ("optimistic", 3.0))
    }


# ---------------------------------------------------------------------------
BANNER = (
    "SIMULATED — NOT A FORECAST. Every figure below is computed from the stated "
    "assumptions, not from observed demand. This system has 2 of 12 market "
    "signal sources connected and no history for this account, so the demand "
    "inputs are guesses that the operator has flagged as guesses. Use this to "
    "find which constraint binds, not to predict revenue."
)


def render(results: dict[str, SimulationResult], *, width: int = 78) -> str:
    """Report the range, the constraint, and every assumption behind them."""
    lines = ["=" * width, "ONE-MONTH CASH SIMULATION".center(width), "=" * width, ""]
    for chunk in _wrap(BANNER, width - 4):
        lines.append(f"  {chunk}")
    lines.append("")

    base = results.get("base") or next(iter(results.values()))
    lines.append(f"  Product:        {base.sku}")
    lines.append(f"  Sourcing model: {base.assumptions.sourcing_model} — "
                 f"{SOURCING_MODELS.get(base.assumptions.sourcing_model, '')[:40]}")
    lines.append("")

    if not base.feasible:
        lines.append("-" * width)
        lines.append("  PLAN DOES NOT START")
        lines.append("-" * width)
        for chunk in _wrap(base.blocking_constraint, width - 4):
            lines.append(f"  {chunk}")
        lines.append("")
        lines.append("  No revenue is modelled, because modelling revenue on top "
                     "of an impossible")
        lines.append("  start is the most misleading thing this report could do.")
        lines.append("")
    else:
        lines.append("-" * width)
        header = f"  {'Scenario':<14}{'Orders':>8}{'Revenue':>12}{'End cash':>12}{'Change':>12}"
        lines.append(header)
        lines.append("-" * width)
        for name in ("pessimistic", "base", "optimistic"):
            r = results.get(name)
            if r is None:
                continue
            lines.append(
                f"  {name:<14}{r.total_orders:>8,}{r.total_revenue:>12,.2f}"
                f"{r.ending_cash:>12,.2f}{r.net_change:>+12,.2f}")
        lines.append("")
        spread = (results["optimistic"].ending_cash
                  - results["pessimistic"].ending_cash) if len(results) == 3 else 0
        lines.append(f"  Spread between pessimistic and optimistic: ${spread:,.2f}")
        for chunk in _wrap(
                "That spread is the finding. It comes from demand assumptions "
                "nobody has measured, so treat the band as the answer and the "
                "middle value as arithmetic rather than as a prediction.",
                width - 4):
            lines.append(f"  {chunk}")
        lines.append("")

    seen: set[str] = set()
    warnings = [w for r in results.values() for w in r.warnings
                if not (w in seen or seen.add(w))]
    if warnings:
        lines.append("-" * width)
        lines.append("  WARNINGS")
        lines.append("-" * width)
        for warning in warnings:
            for i, chunk in enumerate(_wrap(warning, width - 6)):
                lines.append(("  - " if i == 0 else "    ") + chunk)
        lines.append("")

    lines.append("-" * width)
    lines.append("  ASSUMPTIONS  (everything not derived from the policy file)")
    lines.append("-" * width)
    for assumption in base.assumptions.as_list():
        lines.append(f"  {assumption.render()}")
    lines.append("")
    lines.append("  Rows marked ASSUMED are the ones to argue with. They are not")
    lines.append("  measurements and this system cannot make them into measurements.")
    lines.append("=" * width)
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]
