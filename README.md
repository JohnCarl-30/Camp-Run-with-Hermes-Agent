# Suki Autopilot

> Suki Mart is losing money and customers. Suki Autopilot finds out why, fixes it, and shows the evidence for every decision.

An autonomous operations agent built on [Hermes Agent](https://hermes-agent.nousresearch.com) for the **CAMP / RUN Hermes Agent hackathon** (Avtica × DEVCON Manila, October 2, 2026), **Business Operations** track.

It diagnoses a struggling branch, fixes stock and purchasing problems directly in the data, and proposes staffing, ticket and review fixes. Every action is written to an evidence ledger before anything changes. A risk policy enforced in code decides what applies automatically and what waits for a manager to approve.

**Live dashboard:** https://suki-autopilot.vercel.app (read-only, phone-friendly)

---

## The problem (measured on a fresh `python data/seed.py`)

| Problem | Evidence in the data |
|---|---|
| **Kapitolyo (KPT) is declining** | Health 26/100, worst of 12 branches. Rating fell from 3.78 to 2.33 in 35 days. 46 missed shifts in 30 days (7.8%, about double the chain average). Its 1–2★ reviews are mostly about stock, staff and cleanliness. 11 regulars stopped ordering. |
| **Stock in the wrong places** | 14 products are out of stock at some branches while another branch holds 60+ days of them: 42 transferable stock-outs. |
| **Food about to spoil** | 224 perishable rows expire within 2 days, ₱674,171 at cost. |
| **Duplicate purchase orders** | 18 duplicate open POs worth ₱170,334. |

The chain of causes at Kapitolyo: not enough staff → empty shelves and messy stores → bad reviews → regulars leave.

## What one autopilot run does

On a fresh dataset, one run takes **28 actions**:

| Action | Count | Status | ₱ impact |
|---|---|---|---|
| Stock transfers | 5 | applied automatically | ₱49,783 |
| Markdowns on expiring food (over 30% off) | 5 | waiting for approval | ₱94,533 |
| Duplicate PO cancellations | 18 | waiting for approval | ₱170,336 |

That's **₱314,652** covered: about ₱50K fixed instantly and ₱265K one Approve away.

## How it works

```
 Hermes Desktop pane ─┐                         ┌─ MCP server: suki-autopilot (12 tools) ─┐
 Telegram bot ────────┼─▶ Skill: suki-autopilot ─┤                                         ├─▶ data/store.db
 Natural prompts ─────┘    (one playbook)        └─ MCP server: bantay (12 tools) ─────────┘   (source of truth)
                                                                    │
                                    after every action: neon_sync.py ─▶ Neon Postgres ─▶ Vercel site (read-only)
```

### Layer 1 · Two MCP servers we built (`mcp-server/`)

**`suki-autopilot`** (`suki_autopilot.py`, scoring in `suki_health.py`):

| Group | Tools |
|---|---|
| Watch and diagnose | `get_branch_health`, `diagnose_branch` |
| Find problems | `find_stock_imbalances`, `find_expiring_stock`, `find_duplicate_purchase_orders` |
| Check products (also used from Telegram) | `check_product_stock`, `list_low_stock` |
| Act | `transfer_stock`, `markdown_expiring`, `cancel_purchase_order` |
| Evidence and approval | `list_actions`, `decide_action` |

**`bantay`** (`server.py`): people and customer fixes, including `branch_health_scan`, `staffing_gaps`, `delivery_hotspots`, `review_pulse`, `forgotten_tickets`, `stock_rebalance_options`, `request_shift_cover`, `escalate_ticket`, `reply_to_review`, `create_stock_transfer` and `action_ledger`.

Every tool answers one business question. There is no generic "run any SQL" tool.

### Risk policy (enforced in code, not in the prompt)

| Action | Applies on its own when | Otherwise |
|---|---|---|
| `transfer_stock` | 100 units or fewer, and the donor keeps 14+ days of cover | waits for approval |
| `markdown_expiring` | 30% off or less | waits for approval |
| `cancel_purchase_order` | never | always waits for approval |

Each action validates its input, writes nothing on an error, refuses to repeat an action that is already live, and records its evidence before applying the change.

### Layer 2 · Skill (`skills/suki-autopilot/SKILL.md`)

One playbook that chains both MCP servers:

- **Diagnose:** "bakit bumababa ang Kapitolyo?" → branch health → diagnosis → chain of causes with numbers → staffing, ticket and review fixes proposed for approval.
- **Run autopilot:** transfers, markdowns, duplicate cancellations and escalation of old tickets, with evidence for each.
- **Approve or reject:** "approve action 7" / "i-approve mo yung #7".
- **Check products:** "magkano stock ng Jasmine Rice sa Kapitolyo?", "anong ubos na sa Ortigas?".

It understands casual English, Tagalog and Taglish (it loaded for 7 of 7 natural test prompts), resolves branch names in any form, and replies in the user's language. In Desktop it shows action cards; on Telegram it replies in short plain text.

### Layer 3 · Hermes Desktop plugin (`desktop-plugin/suki-autopilot/`)

A right-hand pane with branch health, a **Run Autopilot** button, a live action feed with **Approve / Reject**, ⌘K commands, action cards inside the chat, and a status-bar badge. `dashboard/plugin_api.py` is a read-only backend for the pane.

### Telegram

The same Hermes agent, skill and tools answer on Telegram, so a manager can check stock, diagnose a branch or approve an action from their phone.

### Public live dashboard (`web/`)

`web/index.html` polls `web/api/snapshot.js` every 4 seconds. The API reads Neon with the SELECT-only role `suki_web`. After every action, the MCP servers mirror the ledger, branch health (with per-branch detail) and Bantay's fixes to Neon through `mcp-server/neon_sync.py`. The page shows totals (₱ protected and ₱ waiting for approval), branch health with a tap-to-expand diagnosis, and the evidence feed, where cards flash when they're approved. It has dark mode, a stale-data warning and a QR code. Approvals stay in Hermes; the website is read-only.

## Setup

Requirements: Hermes Agent and Hermes Desktop, `uv`, and a model provider (`hermes model`).

```bash
# from the repo root
python data/seed.py                                   # fresh dataset

# MCP servers
hermes mcp add suki-autopilot --command uv --args run "$PWD/mcp-server/suki_autopilot.py"
hermes mcp add bantay --command uv --args run "$PWD/mcp-server/server.py"
hermes mcp test suki-autopilot                       # 12 tools
hermes mcp test bantay                               # 12 tools

# Skill
cp -R skills/suki-autopilot ~/.hermes/skills/

# Desktop plugin
mkdir -p ~/.hermes/plugins/suki-autopilot
cp -R desktop-plugin/suki-autopilot/{dashboard,desktop} ~/.hermes/plugins/suki-autopilot/
hermes config set plugins.enabled '["suki-autopilot"]'
```

Restart Hermes Desktop, enable **Suki Autopilot** under **Capabilities → Plugins**, then run ⌘K → **Reload desktop plugins**. `plugin_api.py` finds the database through the `REPO` constant at the top of the file; update it if you move the repo.

**Live dashboard (optional).** Put the Neon connection string in `mcp-server/.env` as `SUKI_DATABASE_URL=...` (gitignored). Without it, the mirror is skipped and everything else still works. Deploy `web/` to Vercel with `DATABASE_URL` set to a SELECT-only role.

**Telegram (optional).** Run `hermes gateway setup`, choose Telegram, and lock the bot to your own user ID.

## Demo (5 minutes)

1. **Problem:** ₱674K of food expiring, 14 products out of stock while overstocked elsewhere, Kapitolyo's rating 3.78 → 2.33.
2. **Diagnose:** click **Diagnose** on KPT. Hermes explains the chain of causes with numbers and proposes staffing, ticket and review fixes.
3. **Run Autopilot:** cards appear in the pane, the chat and the live site. Transfers apply on their own; deep markdowns and PO cancellations wait.
4. **Approve:** click **Approve** on a cancellation (or send "approve action N" on Telegram). The card flips on every phone.
5. **Close:** "28 actions, ₱315K covered, and every decision has its evidence."

Reset before demoing: `python data/seed.py`, then restart Hermes so the live site shows the fresh state.

## Repository layout

```
data/            Suki Mart sandbox (store.db, seed.py, SCHEMA.md) from the starter kit
mcp-server/      suki_autopilot.py, suki_health.py, server.py (bantay), neon_sync.py
skills/          suki-autopilot (main skill), bantay-branch-doctor (earlier build)
desktop-plugin/  suki-autopilot pane and backend, bantay-panel (earlier build)
web/             public live dashboard (index.html, api/snapshot.js)
scripts/         export_snapshot.py
docs/            SUKI_AUTOPILOT.md, BANTAY.md, JUDGING.md
```

## The sandbox

Suki Mart is a fictional grocery and delivery chain with 12 branches across Metro Manila, in one SQLite file (`data/store.db`, about 230,000 rows across 18 tables, six months of history). "Today" inside the data is 2026-09-30. `python data/seed.py` rebuilds the identical database. Full column reference: [data/SCHEMA.md](data/SCHEMA.md).

## Credits and license

Built on the official CAMP / RUN starter kit ([pabilandokarenpv/Camp-Run-with-Hermes-Agent](https://github.com/pabilandokarenpv/Camp-Run-with-Hermes-Agent)). Powered by [Hermes Agent](https://hermes-agent.nousresearch.com) by Nous Research.

[MIT](LICENSE). Suki Mart and every person, business and record in the dataset are fictional.
