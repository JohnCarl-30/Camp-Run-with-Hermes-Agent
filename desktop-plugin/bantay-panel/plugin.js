// BANTAY PANEL — Layer 3 (the face) for the Bantay branch watchdog.
//
// Flow: pane button → prompt → Hermes loads the `bantay-branch-doctor` SKILL
//       → calls the `bantay` MCP tools → the agent answers with
//       ::bantay-branch{…} / ::bantay-action{…} directives → this plugin renders
//       them as native cards in the chat AND feeds them back into the pane's
//       live branch board + action feed. GUI → skill → MCP → real data → GUI.
//
// Install: ~/.hermes/desktop-plugins/bantay-panel/plugin.js (folder == id),
// then ⌘K → "Reload desktop plugins".
// Loader rules: only '@hermes/plugin-sdk', 'react', 'react/jsx-runtime';
// jsx() calls (no JSX syntax); theme variables only, no hardcoded colors.

import {
  host,
  useValue,
  atom,
  Button,
  Badge,
  StatusDot,
  PANES_AREA,
  PALETTE_AREA,
  TRANSCRIPT_DIRECTIVE_AREA
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect } from 'react'

const PLUGIN_ID = 'bantay-panel'
const SKILL = 'bantay-branch-doctor'

const BRANCHES = {
  BGC: 'BGC High Street',
  MKT: 'Makati Legazpi',
  ORT: 'Ortigas Center',
  KPT: 'Kapitolyo',
  CUB: 'Cubao',
  KAT: 'Katipunan',
  TMR: 'Tomas Morato',
  MAN: 'Mandaluyong Shaw',
  ALB: 'Alabang',
  PQE: 'Parañaque BF',
  MKN: 'Marikina',
  ERM: 'Ermita'
}

const ISSUE_LABEL = {
  rating_drop: 'Rating drop',
  understaffing: 'Understaffed',
  late_deliveries: 'Late deliveries',
  forgotten_tickets: 'Forgotten tickets',
  stockouts: 'Stock-outs'
}

const ACTION_LABEL = {
  escalate_ticket: 'Ticket escalated',
  reply_to_review: 'Review replied',
  shift_cover: 'Shift cover requested',
  stock_transfer: 'Stock transfer requested'
}

const TONE = { red: 'bad', amber: 'warn', green: 'good' }
const BADGE = { red: 'destructive', amber: 'warn', green: 'success' }

// ------------------------------------------------------------------ state
const $board = atom({}) // code -> latest branch card
const $actions = atom([]) // newest first
const $selected = atom('KPT')
let storage = null

function saveBoard(next) {
  $board.set(next)
  storage?.set('board', next)
}

function upsertBranch(card) {
  const cur = $board.get()
  if (JSON.stringify(cur[card.code]) === JSON.stringify(card)) return
  saveBoard({ ...cur, [card.code]: card })
}

function pushAction(a) {
  const cur = $actions.get()
  if (cur.some((x) => x.id === a.id)) return
  const next = [a, ...cur].slice(0, 30)
  $actions.set(next)
  storage?.set('actions', next)
}

// Directive attrs are untrusted model output — validate, render nothing on garbage.
function parseBranch(attrs) {
  const code = String(attrs?.code || '').toUpperCase()
  const status = String(attrs?.status || '').toLowerCase()
  const score = Number(attrs?.score)
  if (!BRANCHES[code] || !TONE[status] || Number.isNaN(score)) return null
  return {
    code,
    name: BRANCHES[code],
    score,
    status,
    rating: attrs.rating || '-',
    before: attrs.before || '-',
    late: attrs.late || '-',
    staffing: attrs.staffing || '-',
    tickets: attrs.tickets || '0',
    issue: attrs.issue || ''
  }
}

function parseAction(attrs) {
  const id = String(attrs?.id || '')
  const type = String(attrs?.type || '')
  if (!id || !ACTION_LABEL[type]) return null
  return {
    id,
    type,
    branch: String(attrs.branch || '').toUpperCase(),
    target: String(attrs.target || ''),
    status: String(attrs.status || 'done')
  }
}

// ---------------------------------------------------------------- prompts
const PROMPTS = {
  sweep: () => `Use the ${SKILL} skill: run the morning sweep for all 12 Suki Mart branches.`,
  diagnose: (code) =>
    `Use the ${SKILL} skill: diagnose why the ${code} (${BRANCHES[code]}) branch is struggling and propose fixes.`,
  approve: (code) => `I approve. Use the ${SKILL} skill to apply the proposed fixes for ${code} (${BRANCHES[code]}).`,
  ledger: () => `Use the ${SKILL} skill: show the Bantay action ledger and what each action was based on.`
}

function send(prompt) {
  const ok = host.composer.submit(null, prompt)
  if (!ok) host.notify({ kind: 'info', message: 'Open or focus a chat first, then click again.' })
}

// ------------------------------------------------------- shared fragments
function metricsLine(c) {
  const parts = [`★ ${c.rating} (was ${c.before})`, `Staffing ${c.staffing}%`, `Tickets ${c.tickets}`]
  if (c.late !== '-' && c.late !== 'null') parts.splice(1, 0, `Late ${c.late}%`)
  return parts.join(' · ')
}

// ------------------------------------------------------ transcript cards
function BranchCard({ attrs, streaming }) {
  const card = parseBranch(attrs)
  const key = card ? JSON.stringify(card) : ''
  useEffect(() => {
    if (card && !streaming) upsertBranch(card)
  }, [key, streaming])
  if (!card) return null
  return jsxs('div', {
    className: 'my-1 flex items-center gap-3 rounded-md border border-(--ui-stroke-secondary) px-3 py-2 text-sm',
    children: [
      jsx(StatusDot, { tone: TONE[card.status], className: 'size-2.5' }),
      jsxs('div', {
        className: 'flex min-w-0 flex-1 flex-col',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx('span', { className: 'font-medium', children: card.code }),
              jsx('span', { className: 'truncate text-(--ui-text-secondary)', children: card.name }),
              card.issue && ISSUE_LABEL[card.issue]
                ? jsx('span', { className: 'text-xs text-(--ui-text-tertiary)', children: `· ${ISSUE_LABEL[card.issue]}` })
                : null
            ]
          }),
          jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: metricsLine(card) })
        ]
      }),
      jsx(Badge, { variant: BADGE[card.status], children: `${card.score}/100` }),
      jsx(Button, {
        size: 'xs',
        variant: 'outline',
        disabled: streaming,
        onClick: () => {
          $selected.set(card.code)
          send(PROMPTS.diagnose(card.code))
        },
        children: 'Diagnose'
      })
    ]
  })
}

function ActionCard({ attrs, streaming }) {
  const a = parseAction(attrs)
  const key = a ? JSON.stringify(a) : ''
  useEffect(() => {
    if (a && !streaming) pushAction(a)
  }, [key, streaming])
  if (!a) return null
  return jsxs('div', {
    className: 'my-1 flex items-center gap-2 rounded-md border border-(--ui-stroke-secondary) px-3 py-1.5 text-xs',
    children: [
      jsx(StatusDot, { tone: a.status === 'done' ? 'good' : 'warn' }),
      jsx('span', { className: 'font-medium', children: ACTION_LABEL[a.type] }),
      jsx('span', { className: 'truncate text-(--ui-text-secondary)', children: `${a.branch} · ${a.target}` }),
      jsx('span', { className: 'ml-auto text-(--ui-text-tertiary)', children: `#${a.id} · ${a.status}` })
    ]
  })
}

// ------------------------------------------------------------------- pane
function BoardRow({ card, selected }) {
  return jsxs('button', {
    type: 'button',
    onClick: () => $selected.set(card.code),
    onDoubleClick: () => send(PROMPTS.diagnose(card.code)),
    className:
      'flex w-full items-center gap-2 rounded-md border px-2 py-1.5 text-left hover:opacity-80 ' +
      (selected ? 'border-(--ui-accent)' : 'border-(--ui-stroke-secondary)'),
    children: [
      jsx(StatusDot, { tone: TONE[card.status], className: 'size-2' }),
      jsx('span', { className: 'w-9 font-medium', children: card.code }),
      jsx('span', {
        className: 'min-w-0 flex-1 truncate text-xs text-(--ui-text-tertiary)',
        children: ISSUE_LABEL[card.issue] || card.name
      }),
      jsx(Badge, { size: 'xs', variant: BADGE[card.status], children: String(card.score) })
    ]
  })
}

function BantayPane() {
  const busy = useValue(host.state.busy)
  const board = useValue($board)
  const actions = useValue($actions)
  const selected = useValue($selected)
  const cards = Object.values(board).sort((a, b) => a.score - b.score)
  const red = cards.filter((c) => c.status === 'red').length

  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 overflow-auto p-3 text-sm',
    children: [
      jsxs('div', {
        children: [
          jsx('div', { className: 'font-medium', children: 'Bantay · Branch Watch' }),
          jsx('div', {
            className: 'text-xs text-(--ui-text-tertiary)',
            children: busy
              ? 'Bantay is investigating…'
              : cards.length
                ? `${cards.length} branches · ${red} need attention`
                : 'Connects reviews, deliveries, staffing, tickets & stock.'
          })
        ]
      }),
      jsx(Button, { disabled: busy, onClick: () => send(PROMPTS.sweep()), children: 'Run morning sweep' }),

      cards.length
        ? jsx('div', {
            className: 'flex flex-col gap-1',
            children: cards.map((c) => jsx(BoardRow, { card: c, selected: c.code === selected }, c.code))
          })
        : jsx('div', {
            className: 'rounded-md border border-dashed border-(--ui-stroke-secondary) p-3 text-xs text-(--ui-text-tertiary)',
            children: 'No scan yet — run the morning sweep to load live branch health.'
          }),

      jsxs('div', {
        className: 'flex flex-col gap-2 rounded-md border border-(--ui-stroke-secondary) p-2',
        children: [
          jsx('div', {
            className: 'text-xs text-(--ui-text-secondary)',
            children: `Selected: ${selected} · ${BRANCHES[selected] || ''}`
          }),
          jsxs('div', {
            className: 'flex gap-2',
            children: [
              jsx(Button, {
                className: 'flex-1',
                variant: 'secondary',
                disabled: busy,
                onClick: () => send(PROMPTS.diagnose(selected)),
                children: 'Diagnose'
              }),
              jsx(Button, {
                className: 'flex-1',
                disabled: busy,
                onClick: () => send(PROMPTS.approve(selected)),
                children: 'Approve fixes'
              })
            ]
          })
        ]
      }),

      jsxs('div', {
        className: 'flex flex-col gap-1',
        children: [
          jsxs('div', {
            className: 'flex items-center justify-between',
            children: [
              jsx('span', { className: 'text-xs font-medium', children: `Actions taken (${actions.length})` }),
              jsx(Button, {
                size: 'xs',
                variant: 'ghost',
                disabled: busy,
                onClick: () => send(PROMPTS.ledger()),
                children: 'Evidence ledger'
              })
            ]
          }),
          actions.length
            ? actions.slice(0, 8).map((a) =>
                jsxs(
                  'div',
                  {
                    className: 'flex items-center gap-2 text-xs',
                    children: [
                      jsx(StatusDot, { tone: a.status === 'done' ? 'good' : 'warn' }),
                      jsx('span', { className: 'truncate', children: `${a.branch} · ${ACTION_LABEL[a.type]}` }),
                      jsx('span', { className: 'ml-auto text-(--ui-text-tertiary)', children: `#${a.id}` })
                    ]
                  },
                  a.id
                )
              )
            : jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Nothing yet — approve fixes to act.' })
        ]
      })
    ]
  })
}

// --------------------------------------------------------------- register
export default {
  id: PLUGIN_ID,
  name: 'Bantay',
  register(ctx) {
    storage = ctx.storage
    $board.set(ctx.storage.get('board', {}) || {})
    $actions.set(ctx.storage.get('actions', []) || [])

    ctx.register({
      id: 'pane',
      area: PANES_AREA,
      title: 'Bantay',
      data: { placement: 'right', width: '320px' },
      render: () => jsx(BantayPane, {})
    })

    ctx.register({
      id: 'branch-card',
      area: TRANSCRIPT_DIRECTIVE_AREA,
      data: { name: 'bantay-branch', render: ({ attrs, streaming }) => jsx(BranchCard, { attrs, streaming }) }
    })
    ctx.register({
      id: 'action-card',
      area: TRANSCRIPT_DIRECTIVE_AREA,
      data: { name: 'bantay-action', render: ({ attrs, streaming }) => jsx(ActionCard, { attrs, streaming }) }
    })

    const commands = [
      ['sweep', 'Bantay: Run morning sweep', () => send(PROMPTS.sweep())],
      ['diagnose', 'Bantay: Diagnose selected branch', () => send(PROMPTS.diagnose($selected.get()))],
      ['approve', 'Bantay: Approve fixes for selected branch', () => send(PROMPTS.approve($selected.get()))],
      ['ledger', 'Bantay: Show evidence ledger', () => send(PROMPTS.ledger())]
    ]
    for (const [id, label, run] of commands) {
      ctx.register({
        id: `cmd-${id}`,
        area: PALETTE_AREA,
        data: { id: `${PLUGIN_ID}.${id}`, label, keywords: ['bantay', 'suki', 'branch'], run }
      })
    }
  }
}
