"""Analytics dashboard: the whole business on one screen.

Renders to a terminal and to a self-contained HTML file. Both are built from
the same `Dashboard` structure, so the two can never disagree — a dashboard
that says one thing in the terminal and another in the browser is worse than
having only one of them.

The rule that shapes this module: **a tile with no data says so.** Not zero,
not a dash, not a blank. `Tile.value is None` renders as the reason it is
unknown, because a revenue tile showing $0 and a revenue tile showing "no
Shopify connection" lead to opposite decisions, and the difference must survive
being glanced at.

The provenance banner is not decoration either. When any input is seed or
imported rather than live, the banner says so at the top and cannot be
suppressed by a caller — `render_terminal` and `render_html` both read it from
the same field. Deleting or softening it is a change to what the system claims,
not a formatting change.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

# Provenance ranked worst-first. The banner reports the weakest input, because
# a dashboard is only as real as its least real number.
PROVENANCE_RANK = {"seed": 0, "unknown": 1, "import": 2, "cached": 3, "live": 4}

PROVENANCE_BANNER = {
    "seed": ("NOT REAL BUSINESS NUMBERS — at least one figure below comes from "
             "seed data. Nothing here describes an actual business."),
    "unknown": ("PROVENANCE UNKNOWN — at least one input did not declare where "
                "it came from. Treat every figure as unverified."),
    "import": ("IMPORTED DATA — figures come from Seller Center exports rather "
               "than a live API. Real, but as of the export date, not now."),
    "cached": ("CACHED DATA — figures were read from a cache, not fetched. "
               "Accurate as of the cache, which may predate today."),
    "live": "",
}


@dataclass
class Tile:
    """One number, or an explicit statement that there isn't one."""

    label: str
    value: float | int | str | None
    unit: str = ""
    delta_pct: float | None = None
    reason_missing: str = ""
    good_direction: str = "up"       # up | down | neutral

    @property
    def known(self) -> bool:
        return self.value is not None

    def display(self) -> str:
        if not self.known:
            return "—"
        if isinstance(self.value, str):
            return self.value
        if self.unit == "$":
            return f"${self.value:,.2f}"
        if self.unit == "%":
            return f"{self.value:,.2f}%"
        return f"{self.value:,}" if isinstance(self.value, int) else f"{self.value:,.2f}"

    def trend_marker(self) -> str:
        if self.delta_pct is None:
            return ""
        arrow = "▲" if self.delta_pct >= 0 else "▼"
        return f"{arrow} {abs(self.delta_pct):.1f}%"


@dataclass
class Panel:
    title: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    empty_message: str = "No data."


@dataclass
class Dashboard:
    generated_at: str
    period: str
    provenance: str
    tiles: list[Tile] = field(default_factory=list)
    panels: list[Panel] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tasks: list[str] = field(default_factory=list)

    @property
    def banner(self) -> str:
        return PROVENANCE_BANNER.get(self.provenance, PROVENANCE_BANNER["unknown"])

    def tile(self, label: str) -> Tile | None:
        return next((t for t in self.tiles if t.label == label), None)


def _worst_provenance(sources: list[str]) -> str:
    """The weakest input decides the banner."""
    if not sources:
        return "unknown"
    return min((s if s in PROVENANCE_RANK else "unknown" for s in sources),
               key=lambda s: PROVENANCE_RANK[s])


def _pct_change(current: float | None, previous: float | None) -> float | None:
    """Percent change, or None when it would be meaningless.

    A change from zero is not "infinite growth", it is a first sale. Returning
    None keeps a first order from rendering as a spectacular trend.
    """
    if current is None or previous is None or previous == 0:
        return None
    return round((current - previous) / abs(previous) * 100, 1)


# ---------------------------------------------------------------------------
def build_dashboard(*, storefront_rows: list[dict[str, Any]],
                    previous_rows: list[dict[str, Any]] | None = None,
                    video_rows: list[dict[str, Any]] | None = None,
                    product_rows: list[dict[str, Any]] | None = None,
                    pipeline: dict[str, Any] | None = None,
                    experiments: list[dict[str, Any]] | None = None,
                    pending_approvals: list[dict[str, Any]] | None = None,
                    period: str = "", today: date | None = None) -> Dashboard:
    """Assemble the dashboard from whatever is actually available."""
    today = today or datetime.now(timezone.utc).date()
    video_rows = video_rows or []
    product_rows = product_rows or []
    previous_rows = previous_rows or []

    sources = [str(r.get("data_source") or "unknown") for r in storefront_rows]
    sources += [str(r.get("data_source") or "unknown") for r in product_rows]
    provenance = _worst_provenance(sources)

    revenue = sum(float(r.get("revenue") or 0.0) for r in storefront_rows)
    refunds = sum(float(r.get("refunds") or 0.0) for r in storefront_rows)
    orders = sum(int(r.get("orders") or 0) for r in storefront_rows)
    prev_revenue = sum(float(r.get("revenue") or 0.0) for r in previous_rows)
    prev_orders = sum(int(r.get("orders") or 0) for r in previous_rows)

    session_values = [r.get("sessions") for r in storefront_rows
                      if r.get("sessions") is not None]
    sessions = sum(int(s) for s in session_values) if session_values else None

    # Profit needs cost of goods, which lives on the product metrics rather than
    # the storefront rows. Absent it, profit is unknown — not zero, and
    # certainly not equal to revenue.
    profit = None
    profit_reason = ""
    if product_rows:
        profit = round(sum(float(r.get("net_profit") or 0.0) for r in product_rows), 2)
    else:
        profit_reason = ("No per-product cost data for this period, so profit "
                         "cannot be computed. Revenue is not profit and the "
                         "difference is the entire business.")

    warnings: list[str] = []
    tiles = [
        Tile("Revenue", round(revenue, 2), "$",
             _pct_change(revenue, prev_revenue if previous_rows else None)),
        Tile("Estimated profit", profit, "$", reason_missing=profit_reason),
        Tile("Orders", orders, "",
             _pct_change(orders, prev_orders if previous_rows else None)),
        Tile("Refunds", round(refunds, 2), "$", good_direction="down"),
    ]

    if sessions:
        tiles.append(Tile("Conversion rate", round(orders / sessions * 100, 2), "%"))
        tiles.append(Tile("Revenue per visitor", round(revenue / sessions, 2), "$"))
    else:
        reason = ("Shopify's Admin API does not expose session counts, so "
                  "conversion rate cannot be computed. It is reported as unknown "
                  "rather than back-computed from orders.")
        tiles.append(Tile("Conversion rate", None, "%", reason_missing=reason))
        tiles.append(Tile("Revenue per visitor", None, "$", reason_missing=reason))

    tiles.append(Tile("Average order value",
                      round(revenue / orders, 2) if orders else None, "$",
                      reason_missing="" if orders else "No orders in this period."))

    total_views = sum(int(v.get("views") or 0) for v in video_rows)
    tiles.append(Tile("Video views", total_views if video_rows else None, "",
                      reason_missing="" if video_rows else
                      ("No video metrics recorded. TikTok publishes no organic "
                       "analytics API — enter them with `video-metrics`.")))
    tiles.append(Tile("Videos published", len(video_rows) if video_rows else None,
                      "", reason_missing="" if video_rows else
                      "No published videos logged for this period."))

    # --- panels ---------------------------------------------------------
    panels: list[Panel] = []

    by_channel: dict[str, dict[str, float]] = {}
    for row in storefront_rows:
        channel = str(row.get("channel") or "unattributed")
        bucket = by_channel.setdefault(channel, {"orders": 0.0, "revenue": 0.0})
        bucket["orders"] += float(row.get("orders") or 0)
        bucket["revenue"] += float(row.get("revenue") or 0.0)
    panels.append(Panel(
        title="Traffic sources",
        columns=["Channel", "Orders", "Revenue", "Share"],
        rows=[{
            "Channel": channel,
            "Orders": int(data["orders"]),
            "Revenue": f"${data['revenue']:,.2f}",
            "Share": (f"{data['revenue'] / revenue * 100:.1f}%"
                      if revenue else "—"),
        } for channel, data in sorted(by_channel.items(),
                                      key=lambda kv: -kv[1]["revenue"])],
        empty_message="No storefront data for this period."))

    unattributed = by_channel.get("unattributed", {}).get("revenue", 0.0)
    if revenue and unattributed / revenue > 0.3:
        warnings.append(
            f"{unattributed / revenue * 100:.0f}% of revenue has no resolvable "
            "traffic source. Channel shares below are computed on the "
            "attributable remainder and understate every channel — including "
            "the one this business runs on.")

    top_products = sorted(product_rows,
                          key=lambda r: float(r.get("net_profit") or 0.0),
                          reverse=True)[:5]
    panels.append(Panel(
        title="Top products by profit",
        columns=["SKU", "Units", "Revenue", "Net profit"],
        rows=[{
            "SKU": str(r.get("sku", "")),
            "Units": int(r.get("units") or 0),
            "Revenue": f"${float(r.get('revenue') or 0):,.2f}",
            "Net profit": f"${float(r.get('net_profit') or 0):,.2f}",
        } for r in top_products],
        empty_message="No per-product metrics recorded for this period."))

    rateable = [v for v in video_rows if int(v.get("views") or 0) >= 1000]
    top_videos = sorted(rateable, key=lambda v: int(v.get("views") or 0),
                        reverse=True)[:5]
    panels.append(Panel(
        title="Top videos",
        columns=["Package", "Angle", "Views", "Click rate"],
        rows=[{
            "Package": str(v.get("package_id", "")),
            "Angle": str(v.get("angle", "")),
            "Views": f"{int(v.get('views') or 0):,}",
            "Click rate": (f"{int(v['link_clicks']) / int(v['views']) * 100:.2f}%"
                           if v.get("link_clicks") is not None and v.get("views")
                           else "—"),
        } for v in top_videos],
        empty_message=("No videos above the 1,000-view floor. Below it, rates "
                       "are not computed — a handful of clicks on a video "
                       "nobody saw is not a click rate.")))

    if pipeline:
        panels.append(Panel(
            title="Product pipeline",
            columns=["Opportunity", "Stage", "Score", "Coverage", "Promotable"],
            rows=[{
                "Opportunity": row["title"],
                "Stage": row["stage"],
                "Score": f"{row['effective_score']:.1f}",
                "Coverage": f"{row['coverage_pct']:.0f}%",
                "Promotable": "yes" if row["promotable"] else "no",
            } for row in (pipeline.get("ranked") or [])[:8]],
            empty_message="No active opportunities in the pipeline."))
        warnings.extend(pipeline.get("warnings") or [])

    if experiments:
        panels.append(Panel(
            title="Experiments",
            columns=["Experiment", "SKU", "Status", "Verdict"],
            rows=[{
                "Experiment": str(e.get("experiment_id", "")),
                "SKU": str(e.get("sku", "")),
                "Status": str(e.get("status", "")),
                "Verdict": str(e.get("outcome") or "running"),
            } for e in experiments[:8]],
            empty_message="No experiments registered."))

    tasks: list[str] = []
    for approval in (pending_approvals or []):
        tasks.append(f"Approve or reject: {approval.get('action', 'unknown action')} "
                     f"({approval.get('sku', '')}) — {approval.get('action_id', '')}")
    if not video_rows:
        tasks.append("Log video performance with `video-metrics` — without it "
                     "no creative question can be answered.")
    if sessions is None:
        tasks.append("Session counts are unavailable from the Admin API; "
                     "conversion rate stays unknown until another source supplies them.")

    return Dashboard(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        period=period or today.isoformat(),
        provenance=provenance,
        tiles=tiles, panels=panels, warnings=warnings, tasks=tasks)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_terminal(dashboard: Dashboard, *, width: int = 78) -> str:
    lines: list[str] = []
    rule = "=" * width

    lines.append(rule)
    lines.append(f"BUSINESS DASHBOARD — {dashboard.period}".center(width))
    lines.append(rule)
    if dashboard.banner:
        lines.append("")
        for chunk in _wrap(dashboard.banner, width - 4):
            lines.append(f"  {chunk}")
        lines.append("")

    lines.append("")
    for tile in dashboard.tiles:
        marker = tile.trend_marker()
        value = tile.display()
        lines.append(f"  {tile.label:<24} {value:>18}  {marker}")
        if not tile.known and tile.reason_missing:
            for chunk in _wrap(tile.reason_missing, width - 8):
                lines.append(f"        {chunk}")
    lines.append("")

    for panel in dashboard.panels:
        lines.append("-" * width)
        lines.append(f"  {panel.title}")
        lines.append("-" * width)
        if not panel.rows:
            for chunk in _wrap(panel.empty_message, width - 4):
                lines.append(f"  {chunk}")
            lines.append("")
            continue
        widths = {c: max(len(c), max((len(str(r.get(c, ""))) for r in panel.rows),
                                     default=0)) for c in panel.columns}
        header = "  " + "  ".join(c.ljust(widths[c]) for c in panel.columns)
        lines.append(header)
        for row in panel.rows:
            lines.append("  " + "  ".join(
                str(row.get(c, "")).ljust(widths[c]) for c in panel.columns))
        lines.append("")

    if dashboard.warnings:
        lines.append("-" * width)
        lines.append("  WARNINGS")
        lines.append("-" * width)
        for warning in dashboard.warnings:
            for i, chunk in enumerate(_wrap(warning, width - 6)):
                lines.append(("  - " if i == 0 else "    ") + chunk)
        lines.append("")

    if dashboard.tasks:
        lines.append("-" * width)
        lines.append("  TASKS")
        lines.append("-" * width)
        for task in dashboard.tasks:
            for i, chunk in enumerate(_wrap(task, width - 6)):
                lines.append(("  [ ] " if i == 0 else "      ") + chunk)
        lines.append("")

    lines.append(rule)
    lines.append(f"Generated {dashboard.generated_at}".rjust(width))
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


def render_html(dashboard: Dashboard) -> str:
    """A self-contained HTML page. No external assets, by design.

    The dashboard has to open from a file on a machine with no network and no
    build step — the same constraint as the rest of this system running on the
    standard library alone.
    """
    def esc(value: Any) -> str:
        return html.escape(str(value), quote=True)

    tiles_html = []
    for tile in dashboard.tiles:
        if tile.known:
            body = f'<div class="v">{esc(tile.display())}</div>'
            if tile.delta_pct is not None:
                direction = "up" if tile.delta_pct >= 0 else "down"
                body += (f'<div class="d {direction}">'
                         f'{esc(tile.trend_marker())}</div>')
        else:
            body = ('<div class="v unknown">unknown</div>'
                    f'<div class="why">{esc(tile.reason_missing)}</div>')
        tiles_html.append(
            f'<div class="tile"><div class="l">{esc(tile.label)}</div>{body}</div>')

    panels_html = []
    for panel in dashboard.panels:
        if panel.rows:
            head = "".join(f"<th>{esc(c)}</th>" for c in panel.columns)
            body = "".join(
                "<tr>" + "".join(f"<td>{esc(row.get(c, ''))}</td>"
                                 for c in panel.columns) + "</tr>"
                for row in panel.rows)
            table = f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        else:
            table = f'<p class="empty">{esc(panel.empty_message)}</p>'
        panels_html.append(
            f'<section><h2>{esc(panel.title)}</h2>{table}</section>')

    banner = (f'<div class="banner">{esc(dashboard.banner)}</div>'
              if dashboard.banner else "")
    warnings = ("".join(f"<li>{esc(w)}</li>" for w in dashboard.warnings)
                if dashboard.warnings else "")
    warnings_html = (f'<section><h2>Warnings</h2><ul class="warn">{warnings}</ul>'
                     "</section>" if warnings else "")
    tasks = "".join(f"<li>{esc(t)}</li>" for t in dashboard.tasks)
    tasks_html = (f"<section><h2>Tasks</h2><ul>{tasks}</ul></section>"
                  if tasks else "")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Business dashboard — {esc(dashboard.period)}</title>
<style>
:root {{ color-scheme: light dark; --fg:#111; --bg:#fff; --muted:#666;
  --line:#ddd; --warn:#8a5300; --warnbg:#fff6e5; --up:#0a7d34; --down:#a4262c; }}
@media (prefers-color-scheme: dark) {{ :root {{ --fg:#e8e8e8; --bg:#141414;
  --muted:#9a9a9a; --line:#333; --warnbg:#332a14; --warn:#f0b429; }} }}
* {{ box-sizing: border-box; }}
body {{ margin:0; padding:24px; background:var(--bg); color:var(--fg);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif; }}
h1 {{ font-size:20px; margin:0 0 4px; }}
h2 {{ font-size:15px; margin:28px 0 10px; text-transform:uppercase;
  letter-spacing:.06em; color:var(--muted); }}
.period {{ color:var(--muted); margin-bottom:16px; }}
.banner {{ background:var(--warnbg); color:var(--warn); border:1px solid var(--warn);
  border-radius:6px; padding:12px 14px; margin-bottom:20px; font-weight:600; }}
.tiles {{ display:grid; gap:12px;
  grid-template-columns:repeat(auto-fill,minmax(190px,1fr)); }}
.tile {{ border:1px solid var(--line); border-radius:8px; padding:14px; }}
.l {{ color:var(--muted); font-size:12px; text-transform:uppercase;
  letter-spacing:.05em; }}
.v {{ font-size:26px; font-weight:650; margin-top:6px;
  font-variant-numeric:tabular-nums; }}
.v.unknown {{ font-size:18px; color:var(--muted); font-weight:500; }}
.why {{ font-size:12px; color:var(--muted); margin-top:6px; }}
.d {{ font-size:12px; margin-top:4px; }} .d.up {{ color:var(--up); }}
.d.down {{ color:var(--down); }}
table {{ border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }}
th,td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); }}
th {{ font-size:12px; text-transform:uppercase; letter-spacing:.05em;
  color:var(--muted); }}
.empty {{ color:var(--muted); font-size:14px; }}
ul {{ padding-left:20px; }} .warn li {{ color:var(--warn); }}
footer {{ margin-top:32px; color:var(--muted); font-size:12px; }}
.scroll {{ overflow-x:auto; }}
</style></head><body>
<h1>Business dashboard</h1>
<div class="period">{esc(dashboard.period)} · provenance: {esc(dashboard.provenance)}</div>
{banner}
<div class="tiles">{''.join(tiles_html)}</div>
<div class="scroll">{''.join(panels_html)}</div>
{warnings_html}
{tasks_html}
<footer>Generated {esc(dashboard.generated_at)}</footer>
</body></html>"""
