"""Shopify orders → the daily funnel table.

The missing joint. `conversion.py` and `dashboard.py` both read
`storefront_daily`, and until this module existed nothing wrote it: the funnel
was permanently empty and said so honestly, which is correct behaviour for a
gap and useless as a product.

What this does is narrow on purpose — it aggregates orders by day and traffic
channel and writes them down. Three rules make that aggregation trustworthy.

**Cancelled orders are not revenue.** A cancelled order is money that never
arrived. Counting it inflates revenue and conversion at the same time, which is
the worst combination: it makes a bad channel look good and hides the fix.

**Refunds are subtracted where they were earned, not where they were
processed.** A refund is written against the order's own day and channel. Netting
it off the day the refund was issued makes a good week look bad three weeks
later and detaches the loss from the traffic that caused it.

**Sessions are never invented.** The Admin API does not expose them, so the
column stays NULL and every downstream rate that needs it reports unknown.
Deriving sessions from orders and an assumed conversion rate would make the
conversion rate a restatement of the assumption.

Provenance is `live` when it came from the API. Do not relabel it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .store import Store

# Financial states where money has not arrived and may never. Shopify reports
# these as orders, and they are — they are just not revenue.
NON_REVENUE_STATUSES = frozenset({
    "VOIDED", "REFUNDED", "EXPIRED", "PENDING",
})


@dataclass
class SyncResult:
    days_written: int
    orders_seen: int
    orders_counted: int
    orders_excluded: int
    channels: list[str] = field(default_factory=list)
    unattributed_share_pct: float | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "days_written": self.days_written,
            "orders_seen": self.orders_seen,
            "orders_counted": self.orders_counted,
            "orders_excluded": self.orders_excluded,
            "channels": self.channels,
            "unattributed_share_pct": self.unattributed_share_pct,
            "warnings": self.warnings,
        }


def _day_of(timestamp: str) -> str:
    """Calendar day of an order, in UTC.

    UTC rather than the shop's timezone, deliberately and consistently: mixing
    the two produces days that overlap or have gaps, and a funnel computed over
    overlapping days double-counts the orders in the seam.
    """
    try:
        moment = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).date().isoformat()


def sync_orders(store: Store, orders: list[Any], *,
                data_source: str = "live") -> SyncResult:
    """Aggregate `ShopifyOrderSummary` objects into `storefront_daily`.

    Takes summaries rather than a connector so the same code path serves a live
    fetch, a replay, and a test without any of them needing a network.
    """
    buckets: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: {"orders": 0.0, "revenue": 0.0, "refunds": 0.0,
                 "new_customers": 0.0})
    counted = excluded = 0
    unattributed = 0
    warnings: list[str] = []

    for order in orders:
        day = _day_of(getattr(order, "created_at", ""))
        if not day:
            excluded += 1
            warnings.append(
                f"Order {getattr(order, 'name', '?')} has an unreadable "
                "created_at and was excluded rather than bucketed into today.")
            continue

        if getattr(order, "cancelled", False):
            excluded += 1
            continue
        status = str(getattr(order, "financial_status", "") or "").upper()
        if status in NON_REVENUE_STATUSES:
            excluded += 1
            continue

        attribution = getattr(order, "attribution", None)
        channel = getattr(attribution, "channel", "unattributed")
        if channel == "unattributed":
            unattributed += 1

        bucket = buckets[(day, channel)]
        bucket["orders"] += 1
        # gross less refunds, tax and shipping — see ShopifyOrderSummary.
        bucket["revenue"] += float(getattr(order, "net_revenue", 0.0))
        bucket["refunds"] += float(getattr(order, "refunded", 0.0))
        if int(getattr(order, "customer_order_count", 1) or 1) <= 1:
            bucket["new_customers"] += 1
        counted += 1

    for (day, channel), totals in sorted(buckets.items()):
        store.upsert_storefront_daily(
            metric_date=day, channel=channel,
            # Left NULL on purpose. See the module docstring.
            sessions=None,
            orders=int(totals["orders"]),
            revenue=round(totals["revenue"], 2),
            refunds=round(totals["refunds"], 2),
            new_customers=int(totals["new_customers"]),
            data_source=data_source)

    share = round(unattributed / counted * 100, 1) if counted else None
    if share is not None and share > 30:
        warnings.append(
            f"{share}% of counted orders have no resolvable traffic source. "
            "Channel figures are computed on the attributable remainder and "
            "understate every channel — including the one this business runs "
            "on. Per-video utm_content tags on the bio link are what close this.")
    if excluded:
        warnings.append(
            f"{excluded} order(s) excluded as cancelled, voided, or unreadable. "
            "A cancelled order is money that never arrived; counting it would "
            "inflate revenue and conversion at the same time.")
    if counted and data_source != "live":
        warnings.append(
            f"Written with provenance '{data_source}'. Every report built on "
            "these rows keeps that label.")

    return SyncResult(
        days_written=len({day for day, _c in buckets}),
        orders_seen=len(orders), orders_counted=counted,
        orders_excluded=excluded,
        channels=sorted({channel for _d, channel in buckets}),
        unattributed_share_pct=share, warnings=warnings)


def sync_line_items(store: Store, orders: list[Any], policy: Any, *,
                    data_source: str = "live",
                    marketplace: str = "shopify") -> SyncResult:
    """Aggregate order line items into per-SKU `daily_metrics`.

    The companion to `sync_orders`: that one answers "how did the store do",
    this one answers "which product made the money". They are separate tables
    because they answer to different denominators — a two-SKU order is one
    order and two product rows, and forcing both through one table makes every
    per-product rate wrong by the basket size.

    Cost of goods comes from the variant's `unitCost`, which Shopify only holds
    if the operator filled in "cost per item". Where it is missing, `cogs` and
    `net_profit` are left at zero and the SKU is named in the warnings — a
    guessed cost produces a confident margin, and a confident margin on a
    guessed cost is how a loss-making product gets scaled.
    """
    fees_cfg = _fee_schedule(policy, marketplace)
    buckets: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: {"units": 0.0, "revenue": 0.0, "cogs": 0.0, "fees": 0.0,
                 "refunds": 0.0})
    costed: set[str] = set()
    uncosted: set[str] = set()
    counted = excluded = 0

    for order in orders:
        day = _day_of(getattr(order, "created_at", ""))
        if not day or getattr(order, "cancelled", False):
            excluded += 1
            continue
        if str(getattr(order, "financial_status", "") or "").upper() \
                in NON_REVENUE_STATUSES:
            excluded += 1
            continue

        lines = getattr(order, "line_items", None) or []
        order_refund = float(getattr(order, "refunded", 0.0) or 0.0)
        order_revenue = sum(float(li.get("revenue") or 0.0) for li in lines)

        for line in lines:
            sku = str(line.get("sku") or "").strip()
            if not sku:
                # An unmapped line cannot be attributed to a product. Counting
                # it under a blank SKU would create a phantom best-seller.
                excluded += 1
                continue
            units = int(line.get("quantity") or 0)
            revenue = float(line.get("revenue") or 0.0)
            unit_cost = line.get("unit_cost")

            bucket = buckets[(day, sku)]
            bucket["units"] += units
            bucket["revenue"] += revenue
            bucket["fees"] += _estimate_fees(revenue, units, fees_cfg)
            # Refunds are apportioned by the line's share of the order, since
            # Shopify's refund total is per order, not per line.
            if order_refund and order_revenue > 0:
                bucket["refunds"] += order_refund * (revenue / order_revenue)
            if unit_cost is None:
                uncosted.add(sku)
            else:
                costed.add(sku)
                bucket["cogs"] += float(unit_cost) * units
            counted += 1

    for (day, sku), totals in sorted(buckets.items()):
        has_cost = sku in costed and sku not in uncosted
        net = (totals["revenue"] - totals["cogs"] - totals["fees"]
               - totals["refunds"]) if has_cost else 0.0
        store.upsert_daily_metric(
            metric_date=day, marketplace=marketplace, sku=sku,
            units=int(totals["units"]), revenue=round(totals["revenue"], 2),
            cogs=round(totals["cogs"], 2), fees=round(totals["fees"], 2),
            ad_spend=0.0, refunds=round(totals["refunds"], 2),
            net_profit=round(net, 2), data_source=data_source)

    warnings: list[str] = []
    if uncosted:
        names = ", ".join(sorted(uncosted)[:8])
        warnings.append(
            f"{len(uncosted)} SKU(s) have no cost per item set in Shopify "
            f"({names}). Their profit is recorded as 0 rather than estimated — "
            "a guessed cost produces a confident margin, and a confident margin "
            "on a guessed cost is how a loss-making product gets scaled. Set "
            "cost per item on the variant.")
    warnings.append(
        "Fees are estimated from [fees." + marketplace + "] in policy.toml, not "
        "read from payouts. Reconcile them against a real payout before "
        "trusting per-product profit.")

    return SyncResult(
        days_written=len({day for day, _s in buckets}),
        orders_seen=len(orders), orders_counted=counted,
        orders_excluded=excluded,
        channels=sorted({sku for _d, sku in buckets}),
        unattributed_share_pct=None, warnings=warnings)


def _fee_schedule(policy: Any, marketplace: str) -> dict[str, float]:
    fees = (getattr(policy, "raw", {}) or {}).get("fees", {})
    return {k: float(v) for k, v in (fees.get(marketplace) or {}).items()
            if isinstance(v, (int, float))}


def _estimate_fees(revenue: float, units: int, cfg: dict[str, float]) -> float:
    """Marketplace fees on one line, from the policy schedule.

    An estimate and labelled as one. `[fees.*]` values drive every profit
    number in the system and are unverified until reconciled against a real
    payout — see the note in CLAUDE.md before changing them.
    """
    referral = revenue * cfg.get("referral_pct", 0.0) / 100.0
    payment = revenue * cfg.get("payment_pct", 0.0) / 100.0
    payment += cfg.get("payment_flat", 0.0) * (1 if revenue else 0)
    fulfilment = cfg.get("fulfillment_flat", 0.0) * max(units, 0)
    return round(referral + payment + fulfilment, 2)


def sync_from_connector(store: Store, connector: Any, *, days: int = 30,
                        today: date | None = None,
                        policy: Any = None) -> SyncResult:
    """Fetch recent orders from Shopify and write the funnel table.

    The window is deliberately re-fetched rather than incremental: refunds and
    fulfilment states change after the order is created, so a row written on
    day one is wrong by day ten. `upsert_storefront_daily` makes the re-write
    idempotent.
    """
    today = today or datetime.now(timezone.utc).date()
    since = (today - timedelta(days=days - 1)).isoformat()
    envelope = connector.fetch_orders(since=since)
    source = source_of(envelope)
    result = sync_orders(store, envelope.payload, data_source=source)
    result.warnings.extend(envelope.warnings)
    if policy is not None:
        # Per-SKU rows too, when a policy is available to price the fees. Both
        # come from the same fetch so the channel table and the product table
        # can never describe different days.
        per_sku = sync_line_items(store, envelope.payload, policy,
                                  data_source=source)
        result.warnings.extend(per_sku.warnings)
    else:
        result.warnings.append(
            "Per-SKU profit was not written: no policy was supplied, so fees "
            "could not be priced. Revenue by channel is available; profit by "
            "product is not.")
    return result


def source_of(envelope: Any) -> str:
    """Provenance of a fetch, defaulting to unknown rather than to live."""
    return str(getattr(envelope, "source", "") or "unknown")


# ---------------------------------------------------------------------------
# eBay
# ---------------------------------------------------------------------------
def sync_ebay_orders(store: Store, orders: list[Any], policy: Any, *,
                     data_source: str = "live") -> SyncResult:
    """Aggregate eBay order summaries into the funnel and per-SKU tables.

    eBay has no traffic-source concept — a buyer arrived through eBay search,
    and that is all anyone knows. So every row lands in a single `ebay` channel
    rather than being split into invented sources. Reporting a channel
    breakdown here would be fabricating a distinction the platform does not
    make.
    """
    fees_cfg = _fee_schedule(policy, "ebay")
    daily: dict[str, dict[str, float]] = defaultdict(
        lambda: {"orders": 0.0, "revenue": 0.0, "new_customers": 0.0})
    per_sku: dict[tuple[str, str], dict[str, float]] = defaultdict(
        lambda: {"units": 0.0, "revenue": 0.0, "fees": 0.0})
    seen_buyers: set[str] = set()
    counted = excluded = 0
    warnings: list[str] = []

    for order in orders:
        day = _day_of(getattr(order, "created_at", ""))
        # `is_revenue` already excludes cancelled and unpaid. Counting either
        # would overstate sales and conversion at the same time.
        if not day or not getattr(order, "is_revenue", False):
            excluded += 1
            continue

        bucket = daily[day]
        bucket["orders"] += 1
        bucket["revenue"] += float(getattr(order, "gross_total", 0.0))
        buyer = str(getattr(order, "buyer_key", "") or "")
        if buyer and buyer not in seen_buyers:
            seen_buyers.add(buyer)
            bucket["new_customers"] += 1

        for line in (getattr(order, "line_items", None) or []):
            sku = str(line.get("sku") or "").strip()
            if not sku:
                continue
            revenue = float(line.get("revenue") or 0.0)
            units = int(line.get("quantity") or 0)
            row = per_sku[(day, sku)]
            row["units"] += units
            row["revenue"] += revenue
            row["fees"] += _estimate_fees(revenue, units, fees_cfg)
        counted += 1

    for day, totals in sorted(daily.items()):
        store.upsert_storefront_daily(
            metric_date=day, channel="ebay",
            # eBay exposes listing views through the Analytics traffic report,
            # not per session. Left unset rather than approximated.
            sessions=None,
            orders=int(totals["orders"]), revenue=round(totals["revenue"], 2),
            refunds=0.0, new_customers=int(totals["new_customers"]),
            data_source=data_source)

    for (day, sku), totals in sorted(per_sku.items()):
        store.upsert_daily_metric(
            metric_date=day, marketplace="ebay", sku=sku,
            units=int(totals["units"]), revenue=round(totals["revenue"], 2),
            # Cost of goods is not on an eBay order — it comes from the
            # purchase order, which the platform never sees. Left at zero so
            # profit reads as unknown rather than as revenue.
            cogs=0.0, fees=round(totals["fees"], 2), ad_spend=0.0, refunds=0.0,
            net_profit=0.0, data_source=data_source)

    if per_sku:
        warnings.append(
            "Per-SKU profit is 0 because eBay does not know what the goods "
            "cost — that comes from the purchase order. Set cost on the "
            "inventory item, or profit stays unknown rather than being guessed.")
    if excluded:
        warnings.append(
            f"{excluded} order(s) excluded as unpaid, cancelled, or undated.")
    warnings.append(
        "Fees are estimated from [fees.ebay]. Run `ebay-fees` to reconcile them "
        "against what eBay actually charged.")

    return SyncResult(
        days_written=len(daily), orders_seen=len(orders), orders_counted=counted,
        orders_excluded=excluded, channels=["ebay"] if daily else [],
        unattributed_share_pct=None, warnings=warnings)
