import { useCallback, useEffect, useRef, useState } from 'react'
import { generatePlant3d } from '../lib/plant3dApi'
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
  | { phase: 'running'; startedAt: number; chars: number; thinking: string }
  | { phase: 'ready'; url: string }
  | { phase: 'failed'; message: string }

function formatElapsed(ms: number) {
  const seconds = Math.max(0, Math.floor(ms / 1000))
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

// The drawing as a 3D plant, drawn by Claude (agent/src/hazop_agent/plant3d.py).
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
      setState({ phase: 'running', startedAt: Date.now(), chars: 0, thinking: '' })
      try {
        let finished = false
        await generatePlant3d(
          pdf,
          fileName,
          force,
          (event) => {
            if (event.type === 'thinking') {
              // Keep only the tail: it's a "what is it doing now" line, not a log.
              setState((s) => (s.phase === 'running' ? { ...s, thinking: (s.thinking + event.text).slice(-600) } : s))
            } else if (event.type === 'progress') {
              setState((s) => (s.phase === 'running' ? { ...s, chars: event.chars } : s))
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
              {fileName} · drawn by Claude from the P&amp;ID; sizes, elevations and routing are indicative only
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
              if (state.phase !== 'ready' || window.confirm('Draw the 3D view again from scratch? This makes a new Claude call and replaces the current view.')) {
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
            <div className="p3d-status">
              <div className="p3d-spinner" aria-hidden="true" />
              <p>
                <strong>Claude is building the 3D plant…</strong> {formatElapsed(now - state.startedAt)}
              </p>
              <p className="p3d-muted">
                {state.chars
                  ? `Writing the scene: ${(state.chars / 1000).toFixed(0)}k characters so far`
                  : 'Reading the drawing and planning the layout. This usually takes a few minutes; you can close this and keep working.'}
              </p>
              {state.thinking && <p className="p3d-thinking">{state.thinking.trim()}</p>}
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
