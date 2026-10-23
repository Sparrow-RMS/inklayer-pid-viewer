import type { ProposedNode } from '../components/ChatPanel'
import { agentFetch, agentUrl } from './agentClient'

/**
 * Client for the agent server's HAZOP worksheet routes
 * (agent/src/hazop_agent/worksheets.py), proxied by Vite under /agent.
 *
 * A worksheet is the hierarchy HAZOP teams record:
 *   deviation (parameter, guide word, text)
 *     -> cause -> consequence (severity, likelihood, so the risk is per consequence)
 *       -> safeguards, recommendations
 * numbered 1 / 1.1 / 1.1.1 / 1.1.1.1. A study is one drawing, keyed by the same
 * id the pipeline gives its job directory, so a worksheet follows the PDF.
 */
const HAZOP_API = import.meta.env.VITE_HAZOP_API_URL || agentUrl('/agent/hazop')

export type RecommendationStatus = 'open' | 'in progress' | 'closed'

export interface Safeguard {
  id: string
  text: string
}

export interface Recommendation {
  id: string
  text: string
  owner: string
  status: RecommendationStatus
}

export interface Consequence {
  id: string
  text: string
  /** 1–5 on the risk matrix, null until the team rates it. */
  severity: number | null
  likelihood: number | null
  safeguards: Safeguard[]
  recommendations: Recommendation[]
}

export interface Cause {
  id: string
  text: string
  consequences: Consequence[]
}

export interface Deviation {
  id: string
  studyId: string
  nodeId: string
  nodeLabel: string
  parameter: string
  guideword: string
  deviation: string
  position: number
  causes: Cause[]
}

export type ItemKind = 'deviations' | 'causes' | 'consequences' | 'safeguards' | 'recommendations'

/** Guide words as worksheets write them; any other text is kept too. */
export const GUIDEWORDS = ['NO', 'NO/LESS', 'LESS', 'MORE', 'HIGH', 'LOW', 'REVERSE', 'AS WELL AS', 'PART OF', 'OTHER THAN', 'MISDIRECTED', 'EARLY', 'LATE', 'BEFORE', 'AFTER']

/** Parameters in the order a worksheet groups them. */
export const PARAMETERS = ['Flow', 'Level', 'Pressure', 'Temperature', 'Composition', 'Mixing', 'Phase', 'Reaction', 'Other']

export const RECOMMENDATION_STATUSES: RecommendationStatus[] = ['open', 'in progress', 'closed']

/** Same rule as the pipeline's jobs.job_id_for: file-name slug + first 12 hex of sha256(bytes). */
export async function studyIdFor(fileName: string, pdf: ArrayBuffer): Promise<string> {
  const name = fileName || 'document.pdf'
  const stem = /^\.[^.]*$/.test(name) ? name : name.replace(/\.[^.]*$/, '')
  const slug = stem.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || 'document'
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', pdf.slice(0)))
  const hex = Array.from(digest, (b) => b.toString(16).padStart(2, '0')).join('')
  return `${slug}-${hex.slice(0, 12)}`
}

/** Pipeline nodes carry step 3's id; nodes from other sources fall back to page + label. */
export function worksheetNodeId(node: ProposedNode): string {
  return node.nodeId ?? `p${node.pageIndex}:${node.label}`
}

export type RiskBand = 'low' | 'medium' | 'high' | 'very-high'

/** Example 5x5 matrix. Every company has its own; swap the bands here. */
export function riskOf(c: Pick<Consequence, 'severity' | 'likelihood'>): { score: number; band: RiskBand; label: string } | null {
  if (!c.severity || !c.likelihood) return null
  const score = c.severity * c.likelihood
  const band: RiskBand = score <= 4 ? 'low' : score <= 9 ? 'medium' : score <= 16 ? 'high' : 'very-high'
  return { score, band, label: { low: 'L', medium: 'M', high: 'H', 'very-high': 'VH' }[band] }
}

/** Sort key: the worksheet groups deviations by parameter, in PARAMETERS order. */
export function parameterRank(parameter: string): number {
  const i = PARAMETERS.findIndex((p) => p.toLowerCase() === parameter.trim().toLowerCase())
  return i === -1 ? PARAMETERS.length : i
}

export interface DeviationDraft {
  parameter: string
  guideword: string
  deviation: string
  causes: { text: string; consequences: { text: string; safeguards: { text: string }[] }[] }[]
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await agentFetch(`${HAZOP_API}${path}`, {
    ...init,
    headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    const detail = typeof body?.detail === 'string' ? body.detail : body?.detail?.[0]?.msg
    throw new Error(detail || `HAZOP request failed (${response.status})`)
  }
  return (response.status === 204 ? undefined : await response.json()) as T
}

const post = <T,>(path: string, body: unknown) => request<T>(path, { method: 'POST', body: JSON.stringify(body) })

export function getWorksheet(studyId: string, nodeId?: string): Promise<Deviation[]> {
  const query = nodeId ? `?${new URLSearchParams({ nodeId })}` : ''
  return request(`/studies/${encodeURIComponent(studyId)}/worksheet${query}`)
}

/** Deviations per node id. */
export function getSummary(studyId: string): Promise<Record<string, number>> {
  return request(`/studies/${encodeURIComponent(studyId)}/summary`)
}

export function createDeviations(studyId: string, nodeId: string, nodeLabel: string, drafts: DeviationDraft[]): Promise<Deviation[]> {
  return post(`/studies/${encodeURIComponent(studyId)}/deviations/bulk`, drafts.map((d) => ({ ...d, nodeId, nodeLabel })))
}

export interface GenerateResult {
  /** The node's whole worksheet after the draft was added. */
  deviations: Deviation[]
  added: number
  /** Drafted deviations already in the worksheet (same guide word and text). */
  skipped: number
  warnings: string[]
  notes: string
}

/**
 * Step 4: the agent drafts the node's worksheet with the model (deviations,
 * causes naming their tags, consequences, safeguards, recommendations) and
 * adds it. Takes one model call, usually 1-3 minutes. `replace` deletes the
 * node's existing deviations first.
 */
export function generateWorksheet(studyId: string, nodeId: string, replace = false): Promise<GenerateResult> {
  return post(`/studies/${encodeURIComponent(studyId)}/nodes/${encodeURIComponent(nodeId)}/generate`, { replace })
}

// Each of these returns the whole deviation the item belongs to, refreshed.
export const addCause = (deviationId: string, text = '') => post<Deviation>(`/deviations/${deviationId}/causes`, { text })
export const addConsequence = (causeId: string, text = '') => post<Deviation>(`/causes/${causeId}/consequences`, { text })
export const addSafeguard = (consequenceId: string, text = '') => post<Deviation>(`/consequences/${consequenceId}/safeguards`, { text })
export const addRecommendation = (consequenceId: string, text = '') => post<Deviation>(`/consequences/${consequenceId}/recommendations`, { text })

export function updateItem(kind: ItemKind, id: string, fields: Record<string, string | number | null>): Promise<Deviation> {
  return request(`/${kind}/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(fields) })
}

export function deleteItem(kind: ItemKind, id: string): Promise<void> {
  return request(`/${kind}/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

/** One CSV row per consequence (or per cause / deviation with none), numbered like the worksheet. */
export function worksheetToCsv(nodeLabel: string, deviations: Deviation[]): string {
  const cell = (v: string | number | null) => {
    const s = v == null ? '' : String(v)
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  const header = ['Node', 'Parameter', 'Guide word', 'No.', 'Deviation', 'No.', 'Cause', 'No.', 'Consequence', 'Safeguards', 'S', 'L', 'Risk', 'Recommendations']
  const rows: (string | number | null)[][] = []
  deviations.forEach((d, i) => {
    const dn = `${i + 1}`
    const base = [nodeLabel, d.parameter, d.guideword, dn, d.deviation]
    if (!d.causes.length) rows.push([...base, '', '', '', '', '', '', '', '', ''])
    d.causes.forEach((c, j) => {
      const cn = `${dn}.${j + 1}`
      if (!c.consequences.length) rows.push([...base, cn, c.text, '', '', '', '', '', '', ''])
      c.consequences.forEach((q, k) => {
        const qn = `${cn}.${k + 1}`
        const risk = riskOf(q)
        rows.push([...base, cn, c.text, qn, q.text,
          q.safeguards.map((s, m) => `${qn}.${m + 1} ${s.text}`).join('\n'), q.severity, q.likelihood, risk ? `${risk.score} ${risk.label}` : '',
          q.recommendations.map((r) => `${r.text}${r.owner ? ` [${r.owner}]` : ''} (${r.status})`).join('\n')])
      })
    })
  })
  return [header, ...rows].map((r) => r.map(cell).join(',')).join('\n')
}
