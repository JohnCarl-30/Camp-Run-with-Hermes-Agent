# Bantay — Suki Mart's branch watchdog

> Dashboards show one department at a time. **Bantay connects the dots across
> departments to find *why* a branch is failing, fixes what it can, and logs the
> evidence behind every action.**

Track: Business Operations + Customer Experience.

## The problem (found in the data)

Kapitolyo (KPT) went from **3.78★ to 2.33★** in 35 days — the worst drop of all
12 branches. No single dashboard explains it, but the data across departments does:

| Department | Signal (from `store.db`) |
|---|---|
| Reviews | Recent complaints: stock (7), staff (6), cleanliness (6), freshness (3); 25 negative reviews never answered |
| Staffing | Fri–Sun evenings staffed at **59%** of target; Sun 19:00 at 52%; two pickers miss ~1 in 4 shifts |
| Roster | Next 14 days: **35 people short**; Fri–Sun 19:00 blocks have 3 of 6 people rostered |
| Support | 7 tickets never answered, oldest **32 days** |

→ **Likely root cause:** the branch is short-handed when it's busiest, and
service quality collapses in exactly those hours.

## Architecture

```
Bantay pane / ⌘K  ──▶  bantay-branch-doctor skill  ──▶  bantay MCP server  ──▶  store.db
  ▲                                                                            │
  └──── ::bantay-branch / ::bantay-action cards render in chat + feed the pane ◀┘
```

- **Layer 1 — `mcp-server/server.py` (`bantay`, 12 tools)**
  - Watch: `branch_health_scan`, `review_pulse`, `staffing_gaps`,
    `delivery_hotspots`, `forgotten_tickets`, `stock_rebalance_options`
  - Act: `escalate_ticket`, `reply_to_review`, `request_shift_cover`,
    `create_stock_transfer` — every action is written to the `bantay_actions`
    evidence ledger
  - Prove: `action_ledger`
- **Layer 2 — `skills/bantay-branch-doctor`**: morning sweep → diagnose (5 tools,
  cross-department decision rules) → propose ≤5 fixes → act only after approval.
- **Layer 3 — `desktop-plugin/bantay-panel`**: live branch board (health dots +
  scores), Diagnose / Approve fixes buttons, action feed, ⌘K commands, and two
  transcript directives that render native cards inside the chat.

## Setup

```bash
hermes mcp add bantay --command uv --args run "$(pwd)/mcp-server/server.py"
hermes mcp test bantay
cp -r skills/bantay-branch-doctor ~/.hermes/skills/
cp -r desktop-plugin/bantay-panel ~/.hermes/desktop-plugins/
# Hermes Desktop: ⌘K → "Reload desktop plugins", then start a NEW chat
```

Reset the data before a demo: `python data/seed.py` (or `git checkout data/store.db`).

## 5-minute demo script

1. **Problem (45s):** "12 branches, 18 tables, and every team watches its own
   dashboard. Kapitolyo lost 1.45 stars in a month — who connects the dots?"
2. **Sweep (60s):** Bantay pane → **Run morning sweep**. Cards render in chat;
   the pane fills with live health scores. KPT, CUB, ALB, ORT are red.
3. **Diagnose (90s):** click KPT → **Diagnose**. Five tools across five
   departments → cause chain + evidence + proposed fixes.
4. **Act (60s):** **Approve fixes** → 7 tickets escalated, 3 Taglish review
   replies, 3 shift-cover requests. Action cards appear in chat and the pane.
5. **Proof (30s):** **Evidence ledger** → every action, and exactly what it was
   based on.
