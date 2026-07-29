"""The TikTok Shop daily run.

TikTok is the primary marketplace, so this — not `pipeline.run_daily` — is the
operator's main loop. It reads whatever is available, scores it, decides, and
journals every decision with a dedupe key so re-running a day is idempotent.

The ordering is deliberate and is the part worth reading:

  1. Store health first. If the shop is in trouble, nothing below matters and
     several downstream actions get suppressed rather than merely annotated.
  2. Signals and trends, so scoring has evidence rather than arithmetic.
  3. Profit from settlements where available, estimates where not.
  4. Capital allocation, which consumes the scores above.
  5. Content, last, because commissioning video for a product that failed
     screening wastes production budget.

Every stage degrades to "unknown" rather than to a default. A stage with no
data contributes nothing and says so; it never contributes a plausible number.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from connectors import all_status

from .account_health import assess_tiktok, blocks_scaling
from .capital import (
    AllocationRequest,
    CapitalState,
    Position,
    allocate,
    concentration_report,
    portfolio_turns,
)
from .config import Policy
from .economics import economics_for_candidate, lifetime_value, max_acquisition_cost
from .models import ProductCandidate, today_iso
from .pipeline import load_candidates, load_operations
from .scoring import build_scorecard, rank
from .signals import (
    Signal,
    SignalDirection,
    assess_confidence,
    available_weight,
    signal_from_tiktok_velocity,
    unavailable_sources_report,
)
from .store import Store
from .tiktok import (
    TREND_DEAD,
    TREND_GROWING,
    TREND_SPIKE_DECAY,
    DailyPoint,
    analyse_trend,
    build_optimisation_report,
    compute_profit,
)


@dataclass
class TikTokDailyResult:
    """Everything one daily run produced, for reporting and for tests."""

    run_date: str
    data_source: str
    health: Any
    scaling_blocked: bool
    scaling_note: str
    trends: dict[str, Any] = field(default_factory=dict)
    profits: dict[str, Any] = field(default_factory=dict)
    scorecards: list[Any] = field(default_factory=list)
    confidence: dict[str, Any] = field(default_factory=dict)
    actions: list[Any] = field(default_factory=list)
    allocation: list[Any] = field(default_factory=list)
    allocation_notes: list[str] = field(default_factory=list)
    concentration: dict[str, Any] = field(default_factory=dict)
    turns: dict[str, Any] = field(default_factory=dict)
    ltv: dict[str, Any] = field(default_factory=dict)
    repeat_stats: dict[str, Any] = field(default_factory=dict)
    signal_coverage_pct: float = 0.0
    unavailable_signals: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    decisions_journaled: int = 0


def _load_trends(policy: Policy, ops: dict[str, Any]) -> dict[str, Any]:
    min_days = int(policy.tiktok["min_trend_history_days"])
    out: dict[str, Any] = {}
    for pid, rows in (ops.get("tiktok_daily_performance") or {}).items():
        if pid.startswith("_") or not isinstance(rows, list) or not rows:
            continue
        points = [
            DailyPoint(
                day=r["day"], units=int(r.get("units", 0)),
                gmv=float(r.get("gmv", 0.0)),
                page_views=int(r.get("page_views", 0)),
                orders=int(r.get("orders", 0)),
            )
            for r in rows
        ]
        out[pid] = analyse_trend(
            product_id=pid, title=rows[0].get("title", pid),
            history=points, min_days=min_days,
        )
    return out


def _stored_signals(store: Store, sku: str, *, today: date,
                    max_age_days: int) -> list[Signal]:
    """Load human-observed signals for a SKU from the journal."""
    since = (today - timedelta(days=max_age_days)).isoformat()
    out: list[Signal] = []
    for row in store.signals_for_sku(sku, since=since):
        try:
            direction = SignalDirection(row["direction"])
        except ValueError:
            continue
        out.append(Signal(
            source=row["source"], direction=direction,
            strength=float(row["strength"]), observed_at=row["observed_at"],
            detail=row.get("detail", ""),
            raw={"origin": row.get("origin", "manual"),
                 "observer": row.get("observer", "")},
        ))
    return out


def run_tiktok_daily(
    policy: Policy,
    store: Store,
    *,
    run_date: str | None = None,
    operations_path: Path | None = None,
    candidates_path: Path | None = None,
    journal: bool = True,
) -> TikTokDailyResult:
    """Execute one TikTok Shop day."""
    run_date = run_date or today_iso()
    today = date.fromisoformat(run_date)
    ops, source = load_operations(operations_path)
    notes: list[str] = []
    journaled = 0

    # ---- 1. store health, first ------------------------------------------
    health = assess_tiktok(policy, ops.get("tiktok_health", {}))
    scaling_blocked, scaling_note = blocks_scaling(health)
    if journal and (health.breaches or health.warnings):
        store.record_decision(
            domain="store_health", sku="_shop",
            action="hold_scaling" if scaling_blocked else "monitor",
            rationale=scaling_note + " | " + " ".join(health.actions[:2]),
            decision="HOLD" if scaling_blocked else "APPROVE",
            inputs={"severity": health.severity.value,
                    "breaches": [m.label for m in health.breaches],
                    "coverage_pct": health.coverage_pct},
            expected_outcome="Metrics recover before spend increases.",
            requires_approval=bool(health.breaches),
            dedupe_key=f"{run_date}|store_health|_shop|assess",
        )
        journaled += 1

    # ---- 2. trends and signals -------------------------------------------
    trends = _load_trends(policy, ops)
    max_age = int(policy.signals["max_signal_age_days"])

    confidence: dict[str, Any] = {}
    candidates = load_candidates(candidates_path)
    by_pid = {c.sku: c for c in candidates}
    # Seed data keys TikTok performance by product_id; map through when the
    # operations file supplies a mapping, otherwise fall back to the SKU.
    pid_to_sku = ops.get("tiktok_product_sku_map", {}) or {}

    for cand in candidates:
        signals: list[Signal] = []

        pid = next((p for p, s in pid_to_sku.items() if s == cand.sku), None)
        trend = trends.get(pid) if pid else trends.get(cand.sku)
        if trend is not None:
            signals.append(signal_from_tiktok_velocity(
                recent_daily_units=trend.recent_daily_units,
                prior_daily_units=trend.prior_daily_units,
                trend_shape=trend.shape,
                conversion_rate_pct=trend.conversion_rate_pct,
                observed_at=run_date,
            ))

        signals.extend(_stored_signals(store, cand.sku, today=today,
                                       max_age_days=max_age))
        confidence[cand.sku] = assess_confidence(policy, cand.sku, signals,
                                                 today=today)

    # ---- 3. profit --------------------------------------------------------
    costs = ops.get("tiktok_costs", {}) or {}
    settlements = ops.get("tiktok_settlements", {}) or {}
    profits: dict[str, Any] = {}
    for pid, trend in trends.items():
        rows = (ops.get("tiktok_daily_performance") or {}).get(pid, [])
        units = sum(int(r.get("units", 0)) for r in rows)
        gmv = sum(float(r.get("gmv", 0.0)) for r in rows)
        c = costs.get(pid, {})
        profits[pid] = compute_profit(
            policy=policy, product_id=pid, units=units, gross_revenue=gmv,
            cogs_per_unit=float(c.get("cogs_per_unit", 0.0)),
            shipping_per_unit=float(c.get("shipping_per_unit", 0.0)),
            ad_cost=float(c.get("ad_cost", 0.0)),
            settlement=settlements.get(pid),
            affiliate_rate_pct=c.get("affiliate_rate_pct"),
        )

    estimated = [p for p in profits.values() if p.revenue_basis == "estimated"]
    if estimated:
        notes.append(
            f"{len(estimated)} product(s) priced from estimated fees rather than "
            "settlements. Run `tiktok-settlements` once connected — TikTok's real "
            "take rate is routinely several points above the headline commission."
        )

    # ---- 4. scorecards ----------------------------------------------------
    reviews = ops.get("reviews", {}) or {}
    scorecards = []
    for cand in candidates:
        econ = economics_for_candidate(policy, cand)
        revs = reviews.get(cand.sku, [])
        avg = (sum(r["rating"] for r in revs) / len(revs)) if revs else None
        pid = next((p for p, s in pid_to_sku.items() if s == cand.sku), None)
        trend = trends.get(pid) if pid else trends.get(cand.sku)
        scorecards.append(build_scorecard(
            policy, cand, econ,
            trend_shape=trend.shape if trend else None,
            trend_change_pct=trend.change_pct if trend else None,
            avg_rating=avg, review_count=len(revs),
            settlement_lag_days=15,
            marketplace="tiktok",
        ))
    scorecards = rank(scorecards)

    # Reconcile the scorecard against the evidence behind it.
    #
    # The scorecard measures the product; confidence measures how much we
    # actually know about it. They can disagree — a product can look excellent
    # on arithmetic while its only live signal is negative — and a PURSUE
    # verdict on thin or contradicted evidence is precisely how money gets
    # committed to something nobody checked. The charter's multi-signal rule is
    # enforced in screening, and it has to be enforced here too or the
    # scorecard path silently routes around it.
    for card in scorecards:
        conf = confidence.get(card.sku)
        if conf is None or card.verdict not in ("PURSUE", "INVESTIGATE"):
            continue
        if conf.sufficient:
            continue
        original = card.verdict
        card.verdict = "INVESTIGATE" if original == "PURSUE" else "HOLD"
        card.notes.append(
            f"Downgraded {original} -> {card.verdict}: the scorecard is strong but "
            f"the evidence is not ({conf.summary()}). "
            + ("The only live signal points the wrong way — "
               if conf.negative_signals and not conf.positive_signals else "")
            + "A high score on partial or contradicted evidence is a claim about "
            "arithmetic, not about demand."
        )
        notes.append(
            f"{card.sku}: {original} downgraded to {card.verdict} on evidence "
            f"({conf.positive_signals} positive / {conf.negative_signals} negative "
            f"signals, {conf.coverage_pct:.0f}% coverage)."
        )

    if journal:
        for card in scorecards:
            conf = confidence.get(card.sku)
            store.record_decision(
                domain="sourcing", sku=card.sku,
                action=f"score -> {card.verdict}",
                rationale=(card.summary()
                           + (f" | capped by {card.capped_by}" if card.capped_by else "")),
                decision=("NEEDS_HUMAN_APPROVAL" if card.verdict == "PURSUE"
                          else "HOLD" if card.verdict in ("INVESTIGATE", "HOLD")
                          else "REJECT"),
                inputs={"overall": card.overall, "coverage_pct": card.coverage_pct,
                        "confidence": conf.score if conf else None,
                        "dimensions": {d.name: d.value for d in card.dimensions}},
                expected_outcome=(
                    f"Verdict {card.verdict} at {card.coverage_pct:.0f}% coverage."),
                requires_approval=card.verdict == "PURSUE",
                dedupe_key=f"{run_date}|sourcing|{card.sku}|score",
            )
            journaled += 1

    # ---- 5. optimisation actions -----------------------------------------
    inventory_rows = [
        {"product_id": pid, "on_hand_units": int(costs.get(pid, {}).get("on_hand_units", 0))}
        for pid in trends
    ]
    take_rate = ops.get("tiktok_observed_take_rate_pct")
    actions, opt_notes = build_optimisation_report(
        policy=policy, trends=list(trends.values()), profits=profits,
        inventory=inventory_rows, take_rate_pct=take_rate,
    )
    notes.extend(opt_notes)

    if scaling_blocked:
        # Store health outranks growth. Suppress the actions that pour more
        # volume through a process that is already failing, rather than merely
        # noting the problem alongside them.
        before = len(actions)
        actions = [a for a in actions
                   if "Scale" not in a.action and "promotion" not in a.action.lower()]
        if len(actions) < before:
            notes.append(
                f"Suppressed {before - len(actions)} scaling action(s): {scaling_note}"
            )

    if journal:
        for a in actions:
            store.record_decision(
                domain="tiktok_ops", sku=a.product_id,
                action=a.action, rationale=a.rationale,
                decision="NEEDS_HUMAN_APPROVAL" if a.requires_approval else "APPROVE",
                inputs={"priority": a.priority,
                        "impact_usd": a.estimated_impact_usd},
                expected_outcome=f"Impact ~${a.estimated_impact_usd:,.2f}",
                requires_approval=a.requires_approval,
                dedupe_key=f"{run_date}|tiktok_ops|{a.product_id}|{a.action[:40]}",
            )
            journaled += 1

    # ---- 6. capital -------------------------------------------------------
    positions = [
        Position(
            sku=x["sku"], category=x.get("category", "uncategorised"),
            supplier_id=x.get("supplier_id", "unknown"),
            units_on_hand=int(x["on_hand_units"]),
            units_inbound=int(x.get("inbound_units", 0)),
            unit_cost=float(x.get("unit_cost", 0.0)),
            annual_units_sold=float(x.get("daily_velocity", 0.0)) * 365,
        )
        for x in ops.get("inventory", [])
    ]
    capital_state = CapitalState(
        total_capital_usd=float(policy.capital["total_capital_usd"]),
        cash_available_usd=float(
            ops.get("spend_state", {}).get("cash_available_usd", 0.0)),
        positions=positions,
    )

    requests: list[AllocationRequest] = []
    for card in scorecards:
        if card.verdict not in ("PURSUE", "INVESTIGATE"):
            continue
        cand = by_pid.get(card.sku)
        if cand is None:
            continue
        econ = economics_for_candidate(policy, cand)
        conf = confidence.get(card.sku)
        src = next((x for x in ops.get("inventory", []) if x["sku"] == card.sku), {})
        requests.append(AllocationRequest(
            sku=card.sku,
            category=src.get("category", cand.category),
            supplier_id=src.get("supplier_id", cand.supplier.supplier_id),
            amount_usd=float(cand.supplier.moq) * cand.supplier.landed_unit_cost,
            expected_roi_pct=econ.roi_pct,
            confidence_pct=conf.effective_score if conf else 0.0,
            cash_cycle_days=cand.supplier.shipping_days + 45 + 15,
            is_new_sku=True,
            rationale=card.verdict,
        ))

    allocation, alloc_notes = allocate(policy, capital_state, requests)
    if journal:
        for d in allocation:
            store.record_decision(
                domain="capital", sku=d.request.sku,
                action=f"allocate ${d.approved_amount:,.2f}",
                rationale="; ".join(d.reasons),
                decision="APPROVE" if d.accepted else "REJECT",
                inputs={"risk_adjusted_roi": d.risk_adjusted_roi_pct,
                        "rank": d.rank},
                expected_outcome=(
                    f"Risk-adjusted {d.risk_adjusted_roi_pct:.0f}% over "
                    f"{d.request.cash_cycle_days}d."),
                dedupe_key=f"{run_date}|capital|{d.request.sku}|allocate",
            )
            journaled += 1

    # ---- 7. lifetime value ------------------------------------------------
    repeat_stats = store.repeat_purchase_stats()
    ltv_by_sku: dict[str, Any] = {}
    for pid, profit in profits.items():
        if profit.units <= 0:
            continue
        per_order = profit.pre_tax_profit / profit.units
        value = lifetime_value(
            policy=policy, first_order_profit=per_order,
            repeat_rate_pct=repeat_stats.get("repeat_rate_pct"),
        )
        ltv_by_sku[pid] = {
            "first_order_profit": value.first_order_profit,
            "lifetime_profit": value.lifetime_profit,
            "basis": value.basis,
            "max_cac": max_acquisition_cost(value),
        }
    if repeat_stats.get("repeat_rate_pct") is None:
        notes.append(repeat_stats["note"])

    coverage = available_weight(policy)

    return TikTokDailyResult(
        run_date=run_date,
        data_source=source,
        health=health,
        scaling_blocked=scaling_blocked,
        scaling_note=scaling_note,
        trends=trends,
        profits=profits,
        scorecards=scorecards,
        confidence=confidence,
        actions=actions,
        allocation=allocation,
        allocation_notes=alloc_notes,
        concentration=concentration_report(policy, capital_state),
        turns=portfolio_turns(policy, capital_state),
        ltv=ltv_by_sku,
        repeat_stats=repeat_stats,
        signal_coverage_pct=coverage,
        unavailable_signals=unavailable_sources_report(policy),
        notes=notes,
        decisions_journaled=journaled,
    )


def render_tiktok_daily(policy: Policy, result: TikTokDailyResult) -> str:
    """Render the TikTok daily run as markdown."""
    p: list[str] = []
    biz = policy.meta.get("business_name", "Shop")

    p.append(f"# TikTok Shop Daily Run — {result.run_date}")
    p.append(f"**{biz}** · primary marketplace: TikTok Shop")
    p.append("")

    if result.data_source != "live":
        p.append("> ## ⚠️ THESE ARE NOT REAL BUSINESS NUMBERS")
        p.append(">")
        p.append(
            f"> Data source: **{result.data_source.upper()}**. TikTok Shop is not "
            "connected, so no live orders, inventory, settlements, or analytics "
            "could be read. Every figure below is seed data."
        )
        p.append("")

    mode = "LIVE EXECUTION" if policy.live_trading_enabled else "ADVISORY ONLY"
    p.append(f"**Operating mode:** {mode} · "
             f"**{result.decisions_journaled}** decisions journaled")
    p.append("")

    # 1. Store health
    h = result.health
    icon = {"CRITICAL": "🚨", "WARN": "⚠️", "INFO": "✓"}.get(h.severity.value, "")
    p.append("## 1. Store Health")
    p.append(f"{icon} **{h.severity.value}** · {h.coverage_pct:.0f}% of metrics measured")
    p.append("")
    p.append("| Metric | Value | Limit | Utilisation |")
    p.append("|---|---:|---:|---:|")
    for m in h.metrics:
        value = f"{m.value:g}{m.unit}" if m.known else "unknown"
        util = f"{m.utilisation_pct:.0f}%" if m.utilisation_pct is not None else "—"
        p.append(f"| {m.label} | {value} | {m.limit:g}{m.unit} | {util} |")
    p.append("")
    for a in h.actions:
        p.append(f"- {a}")
    if result.scaling_blocked:
        p.append("")
        p.append(f"**Scaling is on hold.** {result.scaling_note}")
    p.append("")

    # 2. Profit
    p.append("## 2. Profit by Product")
    if not result.profits:
        p.append("_No performance data._")
    else:
        p.append("")
        p.append("| Product | Units | GMV | Net | Margin | Take | Basis |")
        p.append("|---|---:|---:|---:|---:|---:|---|")
        for pid, pr in result.profits.items():
            title = result.trends[pid].title if pid in result.trends else pid
            p.append(f"| {title[:26]} | {pr.units} | ${pr.gross_revenue:,.2f} | "
                     f"${pr.pre_tax_profit:,.2f} | {pr.margin_pct:.1f}% | "
                     f"{pr.take_rate_pct:.1f}% | {pr.revenue_basis} |")
        total_pre = sum(x.pre_tax_profit for x in result.profits.values())
        total_post = sum(x.after_tax_profit for x in result.profits.values())
        p.append("")
        p.append(f"- Pre-tax **${total_pre:,.2f}** · "
                 f"**after-tax ${total_post:,.2f}** (the KPI)")
    p.append("")

    # 3. Trends
    p.append("## 3. Demand Curves")
    if not result.trends:
        p.append("_No daily history._")
    else:
        p.append("")
        p.append("| Product | Shape | Units/day | Change | Since peak |")
        p.append("|---|---|---:|---:|---:|")
        for t in result.trends.values():
            p.append(f"| {t.title[:26]} | {t.shape} | {t.recent_daily_units:.1f} | "
                     f"{t.change_pct:+.0f}% | {t.days_since_peak}d |")
        p.append("")
        for t in result.trends.values():
            if t.shape in (TREND_SPIKE_DECAY, TREND_DEAD):
                p.append(f"- **{t.title}** — {t.interpretation}")
    p.append("")

    # 4. Scorecard
    p.append("## 4. Product Scorecard")
    p.append("")
    p.append("| SKU | Verdict | Score | Coverage | Confidence | Capped by |")
    p.append("|---|---|---:|---:|---:|---|")
    for card in result.scorecards:
        conf = result.confidence.get(card.sku)
        conf_cell = f"{conf.score:.0f}" if conf else "—"
        p.append(f"| {card.sku} | {card.verdict} | {card.overall:.0f} | "
                 f"{card.coverage_pct:.0f}% | {conf_cell} | "
                 f"{card.capped_by or '—'} |")
    p.append("")

    # 5. Actions
    p.append("## 5. Recommended Actions")
    if not result.actions:
        p.append("_Nothing requiring action today._")
    for i, a in enumerate(result.actions, 1):
        flag = " **[NEEDS APPROVAL]**" if a.requires_approval else ""
        p.append(f"{i}. **[{a.priority}] {a.title}** — {a.action}{flag}")
        p.append(f"   - {a.rationale}")
        if a.estimated_impact_usd:
            p.append(f"   - Impact ~${a.estimated_impact_usd:,.2f}")
    p.append("")

    # 6. Capital
    p.append("## 6. Capital")
    if result.turns:
        t = result.turns
        p.append(f"- Inventory turns **{t['portfolio_turns']:.2f}/yr** "
                 f"(target {t['target_turns']:.1f})")
        if t.get("dead"):
            p.append(f"- Dead stock: {', '.join(t['dead'])} "
                     f"(${t['capital_in_dead']:,.2f})")
    for b in result.concentration.get("breaches", []):
        p.append(f"- ⚠️ {b['dimension']} `{b['value']}` at {b['share_pct']:.0f}% "
                 f"(limit {b['limit_pct']:.0f}%)")
    if result.allocation:
        p.append("")
        p.append("| SKU | Requested | Funded | Risk-adj ROI |")
        p.append("|---|---:|---:|---:|")
        for d in result.allocation:
            p.append(f"| {d.request.sku} | ${d.request.amount_usd:,.0f} | "
                     f"${d.approved_amount:,.0f} | {d.risk_adjusted_roi_pct:.0f}% |")
    for n in result.allocation_notes:
        p.append(f"- {n}")
    p.append("")

    # 7. LTV
    p.append("## 7. Customer Value")
    if result.repeat_stats.get("repeat_rate_pct") is None:
        p.append(f"_{result.repeat_stats['note']}_")
    else:
        p.append(f"- Repeat rate **{result.repeat_stats['repeat_rate_pct']:.1f}%** "
                 f"across {result.repeat_stats['buyers_identified']} identified buyers")
        p.append("")
        p.append("| Product | First-order profit | LTV | Max CAC (3:1) |")
        p.append("|---|---:|---:|---:|")
        for pid, v in result.ltv.items():
            title = result.trends[pid].title if pid in result.trends else pid
            p.append(f"| {title[:26]} | ${v['first_order_profit']:,.2f} | "
                     f"${v['lifetime_profit']:,.2f} | ${v['max_cac']:,.2f} |")
    p.append("")

    # 8. Signals
    p.append("## 8. Signal Coverage")
    p.append(f"**{result.signal_coverage_pct:.0f}%** of the weighted signal set is "
             "observable. Confidence scores and rankings are only as good as this.")
    if result.unavailable_signals:
        p.append("")
        p.append("Not connected:")
        p.append("")
        for line in result.unavailable_signals:
            p.append(f"- {line}")
        p.append("")
        p.append("Sources marked *manually observable* can be logged with "
                 "`signal <sku> <source> --strength N` after checking the app. "
                 "Nothing here is ever estimated.")
    p.append("")

    if result.notes:
        p.append("## 9. Notes")
        for n in result.notes:
            p.append(f"- {n}")
        p.append("")

    p.append("---")
    p.append("_Generated by the autonomous ecommerce operator. Nothing above was "
             "executed against a live TikTok Shop._")
    return "\n".join(p)


def write_tiktok_daily(policy: Policy, content: str, run_date: str) -> Path:
    out_dir = Path(policy.reporting.get("output_dir", "reports"))
    if not out_dir.is_absolute():
        out_dir = Path(__file__).resolve().parent.parent / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"tiktok-daily-{run_date}.md"
    path.write_text(content, encoding="utf-8")
    return path
