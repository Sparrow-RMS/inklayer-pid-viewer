import { useEffect, useRef, useState } from 'react'
import { pickNodeColor } from '../lib/annotationFromNode'
import { worksheetNodeId } from '../lib/hazopApi'
import './ChatPanel.css'

export interface BBox {
  x: number
  y: number
  width: number
  height: number
}

export interface LinePoint {
  x: number
  y: number
}

interface ProposedNodeBase {
  pageIndex: number
  label: string
  description: string
  /** Step 3's node id, for pipeline nodes — keys the node's HAZOP worksheet. */
  nodeId?: string
}

/** One member line's traced route inside a pipeline-clustered node —
 * `entityId` is step 1's id for that line, kept for traceability even though
 * nothing in the frontend needs to resolve it back further right now. */
export interface RouteSegment {
  entityId: string
  /** Ordered with the flow when `flow` is true. */
  points: LinePoint[]
  /** Set on routes traced from the drawing: whether the flow direction is known. */
  flow?: boolean
}

/** The HAZOP node-creation rules' Step 6 fields, as the agent's rules-based
 * step 3 writes them (agent/src/hazop_agent/pipeline/step3/build.py). */
export interface HazopBoundary {
  tag: string
  kind: string
  note: string
}

/** One parameter the node studies (node rules Step 6a), with its own safeguards. */
export interface HazopParameter {
  parameter: string
  name: string
  highSafeguards: string[]
  lowSafeguards: string[]
}

export interface HazopDeviation {
  guideword: string
  /** Step 6a parameter: flow, level, pressure, temperature, composition, mixing, … */
  parameter: string
  /** What deviates, e.g. "flow into ST-V303". */
  detail: string
  /** One item per cause, each naming where it happens. */
  causes: HazopCause[]
}

export interface HazopCause {
  /** e.g. "XV-001 fails closed on 16\"-HSD-065-A21A", or "not credible: …". */
  cause: string
  /** Valve tags, line numbers and equipment tags the cause happens at. */
  tags: string[]
  consequences: HazopConsequence[]
}

export interface HazopConsequence {
  /** What happens if the cause is not stopped: what is affected, and the harm. */
  consequence: string
  /** Tags / C&E rows that prevent or mitigate it. */
  safeguards: string[]
}

export interface HazopDetails {
  localId: string
  name: string
  boundaryFrom: HazopBoundary[]
  boundaryTo: HazopBoundary[]
  equipmentTags: string[]
  designIntent: string
  designConditions: { pressure: string; temperature: string; material: string; source: string }
  controlLoops: string[]
  interlockRefs: string[]
  reliefDevices: { tag: string; setPressure: string }[]
  parameters: HazopParameter[]
  vacuumProtection: string | null
  keyDeviations: HazopDeviation[]
  operatingModes: string[]
  utilitiesAsSafeguards: string[]
  typicalOf: string | null
  typicalOfName: string | null
  differencesFromTypical: string[]
  revisedScope: boolean
  justification: { rule: string; reason: string }[]
  unverified: string[]
}

export type ProposedNode =
  | (ProposedNodeBase & { shapeType: 'path'; path: LinePoint[] })
  | (ProposedNodeBase & { shapeType: 'rect'; bbox: BBox })
  | (ProposedNodeBase & {
      /** A node produced by the agent's pipeline (a step3 /
       * step3-consolidated.json document). Unlike 'path'/'rect', it can carry
       * MULTIPLE traced routes — one per line entity the clustering folded
       * in — plus an overall bbox as a fallback for a node with no traced
       * line at all (a pure equipment/utility node). */
      shapeType: 'hazop'
      categories: string[]
      bbox: BBox
      routes: RouteSegment[]
      entityCount: number
      processCount: number
      /** Present on nodes built by the rules-based step 3. */
      hazop?: HazopDetails
    })

export type NodeStatus = 'pending' | 'accepted' | 'rejected'

/** colorIndex and nodeNumber are assigned once, when the node is first
 * proposed and painted onto the PDF, and stay fixed through accept/reject/
 * undo — colorIndex so the card's swatch always matches the shape already on
 * canvas, nodeNumber so InkLayer's own on-canvas hover label reads "#2 ·
 * Node 2" instead of an arbitrary, ever-climbing number.
 * mergedInto: when set, this node's shape has been repainted with another
 * node's identity (colorIndex/nodeNumber already point at the target) —
 * mergedInto just drives the "→ merged into Node N" note on the card. */
export type ReviewedNode = ProposedNode & { status: NodeStatus; colorIndex: number; nodeNumber: number; mergedInto?: number }

export type ChatMessage =
  | { id: string; role: 'user' | 'assistant'; kind: 'text'; text: string }
  | { id: string; role: 'assistant'; kind: 'nodes'; nodes: ReviewedNode[] }

export interface ChatPanelProps {
  messages: ChatMessage[]
  isSending: boolean
  onSend: (message: string) => void
  onNodeStatusChange: (messageId: string, nodeIndex: number, node: ReviewedNode, status: NodeStatus) => void
  collapsed: boolean
  onToggleCollapsed: () => void
  onOpenWorksheet: (node: ReviewedNode) => void
  /** Worksheet row count per node id, for the button label. */
  worksheetCounts: Record<string, number>
}

let nextId = 0
export const makeMessageId = () => `msg-${Date.now()}-${nextId++}`

const list = (items: string[]) => (items.length ? items.join(', ') : '—')
const boundaries = (items: HazopBoundary[]) =>
  items.length ? items.map((b) => `${b.tag} (${b.kind}${b.note ? `, ${b.note}` : ''})`).join('; ') : '—'

/** The Step 6 table for one node, collapsed by default so the card list stays scannable. */
function HazopSheet({ h }: { h: HazopDetails }) {
  const rows: [string, string][] = [
    ['Boundary from', boundaries(h.boundaryFrom)],
    ['Boundary to', boundaries(h.boundaryTo)],
    ['Equipment', list(h.equipmentTags)],
    ['Design conditions', `${h.designConditions.pressure} · ${h.designConditions.temperature} · ${h.designConditions.material}` +
      (h.designConditions.source ? ` (${h.designConditions.source})` : '')],
    ['Controls', list(h.controlLoops)],
    ['Interlocks', list(h.interlockRefs)],
    ['Relief devices', h.reliefDevices.length ? h.reliefDevices.map((r) => `${r.tag} @ ${r.setPressure}`).join(', ') : '—'],
    ...h.parameters.map((p): [string, string] => [p.name || p.parameter, `HIGH: ${list(p.highSafeguards)}; LOW: ${list(p.lowSafeguards)}`]),
    ...(h.vacuumProtection ? [['Vacuum', h.vacuumProtection] as [string, string]] : []),
    ['Operating modes', list(h.operatingModes)],
    ['Utilities (safeguards)', list(h.utilitiesAsSafeguards)],
  ]
  if (h.typicalOf) rows.push(['Typical of', `${h.typicalOfName ?? h.typicalOf} — differences: ${list(h.differencesFromTypical)}`])
  if (h.unverified.length) rows.push(['UNVERIFIED', list(h.unverified)])

  return (
    <details className="hazop-sheet">
      <summary>HAZOP node sheet</summary>
      <dl>
        {rows.map(([k, v]) => (
          <div key={k} className="hazop-sheet-row">
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
      {h.keyDeviations.length > 0 && (
        <>
          <div className="hazop-sheet-heading">Key deviations</div>
          <ul>
            {h.keyDeviations.map((d, i) => (
              <li key={i}>
                <strong>{d.guideword} {d.detail || d.parameter}</strong>
                <ul>
                  {d.causes.map((c, j) => (
                    <li key={j}>
                      {c.cause}
                      {c.consequences.map((q, k) => (
                        <div key={k}>
                          → {q.consequence || 'consequence to describe'}
                          {q.safeguards.length > 0 && <> · safeguards: {q.safeguards.join(', ')}</>}
                        </div>
                      ))}
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        </>
      )}
      {h.justification.length > 0 && (
        <>
          <div className="hazop-sheet-heading">Justification</div>
          <ul>
            {h.justification.map((j, i) => (
              <li key={i}>
                <strong>{j.rule}</strong> {j.reason}
              </li>
            ))}
          </ul>
        </>
      )}
    </details>
  )
}

function NodeCard({
  node,
  onStatusChange,
  onOpenWorksheet,
  worksheetCount,
}: {
  node: ReviewedNode
  onStatusChange: (status: NodeStatus) => void
  onOpenWorksheet: () => void
  worksheetCount: number
}) {
  return (
    <div className={`node-card node-card-${node.status}`}>
      <div className="node-card-header">
        <span className="node-card-swatch" style={{ background: pickNodeColor(node.colorIndex) }} />
        <span className="node-card-label">{node.label}</span>
        <span className="node-card-page">page {node.pageIndex + 1}</span>
      </div>
      {node.shapeType === 'hazop' && node.hazop && (node.hazop.revisedScope || node.hazop.typicalOf) && (
        <div className="node-card-badges">
          {node.hazop.revisedScope && <span className="node-badge node-badge-revised">New / modified scope</span>}
          {node.hazop.typicalOf && <span className="node-badge">Typical of {node.hazop.typicalOfName ?? node.hazop.typicalOf}</span>}
        </div>
      )}
      <p className="node-card-description">{node.description}</p>
      {node.mergedInto != null && <p className="node-card-merged">→ merged into Node {node.mergedInto}</p>}
      <div className="node-card-bbox">
        {node.shapeType === 'rect' ? (
          <>
            rect · x {node.bbox.x.toFixed(2)} · y {node.bbox.y.toFixed(2)} · w {node.bbox.width.toFixed(2)} · h{' '}
            {node.bbox.height.toFixed(2)}
          </>
        ) : node.shapeType === 'path' ? (
          <>path · {node.path.map((p) => `(${p.x.toFixed(2)}, ${p.y.toFixed(2)})`).join(' → ')}</>
        ) : (
          <>
            {node.categories.join(' · ')} · {node.entityCount} entit{node.entityCount === 1 ? 'y' : 'ies'} ·{' '}
            {node.processCount} process{node.processCount === 1 ? '' : 'es'} · {node.routes.length} traced line
            {node.routes.length === 1 ? '' : 's'}
          </>
        )}
      </div>
      {node.shapeType === 'hazop' && node.hazop && <HazopSheet h={node.hazop} />}
      <div className="node-card-actions">
        {node.status !== 'accepted' && <button onClick={() => onStatusChange('accepted')}>Accept</button>}
        {node.status !== 'rejected' && <button onClick={() => onStatusChange('rejected')}>Reject</button>}
        {node.status !== 'pending' && <button onClick={() => onStatusChange('pending')}>Undo</button>}
        {node.status !== 'rejected' && (
          <button className="node-card-worksheet" onClick={onOpenWorksheet}>
            HAZOP worksheet{worksheetCount ? ` (${worksheetCount})` : ''}
          </button>
        )}
      </div>
    </div>
  )
}

export default function ChatPanel({
  messages,
  isSending,
  onSend,
  onNodeStatusChange,
  collapsed,
  onToggleCollapsed,
  onOpenWorksheet,
  worksheetCounts,
}: ChatPanelProps) {
  const [draft, setDraft] = useState('')
  const listRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, isSending])

  if (collapsed) {
    return (
      <button className="chat-panel-collapsed" onClick={onToggleCollapsed} title="Open chat" aria-label="Open chat">
        💬
      </button>
    )
  }

  const submit = () => {
    const text = draft.trim()
    if (!text || isSending) return
    onSend(text)
    setDraft('')
  }

  return (
    <div className="chat-panel">
      <div className="chat-panel-header">
        <span>Assistant</span>
        <button className="chat-panel-icon-btn" onClick={onToggleCollapsed} title="Collapse chat" aria-label="Collapse chat">
          ›
        </button>
      </div>

      <div className="chat-panel-messages" ref={listRef}>
        {messages.length === 0 && (
          <div className="chat-panel-empty">
            Run the HAZOP pipeline to get this drawing's nodes, then ask about them here: merge, rename or inspect nodes.
          </div>
        )}
        {messages.map((m) => {
          if (m.kind === 'nodes') {
            return (
              <div key={m.id} className="node-list">
                {m.nodes.map((node, i) => (
                  <NodeCard
                    key={i}
                    node={node}
                    onStatusChange={(status) => onNodeStatusChange(m.id, i, node, status)}
                    onOpenWorksheet={() => onOpenWorksheet(node)}
                    worksheetCount={worksheetCounts[worksheetNodeId(node)] ?? 0}
                  />
                ))}
              </div>
            )
          }
          return (
            <div key={m.id} className={`chat-bubble chat-bubble-${m.role}`}>
              {m.text}
            </div>
          )
        })}
        {isSending && (
          <div className="chat-bubble chat-bubble-assistant chat-bubble-typing">
            <span />
            <span />
            <span />
          </div>
        )}
      </div>

      <div className="chat-panel-input-row">
        <textarea
          ref={textareaRef}
          value={draft}
          placeholder="Message the assistant…"
          rows={1}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              submit()
            }
          }}
        />
        <button onClick={submit} disabled={!draft.trim() || isSending} aria-label="Send message">
          ↑
        </button>
      </div>
    </div>
  )
}
