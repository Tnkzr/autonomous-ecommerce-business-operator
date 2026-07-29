"""Capital allocation: where the next dollar goes, and where it must not.

Two ideas from the charter drive this module.

**Risk-adjusted return, not raw ROI.** A 120% ROI product we know little about,
that ties cash up for 150 days, is worse than a 60% ROI product we understand
that recycles in 60. Ranking on headline ROI systematically over-allocates to
the least understood opportunities, because uncertainty and optimism look
identical in a spreadsheet.

**Concentration is the risk that ends businesses.** A seller with 70% of
capital in one SKU is one listing suspension, one patent claim, or one supplier
failure away from insolvency — regardless of how good that SKU's margin was.
Concentration limits therefore bind even when the concentrated product is the
most profitable one available. That is the point of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Policy
from .models import money


@dataclass
class Position:
    """Capital currently committed to one SKU."""

    sku: str
    category: str
    supplier_id: str
    units_on_hand: int
    units_inbound: int
    unit_cost: float
    annual_units_sold: float = 0.0

    @property
    def capital_deployed(self) -> float:
        return money((self.units_on_hand + self.units_inbound) * self.unit_cost)

    def turns_per_year(self) -> float:
        """Inventory turns = annual units sold / units held.

        Uses units held directly rather than half of them. Halving assumes a
        sawtooth that drains to zero before each reorder, which no operator
        running safety stock actually does — and it doubles the reported turns,
        making sleeping capital look productive.
        """
        held = self.units_on_hand + self.units_inbound
        if held <= 0:
            return 0.0
        return round(self.annual_units_sold / held, 2)


@dataclass
class AllocationRequest:
    """A proposed use of capital, competing against the others."""

    sku: str
    category: str
    supplier_id: str
    amount_usd: float
    expected_roi_pct: float
    confidence_pct: float          # from signals.assess_confidence
    cash_cycle_days: int
    is_new_sku: bool = False
    rationale: str = ""


@dataclass
class AllocationDecision:
    request: AllocationRequest
    approved_amount: float
    risk_adjusted_roi_pct: float
    annualised_roi_pct: float
    rank: int
    accepted: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class CapitalState:
    """Where the money currently is."""

    total_capital_usd: float
    cash_available_usd: float
    positions: list[Position] = field(default_factory=list)

    @property
    def deployed_usd(self) -> float:
        return money(sum(p.capital_deployed for p in self.positions))

    def share_by(self, attr: str) -> dict[str, float]:
        """Share of deployed capital grouped by sku / category / supplier_id."""
        deployed = self.deployed_usd
        if deployed <= 0:
            return {}
        grouped: dict[str, float] = {}
        for p in self.positions:
            key = getattr(p, attr)
            grouped[key] = grouped.get(key, 0.0) + p.capital_deployed
        return {k: round(v / deployed * 100, 1) for k, v in grouped.items()}


def reserves(policy: Policy) -> dict[str, float]:
    """Cash held back from inventory purchases, by purpose."""
    cfg = policy.raw["capital"]
    total = float(cfg["total_capital_usd"])
    out = {
        "advertising": money(total * float(cfg["reserve_advertising_pct"]) / 100),
        "refunds": money(total * float(cfg["reserve_refunds_pct"]) / 100),
        "contingency": money(total * float(cfg["reserve_contingency_pct"]) / 100),
    }
    out["total"] = money(sum(out.values()))
    out["deployable"] = money(max(0.0, total - out["total"]))
    return out


def risk_adjusted_roi(request: AllocationRequest) -> float:
    """Discount headline ROI by confidence and by capital velocity.

    Two adjustments, both deliberate:

      confidence — a 50% confident 100% ROI is treated as a 50% ROI, because
      that is what it is in expectation. Ignoring this ranks the unknown above
      the understood.

      velocity — ROI is normalised to a 90-day reference cycle. A return that
      takes a year is not the same return as one that lands in a quarter, and
      comparing them without adjusting silently favours slow money.
    """
    confidence = max(0.0, min(request.confidence_pct, 100.0)) / 100.0
    cycle = max(request.cash_cycle_days, 1)
    velocity = 90.0 / cycle
    return round(request.expected_roi_pct * confidence * velocity, 1)


def annualised_roi(request: AllocationRequest) -> float:
    """What the ROI compounds to over a year at this cash-cycle speed."""
    cycle = max(request.cash_cycle_days, 1)
    return round(request.expected_roi_pct * (365.0 / cycle), 1)


def allocate(
    policy: Policy,
    state: CapitalState,
    requests: list[AllocationRequest],
) -> tuple[list[AllocationDecision], list[str]]:
    """Rank competing requests and fund them until a limit binds.

    Limits are checked against the position the portfolio would be in *after*
    funding, not before — a request that is fine in isolation and breaches
    concentration once granted must be caught before the money moves.
    """
    cfg = policy.raw["capital"]
    notes: list[str] = []

    res = reserves(policy)
    deployable = min(res["deployable"], state.cash_available_usd)
    notes.append(
        f"Deployable capital ${deployable:,.2f} "
        f"(${res['total']:,.2f} held in reserve: ads, refunds, contingency)."
    )

    min_rar = float(cfg["min_risk_adjusted_roi_pct"])
    max_sku = float(cfg["max_single_sku_share_pct"])
    max_supplier = float(cfg["max_single_supplier_share_pct"])
    max_category = float(cfg["max_single_category_share_pct"])

    scored = sorted(requests, key=lambda r: -risk_adjusted_roi(r))
    decisions: list[AllocationDecision] = []

    # Simulate the portfolio as we fund, so limits reflect the end state.
    running: dict[str, dict[str, float]] = {
        "sku": {}, "category": {}, "supplier_id": {},
    }
    for p in state.positions:
        running["sku"][p.sku] = running["sku"].get(p.sku, 0.0) + p.capital_deployed
        running["category"][p.category] = (
            running["category"].get(p.category, 0.0) + p.capital_deployed)
        running["supplier_id"][p.supplier_id] = (
            running["supplier_id"].get(p.supplier_id, 0.0) + p.capital_deployed)
    running_total = state.deployed_usd
    remaining = deployable

    for rank, req in enumerate(scored, start=1):
        rar = risk_adjusted_roi(req)
        ann = annualised_roi(req)
        reasons: list[str] = []
        accepted = True
        approved = req.amount_usd

        if rar < min_rar:
            accepted = False
            reasons.append(
                f"Risk-adjusted ROI {rar:.1f}% is under the {min_rar:.0f}% floor "
                f"(headline {req.expected_roi_pct:.0f}% discounted by "
                f"{req.confidence_pct:.0f}% confidence and a {req.cash_cycle_days}-day "
                "cash cycle)."
            )

        if accepted and approved > remaining:
            if remaining <= 0:
                accepted = False
                reasons.append("No deployable capital remaining this cycle.")
            else:
                reasons.append(
                    f"Partially funded: ${remaining:,.2f} of ${req.amount_usd:,.2f} "
                    "available after higher-ranked allocations."
                )
                approved = remaining

        if accepted:
            projected_total = running_total + approved
            checks = (
                ("sku", req.sku, max_sku, "SKU"),
                ("category", req.category, max_category, "category"),
                ("supplier_id", req.supplier_id, max_supplier, "supplier"),
            )
            for key, value, limit, label in checks:
                if str(value).lower() in UNKNOWN_KEYS:
                    # Placeholder grouping, not a real exposure. Flag and move on.
                    reasons.append(
                        f"No {label} recorded for this position — {label} "
                        "concentration could not be checked. Record it so the limit "
                        "can actually protect you."
                    )
                    continue
                projected = running[key].get(value, 0.0) + approved
                share = projected / projected_total * 100 if projected_total else 0.0
                if share > limit:
                    # Fund up to the limit rather than refusing outright — the
                    # opportunity is good, the concentration is the problem.
                    headroom = (limit / 100.0) * running_total - running[key].get(value, 0.0)
                    headroom = headroom / (1 - limit / 100.0) if limit < 100 else headroom
                    headroom = max(0.0, money(headroom))
                    if headroom < 1.0:
                        accepted = False
                        reasons.append(
                            f"Would put {share:.0f}% of deployed capital in one "
                            f"{label} ({value}), over the {limit:.0f}% limit. "
                            "Concentration risk is not offset by a good margin — "
                            "one suspension or supplier failure would be fatal."
                        )
                    else:
                        reasons.append(
                            f"Capped at ${headroom:,.2f} to hold {label} {value} "
                            f"at the {limit:.0f}% concentration limit."
                        )
                        approved = min(approved, headroom)

        if accepted and approved > 0:
            running_total += approved
            running["sku"][req.sku] = running["sku"].get(req.sku, 0.0) + approved
            running["category"][req.category] = (
                running["category"].get(req.category, 0.0) + approved)
            running["supplier_id"][req.supplier_id] = (
                running["supplier_id"].get(req.supplier_id, 0.0) + approved)
            remaining = money(remaining - approved)
            reasons.insert(0, f"Funded ${approved:,.2f} at rank {rank}.")
        else:
            approved = 0.0

        decisions.append(AllocationDecision(
            request=req, approved_amount=money(approved),
            risk_adjusted_roi_pct=rar, annualised_roi_pct=ann,
            rank=rank, accepted=accepted and approved > 0, reasons=reasons,
        ))

    funded = sum(d.approved_amount for d in decisions)
    notes.append(f"Allocated ${funded:,.2f} across "
                 f"{sum(1 for d in decisions if d.accepted)} of {len(decisions)} requests.")
    if remaining > 0 and any(not d.accepted for d in decisions):
        notes.append(
            f"${remaining:,.2f} left undeployed — the remaining requests failed a "
            "return or concentration test. Holding cash beats funding a bad "
            "allocation; the capital keeps its option value."
        )
    return decisions, notes


UNKNOWN_KEYS = {"unknown", "uncategorised", "uncategorized", ""}


def concentration_report(policy: Policy, state: CapitalState) -> dict[str, Any]:
    """Current concentration against the policy limits.

    Positions missing a category or supplier all land in one bucket, which
    reads as 100% concentration. That is an artefact of missing data, not a
    real exposure, so it is reported separately — enforcing a limit against a
    placeholder would block every allocation for a bookkeeping reason.
    """
    cfg = policy.raw["capital"]
    limits = {
        "sku": float(cfg["max_single_sku_share_pct"]),
        "category": float(cfg["max_single_category_share_pct"]),
        "supplier_id": float(cfg["max_single_supplier_share_pct"]),
    }
    out: dict[str, Any] = {"deployed_usd": state.deployed_usd, "breaches": [], "shares": {}}

    out["unmapped"] = []
    for attr, limit in limits.items():
        shares = state.share_by(attr)
        out["shares"][attr] = shares
        for key, pct in sorted(shares.items(), key=lambda kv: -kv[1]):
            if str(key).lower() in UNKNOWN_KEYS:
                if pct > 0:
                    out["unmapped"].append({
                        "dimension": attr, "share_pct": pct,
                        "detail": (
                            f"{pct:.0f}% of deployed capital has no {attr} recorded. "
                            "Concentration in this dimension cannot be assessed until "
                            "positions carry it."
                        ),
                    })
                continue
            if pct > limit:
                out["breaches"].append({
                    "dimension": attr, "value": key, "share_pct": pct, "limit_pct": limit,
                })
    return out


def portfolio_turns(policy: Policy, state: CapitalState) -> dict[str, Any]:
    """Inventory turnover, portfolio-wide and per SKU.

    Turns are the bridge between margin and compounding: the same 30% margin
    earns four times as much a year at 4 turns as it does at 1.
    """
    cfg = policy.raw["capital"]
    target = float(cfg["target_inventory_turns_per_year"])
    floor = float(cfg["min_inventory_turns_per_year"])

    per_sku = {p.sku: p.turns_per_year() for p in state.positions}
    total_cogs = sum(p.annual_units_sold * p.unit_cost for p in state.positions)
    inventory_value = sum(p.capital_deployed for p in state.positions)
    portfolio = round(total_cogs / inventory_value, 2) if inventory_value > 0 else 0.0

    sluggish = [sku for sku, t in per_sku.items() if 0 < t < floor]
    dead = [sku for sku, t in per_sku.items() if t <= 0]

    return {
        "portfolio_turns": portfolio,
        "target_turns": target,
        "min_turns": floor,
        "per_sku": per_sku,
        "sluggish": sluggish,
        "dead": dead,
        "capital_in_sluggish": money(sum(
            p.capital_deployed for p in state.positions if p.sku in sluggish
        )),
        "capital_in_dead": money(sum(
            p.capital_deployed for p in state.positions if p.sku in dead
        )),
    }
