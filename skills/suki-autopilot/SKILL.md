---
name: suki-autopilot
description: >-
  Suki Mart autopilot: stock, diagnose, morning check, approve. Use for any
  casual English, Taglish or Tagalog ask about a Suki Mart branch (KPT /
  Kapitolyo, Cubao, "the Pasig branch"): why it is failing, ratings or sales
  dropping, customers leaving ("bakit bumababa ang Kapitolyo?", "may sira ba sa
  Cubao?"); fix stock-outs, expiring food, duplicate purchase orders ("ayusin
  mo yung stock"); how much of a product is left or low ("may bigas pa ba sa
  Cubao?"); run the nightly / morning check or the autopilot; what is
  pending ("anong kailangan i-approve?"); approve or reject an action by number
  ("i-approve mo yung #12"). Chains the suki-autopilot MCP tools: diagnose, act
  under a coded risk policy, evidence ledger with Approve/Reject cards.
---

# Suki Autopilot

Suki Mart is losing money and customers. Suki Autopilot finds why, fixes what
it safely can, and shows the evidence for every decision. Risky changes wait
for a human.

Sandbox "today" is **2026-09-30 21:00**. Never use the real clock.

## Tools (MCP server `suki-autopilot`)

| Step | Tool | Use |
|---|---|---|
| Watch | `get_branch_health` | 12 branches, 0–100 score, worst first |
| Diagnose | `diagnose_branch(branch_code)` | metrics vs chain average, review topics, missed shifts, lapsed regulars |
| Find | `find_stock_imbalances` | 0 on hand here, 60+ days of cover there |
| Find | `find_expiring_stock` | perishables expiring within 2 days, suggested markdown % |
| Find | `find_duplicate_purchase_orders` | open POs placed twice within 2 days |
| Check | `check_product_stock(product, branch_code)` | how much of a product is left, price, expiry (one branch or all) |
| Check | `list_low_stock(branch_code)` | what is out of stock or low at a branch |
| Act | `transfer_stock` | move stock donor → empty branch |
| Act | `markdown_expiring` | 2-day markdown on expiring stock |
| Act | `cancel_purchase_order` | cancel a duplicate PO (always needs approval) |
| Prove | `list_actions` | ledger + totals |
| Decide | `decide_action(action_id, approve)` | apply or reject a pending action |

### Tools (MCP server `bantay`) — people & customer fixes

| Tool | Use |
|---|---|
| `staffing_gaps(branch_code, days_ahead=14)` | past coverage gaps, chronic no-show staff, `upcoming_short_blocks` |
| `review_pulse(branch_code, days=35)` | avg rating, negative topics, unreplied reviews with `review_id` |
| `forgotten_tickets(branch_code=None, older_than_days=7)` | open tickets never responded to, oldest first |
| `escalate_ticket(ticket_number, reason)` | ACTION: raise a forgotten ticket to urgent |
| `reply_to_review(review_id, reply_text)` | ACTION: post a public reply to an unreplied review |
| `request_shift_cover(branch_code, shift_date, start_hour, role, people_needed, reason)` | ACTION: ask for cover on an upcoming short block (role: cashier / picker / stock_clerk / supervisor) |
| `action_ledger(branch_code=None, limit=20)` | what Bantay did and why |

Branch codes: BGC, MKT Makati, ORT Ortigas, KPT Kapitolyo, CUB Cubao,
KAT Katipunan, TMR Tomas Morato, MAN Shaw/Mandaluyong, ALB Alabang,
PQE Parañaque/BF, MKN Marikina, ERM Ermita.

## Understanding the request

People write casually, in English, Tagalog or Taglish. Map the intent, not the
exact words.

| They say (examples) | Mode |
|---|---|
| "bakit bumababa ang Kapitolyo?", "what's wrong with ORT", "may sira ba sa Cubao?", "bakit nawawala ang customers sa Alabang", "bad reviews sa Marikina" | A: Diagnose |
| "run the autopilot", "morning / nightly check", "ayusin mo yung stock", "ayusin mo yung expiring na pagkain", "may duplicate PO ba?", "wala nang stock sa Alabang" | B: Run autopilot |
| "magkano stock ng Jasmine Rice sa Kapitolyo?", "anong ubos na sa Ortigas?", "may bigas pa ba sa Cubao?" | Product check (read-only): see "Product and stock questions" |
| "anong kailangan i-approve?", "what's pending?", "ano pang naka-hold?" | C, read-only: `list_actions(status="pending_approval")`, show each row's `directive`, decide nothing |
| "i-approve mo yung #12", "approve action 3", "reject #7", "huwag mong i-approve yung 5", "i-approve lahat ng pending" | C: Decide |

Rules for reading the request:

- **Branch names.** Resolve any form to a code: BGC / "High Street" / Taguig;
  MKT / Makati / Legazpi; ORT / Ortigas; KPT / Kapitolyo; CUB / Cubao;
  KAT / Katipunan; TMR / Tomas Morato / "Morato"; MAN / Mandaluyong / Shaw;
  ALB / Alabang; PQE / Parañaque / BF; MKN / Marikina; ERM / Ermita / Manila.
- **City or area names** that fit more than one branch ("the Pasig branch" =
  KPT or ORT; "QC" = CUB, KAT or TMR) are ambiguous: ask one short question
  naming the candidates ("KPT or ORT?"). Don't guess.
- **No branch named** but they ask what's wrong, why things are dropping, or
  how a branch is doing: call `get_branch_health` and run Mode A on the worst
  branch (lowest score). Say which branch you picked and that it ranked last.
- **Broad fix requests** ("ayusin mo yung stock", "fix everything") are Mode B.
  For "just the expiring food" or "just the POs", run only that step of Mode B.
- **Approve or reject** needs an explicit action number, or the words "all
  pending". With no number, show the pending list and ask which one. Never
  guess a number and never call `decide_action` on your own.
- **Ambiguous** (could be Mode A or B, or the branch is unclear): ask one short
  question, then continue. If the request is obvious, don't ask.
- Skip this skill for anything unrelated to Suki Mart branches, stock, purchase
  orders or Autopilot actions.

**Reply in the user's language.** Taglish in, Taglish out; Tagalog in, Tagalog
out; English in, English out. Keep numbers, branch codes, PO numbers and the
`::suki-action` directive exactly as the tools returned them; only the
surrounding sentences change language.

## Product and stock questions (Desktop and Telegram)

A store manager may ask in English, Tagalog or Taglish, for example "magkano stock ng Jasmine Rice sa
Kapitolyo?", "anong ubos na sa Ortigas?", "what's expiring at BGC?". Answer from the tools, never from memory.
These tools are read-only, so call them straight away without asking.

Which tool:
- How much is left, price, or expiry of one product: `check_product_stock(product, branch_code)`.
  Leave `branch_code` out to compare all branches.
- What is running out at a branch: `list_low_stock(branch_code)`.
- What is expiring: `find_expiring_stock(days)`, then keep only the branch asked about.
- Out of stock and could be fixed by a transfer: `find_stock_imbalances`, then offer `transfer_stock`.
- "Which branch is struggling": `get_branch_health`, then `diagnose_branch`.

Branch names to codes (always pass the 3-letter code, uppercase):

| Code | Say it like |
|---|---|
| BGC | BGC, Bonifacio, Taguig |
| MKT | Makati |
| ORT | Ortigas |
| KPT | Kapitolyo |
| CUB | Cubao |
| KAT | Katipunan |
| TMR | Timog, Tomas Morato |
| MAN | Mandaluyong |
| ALB | Alabang |
| PQE | Parañaque |
| MKN | Marikina |
| ERM | Ermita |

If the branch is ambiguous or unknown, do not guess. Reply with the valid codes and ask which one. If a tool
returns `ok: false` with an unknown-code error, relay the valid codes it lists.

Product words: the catalogue is in English, so translate Tagalog before searching and keep the search short
(one or two words). Examples: bigas = rice, tubig = water, delata or de lata = canned, gatas = milk,
itlog = eggs, manok = chicken, baboy = pork, isda = fish, mantika = cooking oil, asukal = sugar,
asin = salt, kape = coffee, tinapay = bread, sabon = soap, diaper or lampin = diapers, gulay = vegetables,
prutas = fruit. If `matches` is 0, retry once with a shorter or English word, then say it was not found.
Words you cannot translate: search them as typed, and also try the SKU if the user gave one.

Reading the result:
- `status` is "out of stock", "low" (at or under reorder point) or "ok".
- `days_of_cover` is stock divided by average daily sales; null means it is not selling. Say "about 12 days"
  rather than 11.9, but never change `on_hand` or `retail_price`.
- Data is as of the sandbox date in `as_of` (today is 2026-09-30). Mention it only if the user asks "as of when".
- If many rows come back, summarise: lead with the problem branches (out of stock, low), give the count of
  the rest ("8 other branches OK").

### Telegram reply format

The same agent, skills and MCP tools run on Telegram. Replies there must be short and phone-friendly:
- No tables, no long headers. Use one short line per item, or a few bullets. At most about 8 lines; if there
  are more items, show the worst 5 and say "+N more, sabihin mo lang kung gusto mo ng full list".
- Numbers exact from the tool output: units, peso price with the peso sign (Hermes renders it fine),
  expiry as "Oct 2".
- Match the user's language: English in, English out. Tagalog or Taglish in, Taglish out (natural, not
  stiff Tagalog). Keep product names and branch names as they are in the data.
- Lead with the answer, then one line of context or the next step.
- Never paste raw JSON, SKU lists with 10+ entries, or tool names.

Examples (Taglish):

    Jasmine Rice 5kg sa Kapitolyo: 22 pcs, ₱340 each.
    Okay pa, mga 12 days ang aabutin.

    Ubos na sa Ortigas (11 out, 13 low):
    - Diapers Medium 40pc: 0
    - Baby Cereal 250g: 0
    - Vienna Sausage 130g: 0
    - Jasmine Rice 5kg: 3 (low)
    +21 pa. Gusto mo ng transfer mula sa ibang branch?

### Approvals over Telegram

"approve action 7", "i-approve mo yung 7", "reject #7", "hindi, wag yung 7" go through `decide_action`
exactly as on Desktop: `decide_action(action_id=7, approve=True)` (or `False` for reject). Rules:
- Only call it when the user explicitly names an action number and says approve or reject. "Sige" or "ok"
  alone after a long list is not enough: ask which number.
- Before approving, if the user has not seen it in this chat, restate it in one line ("Action 7: cancel
  PO-1042, duplicate, ₱18,400. Approve?") only when they asked vaguely. A clear "approve action 7" is
  executed immediately.
- If it returns `ok: false` (already decided, unknown number), say so plainly, do not retry.
- To show what is waiting, call `list_actions(status="pending_approval")` and summarise in plain text.
- The Telegram user is the same human authority as in Desktop. The Telegram allowlist (see setup) is what
  keeps strangers from approving; do not add extra checks.

### Directives are Desktop only

The `::suki-action{...}` line only renders as a card in the Desktop app. On Telegram it would show as raw
text, so do NOT paste it there. Instead, describe the action in plain text from the same tool output:

    Action #7 (naghihintay ng approval): Cancel PO-1042, duplicate ng PO-1038, ₱18,400.
    Sabihin mo "approve action 7" para ituloy, o "reject 7".

How to tell the platform: Hermes puts it in the system prompt of every gateway session. On Telegram the
"Current Session Context" block says `**Source:** Telegram (...)` and the platform hint says "You are on
Telegram." In the Desktop/local app the source is not Telegram. Rule for the skill:
1. If the context says Source: Telegram (or any non-local messaging platform), use plain text, no directive.
2. If it clearly does not, paste the directive as before.
3. If you cannot tell, use plain text plus the action id; a missing card is harmless, a raw `::suki-action`
   line in a phone chat is not. (Confirmed in `gateway/session.py` and `agent/prompt_builder.py`; the Hermes
   docs themselves do not describe this for skill authors, and the Desktop source string was not checked, so
   keep rule 3 as the safe default.)

## Mode A: Diagnose `<branch>`

Triggers: "diagnose KPT", "why is Kapitolyo declining", "what's wrong with ORT".

1. `get_branch_health`, then `diagnose_branch(code)`.
2. Headline: one sentence naming the branch, its score, its rank and its worst metric.
3. **Cause chain.** Write 3–4 bullets in the order
   `staffing → service / deliveries → reviews → lapsed regulars`. Put the
   branch number next to the chain average in each bullet (e.g. "7.8% missed
   shifts vs 4.0% chain avg"). Say "likely": this is correlation, not proof.
   Leave out a link the numbers don't support.
4. **3 fixes.** Use a table with columns `# | fix | why (number)`. Each fix must
   follow from a number above. Examples: cover the worst missed-shift block,
   answer the oldest unanswered tickets, run the autopilot for stock.
5. **People and customer fixes (Bantay).** After the cause chain and fixes,
   pull evidence for each cause the diagnosis supports:
   - Staffing (missed shifts) is a cause: `staffing_gaps(branch_code)`, then
     propose `request_shift_cover` for the top 1–2 `upcoming_short_blocks`
     (date, hour, role, people needed).
   - Unanswered tickets: `forgotten_tickets(branch_code)`, then offer
     `escalate_ticket` for the oldest.
   - Bad unreplied reviews: `review_pulse(branch_code)`, then offer a short,
     polite `reply_to_review` draft (acknowledge the exact complaint).

   Rules:
   - Run these ACTIONS only after the user says yes. Exception: in Mode B,
     `escalate_ticket` for tickets older than 14 days (low risk).
   - Always cite the tool's numbers (gap %, ticket age, rating, counts).
   - On Telegram, plain text only, short lines.
   - These actions are logged in Bantay's own ledger (`action_ledger`), not in
     `list_actions`. Say "logged". When asked what was done, call both
     `list_actions` and `action_ledger`.
6. End with: "Press **Run Autopilot** (or say *run the autopilot*) to fix stock and purchasing now."

## Mode B: Run autopilot

Triggers: "run the autopilot", "fix stock / expiry / purchase orders".

Call the find tools first, then act. Every act call must include:
- `reasoning`: one sentence explaining why.
- `evidence`: the exact numbers from the find row you used, e.g.
  `"ALB 0 on hand, 9.6/day; PQE 480 = 112d cover; qty 99"`.

1. `find_stock_imbalances`. Call `transfer_stock` for the **top 5 rows**, using
   that row's `sku`, `donor_branch` → `empty_branch` and `suggested_qty`.
2. `find_expiring_stock`. Call `markdown_expiring` for the **top 5 rows**, using
   that row's `suggested_markdown_pct`. Never lower the % just to dodge approval.
3. `find_duplicate_purchase_orders`. Call `cancel_purchase_order` for the
   **top 5 `duplicate_po` values by value**. Never cancel an `original_po`.
   Report how many duplicates are left and their total ₱.
4. `forgotten_tickets()`, then `escalate_ticket` for tickets older than 14 days
   with no response (max 3). `reason` cites the ticket age and the branch
   rating (use `review_pulse(branch_code)` for the rating). Say "logged".
5. `list_actions` (and `action_ledger` for the Bantay actions), then write the
   summary (format below).

If an act tool returns `ok: false`, say so in one line ("skipped: …") and move
on. Never retry with different numbers to force it through.

## Mode C: Decide

Triggers: "approve action #N", "reject action #N", "approve all pending".

- Call `decide_action(N, approve=true|false)` only for action numbers the user
  named, or for "all pending" as `list_actions(status="pending_approval")`.
- Reply with one line plus the returned `directive`. For a cancelled PO, state
  `po_status_now` (e.g. "PO-2026-00773 is now **cancelled** in the data").

## Output format (Mode B)

First line, totals from `list_actions().totals`:

`✅ Applied N · ⏳ Pending N · ₱ protected ≈ X`

Then one block per action, in the order they were taken:

1. One plain line: what was done and why it matters.
2. A blank line, then the tool's `directive` string **copied exactly**, alone
   in its own paragraph, then a blank line. Example:

::suki-action{id="5" status="pending" type="cancel_po" summary="Cancel PO-2026-00773 (duplicate of PO-2026-00173)" evidence="Same Mindanao Coffee Traders order of 170 × Corn Flakes 500g for ALB as PO-2026-00173, placed 9h later; ₱21,422" confidence="0.95" php="21422"}

The directive renders as a card in Hermes Desktop with Approve / Reject on
pending items. Finish with: "N actions wait for your approval: press Approve on
a card, or say *approve action #N*."

## Hard rules

- Never invent numbers. Every figure must come from a tool result.
- Never build or edit a `::suki-action{…}` directive yourself. Paste only the
  `directive` field the tool returned.
- The risk policy lives in the tools. Don't bypass it, and don't describe a
  pending action as done.
  - transfer: applied automatically if qty ≤ 100 and the donor keeps ≥ 14 days
    of cover; otherwise it waits for approval.
  - markdown: applied automatically at ≤ 30%; otherwise it waits.
  - PO cancel: always waits for approval.
- Never call `decide_action` without an explicit approve / reject from the user.
- To reset the data: `python data/seed.py`. This also wipes the ledger.
