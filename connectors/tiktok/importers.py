"""Seller Center CSV/TSV import.

TikTok Shop's Open API is gated on seller eligibility, but Seller Center will
export orders, products, and settlements to a file today, for any shop, with no
application process. That export is **real business data** — it is the same data
the API would return, moved by hand instead of over HTTP.

So this module is not a fallback or a stand-in. It is a second, legitimate data
path with different properties: real but manual, therefore accurate but stale.
Provenance is tracked as `import` rather than `live` or `seed` precisely so the
reporting layer can say "these are your real numbers, exported on the 3rd"
instead of either pretending they are live or dismissing them as simulated.

Two things this must get right:

**Header drift.** TikTok changes column names between export versions and
between regions. Matching one exact string produces an importer that silently
reads zero rows after an unrelated platform update, so every field is matched
against a set of known aliases and an unmatched required column is a loud error
naming the headers actually present.

**Money in exported strings.** Values arrive as "US$12.34", "1,234.56", "12,34"
(EU decimal comma), or "(4.50)" for negatives. Parsing these with `float()`
either raises or, worse, silently truncates.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


class ImportError_(ValueError):
    """Raised when a file cannot be read as the export type it claims to be."""


# Column aliases, lowercased and stripped. TikTok's headers differ across
# export versions and regions; the importer matches any of these.
ORDER_COLUMNS = {
    "order_id": ("order id", "order_id", "orderid", "order no.", "order number"),
    "created_at": ("created time", "order created time", "create time",
                   "created_at", "order time", "paid time"),
    "status": ("order status", "status", "order_status"),
    "sku": ("seller sku", "seller_sku", "sku", "sku id", "merchant sku"),
    "product_name": ("product name", "product_name", "item name"),
    "quantity": ("quantity", "qty", "item quantity", "units"),
    "unit_price": ("sku unit original price", "unit price", "original price",
                   "sku_unit_original_price"),
    "order_total": ("order amount", "total amount", "grand total", "order_total",
                    "payment amount", "sku subtotal after discount"),
    "buyer": ("buyer username", "buyer", "customer", "user id", "buyer_id"),
    "currency": ("currency", "payment currency"),
}

SETTLEMENT_COLUMNS = {
    "statement_id": ("statement id", "statement_id", "settlement id"),
    "order_id": ("order id", "order_id", "related order id"),
    "settlement_time": ("statement time", "settlement time", "payout time",
                        "statement_time"),
    "revenue": ("revenue", "total revenue", "gross revenue", "subtotal after discount"),
    "fees": ("fee", "fees", "total fees", "platform commission", "tiktok commission"),
    "affiliate": ("affiliate commission", "affiliate_commission",
                  "creator commission", "partner commission"),
    "settlement_amount": ("settlement amount", "net settlement", "total settlement",
                          "settlement_amount", "payout amount"),
    "currency": ("currency", "settlement currency"),
}

PRODUCT_COLUMNS = {
    "product_id": ("product id", "product_id", "productid"),
    "sku": ("seller sku", "seller_sku", "sku", "sku id"),
    "title": ("product name", "title", "product_name"),
    "status": ("status", "product status", "listing status"),
    "price": ("price", "sale price", "retail price", "current price"),
    "stock": ("stock", "quantity", "available stock", "inventory"),
}

# Statuses that did not result in money changing hands. Counting these as sales
# inflates revenue, repeat-purchase rate, and every downstream LTV figure.
NON_SALE_STATUSES = frozenset({
    "unpaid", "cancelled", "canceled", "on hold", "on_hold", "closed",
    "payment failed", "expired",
})


@dataclass
class ImportResult:
    kind: str                       # orders | settlements | products
    source_path: str
    exported_at: str | None
    rows: list[dict[str, Any]] = field(default_factory=list)
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    column_map: dict[str, str] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.rows)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
# An empty cell and an unreadable cell are different facts. Only these exact
# markers mean "the exporter had nothing to put here"; everything else that
# fails to parse is an error, never a zero.
BLANK_MARKERS = frozenset({"", "-", "--", "n/a", "na", "null", "none", "—"})

# A currency affix sits at one end or the other: "US$12.34", "IDR 1.234.567",
# "12.34 USD". Anything non-numeric *between* digits is prose, not an amount.
_LEADING_AFFIX = re.compile(r"^[^\d\-]+")
_TRAILING_AFFIX = re.compile(r"[^\d]+$")
_HAS_LETTER = re.compile(r"[A-Za-z]")
_HAS_DIGIT = re.compile(r"\d")


def _unparseable(raw: Any, kind: str) -> ImportError_:
    return ImportError_(
        f"Could not parse {raw!r} as {kind}. Refusing to treat it as zero — a "
        "silently dropped amount corrupts every total built from this file, and "
        "the error surfaces a month later as an unexplained reconciliation gap."
    )


def parse_money(raw: Any) -> float:
    """Parse a money value out of an exported string.

    Handles currency symbols and codes, thousands separators, European decimal
    commas, and parenthesised negatives. Returns 0.0 only for the recognised
    blank markers; anything else that cannot be read raises, because a silently
    zeroed amount is indistinguishable from a real zero downstream.
    """
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)

    text = str(raw).strip()
    if text.lower() in BLANK_MARKERS:
        return 0.0

    negative = text.startswith("(") and text.endswith(")")
    cleaned = text[1:-1].strip() if negative else text
    cleaned = _TRAILING_AFFIX.sub("", _LEADING_AFFIX.sub("", cleaned))
    # Stripping the affixes must have left a number behind. If it consumed the
    # whole cell, or letters survive in the middle, the cell was never an
    # amount — "twelve dollars and one cent" must not become 0.0.
    if not cleaned or _HAS_LETTER.search(cleaned) or not _HAS_DIGIT.search(cleaned):
        raise _unparseable(raw, "a monetary value")

    if "," in cleaned and "." in cleaned:
        # Whichever separator appears last is the decimal point.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif cleaned.count(".") > 1:
        # Multiple dots can only be thousands separators — "1.234.567" is the
        # Indonesian and German form, and TikTok Shop operates in both markets.
        # Unambiguous because no number has two decimal points.
        cleaned = cleaned.replace(".", "")
    elif "," in cleaned:
        # A single comma is a decimal separator when it splits 1-2 trailing
        # digits ("12,34"), otherwise a thousands separator ("1,234").
        parts = cleaned.split(",")
        cleaned = (cleaned.replace(",", ".") if len(parts) == 2 and len(parts[1]) <= 2
                   else cleaned.replace(",", ""))

    try:
        value = float(cleaned)
    except ValueError as exc:
        raise _unparseable(raw, "a monetary value") from exc
    return -value if negative else value


def parse_int(raw: Any) -> int:
    """Parse a unit count. Same blank-vs-garbage rule as `parse_money`.

    A quantity that silently reads as 0 removes a real sale from velocity,
    turnover, and every reorder quantity derived from them.
    """
    if raw is None:
        return 0
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    if text.lower() in BLANK_MARKERS:
        return 0
    try:
        return int(float(text.replace(",", "")))
    except ValueError as exc:
        raise _unparseable(raw, "a whole number") from exc


def parse_timestamp(raw: Any) -> str:
    """Normalise an exported timestamp to ISO 8601, or return '' if unparseable."""
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""

    # Unix epoch, sometimes exported for API-adjacent reports.
    if text.isdigit() and len(text) in (10, 13):
        seconds = int(text) / (1000 if len(text) == 13 else 1)
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(
            timespec="seconds")

    formats = (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y",
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d",
    )
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).replace(
                tzinfo=timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat(
            timespec="seconds")
    except ValueError:
        return ""


def _sniff(text: str) -> csv.Dialect | type[csv.Dialect]:
    sample = text[:8192]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def _map_columns(headers: Iterable[str], spec: dict[str, tuple[str, ...]],
                 *, required: set[str]) -> tuple[dict[str, str], list[str]]:
    """Match actual headers against known aliases.

    Returns (field -> actual header) plus warnings. An unmatched required field
    raises, naming the headers that were present — an importer that silently
    reads zero rows after a header change is worse than one that stops.
    """
    normalised = {str(h).strip().lower().lstrip("﻿"): str(h) for h in headers if h}
    mapping: dict[str, str] = {}
    warnings: list[str] = []

    for field_name, aliases in spec.items():
        for alias in aliases:
            if alias in normalised:
                mapping[field_name] = normalised[alias]
                break
        else:
            # Fall back to a containment match before giving up.
            for norm, original in normalised.items():
                if any(alias in norm for alias in aliases):
                    mapping[field_name] = original
                    warnings.append(
                        f"Matched {field_name!r} to column {original!r} by partial "
                        "name. Verify it is the right column — TikTok renames "
                        "headers between export versions."
                    )
                    break

    missing = required - set(mapping)
    if missing:
        raise ImportError_(
            f"Export is missing required column(s): {', '.join(sorted(missing))}.\n"
            f"Headers found: {', '.join(sorted(normalised.values()))}.\n"
            "TikTok changes column names between export versions and regions. Add "
            "the actual header to the alias table in connectors/tiktok/importers.py "
            "rather than renaming your export, so the next one works too."
        )
    return mapping, warnings


def _read_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - latin-1 accepts any byte sequence
        raise ImportError_(f"Could not decode {path} in any expected encoding.")

    dialect = _sniff(text)
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    rows = [r for r in reader]
    return rows, list(reader.fieldnames or [])


# ---------------------------------------------------------------------------
# Importers
# ---------------------------------------------------------------------------
def import_orders(path: str | Path) -> ImportResult:
    """Import a Seller Center order export.

    One CSV row is one order line, so an order with three products appears
    three times. Buyer identity is preserved when the export includes it —
    that is what makes repeat-purchase rate measurable without the PII API
    scope.
    """
    p = Path(path)
    rows, headers = _read_rows(p)
    mapping, warnings = _map_columns(
        headers, ORDER_COLUMNS, required={"order_id", "sku", "quantity"})

    out: list[dict[str, Any]] = []
    skipped = 0
    unparseable_dates = 0

    for row in rows:
        order_id = str(row.get(mapping["order_id"], "")).strip()
        if not order_id:
            skipped += 1
            continue

        status = str(row.get(mapping.get("status", ""), "")).strip()
        created = parse_timestamp(row.get(mapping.get("created_at", "")))
        if not created:
            unparseable_dates += 1

        out.append({
            "order_id": order_id,
            "created_at": created,
            "status": status,
            "is_sale": status.strip().lower() not in NON_SALE_STATUSES,
            "sku": str(row.get(mapping["sku"], "")).strip(),
            "product_name": str(row.get(mapping.get("product_name", ""), "")).strip(),
            "quantity": parse_int(row.get(mapping["quantity"])),
            "unit_price": parse_money(row.get(mapping.get("unit_price", ""))),
            "order_total": parse_money(row.get(mapping.get("order_total", ""))),
            "buyer_key": str(row.get(mapping.get("buyer", ""), "")).strip(),
            "currency": str(row.get(mapping.get("currency", ""), "")).strip() or "USD",
        })

    if unparseable_dates:
        warnings.append(
            f"{unparseable_dates} row(s) had an unparseable timestamp and were "
            "imported without one. They will not appear in date-ranged reports."
        )
    if "buyer" not in mapping:
        warnings.append(
            "This export has no buyer column, so repeat-purchase rate cannot be "
            "derived from it. In Seller Center choose the order export variant "
            "that includes buyer username."
        )
    non_sales = sum(1 for r in out if not r["is_sale"])
    if non_sales:
        warnings.append(
            f"{non_sales} row(s) are unpaid, cancelled, or on hold. They are "
            "imported but flagged `is_sale=False` — counting them would inflate "
            "revenue and repeat rate, and every LTV figure built on those."
        )

    return ImportResult(
        kind="orders", source_path=str(p),
        exported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        rows=out, skipped=skipped, warnings=warnings, column_map=mapping,
    )


def import_settlements(path: str | Path) -> ImportResult:
    """Import a Seller Center finance/settlement export.

    This is the highest-value import available: it carries the *realised* take
    rate, which is what determines whether a product is actually profitable.
    Everything computed from `[fees.tiktok]` estimates is provisional until
    this file exists.
    """
    p = Path(path)
    rows, headers = _read_rows(p)
    mapping, warnings = _map_columns(
        headers, SETTLEMENT_COLUMNS, required={"settlement_amount"})

    out: list[dict[str, Any]] = []
    for row in rows:
        revenue = parse_money(row.get(mapping.get("revenue", "")))
        fees = abs(parse_money(row.get(mapping.get("fees", ""))))
        affiliate = abs(parse_money(row.get(mapping.get("affiliate", ""))))
        settlement = parse_money(row.get(mapping["settlement_amount"]))
        if revenue == 0 and settlement == 0:
            continue
        out.append({
            "statement_id": str(row.get(mapping.get("statement_id", ""), "")).strip(),
            "order_id": str(row.get(mapping.get("order_id", ""), "")).strip(),
            "settlement_time": parse_timestamp(
                row.get(mapping.get("settlement_time", ""))),
            "revenue": revenue,
            "fees": fees,
            "affiliate_commission": affiliate,
            "settlement_amount": settlement,
            "currency": str(row.get(mapping.get("currency", ""), "")).strip() or "USD",
        })

    total_revenue = sum(r["revenue"] for r in out)
    total_deductions = sum(r["fees"] + r["affiliate_commission"] for r in out)
    if total_revenue > 0:
        take = total_deductions / total_revenue * 100
        warnings.append(
            f"Realised take rate across this export: {take:.1f}% of revenue. "
            "Reconcile [fees.tiktok] against it — every margin, price floor, and "
            "break-even in the system is computed from the policy estimate."
        )
    elif out:
        warnings.append(
            "No revenue column was matched, so the take rate could not be "
            "computed from this file. The settlement amounts imported fine."
        )

    return ImportResult(
        kind="settlements", source_path=str(p),
        exported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        rows=out, warnings=warnings, column_map=mapping,
    )


def import_products(path: str | Path) -> ImportResult:
    """Import a Seller Center product export."""
    p = Path(path)
    rows, headers = _read_rows(p)
    mapping, warnings = _map_columns(
        headers, PRODUCT_COLUMNS, required={"sku"})

    out: list[dict[str, Any]] = []
    for row in rows:
        sku = str(row.get(mapping["sku"], "")).strip()
        if not sku:
            continue
        out.append({
            "product_id": str(row.get(mapping.get("product_id", ""), "")).strip(),
            "sku": sku,
            "title": str(row.get(mapping.get("title", ""), "")).strip(),
            "status": str(row.get(mapping.get("status", ""), "")).strip(),
            "price": parse_money(row.get(mapping.get("price", ""))),
            "stock": parse_int(row.get(mapping.get("stock", ""))),
        })

    zero_stock = [r["sku"] for r in out if r["stock"] == 0]
    if zero_stock:
        warnings.append(
            f"{len(zero_stock)} SKU(s) at zero stock. TikTok suppresses "
            "out-of-stock products from the feed, and the placement takes longer "
            "to recover than the stockout itself lasts."
        )
    return ImportResult(
        kind="products", source_path=str(p),
        exported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        rows=out, warnings=warnings, column_map=mapping,
    )


def detect_export_kind(path: str | Path) -> str:
    """Guess which export a file is, from its headers.

    Returns orders | settlements | products | unknown. Guessing wrong is
    recoverable — importing with the wrong parser raises on missing required
    columns rather than producing plausible nonsense.
    """
    p = Path(path)
    try:
        _rows, headers = _read_rows(p)
    except Exception:
        return "unknown"
    normalised = {str(h).strip().lower() for h in headers if h}

    def hits(spec: dict[str, tuple[str, ...]]) -> int:
        return sum(1 for aliases in spec.values()
                   if any(a in normalised for a in aliases))

    scores = {
        "settlements": hits(SETTLEMENT_COLUMNS),
        "orders": hits(ORDER_COLUMNS),
        "products": hits(PRODUCT_COLUMNS),
    }
    # Settlements first: a settlement export also contains an order id, so
    # scoring orders first would misclassify it.
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] >= 2 else "unknown"


def daily_performance_from_orders(rows: list[dict[str, Any]]) -> dict[str, list[dict]]:
    """Build per-product daily series from imported order lines.

    This is what makes trend classification work without the Analytics API:
    units per product per day is exactly what `analyse_trend` needs. Page views
    are absent — an order export cannot know them — so conversion rate stays
    unmeasured rather than being invented.
    """
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        if not row.get("is_sale", True):
            continue
        sku = row.get("sku") or ""
        day = (row.get("created_at") or "")[:10]
        if not sku or not day:
            continue
        bucket = grouped.setdefault(sku, {})
        entry = bucket.setdefault(day, {
            "day": day, "title": row.get("product_name") or sku,
            "units": 0, "gmv": 0.0, "page_views": 0, "orders": 0,
        })
        entry["units"] += int(row.get("quantity", 0))
        entry["gmv"] += float(row.get("order_total", 0.0))
        entry["orders"] += 1

    return {sku: sorted(days.values(), key=lambda d: d["day"])
            for sku, days in grouped.items()}
