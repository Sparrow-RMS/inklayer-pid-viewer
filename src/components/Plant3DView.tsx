import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { generatePlant3d } from '../lib/plant3dApi'
import ConstructionSite from './ConstructionSite'
import { progressFor, siteStage, SITE_STAGES } from '../lib/constructionProgress'
// The building state reuses the pipeline run dialog's header, progress bar,
// stage list and stream box, so the two screens stay one look.
import './PipelineRunDialog.css'
import './Plant3DView.css'

export interface Plant3DViewProps {
  pdf: ArrayBuffer
  fileName: string
  /** Hidden rather than unmounted when closed, so a generation in flight keeps going. */
  open: boolean
  /** Room to leave on the right for the chat panel, in px. */
  rightOffset: number
  onClose: () => void
  /** Reports whether a generation is running, for the toolbar button. */
  onBusyChange?: (busy: boolean) => void
}

type State =
  | { phase: 'idle' }
  | {
      phase: 'running'
      startedAt: number
      chars: number
      thinking: string
      /** Streamed events so far: each one sets something off on the construction site. */
      activity: number
      latestTag: string | null
    }
  | { phase: 'ready'; url: string }
  | { phase: 'failed'; message: string }

// The reasoning kept for the panel; past this the oldest is dropped.
const MAX_THINKING_CHARS = 40000
// Equipment-style tags (ST-V303, P-V303A, VES-V305B) in the reasoning, for the site's callouts.
const TAG = /\b[A-Z]{1,4}-[A-Z]{0,3}-?\d{2,5}[A-Z]?\b/g

function formatElapsed(ms: number) {
  const seconds = Math.max(0, Math.floor(ms / 1000))
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

function Icon({ name }: { name: string }) {
  return <span aria-hidden="true" className="run-symbol">{name}</span>
}

// The drawing as a 3D plant, drawn by Sparrow (agent/src/hazop_agent/plant3d.py).
// The page is model-written code, so its iframe gets scripts only: no access to
// this app's origin, storage or cookies.
export default function Plant3DView({ pdf, fileName, open, rightOffset, onClose, onBusyChange }: Plant3DViewProps) {
  const [state, setState] = useState<State>({ phase: 'idle' })
  const [now, setNow] = useState(() => Date.now())
  // Bumped per page load, so a regenerated page at the same URL reloads.
  const [version, setVersion] = useState(0)
  const abortRef = useRef<AbortController | null>(null)

  const generate = useCallback(
    async (force: boolean) => {
      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      setState({ phase: 'running', startedAt: Date.now(), chars: 0, thinking: '', activity: 0, latestTag: null })
      try {
        let finished = false
        await generatePlant3d(
          pdf,
          fileName,
          force,
          (event) => {
            if (event.type === 'thinking') {
              const tags = event.text.match(TAG)
              setState((s) =>
                s.phase === 'running'
                  ? {
                      ...s,
                      thinking: (s.thinking + event.text).slice(-MAX_THINKING_CHARS),
                      activity: s.activity + 1,
                      latestTag: tags ? tags[tags.length - 1] : s.latestTag,
                    }
                  : s,
              )
            } else if (event.type === 'progress') {
              setState((s) => (s.phase === 'running' ? { ...s, chars: event.chars, activity: s.activity + 1 } : s))
            } else if (event.type === 'result') {
              finished = true
              setVersion((v) => v + 1)
              setState({ phase: 'ready', url: event.url })
            } else if (event.type === 'error') {
              finished = true
              setState({ phase: 'failed', message: event.message })
            }
          },
          controller.signal,
        )
        if (!finished) setState({ phase: 'failed', message: 'The agent server closed the connection before the 3D view was ready.' })
      } catch (err) {
        if (controller.signal.aborted) return
        setState({ phase: 'failed', message: err instanceof Error ? err.message : String(err) })
      }
    },
    [pdf, fileName],
  )

  // First open: show the cached page if there is one, else start drawing it.
  useEffect(() => {
    if (open && state.phase === 'idle') void generate(false)
  }, [open, state.phase, generate])

  useEffect(() => () => abortRef.current?.abort(), [])

  const running = state.phase === 'running'
  useEffect(() => {
    onBusyChange?.(running)
    // A new drawing unmounts this one, aborting its run.
    return () => onBusyChange?.(false)
  }, [running, onBusyChange])
  useEffect(() => {
    if (!running) return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [running])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  return (
    <div className="p3d-backdrop" style={{ right: rightOffset }} hidden={!open}>
      <div className="p3d" role="dialog" aria-modal="true" aria-label="3D plant view">
        <header className="p3d-header">
          <div className="p3d-title">
            <h2>3D plant view</h2>
            <span className="p3d-muted">
              {fileName} · drawn by Sparrow from the P&amp;ID; sizes, elevations and routing are indicative only
            </span>
          </div>
          {state.phase === 'ready' && (
            <a className="p3d-button" href={`${state.url}?v=${version}`} target="_blank" rel="noopener noreferrer">
              Open in new tab
            </a>
          )}
          <button
            className="p3d-button"
            onClick={() => {
              if (state.phase !== 'ready' || window.confirm('Draw the 3D view again from scratch? Sparrow will build a new one and replace the current view.')) {
                void generate(true)
              }
            }}
            disabled={running}
          >
            Regenerate
          </button>
          <button className="p3d-close" onClick={onClose} aria-label="Close 3D view" title="Close (Esc)">
            ×
          </button>
        </header>
        <div className="p3d-body">
          {state.phase === 'ready' && (
            <iframe
              key={version}
              className="p3d-frame"
              title={`3D plant view of ${fileName}`}
              src={`${state.url}?v=${version}`}
              sandbox="allow-scripts"
            />
          )}
          {running && (
            <div className="p3d-building">
              <section className="p3d-site" aria-label="Construction site">
                <ConstructionSite chars={state.chars} activity={state.activity} latestTag={state.latestTag} fileName={fileName} />
              </section>
              <BuildPanel
                thinking={state.thinking}
                chars={state.chars}
                elapsed={formatElapsed(now - state.startedAt)}
                onClose={onClose}
              />
            </div>
          )}
          {state.phase === 'failed' && (
            <div className="p3d-status">
              <p className="p3d-error">{state.message}</p>
              <button className="p3d-button" onClick={() => void generate(false)}>
                Try again
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

/** The build's progress and Sparrow's reasoning, in the pipeline run dialog's look.
 * The reasoning follows the newest text until the reader scrolls up, and again
 * once they scroll back to the bottom. Only the reader's own input stops it:
 * text arrives faster than scroll events, so a scroll event alone can't tell
 * the reader's scrolling from the panel's own. */
function BuildPanel({ thinking, chars, elapsed, onClose }: { thinking: string; chars: number; elapsed: string; onClose: () => void }) {
  const logRef = useRef<HTMLDivElement>(null)
  const following = useRef(true)

  useLayoutEffect(() => {
    const log = logRef.current
    if (log && following.current) log.scrollTop = log.scrollHeight
  }, [thinking])

  const progress = progressFor(chars)
  const stage = siteStage(progress)
  const paragraphs = thinking.trim() ? thinking.trim().split(/\n{2,}/) : []

  return (
    <aside className="p3d-panel run-theme" aria-label="Build progress and Sparrow reasoning">
      <header className="run-header">
        <span className="run-status-icon phase-running">
          <Icon name="view_in_ar" />
        </span>
        <div>
          <h2>Building your 3D shop floor</h2>
          <p>{chars ? `Writing the scene: ${(chars / 1000).toFixed(0)}k characters so far.` : 'Reading the drawing and planning the layout.'} You can close this; the build keeps going.</p>
        </div>
        <button aria-label="Close" className="run-close" onClick={onClose} type="button">
          <Icon name="close" />
        </button>
      </header>

      <div className="run-progress">
        <div className="run-progress-bar">
          <span className="phase-running" style={{ width: `${Math.max(4, Math.round(progress * 100))}%` }} />
        </div>
        <span className="run-elapsed">
          <Icon name="schedule" />
          {elapsed}
        </span>
      </div>

      <ol className="run-stages p3d-stages">
        {SITE_STAGES.map((s, i) => {
          const state = i < stage ? 'done' : i === stage ? 'active' : 'pending'
          return (
            <li className={`stage-${state}`} key={s.label}>
              <span className="run-stage-dot">{state === 'done' ? <Icon name="check" /> : null}</span>
              <span className="run-stage-copy">
                <strong>{s.label}</strong>
                <small>{s.detail}</small>
              </span>
            </li>
          )
        })}
      </ol>

      <div className="run-log-head">
        <span className="live">
          <i />
          Sparrow reasoning
        </span>
        <small>{paragraphs.length} {paragraphs.length === 1 ? 'thought' : 'thoughts'}</small>
      </div>
      <div
        ref={logRef}
        aria-live="polite"
        className="run-log p3d-log"
        onScroll={(e) => {
          const el = e.currentTarget
          if (el.scrollHeight - el.scrollTop - el.clientHeight < 24) following.current = true
        }}
        onWheel={(e) => {
          if (e.deltaY < 0) following.current = false
        }}
        onTouchMove={() => (following.current = false)}
        onKeyDown={(e) => {
          if (['ArrowUp', 'PageUp', 'Home'].includes(e.key)) following.current = false
        }}
        tabIndex={0}
      >
        {paragraphs.length === 0 && <div className="run-log-line kind-detail">Opening the drawing and warming up…</div>}
        {paragraphs.map((p, i) => (
          <div className={`run-log-line p3d-thought${i === paragraphs.length - 1 ? ' p3d-thought-latest' : ''}`} key={i}>
            <span>{p}</span>
          </div>
        ))}
        <div className="run-log-line kind-thinking">
          <span>thinking</span>
          <span className="run-cursor" />
        </div>
      </div>
    </aside>
  )
}
