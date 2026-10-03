"""Suki Autopilot: desktop plugin backend (read-only).

Mounted by Hermes at /api/plugins/suki-autopilot/ when `suki-autopilot` is in
`plugins.enabled`. This router only READS the database. Every write goes
through chat → skill → MCP, so each change shows up in the transcript.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3

from fastapi import APIRouter

# Absolute path to the hackathon repo. Change this if the repo moves.
REPO = "/Users/dyeyyyc/Documents/hermes-hackathon/Camp-Run-with-Hermes-Agent"
DB_PATH = os.path.join(REPO, "data", "store.db")

# Load the ONE shared copy of the health-score SQL from the repo
# (mcp-server/suki_health.py) instead of keeping a duplicate here.
_spec = importlib.util.spec_from_file_location("suki_autopilot_health", os.path.join(REPO, "mcp-server", "suki_health.py"))
health = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(health)  # type: ignore[union-attr]

router = APIRouter()
STATUS_ATTR = {"applied": "applied", "pending_approval": "pending", "approved": "approved", "rejected": "rejected"}


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _has_ledger(con: sqlite3.Connection) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_actions'").fetchone() is not None


@router.get("/ping")
def ping():
    return {"ok": True, "db": os.path.exists(DB_PATH)}


@router.get("/actions")
def actions(limit: int = 30):
    empty = {"actions": [], "totals": {"applied": 0, "pending": 0, "rejected": 0, "php_protected": 0}}
    con = _con()
    try:
        if not _has_ledger(con):  # fresh seed.py: no ledger yet → empty, not a 500
            return empty
        cols = {r[1] for r in con.execute("PRAGMA table_info(agent_actions)")}
        php = "php_impact" if "php_impact" in cols else "0"
        out = []
        for r in con.execute(
            f"""SELECT id, created_at, action_type, target, summary, evidence_json, reasoning, confidence,
                       risk, status, decided_at, {php} AS php_impact
                FROM agent_actions ORDER BY id DESC LIMIT ?""", (max(1, min(limit, 200)),)):
            r = dict(r)
            try:
                r["evidence_line"] = json.loads(r.pop("evidence_json") or "{}").get("line", "")
            except (ValueError, AttributeError):
                r["evidence_line"] = ""
            r["status_short"] = STATUS_ATTR.get(r["status"], r["status"])
            out.append(r)
        t = con.execute(f"""SELECT sum(status IN ('applied','approved')) AS done,
                                   sum(status = 'pending_approval') AS pending, sum(status = 'rejected') AS rejected,
                                   coalesce(sum(CASE WHEN status IN ('applied','approved') THEN {php} END), 0) AS php
                            FROM agent_actions""").fetchone()
        return {"actions": out, "totals": {"applied": t["done"] or 0, "pending": t["pending"] or 0,
                                           "rejected": t["rejected"] or 0, "php_protected": round(t["php"] or 0)}}
    finally:
        con.close()


@router.get("/branches")
def branches():
    con = _con()
    try:
        return {"branches": health.branch_health(con), "as_of": health.sandbox_now(con)}
    finally:
        con.close()
