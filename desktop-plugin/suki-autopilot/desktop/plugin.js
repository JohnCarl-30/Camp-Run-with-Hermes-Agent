// SUKI AUTOPILOT: Layer 3 (the face).
//
// Flow: button → chat prompt → Hermes loads the `suki-autopilot` SKILL → calls the
// `suki-autopilot` MCP tools → writes to store.db + the agent_actions ledger.
// The pane READS the ledger and branch health from this plugin's own read-only
// backend (dashboard/plugin_api.py → ctx.rest) and polls it. Every WRITE goes
// through the chat, so each change is visible in the transcript.
// If the backend is off, the pane says so; buttons, ⌘K and chat cards keep working.
//
// Loader rules: only '@hermes/plugin-sdk', 'react', 'react/jsx-runtime';
// jsx()/jsxs() calls (no JSX syntax); theme variables only, no hardcoded colors.

import {
  host,
  useValue,
  useQuery,
  queryClient,
  Button,
  Badge,
  StatusDot,
  ScrollArea,
  EmptyState,
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
  PANES_AREA,
  PALETTE_AREA,
  STATUSBAR_AREAS,
  TRANSCRIPT_DIRECTIVE_AREA
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useRef, useState } from 'react'

const PLUGIN_ID = 'suki-autopilot'
const SKILL = 'suki-autopilot'
const CODES = ['KPT', 'ORT', 'KAT', 'CUB', 'MKN', 'ALB', 'TMR', 'BGC', 'MKT', 'PQE', 'ERM', 'MAN']
const TYPE_LABEL = { transfer_stock: 'Transfer stock', markdown: 'Markdown', cancel_po: 'Cancel PO' }
const STATUS = {
  applied: { label: 'Applied', icon: '✅', badge: 'success', tone: 'good' },
  approved: { label: 'Approved', icon: '✅', badge: 'success', tone: 'good' },
  pending: { label: 'Pending', icon: '⏳', badge: 'warn', tone: 'warn' },
  rejected: { label: 'Rejected', icon: '✕', badge: 'muted', tone: 'muted' }
}
const HEALTH_TONE = { red: 'bad', amber: 'warn', green: 'good' }
const HEALTH_BADGE = { red: 'destructive', amber: 'warn', green: 'success' }

let rest = null // set in register(); ctx.rest to /api/plugins/suki-autopilot

// ---------------------------------------------------------------- prompts
const PROMPTS = {
  run: () => `Use the ${SKILL} skill to run the autopilot.`,
  diagnose: (code) => `Use the ${SKILL} skill to diagnose branch ${code}.`,
  approve: (id) => `Use the ${SKILL} skill to approve action #${id}.`,
  reject: (id) => `Use the ${SKILL} skill to reject action #${id}.`
}

function send(prompt) {
  const ok = host.composer.submit(null, prompt)
  if (!ok) host.notify({ kind: 'info', message: 'Open or focus a chat first, then click again.' })
  return ok
}

// ------------------------------------------------------------------- data
const KEYS = {
  ping: ['suki-autopilot', 'ping'],
  actions: ['suki-autopilot', 'actions'],
  branches: ['suki-autopilot', 'branches']
}

function usePing() {
  return useQuery({ queryKey: KEYS.ping, queryFn: () => rest('/ping'), refetchInterval: 5000, retry: false })
}

function useActions(enabled) {
  return useQuery({
    queryKey: KEYS.actions,
    queryFn: () => rest('/actions?limit=30'),
    refetchInterval: 3000,
    enabled,
    retry: false
  })
}

function useBranches(enabled) {
  return useQuery({
    queryKey: KEYS.branches,
    queryFn: () => rest('/branches'),
    refetchInterval: 10000,
    enabled,
    retry: false
  })
}

function refreshAll() {
  queryClient.invalidateQueries({ queryKey: ['suki-autopilot'] })
}

// Refresh as soon as a chat turn finishes (busy true → false).
function useRefreshAfterTurn() {
  const busy = useValue(host.state.busy)
  const prev = useRef(busy)
  useEffect(() => {
    if (prev.current && !busy) refreshAll()
    prev.current = busy
  }, [busy])
  return busy
}

const peso = (n) => `₱${Math.round(Number(n) || 0).toLocaleString('en-PH')}`
const text = (v, max = 160) => {
  const s = String(v ?? '').replace(/\s+/g, ' ').trim()
  return s.length > max ? s.slice(0, max - 1) + '…' : s
}

// ------------------------------------------------------------ action card
// Shared by the pane feed and the transcript directive. Plain text only.
function ActionCard({ a, busy, compact }) {
  const st = STATUS[a.status] || STATUS.pending
  return jsxs('div', {
    className: 'my-1 flex flex-col gap-1 rounded-md border border-(--ui-stroke-secondary) px-3 py-2 text-xs',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx(StatusDot, { tone: st.tone }),
          jsx(Badge, { size: 'xs', variant: st.badge, children: `${st.icon} ${st.label.toUpperCase()}` }),
          jsx('span', { className: 'font-medium', children: TYPE_LABEL[a.type] || text(a.type, 24) }),
          a.id ? jsx('span', { className: 'text-(--ui-text-tertiary)', children: `#${a.id}` }) : null,
          jsx('span', {
            className: 'ml-auto text-(--ui-text-tertiary)',
            children: a.confidence ? `conf ${Number(a.confidence).toFixed(2)}` : ''
          })
        ]
      }),
      jsx('div', { className: compact ? 'truncate' : '', children: text(a.summary) }),
      a.evidence
        ? jsx('div', {
            className: 'text-(--ui-text-tertiary)' + (compact ? ' line-clamp-2' : ''),
            children: `Based on: ${text(a.evidence, 220)}`
          })
        : null,
      a.php
        ? jsx('div', { className: 'text-(--ui-text-secondary)', children: `₱ impact ≈ ${peso(a.php)}` })
        : null,
      a.status === 'pending' && a.id
        ? jsxs('div', {
            className: 'mt-1 flex gap-2',
            children: [
              jsx(Button, { size: 'xs', disabled: busy, onClick: () => send(PROMPTS.approve(a.id)), children: 'Approve' }),
              jsx(Button, {
                size: 'xs',
                variant: 'outline',
                disabled: busy,
                onClick: () => send(PROMPTS.reject(a.id)),
                children: 'Reject'
              })
            ]
          })
        : null
    ]
  })
}

function fromLedger(r) {
  return {
    id: String(r.id),
    status: r.status_short,
    type: r.action_type,
    summary: r.summary,
    evidence: r.evidence_line,
    confidence: r.confidence,
    php: r.php_impact
  }
}

// Directive attrs are untrusted model output: validate, render nothing on garbage.
function parseDirective(attrs) {
  const id = String(attrs?.id || '').trim()
  const status = String(attrs?.status || '').toLowerCase()
  const type = String(attrs?.type || '')
  if (!/^\d{1,6}$/.test(id) || !STATUS[status] || !TYPE_LABEL[type]) return null
  const conf = Number(attrs.confidence)
  return {
    id,
    status,
    type,
    summary: text(attrs.summary),
    evidence: text(attrs.evidence, 220),
    confidence: Number.isFinite(conf) && conf >= 0 && conf <= 1 ? conf : null,
    php: Number.isFinite(Number(attrs.php)) ? Number(attrs.php) : null
  }
}

// In-chat card: renders from attrs (works with no backend) but follows the live
// ledger status when the backend is up, so Approve flips the card in place.
function DirectiveCard({ attrs, streaming }) {
  const busy = useValue(host.state.busy)
  const card = parseDirective(attrs)
  const { data } = useQuery({
    queryKey: KEYS.actions,
    queryFn: () => rest('/actions?limit=30'),
    enabled: !!card && !streaming,
    retry: false
  })
  if (!card) return null
  const live = data?.actions?.find((r) => String(r.id) === card.id && r.action_type === card.type)
  const a = live ? { ...card, status: live.status_short || card.status } : card
  return jsx(ActionCard, { a, busy: busy || streaming })
}

// ------------------------------------------------------------------- pane
function Section({ title, right, children }) {
  return jsxs('div', {
    className: 'flex flex-col gap-1.5',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between',
        children: [
          jsx('span', { className: 'text-[0.6875rem] font-medium tracking-wide text-(--ui-text-tertiary)', children: title }),
          right || null
        ]
      }),
      children
    ]
  })
}

function Kpi({ label, value }) {
  return jsxs('div', {
    className: 'flex flex-1 flex-col rounded-md border border-(--ui-stroke-secondary) px-2 py-1.5',
    children: [
      jsx('span', { className: 'text-sm font-medium', children: value }),
      jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: label })
    ]
  })
}

function BranchRow({ b, busy }) {
  const rating =
    b.rating_recent != null ? `${b.rating_recent}★${b.rating_before != null ? ` (was ${b.rating_before})` : ''}` : ''
  return jsxs('div', {
    className: 'flex items-center gap-2 rounded-md px-1 py-1 text-xs hover:bg-(--chrome-action-hover)',
    children: [
      jsx(StatusDot, { tone: HEALTH_TONE[b.status] || 'muted', className: 'size-2' }),
      jsx('span', { className: 'w-9 font-medium', children: b.branch }),
      jsx(Badge, { size: 'xs', variant: HEALTH_BADGE[b.status] || 'muted', children: String(b.health_score) }),
      jsx('span', {
        className: 'min-w-0 flex-1 truncate text-(--ui-text-tertiary)',
        children: [b.worst_metric ? `▼ ${b.worst_metric}` : 'healthy', rating].filter(Boolean).join(' · ')
      }),
      jsx(Button, {
        size: 'xs',
        variant: 'ghost',
        disabled: busy,
        onClick: () => send(PROMPTS.diagnose(b.branch)),
        children: 'Diagnose'
      })
    ]
  })
}

function OfflineDiagnose({ busy }) {
  const [code, setCode] = useState('KPT')
  return jsxs('div', {
    className: 'flex items-center gap-2',
    children: [
      jsxs(Select, {
        value: code,
        onValueChange: setCode,
        children: [
          jsx(SelectTrigger, { className: 'h-7 w-24 text-xs', children: jsx(SelectValue, {}) }),
          jsx(SelectContent, { children: CODES.map((c) => jsx(SelectItem, { value: c, children: c }, c)) })
        ]
      }),
      jsx(Button, {
        size: 'xs',
        variant: 'secondary',
        disabled: busy,
        onClick: () => send(PROMPTS.diagnose(code)),
        children: 'Diagnose'
      })
    ]
  })
}

function SukiPane() {
  const busy = useRefreshAfterTurn()
  const ping = usePing()
  const live = ping.isSuccess && ping.data?.ok === true
  const actionsQ = useActions(live)
  const branchesQ = useBranches(live)
  const totals = actionsQ.data?.totals || { applied: 0, pending: 0, php_protected: 0 }
  const actions = (actionsQ.data?.actions || []).map(fromLedger)
  const branches = branchesQ.data?.branches || []

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col gap-3 p-3 text-sm',
    children: [
      // header
      jsxs('div', {
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx('span', { className: 'font-medium', children: '☤ Suki Autopilot' }),
              jsxs('span', {
                className: 'ml-auto flex items-center gap-1 text-xs text-(--ui-text-tertiary)',
                children: [jsx(StatusDot, { tone: live ? 'good' : ping.isLoading ? 'muted' : 'bad' }), live ? 'Live' : 'Offline']
              })
            ]
          }),
          jsx('div', {
            className: 'text-xs text-(--ui-text-tertiary)',
            children: busy
              ? 'Autopilot is working…'
              : 'Finds why branches lose money, fixes it, shows the evidence.'
          })
        ]
      }),

      // KPIs
      live
        ? jsxs('div', {
            className: 'flex gap-2',
            children: [
              jsx(Kpi, { label: 'applied', value: `✅ ${totals.applied}` }),
              jsx(Kpi, { label: 'pending', value: `⏳ ${totals.pending}` }),
              jsx(Kpi, { label: 'protected ≈', value: peso(totals.php_protected) })
            ]
          })
        : null,

      jsx(Button, { disabled: busy, onClick: () => send(PROMPTS.run()), children: '▶ Run Autopilot' }),

      live
        ? null
        : jsxs('div', {
            className:
              'flex flex-col gap-2 rounded-md border border-dashed border-(--ui-stroke-secondary) p-2 text-xs text-(--ui-text-tertiary)',
            children: ['Live feed offline. Results appear in chat.', jsx(OfflineDiagnose, { busy })]
          }),

      live
        ? jsx(ScrollArea, {
            className: 'min-h-0 flex-1',
            children: jsxs('div', {
              className: 'flex flex-col gap-3 pr-2',
              children: [
                jsx(Section, {
                  title: 'BRANCH HEALTH',
                  children: branches.length
                    ? jsx('div', {
                        className: 'flex flex-col',
                        children: branches.map((b) => jsx(BranchRow, { b, busy }, b.branch))
                      })
                    : jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Loading…' })
                }),
                jsx(Section, {
                  title: 'ACTION FEED',
                  right: actions.length
                    ? jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: `${actions.length} latest` })
                    : null,
                  children: actions.length
                    ? actions.map((a) => jsx(ActionCard, { a, busy, compact: true }, a.id))
                    : jsx(EmptyState, {
                        className: 'min-h-24',
                        title: 'No actions yet',
                        description: 'Press Run Autopilot.'
                      })
                })
              ]
            })
          })
        : null
    ]
  })
}

// ------------------------------------------------------------- status bar
function PendingChip() {
  const ping = usePing()
  const live = ping.isSuccess && ping.data?.ok === true
  const { data } = useActions(live)
  const n = data?.totals?.pending || 0
  if (!live || !n) return null
  return jsx('span', {
    className: 'px-1.5 text-[0.6875rem] text-(--ui-text-secondary)',
    title: 'Suki Autopilot actions waiting for approval',
    children: `⏳ Suki: ${n} pending approval`
  })
}

// --------------------------------------------------------------- register
export default {
  id: PLUGIN_ID,
  name: 'Suki Autopilot',
  register(ctx) {
    rest = (path) => ctx.rest(path)

    ctx.register({
      id: 'pane',
      area: PANES_AREA,
      title: 'Suki Autopilot',
      data: { placement: 'right', width: '360px' },
      render: () => jsx(SukiPane, {})
    })

    ctx.register({
      id: 'action-card',
      area: TRANSCRIPT_DIRECTIVE_AREA,
      data: { name: 'suki-action', render: ({ attrs, streaming }) => jsx(DirectiveCard, { attrs, streaming }) }
    })

    ctx.register({
      id: 'pending',
      area: STATUSBAR_AREAS.left,
      order: 200,
      render: () => jsx(PendingChip, {})
    })

    ctx.registerMany([
      {
        id: 'cmd-run',
        area: PALETTE_AREA,
        data: {
          id: `${PLUGIN_ID}.run`,
          label: 'Suki: Run Autopilot',
          keywords: ['suki', 'autopilot', 'stock', 'expiry', 'purchase'],
          run: () => send(PROMPTS.run())
        }
      },
      {
        id: 'cmd-diagnose',
        area: PALETTE_AREA,
        data: {
          id: `${PLUGIN_ID}.diagnose-kpt`,
          label: 'Suki: Diagnose Kapitolyo (KPT)',
          keywords: ['suki', 'diagnose', 'kpt', 'kapitolyo', 'branch'],
          run: () => send(PROMPTS.diagnose('KPT'))
        }
      }
    ])
  }
}
