"""Fee reconciliation: replace the estimated schedule with measured numbers.

`[fees.*]` drives every profit calculation in this system. It sets which
products clear the 30% margin gate, where the repricer's floor sits, and which
opportunities get capital. Every one of those numbers is currently computed
from an estimate somebody typed into a config file — including me.

This module closes that gap by reading what the marketplace actually charged.
eBay's Finances API reports fees per transaction and broken out by type, which
is better than a blended take rate: "13.4% overall" hides that 12.9% is the
final value fee, 0.4% is an ad fee you opted into, and 0.1% is a regulatory
charge you cannot avoid. Those three have completely different responses.

Two rules.

**It proposes, it does not apply.** Nothing here writes `policy.toml`. Changing
a fee schedule silently changes which products qualify and what prices are
floors, so it is a decision with a commit message attached, not a background
sync. The output is a diff a human applies.

**It refuses on thin samples.** A take rate computed from four orders is four
orders' worth of category mix, promotions, and rounding. `MIN_ORDERS` gates the
recommendation, and below it the reconciliation still reports what it saw —
labelled as insufficient rather than withheld, because "we looked and it is
early" is useful and "no data" is not.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .config import Policy

# Below this, category mix and promotions dominate and the measured rate says
# more about which orders happened to land than about the fee schedule.
MIN_ORDERS = 25
# A drift smaller than this is noise from rounding and mixed categories; acting
# on it would churn the policy file without changing any decision.
MATERIAL_DRIFT_PCT = 0.5

# eBay fee types worth naming. The point of the mapping is that these have
# different responses: a final value fee is the cost of doing business, an ad
# fee is a choice, and a below-standard fee is a penalty that can be removed by
# fixing the account.
FEE_MEANINGS = {
    "FINAL_VALUE_FEE": ("Core commission on the sale. Category-dependent and "
                        "not negotiable — this is the number that belongs in "
                        "referral_pct."),
    "FINAL_VALUE_FEE_FIXED_PER_ORDER": (
        "Flat per-order charge. Belongs in payment_flat, and it is the fee that "
        "makes low-price items disproportionately unprofitable."),
    "AD_FEE": ("Promoted Listings. This one is a choice — if it is material and "
               "the listings would sell anyway, it is pure margin given away."),
    "INTERNATIONAL_FEE": ("Charged on cross-border sales. If material, the "
                          "margin model needs to differ by buyer country."),
    "REGULATORY_OPERATING_FEE": (
        "Statutory charge, unavoidable. Small but it is real margin and most "
        "estimates omit it entirely."),
    "BELOW_STANDARD_FEE": (
        "A penalty for seller performance, not a cost of trading. This one is "
        "removable — fix the account health metric causing it."),
    "INSERTION_FEE": ("Charged per listing beyond the free allowance. A cost of "
                      "catalogue breadth rather than of sales."),
}

# Transaction types that represent money in. Everything else is a cost, a
# refund, or a transfer, and folding them into revenue overstates the base the
# take rate is computed against.
REVENUE_TYPES = frozenset({"SALE"})
REFUND_TYPES = frozenset({"REFUND", "DISPUTE"})


@dataclass
class FeeComponent:
    fee_type: str
    total: float
    occurrences: int
    meaning: str = ""

    def pct_of(self, revenue: float) -> float | None:
        return round(self.total / revenue * 100, 3) if revenue > 0 else None

    def per_order(self, orders: int) -> float | None:
        return round(self.total / orders, 4) if orders else None


@dataclass
class Reconciliation:
    marketplace: str
    orders: int
    gross_revenue: float
    refunds: float
    total_fees: float
    components: list[FeeComponent] = field(default_factory=list)
    policy_estimate_pct: float = 0.0
    warnings: list[str] = field(default_factory=list)
    sufficient: bool = False

    @property
    def realised_take_rate_pct(self) -> float | None:
        """Fees as a share of gross revenue. None when there is no base."""
        if self.gross_revenue <= 0:
            return None
        return round(self.total_fees / self.gross_revenue * 100, 3)

    @property
    def drift_pct(self) -> float | None:
        realised = self.realised_take_rate_pct
        if realised is None:
            return None
        return round(realised - self.policy_estimate_pct, 3)

    @property
    def refund_rate_pct(self) -> float | None:
        if self.gross_revenue <= 0:
            return None
        return round(self.refunds / self.gross_revenue * 100, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "marketplace": self.marketplace,
            "orders": self.orders,
            "gross_revenue": round(self.gross_revenue, 2),
            "refunds": round(self.refunds, 2),
            "total_fees": round(self.total_fees, 2),
            "realised_take_rate_pct": self.realised_take_rate_pct,
            "policy_estimate_pct": self.policy_estimate_pct,
            "drift_pct": self.drift_pct,
            "refund_rate_pct": self.refund_rate_pct,
            "sufficient": self.sufficient,
            "components": [
                {"fee_type": c.fee_type, "total": round(c.total, 2),
                 "occurrences": c.occurrences,
                 "pct_of_revenue": c.pct_of(self.gross_revenue),
                 "per_order": c.per_order(self.orders),
                 "meaning": c.meaning}
                for c in self.components
            ],
            "warnings": self.warnings,
        }


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _amount(node: Any) -> float:
    if isinstance(node, dict):
        return _f(node.get("value"))
    return _f(node)


def reconcile_ebay(transactions: list[dict[str, Any]], policy: Policy
                   ) -> Reconciliation:
    """Reconcile `[fees.ebay]` against Finances API transactions.

    Takes raw transaction records rather than a connector so the same code
    serves a live fetch, a replay, and a test without a network.
    """
    schedule = policy.raw.get("fees", {}).get("ebay", {})
    estimate = _f(schedule.get("referral_pct")) + _f(schedule.get("payment_pct"))

    gross = refunds = 0.0
    fee_totals: dict[str, float] = defaultdict(float)
    fee_counts: dict[str, int] = defaultdict(int)
    order_ids: set[str] = set()
    unmatched_fees = 0.0

    for txn in transactions:
        txn_type = str(txn.get("transactionType") or txn.get("type") or "").upper()
        amount = _amount(txn.get("amount"))
        order_id = str(txn.get("orderId") or "")

        if txn_type in REVENUE_TYPES:
            gross += amount
            if order_id:
                order_ids.add(order_id)
            # Fee breakdown lives under the order line items on a SALE.
            for line in (txn.get("orderLineItems") or []):
                for fee in (line.get("marketplaceFees") or []):
                    fee_type = str(fee.get("feeType", "") or "UNKNOWN").upper()
                    fee_totals[fee_type] += abs(_amount(fee.get("amount")))
                    fee_counts[fee_type] += 1
            # Some records only carry a rolled-up total. Count it so the take
            # rate stays right even when the breakdown is absent.
            if not (txn.get("orderLineItems") or []):
                rolled = abs(_amount(txn.get("totalFeeAmount")))
                if rolled:
                    fee_totals["UNBROKEN_TOTAL"] += rolled
                    fee_counts["UNBROKEN_TOTAL"] += 1
                    unmatched_fees += rolled

        elif txn_type in REFUND_TYPES:
            refunds += abs(amount)

        elif txn_type == "NON_SALE_CHARGE":
            # Charges not tied to a sale — insertion fees, subscriptions. Real
            # money, and omitted from every "take rate" that only looks at SALE
            # records, which is how a catalogue of dead listings quietly costs
            # more than it earns.
            fee_type = str(txn.get("feeType", "") or "NON_SALE_CHARGE").upper()
            fee_totals[fee_type] += abs(amount)
            fee_counts[fee_type] += 1

    components = [
        FeeComponent(fee_type=name, total=total, occurrences=fee_counts[name],
                     meaning=FEE_MEANINGS.get(name, ""))
        for name, total in sorted(fee_totals.items(), key=lambda kv: -kv[1])
    ]
    total_fees = sum(fee_totals.values())
    orders = len(order_ids)

    warnings: list[str] = []
    sufficient = orders >= MIN_ORDERS

    if not sufficient:
        warnings.append(
            f"{orders} order(s) in this window, below the {MIN_ORDERS} needed "
            "for a reliable rate. Category mix, promotions and rounding "
            "dominate at this size — the numbers below are what happened, not "
            "yet what to expect.")
    if unmatched_fees:
        warnings.append(
            f"${unmatched_fees:,.2f} of fees arrived without a per-type "
            "breakdown and are grouped as UNBROKEN_TOTAL. The take rate is "
            "still correct; the attribution to specific fee types is not.")

    ad_fee = next((c for c in components if c.fee_type == "AD_FEE"), None)
    if ad_fee and gross > 0 and (ad_fee.pct_of(gross) or 0) >= 1.0:
        warnings.append(
            f"Promoted Listings cost {ad_fee.pct_of(gross):.2f}% of revenue "
            f"(${ad_fee.total:,.2f}). That is a choice, not a cost of trading — "
            "worth testing whether the listings sell without it.")
    penalty = next((c for c in components if c.fee_type == "BELOW_STANDARD_FEE"),
                   None)
    if penalty:
        warnings.append(
            f"${penalty.total:,.2f} of below-standard penalty fees. This is a "
            "performance penalty, not a trading cost, and it is removable — fix "
            "the account metric causing it before optimising anything else.")

    return Reconciliation(
        marketplace="ebay", orders=orders, gross_revenue=gross, refunds=refunds,
        total_fees=total_fees, components=components,
        policy_estimate_pct=estimate, warnings=warnings, sufficient=sufficient)


def propose_policy_patch(reconciliation: Reconciliation) -> dict[str, Any]:
    """Suggest a `[fees.*]` update. Never applies it.

    Returns the proposal and whether it is worth making. Changing a fee
    schedule changes which products qualify and where price floors sit, so it
    belongs in a commit with a stated source — not in a background job.
    """
    if not reconciliation.sufficient:
        return {
            "recommended": False,
            "reason": (f"Only {reconciliation.orders} order(s). A schedule "
                       f"changed on fewer than {MIN_ORDERS} would move every "
                       "margin gate in the system on the strength of a small "
                       "sample."),
            "changes": {},
        }

    drift = reconciliation.drift_pct
    if drift is None:
        return {"recommended": False,
                "reason": "No revenue in the window, so no rate to compare.",
                "changes": {}}
    if abs(drift) < MATERIAL_DRIFT_PCT:
        return {
            "recommended": False,
            "reason": (f"Drift is {drift:+.2f}pp, inside the "
                       f"{MATERIAL_DRIFT_PCT}pp noise band. Changing the policy "
                       "here would churn the file without changing a decision."),
            "changes": {},
        }

    gross = reconciliation.gross_revenue
    changes: dict[str, float] = {}
    for component in reconciliation.components:
        share = component.pct_of(gross)
        if share is None:
            continue
        if component.fee_type == "FINAL_VALUE_FEE":
            changes["referral_pct"] = round(share, 2)
        elif component.fee_type == "FINAL_VALUE_FEE_FIXED_PER_ORDER":
            per_order = component.per_order(reconciliation.orders)
            if per_order:
                changes["payment_flat"] = round(per_order, 2)

    refund_rate = reconciliation.refund_rate_pct
    if refund_rate is not None:
        changes["expected_return_rate_pct"] = round(refund_rate, 1)

    return {
        "recommended": bool(changes),
        "reason": (f"Realised take rate is "
                   f"{reconciliation.realised_take_rate_pct:.2f}% against a "
                   f"{reconciliation.policy_estimate_pct:.2f}% estimate "
                   f"({drift:+.2f}pp) over {reconciliation.orders} orders."),
        "changes": changes,
        "apply_by_hand": (
            "Edit [fees.ebay] in config/policy.toml and say in the commit "
            "message that the numbers came from the Finances API, with the "
            "date range. A fee schedule changed without a stated source cannot "
            "be audited later."),
    }


def render(reconciliation: Reconciliation, patch: dict[str, Any],
           *, width: int = 78) -> str:
    """Human-readable reconciliation."""
    lines = ["=" * width,
             f"FEE RECONCILIATION — {reconciliation.marketplace}".center(width),
             "=" * width, ""]

    realised = reconciliation.realised_take_rate_pct
    lines.append(f"  Orders             {reconciliation.orders:,}")
    lines.append(f"  Gross revenue      ${reconciliation.gross_revenue:,.2f}")
    lines.append(f"  Total fees         ${reconciliation.total_fees:,.2f}")
    lines.append(f"  Realised take rate "
                 + (f"{realised:.2f}%" if realised is not None else "unknown"))
    lines.append(f"  Policy estimate    {reconciliation.policy_estimate_pct:.2f}%")
    if reconciliation.drift_pct is not None:
        lines.append(f"  Drift              {reconciliation.drift_pct:+.2f}pp")
    if reconciliation.refund_rate_pct is not None:
        lines.append(f"  Refund rate        {reconciliation.refund_rate_pct:.2f}%")
    lines.append("")

    if reconciliation.components:
        lines.append("-" * width)
        lines.append(f"  {'FEE TYPE':<34}{'TOTAL':>12}  {'% REV':>7}  {'PER ORDER':>10}")
        lines.append("-" * width)
        for component in reconciliation.components:
            share = component.pct_of(reconciliation.gross_revenue)
            per_order = component.per_order(reconciliation.orders)
            share_cell = f"{share:6.2f}%" if share is not None else f"{'—':>7}"
            order_cell = (f"${per_order:>9,.2f}" if per_order is not None
                          else f"{'—':>10}")
            lines.append(
                f"  {component.fee_type[:32]:<34}"
                f"${component.total:>11,.2f}  {share_cell}  {order_cell}")
            if component.meaning:
                for chunk in _wrap(component.meaning, width - 8):
                    lines.append(f"      {chunk}")
        lines.append("")

    lines.append("-" * width)
    lines.append("  POLICY PROPOSAL")
    lines.append("-" * width)
    for chunk in _wrap(patch["reason"], width - 4):
        lines.append(f"  {chunk}")
    if patch["recommended"]:
        lines.append("")
        lines.append("  Suggested [fees.ebay] changes:")
        for key, value in patch["changes"].items():
            lines.append(f"    {key} = {value}")
        lines.append("")
        for chunk in _wrap(patch["apply_by_hand"], width - 4):
            lines.append(f"  {chunk}")
    lines.append("")

    if reconciliation.warnings:
        lines.append("-" * width)
        lines.append("  WARNINGS")
        lines.append("-" * width)
        for warning in reconciliation.warnings:
            for i, chunk in enumerate(_wrap(warning, width - 6)):
                lines.append(("  - " if i == 0 else "    ") + chunk)
        lines.append("")
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
