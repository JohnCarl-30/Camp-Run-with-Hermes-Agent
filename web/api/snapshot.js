// GET /api/snapshot: live Suki Autopilot state from Neon (read-only role).
// The MCP server on the demo laptop mirrors every action into Neon; this reads it.
import { neon } from '@neondatabase/serverless'

export default async function handler(req, res) {
  res.setHeader('Cache-Control', 'no-store')
  if (!process.env.DATABASE_URL) return res.status(500).json({ error: 'DATABASE_URL not set' })
  try {
    const sql = neon(process.env.DATABASE_URL)
    const [actions, branches, state, bantay] = await Promise.all([
      sql`SELECT id, created_at, action_type AS type, summary, evidence_line AS evidence, reasoning,
                 confidence, risk, status, decided_at, round(php_impact)::int AS php
          FROM agent_actions ORDER BY id DESC LIMIT 100`,
      // SELECT * so the optional `detail` jsonb (drill-down) is returned once the mirror has added it
      sql`SELECT * FROM branch_health ORDER BY health_score, branch`,
      sql`SELECT as_of, synced_at, problems FROM sync_state WHERE id = 1`,
      // Bantay people/customer fixes; the table appears on the first sync after the mirror update
      sql`SELECT id, created_at, action_type AS type, branch_code AS branch, target, summary, evidence, status
          FROM bantay_actions ORDER BY id DESC LIMIT 50`.catch((e) => {
        if (e?.code !== '42P01') console.error('bantay_actions:', e?.code, e?.message)
        return []
      })
    ])
    if (!state.length) return res.status(503).json({ error: 'not synced yet' })
    const totals = { applied: 0, pending: 0, rejected: 0, php_protected: 0, php_pending: 0 }
    for (const a of actions) {
      if (a.status === 'applied' || a.status === 'approved') { totals.applied++; totals.php_protected += a.php }
      else if (a.status === 'pending_approval') { totals.pending++; totals.php_pending += a.php }
      else if (a.status === 'rejected') totals.rejected++
    }
    res.status(200).json({
      live: true,
      as_of: state[0].as_of,
      synced_at: state[0].synced_at,
      problems: state[0].problems,
      branches,
      totals,
      actions,
      bantay
    })
  } catch (e) {
    res.status(502).json({ error: 'database unavailable' })
  }
}
