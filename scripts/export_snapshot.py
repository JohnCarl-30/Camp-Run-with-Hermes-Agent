# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2"]
# ///
"""Export a read-only snapshot of Suki Autopilot for the public website.

    uv run scripts/export_snapshot.py            # writes web/snapshot.json

It reads data/store.db through the same MCP tool functions the agent uses,
so the website shows exactly what Hermes saw. Nothing is written to the DB.
"""
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "mcp-server"))
import suki_autopilot as s  # noqa: E402

health = s.get_branch_health()
kpt = s.diagnose_branch("KPT")
imb = s.find_stock_imbalances(limit=5)
exp = s.find_expiring_stock(limit=5)
dup = s.find_duplicate_purchase_orders()
ledger = s.list_actions(limit=50)

snapshot = {
    "as_of": s.now(),
    "problems": {
        "kpt": {"rating_before": kpt["metrics"]["rating_before"], "rating_recent": kpt["metrics"]["rating_recent"],
                "missed_shifts_30d": kpt["metrics"]["missed_shifts_30d"], "score": kpt["metrics"]["health_score"],
                "top_topics": kpt["top_negative_review_topics"], "lapsed_regulars": kpt["lapsed_regulars"]},
        "stock": {"products": imb["products_affected"], "pairs": imb["empty_donor_pairs"],
                  "fixable": imb["stockouts_with_donor"]},
        "expiring": {"rows": exp["rows_expiring"], "php": exp["total_value_at_cost_php"]},
        "duplicates": {"count": dup["duplicates"], "php": dup["total_value_php"]},
    },
    "branches": [{k: b[k] for k in ("branch", "name", "health_score", "status", "rating_recent", "rating_before",
                                    "late_delivery_pct_30d", "missed_shift_pct_30d", "unanswered_tickets",
                                    "worst_metric")} for b in health],
    "totals": ledger["totals"],
    "actions": [{"id": a["id"], "type": a["action_type"], "status": a["status"], "summary": a["summary"],
                 "evidence": a["evidence_line"], "reasoning": a["reasoning"], "confidence": a["confidence"],
                 "risk": a["risk"], "php": round(a["php_impact"]), "created_at": a["created_at"],
                 "decided_at": a["decided_at"]} for a in ledger["actions"]],
}
out = os.path.join(ROOT, "web", "snapshot.json")
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w") as f:
    json.dump(snapshot, f, indent=1, ensure_ascii=False)
print(f"wrote {out}: {len(snapshot['branches'])} branches, {len(snapshot['actions'])} actions")
