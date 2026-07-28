"""Domain models.

Money is carried as float dollars for readability, and every value that leaves
the system is rounded at the boundary via `money()`. If this ever handles real
settlement reconciliation, migrate to integer cents — float accumulation drift
is real, it just does not bite at the scale of per-unit decision math.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from enum import Enum
from typing import Any


def money(value: float) -> float:
    """Round to cents, half-up-ish. Use at every output boundary."""
    return round(value + 1e-9, 2)


class Marketplace(str, Enum):
    AMAZON = "amazon"
    SHOPIFY = "shopify"
    WALMART = "walmart"
    EBAY = "ebay"
    TIKTOK = "tiktok"


class Decision(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    NEEDS_HUMAN_APPROVAL = "NEEDS_HUMAN_APPROVAL"
    HOLD = "HOLD"


class Severity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


@dataclass
class Supplier:
    supplier_id: str
    name: str
    country: str
    rating: float                      # 0-5 platform rating
    unit_cost: float                   # ex-works per unit
    moq: int
    shipping_cost_per_unit: float
    shipping_days: int
    domestic_stock: bool = False
    quality_score: float = 0.0         # 0-100, from samples/defect history
    communication_score: float = 0.0   # 0-100, responsiveness
    inventory_stability: float = 0.0   # 0-100, historical fill rate
    defect_rate_pct: float = 0.0
    on_time_rate_pct: float = 100.0
    tooling_fee: float = 0.0           # one-off, amortised separately
    notes: str = ""

    @property
    def landed_unit_cost(self) -> float:
        """Ex-works + freight. Duties are added by the economics layer."""
        return money(self.unit_cost + self.shipping_cost_per_unit)


@dataclass
class ProductCandidate:
    sku: str
    title: str
    category: str
    marketplace: str
    target_price: float
    supplier: Supplier
    est_monthly_demand_units: int
    competitor_count: int = 0
    top_rival_review_count: int = 0
    avg_rival_price: float = 0.0
    avg_rival_rating: float = 0.0
    weight_lb: float = 1.0
    duty_pct: float = 0.0
    keywords: list[str] = field(default_factory=list)
    description: str = ""
    brand: str = ""
    is_gated_category: bool = False
    gated_approval_on_file: bool = False

    def searchable_text(self) -> str:
        """Everything a compliance screen should read."""
        parts = [self.title, self.description, self.category, self.brand, *self.keywords]
        return " ".join(p for p in parts if p).lower()


@dataclass
class UnitEconomics:
    """Full per-unit P&L for one product on one marketplace."""

    sku: str
    marketplace: str
    sale_price: float
    landed_cost: float
    duty: float
    referral_fee: float
    fulfillment_fee: float
    payment_fee: float
    storage_fee: float
    return_cost: float
    ad_cost_per_unit: float
    misc_cost: float

    @property
    def total_cost(self) -> float:
        return money(
            self.landed_cost + self.duty + self.referral_fee + self.fulfillment_fee
            + self.payment_fee + self.storage_fee + self.return_cost
            + self.ad_cost_per_unit + self.misc_cost
        )

    @property
    def net_profit(self) -> float:
        return money(self.sale_price - self.total_cost)

    @property
    def margin_pct(self) -> float:
        if self.sale_price <= 0:
            return 0.0
        return round(self.net_profit / self.sale_price * 100, 2)

    @property
    def roi_pct(self) -> float:
        """Return on the cash actually tied up in goods (landed + duty)."""
        invested = self.landed_cost + self.duty
        if invested <= 0:
            return 0.0
        return round(self.net_profit / invested * 100, 2)

    @property
    def break_even_acos_pct(self) -> float:
        """Max ad spend as % of revenue before this unit stops making money.

        Computed on profit *before* advertising, since ad spend is the variable
        under test.
        """
        if self.sale_price <= 0:
            return 0.0
        profit_before_ads = self.net_profit + self.ad_cost_per_unit
        return round(max(profit_before_ads, 0.0) / self.sale_price * 100, 2)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.update(
            total_cost=self.total_cost,
            net_profit=self.net_profit,
            margin_pct=self.margin_pct,
            roi_pct=self.roi_pct,
            break_even_acos_pct=self.break_even_acos_pct,
        )
        return d


@dataclass
class GateResult:
    """Outcome of one policy gate."""

    name: str
    passed: bool
    detail: str
    severity: Severity = Severity.INFO
    blocking: bool = True


@dataclass
class ScreeningResult:
    sku: str
    decision: Decision
    gates: list[GateResult]
    economics: UnitEconomics | None = None
    score: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def failures(self) -> list[GateResult]:
        return [g for g in self.gates if not g.passed]

    @property
    def blocking_failures(self) -> list[GateResult]:
        return [g for g in self.gates if not g.passed and g.blocking]

    def reason_summary(self) -> str:
        fails = self.failures
        if not fails:
            return "All gates passed."
        return "; ".join(f"{g.name}: {g.detail}" for g in fails)


@dataclass
class InventoryItem:
    sku: str
    marketplace: str
    on_hand_units: int
    inbound_units: int
    daily_velocity: float              # trailing units/day
    velocity_stddev: float = 0.0
    lead_time_days: int = 30
    unit_cost: float = 0.0
    moq: int = 50


@dataclass
class Campaign:
    campaign_id: str
    name: str
    sku: str
    marketplace: str
    spend: float
    sales: float
    clicks: int
    impressions: int
    orders: int
    daily_budget: float
    period_days: int = 30

    @property
    def acos_pct(self) -> float:
        if self.sales <= 0:
            return float("inf") if self.spend > 0 else 0.0
        return round(self.spend / self.sales * 100, 2)

    @property
    def roas(self) -> float:
        if self.spend <= 0:
            return 0.0
        return round(self.sales / self.spend, 2)

    @property
    def ctr_pct(self) -> float:
        if self.impressions <= 0:
            return 0.0
        return round(self.clicks / self.impressions * 100, 3)

    @property
    def cvr_pct(self) -> float:
        if self.clicks <= 0:
            return 0.0
        return round(self.orders / self.clicks * 100, 2)

    @property
    def cpc(self) -> float:
        if self.clicks <= 0:
            return 0.0
        return money(self.spend / self.clicks)


@dataclass
class CompetitorOffer:
    seller: str
    price: float
    rating: float = 0.0
    review_count: int = 0
    is_prime: bool = False
    in_stock: bool = True
    is_buybox: bool = False


@dataclass
class Review:
    review_id: str
    sku: str
    marketplace: str
    rating: int
    title: str
    body: str
    created_at: str
    verified: bool = True


@dataclass
class ActionRecord:
    """One journaled decision. This is the memory that makes the loop improve."""

    action_id: str
    created_at: str
    domain: str                        # sourcing | pricing | inventory | ads | listing
    sku: str
    action: str
    rationale: str
    decision: str
    inputs_json: str
    expected_outcome: str
    requires_approval: bool = False
    approved_by: str = ""
    executed: bool = False
    outcome_json: str = ""
    outcome_recorded_at: str = ""


def today_iso() -> str:
    return date.today().isoformat()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
