"""Advertising optimisation.

Two ideas do most of the work here:

1. Break-even ACOS comes from the product's real margin, not a global number.
   A 45%-margin product can profitably run at 40% ACOS; a 22%-margin product
   bleeds at 30%. A single "target ACOS" applied portfolio-wide is the most
   common and most expensive advertising mistake.

2. Statistical patience. Acting on 3 clicks is noise-chasing. Nothing is paused,
   scaled, or negatived until the minimum click/spend evidence exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Policy
from .models import Campaign, money


@dataclass
class KeywordStat:
    keyword: str
    match_type: str
    clicks: int
    impressions: int
    spend: float
    sales: float
    orders: int
    current_bid: float

    @property
    def acos_pct(self) -> float:
        if self.sales <= 0:
            return float("inf") if self.spend > 0 else 0.0
        return round(self.spend / self.sales * 100, 2)

    @property
    def cpc(self) -> float:
        return money(self.spend / self.clicks) if self.clicks else 0.0

    @property
    def cvr_pct(self) -> float:
        return round(self.orders / self.clicks * 100, 2) if self.clicks else 0.0


@dataclass
class AdAction:
    target: str          # campaign id or keyword
    level: str           # CAMPAIGN | KEYWORD
    action: str          # PAUSE | INCREASE_BUDGET | DECREASE_BID | INCREASE_BID | NEGATE | HARVEST | HOLD
    current_value: float
    proposed_value: float
    rationale: str
    projected_monthly_impact: float = 0.0
    requires_approval: bool = False


@dataclass
class AdReview:
    campaign: Campaign
    break_even_acos_pct: float
    verdict: str         # SCALE | HEALTHY | OPTIMISE | PAUSE | INSUFFICIENT_DATA
    actions: list[AdAction] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def review_campaign(
    policy: Policy,
    campaign: Campaign,
    *,
    product_margin_pct: float,
    weekly_budget_increase_so_far_pct: float = 0.0,
) -> AdReview:
    cfg = policy.advertising
    notes: list[str] = []
    actions: list[AdAction] = []

    # Break-even ACOS is simply the product's pre-ad margin: spend more than the
    # margin to acquire the sale and the sale loses money.
    break_even = round(max(product_margin_pct, 0.0), 2)
    notes.append(
        f"Break-even ACOS for this SKU is {break_even:.1f}% (derived from its "
        f"{product_margin_pct:.1f}% pre-ad margin), not the portfolio default."
    )

    min_clicks = int(cfg["min_clicks_before_judgement"])
    min_spend = float(cfg["min_spend_before_judgement"])
    if campaign.clicks < min_clicks and campaign.spend < min_spend:
        notes.append(
            f"Only {campaign.clicks} clicks / ${campaign.spend:.2f} spent — below the "
            f"{min_clicks}-click, ${min_spend:.2f} evidence bar. Holding: acting now "
            "would be reacting to noise."
        )
        return AdReview(campaign, break_even, "INSUFFICIENT_DATA", actions, notes)

    acos = campaign.acos_pct
    target_acos = float(cfg["target_acos_pct"])
    max_acos = float(cfg["max_acos_pct"])
    pause_acos = float(cfg["pause_acos_pct"])
    scale_below = float(cfg["scale_only_if_acos_below_pct"])

    if campaign.orders == 0:
        zero_thresh = int(cfg["zero_sale_click_threshold"])
        if campaign.clicks >= zero_thresh:
            actions.append(
                AdAction(
                    target=campaign.campaign_id,
                    level="CAMPAIGN",
                    action="PAUSE",
                    current_value=campaign.daily_budget,
                    proposed_value=0.0,
                    rationale=(
                        f"{campaign.clicks} clicks and ${campaign.spend:.2f} spent with zero "
                        "orders. Pure loss with no evidence of conversion."
                    ),
                    projected_monthly_impact=money(campaign.spend / max(campaign.period_days, 1) * 30),
                    requires_approval=campaign.daily_budget >= float(
                        policy.approval_threshold("ad_budget_change_usd") or 0.0
                    ),
                )
            )
            return AdReview(campaign, break_even, "PAUSE", actions, notes)
        notes.append(
            f"No orders yet at {campaign.clicks} clicks (pause line is {zero_thresh}). "
            "Still inside the learning window."
        )
        return AdReview(campaign, break_even, "INSUFFICIENT_DATA", actions, notes)

    if acos >= pause_acos or acos >= break_even * 1.5:
        actions.append(
            AdAction(
                target=campaign.campaign_id,
                level="CAMPAIGN",
                action="PAUSE",
                current_value=campaign.daily_budget,
                proposed_value=0.0,
                rationale=(
                    f"ACOS {acos:.1f}% against a {break_even:.1f}% break-even. Every "
                    f"additional sale destroys roughly ${campaign.sales * (acos - break_even) / 100 / max(campaign.orders, 1):.2f} "
                    "of value."
                ),
                projected_monthly_impact=money(
                    campaign.spend * (acos - break_even) / 100 / max(campaign.period_days, 1) * 30
                ),
                requires_approval=campaign.daily_budget >= float(
                    policy.approval_threshold("ad_budget_change_usd") or 0.0
                ),
            )
        )
        verdict = "PAUSE"
    elif acos > max_acos:
        new_budget = money(campaign.daily_budget * 0.7)
        actions.append(
            AdAction(
                target=campaign.campaign_id,
                level="CAMPAIGN",
                action="DECREASE_BID",
                current_value=campaign.daily_budget,
                proposed_value=new_budget,
                rationale=(
                    f"ACOS {acos:.1f}% is over the {max_acos:.1f}% ceiling but under "
                    f"break-even ({break_even:.1f}%) — still contributing. Cutting budget "
                    "30% and tightening bids rather than killing a working campaign."
                ),
                requires_approval=abs(campaign.daily_budget - new_budget) >= float(
                    policy.approval_threshold("ad_budget_change_usd") or 0.0
                ),
            )
        )
        verdict = "OPTIMISE"
    elif acos <= scale_below:
        capacity = float(cfg["max_budget_increase_pct_per_week"]) - weekly_budget_increase_so_far_pct
        if capacity <= 0:
            notes.append(
                "Campaign is profitable and deserves more budget, but the weekly "
                f"{float(cfg['max_budget_increase_pct_per_week']):.0f}% scaling cap is spent. "
                "Scaling faster than this destabilises ACOS. Resumes next week."
            )
            verdict = "HEALTHY"
        else:
            step = min(float(cfg["budget_increase_step_pct"]), capacity)
            new_budget = money(campaign.daily_budget * (1 + step / 100))
            extra_daily = new_budget - campaign.daily_budget
            profit_rate = (break_even - acos) / 100.0
            actions.append(
                AdAction(
                    target=campaign.campaign_id,
                    level="CAMPAIGN",
                    action="INCREASE_BUDGET",
                    current_value=campaign.daily_budget,
                    proposed_value=new_budget,
                    rationale=(
                        f"ACOS {acos:.1f}% vs {break_even:.1f}% break-even and "
                        f"{campaign.roas:.2f}x ROAS. Profitable — scaling {step:.0f}%."
                    ),
                    projected_monthly_impact=money(
                        extra_daily * 30 * (1 / max(acos / 100, 0.01)) * profit_rate
                    ),
                    requires_approval=extra_daily * 30 >= float(
                        policy.approval_threshold("ad_budget_change_usd") or 0.0
                    ),
                )
            )
            verdict = "SCALE"
    elif acos <= target_acos:
        notes.append(f"ACOS {acos:.1f}% is inside target ({target_acos:.1f}%). No change.")
        verdict = "HEALTHY"
    else:
        notes.append(
            f"ACOS {acos:.1f}% sits between target ({target_acos:.1f}%) and ceiling "
            f"({max_acos:.1f}%). Monitoring; no action warranted yet."
        )
        verdict = "OPTIMISE"

    if campaign.ctr_pct < 0.3 and campaign.impressions > 5000:
        notes.append(
            f"CTR {campaign.ctr_pct:.3f}% on {campaign.impressions:,} impressions is weak. "
            "This is a creative/relevance problem, not a bidding one — fix the main "
            "image and title before spending more."
        )
    if campaign.cvr_pct < 5 and campaign.clicks > 50:
        notes.append(
            f"CVR {campaign.cvr_pct:.1f}% on {campaign.clicks} clicks. Traffic arrives and "
            "does not buy — the listing or price is the bottleneck, not the campaign."
        )

    return AdReview(campaign, break_even, verdict, actions, notes)


def review_keywords(policy: Policy, keywords: list[KeywordStat],
                    *, break_even_acos_pct: float) -> list[AdAction]:
    """Per-keyword actions: negate the wasters, harvest and scale the winners."""
    cfg = policy.advertising
    min_clicks = int(cfg["min_clicks_before_judgement"])
    zero_thresh = int(cfg["zero_sale_click_threshold"])
    actions: list[AdAction] = []

    for kw in keywords:
        if kw.orders == 0 and kw.clicks >= zero_thresh:
            actions.append(
                AdAction(
                    target=kw.keyword,
                    level="KEYWORD",
                    action="NEGATE",
                    current_value=kw.current_bid,
                    proposed_value=0.0,
                    rationale=(
                        f"{kw.clicks} clicks, ${kw.spend:.2f} spent, zero orders. "
                        "Add as a negative exact to stop the leak."
                    ),
                    projected_monthly_impact=money(kw.spend),
                )
            )
            continue

        if kw.clicks < min_clicks:
            continue

        if kw.orders > 0 and kw.acos_pct <= break_even_acos_pct * 0.6:
            actions.append(
                AdAction(
                    target=kw.keyword,
                    level="KEYWORD",
                    action="HARVEST",
                    current_value=kw.current_bid,
                    proposed_value=money(kw.current_bid * 1.25),
                    rationale=(
                        f"ACOS {kw.acos_pct:.1f}% at {kw.cvr_pct:.1f}% CVR — well inside "
                        f"break-even ({break_even_acos_pct:.1f}%). Move to an exact-match "
                        "campaign and raise the bid 25% to take more of the impression share."
                    ),
                )
            )
        elif kw.orders > 0 and kw.acos_pct > break_even_acos_pct:
            # Bid down proportionally to how far past break-even we are, rather
            # than cutting a converting keyword outright.
            ratio = break_even_acos_pct / max(kw.acos_pct, 0.01)
            new_bid = money(max(kw.current_bid * ratio, 0.05))
            actions.append(
                AdAction(
                    target=kw.keyword,
                    level="KEYWORD",
                    action="DECREASE_BID",
                    current_value=kw.current_bid,
                    proposed_value=new_bid,
                    rationale=(
                        f"ACOS {kw.acos_pct:.1f}% exceeds break-even {break_even_acos_pct:.1f}%. "
                        f"It converts, so bid down to ${new_bid:.2f} rather than cutting it."
                    ),
                    projected_monthly_impact=money((kw.current_bid - new_bid) * kw.clicks),
                )
            )

    actions.sort(key=lambda a: -a.projected_monthly_impact)
    return actions


def portfolio_ad_summary(reviews: list[AdReview]) -> dict[str, float]:
    spend = sum(r.campaign.spend for r in reviews)
    sales = sum(r.campaign.sales for r in reviews)
    return {
        "total_spend": money(spend),
        "total_ad_sales": money(sales),
        "blended_acos_pct": round(spend / sales * 100, 2) if sales else 0.0,
        "blended_roas": round(sales / spend, 2) if spend else 0.0,
        "campaigns": len(reviews),
        "to_pause": sum(1 for r in reviews if r.verdict == "PAUSE"),
        "to_scale": sum(1 for r in reviews if r.verdict == "SCALE"),
    }
