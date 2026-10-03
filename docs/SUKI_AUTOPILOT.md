# Suki Autopilot

> Suki Mart is losing money and customers. Suki Autopilot finds out why, fixes it, and shows the evidence for every decision.

This is an autonomous operations agent for the **Business Operations** track. It diagnoses a struggling branch and then acts directly on stock and purchasing in `data/store.db`. Every action is written to an evidence ledger before the change is applied. A risk policy, enforced in code, decides which actions run automatically and which wait for a human to click **Approve**.

| Layer | Path | What it is |
|---|---|---|
| 1 · MCP server | `mcp-server/suki_autopilot.py` (+ `suki_health.py`) | 12 domain tools: diagnose, find, check products, act, ledger, decide |
| 2 · Skill | `skills/suki-autopilot/SKILL.md` | Mode A: diagnose · Mode B: run autopilot · Mode C: approve/reject |
| 3 · Desktop plugin | `desktop-plugin/suki-autopilot/` | right pane, ⌘K commands, in-chat `::suki-action` cards, status-bar badge, read-only `plugin_api.py` |

The Bantay MCP server (`mcp-server/server.py`, 12 tools) is part of the entry: the `suki-autopilot` skill calls its staffing, ticket and review tools after a diagnosis. The older `bantay-branch-doctor` skill and `bantay-panel` plugin are kept for reference but not used.

## Problems it solves (measured on a fresh `seed.py`)

| Problem | Evidence |
|---|---|
| Kapitolyo (KPT) is declining | Health score 26/100, the worst of 12. Rating fell from 3.78 to 2.33 (last 35 days). 46 missed shifts in 30 days (7.8%, vs 4.0% chain average). |
| Stock imbalance | 14 products across 168 branch pairs have 0 on hand at one branch while another branch has 60+ days of cover. That gives 42 transferable stock-outs. |
| Expiring perishables | 224 rows expire within 2 days, ₱674,171 at cost. |
| Duplicate purchase orders | 18 duplicate open POs worth ₱170,334. Each is the same supplier, branch, product and quantity, placed within 48 hours of an earlier PO. The spec's figure of "32 pairs" counted every pairwise match, including received POs. |

## Risk policy (enforced in `suki_autopilot.py`)

| Action | Auto-applied when | Otherwise |
|---|---|---|
| `transfer_stock` | qty ≤ 100 and the donor keeps ≥ 14 days of cover | pending approval |
| `markdown_expiring` | pct ≤ 30 | pending approval |
| `cancel_purchase_order` | never | always pending approval |

Every action tool validates its input and writes nothing on an error. It also refuses to repeat an action that is already live. Each tool returns a ready `directive` string. The skill pastes it unchanged, so the model never writes card numbers itself.

## Setup

```bash
# from the repo root
uv run mcp-server/suki_autopilot.py      # starts and waits; Ctrl+C
hermes mcp add suki-autopilot --command uv --args run "$PWD/mcp-server/suki_autopilot.py"
hermes mcp test suki-autopilot           # 12 tools

cp -R skills/suki-autopilot ~/.hermes/skills/
mkdir -p ~/.hermes/plugins/suki-autopilot
cp -R desktop-plugin/suki-autopilot/{dashboard,desktop} ~/.hermes/plugins/suki-autopilot/
hermes config set plugins.enabled '["suki-autopilot"]'   # lets plugin_api.py load
```

Restart Hermes Desktop so the gateway mounts `/api/plugins/suki-autopilot/` and loads the MCP server. Then enable **Suki Autopilot** under **Capabilities → Plugins**; the desktop half ships turned off. Finally, run ⌘K → **Reload desktop plugins**.

`plugin_api.py` reads the database through the absolute `REPO` constant at the top of the file. Update it if you move the repo.

## Demo (5 min)

Reset first: `python data/seed.py`

1. **Problem (0:30).** ₱674K of food is about to expire. 14 products are out of stock at some branches while overstocked at others. Kapitolyo's rating fell from 3.78 to 2.33.
2. **Diagnose (1:00).** In the pane, click **Diagnose** on KPT. Hermes explains the chain: staffing → stock and cleanliness complaints → rating → lapsed regulars.
3. **Autopilot (1:30).** Click **▶ Run Autopilot**. Cards appear in the chat and the pane, each with its evidence. Transfers apply on their own. Deep markdowns and PO cancellations wait for approval.
4. **Approve (0:30).** Click **Approve** on a cancel-PO card. The card flips to APPROVED and the PO is now `cancelled` in the data.
5. **Close (0:30).** "N actions, ₱X protected, and every decision has its evidence." Next steps: Bagyo Mode, nightly runs, Telegram approvals.

Fallback: if the pane shows **Offline**, the backend isn't mounted. **Run Autopilot**, the branch picker, ⌘K and the chat cards all still work.

## Public live dashboard (Vercel + Neon)

**URL:** https://suki-autopilot.vercel.app

```
Hermes → MCP suki-autopilot → store.db (source of truth)
                     └─ after every action: neon_sync.py → Neon Postgres (project sweet-tooth-89855345)
Vercel: web/index.html polls /api/snapshot every 4 s → web/api/snapshot.js → Neon (read-only role suki_web)
```

- `mcp-server/neon_sync.py` replaces the Neon copy in one transaction: `agent_actions`, `branch_health` and `sync_state`. If the sync fails, the local action still succeeds, and the tool result shows the failure in `public_dashboard`.
- The connection string lives in `mcp-server/.env` as `SUKI_DATABASE_URL` (gitignored). Without it, sync is skipped and everything else still works.
- Vercel reads with the SELECT-only role `suki_web`, set in the `DATABASE_URL` env var on the Vercel project `suki-autopilot`.
- Reset for a demo: run `python data/seed.py`, then restart Hermes. The MCP server mirrors the fresh state on startup, so the site shows 0 actions.
