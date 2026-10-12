import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { pickNodeColor } from '../lib/annotationFromNode'
import {
  GUIDEWORDS,
  PARAMETERS,
  RECOMMENDATION_STATUSES,
  addCause,
  addConsequence,
  addRecommendation,
  addSafeguard,
  createDeviations,
  deleteItem,
  generateWorksheet,
  getWorksheet,
  parameterRank,
  riskOf,
  updateItem,
  worksheetNodeId,
  worksheetToCsv,
  type Cause,
  type Consequence,
  type Deviation,
  type GenerateResult,
  type ItemKind,
} from '../lib/hazopApi'
import type { ReviewedNode } from './ChatPanel'
import './HazopWorksheet.css'

export interface HazopWorksheetProps {
  node: ReviewedNode
  /** Null while the PDF's hash is still being computed. */
  studyId: string | null
  /** Room to leave on the right for the chat panel, in px. */
  rightOffset: number
  onClose: () => void
  /** Called with the node's deviation count after every load or change. */
  onRowsChanged: (nodeId: string, count: number) => void
  /** Study-package documents still missing; null while unknown. */
  packageMissing?: string[] | null
  onOpenPackage?: () => void
}

/** Text that turns into a textarea on click and saves on blur (Esc cancels). */
function EditableText({ value, placeholder, onSave, className }: { value: string; placeholder: string; onSave: (v: string) => void; className?: string }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(value)
  const ref = useRef<HTMLTextAreaElement>(null)
  useEffect(() => {
    if (editing) ref.current?.focus()
  }, [editing])
  const start = () => {
    setDraft(value)
    setEditing(true)
  }
  if (!editing) {
    return (
      <span
        className={`ws-text ${value ? '' : 'ws-placeholder'} ${className ?? ''}`}
        role="button"
        tabIndex={0}
        title="Click to edit"
        onClick={start}
        onKeyDown={(e) => e.key === 'Enter' && start()}
      >
        {value || placeholder}
      </span>
    )
  }
  const finish = (save: boolean) => {
    setEditing(false)
    if (save && draft.trim() !== value.trim()) onSave(draft.trim())
  }
  return (
    <textarea
      ref={ref}
      className="ws-edit"
      aria-label={placeholder}
      value={draft}
      rows={Math.min(6, Math.max(2, Math.ceil(draft.length / 40)))}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => finish(true)}
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          e.stopPropagation()
          finish(false)
        }
        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) finish(true)
      }}
    />
  )
}

function Rating({ value, label, onChange }: { value: number | null; label: string; onChange: (v: number | null) => void }) {
  return (
    <select className="ws-rating" aria-label={label} value={value ?? ''} onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}>
      <option value="">—</option>
      {[1, 2, 3, 4, 5].map((n) => (
        <option key={n} value={n}>
          {n}
        </option>
      ))}
    </select>
  )
}

/** Deviations in the order the worksheet shows them: grouped by parameter. */
function ordered(deviations: Deviation[]) {
  return [...deviations].sort((a, b) => parameterRank(a.parameter) - parameterRank(b.parameter) || a.position - b.position)
}

/** One table row per consequence (or per cause / deviation that has none yet). */
interface Row {
  dev: Deviation
  devNo: string
  devSpan: number // 0 = covered by a row above
  paramSpan: number
  cause: Cause | null
  causeNo: string
  causeSpan: number
  cons: Consequence | null
  consNo: string
}

function layout(deviations: Deviation[]): Row[] {
  const rows: Row[] = []
  ordered(deviations).forEach((dev, i) => {
    const devNo = `${i + 1}`
    const start = rows.length
    const causes = dev.causes.length ? dev.causes : [null]
    causes.forEach((cause, j) => {
      const causeNo = cause ? `${devNo}.${j + 1}` : ''
      const conses = cause?.consequences.length ? cause.consequences : [null]
      conses.forEach((cons, k) => {
        rows.push({ dev, devNo, devSpan: 0, paramSpan: 0, cause, causeNo, causeSpan: k === 0 ? conses.length : 0, cons, consNo: cons ? `${causeNo}.${k + 1}` : '' })
      })
    })
    rows[start].devSpan = rows.length - start
  })
  // a parameter cell spans every row of consecutive deviations with that parameter
  for (let i = 0; i < rows.length; ) {
    let j = i
    while (j < rows.length && rows[j].dev.parameter.toLowerCase() === rows[i].dev.parameter.toLowerCase()) j++
    rows[i].paramSpan = j - i
    i = j
  }
  return rows
}

export default function HazopWorksheet({ node, studyId, rightOffset, onClose, onRowsChanged, packageMissing, onOpenPackage }: HazopWorksheetProps) {
  const nodeId = worksheetNodeId(node)
  const [deviations, setDeviations] = useState<Deviation[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [generated, setGenerated] = useState<GenerateResult | null>(null)

  const hazop = node.shapeType === 'hazop' ? node.hazop : undefined
  const rows = useMemo(() => layout(deviations), [deviations])

  const commit = useCallback(
    (next: Deviation[]) => {
      setDeviations(next)
      onRowsChanged(nodeId, next.length)
    },
    [nodeId, onRowsChanged],
  )

  useEffect(() => {
    if (!studyId) return
    let cancelled = false
    getWorksheet(studyId, nodeId)
      .then((loaded) => {
        if (cancelled) return
        commit(loaded)
        setError(null)
      })
      .catch((err: Error) => !cancelled && setError(`${err.message}. Is the agent server running (\`uv run hazop-agent serve\` in agent/)?`))
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [studyId, nodeId, commit])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !(document.activeElement instanceof HTMLTextAreaElement)) onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  /** Runs a change; the server returns the refreshed deviation(s), which replace the local copy. */
  const change = async (action: () => Promise<Deviation | Deviation[] | undefined>, removedDeviation?: string) => {
    setBusy(true)
    try {
      const result = await action()
      if (removedDeviation) commit(deviations.filter((d) => d.id !== removedDeviation))
      else if (Array.isArray(result)) commit([...deviations, ...result])
      else if (result) {
        const known = deviations.some((d) => d.id === result.id)
        commit(known ? deviations.map((d) => (d.id === result.id ? result : d)) : [...deviations, result])
      }
      setError(null)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const edit = (kind: ItemKind, id: string, fields: Record<string, string | number | null>) => change(() => updateItem(kind, id, fields))

  const remove = (kind: ItemKind, id: string, what: string, dev: Deviation) => {
    const cascades = kind === 'deviations' || kind === 'causes' || kind === 'consequences'
    if (cascades && !window.confirm(`Delete this ${what} and everything under it?`)) return
    if (kind === 'deviations') return change(() => deleteItem(kind, id).then(() => undefined), id)
    // reload the deviation after deleting something inside it
    return change(async () => {
      await deleteItem(kind, id)
      return (await getWorksheet(studyId!, nodeId)).find((d) => d.id === dev.id)
    })
  }

  /** Step 4: the agent drafts this node's worksheet and adds it. */
  const generate = async (replace: boolean) => {
    if (replace && !window.confirm(`Delete this node's ${deviations.length} deviations and replace them with a new draft?`)) return
    setBusy(true)
    setGenerating(true)
    setGenerated(null)
    try {
      const result = await generateWorksheet(studyId!, nodeId, replace)
      commit(result.deviations)
      setGenerated(result)
      setError(null)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
      setGenerating(false)
    }
  }

  const addDeviation = () =>
    change(async () => (await createDeviations(studyId!, nodeId, node.label, [{ parameter: 'Flow', guideword: 'NO/LESS', deviation: '', causes: [] }]))[0])

  const exportCsv = () => {
    const url = URL.createObjectURL(new Blob([worksheetToCsv(node.label, ordered(deviations))], { type: 'text/csv' }))
    const a = document.createElement('a')
    a.href = url
    a.download = `${studyId ?? 'drawing'}-${nodeId.replace(/[^a-z0-9-]+/gi, '_')}-hazop.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  const consequences = deviations.flatMap((d) => d.causes.flatMap((c) => c.consequences))
  const openRecs = consequences.flatMap((q) => q.recommendations).filter((r) => r.status !== 'closed').length
  const highRisk = consequences.filter((q) => ['high', 'very-high'].includes(riskOf(q)?.band ?? '')).length

  return (
    <div className="hazop-ws-backdrop" style={{ right: rightOffset }} onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="hazop-ws" role="dialog" aria-modal="true" aria-label={`HAZOP worksheet for ${node.label}`}>
        <header className="hazop-ws-header">
          <span className="hazop-swatch" style={{ background: pickNodeColor(node.colorIndex) }} />
          <div className="hazop-ws-title">
            <h2>
              Node {node.nodeNumber} · {node.label}
            </h2>
            <span className="hazop-muted">
              page {node.pageIndex + 1} · {nodeId} · study {studyId ?? '…'}
            </span>
          </div>
          <button className="hazop-close" onClick={onClose} aria-label="Close worksheet" title="Close (Esc)">
            ×
          </button>
        </header>

        {hazop && (
          <details className="hazop-context">
            <summary>Node context from the pipeline</summary>
            <dl>
              <dt>Design intent</dt>
              <dd>{hazop.designIntent || '—'}</dd>
              <dt>Design conditions</dt>
              <dd>
                {hazop.designConditions.pressure} · {hazop.designConditions.temperature} · {hazop.designConditions.material}
              </dd>
              <dt>Equipment</dt>
              <dd>{hazop.equipmentTags.join(', ') || '—'}</dd>
              {hazop.parameters.map((p) => (
                <Fragment key={`${p.parameter}:${p.name}`}>
                  <dt>{p.name || p.parameter}</dt>
                  <dd>
                    HIGH: {p.highSafeguards.join('; ') || '—'} · LOW: {p.lowSafeguards.join('; ') || '—'}
                  </dd>
                </Fragment>
              ))}
              <dt>Interlocks</dt>
              <dd>{hazop.interlockRefs.join(', ') || '—'}</dd>
            </dl>
          </details>
        )}

        <div className="hazop-toolbar">
          <button className="hazop-btn-primary" onClick={addDeviation} disabled={busy || !studyId || loading}>
            Add deviation
          </button>
          <button
            onClick={() => generate(false)}
            disabled={busy || !studyId || loading}
            title="Draft this node's deviations, causes (with their valve / line tags), consequences, safeguards and recommendations from the drawing, the C&E table and the study package. Deviations already here are kept."
          >
            {generating ? 'Generating…' : 'Generate worksheet'}
          </button>
          {deviations.length > 0 && (
            <button onClick={() => generate(true)} disabled={busy || !studyId || loading} title="Delete this node's deviations and draft them again">
              Replace with new draft
            </button>
          )}
          <button onClick={exportCsv} disabled={deviations.length === 0}>
            Export CSV
          </button>
          <span className="hazop-summary">
            {deviations.length} deviations · {consequences.length} consequences · {openRecs} open recommendations · {highRisk} high risk
          </span>
        </div>

        {packageMissing && packageMissing.length > 0 && (
          <div className="hazop-package-warning">
            <span>
              Study package incomplete: no {packageMissing.join(', ')}. HAZOP teams ask for these before starting; without them causes,
              safeguards and set values come from the P&ID alone.
            </span>
            {onOpenPackage && <button onClick={onOpenPackage}>Open study package</button>}
          </div>
        )}
        {error && <div className="hazop-error">{error}</div>}
        {generating && (
          <div className="hazop-generate-note">
            Drafting the worksheet for this node from the drawing, the C&E table and the study package. This usually takes 1–3 minutes.
          </div>
        )}
        {generated && (
          <div className="hazop-generate-note">
            <div>
              Added {generated.added} drafted deviation{generated.added === 1 ? '' : 's'}
              {generated.skipped > 0 && <> ({generated.skipped} already in the worksheet, skipped)</>}. Review every row: severity, likelihood
              and owners are for the team.
              <button className="ws-del" onClick={() => setGenerated(null)} aria-label="Dismiss">
                ×
              </button>
            </div>
            {[...generated.warnings, ...(generated.notes ? [generated.notes] : [])].map((w, i) => (
              <div key={i} className="hazop-generate-warning">
                {w}
              </div>
            ))}
          </div>
        )}

        <div className="hazop-table-wrap">
          <datalist id="hazop-parameters">
            {PARAMETERS.map((p) => (
              <option key={p} value={p} />
            ))}
          </datalist>
          <table className="hazop-table ws-grid">
            <thead>
              <tr>
                <th>Parameter</th>
                <th>Guide word</th>
                <th>Deviation</th>
                <th>Cause</th>
                <th>Consequence</th>
                <th>Effective safeguards</th>
                <th title="Severity, 1–5">S</th>
                <th title="Likelihood, 1–5">L</th>
                <th title="Risk rating, S × L (example 5×5 matrix)">RR</th>
                <th>Recommendations</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const { dev, cause, cons } = r
                const risk = cons ? riskOf(cons) : null
                return (
                  <tr key={`${dev.id}:${cause?.id ?? '-'}:${cons?.id ?? '-'}`} className={r.devSpan ? 'ws-dev-start' : ''}>
                    {r.paramSpan > 0 && (
                      <td rowSpan={r.paramSpan} className="ws-param">
                        {dev.parameter}
                      </td>
                    )}
                    {r.devSpan > 0 && (
                      <>
                        <td rowSpan={r.devSpan} className="ws-guide">
                          <input
                            key={dev.parameter}
                            className="ws-input"
                            aria-label="Parameter"
                            list="hazop-parameters"
                            defaultValue={dev.parameter}
                            onBlur={(e) => e.target.value.trim() && e.target.value.trim() !== dev.parameter && edit('deviations', dev.id, { parameter: e.target.value.trim() })}
                          />
                          <select aria-label="Guide word" value={dev.guideword} onChange={(e) => edit('deviations', dev.id, { guideword: e.target.value })}>
                            {[...new Set([...GUIDEWORDS, dev.guideword].filter(Boolean))].map((g) => (
                              <option key={g} value={g}>
                                {g}
                              </option>
                            ))}
                          </select>
                        </td>
                        <td rowSpan={r.devSpan} className="ws-dev">
                          <span className="ws-no">{r.devNo}.</span>
                          <EditableText value={dev.deviation} placeholder="Describe the deviation" onSave={(v) => edit('deviations', dev.id, { deviation: v })} />
                          <div className="ws-actions">
                            <button onClick={() => change(() => addCause(dev.id))} disabled={busy}>
                              + cause
                            </button>
                            <button className="ws-del" onClick={() => remove('deviations', dev.id, 'deviation', dev)} disabled={busy} aria-label={`Delete deviation ${r.devNo}`}>
                              ×
                            </button>
                          </div>
                        </td>
                      </>
                    )}
                    {r.causeSpan > 0 && (
                      <td rowSpan={r.causeSpan} className="ws-cause">
                        {cause ? (
                          <>
                            <span className="ws-no">{r.causeNo}.</span>
                            <EditableText value={cause.text} placeholder="Describe the cause" onSave={(v) => edit('causes', cause.id, { text: v })} />
                            <div className="ws-actions">
                              <button onClick={() => change(() => addConsequence(cause.id))} disabled={busy}>
                                + consequence
                              </button>
                              <button className="ws-del" onClick={() => remove('causes', cause.id, 'cause', dev)} disabled={busy} aria-label={`Delete cause ${r.causeNo}`}>
                                ×
                              </button>
                            </div>
                          </>
                        ) : (
                          <span className="hazop-muted">Add a cause with “+ cause”.</span>
                        )}
                      </td>
                    )}
                    <td className="ws-cons">
                      {cons ? (
                        <>
                          <span className="ws-no">{r.consNo}.</span>
                          <EditableText value={cons.text} placeholder="Describe the consequence" onSave={(v) => edit('consequences', cons.id, { text: v })} />
                          <div className="ws-actions">
                            <button className="ws-del" onClick={() => remove('consequences', cons.id, 'consequence', dev)} disabled={busy} aria-label={`Delete consequence ${r.consNo}`}>
                              ×
                            </button>
                          </div>
                        </>
                      ) : cause ? (
                        <span className="hazop-muted">Add one with “+ consequence”.</span>
                      ) : null}
                    </td>
                    <td className="ws-list">
                      {cons && (
                        <>
                          {cons.safeguards.map((s, m) => (
                            <div key={s.id} className="ws-item">
                              <span className="ws-no">
                                {r.consNo}.{m + 1}.
                              </span>
                              <EditableText value={s.text} placeholder="Safeguard" onSave={(v) => edit('safeguards', s.id, { text: v })} />
                              <button className="ws-del" onClick={() => remove('safeguards', s.id, 'safeguard', dev)} disabled={busy} aria-label="Delete safeguard">
                                ×
                              </button>
                            </div>
                          ))}
                          <button className="ws-add" onClick={() => change(() => addSafeguard(cons.id))} disabled={busy}>
                            + safeguard
                          </button>
                        </>
                      )}
                    </td>
                    <td className="ws-num">{cons && <Rating label="Severity" value={cons.severity} onChange={(v) => edit('consequences', cons.id, { severity: v })} />}</td>
                    <td className="ws-num">{cons && <Rating label="Likelihood" value={cons.likelihood} onChange={(v) => edit('consequences', cons.id, { likelihood: v })} />}</td>
                    <td className={`ws-num ${risk ? `ws-risk ws-risk-${risk.band}` : ''}`}>{risk ? `${risk.label} (${risk.score})` : cons ? '—' : ''}</td>
                    <td className="ws-list">
                      {cons && (
                        <>
                          {cons.recommendations.map((rec) => (
                            <div key={rec.id} className="ws-item ws-rec">
                              <EditableText value={rec.text} placeholder="Recommendation" onSave={(v) => edit('recommendations', rec.id, { text: v })} />
                              <div className="ws-rec-meta">
                                <EditableText value={rec.owner} placeholder="Owner" className="ws-owner" onSave={(v) => edit('recommendations', rec.id, { owner: v })} />
                                <select aria-label="Status" value={rec.status} onChange={(e) => edit('recommendations', rec.id, { status: e.target.value })}>
                                  {RECOMMENDATION_STATUSES.map((st) => (
                                    <option key={st} value={st}>
                                      {st}
                                    </option>
                                  ))}
                                </select>
                                <button className="ws-del" onClick={() => remove('recommendations', rec.id, 'recommendation', dev)} disabled={busy} aria-label="Delete recommendation">
                                  ×
                                </button>
                              </div>
                            </div>
                          ))}
                          <button className="ws-add" onClick={() => change(() => addRecommendation(cons.id))} disabled={busy}>
                            + recommendation
                          </button>
                        </>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          {!loading && deviations.length === 0 && (
            <div className="hazop-empty">
              No deviations yet. Use “Generate worksheet” to draft them from the drawing, or add them one by one.
            </div>
          )}
          {loading && <div className="hazop-empty">Loading…</div>}
        </div>
      </div>
    </div>
  )
}
