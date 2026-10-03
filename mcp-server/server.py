# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2", "psycopg[binary]>=3.1"]  # pinned: v1 API (FastMCP); psycopg = public-site mirror
# ///
"""
BANTAY — Suki Mart's branch watchdog (Layer 1: the hands).

Most dashboards show one department at a time. Bantay's tools let Hermes
connect symptoms ACROSS departments (reviews, deliveries, staffing, support,
inventory) to find WHY a branch is failing, then act on it — and every action
is written to an evidence ledger (`bantay_actions`) so humans can see exactly
what it was based on.

Run standalone:   uv run mcp-server/server.py
Register:         hermes mcp add bantay --command uv --args run /ABS/PATH/mcp-server/server.py
Test:             hermes mcp test bantay
"""
import os
import sqlite3
import sys
import threading
from typing import Any

from mcp.server.fastmcp import FastMCP

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "store.db")

mcp = FastMCP("bantay")

# Weekend evenings = Fri/Sat/Sun (SQLite %w: 5, 6, 0) shift blocks starting 15:00 or later.
WEEKEND_EVENING = "cast(strftime('%w', {d}) as int) in (5, 6, 0) and {h} >= 15"
DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


# ----------------------------------------------------------------- db helpers
def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    con = sqlite3.connect(f"file:{os.path.abspath(DB_PATH)}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def execute(sql: str, params: tuple = ()) -> int:
    con = sqlite3.connect(os.path.abspath(DB_PATH))
    try:
        cur = con.execute(sql, params)
        con.commit()
        return cur.rowcount
    finally:
        con.close()


def now() -> str:
    """The sandbox's 'today' (2026-09-30 21:00:00) — never the real clock."""
    return query("SELECT value FROM sandbox_info WHERE key='sandbox_now'")[0]["value"]


def branch(code: str) -> dict:
    rows = query("SELECT id, code, name, has_delivery FROM branches WHERE code = ?", (code.strip().upper(),))
    if not rows:
        valid = ", ".join(r["code"] for r in query("SELECT code FROM branches ORDER BY code"))
        raise ValueError(f"Unknown branch code '{code}'. Valid codes: {valid}")
    return rows[0]


def ensure_ledger() -> None:
    execute(
        """CREATE TABLE IF NOT EXISTS bantay_actions (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             created_at TEXT NOT NULL,
             action_type TEXT NOT NULL,
             branch_code TEXT,
             target TEXT,
             summary TEXT NOT NULL,
             evidence TEXT NOT NULL,
             status TEXT NOT NULL DEFAULT 'done')"""
    )


def log_action(action_type: str, branch_code: str | None, target: str, summary: str, evidence: str,
               status: str = "done") -> int:
    ensure_ledger()
    execute(
        "INSERT INTO bantay_actions (created_at, action_type, branch_code, target, summary, evidence, status) "
        "VALUES (?,?,?,?,?,?,?)",
        (now(), action_type, branch_code, target, summary, evidence, status),
    )
    aid = query("SELECT max(id) AS id FROM bantay_actions")[0]["id"]
    mirror_public()
    return aid


# ------------------------------------------------- public website mirror (Neon)
# After each Bantay action, refresh the Neon mirror that https://suki-autopilot.vercel.app
# reads, so the action shows up in the site's feed within seconds. It reuses Suki
# Autopilot's snapshot() + neon_sync (one source of truth for the mirror); that module is
# imported lazily and only its functions are called (its FastMCP server is never run).
# Runs on one background worker that coalesces bursts; any failure is swallowed, so the
# mirror can never break or slow down a Bantay action.
_mirror_wake = threading.Event()
_mirror_start = threading.Lock()
_mirror_thread: threading.Thread | None = None


def _mirror_worker() -> None:
    while True:
        _mirror_wake.wait()
        _mirror_wake.clear()
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            if here not in sys.path:
                sys.path.insert(0, here)
            import suki_autopilot  # noqa: PLC0415  (lazy on purpose)
            suki_autopilot.sync_public()
        except Exception:
            pass


def mirror_public() -> None:
    global _mirror_thread
    try:
        with _mirror_start:
            if _mirror_thread is None or not _mirror_thread.is_alive():
                _mirror_thread = threading.Thread(target=_mirror_worker, name="neon-mirror", daemon=True)
                _mirror_thread.start()
        _mirror_wake.set()
    except Exception:
        pass


# ------------------------------------------------------------- signal helpers
def _rating(branch_id: int, days: int) -> dict:
    r = query(
        """SELECT round(avg(CASE WHEN created_at >= datetime(?, ?) THEN rating END), 2) AS recent,
                  round(avg(CASE WHEN created_at <  datetime(?, ?) THEN rating END), 2) AS before,
                  sum(CASE WHEN created_at >= datetime(?, ?) THEN 1 ELSE 0 END) AS recent_n,
                  sum(CASE WHEN rating <= 2 AND replied_at IS NULL THEN 1 ELSE 0 END) AS unreplied_negative
           FROM reviews WHERE branch_id = ?""",
        (now(), f"-{days} days") * 3 + (branch_id,),
    )[0]
    r["drop"] = round((r["before"] or 0) - (r["recent"] or 0), 2)
    return r


def _late(branch_id: int, days: int) -> dict:
    return query(
        """SELECT count(*) AS deliveries,
                  round(100.0 * sum(CASE WHEN d.delivered_at > d.promised_by THEN 1 ELSE 0 END) / max(count(*), 1), 1) AS late_pct,
                  round(100.0 * sum(CASE WHEN d.status IN ('failed','returned') THEN 1 ELSE 0 END) / max(count(*), 1), 1) AS failed_pct
           FROM deliveries d JOIN orders o ON o.id = d.order_id
           WHERE d.branch_id = ? AND o.created_at >= datetime(?, ?)""",
        (branch_id, now(), f"-{days} days"),
    )[0]


def _weekend_evening_coverage(branch_id: int) -> float | None:
    r = query(
        f"""WITH s AS (SELECT shift_date, cast(substr(start_time, 1, 2) AS int) AS sh,
                          sum(CASE WHEN status IN ('completed','swapped') THEN 1 ELSE 0 END) AS present
                   FROM shifts WHERE branch_id = ? AND shift_date <= date(?) GROUP BY shift_date, sh)
            SELECT round(avg(1.0 * s.present / t.min_staff), 2) AS cov
            FROM s JOIN staffing_targets t ON t.branch_id = ? AND t.start_hour = s.sh
                   AND t.day_of_week = cast(strftime('%w', s.shift_date) AS int)
            WHERE {WEEKEND_EVENING.format(d='s.shift_date', h='s.sh')}""",
        (branch_id, now(), branch_id),
    )[0]
    return r["cov"]


def _forgotten_count(branch_id: int, older_than_days: int = 7) -> int:
    return query(
        """SELECT count(*) AS n FROM support_tickets
           WHERE branch_id = ? AND status IN ('open','pending') AND first_response_at IS NULL
             AND created_at < datetime(?, ?)""",
        (branch_id, now(), f"-{older_than_days} days"),
    )[0]["n"]


def _stockouts(branch_id: int) -> int:
    return query(
        "SELECT count(*) AS n FROM inventory WHERE branch_id = ? AND on_hand <= reorder_point / 3",
        (branch_id,),
    )[0]["n"]


# =============================================================== WATCH tools
@mcp.tool()
def branch_health_scan(days: int = 35) -> list[dict]:
    """Morning sweep: score all 12 Suki Mart branches (0-100, worst first) by
    combining FIVE departments' signals — customer rating trend (reviews),
    late deliveries, weekend-evening staffing coverage, forgotten support
    tickets, and stock-outs. Use this first for any 'morning sweep', 'which
    branch needs attention', or 'how are our branches doing' request.
    status: red (<60), amber (60-79), green (80+). `days` = recent window."""
    out = []
    for b in query("SELECT id, code, name, has_delivery FROM branches ORDER BY code"):
        rt = _rating(b["id"], days)
        late = _late(b["id"], 30) if b["has_delivery"] else None
        cov = _weekend_evening_coverage(b["id"])
        forgotten = _forgotten_count(b["id"])
        stockouts = _stockouts(b["id"])
        penalties = {
            "rating_drop": max(0.0, rt["drop"]) * 25,
            "understaffing": max(0.0, 0.9 - (cov or 1)) * 100,
            "late_deliveries": max(0.0, (late["late_pct"] if late else 0) - 25) * 1.2,
            "forgotten_tickets": forgotten * 2.0,
            "stockouts": stockouts * 1.0,
        }
        score = max(0, round(100 - sum(penalties.values())))
        out.append({
            "branch": b["code"], "name": b["name"], "health_score": score,
            "status": "red" if score < 60 else "amber" if score < 80 else "green",
            "rating_recent": rt["recent"], "rating_before": rt["before"], "rating_drop": rt["drop"],
            "late_delivery_pct_30d": late["late_pct"] if late else None,
            "weekend_evening_staffing_pct": round((cov or 0) * 100),
            "forgotten_tickets": forgotten, "unreplied_negative_reviews": rt["unreplied_negative"],
            "stockout_skus": stockouts,
            "biggest_issue": max(penalties, key=lambda k: penalties[k]) if sum(penalties.values()) > 0 else None,
        })
    return sorted(out, key=lambda r: r["health_score"])


@mcp.tool()
def staffing_gaps(branch_code: str, days_ahead: int = 14) -> dict:
    """People & operations evidence for one branch: (1) past 8 weeks' staffing
    coverage by day-of-week and shift block vs. the branch's staffing targets,
    (2) UPCOMING scheduled shift blocks that are below target in the next
    `days_ahead` days, and (3) repeat absentees (staff with 4+ no-shows /
    sick calls). Use when diagnosing a branch or planning shift cover."""
    b = branch(branch_code)
    by_block = query(
        """WITH s AS (SELECT shift_date, cast(substr(start_time,1,2) AS int) AS sh,
                         sum(CASE WHEN status IN ('completed','swapped') THEN 1 ELSE 0 END) AS present
                  FROM shifts WHERE branch_id = ? AND shift_date <= date(?) GROUP BY shift_date, sh)
           SELECT cast(strftime('%w', s.shift_date) AS int) AS dow, s.sh AS start_hour,
                  round(avg(s.present), 1) AS avg_present, t.min_staff AS target,
                  round(100.0 * avg(1.0 * s.present / t.min_staff)) AS coverage_pct
           FROM s JOIN staffing_targets t ON t.branch_id = ? AND t.start_hour = s.sh
                  AND t.day_of_week = cast(strftime('%w', s.shift_date) AS int)
           GROUP BY dow, s.sh HAVING coverage_pct < 85 ORDER BY coverage_pct LIMIT 10""",
        (b["id"], now(), b["id"]),
    )
    for r in by_block:
        r["day"] = DOW[r.pop("dow")]
    upcoming = query(
        """WITH s AS (SELECT shift_date, cast(substr(start_time,1,2) AS int) AS sh, count(*) AS rostered
                  FROM shifts WHERE branch_id = ? AND status = 'scheduled'
                    AND shift_date > date(?) AND shift_date <= date(?, ?) GROUP BY shift_date, sh)
           SELECT s.shift_date, s.sh AS start_hour, s.rostered, t.min_staff AS target,
                  t.min_staff - s.rostered AS missing
           FROM s JOIN staffing_targets t ON t.branch_id = ? AND t.start_hour = s.sh
                  AND t.day_of_week = cast(strftime('%w', s.shift_date) AS int)
           WHERE s.rostered < t.min_staff ORDER BY s.shift_date, s.sh""",
        (b["id"], now(), now(), f"+{days_ahead} days", b["id"]),
    )
    absentees = query(
        """SELECT st.employee_number, st.first_name || ' ' || st.last_name AS name, st.role,
                  sum(CASE WHEN sh.status IN ('no_show','called_in_sick') THEN 1 ELSE 0 END) AS missed,
                  count(*) AS shifts
           FROM shifts sh JOIN staff st ON st.id = sh.staff_id
           WHERE sh.branch_id = ? AND sh.shift_date <= date(?)
           GROUP BY st.id HAVING missed >= 4 ORDER BY missed DESC""",
        (b["id"], now()),
    )
    return {
        "branch": b["code"],
        "weekend_evening_coverage_pct": round((_weekend_evening_coverage(b["id"]) or 0) * 100),
        "weakest_blocks_past_8_weeks": by_block,
        "upcoming_short_blocks": upcoming,
        "upcoming_people_missing": sum(r["missing"] for r in upcoming),
        "repeat_absentees": absentees,
    }


@mcp.tool()
def delivery_hotspots(branch_code: str, days: int = 30) -> dict:
    """Delivery evidence for one branch: overall late/failed rate, the worst
    day-of-week x hour windows (where lateness clusters), average minutes late,
    and the branch's worst riders. Use when diagnosing late-delivery
    complaints or a branch's bad delivery reviews."""
    b = branch(branch_code)
    if not b["has_delivery"]:
        return {"branch": b["code"], "note": "This branch does not offer delivery."}
    windows = query(
        """SELECT cast(strftime('%w', o.created_at) AS int) AS dow,
                  (cast(strftime('%H', o.created_at) AS int) / 2) * 2 AS from_hour,
                  count(*) AS deliveries,
                  round(100.0 * sum(CASE WHEN d.delivered_at > d.promised_by THEN 1 ELSE 0 END) / count(*), 1) AS late_pct,
                  round(avg(CASE WHEN d.delivered_at > d.promised_by
                       THEN (julianday(d.delivered_at) - julianday(d.promised_by)) * 1440 END)) AS avg_min_late
           FROM deliveries d JOIN orders o ON o.id = d.order_id
           WHERE d.branch_id = ? AND o.created_at >= datetime(?, ?) AND d.delivered_at IS NOT NULL
           GROUP BY dow, from_hour HAVING deliveries >= 4 ORDER BY late_pct DESC, deliveries DESC LIMIT 5""",
        (b["id"], now(), f"-{days} days"),
    )
    for w in windows:
        start = w.pop("from_hour")
        w["window"] = f"{DOW[w.pop('dow')]} {start:02d}:00-{start + 2:02d}:00"
    riders = query(
        """SELECT r.id AS rider_id, r.first_name || ' ' || r.last_name AS name, r.status, r.rating,
                  count(*) AS deliveries,
                  round(100.0 * sum(CASE WHEN d.status IN ('failed','returned') THEN 1 ELSE 0 END) / count(*), 1) AS failed_pct,
                  round(100.0 * sum(CASE WHEN d.delivered_at > d.promised_by THEN 1 ELSE 0 END) / count(*), 1) AS late_pct
           FROM deliveries d JOIN riders r ON r.id = d.rider_id
           WHERE d.branch_id = ? GROUP BY r.id HAVING deliveries >= 15 ORDER BY failed_pct DESC LIMIT 3""",
        (b["id"],),
    )
    return {"branch": b["code"], "last_n_days": days, **_late(b["id"], days),
            "worst_windows": windows, "worst_riders": riders}


@mcp.tool()
def review_pulse(branch_code: str, days: int = 35) -> dict:
    """Customer-experience evidence for one branch: average rating in the last
    `days` vs. before, what recent negative reviews complain about (topic
    counts), and up to 8 UNREPLIED negative reviews (with review_id) that need
    a response. Use when diagnosing a branch or before replying to reviews."""
    b = branch(branch_code)
    rt = _rating(b["id"], days)
    topics = query(
        """SELECT topic, count(*) AS negative_reviews FROM reviews
           WHERE branch_id = ? AND rating <= 2 AND created_at >= datetime(?, ?)
           GROUP BY topic ORDER BY negative_reviews DESC""",
        (b["id"], now(), f"-{days} days"),
    )
    unreplied = query(
        """SELECT id AS review_id, source, rating, topic, title, body, created_at FROM reviews
           WHERE branch_id = ? AND rating <= 2 AND replied_at IS NULL
           ORDER BY created_at DESC LIMIT 8""",
        (b["id"],),
    )
    return {"branch": b["code"], "rating_recent": rt["recent"], "rating_before": rt["before"],
            "rating_drop": rt["drop"], "recent_reviews": rt["recent_n"],
            "recent_negative_topics": topics, "unreplied_negative_total": rt["unreplied_negative"],
            "unreplied_negative_latest": unreplied}


@mcp.tool()
def forgotten_tickets(branch_code: str | None = None, older_than_days: int = 7) -> list[dict]:
    """Support tickets that are still open/pending, were NEVER responded to,
    and are older than `older_than_days` — customers who were forgotten.
    Optionally filter by branch_code. Oldest first."""
    sql = """SELECT t.ticket_number, b.code AS branch, t.category, t.priority, t.channel, t.subject,
                    t.created_at, cast(julianday(?) - julianday(t.created_at) AS int) AS days_waiting
             FROM support_tickets t LEFT JOIN branches b ON b.id = t.branch_id
             WHERE t.status IN ('open','pending') AND t.first_response_at IS NULL
               AND t.created_at < datetime(?, ?)"""
    params: tuple = (now(), now(), f"-{older_than_days} days")
    if branch_code:
        sql += " AND t.branch_id = ?"
        params += (branch(branch_code)["id"],)
    return query(sql + " ORDER BY t.created_at LIMIT 25", params)


@mcp.tool()
def stock_rebalance_options(branch_code: str | None = None, limit: int = 15) -> list[dict]:
    """Inventory: products that are (nearly) OUT at one branch while another
    branch holds 60+ days of cover — fix by TRANSFER instead of a new purchase
    order. Suggests the donor branch and a transfer quantity, and flags if an
    open purchase order already exists. Optionally filter to the short branch."""
    sql = """WITH inv AS (SELECT i.*, b.code, p.name, p.sku,
                                 CASE WHEN i.avg_daily_sales > 0 THEN i.on_hand / i.avg_daily_sales END AS cover
                          FROM inventory i JOIN branches b ON b.id = i.branch_id JOIN products p ON p.id = i.product_id)
             SELECT s.sku, s.name AS product, s.code AS short_branch, s.on_hand AS short_on_hand,
                    s.reorder_point, round(s.avg_daily_sales, 1) AS daily_sales,
                    d.code AS donor_branch, d.on_hand AS donor_on_hand, round(d.cover) AS donor_days_cover,
                    min(s.reorder_qty, cast(d.on_hand - 30 * d.avg_daily_sales AS int)) AS suggested_qty,
                    (SELECT count(*) FROM purchase_orders po WHERE po.branch_id = s.branch_id
                       AND po.product_id = s.product_id AND po.status IN ('pending','in_transit')) AS open_pos
             FROM inv s JOIN inv d ON d.product_id = s.product_id AND d.branch_id != s.branch_id
             WHERE s.on_hand <= s.reorder_point / 3 AND d.cover >= 60
               AND d.cover = (SELECT max(x.cover) FROM inv x WHERE x.product_id = s.product_id)"""
    params: tuple = ()
    if branch_code:
        sql += " AND s.branch_id = ?"
        params = (branch(branch_code)["id"],)
    return query(sql + " ORDER BY s.on_hand, d.cover DESC LIMIT ?", params + (limit,))


# ================================================================ ACT tools
@mcp.tool()
def escalate_ticket(ticket_number: str, reason: str) -> dict:
    """ACTION: escalate a forgotten support ticket to 'urgent' priority so the
    CSR team handles it first. `reason` must cite the evidence (e.g. '23 days
    with no response; branch rating dropped 1.4 stars'). Logged to the ledger."""
    t = query("SELECT ticket_number, priority, status, created_at FROM support_tickets WHERE ticket_number = ?",
              (ticket_number,))
    if not t:
        return {"ok": False, "error": f"Ticket {ticket_number} not found"}
    if t[0]["status"] not in ("open", "pending"):
        return {"ok": False, "error": f"Ticket is already {t[0]['status']}"}
    execute("UPDATE support_tickets SET priority = 'urgent' WHERE ticket_number = ?", (ticket_number,))
    code = query("""SELECT b.code FROM support_tickets t JOIN branches b ON b.id = t.branch_id
                    WHERE t.ticket_number = ?""", (ticket_number,))
    aid = log_action("escalate_ticket", code[0]["code"] if code else None, ticket_number,
                     f"Escalated {ticket_number} from {t[0]['priority']} to urgent", reason)
    return {"ok": True, "action_id": aid, "ticket": ticket_number, "priority": "urgent"}


@mcp.tool()
def reply_to_review(review_id: int, reply_text: str) -> dict:
    """ACTION: post a public reply to an unreplied customer review (use
    review_id from review_pulse). Write warm, Taglish-friendly, specific
    replies that acknowledge the exact complaint — no generic templates.
    Logged to the ledger."""
    r = query("""SELECT r.id, r.rating, r.topic, r.replied_at, b.code FROM reviews r
                 JOIN branches b ON b.id = r.branch_id WHERE r.id = ?""", (review_id,))
    if not r:
        return {"ok": False, "error": f"Review {review_id} not found"}
    if r[0]["replied_at"]:
        return {"ok": False, "error": "Review already has a reply"}
    execute("UPDATE reviews SET replied_at = ?, reply_text = ? WHERE id = ?", (now(), reply_text, review_id))
    aid = log_action("reply_to_review", r[0]["code"], f"review #{review_id}",
                     f"Replied to {r[0]['rating']}★ {r[0]['topic']} review", reply_text)
    return {"ok": True, "action_id": aid, "review_id": review_id}


@mcp.tool()
def request_shift_cover(branch_code: str, shift_date: str, start_hour: int, role: str,
                        people_needed: int, reason: str) -> dict:
    """ACTION: file a shift-cover request for an UPCOMING understaffed block
    (from staffing_gaps.upcoming_short_blocks) so the branch manager can fill
    it from the floater pool. role: cashier | picker | stock_clerk | supervisor.
    `reason` must cite the evidence. Logged to the ledger as 'requested'."""
    b = branch(branch_code)
    aid = log_action("shift_cover", b["code"], f"{shift_date} {start_hour:02d}:00 · {role} x{people_needed}",
                     f"Requested {people_needed} {role}(s) for {b['code']} {shift_date} {start_hour:02d}:00",
                     reason, status="requested")
    return {"ok": True, "action_id": aid, "status": "requested"}


@mcp.tool()
def create_stock_transfer(sku: str, from_branch: str, to_branch: str, quantity: int, reason: str) -> dict:
    """ACTION: request an inter-branch stock transfer (use rows from
    stock_rebalance_options) instead of a new purchase order. Refuses if the
    donor would drop below 30 days of cover. Logged to the ledger."""
    src, dst = branch(from_branch), branch(to_branch)
    inv = query("""SELECT i.on_hand, i.avg_daily_sales, p.name FROM inventory i JOIN products p ON p.id = i.product_id
                   WHERE p.sku = ? AND i.branch_id = ?""", (sku, src["id"]))
    if not inv:
        return {"ok": False, "error": f"SKU {sku} not stocked at {src['code']}"}
    keep = 30 * inv[0]["avg_daily_sales"]
    if inv[0]["on_hand"] - quantity < keep:
        return {"ok": False, "error": f"{src['code']} would drop below 30 days of cover (keep {round(keep)})"}
    aid = log_action("stock_transfer", dst["code"], f"{sku} {src['code']}→{dst['code']} x{quantity}",
                     f"Transfer {quantity} x {inv[0]['name']} from {src['code']} to {dst['code']}",
                     reason, status="requested")
    return {"ok": True, "action_id": aid, "status": "requested"}


@mcp.tool()
def action_ledger(branch_code: str | None = None, limit: int = 20) -> list[dict]:
    """The evidence ledger: every action Bantay has taken (escalations, review
    replies, shift-cover requests, stock transfers) with the evidence it was
    based on. Use to show 'what did Bantay do and why'."""
    ensure_ledger()
    sql = "SELECT id, created_at, action_type, branch_code, target, summary, evidence, status FROM bantay_actions"
    params: tuple = ()
    if branch_code:
        sql += " WHERE branch_code = ?"
        params = (branch_code.upper(),)
    return query(sql + " ORDER BY id DESC LIMIT ?", params + (limit,))


@mcp.tool()
def describe_sandbox() -> dict:
    """Describe the Suki Mart sandbox: business context, 'sandbox_now', and
    every table with its row count. Only needed if the data model is unclear."""
    info = {r["key"]: r["value"] for r in query("SELECT key, value FROM sandbox_info")}
    tables = {t["name"]: query(f"SELECT COUNT(*) AS n FROM {t['name']}")[0]["n"]
              for t in query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}
    return {"info": info, "tables": tables}


if __name__ == "__main__":
    mcp.run()
