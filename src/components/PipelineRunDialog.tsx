import { useEffect, useRef, useState } from 'react'
import './PipelineRunDialog.css'

export type RunPhase = 'running' | 'done' | 'failed'

export interface PipelineRun {
  open: boolean
  phase: RunPhase
  startedAt: number
  finishedAt?: number
  /** The agent's progress lines, oldest first. */
  lines: string[]
  /** step -> 'computed' | 'replayed' | 'cached', as each step reports. */
  steps: Record<string, string>
  error?: string
  consolidating: boolean
}

// TODO(company): replace with Sparrow's real copy, logo and link.
const COMPANY = {
  name: 'Sparrow',
  tagline: 'Process safety, structured.',
  about: 'Add a two-line description of Sparrow here: who you are, who you serve and what you are building for process safety teams.',
  url: '#',
}

const SLIDES = [
  { icon: 'account_tree', title: 'Nodes, not guesswork', text: 'Inklayer traces whole pipe runs and paints each HAZOP node directly on your drawing.' },
  { icon: 'table_chart', title: 'Worksheets in minutes', text: 'Deviations, causes, consequences, safeguards and recommendations, drafted for every node.' },
  { icon: 'view_in_ar', title: 'Your shop floor in 3D', text: 'Open 3D view to walk the plant drawn from this P&ID, or Nodes in 3D to see every HAZOP node on it.' },
  { icon: 'forum', title: 'Your team stays in charge', text: 'Accept, merge and rename nodes by hand or just ask the assistant.' },
  { icon: 'domain', title: `Built by ${COMPANY.name}`, text: COMPANY.about },
]

const STAGES = [
  { key: 'step1', label: 'Reading the drawing', detail: 'Symbols, text and line work, tile by tile' },
  { key: 'network', label: 'Tracing the pipe network', detail: 'Following every drawn line between equipment' },
  { key: 'step2', label: 'Classifying processes', detail: 'Grouping equipment into process functions' },
  { key: 'sheet', label: 'Reading the C&E table', detail: 'Interlocks and trips marked on the sheet' },
  { key: 'step3', label: 'Building HAZOP nodes', detail: 'Proposing nodes and checking them against the rules' },
  { key: 'consolidate', label: 'Consolidating nodes', detail: 'Merging down to your max nodes per page' },
]

function Icon({ name }: { name: string }) {
  return <span aria-hidden="true" className="run-symbol">{name}</span>
}

function formatElapsed(ms: number) {
  const seconds = Math.max(0, Math.floor(ms / 1000))
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

/** "[step1] page 0: ..." -> a tag and the rest, so the log can style the tag. */
function splitLine(line: string) {
  const match = /^\s*\[(\w+)\]\s*(.*)$/.exec(line)
  if (match) return { tag: match[1], text: match[2], kind: 'head' }
  const text = line.trim()
  if (text.startsWith('!')) return { tag: null, text: text.slice(1).trim(), kind: 'error' }
  if (text.startsWith('->')) return { tag: null, text: text.slice(2).trim(), kind: 'result' }
  return { tag: null, text, kind: 'detail' }
}

// Shown over the viewer while a fresh (uncached) pipeline run is working:
// the agent's live progress on one side, the company's story on the other.
export default function PipelineRunDialog({ run, onClose }: { run: PipelineRun; onClose: () => void }) {
  const [now, setNow] = useState(() => Date.now())
  const [slide, setSlide] = useState(0)
  const logRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (run.phase !== 'running') return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [run.phase])

  useEffect(() => {
    const timer = window.setInterval(() => setSlide((s) => (s + 1) % SLIDES.length), 6000)
    return () => window.clearInterval(timer)
  }, [slide])

  useEffect(() => {
    const log = logRef.current
    if (log) log.scrollTop = log.scrollHeight
  }, [run.lines.length])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const stages = STAGES.filter((s) => s.key !== 'consolidate' || run.consolidating)
  const firstPending = stages.findIndex((s) => !run.steps[s.key])
  const doneCount = stages.filter((s) => run.steps[s.key]).length
  const progress = run.phase === 'done' ? 100 : Math.round((doneCount / stages.length) * 100)
  const elapsed = (run.finishedAt ?? now) - run.startedAt
  const current = SLIDES[slide]

  return (
    <div className="run-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <section aria-labelledby="run-title" aria-modal="true" className="run-dialog" role="dialog">
        <aside className="run-brand">
          <div aria-hidden="true" className="run-brand-glow" />
          <div aria-label={`${COMPANY.name} logo placeholder`} className="run-logo-slot" role="img">SPARROW LOGO</div>

          <div className="run-slide" key={slide}>
            <span className="run-slide-icon"><Icon name={current.icon} /></span>
            <h3>{current.title}</h3>
            <p>{current.text}</p>
          </div>

          <div className="run-dots" role="tablist" aria-label="About Inklayer">
            {SLIDES.map((s, i) => (
              <button aria-label={s.title} aria-selected={i === slide} className={i === slide ? 'active' : ''} key={s.title} onClick={() => setSlide(i)} role="tab" type="button" />
            ))}
          </div>

          <footer className="run-brand-footer">
            <span>{COMPANY.tagline}</span>
            <a href={COMPANY.url} rel="noreferrer" target="_blank">Learn more about {COMPANY.name}<Icon name="arrow_outward" /></a>
          </footer>
        </aside>

        <div className="run-main">
          <header className="run-header">
            <span className={`run-status-icon phase-${run.phase}`}>
              <Icon name={run.phase === 'done' ? 'check' : run.phase === 'failed' ? 'error' : 'auto_awesome'} />
            </span>
            <div>
              <h2 id="run-title">
                {run.phase === 'done' ? 'Your HAZOP nodes are ready' : run.phase === 'failed' ? 'The pipeline stopped' : 'Analysing your P&ID'}
              </h2>
              <p>
                {run.phase === 'done'
                  ? 'They are painted on the drawing and listed in the assistant.'
                  : run.phase === 'failed'
                    ? run.error
                    : 'First runs take a few minutes. You can close this; the run keeps going.'}
              </p>
            </div>
            <button aria-label="Close" className="run-close" onClick={onClose} type="button"><Icon name="close" /></button>
          </header>

          <div className="run-progress">
            <div className="run-progress-bar"><span className={`phase-${run.phase}`} style={{ width: `${Math.max(progress, run.phase === 'running' ? 4 : 0)}%` }} /></div>
            <span className="run-elapsed"><Icon name="schedule" />{formatElapsed(elapsed)}</span>
          </div>

          <ol className="run-stages">
            {stages.map((stage, i) => {
              const state = run.steps[stage.key] ? 'done' : run.phase === 'running' && i === firstPending ? 'active' : run.phase === 'failed' && i === firstPending ? 'failed' : 'pending'
              return (
                <li className={`stage-${state}`} key={stage.key}>
                  <span className="run-stage-dot">{state === 'done' ? <Icon name="check" /> : state === 'failed' ? <Icon name="close" /> : null}</span>
                  <span className="run-stage-copy">
                    <strong>{stage.label}</strong>
                    <small>{state === 'done' && run.steps[stage.key] !== 'computed' ? `${stage.detail} · reused` : stage.detail}</small>
                  </span>
                </li>
              )
            })}
          </ol>

          <div className="run-log-head"><span className={run.phase === 'running' ? 'live' : ''}><i />Agent stream</span><small>{run.lines.length} events</small></div>
          <div aria-live="polite" className="run-log" ref={logRef}>
            {run.lines.length === 0 && <div className="run-log-line kind-detail">Uploading the drawing and warming up the agent…</div>}
            {run.lines.map((line, i) => {
              const { tag, text, kind } = splitLine(line)
              return (
                <div className={`run-log-line kind-${kind}`} key={i}>
                  {tag && <span className="run-log-tag">{tag}</span>}
                  {kind === 'result' && <span className="run-log-arrow">→</span>}
                  <span>{text}</span>
                </div>
              )
            })}
            {run.phase === 'running' && <div className="run-log-line kind-thinking"><span>thinking</span><span className="run-cursor" /></div>}
          </div>

          {run.phase !== 'running' && (
            <footer className="run-actions">
              <button className={run.phase === 'done' ? 'run-primary' : 'run-secondary'} onClick={onClose} type="button">
                {run.phase === 'done' ? <>View nodes<Icon name="arrow_forward" /></> : 'Close'}
              </button>
            </footer>
          )}
        </div>
      </section>
    </div>
  )
}
