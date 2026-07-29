"""Conversion optimisation: where the funnel leaks and what to do about it.

The funnel in this business has four stages and each has a different fix:

    views  ->  link clicks  ->  sessions  ->  orders

A video with a million views and no clicks is a *creative* problem — the hook
worked and the offer did not. A page with clicks and no orders is a *landing
page or price* problem. Recommending "improve the landing page" to fix a hook
is the single most common wasted month in ecommerce, so `diagnose_funnel`
locates the leak before anything suggests a fix.

Two rules govern every recommendation here.

**Nothing is recommended below its sample floor.** A 2% conversion rate on 40
sessions is one order; the same number on 4,000 sessions is a finding. Each
diagnosis carries the sample it was computed on and refuses to fire below the
floor in `[conversion]`. Acting on noise is worse than acting on nothing,
because it also destroys the baseline you would have compared against.

**A rate with no denominator is not reported.** Shopify's Admin API does not
expose session counts — that lives in Analytics, which needs a different
permission and, for many stores, a different plan. So `sessions` is nullable
throughout, and conversion rate comes back `None` with an explanation rather
than being back-computed from orders. A conversion rate invented from a guessed
denominator is the most confidently wrong number a store can produce.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Policy

# Funnel stages, in order. Named so a diagnosis can point at one.
STAGES = ("reach", "click", "landing", "checkout")

STAGE_MEANING = {
    "reach": "Videos are not being distributed. Nothing downstream can be "
             "diagnosed until they are.",
    "click": "People watch and do not click. The video earns attention and the "
             "offer does not convert it — a creative and offer problem, not a "
             "store problem.",
    "landing": "People click and do not buy. The product page, price, or "
               "shipping promise is losing them after the click.",
    "checkout": "People start buying and do not finish. Payment options, "
                "shipping cost revealed late, or a forced account.",
}


@dataclass
class FunnelMetrics:
    """One period's funnel. Every rate carries the sample it came from."""

    period: str
    channel: str
    views: int | None
    clicks: int | None
    sessions: int | None
    orders: int
    revenue: float
    refunds: float
    new_customers: int

    @property
    def click_rate_pct(self) -> float | None:
        if not self.views or self.clicks is None:
            return None
        return round(self.clicks / self.views * 100, 3)

    @property
    def conversion_rate_pct(self) -> float | None:
        """None when sessions are unknown — never back-computed from orders."""
        if not self.sessions:
            return None
        return round(self.orders / self.sessions * 100, 3)

    @property
    def aov(self) -> float | None:
        if not self.orders:
            return None
        return round(self.revenue / self.orders, 2)

    @property
    def revenue_per_visitor(self) -> float | None:
        if not self.sessions:
            return None
        return round(self.revenue / self.sessions, 3)

    @property
    def refund_rate_pct(self) -> float | None:
        if self.revenue <= 0:
            return None
        return round(self.refunds / self.revenue * 100, 2)

    @property
    def repeat_share_pct(self) -> float | None:
        if not self.orders:
            return None
        repeat = max(self.orders - self.new_customers, 0)
        return round(repeat / self.orders * 100, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "channel": self.channel,
            "views": self.views,
            "clicks": self.clicks,
            "sessions": self.sessions,
            "orders": self.orders,
            "revenue": round(self.revenue, 2),
            "click_rate_pct": self.click_rate_pct,
            "conversion_rate_pct": self.conversion_rate_pct,
            "aov": self.aov,
            "revenue_per_visitor": self.revenue_per_visitor,
            "refund_rate_pct": self.refund_rate_pct,
            "repeat_share_pct": self.repeat_share_pct,
        }


@dataclass
class Recommendation:
    stage: str
    priority: str               # HIGH | MEDIUM | LOW
    action: str
    rationale: str
    expected_effect: str
    sample_basis: str
    confident: bool = True


@dataclass
class FunnelDiagnosis:
    metrics: FunnelMetrics
    leak_stage: str | None
    recommendations: list[Recommendation] = field(default_factory=list)
    unmeasurable: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics.to_dict(),
            "leak_stage": self.leak_stage,
            "leak_meaning": STAGE_MEANING.get(self.leak_stage or "", ""),
            "recommendations": [
                {"stage": r.stage, "priority": r.priority, "action": r.action,
                 "rationale": r.rationale, "expected_effect": r.expected_effect,
                 "sample_basis": r.sample_basis, "confident": r.confident}
                for r in self.recommendations
            ],
            "unmeasurable": self.unmeasurable,
            "warnings": self.warnings,
        }


def _conv(policy: Policy) -> dict[str, Any]:
    return policy.raw.get("conversion", {})


# ---------------------------------------------------------------------------
def aggregate_funnel(*, period: str, channel: str,
                     storefront_rows: list[dict[str, Any]],
                     video_rows: list[dict[str, Any]] | None = None
                     ) -> FunnelMetrics:
    """Combine storefront and video data into one funnel.

    Views and clicks come from the video side, orders and revenue from the
    store. They are joined on the period rather than per-video, because a
    Shopify order attributed to TikTok cannot always be traced to the specific
    video that earned it — only videos carrying a `utm_content` tag can.
    """
    rows = [r for r in storefront_rows
            if not channel or r.get("channel") == channel]
    sessions_values = [r.get("sessions") for r in rows if r.get("sessions") is not None]

    views = clicks = None
    if video_rows:
        views = sum(int(v.get("views") or 0) for v in video_rows)
        click_values = [v.get("link_clicks") for v in video_rows
                        if v.get("link_clicks") is not None]
        clicks = sum(int(c) for c in click_values) if click_values else None

    return FunnelMetrics(
        period=period,
        channel=channel or "all",
        views=views,
        clicks=clicks,
        # Summed only if at least one row reported it; otherwise None, because
        # a zero here would be read as "nobody visited".
        sessions=sum(int(s) for s in sessions_values) if sessions_values else None,
        orders=sum(int(r.get("orders") or 0) for r in rows),
        revenue=sum(float(r.get("revenue") or 0.0) for r in rows),
        refunds=sum(float(r.get("refunds") or 0.0) for r in rows),
        new_customers=sum(int(r.get("new_customers") or 0) for r in rows),
    )


def locate_leak(metrics: FunnelMetrics, policy: Policy) -> tuple[str | None, list[str]]:
    """Find the earliest stage falling below its benchmark.

    Earliest, not worst. A funnel is sequential: fixing checkout while nobody
    clicks changes nothing, and the effort spent proving that is the expensive
    part.
    """
    cfg = _conv(policy)
    unmeasurable: list[str] = []

    if metrics.views is None:
        unmeasurable.append(
            "Reach is unmeasured — no video metrics recorded for this period. "
            "TikTok publishes no organic analytics API, so these are typed in "
            "from the app with `video-metrics`.")
    elif metrics.views < int(cfg.get("min_views_for_diagnosis", 5000)):
        return "reach", unmeasurable

    if metrics.clicks is None:
        unmeasurable.append(
            "Click-through is unmeasured — no link-click figures recorded. "
            "Without it, a creative problem and a landing-page problem are "
            "indistinguishable.")
    elif metrics.click_rate_pct is not None and \
            metrics.click_rate_pct < float(cfg.get("min_click_rate_pct", 0.5)):
        return "click", unmeasurable

    if metrics.sessions is None:
        unmeasurable.append(
            "Sessions are unmeasured. The Shopify Admin API does not expose "
            "session counts, so conversion rate cannot be computed and is "
            "reported as unknown rather than back-computed from orders.")
    elif metrics.conversion_rate_pct is not None and \
            metrics.conversion_rate_pct < float(cfg.get("min_conversion_rate_pct", 1.0)):
        return "landing", unmeasurable

    return None, unmeasurable


def recommend(metrics: FunnelMetrics, policy: Policy,
              *, leak: str | None) -> list[Recommendation]:
    """Recommendations for the located leak, and for economics regardless."""
    cfg = _conv(policy)
    min_sessions = int(cfg.get("min_sessions_for_recommendation", 500))
    min_orders = int(cfg.get("min_orders_for_recommendation", 25))
    recs: list[Recommendation] = []

    sample_note = (f"{metrics.sessions:,} sessions, {metrics.orders} orders"
                   if metrics.sessions is not None
                   else f"{metrics.orders} orders, sessions unknown")
    enough_traffic = (metrics.sessions or 0) >= min_sessions
    enough_orders = metrics.orders >= min_orders

    if leak == "reach":
        recs.append(Recommendation(
            stage="reach", priority="HIGH",
            action="Increase posting cadence and widen the angle mix before "
                   "changing anything on the store.",
            rationale="Distribution is the binding constraint. No page change "
                      "can be measured while the sample upstream is this small.",
            expected_effect="More views; nothing else is diagnosable until then.",
            sample_basis=f"{metrics.views or 0:,} views in period",
        ))
    elif leak == "click":
        recs.append(Recommendation(
            stage="click", priority="HIGH",
            action="Rewrite the CTA and move the offer earlier in the video. "
                   "Test a qualifying CTA against the passive one.",
            rationale="Views without clicks mean the hook earns attention that "
                      "the offer does not convert. That is a creative problem; "
                      "the store is not involved.",
            expected_effect="Higher click rate on the same reach. Expect click "
                            "volume to fall if a qualifying CTA is used — the "
                            "clicks that remain convert better.",
            sample_basis=f"{metrics.views or 0:,} views, {metrics.clicks or 0:,} clicks",
        ))
    elif leak == "landing":
        recs.extend([
            Recommendation(
                stage="landing", priority="HIGH",
                action="Put the video's own footage at the top of the product "
                       "page, above the fold.",
                rationale="The visitor arrived from a specific video and lands "
                          "on a page that looks unrelated to it. Continuity "
                          "between the ad and the page is the largest single "
                          "lever at this stage.",
                expected_effect="Lower bounce; higher conversion on the same traffic.",
                sample_basis=sample_note,
                confident=enough_traffic,
            ),
            Recommendation(
                stage="landing", priority="MEDIUM",
                action="State the delivery window and the return policy above "
                       "the buy button.",
                rationale="Unstated shipping time is the most common silent "
                          "objection for a new store with no brand recognition.",
                expected_effect="Fewer pre-purchase abandonments.",
                sample_basis=sample_note,
                confident=enough_traffic,
            ),
        ])

    # Economics recommendations run regardless of where the leak is, because
    # AOV and repeat rate change profitability without changing traffic.
    if enough_orders and metrics.aov is not None:
        target_aov = float(cfg.get("target_aov_usd", 0) or 0)
        if target_aov and metrics.aov < target_aov:
            recs.append(Recommendation(
                stage="checkout", priority="MEDIUM",
                action=f"Introduce a two-item bundle priced below "
                       f"${metrics.aov * 1.8:,.2f}.",
                rationale=f"AOV is ${metrics.aov:,.2f} against a ${target_aov:,.2f} "
                          "target. On organic traffic the acquisition cost is "
                          "fixed at zero, so AOV is the cheapest lever on profit "
                          "per order.",
                expected_effect="Higher revenue per order at unchanged traffic.",
                sample_basis=sample_note,
            ))
        repeat = metrics.repeat_share_pct
        if repeat is not None and repeat < float(cfg.get("min_repeat_share_pct", 15)):
            recs.append(Recommendation(
                stage="checkout", priority="MEDIUM",
                action="Add a post-purchase offer for the complementary SKU with "
                       "a bounded, dated discount code.",
                rationale=f"Only {repeat:.0f}% of orders are from returning "
                          "customers. A business with no repeat purchase has to "
                          "win every sale from scratch, which caps LTV at one "
                          "order and caps what the product can ever afford.",
                expected_effect="Higher repeat share and LTV; no traffic change.",
                sample_basis=sample_note,
            ))

    refund_rate = metrics.refund_rate_pct
    if refund_rate is not None and enough_orders and \
            refund_rate > float(cfg.get("max_refund_rate_pct", 5)):
        recs.append(Recommendation(
            stage="landing", priority="HIGH",
            action="Audit the product page against the videos for over-promising, "
                   "then correct the page — not the videos.",
            rationale=f"Refund rate is {refund_rate:.1f}%. A high refund rate "
                      "after good conversion usually means the content sold "
                      "something the product is not, which is a compliance "
                      "exposure as well as a margin one.",
            expected_effect="Lower refunds and fewer negative reviews; conversion "
                            "may fall, and that fall is the point.",
            sample_basis=sample_note,
        ))

    if not enough_traffic and metrics.sessions is not None:
        for rec in recs:
            if rec.stage in ("landing", "checkout"):
                rec.confident = False
    return recs


def diagnose_funnel(metrics: FunnelMetrics, policy: Policy) -> FunnelDiagnosis:
    """Locate the leak, then recommend for it. In that order, deliberately."""
    leak, unmeasurable = locate_leak(metrics, policy)
    recs = recommend(metrics, policy, leak=leak)
    cfg = _conv(policy)
    warnings: list[str] = []

    min_sessions = int(cfg.get("min_sessions_for_recommendation", 500))
    if metrics.sessions is not None and metrics.sessions < min_sessions:
        warnings.append(
            f"{metrics.sessions:,} sessions is below the {min_sessions:,} floor "
            "for a store-side recommendation. Anything below it is one or two "
            "orders of noise, and acting on it also destroys the baseline you "
            "would have compared the change against.")
    if leak is None and not unmeasurable:
        warnings.append(
            "No stage is below its benchmark. Optimise for order value and "
            "repeat purchase rather than for conversion rate — there is more "
            "profit in the second order than in the last percent of the first.")
    if unmeasurable:
        warnings.append(
            f"{len(unmeasurable)} funnel stage(s) could not be measured. A "
            "diagnosis skipping a stage can point at the wrong one; the gaps "
            "are listed rather than assumed healthy.")

    return FunnelDiagnosis(metrics=metrics, leak_stage=leak,
                           recommendations=recs, unmeasurable=unmeasurable,
                           warnings=warnings)


def bundle_opportunities(order_lines: list[dict[str, Any]], *,
                         min_co_occurrences: int = 10) -> list[dict[str, Any]]:
    """SKU pairs bought together often enough to be worth bundling.

    Counts real co-occurrence in real orders. The floor exists because with a
    small catalogue every pair co-occurs eventually, and a bundle built on
    three coincidences discounts two products that were selling fine.
    """
    by_order: dict[str, set[str]] = {}
    for line in order_lines:
        order_id = str(line.get("order_id") or "")
        sku = str(line.get("sku") or "")
        if order_id and sku:
            by_order.setdefault(order_id, set()).add(sku)

    pairs: dict[tuple[str, str], int] = {}
    singles: dict[str, int] = {}
    for skus in by_order.values():
        for sku in skus:
            singles[sku] = singles.get(sku, 0) + 1
        ordered = sorted(skus)
        for i, left in enumerate(ordered):
            for right in ordered[i + 1:]:
                pairs[(left, right)] = pairs.get((left, right), 0) + 1

    out = []
    for (left, right), count in sorted(pairs.items(), key=lambda kv: -kv[1]):
        if count < min_co_occurrences:
            continue
        support = count / max(len(by_order), 1)
        out.append({
            "skus": [left, right],
            "orders_together": count,
            "orders_total": len(by_order),
            "support_pct": round(support * 100, 2),
            # Confidence in the association-rule sense: given the first, how
            # often does the second appear. Asymmetric, so both are reported —
            # the bundle should lead with whichever pulls the other.
            "confidence_left_to_right_pct": round(count / singles[left] * 100, 1),
            "confidence_right_to_left_pct": round(count / singles[right] * 100, 1),
        })
    return out
