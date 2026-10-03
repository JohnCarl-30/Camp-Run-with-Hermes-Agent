"""Mirror the Suki Autopilot state to Neon Postgres for the public website.

SQLite (data/store.db) stays the source of truth: every tool reads and writes it
locally. After each change the MCP server calls `sync(snapshot)`, which
replaces the Neon copy in one transaction:
  agent_actions  (the evidence ledger)
  branch_health  (12 branches, same scoring as the pane; `detail` jsonb = drill-down)
  sync_state     (as_of + headline problem numbers)
  bantay_actions (people / customer fixes from the Bantay MCP server)
The Vercel site reads only these tables, through the SELECT-only role `suki_web`.

The connection string comes from SUKI_DATABASE_URL (environment variable, or
mcp-server/.env, which is gitignored). With no URL, sync is a silent no-op, so
the demo still works offline.

Schema additions (bantay_actions, branch_health.detail) are created once per
process, idempotently, in their own transaction, and the new table is granted
to the website's read-only role (SUKI_WEB_ROLE, default `suki_web`: it holds
per-table grants only, so a new table is invisible to it without this). If that
step fails, the original three tables still sync exactly as before.
"""
from __future__ import annotations

import json
import os
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
WEB_ROLE = os.environ.get("SUKI_WEB_ROLE", "suki_web")
_LOCK_KEY = 4_815_162_342          # pg advisory lock: syncs from both MCP servers never interleave
_lock = threading.Lock()           # ...and syncs from threads of one process run one at a time
_schema_ok = False                 # bantay_actions + branch_health.detail exist and are granted


def database_url() -> str | None:
    url = os.environ.get("SUKI_DATABASE_URL")
    if url:
        return url
    path = os.path.join(_HERE, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            k, _, v = line.strip().partition("=")
            if k == "SUKI_DATABASE_URL" and v:
                return v.strip().strip('"')
    return None


def _ensure_schema(con) -> bool:
    """Additive schema for the drill-down and the Bantay feed. Own transaction; never raises."""
    global _schema_ok
    if _schema_ok:
        return True
    from psycopg import sql

    try:
        with con.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
            cur.execute("""CREATE TABLE IF NOT EXISTS bantay_actions (
                             id integer PRIMARY KEY, created_at text, action_type text, branch_code text,
                             target text, summary text, evidence text, status text)""")
            cur.execute("ALTER TABLE branch_health ADD COLUMN IF NOT EXISTS detail jsonb")
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (WEB_ROLE,))
            if cur.fetchone():
                cur.execute(sql.SQL("GRANT SELECT ON TABLE bantay_actions TO {}").format(sql.Identifier(WEB_ROLE)))
        con.commit()
        _schema_ok = True
    except Exception:
        con.rollback()
    return _schema_ok


def sync(snapshot: dict) -> dict:
    """Replace the Neon mirror with `snapshot`. Never raises; returns a status dict."""
    url = database_url()
    if not url:
        return {"neon": "skipped (no SUKI_DATABASE_URL)"}
    t0 = time.time()
    try:
        import psycopg  # imported lazily so the server runs without it

        with _lock, psycopg.connect(url, connect_timeout=6, autocommit=False) as con:
            extra = _ensure_schema(con)
            details = snapshot.get("branch_detail") or {}
            with con.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
                cur.execute("DELETE FROM agent_actions")
                if snapshot["actions"]:
                    cur.executemany(
                        """INSERT INTO agent_actions (id, created_at, action_type, target, summary, evidence_line,
                                                      reasoning, confidence, risk, status, decided_at, php_impact)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        [(a["id"], a["created_at"], a["action_type"], a["target"], a["summary"], a["evidence_line"],
                          a["reasoning"], a["confidence"], a["risk"], a["status"], a["decided_at"], a["php_impact"])
                         for a in snapshot["actions"]])
                cur.execute("DELETE FROM branch_health")
                cols = ("branch, name, health_score, status, rating_recent, rating_before, "
                        "late_delivery_pct_30d, missed_shift_pct_30d, unanswered_tickets, worst_metric")
                rows = [(b["branch"], b["name"], b["health_score"], b["status"], b["rating_recent"], b["rating_before"],
                         b["late_delivery_pct_30d"], b["missed_shift_pct_30d"], b["unanswered_tickets"], b["worst_metric"])
                        for b in snapshot["branches"]]
                if extra:
                    cur.executemany(f"INSERT INTO branch_health ({cols}, detail) VALUES ({', '.join(['%s'] * 11)})",
                                    [r + (json.dumps(details[r[0]]) if r[0] in details else None,) for r in rows])
                else:
                    cur.executemany(f"INSERT INTO branch_health ({cols}) VALUES ({', '.join(['%s'] * 10)})", rows)
                if extra and snapshot.get("bantay") is not None:
                    cur.execute("DELETE FROM bantay_actions")
                    if snapshot["bantay"]:
                        cur.executemany(
                            """INSERT INTO bantay_actions (id, created_at, action_type, branch_code, target, summary,
                                                           evidence, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                            [(r["id"], r["created_at"], r["action_type"], r["branch_code"], r["target"], r["summary"],
                              r["evidence"], r["status"]) for r in snapshot["bantay"]])
                cur.execute(
                    """INSERT INTO sync_state (id, as_of, synced_at, problems) VALUES (1, %s, now(), %s)
                       ON CONFLICT (id) DO UPDATE SET as_of = EXCLUDED.as_of, synced_at = now(), problems = EXCLUDED.problems""",
                    (snapshot["as_of"], json.dumps(snapshot["problems"])))
            con.commit()
        out = {"neon": "synced", "ms": round((time.time() - t0) * 1000)}
        if not extra:
            out["note"] = "drill-down / Bantay tables not available; core tables synced"
        return out
    except Exception as e:  # the local action already succeeded; report, don't fail it
        return {"neon": f"sync failed: {type(e).__name__}: {str(e)[:160]}"}
