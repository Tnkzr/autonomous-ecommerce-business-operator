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


def sync_from_connector(store: Store, connector: Any, *, days: int = 30,
                        today: date | None = None) -> SyncResult:
    """Fetch recent orders from Shopify and write the funnel table.

    The window is deliberately re-fetched rather than incremental: refunds and
    fulfilment states change after the order is created, so a row written on
    day one is wrong by day ten. `upsert_storefront_daily` makes the re-write
    idempotent.
    """
    today = today or datetime.now(timezone.utc).date()
    since = (today - timedelta(days=days - 1)).isoformat()
    envelope = connector.fetch_orders(since=since)
    result = sync_orders(store, envelope.payload,
                         data_source=source_of(envelope))
    result.warnings.extend(envelope.warnings)
    return result


def source_of(envelope: Any) -> str:
    """Provenance of a fetch, defaulting to unknown rather than to live."""
    return str(getattr(envelope, "source", "") or "unknown")
