"""The spend gate. Every action that costs money or touches the account passes
through `authorise()` before it can execute.

This module is intentionally paranoid and intentionally dumb: it makes no
judgement about whether an action is *smart*, only whether it is *permitted*.
Separating "is this a good idea" (engines) from "are we allowed" (here) means
a bug in an engine's reasoning cannot spend money it should not.

Order of checks matters. Never-autonomous first, then negative-profit, then
hard caps, then approval thresholds, and the advisory-mode kill switch last.
The most categorical rules win, and the *reason* reported is the most
substantive one: an over-cap request in advisory mode should tell you it broke
the cap, not merely that the mode flag is off.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Policy
from .models import Decision, money


@dataclass
class SpendState:
    """Current exposure. In production this is derived from the ledger."""

    spent_today_usd: float = 0.0
    open_po_exposure_usd: float = 0.0
    cash_available_usd: float = 0.0
    new_skus_this_week: int = 0


@dataclass
class AuthorisationResult:
    permitted: bool
    decision: Decision
    reasons: list[str] = field(default_factory=list)
    approval_required_from_human: bool = False
    action_summary: str = ""

    def explain(self) -> str:
        return " | ".join(self.reasons)


def authorise(
    *,
    policy: Policy,
    action_type: str,
    amount_usd: float = 0.0,
    projected_profit_usd: float | None = None,
    state: SpendState | None = None,
    is_new_supplier: bool = False,
    is_new_sku: bool = False,
    change_pct: float | None = None,
    description: str = "",
) -> AuthorisationResult:
    """Decide whether an action may proceed, and on whose authority."""
    state = state or SpendState()
    reasons: list[str] = []
    risk = policy.risk

    # 1. Categorically forbidden actions. No amount, no profit, no override.
    never = [a.lower() for a in risk.get("never_autonomous", [])]
    if action_type.lower() in never:
        return AuthorisationResult(
            permitted=False,
            decision=Decision.NEEDS_HUMAN_APPROVAL,
            reasons=[
                f"'{action_type}' is on the never-autonomous list. This class of action "
                "is irreversible or account-defining and must be performed by a human, "
                "regardless of value."
            ],
            approval_required_from_human=True,
            action_summary=description or action_type,
        )

    # 2. Never act into a known loss.
    if projected_profit_usd is not None and projected_profit_usd < 0:
        return AuthorisationResult(
            permitted=False,
            decision=Decision.REJECT,
            reasons=[
                f"Projected profit is ${projected_profit_usd:,.2f}. Policy forbids "
                "committing capital to a knowingly negative outcome."
            ],
            action_summary=description or action_type,
        )

    # 3. Hard exposure caps.
    if amount_usd > 0:
        max_single = float(risk["max_single_po_usd"])
        if amount_usd > max_single:
            reasons.append(
                f"${amount_usd:,.2f} exceeds the ${max_single:,.2f} single-transaction cap."
            )
            return AuthorisationResult(False, Decision.REJECT, reasons,
                                       action_summary=description or action_type)

        max_daily = float(risk["max_daily_spend_usd"])
        if state.spent_today_usd + amount_usd > max_daily:
            reasons.append(
                f"${amount_usd:,.2f} would take today's spend to "
                f"${state.spent_today_usd + amount_usd:,.2f}, over the ${max_daily:,.2f} "
                "daily cap."
            )
            return AuthorisationResult(False, Decision.REJECT, reasons,
                                       action_summary=description or action_type)

        max_exposure = float(risk["max_open_po_exposure_usd"])
        if state.open_po_exposure_usd + amount_usd > max_exposure:
            reasons.append(
                f"Open PO exposure would reach "
                f"${state.open_po_exposure_usd + amount_usd:,.2f}, over the "
                f"${max_exposure:,.2f} ceiling. Too much capital in transit at once."
            )
            return AuthorisationResult(False, Decision.REJECT, reasons,
                                       action_summary=description or action_type)

        buffer = float(risk["min_cash_buffer_usd"])
        if state.cash_available_usd - amount_usd < buffer:
            reasons.append(
                f"Spending ${amount_usd:,.2f} would leave "
                f"${state.cash_available_usd - amount_usd:,.2f}, under the "
                f"${buffer:,.2f} cash reserve. The reserve exists to absorb refunds "
                "and fee settlements — it is not spendable."
            )
            return AuthorisationResult(False, Decision.REJECT, reasons,
                                       action_summary=description or action_type)

    if is_new_sku:
        max_new = int(risk["max_new_skus_per_week"])
        if state.new_skus_this_week >= max_new:
            reasons.append(
                f"Already launched {state.new_skus_this_week} SKUs this week (cap {max_new}). "
                "Launch discipline beats launch volume — each SKU needs attention to "
                "reach profitability."
            )
            return AuthorisationResult(False, Decision.HOLD, reasons,
                                       action_summary=description or action_type)

    # 4. Approval thresholds.
    needs_approval = False
    thresholds = risk["approval_thresholds"]

    po_threshold = float(thresholds.get("purchase_order_usd", float("inf")))
    if amount_usd >= po_threshold:
        needs_approval = True
        reasons.append(
            f"${amount_usd:,.2f} meets the ${po_threshold:,.2f} approval threshold."
        )

    if change_pct is not None:
        pct_threshold = float(thresholds.get("price_change_pct", float("inf")))
        if abs(change_pct) >= pct_threshold:
            needs_approval = True
            reasons.append(
                f"{change_pct:+.1f}% change meets the {pct_threshold:.0f}% approval threshold."
            )

    if is_new_supplier and bool(thresholds.get("new_supplier_first_order", True)):
        needs_approval = True
        reasons.append(
            "First order with a new supplier always requires approval — this is where "
            "fraud and quality failures concentrate."
        )

    if action_type.lower() == "publish_listing" and bool(thresholds.get("listing_publish", True)):
        needs_approval = True
        reasons.append("Publishing a live listing requires human review before it goes public.")

    if action_type.lower() == "change_ad_budget" and change_pct is None:
        ad_threshold = float(thresholds.get("ad_budget_change_usd", float("inf")))
        if amount_usd >= ad_threshold:
            needs_approval = True

    if needs_approval:
        return AuthorisationResult(
            permitted=False,
            decision=Decision.NEEDS_HUMAN_APPROVAL,
            reasons=reasons,
            approval_required_from_human=True,
            action_summary=description or action_type,
        )

    # 5. Advisory-mode kill switch, checked last so that a request which is both
    # over-cap and in advisory mode reports the cap breach — the substantive
    # reason — rather than the mode flag. Anything still permitted stops here.
    is_write = amount_usd > 0 or action_type.lower() in {
        "place_purchase_order", "publish_listing", "change_price",
        "change_ad_budget", "pause_campaign",
    }
    if is_write and not policy.live_trading_enabled:
        return AuthorisationResult(
            permitted=False,
            decision=Decision.HOLD,
            reasons=reasons + [
                "meta.live_trading_enabled is false. The operator is in advisory mode: "
                "it will produce the recommendation but will not execute it. Flip the "
                "flag deliberately, after verifying credentials, to enable execution."
            ],
            approval_required_from_human=True,
            action_summary=description or action_type,
        )

    reasons.append("Within all autonomous limits.")
    return AuthorisationResult(
        permitted=True,
        decision=Decision.APPROVE,
        reasons=reasons,
        action_summary=description or action_type,
    )


def preflight_report(policy: Policy, state: SpendState) -> list[str]:
    """Human-readable snapshot of remaining headroom, for the daily report."""
    risk = policy.risk
    lines = [
        f"Mode: {'LIVE EXECUTION' if policy.live_trading_enabled else 'ADVISORY ONLY (no writes)'}",
        f"Daily spend used: ${state.spent_today_usd:,.2f} of ${float(risk['max_daily_spend_usd']):,.2f}",
        f"Open PO exposure: ${state.open_po_exposure_usd:,.2f} of ${float(risk['max_open_po_exposure_usd']):,.2f}",
        f"Cash available: ${state.cash_available_usd:,.2f} (reserve floor ${float(risk['min_cash_buffer_usd']):,.2f})",
        f"New SKUs this week: {state.new_skus_this_week} of {int(risk['max_new_skus_per_week'])}",
    ]
    spendable = money(max(0.0, state.cash_available_usd - float(risk["min_cash_buffer_usd"])))
    lines.append(f"Deployable capital right now: ${spendable:,.2f}")
    return lines
