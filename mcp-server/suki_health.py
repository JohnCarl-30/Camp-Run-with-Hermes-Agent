"""Suki Autopilot — shared, read-only branch-health scoring.

The ONE copy of the health-score SQL. Imported by both the MCP server
(`suki_autopilot.py`) and the desktop plugin backend (`plugin_api.py`), so the
pane and the agent always show the same numbers. Pure stdlib: no `mcp` import.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

DB_PATH = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "store.db"))

RATING_WINDOW_DAYS = 35
OPS_WINDOW_DAYS = 30


def connect_ro(db_path: str = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def rows(con: sqlite3.Connection, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def sandbox_now(con: sqlite3.Connection) -> str:
    """The sandbox's 'today' (2026-09-30 21:00:00) — never the real clock."""
    return con.execute("SELECT value FROM sandbox_info WHERE key='sandbox_now'").fetchone()[0]


# One row per branch with the four raw signals the score is built from.
BRANCH_METRICS_SQL = """
SELECT b.id, b.code, b.name, b.has_delivery,
  (SELECT round(avg(r.rating), 2) FROM reviews r
     WHERE r.branch_id = b.id AND r.created_at >= datetime(:now, :rwin))            AS rating_recent,
  (SELECT round(avg(r.rating), 2) FROM reviews r
     WHERE r.branch_id = b.id AND r.created_at <  datetime(:now, :rwin))            AS rating_before,
  (SELECT round(100.0 * sum(d.delivered_at > d.promised_by) / max(count(*), 1), 1)
     FROM deliveries d JOIN orders o ON o.id = d.order_id
     WHERE d.branch_id = b.id AND o.created_at >= datetime(:now, :owin))            AS late_pct,
  (SELECT sum(s.status IN ('no_show', 'called_in_sick')) FROM shifts s
     WHERE s.branch_id = b.id AND s.shift_date >= date(:now, :owin) AND s.shift_date <= date(:now)) AS missed_shifts,
  (SELECT round(100.0 * sum(s.status IN ('no_show', 'called_in_sick')) / max(count(*), 1), 1) FROM shifts s
     WHERE s.branch_id = b.id AND s.shift_date >= date(:now, :owin) AND s.shift_date <= date(:now)) AS missed_shift_pct,
  (SELECT count(*) FROM support_tickets t
     WHERE t.branch_id = b.id AND t.status IN ('open', 'pending') AND t.first_response_at IS NULL) AS unanswered_tickets
FROM branches b
"""

METRIC_LABEL = {
    "rating": "rating",
    "late": "late deliveries",
    "missed_shifts": "missed shifts",
    "unanswered": "unanswered tickets",
}


def score(m: dict) -> tuple[int, dict[str, float]]:
    """0-100 health score (100 = healthy) and the per-signal penalties."""
    recent, before = m["rating_recent"] or 0, m["rating_before"] or 0
    pen = {
        # a fall in rating hurts most; a low absolute rating hurts too
        "rating": max(0.0, before - recent) * 25 + max(0.0, 3.5 - recent) * 10,
        "late": max(0.0, (m["late_pct"] or 0) - 25) * 1.0,
        "missed_shifts": max(0.0, (m["missed_shift_pct"] or 0) - 3) * 3,
        "unanswered": (m["unanswered_tickets"] or 0) * 1.5,
    }
    return max(0, min(100, round(100 - sum(pen.values())))), {k: round(v, 1) for k, v in pen.items()}


def branch_health(con: sqlite3.Connection | None = None) -> list[dict]:
    """All 12 branches, worst first, with score, signals and the worst metric."""
    own = con is None
    con = con or connect_ro()
    try:
        now = sandbox_now(con)
        out = []
        for m in rows(con, BRANCH_METRICS_SQL,
                      {"now": now, "rwin": f"-{RATING_WINDOW_DAYS} days", "owin": f"-{OPS_WINDOW_DAYS} days"}):
            s, pen = score(m)
            worst = max(pen, key=lambda k: pen[k])
            out.append({
                "branch": m["code"], "name": m["name"], "health_score": s,
                "status": "red" if s < 50 else "amber" if s < 75 else "green",
                "rating_recent": m["rating_recent"], "rating_before": m["rating_before"],
                "late_delivery_pct_30d": m["late_pct"] if m["has_delivery"] else None,
                "missed_shifts_30d": m["missed_shifts"] or 0, "missed_shift_pct_30d": m["missed_shift_pct"],
                "unanswered_tickets": m["unanswered_tickets"],
                "worst_metric": METRIC_LABEL[worst] if pen[worst] > 0 else None,
                "penalties": pen,
            })
        return sorted(out, key=lambda r: (r["health_score"], r["branch"]))
    finally:
        if own:
            con.close()
