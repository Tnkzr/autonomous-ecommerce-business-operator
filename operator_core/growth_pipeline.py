"""The content-to-cash loop, run end to end.

    research -> score -> select -> list -> create -> publish -> measure -> learn

This module is the loop, not another engine. Its job is to run the engines in
the right order, carry each one's uncertainty into the next, and stop at the
points where a human has to decide.

Two things it deliberately does not do.

**It does not decide to spend or publish.** Every irreversible step —
publishing a product, creating an unbounded discount — produces a *proposal*
that goes through `risk.authorise()` and lands in the journal awaiting
approval. The loop's output is a set of decisions, not a set of actions taken.

**It does not manufacture the inputs it lacks.** Where a stage cannot run
because a feed is missing, it records that the stage did not run and why,
rather than substituting a default. The result is a report where "we did not
look" and "we looked and found nothing" are different lines, which is the
distinction the whole system is built around.

Every decision is journaled with a `dedupe_key` so a re-run on the same day is
idempotent. A loop that duplicates its own journal entries teaches the learning
engine that it does twice as much as it does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import risk
from .config import Policy
from .conversion import aggregate_funnel, diagnose_funnel
from .creative import build_creative_bank
from .content import build_content_plan
from .dashboard import build_dashboard
from .learning import learning_report
from .models import Decision, ProductCandidate, today_iso
from .production import build_production_package
from .publishing import build_publishing_plan, cadence_report, recommend_posting_times
from .listings import generate_listing
from .research import Opportunity, coverage_report, rank_pipeline
from .screening import screen_candidate
from .shopify_listing import build_product_plan
from .store import Store


@dataclass
class StageResult:
    """What one stage did, or why it could not run."""

    name: str
    ran: bool
    summary: str
    payload: Any = None
    skipped_reason: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class GrowthRunResult:
    run_date: str
    stages: list[StageResult] = field(default_factory=list)
    proposals: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def stage(self, name: str) -> StageResult | None:
        return next((s for s in self.stages if s.name == name), None)

    @property
    def stages_run(self) -> int:
        return sum(1 for s in self.stages if s.ran)

    @property
    def approvals_pending(self) -> int:
        return sum(1 for p in self.proposals if p.get("requires_approval"))

    def summary(self) -> dict[str, Any]:
        return {
            "run_date": self.run_date,
            "stages_total": len(self.stages),
            "stages_run": self.stages_run,
            "stages_skipped": len(self.stages) - self.stages_run,
            "proposals": len(self.proposals),
            "approvals_pending": self.approvals_pending,
            "warnings": len(self.warnings),
        }


def _journal(store: Store, *, domain: str, sku: str, action: str,
             rationale: str, decision: Decision, inputs: dict[str, Any],
             expected: str, requires_approval: bool, run_date: str) -> str:
    """Write one decision, idempotently for the day."""
    return store.record_decision(
        domain=domain, sku=sku, action=action, rationale=rationale,
        decision=decision, inputs=inputs, expected_outcome=expected,
        requires_approval=requires_approval,
        dedupe_key=f"{run_date}:{domain}:{sku}:{action}",
    )


# ---------------------------------------------------------------------------
def run_growth_cycle(
    policy: Policy,
    store: Store,
    *,
    opportunities: list[Opportunity] | None = None,
    candidates: list[ProductCandidate] | None = None,
    unit_margins: dict[str, float] | None = None,
    run_date: str | None = None,
    lookback_days: int = 28,
) -> GrowthRunResult:
    """Run the full loop once.

    Inputs are passed in rather than fetched here so the loop is testable
    offline and so a caller can run it against imported data, live data, or a
    single candidate under evaluation.
    """
    run_date = run_date or today_iso()
    today = date.fromisoformat(run_date)
    result = GrowthRunResult(run_date=run_date)
    candidates = candidates or []
    unit_margins = unit_margins or {}

    # -- 1. Research -----------------------------------------------------
    coverage = coverage_report()
    if opportunities:
        pipeline = rank_pipeline(opportunities, policy, today=today)
        result.stages.append(StageResult(
            name="research", ran=True,
            summary=(f"{pipeline['active']} active opportunity(ies), "
                     f"{pipeline['promotable']} promotable, "
                     f"{len(pipeline['retired'])} retired as stale."),
            payload=pipeline, warnings=pipeline["warnings"]))
        result.warnings.extend(pipeline["warnings"])
    else:
        pipeline = None
        result.stages.append(StageResult(
            name="research", ran=False, summary="No opportunities supplied.",
            skipped_reason=(
                "Nothing to rank. The pipeline is a memory of ideas under "
                "investigation; it is empty rather than exhausted.")))
    result.warnings.append(
        f"Research coverage: {coverage['sources_connected']} of "
        f"{coverage['sources_total']} signal sources connected "
        f"({coverage['connected_pct']}%). Every score this run is capped by that.")

    # -- 2. Screen and select --------------------------------------------
    selected: list[ProductCandidate] = []
    rejected: list[dict[str, Any]] = []
    if candidates:
        for candidate in candidates:
            screening = screen_candidate(policy, candidate)
            # APPROVE or NEEDS_HUMAN_APPROVAL both mean every gate passed —
            # the second only routes the *spending* step to a human. Generating
            # creative is free and reversible, so it proceeds; the approval is
            # enforced at the publish proposal below, which is where money and
            # irreversibility actually enter.
            #
            # HOLD does not proceed: it means the economics work and nothing
            # shows that anyone wants the product, and a shoot day spent on the
            # strength of a spreadsheet is the expensive version of that
            # mistake.
            if screening.decision in (Decision.APPROVE,
                                      Decision.NEEDS_HUMAN_APPROVAL):
                selected.append(candidate)
                continue
            rejected.append({
                "sku": candidate.sku,
                "decision": str(screening.decision),
                "reasons": [f"{g.name}: {g.detail}"
                            for g in screening.blocking_failures],
            })
            _journal(store, domain="sourcing", sku=candidate.sku,
                     action="reject_candidate",
                     rationale=screening.reason_summary(),
                     decision=screening.decision,
                     inputs={"title": candidate.title},
                     expected="Candidate does not enter the content pipeline.",
                     requires_approval=False, run_date=run_date)
        result.stages.append(StageResult(
            name="screen", ran=True,
            summary=f"{len(selected)} of {len(candidates)} candidates passed "
                    f"screening; {len(rejected)} rejected.",
            payload={"selected": [c.sku for c in selected], "rejected": rejected}))
    else:
        result.stages.append(StageResult(
            name="screen", ran=False, summary="No candidates supplied.",
            skipped_reason="Nothing to screen this run."))

    # -- 3. Listing ------------------------------------------------------
    # Copy and the Shopify product shape are prepared for every selected
    # candidate, whether or not credentials exist. A plan is not a write: it is
    # reviewable, diffable, and costs nothing to produce, which is exactly what
    # should happen before the irreversible step.
    plans: list[Any] = []
    for candidate in selected:
        draft = generate_listing(candidate, marketplace="shopify")
        plan = build_product_plan(policy, candidate, draft)
        plans.append(plan)
        result.warnings.extend(f"{candidate.sku} listing: {w}"
                               for w in plan.blockers)

    if selected:
        ready_plans = [p for p in plans if p.ready]
        result.stages.append(StageResult(
            name="listing", ran=True,
            summary=(f"{len(ready_plans)} of {len(plans)} Shopify product plan(s) "
                     "ready to create as drafts."),
            payload={"plans": [p.to_dict() for p in plans]},
            warnings=[w for p in plans for w in p.warnings]))
    else:
        result.stages.append(StageResult(
            name="listing", ran=False, summary="No products passed screening.",
            skipped_reason="A listing is built per selected product."))

    # -- 4. Creative -----------------------------------------------------
    packages: list[Any] = []
    creative_summary: list[dict[str, Any]] = []
    for candidate in selected:
        bank = build_creative_bank(candidate)
        margin = unit_margins.get(candidate.sku, 0.0)
        plan = build_content_plan(policy, candidate, unit_margin=margin)
        for idea, concept in zip(bank.ideas, plan.concepts):
            packages.append(build_production_package(
                concept=concept, idea=idea, title=candidate.title,
                hashtags=bank.hashtags))
        coverage_stats = bank.coverage()
        creative_summary.append({"sku": candidate.sku, **coverage_stats})
        result.warnings.extend(bank.warnings)

    if selected:
        ready = sum(1 for p in packages if p.ready)
        result.stages.append(StageResult(
            name="creative", ran=True,
            summary=(f"{len(packages)} production package(s) built for "
                     f"{len(selected)} product(s); {ready} shootable as generated."),
            payload={"packages": len(packages), "shootable": ready,
                     "by_sku": creative_summary}))
        if packages and not ready:
            result.warnings.append(
                "No package is shootable as generated — every one carries an "
                "unfilled slot or an unwritten line. The calendar below will "
                "have nothing to post into until those are filled.")
    else:
        result.stages.append(StageResult(
            name="creative", ran=False, summary="No products passed screening.",
            skipped_reason="Creative is generated per selected product."))

    # -- 5. Publishing calendar ------------------------------------------
    history = store.published_videos()
    metrics = store.latest_video_metrics()
    timing = recommend_posting_times(metrics)
    slots = ([r["hour"] for r in timing["recommendations"]]
             if timing["confident"] else None)

    pub_cfg = policy.raw.get("publishing", {})
    plan = build_publishing_plan(
        packages, days=int(pub_cfg.get("calendar_days", 14)),
        posts_per_day=int(pub_cfg.get("posts_per_day", 2)),
        start=today, recommended_slots=slots)
    cadence = cadence_report(history, days=lookback_days, today=today)
    result.stages.append(StageResult(
        name="publishing", ran=bool(packages),
        summary=(f"{len(plan.posts)} slot(s) scheduled, "
                 f"{len(plan.shootable)} fillable. Posting times: "
                 f"{'from own history' if timing['confident'] else 'not yet known'}."),
        payload={"plan": plan.summary(), "timing": timing, "cadence": cadence},
        skipped_reason="" if packages else "Nothing to schedule.",
        warnings=plan.warnings + cadence["warnings"]))
    result.warnings.extend(plan.warnings + cadence["warnings"])

    # -- 6. Measure ------------------------------------------------------
    start = (today - timedelta(days=lookback_days - 1)).isoformat()
    storefront = store.storefront_range(start, run_date)
    if storefront:
        funnel = aggregate_funnel(period=f"{start}..{run_date}", channel="tiktok",
                                  storefront_rows=storefront, video_rows=metrics)
        diagnosis = diagnose_funnel(funnel, policy)
        result.stages.append(StageResult(
            name="measure", ran=True,
            summary=(f"Funnel over {lookback_days} days: "
                     f"{'leak at ' + diagnosis.leak_stage if diagnosis.leak_stage else 'no stage below benchmark'}."),
            payload=diagnosis.to_dict(), warnings=diagnosis.warnings))
        result.warnings.extend(diagnosis.warnings)

        for rec in diagnosis.recommendations:
            if rec.priority != "HIGH":
                continue
            _journal(store, domain="conversion", sku="",
                     action=f"funnel_{rec.stage}",
                     rationale=rec.rationale, decision=Decision.PROCEED,
                     inputs={"stage": rec.stage, "confident": rec.confident,
                             "sample": rec.sample_basis},
                     expected=rec.expected_effect, requires_approval=False,
                     run_date=run_date)
    else:
        diagnosis = None
        result.stages.append(StageResult(
            name="measure", ran=False,
            summary="No storefront data in the window.",
            skipped_reason=(
                "Nothing recorded in `storefront_daily`. Sync Shopify orders "
                "first — a funnel with no orders in it is not a funnel with a "
                "problem, it is a funnel with no data.")))

    # -- 7. Learn --------------------------------------------------------
    metric_rows = store.metrics_range(start, run_date)
    learning = learning_report(video_rows=metrics, metric_rows=metric_rows)
    result.stages.append(StageResult(
        name="learn", ran=bool(metrics or metric_rows),
        summary=learning["headline"],
        payload=learning,
        skipped_reason="" if (metrics or metric_rows) else
                       ("No published-video metrics and no product metrics. "
                        "Nothing to learn from yet.")))

    # -- 8. Proposals ----------------------------------------------------
    for candidate in selected:
        # Deliberately not gated on having shootable creative. Publishing a
        # listing and finishing a video are independent tasks, and coupling
        # them means a product that cleared every gate silently produces no
        # proposal at all — the loop looks inert when it is actually blocked on
        # one unwritten line.
        built = [p for p in packages if p.sku == candidate.sku]
        shootable = [p for p in built if p.ready]
        plan = next((p for p in plans if p.sku == candidate.sku), None)
        if plan is not None and not plan.ready:
            # A product plan that cannot be created cannot be published. Skip
            # the proposal rather than asking for approval on something that
            # would fail at the first call.
            result.warnings.append(
                f"{candidate.sku}: no publish proposal — the Shopify product "
                f"plan is blocked ({'; '.join(plan.blockers)}).")
            continue
        authorisation = risk.authorise(
            policy=policy, action_type="publish_listing", amount_usd=0.0,
            is_new_sku=True,
            description=f"Publish {candidate.sku} to the storefront")
        rationale = (
            f"Candidate passed screening; {len(built)} package(s) built, "
            f"{len(shootable)} shootable as generated. Publishing is the "
            "irreversible step: the page can be indexed and bought within "
            "seconds of going live.")
        if built and not shootable:
            rationale += (" No package is shootable yet, so the listing would "
                          "go live with no traffic behind it — publish only if "
                          "the page is wanted before the videos are.")
        action_id = _journal(
            store, domain="listing", sku=candidate.sku, action="publish_product",
            rationale=rationale,
            decision=authorisation.decision, inputs={
                "packages_built": len(built),
                "packages_shootable": len(shootable),
                "handle": plan.handle if plan else "",
                "price": candidate.target_price,
                "authorisation": authorisation.explain(),
            },
            expected="Product live and receiving traffic from scheduled videos.",
            requires_approval=authorisation.approval_required_from_human,
            run_date=run_date)
        result.proposals.append({
            "action_id": action_id, "sku": candidate.sku,
            "action": "publish_product",
            "permitted": authorisation.permitted,
            "requires_approval": authorisation.approval_required_from_human,
            "packages_shootable": len(shootable),
            "reasons": authorisation.reasons,
        })

    return result


# ---------------------------------------------------------------------------
def render_growth_run(result: GrowthRunResult, *, width: int = 78) -> str:
    """Plain-text report of one loop run."""
    lines = ["=" * width,
             f"GROWTH CYCLE — {result.run_date}".center(width),
             "=" * width, ""]

    summary = result.summary()
    lines.append(f"  Stages run       {summary['stages_run']}/{summary['stages_total']}")
    lines.append(f"  Proposals        {summary['proposals']}")
    lines.append(f"  Awaiting approval {summary['approvals_pending']}")
    lines.append("")

    for stage in result.stages:
        marker = "ran" if stage.ran else "skipped"
        lines.append("-" * width)
        lines.append(f"  {stage.name.upper()}  [{marker}]")
        lines.append("-" * width)
        for chunk in _wrap(stage.summary, width - 4):
            lines.append(f"  {chunk}")
        if not stage.ran and stage.skipped_reason:
            for chunk in _wrap(stage.skipped_reason, width - 6):
                lines.append(f"    {chunk}")
        lines.append("")

    if result.proposals:
        lines.append("-" * width)
        lines.append("  PROPOSALS")
        lines.append("-" * width)
        for proposal in result.proposals:
            state = ("NEEDS APPROVAL" if proposal["requires_approval"]
                     else ("permitted" if proposal["permitted"] else "REJECTED"))
            lines.append(f"  [{state}] {proposal['action']} — {proposal['sku']}")
            lines.append(f"      id: {proposal['action_id']}")
            for reason in proposal["reasons"]:
                for chunk in _wrap(reason, width - 10):
                    lines.append(f"      {chunk}")
        lines.append("")

    if result.warnings:
        lines.append("-" * width)
        lines.append("  WARNINGS")
        lines.append("-" * width)
        seen: set[str] = set()
        for warning in result.warnings:
            if warning in seen:
                continue
            seen.add(warning)
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
