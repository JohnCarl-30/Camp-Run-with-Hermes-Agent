# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2", "psycopg[binary]>=3.1"]
# ///
"""
SUKI AUTOPILOT: an autonomous operations agent for Suki Mart (Layer 1, the hands).

It diagnoses a struggling branch, then acts on stock and purchasing problems in
the database. Every action is written to the `agent_actions` evidence ledger
BEFORE the change is applied, and a risk policy enforced here in code (not left
to the model) decides what is applied automatically and what waits for a human
to approve.

Run standalone:   uv run mcp-server/suki_autopilot.py
Register:         hermes mcp add suki-autopilot --command uv --args run /ABS/PATH/mcp-server/suki_autopilot.py
Test:             hermes mcp test suki-autopilot
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import suki_health  # noqa: E402  (shared health-score SQL, also used by the desktop plugin backend)
import neon_sync  # noqa: E402  (mirrors state to Neon for the public Vercel site)

DB_PATH = suki_health.DB_PATH
mcp = FastMCP("suki-autopilot")

# ------------------------------------------------------------------ risk policy
TRANSFER_AUTO_MAX_QTY = 100        # transfers above this wait for approval
TRANSFER_MIN_DONOR_COVER = 14      # donor must keep this many days of cover, or approval
MARKDOWN_AUTO_MAX_PCT = 30         # markdowns above this wait for approval
DONOR_KEEP_DAYS = 30               # suggested qty never takes the donor below 30 days
DUPLICATE_WINDOW_DAYS = 2
OPEN_PO = ("pending", "in_transit")
LIVE_STATUSES = ("applied", "pending_approval", "approved")   # an action that still "counts"


# -------------------------------------------------------------------- db helpers
def query(sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    con = suki_health.connect_ro()
    try:
        return suki_health.rows(con, sql, params)
    finally:
        con.close()


def connect_rw() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def ensure_tables() -> None:
    """Created on startup and before every write, so they survive `seed.py` resets."""
    con = connect_rw()
    try:
        con.executescript(
            """CREATE TABLE IF NOT EXISTS agent_actions (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 created_at TEXT NOT NULL,
                 action_type TEXT NOT NULL,          -- transfer_stock | markdown | cancel_po
                 target TEXT NOT NULL,
                 summary TEXT NOT NULL,
                 evidence_json TEXT NOT NULL,
                 reasoning TEXT NOT NULL,
                 confidence REAL NOT NULL,
                 risk TEXT NOT NULL,                 -- low | high
                 status TEXT NOT NULL,               -- applied | pending_approval | approved | rejected
                 payload_json TEXT NOT NULL,
                 decided_at TEXT,
                 php_impact REAL NOT NULL DEFAULT 0);
               CREATE TABLE IF NOT EXISTS markdowns (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 branch_id INTEGER NOT NULL,
                 product_id INTEGER NOT NULL,
                 pct INTEGER NOT NULL,
                 starts_on TEXT NOT NULL,
                 ends_on TEXT NOT NULL,
                 action_id INTEGER NOT NULL);"""
        )
        con.commit()
    finally:
        con.close()


def now() -> str:
    return query("SELECT value FROM sandbox_info WHERE key='sandbox_now'")[0]["value"]


def today() -> str:
    return now()[:10]


def err(msg: str) -> dict:
    return {"ok": False, "error": msg, "written": False}


def branch_row(code: str) -> dict | None:
    r = query("SELECT id, code, name FROM branches WHERE code = ?", ((code or "").strip().upper(),))
    return r[0] if r else None


def valid_codes() -> str:
    return ", ".join(r["code"] for r in query("SELECT code FROM branches ORDER BY code"))


def product_row(sku: str) -> dict | None:
    r = query("SELECT id, sku, name, cost_price, retail_price, is_perishable FROM products WHERE sku = ?",
              ((sku or "").strip().upper(),))
    return r[0] if r else None


def inv_row(branch_id: int, product_id: int) -> dict | None:
    r = query("SELECT * FROM inventory WHERE branch_id = ? AND product_id = ?", (branch_id, product_id))
    return r[0] if r else None


def cover_days(on_hand: float, ads: float) -> float | None:
    return round(on_hand / ads, 1) if ads and ads > 0 else None


def clean_attr(v: Any, limit: int = 140) -> str:
    """Directive attribute values are plain strings with no double quotes or newlines."""
    s = " ".join(str(v).replace('"', "'").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


STATUS_ATTR = {"applied": "applied", "pending_approval": "pending", "approved": "approved", "rejected": "rejected"}


def directive(a: dict) -> str:
    """The exact `::suki-action{…}` paragraph the skill tells Hermes to paste."""
    return ('::suki-action{id="%s" status="%s" type="%s" summary="%s" evidence="%s" confidence="%.2f" php="%d"}'
            % (a["id"], STATUS_ATTR[a["status"]], a["action_type"], clean_attr(a["summary"]),
               clean_attr(a["evidence_line"]), a["confidence"], round(a["php_impact"])))


def existing_live_action(action_type: str, target: str) -> dict | None:
    ensure_tables()
    r = query(f"""SELECT id, status FROM agent_actions WHERE action_type = ? AND target = ?
                  AND status IN ({','.join('?' * len(LIVE_STATUSES))}) ORDER BY id DESC LIMIT 1""",
              (action_type, target) + LIVE_STATUSES)
    return r[0] if r else None


def record(con: sqlite3.Connection, *, action_type: str, target: str, summary: str, facts: dict,
           evidence_line: str, agent_evidence: str, reasoning: str, confidence: float, risk: str,
           payload: dict, php_impact: float) -> dict:
    """Write the ledger row FIRST (inside the caller's transaction); the caller applies after."""
    status = "applied" if risk == "low" else "pending_approval"
    evidence = {"facts": facts, "line": evidence_line, "agent_evidence": agent_evidence}
    cur = con.execute(
        """INSERT INTO agent_actions (created_at, action_type, target, summary, evidence_json, reasoning,
                                      confidence, risk, status, payload_json, decided_at, php_impact)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now(), action_type, target, summary, json.dumps(evidence), reasoning, round(confidence, 2), risk,
         status, json.dumps(payload), now() if status == "applied" else None, round(php_impact, 2)),
    )
    return {"id": cur.lastrowid, "action_type": action_type, "target": target, "summary": summary,
            "status": status, "risk": risk, "confidence": round(confidence, 2), "evidence_line": evidence_line,
            "php_impact": round(php_impact, 2)}


def snapshot() -> dict:
    """Everything the public dashboard shows, read from the local DB."""
    kpt = diagnose_branch("KPT")
    imb, exp, dup = find_stock_imbalances(limit=1), find_expiring_stock(limit=1), find_duplicate_purchase_orders()
    actions = _ledger_rows(None, 500)
    return {
        "as_of": now(),
        "problems": {
            "kpt": {"rating_before": kpt["metrics"]["rating_before"], "rating_recent": kpt["metrics"]["rating_recent"],
                    "missed_shifts_30d": kpt["metrics"]["missed_shifts_30d"], "score": kpt["metrics"]["health_score"]},
            "stock": {"products": imb["products_affected"], "pairs": imb["empty_donor_pairs"], "fixable": imb["stockouts_with_donor"]},
            "expiring": {"rows": exp["rows_expiring"], "php": exp["total_value_at_cost_php"]},
            "duplicates": {"count": dup["duplicates"], "php": dup["total_value_php"]},
        },
        "branches": suki_health.branch_health(),
        "actions": actions,
        "branch_detail": _additive(_branch_detail, actions, default={}),   # website drill-down
        "bantay": _additive(_bantay_rows, 50, default=None),  # Bantay fixes; None = read failed, Neon rows kept
    }


def _additive(fn, *args, default):
    """Website-only extras must never stop the core mirror (actions, branches, problems) from syncing."""
    try:
        return fn(*args)
    except Exception:
        return default


def _bantay_rows(limit: int = 50) -> list[dict]:
    """Latest Bantay ledger rows (mcp-server/server.py). [] until Bantay has created its table."""
    try:
        return query("""SELECT id, created_at, action_type, branch_code, target, summary, evidence, status
                        FROM bantay_actions ORDER BY id DESC LIMIT ?""", (limit,))
    except sqlite3.OperationalError:  # no such table (fresh seed.py, Bantay never acted)
        return []


def _branch_detail(actions: list[dict]) -> dict[str, dict]:
    """Per-branch 'why it's struggling' extras for the website (cheap: ~15 small queries, once per sync).
    Same definitions as diagnose_branch: negative = rating <= 2 in the last 35 days; lapsed regular =
    8+ orders before Aug 1 and none since."""
    n = now()
    out: dict[str, dict] = {}
    for b in query("SELECT id, code FROM branches"):  # the exact topic query of diagnose_branch (same tie order)
        topics = query("""SELECT topic, count(*) AS negative_reviews FROM reviews
                          WHERE branch_id = ? AND rating <= 2 AND created_at >= datetime(?, '-35 days')
                          GROUP BY topic ORDER BY negative_reviews DESC LIMIT 3""", (b["id"], n))
        out[b["code"]] = {"top_negative_topics": topics, "lapsed_regulars": 0, "action_ids": []}
    for r in query("""SELECT b.code, count(*) AS n FROM customers c JOIN branches b ON b.id = c.home_branch_id
                      WHERE (SELECT count(*) FROM orders o WHERE o.customer_id = c.id AND o.created_at < '2026-08-01') >= 8
                        AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id AND o.created_at >= '2026-08-01')
                      GROUP BY b.code"""):
        out[r["code"]]["lapsed_regulars"] = r["n"]
    pos = [a["payload"].get("po_number") for a in actions if a["action_type"] == "cancel_po"]
    po_branch = {r["po_number"]: r["code"] for r in query(
        f"""SELECT po.po_number, b.code FROM purchase_orders po JOIN branches b ON b.id = po.branch_id
            WHERE po.po_number IN ({','.join('?' * len(pos))})""", tuple(pos))} if pos else {}
    for a in actions:  # the branch each action fixes: transfer -> receiving branch, markdown -> its branch, PO -> its branch
        p = a["payload"]
        code = p.get("to_branch") or p.get("branch_code") or po_branch.get(p.get("po_number"))
        if code in out:
            out[code]["action_ids"].append(a["id"])
    return out


def sync_public() -> dict:
    try:
        return neon_sync.sync(snapshot())
    except Exception as e:  # never let the mirror break a local action
        return {"neon": f"sync failed: {e}"}


def finish(a: dict, extra: dict | None = None) -> dict:
    out = {"ok": True, "action_id": a["id"], "status": a["status"], "risk": a["risk"],
           "summary": a["summary"], "evidence": a["evidence_line"], "confidence": a["confidence"],
           "php_impact": a["php_impact"], "directive": directive(a)}
    if a["status"] == "pending_approval":
        out["note"] = f"Held for human approval (risk policy). Ask the user to approve or reject action #{a['id']}."
    return {**out, **(extra or {}), "public_dashboard": sync_public()["neon"]}


# ============================================================ DIAGNOSE tools
@mcp.tool()
def get_branch_health() -> list[dict]:
    """Score all 12 Suki Mart branches 0-100 (100 = healthy), worst first.
    Built from: rating in the last 35 days vs before, late-delivery % (30 d),
    missed-shift % (no-shows + sick calls, 30 d), and open support tickets with
    no first response. Returns each branch's raw signals and its worst metric.
    Call this first for any 'which branch is struggling' / autopilot / diagnose request."""
    return suki_health.branch_health()


@mcp.tool()
def diagnose_branch(branch_code: str) -> dict:
    """Deep diagnosis of ONE branch (e.g. KPT): its health metrics next to the
    chain average, the top 3 topics of recent negative reviews (rating <= 2,
    last 35 days), missed shifts and late-delivery %, unanswered tickets, and
    how many lapsed regulars (8+ orders before Aug 1, none since) call it home.
    Use to explain WHY a branch is declining."""
    b = branch_row(branch_code)
    if not b:
        return err(f"Unknown branch code '{branch_code}'. Valid codes: {valid_codes()}")
    health = suki_health.branch_health()
    me = next(h for h in health if h["branch"] == b["code"])

    def avg(key: str) -> float | None:
        vals = [h[key] for h in health if h[key] is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    keys = ["health_score", "rating_recent", "rating_before", "late_delivery_pct_30d", "missed_shifts_30d",
            "missed_shift_pct_30d", "unanswered_tickets"]
    n = now()
    topics = query("""SELECT topic, count(*) AS negative_reviews FROM reviews
                      WHERE branch_id = ? AND rating <= 2 AND created_at >= datetime(?, '-35 days')
                      GROUP BY topic ORDER BY negative_reviews DESC LIMIT 3""", (b["id"], n))
    sample = query("""SELECT topic, rating, title, substr(body, 1, 140) AS excerpt, created_at FROM reviews
                      WHERE branch_id = ? AND rating <= 2 AND created_at >= datetime(?, '-35 days')
                      ORDER BY created_at DESC LIMIT 3""", (b["id"], n))
    missed_by_block = query("""SELECT CASE CAST(strftime('%w', shift_date) AS int) WHEN 0 THEN 'Sun' WHEN 1 THEN 'Mon'
                                 WHEN 2 THEN 'Tue' WHEN 3 THEN 'Wed' WHEN 4 THEN 'Thu' WHEN 5 THEN 'Fri' ELSE 'Sat' END AS day,
                                 substr(start_time, 1, 5) AS starts, count(*) AS missed
                               FROM shifts WHERE branch_id = ? AND status IN ('no_show','called_in_sick')
                                 AND shift_date >= date(?, '-30 days') AND shift_date <= date(?)
                               GROUP BY day, starts ORDER BY missed DESC LIMIT 3""", (b["id"], n, n))
    lapsed = query("""SELECT count(*) AS n FROM customers c
                      WHERE c.home_branch_id = ?
                        AND (SELECT count(*) FROM orders o WHERE o.customer_id = c.id AND o.created_at < '2026-08-01') >= 8
                        AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id AND o.created_at >= '2026-08-01')""",
                   (b["id"],))[0]["n"]
    oldest = query("""SELECT ticket_number, category, cast(julianday(?) - julianday(created_at) AS int) AS days_waiting
                      FROM support_tickets WHERE branch_id = ? AND status IN ('open','pending')
                        AND first_response_at IS NULL ORDER BY created_at LIMIT 3""", (n, b["id"]))
    return {
        "branch": b["code"], "name": b["name"], "as_of": n,
        "rank": f"{[h['branch'] for h in health].index(b['code']) + 1} of {len(health)} (1 = worst)",
        "metrics": {k: me[k] for k in keys}, "chain_average": {k: avg(k) for k in keys},
        "worst_metric": me["worst_metric"], "penalties": me["penalties"],
        "top_negative_review_topics": topics, "recent_negative_reviews": sample,
        "missed_shifts_by_block": missed_by_block, "oldest_unanswered_tickets": oldest,
        "lapsed_regulars": lapsed,
    }


# =============================================================== FIND tools
@mcp.tool()
def find_stock_imbalances(min_donor_days: int = 60, limit: int = 20) -> dict:
    """Stock-outs fixable by TRANSFER instead of a new purchase order: a product
    with 0 on hand at one branch while another branch holds >= min_donor_days
    of cover. One row per (product, empty branch) using the best donor.
    suggested_qty = min(empty branch reorder_qty, donor stock above 30 days of cover).
    Ordered by daily sales lost at the empty branch."""
    sql = """
    WITH inv AS (SELECT i.*, b.code, p.sku, p.name, p.cost_price, p.retail_price,
                        CASE WHEN i.avg_daily_sales > 0 THEN 1.0 * i.on_hand / i.avg_daily_sales END AS cover
                 FROM inventory i JOIN branches b ON b.id = i.branch_id JOIN products p ON p.id = i.product_id),
    pairs AS (SELECT e.sku, e.name AS product, e.code AS empty_branch, e.avg_daily_sales AS empty_daily_sales,
                     e.reorder_qty, d.code AS donor_branch, d.on_hand AS donor_on_hand,
                     d.avg_daily_sales AS donor_daily_sales, d.cover AS donor_cover, e.cost_price, e.retail_price,
                     row_number() OVER (PARTITION BY e.product_id, e.branch_id ORDER BY d.cover DESC) AS rk,
                     count(*) OVER (PARTITION BY e.product_id, e.branch_id) AS donor_options
              FROM inv e JOIN inv d ON d.product_id = e.product_id AND d.branch_id != e.branch_id
              WHERE e.on_hand = 0 AND d.cover >= ?)
    SELECT * FROM pairs WHERE rk = 1 ORDER BY empty_daily_sales * retail_price DESC"""
    found = query(sql, (min_donor_days,))
    out = []
    for r in found:
        qty = min(r["reorder_qty"], int(r["donor_on_hand"] - DONOR_KEEP_DAYS * r["donor_daily_sales"]))
        if qty <= 0:
            continue
        after = cover_days(r["donor_on_hand"] - qty, r["donor_daily_sales"])
        out.append({
            "sku": r["sku"], "product": r["product"], "empty_branch": r["empty_branch"], "empty_on_hand": 0,
            "empty_daily_sales": round(r["empty_daily_sales"], 1), "donor_branch": r["donor_branch"],
            "donor_on_hand": r["donor_on_hand"], "donor_days_cover": round(r["donor_cover"]),
            "suggested_qty": qty, "donor_days_cover_after": after,
            "lost_sales_per_day_php": round(r["empty_daily_sales"] * r["retail_price"]),
            "would_auto_apply": qty <= TRANSFER_AUTO_MAX_QTY and (after or 0) >= TRANSFER_MIN_DONOR_COVER,
        })
    pairs_total = query("""SELECT count(*) AS pairs, count(DISTINCT e.product_id) AS products FROM inventory e
                           JOIN inventory d ON d.product_id = e.product_id AND d.branch_id != e.branch_id
                           WHERE e.on_hand = 0 AND d.avg_daily_sales > 0 AND 1.0 * d.on_hand / d.avg_daily_sales >= ?""",
                        (min_donor_days,))[0]
    return {"products_affected": pairs_total["products"], "empty_donor_pairs": pairs_total["pairs"],
            "stockouts_with_donor": len(out), "rows": out[:limit]}


def _markdown_pct(on_hand: int, ads: float, days_left: int) -> tuple[int, float]:
    """Deeper markdown the further stock is from selling through before expiry."""
    ratio = (on_hand / ads) / days_left if ads > 0 else 99.0
    for limit, pct in ((1, 10), (2, 20), (4, 30), (8, 40)):
        if ratio <= limit:
            return pct, round(ratio, 1)
    return 50, round(ratio, 1)


@mcp.tool()
def find_expiring_stock(days: int = 2, limit: int = 20) -> dict:
    """Perishable stock (on_hand > 0) whose nearest expiry is within `days` days
    of today: sku, branch, qty, value at cost, average daily sales, and a
    suggested markdown % (10-50, deeper when stock can't sell through before
    expiry at the current pace). Largest peso exposure first."""
    t = today()
    found = query("""SELECT p.sku, p.name AS product, b.code AS branch, i.on_hand, i.avg_daily_sales,
                            i.nearest_expiry_date, p.cost_price, p.retail_price,
                            round(i.on_hand * p.cost_price, 2) AS value_at_cost
                     FROM inventory i JOIN products p ON p.id = i.product_id JOIN branches b ON b.id = i.branch_id
                     WHERE p.is_perishable = 1 AND i.on_hand > 0 AND i.nearest_expiry_date IS NOT NULL
                       AND i.nearest_expiry_date <= date(?, ?)
                     ORDER BY value_at_cost DESC""", (t, f"+{days} days"))
    rows_out = []
    for r in found:
        days_left = max(1, (datetime.fromisoformat(r["nearest_expiry_date"]) - datetime.fromisoformat(t)).days + 1)
        pct, ratio = _markdown_pct(r["on_hand"], r["avg_daily_sales"], days_left)
        rows_out.append({
            "sku": r["sku"], "product": r["product"], "branch": r["branch"], "on_hand": r["on_hand"],
            "expires": r["nearest_expiry_date"], "selling_days_left": days_left,
            "avg_daily_sales": round(r["avg_daily_sales"], 1),
            "days_of_stock": cover_days(r["on_hand"], r["avg_daily_sales"]),
            "value_at_cost_php": round(r["value_at_cost"]), "suggested_markdown_pct": pct,
            "stock_to_sell_ratio": ratio, "would_auto_apply": pct <= MARKDOWN_AUTO_MAX_PCT,
        })
    return {"as_of": t, "rows_expiring": len(found),
            "total_value_at_cost_php": round(sum(r["value_at_cost"] for r in found)), "rows": rows_out[:limit]}


@mcp.tool()
def find_duplicate_purchase_orders() -> dict:
    """Duplicate purchase orders: open (pending / in_transit) POs for the same
    supplier, branch, product and quantity placed within 2 days of an earlier
    open PO. The earliest PO in each group is kept as the original; every later
    one is marked as the duplicate to cancel, with its peso value."""
    found = query("""SELECT po.po_number, po.supplier_id, s.name AS supplier, po.branch_id, b.code AS branch,
                            po.product_id, p.sku, p.name AS product, po.quantity, po.unit_cost, po.status, po.ordered_at
                     FROM purchase_orders po JOIN suppliers s ON s.id = po.supplier_id
                     JOIN branches b ON b.id = po.branch_id JOIN products p ON p.id = po.product_id
                     WHERE po.status IN ('pending','in_transit') ORDER BY po.ordered_at, po.id""")
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in found:
        groups[(r["supplier_id"], r["branch_id"], r["product_id"], r["quantity"])].append(r)
    dupes = []
    for g in groups.values():
        first = g[0]
        t0 = datetime.fromisoformat(first["ordered_at"])
        for r in g[1:]:
            gap_h = (datetime.fromisoformat(r["ordered_at"]) - t0).total_seconds() / 3600
            if gap_h <= DUPLICATE_WINDOW_DAYS * 24:
                dupes.append({
                    "duplicate_po": r["po_number"], "original_po": first["po_number"],
                    "original_status": first["status"], "supplier": r["supplier"], "branch": r["branch"],
                    "sku": r["sku"], "product": r["product"], "quantity": r["quantity"],
                    "hours_after_original": round(gap_h, 1), "value_php": round(r["quantity"] * r["unit_cost"]),
                })
    dupes.sort(key=lambda d: -d["value_php"])
    return {"duplicates": len(dupes), "total_value_php": sum(d["value_php"] for d in dupes), "rows": dupes}


def _stock_status(on_hand: float, reorder_point: float) -> str:
    return "out of stock" if on_hand <= 0 else "low" if on_hand <= reorder_point else "ok"


_STOCK_ORDER = {"out of stock": 0, "low": 1, "ok": 2}


def _stock_rows(where: str, params: tuple) -> list[dict]:
    found = query(f"""SELECT p.sku, p.name AS product, p.unit, p.retail_price, b.code AS branch, i.on_hand,
                             i.reorder_point, i.avg_daily_sales, i.nearest_expiry_date
                      FROM inventory i JOIN products p ON p.id = i.product_id JOIN branches b ON b.id = i.branch_id
                      WHERE {where}""", params)
    out = []
    for r in found:
        out.append({"sku": r["sku"], "product": r["product"], "unit": r["unit"], "retail_price": r["retail_price"],
                    "branch": r["branch"], "on_hand": r["on_hand"], "reorder_point": r["reorder_point"],
                    "avg_daily_sales": round(r["avg_daily_sales"] or 0, 1),
                    "days_of_cover": cover_days(r["on_hand"], r["avg_daily_sales"]),
                    "nearest_expiry_date": r["nearest_expiry_date"],
                    "status": _stock_status(r["on_hand"], r["reorder_point"])})
    # out of stock first, then low, then ok; within a status the least cover (unknown cover last) first
    out.sort(key=lambda x: (_STOCK_ORDER[x["status"]], x["days_of_cover"] is None, x["days_of_cover"] or 0,
                            x["branch"], x["sku"]))
    return out


@mcp.tool()
def check_product_stock(product: str, branch_code: str | None = None, limit: int = 12) -> dict:
    """READ-ONLY. Use when someone asks how much of a product is left, its price,
    or when it expires ("magkano stock ng Jasmine Rice sa Kapitolyo?", "may stock
    pa ba ng gatas sa BGC?") — including from Telegram. `product` is matched
    case-insensitively against product name or SKU; every word you pass must
    appear (so "jasmine rice" works, "rice jasmine" works). Pass ENGLISH product
    words as they appear in the catalogue — translate Tagalog first (bigas →
    rice, tubig → water). `branch_code` (optional, e.g. KPT) limits it to one
    branch; leave it out to compare all 12. Returns per product and branch: sku,
    unit, retail_price, on_hand, avg_daily_sales, days_of_cover (null when it
    doesn't sell), nearest_expiry_date and status ("out of stock" | "low" |
    "ok"), out-of-stock first. Changes nothing."""
    words = [w for w in (product or "").lower().split() if w]
    if not words:
        return {"matches": 0, "hint": "Give a product name or SKU, e.g. 'Jasmine Rice' or 'SM-BAB-0119'."}
    where = " AND ".join("(lower(p.name) LIKE ? OR lower(p.sku) LIKE ?)" for _ in words)
    params: tuple = tuple(x for w in words for x in (f"%{w}%", f"%{w}%"))
    branch = None
    if branch_code:
        branch = branch_row(branch_code)
        if not branch:
            return err(f"Unknown branch code '{branch_code}'. Valid codes: {valid_codes()}")
        where += " AND i.branch_id = ?"
        params += (branch["id"],)
    limit = max(1, min(int(limit), 50))
    rows_all = _stock_rows(where, params)
    if not rows_all:
        return {"matches": 0, "hint": f"No product matching '{product}'"
                + (f" at {branch['code']}" if branch else "")
                + ". Try a shorter or English word (e.g. 'rice' not 'bigas'), or a SKU."}
    return {"as_of": today(), "branch": branch["code"] if branch else "all",
            "products_matched": len({r["sku"] for r in rows_all}), "matches": len(rows_all),
            "out_of_stock": sum(r["status"] == "out of stock" for r in rows_all),
            "low": sum(r["status"] == "low" for r in rows_all),
            "showing": min(limit, len(rows_all)), "rows": rows_all[:limit]}


@mcp.tool()
def list_low_stock(branch_code: str, limit: int = 15) -> dict:
    """READ-ONLY. Use when someone asks what is running out, "ubos na", or needs
    restocking at ONE branch ("anong ubos na sa Ortigas?") — including from
    Telegram. Lists products at that branch with on_hand <= reorder_point,
    out-of-stock first, then the lowest days of cover. Each row: sku, product,
    unit, retail_price, on_hand, reorder_point, avg_daily_sales, days_of_cover,
    nearest_expiry_date, status. For a fix (transfer from another branch) follow
    up with find_stock_imbalances. Changes nothing."""
    b = branch_row(branch_code)
    if not b:
        return err(f"Unknown branch code '{branch_code}'. Valid codes: {valid_codes()}")
    limit = max(1, min(int(limit), 50))
    rows_all = _stock_rows("i.branch_id = ? AND i.on_hand <= i.reorder_point", (b["id"],))
    return {"as_of": today(), "branch": b["code"], "name": b["name"], "low_or_out_total": len(rows_all),
            "out_of_stock": sum(r["status"] == "out of stock" for r in rows_all),
            "showing": min(limit, len(rows_all)), "rows": rows_all[:limit]}


# ================================================================ ACT tools
def _transfer_check(sku: str, from_branch: str, to_branch: str, qty: int) -> dict:
    p, src, dst = product_row(sku), branch_row(from_branch), branch_row(to_branch)
    if not p:
        return err(f"Unknown SKU '{sku}'")
    if not src or not dst:
        return err(f"Unknown branch code. Valid codes: {valid_codes()}")
    if src["id"] == dst["id"]:
        return err("from_branch and to_branch must differ")
    if not isinstance(qty, int) or qty <= 0:
        return err("qty must be a positive integer")
    s_inv, d_inv = inv_row(src["id"], p["id"]), inv_row(dst["id"], p["id"])
    if not s_inv or not d_inv:
        return err(f"{p['sku']} is not stocked at both {src['code']} and {dst['code']}")
    if qty > s_inv["on_hand"]:
        return err(f"qty {qty} is larger than donor stock ({src['code']} has {s_inv['on_hand']})")
    return {"ok": True, "p": p, "src": src, "dst": dst, "s_inv": s_inv, "d_inv": d_inv}


@mcp.tool()
def transfer_stock(sku: str, from_branch: str, to_branch: str, qty: int, reasoning: str, evidence: str) -> dict:
    """ACTION: move `qty` units of `sku` from a donor branch to a branch that ran
    out (use rows from find_stock_imbalances). `reasoning` = one sentence why;
    `evidence` = the exact numbers you relied on. Risk policy (enforced here):
    auto-applied when qty <= 100 AND the donor keeps >= 14 days of cover;
    otherwise held as pending_approval. Writes the ledger row before applying.
    Return value includes `directive`: paste it verbatim as its own paragraph."""
    ensure_tables()
    c = _transfer_check(sku, from_branch, to_branch, qty)
    if not c["ok"]:
        return c
    p, src, dst, s_inv, d_inv = c["p"], c["src"], c["dst"], c["s_inv"], c["d_inv"]
    target = f"{p['sku']}:{src['code']}->{dst['code']}"
    dup = existing_live_action("transfer_stock", target)
    if dup:
        return err(f"Already handled by action #{dup['id']} ({dup['status']}); nothing written")
    before_cover = cover_days(s_inv["on_hand"], s_inv["avg_daily_sales"])
    after_cover = cover_days(s_inv["on_hand"] - qty, s_inv["avg_daily_sales"])
    low = qty <= TRANSFER_AUTO_MAX_QTY and (after_cover is None or after_cover >= TRANSFER_MIN_DONOR_COVER)
    facts = {"to_on_hand": d_inv["on_hand"], "to_daily_sales": round(d_inv["avg_daily_sales"], 1),
             "donor_on_hand": s_inv["on_hand"], "donor_days_cover": before_cover,
             "donor_days_cover_after": after_cover, "qty": qty}
    def d(x: float | None) -> str:
        return "n/a" if x is None else f"{x:.0f}"
    line = (f"{dst['code']} {d_inv['on_hand']} on hand, sells {d_inv['avg_daily_sales']:.1f}/day; "
            f"{src['code']} {s_inv['on_hand']} on hand = {d(before_cover)} days cover → {d(after_cover)} after")
    cover_gap = d_inv["avg_daily_sales"] * 14  # two weeks of sales the empty branch can't make
    conf = 0.92 if (before_cover or 0) >= 90 else 0.86 if (before_cover or 0) >= 60 else 0.75
    con = connect_rw()
    try:
        a = record(con, action_type="transfer_stock", target=target,
                   summary=f"Move {qty} × {p['name']} {src['code']} → {dst['code']}", facts=facts, evidence_line=line,
                   agent_evidence=evidence, reasoning=reasoning, confidence=conf, risk="low" if low else "high",
                   payload={"sku": p["sku"], "from_branch": src["code"], "to_branch": dst["code"], "qty": qty},
                   php_impact=min(qty, cover_gap) * p["retail_price"])
        if a["status"] == "applied":
            _apply_transfer(con, p["id"], src["id"], dst["id"], qty)
        con.commit()
    finally:
        con.close()
    return finish(a, {"risk_rule": f"auto if qty <= {TRANSFER_AUTO_MAX_QTY} and donor keeps >= "
                                   f"{TRANSFER_MIN_DONOR_COVER} days cover"})


def _apply_transfer(con: sqlite3.Connection, product_id: int, src_id: int, dst_id: int, qty: int) -> None:
    cur = con.execute("UPDATE inventory SET on_hand = on_hand - ? WHERE branch_id = ? AND product_id = ? AND on_hand >= ?",
                      (qty, src_id, product_id, qty))
    if cur.rowcount != 1:
        raise ValueError("donor no longer has enough stock")
    con.execute("UPDATE inventory SET on_hand = on_hand + ?, last_restocked_at = ? WHERE branch_id = ? AND product_id = ?",
                (qty, now(), dst_id, product_id))


@mcp.tool()
def markdown_expiring(sku: str, branch_code: str, pct: int, reasoning: str, evidence: str) -> dict:
    """ACTION: put a 2-day markdown of `pct`% on perishable stock that expires
    soon (use rows from find_expiring_stock and its suggested_markdown_pct).
    `reasoning` = one sentence why; `evidence` = the exact numbers you used.
    Risk policy (enforced here): pct <= 30 is auto-applied; pct > 30 waits for
    approval. Writes the ledger row before applying. Return value includes
    `directive`: paste it verbatim as its own paragraph."""
    ensure_tables()
    p, b = product_row(sku), branch_row(branch_code)
    if not p:
        return err(f"Unknown SKU '{sku}'")
    if not b:
        return err(f"Unknown branch code '{branch_code}'. Valid codes: {valid_codes()}")
    if not p["is_perishable"]:
        return err(f"{p['sku']} is not perishable")
    if not isinstance(pct, int) or not 5 <= pct <= 70:
        return err("pct must be an integer between 5 and 70")
    inv = inv_row(b["id"], p["id"])
    if not inv or inv["on_hand"] <= 0:
        return err(f"No stock of {p['sku']} at {b['code']}")
    if not inv["nearest_expiry_date"]:
        return err(f"{p['sku']} at {b['code']} has no expiry date on record")
    target = f"{p['sku']}@{b['code']}"
    dup = existing_live_action("markdown", target)
    if dup:
        return err(f"Already handled by action #{dup['id']} ({dup['status']}); nothing written")
    t = today()
    days_left = max(1, (datetime.fromisoformat(inv["nearest_expiry_date"]) - datetime.fromisoformat(t)).days + 1)
    suggested, ratio = _markdown_pct(inv["on_hand"], inv["avg_daily_sales"], days_left)
    value = inv["on_hand"] * p["cost_price"]
    facts = {"on_hand": inv["on_hand"], "expires": inv["nearest_expiry_date"], "selling_days_left": days_left,
             "avg_daily_sales": round(inv["avg_daily_sales"], 1), "value_at_cost_php": round(value),
             "suggested_pct": suggested}
    line = (f"{inv['on_hand']} units worth ₱{value:,.0f} at cost expire {inv['nearest_expiry_date']}; "
            f"sells {inv['avg_daily_sales']:.1f}/day ({ratio}× too slow)")
    conf = 0.9 if pct == suggested else 0.8 if abs(pct - suggested) <= 10 else 0.65
    ends = (datetime.fromisoformat(t) + timedelta(days=2)).date().isoformat()
    con = connect_rw()
    try:
        a = record(con, action_type="markdown", target=target,
                   summary=f"{pct}% off {p['name']} at {b['code']} until {ends}", facts=facts, evidence_line=line,
                   agent_evidence=evidence, reasoning=reasoning, confidence=conf,
                   risk="low" if pct <= MARKDOWN_AUTO_MAX_PCT else "high",
                   payload={"sku": p["sku"], "branch_code": b["code"], "pct": pct, "starts_on": t, "ends_on": ends},
                   php_impact=value)
        if a["status"] == "applied":
            _apply_markdown(con, b["id"], p["id"], pct, t, ends, a["id"])
        con.commit()
    finally:
        con.close()
    return finish(a, {"suggested_pct": suggested, "risk_rule": f"auto if pct <= {MARKDOWN_AUTO_MAX_PCT}"})


def _apply_markdown(con, branch_id, product_id, pct, starts, ends, action_id) -> None:
    con.execute("INSERT INTO markdowns (branch_id, product_id, pct, starts_on, ends_on, action_id) VALUES (?,?,?,?,?,?)",
                (branch_id, product_id, pct, starts, ends, action_id))


@mcp.tool()
def cancel_purchase_order(po_number: str, reasoning: str, evidence: str) -> dict:
    """ACTION: cancel a duplicate purchase order (use duplicate_po from
    find_duplicate_purchase_orders, never the original). `reasoning` = one
    sentence why; `evidence` = the exact numbers you used. Risk policy: PO
    cancellations ALWAYS wait for human approval — this records the request as
    pending_approval and changes nothing until decide_action approves it.
    Return value includes `directive`: paste it verbatim as its own paragraph."""
    ensure_tables()
    po = query("""SELECT po.*, b.code AS branch, p.name AS product, s.name AS supplier FROM purchase_orders po
                  JOIN branches b ON b.id = po.branch_id JOIN products p ON p.id = po.product_id
                  JOIN suppliers s ON s.id = po.supplier_id WHERE po.po_number = ?""", ((po_number or "").strip().upper(),))
    if not po:
        return err(f"Unknown PO '{po_number}'")
    po = po[0]
    if po["status"] not in OPEN_PO:
        return err(f"{po['po_number']} is already {po['status']}; only pending / in_transit POs can be cancelled")
    dup = existing_live_action("cancel_po", po["po_number"])
    if dup:
        return err(f"Already handled by action #{dup['id']} ({dup['status']}); nothing written")
    twin = next((d for d in find_duplicate_purchase_orders()["rows"] if d["duplicate_po"] == po["po_number"]), None)
    value = po["quantity"] * po["unit_cost"]
    facts = {"po": po["po_number"], "status": po["status"], "quantity": po["quantity"], "value_php": round(value),
             "original_po": twin["original_po"] if twin else None,
             "hours_after_original": twin["hours_after_original"] if twin else None}
    line = (f"Same {po['supplier']} order of {po['quantity']} × {po['product']} for {po['branch']} as "
            f"{twin['original_po']}, placed {twin['hours_after_original']:.0f}h later; ₱{value:,.0f}"
            if twin else f"{po['quantity']} × {po['product']} for {po['branch']}, ₱{value:,.0f}; no matching original found")
    conf = (0.95 if twin["hours_after_original"] <= 24 else 0.88) if twin else 0.5
    con = connect_rw()
    try:
        a = record(con, action_type="cancel_po", target=po["po_number"],
                   summary=f"Cancel {po['po_number']} (duplicate of {twin['original_po']})" if twin
                   else f"Cancel {po['po_number']}", facts=facts, evidence_line=line, agent_evidence=evidence,
                   reasoning=reasoning, confidence=conf, risk="high", payload={"po_number": po["po_number"]},
                   php_impact=value)
        con.commit()
    finally:
        con.close()
    return finish(a, {"risk_rule": "PO cancellations always need human approval"})


def _apply_cancel(con, po_number: str, action_id: int) -> None:
    cur = con.execute(f"""UPDATE purchase_orders SET status = 'cancelled',
                          notes = trim(coalesce(notes, '') || ' [Cancelled by Suki Autopilot, action #{action_id}: duplicate]')
                          WHERE po_number = ? AND status IN ('pending','in_transit')""", (po_number,))
    if cur.rowcount != 1:
        raise ValueError(f"{po_number} is no longer open")


# ============================================================ LEDGER tools
def _ledger_rows(status: str | None, limit: int) -> list[dict]:
    ensure_tables()
    sql = "SELECT * FROM agent_actions"
    params: tuple = ()
    if status:
        sql += " WHERE status = ?"
        params = (status,)
    out = []
    for r in query(sql + " ORDER BY id DESC LIMIT ?", params + (limit,)):
        ev = json.loads(r["evidence_json"])
        r["evidence_line"] = ev.get("line", "")
        r["evidence"] = ev
        r["payload"] = json.loads(r.pop("payload_json"))
        r.pop("evidence_json")
        r["directive"] = directive(r)
        out.append(r)
    return out


@mcp.tool()
def list_actions(status: str | None = None, limit: int = 30) -> dict:
    """The evidence ledger, newest first: every action Suki Autopilot took or
    proposed, with its evidence, reasoning, confidence, risk and status
    (applied | pending_approval | approved | rejected), plus totals. Each row has
    a ready `directive` to render it as a card."""
    ensure_tables()
    tot = query("""SELECT sum(status IN ('applied','approved')) AS done, sum(status = 'pending_approval') AS pending,
                          sum(status = 'rejected') AS rejected,
                          coalesce(sum(CASE WHEN status IN ('applied','approved') THEN php_impact END), 0) AS php
                   FROM agent_actions""")[0]
    return {"totals": {"applied": tot["done"] or 0, "pending": tot["pending"] or 0, "rejected": tot["rejected"] or 0,
                       "php_protected": round(tot["php"])},
            "actions": _ledger_rows(status, limit)}


@mcp.tool()
def decide_action(action_id: int, approve: bool) -> dict:
    """A human's decision on a pending action ("approve action #12" / "reject
    action #12"). approve=True applies the stored payload and marks it
    `approved`; approve=False marks it `rejected` and changes nothing else.
    Only works on actions in pending_approval. Never call this unless the user
    explicitly approved or rejected that action number."""
    ensure_tables()
    r = query("SELECT * FROM agent_actions WHERE id = ?", (action_id,))
    if not r:
        return err(f"No action #{action_id}")
    a = r[0]
    if a["status"] != "pending_approval":
        return err(f"Action #{action_id} is {a['status']}, not pending_approval")
    payload = json.loads(a["payload_json"])
    con = connect_rw()
    try:
        if approve:
            try:
                if a["action_type"] == "transfer_stock":
                    c = _transfer_check(payload["sku"], payload["from_branch"], payload["to_branch"], payload["qty"])
                    if not c["ok"]:
                        return c
                    _apply_transfer(con, c["p"]["id"], c["src"]["id"], c["dst"]["id"], payload["qty"])
                elif a["action_type"] == "markdown":
                    p, b = product_row(payload["sku"]), branch_row(payload["branch_code"])
                    if not p or not b:
                        return err("Stored markdown payload no longer matches a product/branch")
                    _apply_markdown(con, b["id"], p["id"], payload["pct"], payload["starts_on"], payload["ends_on"], a["id"])
                elif a["action_type"] == "cancel_po":
                    _apply_cancel(con, payload["po_number"], a["id"])
            except ValueError as e:
                con.rollback()
                return err(f"Could not apply action #{action_id}: {e}")
        new = "approved" if approve else "rejected"
        con.execute("UPDATE agent_actions SET status = ?, decided_at = ? WHERE id = ?", (new, now(), action_id))
        con.commit()
    finally:
        con.close()
    row = _ledger_rows(None, 10_000)
    row = next(x for x in row if x["id"] == action_id)
    out = {"ok": True, "action_id": action_id, "status": new, "summary": row["summary"],
           "directive": row["directive"]}
    out["public_dashboard"] = sync_public()["neon"]
    if a["action_type"] == "cancel_po" and approve:
        out["po_status_now"] = query("SELECT status FROM purchase_orders WHERE po_number = ?",
                                     (payload["po_number"],))[0]["status"]
    return out


ensure_tables()

if __name__ == "__main__":
    import threading
    threading.Thread(target=sync_public, daemon=True).start()  # mirror current state (e.g. after seed.py)
    mcp.run()
