"""The daily run: loads data, executes every engine, journals every decision.

Data loading is deliberately explicit about provenance. `load_operations`
returns the source tag alongside the data, and the pipeline carries it through
to the report. There is no code path that lets seed data reach a report
without being labelled.
"""

from __future__ import annotations

import json
from datetime import date
from dataclasses import asdict
from pathlib import Path
from typing import Any

from connectors import all_status

from .advertising import (
    AdReview,
    KeywordStat,
    portfolio_ad_summary,
    review_campaign,
    review_keywords,
)
from .config import Policy
from .economics import after_tax_roi_pct, economics_for_candidate
from .inventory import plan_all
from .models import (
    Campaign,
    CompetitorOffer,
    InventoryItem,
    ProductCandidate,
    Review,
    Supplier,
    today_iso,
)
from .account_health import assess as assess_account_health, blocks_scaling
from .capital import (
    AllocationRequest,
    CapitalState,
    Position,
    allocate,
    concentration_report,
    portfolio_turns,
)
from .pricing import recommend_price
from .signals import (
    Signal,
    SignalDirection,
    assess_confidence,
    available_weight,
    signal_from_sales_rank,
    unavailable_sources_report,
)
from .suppliers import detect_deterioration
from .reporting import ReportContext, build_daily_report, prior_period_dates, write_report
from .reviews import analyse_reviews
from .risk import SpendState
from .screening import screen_all
from .store import Store

SEED_DIR = Path(__file__).resolve().parent.parent / "data" / "seed"


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------
def _supplier_from(d: dict[str, Any]) -> Supplier:
    return Supplier(
        supplier_id=d["supplier_id"],
        name=d["name"],
        country=d.get("country", ""),
        rating=float(d["rating"]),
        unit_cost=float(d["unit_cost"]),
        moq=int(d.get("moq", 0)),
        shipping_cost_per_unit=float(d.get("shipping_cost_per_unit", 0.0)),
        shipping_days=int(d.get("shipping_days", 0)),
        domestic_stock=bool(d.get("domestic_stock", False)),
        quality_score=float(d.get("quality_score", 0.0)),
        communication_score=float(d.get("communication_score", 0.0)),
        inventory_stability=float(d.get("inventory_stability", 0.0)),
        defect_rate_pct=float(d.get("defect_rate_pct", 0.0)),
        on_time_rate_pct=float(d.get("on_time_rate_pct", 100.0)),
        tooling_fee=float(d.get("tooling_fee", 0.0)),
        notes=d.get("notes", ""),
    )


def load_candidates(path: Path | None = None) -> list[ProductCandidate]:
    p = path or SEED_DIR / "candidates.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    out = []
    for d in raw:
        out.append(
            ProductCandidate(
                sku=d["sku"],
                title=d["title"],
                category=d["category"],
                marketplace=d["marketplace"],
                target_price=float(d["target_price"]),
                supplier=_supplier_from(d["supplier"]),
                est_monthly_demand_units=int(d.get("est_monthly_demand_units", 0)),
                competitor_count=int(d.get("competitor_count", 0)),
                top_rival_review_count=int(d.get("top_rival_review_count", 0)),
                avg_rival_price=float(d.get("avg_rival_price", 0.0)),
                avg_rival_rating=float(d.get("avg_rival_rating", 0.0)),
                weight_lb=float(d.get("weight_lb", 1.0)),
                duty_pct=float(d.get("duty_pct", 0.0)),
                keywords=d.get("keywords", []),
                description=d.get("description", ""),
                brand=d.get("brand", ""),
                is_gated_category=bool(d.get("is_gated_category", False)),
                gated_approval_on_file=bool(d.get("gated_approval_on_file", False)),
            )
        )
    return out


def load_operations(path: Path | None = None) -> tuple[dict[str, Any], str]:
    """Return (data, provenance). Provenance is never 'live' from a seed file."""
    p = path or SEED_DIR / "operations.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    return data, "seed"


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------
def run_daily(
    policy: Policy,
    store: Store,
    *,
    report_date: str | None = None,
    candidates_path: Path | None = None,
    operations_path: Path | None = None,
    journal: bool = True,
) -> tuple[str, Path]:
    report_date = report_date or today_iso()
    ops, source = load_operations(operations_path)

    # ---- market signals -------------------------------------------------
    # Signals come from the seed file only when present. Nothing is synthesised:
    # a candidate with no signal entry gets an empty list and scores 0
    # confidence, which correctly reads as "unknown", not "bad".
    confidence_by_sku = {}
    raw_signals = ops.get("signals", {})
    for sku, entries in raw_signals.items():
        # `_`-prefixed keys are documentation in the seed file, not SKUs.
        if sku.startswith("_") or not isinstance(entries, list):
            continue
        sigs = [
            Signal(
                source=e["source"],
                direction=SignalDirection(e.get("direction", "NEUTRAL")),
                strength=float(e.get("strength", 0.0)),
                observed_at=e.get("observed_at", report_date),
                detail=e.get("detail", ""),
            )
            for e in entries
        ]
        confidence_by_sku[sku] = assess_confidence(
            policy, sku, sigs, today=date.fromisoformat(report_date)
        )

    # ---- sourcing ------------------------------------------------------
    candidates = load_candidates(candidates_path)
    by_sku_all = {c.sku: c for c in candidates}
    screened = screen_all(policy, candidates, confidence_by_sku=confidence_by_sku)
    if journal:
        for r in screened:
            store.record_decision(
                domain="sourcing",
                sku=r.sku,
                action=f"screen_candidate -> {r.decision.value}",
                rationale=r.reason_summary(),
                decision=r.decision.value,
                inputs={
                    "score": r.score,
                    "roi_pct": r.economics.roi_pct if r.economics else None,
                    "margin_pct": r.economics.margin_pct if r.economics else None,
                    "after_tax_roi_pct": (
                        after_tax_roi_pct(policy, r.economics) if r.economics else None),
                    "confidence": (
                        confidence_by_sku[r.sku].score if r.sku in confidence_by_sku else None),
                },
                expected_outcome=(
                    f"If sourced, expect ~{r.economics.margin_pct:.0f}% margin at "
                    f"${r.economics.sale_price:.2f}" if r.economics else ""
                ),
                requires_approval=r.decision.value == "NEEDS_HUMAN_APPROVAL",
                dedupe_key=f"{report_date}|sourcing|{r.sku}|screen",
            )

    # ---- inventory -----------------------------------------------------
    items = [
        InventoryItem(
            sku=i["sku"], marketplace=i["marketplace"],
            on_hand_units=int(i["on_hand_units"]), inbound_units=int(i.get("inbound_units", 0)),
            daily_velocity=float(i["daily_velocity"]),
            velocity_stddev=float(i.get("velocity_stddev", 0.0)),
            lead_time_days=int(i.get("lead_time_days", 30)),
            unit_cost=float(i.get("unit_cost", 0.0)), moq=int(i.get("moq", 50)),
        )
        for i in ops.get("inventory", [])
    ]
    plans = plan_all(policy, items)
    if journal:
        for plan in plans:
            if plan.recommended_order_units > 0:
                store.record_decision(
                    domain="inventory", sku=plan.sku,
                    action=f"reorder {plan.recommended_order_units} units",
                    rationale=" ".join(plan.rationale),
                    decision="NEEDS_HUMAN_APPROVAL" if plan.requires_approval else "APPROVE",
                    inputs={"days_of_cover": plan.days_of_cover, "status": plan.status,
                            "cost": plan.estimated_order_cost},
                    expected_outcome=(
                        f"Maintain in-stock through the {plan.reorder_point_units}-unit "
                        "reorder point without a stockout."
                    ),
                    requires_approval=plan.requires_approval,
                    dedupe_key=f"{report_date}|inventory|{plan.sku}|reorder",
                )

    # ---- advertising ---------------------------------------------------
    ad_reviews: list[AdReview] = []
    ad_actions: list[Any] = []
    for c in ops.get("campaigns", []):
        camp = Campaign(
            campaign_id=c["campaign_id"], name=c["name"], sku=c["sku"],
            marketplace=c["marketplace"], spend=float(c["spend"]), sales=float(c["sales"]),
            clicks=int(c["clicks"]), impressions=int(c["impressions"]),
            orders=int(c["orders"]), daily_budget=float(c["daily_budget"]),
            period_days=int(c.get("period_days", 30)),
        )
        rv = review_campaign(policy, camp, product_margin_pct=float(c.get("product_margin_pct", 30.0)))
        ad_reviews.append(rv)
        ad_actions.extend(rv.actions)

    kws = [
        KeywordStat(
            keyword=k["keyword"], match_type=k["match_type"], clicks=int(k["clicks"]),
            impressions=int(k["impressions"]), spend=float(k["spend"]),
            sales=float(k["sales"]), orders=int(k["orders"]),
            current_bid=float(k["current_bid"]),
        )
        for k in ops.get("keywords", [])
    ]
    if kws:
        ad_actions.extend(review_keywords(policy, kws, break_even_acos_pct=31.5))

    if journal:
        for a in ad_actions:
            store.record_decision(
                domain="advertising", sku=a.target, action=f"{a.action} {a.target}",
                rationale=a.rationale, decision="NEEDS_HUMAN_APPROVAL" if a.requires_approval else "APPROVE",
                inputs={"from": a.current_value, "to": a.proposed_value},
                expected_outcome=f"Monthly impact ~${a.projected_monthly_impact:,.2f}",
                requires_approval=a.requires_approval,
                dedupe_key=f"{report_date}|advertising|{a.target}|{a.action}",
            )

    # ---- pricing -------------------------------------------------------
    price_recs = []
    by_sku = by_sku_all
    for sku, offers in ops.get("competitors", {}).items():
        cand = by_sku.get(sku)
        if not cand:
            continue
        comps = [
            CompetitorOffer(
                seller=o["seller"], price=float(o["price"]), rating=float(o.get("rating", 0)),
                review_count=int(o.get("review_count", 0)),
                is_prime=bool(o.get("is_prime", False)), in_stock=bool(o.get("in_stock", True)),
                is_buybox=bool(o.get("is_buybox", False)),
            )
            for o in offers
        ]
        rec = recommend_price(
            policy=policy, sku=sku, marketplace=cand.marketplace,
            current_price=cand.target_price, supplier=cand.supplier, competitors=comps,
            duty_pct=cand.duty_pct, ad_cost_per_unit=cand.target_price * 0.10,
            our_rating=4.4, our_review_count=120,
        )
        price_recs.append(rec)
        if journal and rec.action != "HOLD":
            store.record_decision(
                domain="pricing", sku=sku,
                action=f"{rec.action} price {rec.current_price:.2f} -> {rec.recommended_price:.2f}",
                rationale=" ".join(rec.rationale),
                decision="NEEDS_HUMAN_APPROVAL" if rec.requires_approval else "APPROVE",
                inputs={"floor": rec.floor_price, "target": rec.target_price},
                expected_outcome=f"Margin {rec.projected_margin_pct:.1f}% at new price",
                requires_approval=rec.requires_approval,
                dedupe_key=f"{report_date}|pricing|{sku}|{rec.action}",
            )
            store.record_price_change(
                sku=sku, marketplace=cand.marketplace, old_price=rec.current_price,
                new_price=rec.recommended_price, reason=rec.action,
                applied=False,
            )

    # ---- reviews -------------------------------------------------------
    insights = []
    for sku, revs in ops.get("reviews", {}).items():
        mp = next((i["marketplace"] for i in ops.get("inventory", []) if i["sku"] == sku), "amazon")
        parsed = [
            Review(
                review_id=r["review_id"], sku=sku, marketplace=mp, rating=int(r["rating"]),
                title=r.get("title", ""), body=r.get("body", ""),
                created_at=r.get("created_at", ""),
            )
            for r in revs
        ]
        ins = analyse_reviews(sku, mp, parsed)
        insights.append(ins)
        if journal and ins.severity.value == "CRITICAL":
            store.record_decision(
                domain="reviews", sku=sku, action="escalate_review_risk",
                rationale="; ".join(ins.recommended_actions),
                decision="NEEDS_HUMAN_APPROVAL",
                inputs={"avg_rating": ins.average_rating, "risk_hits": len(ins.account_risk_hits)},
                expected_outcome="Human reads flagged reviews and decides on delisting/recall.",
                requires_approval=True,
                dedupe_key=f"{report_date}|reviews|{sku}|escalate",
            )

    # ---- metrics -------------------------------------------------------
    prior_date = prior_period_dates(report_date)
    for row in ops.get("daily_metrics", []):
        store.upsert_daily_metric(metric_date=report_date, data_source=source, **row)
    for row in ops.get("prior_daily_metrics", []):
        store.upsert_daily_metric(metric_date=prior_date, data_source=source, **row)

    ss = ops.get("spend_state", {})
    spend_state = SpendState(
        spent_today_usd=float(ss.get("spent_today_usd", 0.0)),
        open_po_exposure_usd=float(ss.get("open_po_exposure_usd", 0.0)),
        cash_available_usd=float(ss.get("cash_available_usd", 0.0)),
        new_skus_this_week=int(ss.get("new_skus_this_week", 0)),
    )

    # ---- account health -------------------------------------------------
    health = assess_account_health(policy, "amazon", ops.get("account_health", {}))
    hold_scaling, scaling_note = blocks_scaling(health)
    if hold_scaling:
        # Account health outranks growth: more volume through a failing process
        # produces more defects, not more profit.
        ad_actions = [a for a in ad_actions if a.action != "INCREASE_BUDGET"]
        if journal:
            store.record_decision(
                domain="account_health", sku="_account",
                action="hold_scaling",
                rationale=scaling_note,
                decision="HOLD",
                inputs={"severity": health.severity.value,
                        "breaches": [m.label for m in health.breaches]},
                expected_outcome="Metrics recover before spend increases.",
                requires_approval=bool(health.breaches),
                dedupe_key=f"{report_date}|account_health|_account|hold",
            )

    # ---- capital --------------------------------------------------------
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
        cash_available_usd=float(ss.get("cash_available_usd", 0.0)),
        positions=positions,
    )

    requests = []
    for plan in plans:
        if plan.recommended_order_units <= 0 or plan.estimated_order_cost <= 0:
            continue
        src = next((x for x in ops.get("inventory", []) if x["sku"] == plan.sku), {})
        cand = by_sku_all.get(plan.sku)
        conf = confidence_by_sku.get(plan.sku)
        requests.append(AllocationRequest(
            sku=plan.sku,
            category=src.get("category", "uncategorised"),
            supplier_id=src.get("supplier_id", "unknown"),
            amount_usd=plan.estimated_order_cost,
            expected_roi_pct=(
                economics_for_candidate(policy, cand).roi_pct if cand else 40.0),
            # No signal data means low confidence, not average confidence.
            confidence_pct=conf.effective_score if conf else 35.0,
            cash_cycle_days=int(src.get("lead_time_days", 30)) + int(plan.days_of_cover or 45),
            rationale=plan.status,
        ))

    allocation_decisions, allocation_notes = allocate(policy, capital_state, requests)
    if journal:
        for d in allocation_decisions:
            store.record_decision(
                domain="capital", sku=d.request.sku,
                action=f"allocate ${d.approved_amount:,.2f} of ${d.request.amount_usd:,.2f}",
                rationale="; ".join(d.reasons),
                decision="APPROVE" if d.accepted else "REJECT",
                inputs={"risk_adjusted_roi": d.risk_adjusted_roi_pct,
                        "annualised_roi": d.annualised_roi_pct, "rank": d.rank},
                expected_outcome=(
                    f"Risk-adjusted return {d.risk_adjusted_roi_pct:.0f}% "
                    f"over a {d.request.cash_cycle_days}-day cycle."
                ),
                dedupe_key=f"{report_date}|capital|{d.request.sku}|allocate",
            )

    # ---- supplier health ------------------------------------------------
    supplier_alerts = []
    for sid in {p.supplier_id for p in positions if p.supplier_id != "unknown"}:
        supplier_alerts.extend(detect_deterioration(store.supplier_trend(sid)))
    if journal:
        for a in supplier_alerts:
            store.record_decision(
                domain="suppliers", sku=a.supplier_id,
                action=f"supplier_alert:{a.metric}",
                rationale=f"{a.detail} {a.recommendation}",
                decision="NEEDS_HUMAN_APPROVAL" if a.severity == "CRITICAL" else "HOLD",
                inputs={"severity": a.severity, "metric": a.metric},
                expected_outcome="Backup source qualified or supplier corrected.",
                requires_approval=a.severity == "CRITICAL",
                dedupe_key=f"{report_date}|suppliers|{a.supplier_id}|{a.metric}",
            )

    ctx = ReportContext(
        report_date=report_date,
        data_source=source,
        metrics=store.metrics_for_date(report_date),
        prior_metrics=store.metrics_for_date(prior_date),
        inventory_plans=plans,
        ad_summary=portfolio_ad_summary(ad_reviews),
        ad_actions=ad_actions,
        opportunities=screened,
        review_insights=insights,
        price_recommendations=price_recs,
        spend_state=spend_state,
        connector_status=all_status(),
        account_health=health,
        capital_state=capital_state,
        allocation_decisions=allocation_decisions,
        allocation_notes=allocation_notes,
        concentration=concentration_report(policy, capital_state),
        turns=portfolio_turns(policy, capital_state),
        supplier_alerts=supplier_alerts,
        signal_coverage_pct=available_weight(policy),
        unavailable_signals=unavailable_sources_report(policy),
    )

    content = build_daily_report(policy, ctx, store)
    path = write_report(policy, content, report_date)
    return content, path
