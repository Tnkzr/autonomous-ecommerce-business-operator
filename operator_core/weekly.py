"""The weekly business review.

The daily report answers "what needs doing today". This answers "is the company
getting more valuable", which is a different question on a different clock and
should not be inferred from seven daily reports.

The section that earns its place is **engineering improvements**: the system
introspects on its own gaps — unconnected signal sources, unresolved decisions,
data it cannot measure — and ranks them by what they cost the business rather
than by how interesting they are to build. A backlog derived from real blockers
beats one derived from someone's enthusiasm.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .config import Policy
from .models import money
from .store import Store, learning_summary


@dataclass
class Experiment:
    """A proposed test, with what would make it conclusive."""

    name: str
    hypothesis: str
    method: str
    success_metric: str
    minimum_sample: str
    cost_estimate: str
    priority: str               # HIGH | MEDIUM | LOW


@dataclass
class EngineeringItem:
    area: str
    improvement: str
    business_cost_of_not_doing_it: str
    effort: str                 # S | M | L
    priority: str


@dataclass
class WeeklyReport:
    week_ending: str
    data_source: str
    profit: dict[str, Any]
    opportunities: list[dict[str, Any]]
    risks: list[dict[str, Any]]
    pipeline: dict[str, Any]
    engineering: list[EngineeringItem]
    store_health: dict[str, Any]
    experiments: list[Experiment]
    learning: dict[str, Any]


# ---------------------------------------------------------------------------
# Engineering backlog, derived from measured gaps
# ---------------------------------------------------------------------------
def derive_engineering_backlog(policy: Policy, store: Store,
                               *, signal_coverage_pct: float,
                               connector_status: list[dict[str, Any]],
                               unresolved_decisions: int,
                               scorecard_coverage_pct: float | None = None,
                               ) -> list[EngineeringItem]:
    """Rank engineering work by the decisions it currently blocks.

    Every item here has to name what it costs the business today. An
    improvement that cannot answer that is a preference, and preferences do not
    belong on a backlog that competes with buying inventory.
    """
    items: list[EngineeringItem] = []

    if signal_coverage_pct < 60:
        items.append(EngineeringItem(
            area="Market signals",
            improvement=(
                f"Connect more signal sources — {signal_coverage_pct:.0f}% of the "
                "weighted set is observable. Reddit and YouTube have free-tier "
                "APIs; CPSC recall RSS is free and the highest-value one for "
                "product safety."
            ),
            business_cost_of_not_doing_it=(
                "Every confidence score is computed from a fraction of the "
                "intended evidence, so product selection is running on partial "
                "information and the ranking between candidates is unreliable."
            ),
            effort="M", priority="HIGH",
        ))

    unconfigured = [c["marketplace"] for c in connector_status if not c["configured"]]
    if unconfigured:
        items.append(EngineeringItem(
            area="Connectors",
            improvement=f"Provision credentials for: {', '.join(unconfigured)}.",
            business_cost_of_not_doing_it=(
                "The operator cannot read real sales, inventory, or fees for "
                "these marketplaces, so it runs on seed data and cannot manage "
                "anything real."
            ),
            effort="S", priority="HIGH",
        ))

    fees = policy.fees_for("tiktok")
    if float(fees.get("affiliate_commission_pct", 0)) == 0:
        items.append(EngineeringItem(
            area="Fee calibration",
            improvement=(
                "Reconcile [fees.tiktok] against settled statements — run "
                "`tiktok-settlements` and update the take rate."
            ),
            business_cost_of_not_doing_it=(
                "Every margin, price floor, and break-even ACOS is computed "
                "from estimated fees. If they are wrong, the system is "
                "confidently wrong about which products are profitable."
            ),
            effort="S", priority="HIGH",
        ))

    if unresolved_decisions > 20:
        items.append(EngineeringItem(
            area="Learning loop",
            improvement=(
                f"Record outcomes for the {unresolved_decisions} open decisions. "
                "Use `outcome <id> --met true|false`."
            ),
            business_cost_of_not_doing_it=(
                "Hit rate is only computed over resolved decisions, so the "
                "learning loop currently has nothing to learn from and future "
                "recommendations cannot improve."
            ),
            effort="S", priority="MEDIUM",
        ))

    if scorecard_coverage_pct is not None and scorecard_coverage_pct < 70:
        items.append(EngineeringItem(
            area="Product scoring",
            improvement=(
                f"Scorecard coverage is {scorecard_coverage_pct:.0f}%. The gaps "
                "are trend history, review sentiment, and observed return rates "
                "— all of which need live data rather than new code."
            ),
            business_cost_of_not_doing_it=(
                "Candidates are ranked on partial scorecards, so a "
                "well-understood mediocre product can lose to a poorly-understood "
                "one that merely looks better on the dimensions we happened to "
                "measure."
            ),
            effort="M", priority="MEDIUM",
        ))

    items.append(EngineeringItem(
        area="Advertising",
        improvement=(
            "Implement the TikTok Marketing API connector (separate app from "
            "the Shop API: own credentials, own advertiser_id)."
        ),
        business_cost_of_not_doing_it=(
            "Ad performance is invisible, so the advertising engine runs on "
            "manually entered figures and TACOS cannot be computed from live "
            "data. Scaling decisions are being made without the spend side."
        ),
        effort="L", priority="MEDIUM",
    ))

    items.append(EngineeringItem(
        area="Customer retention",
        improvement=(
            "Derive repeat-purchase rate from order history keyed by buyer, and "
            "feed it into `economics.lifetime_value`."
        ),
        business_cost_of_not_doing_it=(
            "LTV currently assumes a 0% repeat rate, which understates the value "
            "of every customer and caps what the business will pay to acquire "
            "one. That directly suppresses growth on products that do repeat."
        ),
        effort="M", priority="MEDIUM",
    ))

    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    items.sort(key=lambda i: (order.get(i.priority, 9), i.effort))
    return items


def derive_experiments(policy: Policy, *, trends: list[Any] | None = None,
                       ad_summary: dict[str, Any] | None = None,
                       signal_coverage_pct: float = 0.0) -> list[Experiment]:
    """Propose the week's tests, each with a stopping rule.

    An experiment without a minimum sample and a success metric is not an
    experiment — it is a change someone will later claim credit for.
    """
    experiments: list[Experiment] = []

    experiments.append(Experiment(
        name="Hook archetype A/B",
        hypothesis=(
            "Problem-callout hooks retain better than visual-shock hooks for "
            "utility products, because the viewer recognises themselves before "
            "the product appears."
        ),
        method=(
            "Same product, same shot list from shot 2 onward, two different "
            "opening 2 seconds. Post both within the same 48h window to control "
            "for algorithmic conditions."
        ),
        success_metric="3-second retention rate, then conversion rate",
        minimum_sample="4 videos per arm, 10k views per arm",
        cost_estimate="Production time only if filmed in one session.",
        priority="HIGH",
    ))

    experiments.append(Experiment(
        name="Creator tier efficiency",
        hypothesis=(
            "Nano creators (1–10k) produce a better cost per converted order "
            "than micro creators, because commission-only terms remove the "
            "fixed-fee downside on videos that do not perform."
        ),
        method=(
            "Seed 20 nano and 5 micro creators with the same product and the "
            "same brief. Track orders attributed to each."
        ),
        success_metric="Cost per attributed order, including sample cost",
        minimum_sample="25 creators, 30 days",
        cost_estimate="25 sample units plus shipping.",
        priority="HIGH",
    ))

    if signal_coverage_pct < 60:
        experiments.append(Experiment(
            name="Signal source value test",
            hypothesis=(
                "Connecting one additional signal source measurably improves "
                "product-selection hit rate."
            ),
            method=(
                "Connect the cheapest source (Reddit or YouTube free tier). "
                "Score the next 30 candidates with and without it and compare "
                "against realised outcomes 60 days later."
            ),
            success_metric="Hit rate of PURSUE verdicts, with vs without",
            minimum_sample="30 candidates, 60-day outcome window",
            cost_estimate="Engineering time; both APIs have free tiers.",
            priority="MEDIUM",
        ))

    experiments.append(Experiment(
        name="Price elasticity probe",
        hypothesis=(
            "The current price is below the profit-maximising point, so a 10% "
            "rise costs less volume than it gains in margin."
        ),
        method=(
            "Raise price 10% on the best-understood SKU for 14 days. Hold "
            "everything else constant — no new creatives, no budget changes."
        ),
        success_metric="Absolute gross profit, not units or revenue",
        minimum_sample="14 days, or 100 orders, whichever comes first",
        cost_estimate="Potential short-term volume loss; reversible in a day.",
        priority="MEDIUM",
    ))

    if trends:
        spiky = [t for t in trends if getattr(t, "shape", "") == "SPIKE_DECAY"]
        if spiky:
            experiments.append(Experiment(
                name="Spike-decay recovery test",
                hypothesis=(
                    "A decayed product can be revived with fresh creative rather "
                    "than being liquidated — the demand went with the video, not "
                    "with the product."
                ),
                method=(
                    f"Take {spiky[0].title}, commission 5 new UGC videos with "
                    "different hooks, and hold price and budget constant."
                ),
                success_metric="Daily units 14 days after, vs the current tail rate",
                minimum_sample="5 videos, 14 days",
                cost_estimate="5 creator samples.",
                priority="HIGH",
            ))

    return experiments


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def build_weekly_report(
    policy: Policy,
    store: Store,
    *,
    week_ending: str,
    data_source: str,
    metrics_this_week: list[dict[str, Any]],
    metrics_last_week: list[dict[str, Any]],
    scorecards: list[Any] | None = None,
    trends: list[Any] | None = None,
    account_health: Any = None,
    capital_state: Any = None,
    concentration: dict[str, Any] | None = None,
    turns: dict[str, Any] | None = None,
    supplier_alerts: list[Any] | None = None,
    signal_coverage_pct: float = 0.0,
    connector_status: list[dict[str, Any]] | None = None,
    ad_summary: dict[str, Any] | None = None,
) -> WeeklyReport:
    scorecards = scorecards or []
    trends = trends or []
    connector_status = connector_status or []
    supplier_alerts = supplier_alerts or []

    def total(rows: list[dict[str, Any]], key: str) -> float:
        return money(sum(r.get(key, 0.0) for r in rows))

    revenue = total(metrics_this_week, "revenue")
    prior_revenue = total(metrics_last_week, "revenue")
    net = total(metrics_this_week, "net_profit")
    prior_net = total(metrics_last_week, "net_profit")
    ad_spend = total(metrics_this_week, "ad_spend")
    cogs = total(metrics_this_week, "cogs")

    tax_rate = float(policy.tax["income_tax_rate_pct"])
    after_tax = money(net * (1 - tax_rate / 100)) if net > 0 else net

    profit = {
        "revenue": revenue,
        "revenue_change_pct": (
            round((revenue - prior_revenue) / prior_revenue * 100, 1)
            if prior_revenue else None),
        "gross_profit": money(revenue - cogs),
        "net_profit": net,
        "net_change_pct": (
            round((net - prior_net) / abs(prior_net) * 100, 1) if prior_net else None),
        "after_tax_profit": after_tax,
        "net_margin_pct": round(net / revenue * 100, 1) if revenue else 0.0,
        "tacos_pct": round(ad_spend / revenue * 100, 1) if revenue else 0.0,
        "tax_rate_pct": tax_rate,
    }

    ranked = sorted(scorecards, key=lambda s: -(s.overall * s.coverage_pct / 100))
    opportunities = [{
        "sku": s.sku, "title": s.title, "score": s.overall,
        "coverage_pct": s.coverage_pct, "verdict": s.verdict,
        "strengths": s.strengths[:2],
        "blocking": s.capped_by,
    } for s in ranked[:5] if s.verdict in ("PURSUE", "INVESTIGATE")]

    # Risks, ordered by what they cost if ignored rather than by likelihood.
    risks: list[dict[str, Any]] = []
    if account_health is not None and getattr(account_health, "breaches", None):
        risks.append({
            "severity": "CRITICAL", "area": "Account health",
            "detail": "; ".join(m.label for m in account_health.breaches),
            "consequence": (
                "Suspension ends the business, unlike every other risk here "
                "which only costs money."),
        })
    if concentration and concentration.get("breaches"):
        top = concentration["breaches"][0]
        risks.append({
            "severity": "HIGH", "area": "Capital concentration",
            "detail": (f"{top['dimension']} '{top['value']}' holds "
                       f"{top['share_pct']:.0f}% of deployed capital "
                       f"(limit {top['limit_pct']:.0f}%)"),
            "consequence": (
                "One suspension, patent claim, or supplier failure at this "
                "weighting is unrecoverable."),
        })
    for a in supplier_alerts[:2]:
        risks.append({
            "severity": a.severity, "area": "Supplier",
            "detail": f"{a.supplier_name}: {a.detail}",
            "consequence": a.recommendation,
        })
    if turns and turns.get("capital_in_dead", 0) > 0:
        risks.append({
            "severity": "MEDIUM", "area": "Dead stock",
            "detail": (f"${turns['capital_in_dead']:,.2f} in SKUs with zero "
                       f"velocity ({', '.join(turns.get('dead', [])[:3])})"),
            "consequence": "Capital that cannot be redeployed and accrues storage fees.",
        })
    if signal_coverage_pct < 50:
        risks.append({
            "severity": "MEDIUM", "area": "Decision quality",
            "detail": f"Only {signal_coverage_pct:.0f}% signal coverage",
            "consequence": (
                "Product selection is running on partial evidence; the ranking "
                "between candidates is not reliable."),
        })

    pipeline = {
        "scored": len(scorecards),
        "pursue": sum(1 for s in scorecards if s.verdict == "PURSUE"),
        "investigate": sum(1 for s in scorecards if s.verdict == "INVESTIGATE"),
        "hold": sum(1 for s in scorecards if s.verdict == "HOLD"),
        "rejected": sum(1 for s in scorecards if s.verdict == "REJECT"),
        "median_coverage_pct": (
            round(sorted(s.coverage_pct for s in scorecards)[len(scorecards) // 2], 1)
            if scorecards else 0.0),
    }

    ls = learning_summary(store)
    avg_coverage = (
        sum(s.coverage_pct for s in scorecards) / len(scorecards)
        if scorecards else None
    )
    engineering = derive_engineering_backlog(
        policy, store,
        signal_coverage_pct=signal_coverage_pct,
        connector_status=connector_status,
        unresolved_decisions=ls["unresolved_decisions"],
        scorecard_coverage_pct=avg_coverage,
    )

    health = {"measured": False}
    if account_health is not None:
        health = {
            "measured": True,
            "severity": account_health.severity.value,
            "coverage_pct": account_health.coverage_pct,
            "breaches": [m.label for m in account_health.breaches],
            "approaching": [m.label for m in account_health.warnings],
            "actions": account_health.actions[:3],
        }

    return WeeklyReport(
        week_ending=week_ending,
        data_source=data_source,
        profit=profit,
        opportunities=opportunities,
        risks=risks,
        pipeline=pipeline,
        engineering=engineering,
        store_health=health,
        experiments=derive_experiments(
            policy, trends=trends, ad_summary=ad_summary,
            signal_coverage_pct=signal_coverage_pct),
        learning=ls,
    )


def render_weekly_report(policy: Policy, report: WeeklyReport) -> str:
    """Render the weekly review as markdown."""
    p: list[str] = []
    biz = policy.meta.get("business_name", "Store")

    p.append(f"# Weekly Business Review — week ending {report.week_ending}")
    p.append(f"**{biz}**")
    p.append("")

    if report.data_source != "live":
        p.append("> ## ⚠️ THESE ARE NOT REAL BUSINESS NUMBERS")
        p.append(">")
        p.append(
            f"> Data source: **{report.data_source.upper()}**. Every financial "
            "figure below is derived from seed data. Do not make capital, "
            "pricing, or inventory decisions from this report."
        )
        p.append("")

    # 1. Profit
    pr = report.profit
    p.append("## 1. Estimated Profit")
    p.append("")
    p.append("| Metric | This week | Change |")
    p.append("|---|---:|---:|")
    rev_delta = ("—" if pr["revenue_change_pct"] is None
                 else f"{pr['revenue_change_pct']:+.1f}%")
    net_delta = ("—" if pr["net_change_pct"] is None
                 else f"{pr['net_change_pct']:+.1f}%")
    p.append(f"| Revenue | ${pr['revenue']:,.2f} | {rev_delta} |")
    p.append(f"| Gross profit | ${pr['gross_profit']:,.2f} | — |")
    p.append(f"| Net profit | ${pr['net_profit']:,.2f} | {net_delta} |")
    p.append(f"| **After-tax profit** | **${pr['after_tax_profit']:,.2f}** | "
             f"at {pr['tax_rate_pct']:.0f}% |")
    p.append("")
    p.append(f"- Net margin **{pr['net_margin_pct']:.1f}%** · TACOS **{pr['tacos_pct']:.1f}%**")
    p.append("- After-tax profit is the KPI. The pre-tax line is an input to it, "
             "not the result.")
    p.append("")

    # 2. Opportunities
    p.append("## 2. Best Opportunities")
    if not report.opportunities:
        p.append("_No candidates reached PURSUE or INVESTIGATE this week._")
    else:
        p.append("")
        p.append("| SKU | Score | Coverage | Verdict | Strength |")
        p.append("|---|---:|---:|---|---|")
        for o in report.opportunities:
            strength = o["strengths"][0].split(" — ")[0] if o["strengths"] else "—"
            p.append(f"| {o['sku']} | {o['score']:.0f} | {o['coverage_pct']:.0f}% | "
                     f"{o['verdict']} | {strength} |")
        p.append("")
        p.append("_Ranked on score × coverage: a high score from few measured "
                 "dimensions is not a high score._")
    p.append("")

    # 3. Risks
    p.append("## 3. Biggest Risks")
    if not report.risks:
        p.append("_No material risks detected._")
    else:
        for r in report.risks:
            p.append(f"- **[{r['severity']}] {r['area']}** — {r['detail']}")
            p.append(f"  - {r['consequence']}")
    p.append("")

    # 4. Pipeline
    pl = report.pipeline
    p.append("## 4. Product Pipeline")
    p.append("")
    p.append(f"- Scored: **{pl['scored']}** · pursue {pl['pursue']} · "
             f"investigate {pl['investigate']} · hold {pl['hold']} · "
             f"rejected {pl['rejected']}")
    p.append(f"- Median scorecard coverage: **{pl['median_coverage_pct']:.0f}%**")
    if pl["scored"] and pl["pursue"] == 0:
        p.append("- Nothing cleared PURSUE. That is a normal week, not a failure — "
                 "the gates exist to produce this outcome most of the time.")
    p.append("")

    # 5. Engineering
    p.append("## 5. Recommended Engineering Improvements")
    p.append("")
    for i, e in enumerate(report.engineering, 1):
        p.append(f"{i}. **[{e.priority}/{e.effort}] {e.area}** — {e.improvement}")
        p.append(f"   - Cost of not doing it: {e.business_cost_of_not_doing_it}")
    p.append("")

    # 6. Store health
    p.append("## 6. Store Health")
    if not report.store_health.get("measured"):
        p.append("_No account health metrics supplied._ Not assumed healthy — "
                 "an unmeasured defect rate is how a suspension arrives unannounced.")
    else:
        h = report.store_health
        p.append(f"- Status: **{h['severity']}** ({h['coverage_pct']:.0f}% measured)")
        if h["breaches"]:
            p.append(f"- Breached: {', '.join(h['breaches'])}")
        if h["approaching"]:
            p.append(f"- Approaching: {', '.join(h['approaching'])}")
        for a in h["actions"]:
            p.append(f"- {a}")
    p.append("")

    # 7. Experiments
    p.append("## 7. Experiments to Run Next")
    p.append("")
    for e in report.experiments:
        p.append(f"### [{e.priority}] {e.name}")
        p.append(f"- **Hypothesis:** {e.hypothesis}")
        p.append(f"- **Method:** {e.method}")
        p.append(f"- **Success metric:** {e.success_metric}")
        p.append(f"- **Minimum sample:** {e.minimum_sample}")
        p.append(f"- **Cost:** {e.cost_estimate}")
        p.append("")

    # 8. Learning
    ls = report.learning
    p.append("## 8. Learning Loop")
    p.append("")
    p.append(f"- Decisions recorded: **{ls['total_decisions']}** "
             f"({ls['resolved_decisions']} resolved, {ls['unresolved_decisions']} open)")
    for domain, stats in ls.get("by_domain", {}).items():
        p.append(f"- `{domain}`: {stats['as_expected']}/{stats['resolved']} met "
                 f"expectation ({stats['hit_rate_pct']}%)")
    p.append(f"- _{ls['note']}_")
    p.append("")

    p.append("---")
    p.append("_Generated by the autonomous ecommerce operator. Nothing above was "
             "executed against a live account._")
    return "\n".join(p)


def write_weekly_report(policy: Policy, content: str, week_ending: str) -> Path:
    out_dir = Path(policy.reporting.get("output_dir", "reports"))
    if not out_dir.is_absolute():
        out_dir = Path(__file__).resolve().parent.parent / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"weekly-review-{week_ending}.md"
    path.write_text(content, encoding="utf-8")
    return path
