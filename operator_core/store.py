"""SQLite persistence: the decision journal, metrics history, and supplier
scorecard history.

The journal is the mechanism for "improve decisions using historical data".
Every recommendation is written down *with the inputs that produced it and the
outcome it predicted*, so that when the outcome lands we can measure whether
the reasoning was right — not merely whether the result was good. A decision
that was correct on the evidence and unlucky is a different lesson from one
that was wrong on the evidence and lucky.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator

from .models import ActionRecord, now_iso, today_iso

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "operator.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    action_id           TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    domain              TEXT NOT NULL,
    sku                 TEXT NOT NULL,
    action              TEXT NOT NULL,
    rationale           TEXT NOT NULL,
    decision            TEXT NOT NULL,
    inputs_json         TEXT NOT NULL,
    expected_outcome    TEXT NOT NULL,
    requires_approval   INTEGER NOT NULL DEFAULT 0,
    approved_by         TEXT DEFAULT '',
    executed            INTEGER NOT NULL DEFAULT 0,
    outcome_json        TEXT DEFAULT '',
    outcome_recorded_at TEXT DEFAULT '',
    dedupe_key          TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_dedupe
    ON decisions(dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_decisions_sku ON decisions(sku);
CREATE INDEX IF NOT EXISTS idx_decisions_domain ON decisions(domain);
CREATE INDEX IF NOT EXISTS idx_decisions_created ON decisions(created_at);

CREATE TABLE IF NOT EXISTS daily_metrics (
    metric_date   TEXT NOT NULL,
    marketplace   TEXT NOT NULL,
    sku           TEXT NOT NULL,
    units         INTEGER NOT NULL DEFAULT 0,
    revenue       REAL NOT NULL DEFAULT 0,
    cogs          REAL NOT NULL DEFAULT 0,
    fees          REAL NOT NULL DEFAULT 0,
    ad_spend      REAL NOT NULL DEFAULT 0,
    refunds       REAL NOT NULL DEFAULT 0,
    net_profit    REAL NOT NULL DEFAULT 0,
    data_source   TEXT NOT NULL DEFAULT 'unknown',
    PRIMARY KEY (metric_date, marketplace, sku)
);

CREATE TABLE IF NOT EXISTS supplier_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at       TEXT NOT NULL,
    supplier_id       TEXT NOT NULL,
    supplier_name     TEXT NOT NULL,
    score             REAL NOT NULL,
    on_time_rate_pct  REAL,
    defect_rate_pct   REAL,
    notes             TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS observed_signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at  TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    sku          TEXT NOT NULL,
    source       TEXT NOT NULL,
    direction    TEXT NOT NULL,
    strength     REAL NOT NULL,
    detail       TEXT DEFAULT '',
    observer     TEXT DEFAULT '',
    origin       TEXT NOT NULL DEFAULT 'manual'
);
CREATE INDEX IF NOT EXISTS idx_signals_sku ON observed_signals(sku);
CREATE INDEX IF NOT EXISTS idx_signals_observed ON observed_signals(observed_at);

CREATE TABLE IF NOT EXISTS tiktok_orders (
    order_id      TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    status        TEXT NOT NULL,
    buyer_key     TEXT DEFAULT '',
    buyer_total   REAL NOT NULL DEFAULT 0,
    currency      TEXT DEFAULT 'USD',
    sku           TEXT DEFAULT '',
    product_id    TEXT DEFAULT '',
    units         INTEGER NOT NULL DEFAULT 0,
    data_source   TEXT NOT NULL DEFAULT 'unknown'
);
CREATE INDEX IF NOT EXISTS idx_ttorders_buyer ON tiktok_orders(buyer_key);
CREATE INDEX IF NOT EXISTS idx_ttorders_sku ON tiktok_orders(sku);

CREATE TABLE IF NOT EXISTS price_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at   TEXT NOT NULL,
    sku          TEXT NOT NULL,
    marketplace  TEXT NOT NULL,
    old_price    REAL NOT NULL,
    new_price    REAL NOT NULL,
    reason       TEXT NOT NULL,
    applied      INTEGER NOT NULL DEFAULT 0
);
"""


class Store:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Additive migrations for databases created by an earlier version."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(decisions)")}
        if "dedupe_key" not in cols:
            conn.execute("ALTER TABLE decisions ADD COLUMN dedupe_key TEXT")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_dedupe "
                "ON decisions(dedupe_key) WHERE dedupe_key IS NOT NULL"
            )

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- decision journal -------------------------------------------------
    def record_decision(
        self,
        *,
        domain: str,
        sku: str,
        action: str,
        rationale: str,
        decision: str,
        inputs: dict[str, Any] | None = None,
        expected_outcome: str = "",
        requires_approval: bool = False,
        executed: bool = False,
        dedupe_key: str | None = None,
    ) -> str:
        """Journal a decision.

        `dedupe_key` makes the daily run idempotent: re-running the same day
        returns the existing action_id instead of creating a duplicate. Without
        it, a job that runs twice inflates the journal and corrupts the hit-rate
        statistics that the learning loop depends on.
        """
        if dedupe_key:
            with self._conn() as c:
                row = c.execute(
                    "SELECT action_id FROM decisions WHERE dedupe_key=?", (dedupe_key,)
                ).fetchone()
            if row:
                return row["action_id"]

        rec = ActionRecord(
            action_id=str(uuid.uuid4())[:12],
            created_at=now_iso(),
            domain=domain,
            sku=sku,
            action=action,
            rationale=rationale,
            decision=decision,
            inputs_json=json.dumps(inputs or {}, default=str),
            expected_outcome=expected_outcome,
            requires_approval=requires_approval,
            executed=executed,
        )
        with self._conn() as c:
            c.execute(
                """INSERT INTO decisions
                   (action_id, created_at, domain, sku, action, rationale, decision,
                    inputs_json, expected_outcome, requires_approval, approved_by,
                    executed, outcome_json, outcome_recorded_at, dedupe_key)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rec.action_id, rec.created_at, rec.domain, rec.sku, rec.action,
                    rec.rationale, rec.decision, rec.inputs_json, rec.expected_outcome,
                    int(rec.requires_approval), rec.approved_by, int(rec.executed),
                    rec.outcome_json, rec.outcome_recorded_at, dedupe_key,
                ),
            )
        return rec.action_id

    def record_outcome(self, action_id: str, outcome: dict[str, Any]) -> bool:
        """Close the loop on a past decision."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE decisions SET outcome_json=?, outcome_recorded_at=? WHERE action_id=?",
                (json.dumps(outcome, default=str), now_iso(), action_id),
            )
            return cur.rowcount > 0

    def approve(self, action_id: str, approved_by: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE decisions SET approved_by=?, decision='APPROVE' WHERE action_id=?",
                (approved_by, action_id),
            )
            return cur.rowcount > 0

    def pending_approvals(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM decisions WHERE requires_approval=1 AND approved_by='' "
                "ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def decisions_for_sku(self, sku: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM decisions WHERE sku=? ORDER BY created_at DESC LIMIT ?",
                (sku, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_decisions(self, limit: int = 25) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # -- metrics ----------------------------------------------------------
    def upsert_daily_metric(self, **kw: Any) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO daily_metrics
                   (metric_date, marketplace, sku, units, revenue, cogs, fees,
                    ad_spend, refunds, net_profit, data_source)
                   VALUES (:metric_date,:marketplace,:sku,:units,:revenue,:cogs,:fees,
                           :ad_spend,:refunds,:net_profit,:data_source)
                   ON CONFLICT(metric_date, marketplace, sku) DO UPDATE SET
                     units=excluded.units, revenue=excluded.revenue, cogs=excluded.cogs,
                     fees=excluded.fees, ad_spend=excluded.ad_spend,
                     refunds=excluded.refunds, net_profit=excluded.net_profit,
                     data_source=excluded.data_source""",
                {
                    "metric_date": kw.get("metric_date", today_iso()),
                    "marketplace": kw["marketplace"],
                    "sku": kw["sku"],
                    "units": kw.get("units", 0),
                    "revenue": kw.get("revenue", 0.0),
                    "cogs": kw.get("cogs", 0.0),
                    "fees": kw.get("fees", 0.0),
                    "ad_spend": kw.get("ad_spend", 0.0),
                    "refunds": kw.get("refunds", 0.0),
                    "net_profit": kw.get("net_profit", 0.0),
                    "data_source": kw.get("data_source", "unknown"),
                },
            )

    def metrics_for_date(self, metric_date: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM daily_metrics WHERE metric_date=? ORDER BY net_profit DESC",
                (metric_date,),
            ).fetchall()
        return [dict(r) for r in rows]

    def metrics_range(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM daily_metrics WHERE metric_date BETWEEN ? AND ? "
                "ORDER BY metric_date, net_profit DESC",
                (start_date, end_date),
            ).fetchall()
        return [dict(r) for r in rows]

    def has_metrics(self) -> bool:
        with self._conn() as c:
            row = c.execute("SELECT COUNT(*) AS n FROM daily_metrics").fetchone()
        return bool(row["n"])

    # -- price history ----------------------------------------------------
    def record_price_change(self, *, sku: str, marketplace: str, old_price: float,
                            new_price: float, reason: str, applied: bool) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO price_history
                   (changed_at, sku, marketplace, old_price, new_price, reason, applied)
                   VALUES (?,?,?,?,?,?,?)""",
                (now_iso(), sku, marketplace, old_price, new_price, reason, int(applied)),
            )

    def last_price_change(self, sku: str, marketplace: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM price_history WHERE sku=? AND marketplace=? AND applied=1 "
                "ORDER BY changed_at DESC LIMIT 1",
                (sku, marketplace),
            ).fetchone()
        return dict(row) if row else None

    # -- observed signals -------------------------------------------------
    def record_signal(self, *, sku: str, source: str, direction: str,
                      strength: float, observed_at: str, detail: str = "",
                      observer: str = "", origin: str = "manual") -> int:
        """Persist an observed signal.

        `origin` distinguishes a reading pulled from an API from one a human
        typed in after looking at the app. Both are legitimate; conflating them
        is not, because only one of them can be re-derived if it is ever
        questioned.
        """
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO observed_signals
                   (recorded_at, observed_at, sku, source, direction, strength,
                    detail, observer, origin)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (now_iso(), observed_at, sku, source, direction, strength,
                 detail, observer, origin),
            )
            return int(cur.lastrowid or 0)

    def signals_for_sku(self, sku: str, *, since: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM observed_signals WHERE sku=?"
        params: list[Any] = [sku]
        if since:
            query += " AND observed_at >= ?"
            params.append(since)
        query += " ORDER BY observed_at DESC"
        with self._conn() as c:
            rows = c.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def all_signals(self, *, since: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM observed_signals"
        params: list[Any] = []
        if since:
            query += " WHERE observed_at >= ?"
            params.append(since)
        query += " ORDER BY observed_at DESC"
        with self._conn() as c:
            rows = c.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # -- TikTok orders ----------------------------------------------------
    def upsert_tiktok_order(self, **kw: Any) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO tiktok_orders
                   (order_id, created_at, status, buyer_key, buyer_total,
                    currency, sku, product_id, units, data_source)
                   VALUES (:order_id,:created_at,:status,:buyer_key,:buyer_total,
                           :currency,:sku,:product_id,:units,:data_source)
                   ON CONFLICT(order_id) DO UPDATE SET
                     status=excluded.status, buyer_total=excluded.buyer_total,
                     units=excluded.units, data_source=excluded.data_source""",
                {
                    "order_id": kw["order_id"],
                    "created_at": kw.get("created_at", ""),
                    "status": kw.get("status", ""),
                    "buyer_key": kw.get("buyer_key", ""),
                    "buyer_total": float(kw.get("buyer_total", 0.0)),
                    "currency": kw.get("currency", "USD"),
                    "sku": kw.get("sku", ""),
                    "product_id": kw.get("product_id", ""),
                    "units": int(kw.get("units", 0)),
                    "data_source": kw.get("data_source", "unknown"),
                },
            )

    def repeat_purchase_stats(self, *, sku: str | None = None,
                              exclude_statuses: tuple[str, ...] = (
                                  "UNPAID", "CANCELLED", "ON_HOLD")) -> dict[str, Any]:
        """Derive repeat-purchase rate from stored orders.

        Only counts orders that actually settled: an unpaid or cancelled order
        is not a purchase, and including it inflates both the customer count and
        the repeat rate — which then inflates LTV, which then justifies paying
        more to acquire customers than they are worth.

        Returns `buyers_identified=0` when no buyer key is available rather than
        guessing a rate. TikTok only returns buyer identifiers under a PII
        scope, so an operator running without it genuinely cannot measure this.
        """
        placeholders = ",".join("?" for _ in exclude_statuses)
        query = (
            f"SELECT buyer_key, COUNT(*) AS orders FROM tiktok_orders "
            f"WHERE buyer_key != '' AND UPPER(status) NOT IN ({placeholders})"
        )
        params: list[Any] = list(exclude_statuses)
        if sku:
            query += " AND sku = ?"
            params.append(sku)
        query += " GROUP BY buyer_key"

        with self._conn() as c:
            rows = c.execute(query, params).fetchall()
            total_row = c.execute(
                "SELECT COUNT(*) AS n FROM tiktok_orders").fetchone()

        buyers = [dict(r) for r in rows]
        if not buyers:
            return {
                "buyers_identified": 0,
                "repeat_rate_pct": None,
                "orders_per_buyer": None,
                "total_orders_seen": int(total_row["n"]) if total_row else 0,
                "note": (
                    "No buyer identifiers available, so repeat purchase cannot be "
                    "measured. TikTok returns buyer identity only under a "
                    "restricted PII scope. Until that is granted, LTV must assume "
                    "a 0% repeat rate — an assumed rate would inflate every "
                    "acquisition budget downstream."
                ),
            }

        repeat_buyers = sum(1 for b in buyers if b["orders"] > 1)
        total_orders = sum(b["orders"] for b in buyers)
        return {
            "buyers_identified": len(buyers),
            "repeat_buyers": repeat_buyers,
            "repeat_rate_pct": round(repeat_buyers / len(buyers) * 100, 1),
            "orders_per_buyer": round(total_orders / len(buyers), 2),
            "total_orders_seen": int(total_row["n"]) if total_row else 0,
            "note": (
                f"Derived from {len(buyers)} identified buyers. Small samples "
                "swing hard — treat under 100 buyers as directional."
                if len(buyers) < 100 else
                f"Derived from {len(buyers)} identified buyers."
            ),
        }

    # -- supplier history -------------------------------------------------
    def record_supplier_score(self, *, supplier_id: str, supplier_name: str, score: float,
                              on_time_rate_pct: float | None = None,
                              defect_rate_pct: float | None = None, notes: str = "") -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO supplier_history
                   (recorded_at, supplier_id, supplier_name, score, on_time_rate_pct,
                    defect_rate_pct, notes)
                   VALUES (?,?,?,?,?,?,?)""",
                (now_iso(), supplier_id, supplier_name, score, on_time_rate_pct,
                 defect_rate_pct, notes),
            )

    def supplier_trend(self, supplier_id: str) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM supplier_history WHERE supplier_id=? ORDER BY recorded_at",
                (supplier_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def learning_summary(store: Store) -> dict[str, Any]:
    """What the journal has actually taught us so far.

    Only decisions with recorded outcomes count. An unresolved decision is not
    evidence, and treating it as such is how a system convinces itself it is
    performing well.
    """
    all_decisions = store.recent_decisions(limit=1000)
    resolved = [d for d in all_decisions if d["outcome_json"]]

    by_domain: dict[str, dict[str, int]] = {}
    for d in resolved:
        try:
            outcome = json.loads(d["outcome_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        bucket = by_domain.setdefault(d["domain"], {"resolved": 0, "as_expected": 0, "worse": 0})
        bucket["resolved"] += 1
        if outcome.get("met_expectation") is True:
            bucket["as_expected"] += 1
        elif outcome.get("met_expectation") is False:
            bucket["worse"] += 1

    for bucket in by_domain.values():
        bucket["hit_rate_pct"] = (
            round(bucket["as_expected"] / bucket["resolved"] * 100, 1)
            if bucket["resolved"] else 0.0
        )

    return {
        "total_decisions": len(all_decisions),
        "resolved_decisions": len(resolved),
        "unresolved_decisions": len(all_decisions) - len(resolved),
        "by_domain": by_domain,
        "note": (
            "Hit rate is only meaningful once resolved_decisions is comfortably into "
            "double figures per domain. Below that it is anecdote, not evidence."
        ),
    }
