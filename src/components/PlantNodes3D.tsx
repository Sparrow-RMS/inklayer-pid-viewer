import { useEffect, useMemo, useRef, useState } from 'react'
import type { ReviewedNode } from './ChatPanel'
import { pickNodeColor } from '../lib/annotationFromNode'
import { getPlantScene, SceneNotReadyError, sizePlantScene, type PlantScene } from '../lib/plantSceneApi'
import { PlantSceneView, type PickedObject, type SceneNode } from '../lib/plantScene3d'
import './PlantNodes3D.css'

export interface PlantNodes3DProps {
  /** The drawing's job id (studyId); null while it is still being computed. */
  jobId: string | null
  fileName: string
  /** Every node on the viewer's cards; rejected ones are left uncoloured. */
  nodes: ReviewedNode[]
  /** Room to leave on the right for the chat panel, in px. */
  rightOffset: number
  onClose: () => void
}

/** One entry in the node list: merged nodes share their target's number and colour. */
interface NodeGroup {
  key: string
  nodeNumber: number
  label: string
  color: string
  memberIds: string[]
  members: number
}

type Sizing = 'idle' | 'running' | 'failed'

function groupNodes(nodes: ReviewedNode[], pageIndex: number): NodeGroup[] {
  const groups = new Map<number, NodeGroup>()
  for (const node of nodes) {
    if (node.status === 'rejected' || node.pageIndex !== pageIndex || node.shapeType !== 'hazop') continue
    const ids = [...(node.entityIds ?? []), ...node.routes.map((r) => r.entityId)]
    const group = groups.get(node.nodeNumber)
    if (group) {
      group.memberIds.push(...ids)
      group.members += 1
      // The merge target names the group.
      if (!node.mergedInto) group.label = node.label
    } else {
      groups.set(node.nodeNumber, {
        key: String(node.nodeNumber),
        nodeNumber: node.nodeNumber,
        label: node.label,
        color: pickNodeColor(node.colorIndex),
        memberIds: ids,
        members: 1,
      })
    }
  }
  return [...groups.values()].sort((a, b) => a.nodeNumber - b.nodeNumber)
}

// The pipeline's own entities and traced pipes in 3D, with each HAZOP node's
// members in the node's colour (agent/src/hazop_agent/plant_scene.py). Separate
// from the 3D view (Plant3DView), whose page Claude writes freely and which
// therefore can't show nodes.
export default function PlantNodes3D({ jobId, fileName, nodes, rightOffset, onClose }: PlantNodes3DProps) {
  const canvasRef = useRef<HTMLDivElement>(null)
  const viewRef = useRef<PlantSceneView | null>(null)
  const [scene, setScene] = useState<PlantScene | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notReady, setNotReady] = useState<string | null>(null)
  const [sizing, setSizing] = useState<Sizing>('idle')
  const [sizingError, setSizingError] = useState<string | null>(null)
  const [pageIndex, setPageIndex] = useState(0)
  const [focused, setFocused] = useState<string | null>(null)
  const [picked, setPicked] = useState<PickedObject | null>(null)

  const page = scene?.pages.find((p) => p.pageIndex === pageIndex) ?? scene?.pages[0] ?? null
  const groups = useMemo(() => groupNodes(nodes, page?.pageIndex ?? 0), [nodes, page?.pageIndex])

  // Load the scene; if Claude hasn't sized the equipment yet, draw it with the
  // defaults now and ask Claude in the background.
  useEffect(() => {
    if (!jobId) return
    let cancelled = false
    void (async () => {
      try {
        const loaded = await getPlantScene(jobId)
        if (cancelled) return
        setScene(loaded)
        if (loaded.forms && !loaded.formsStale) return
        setSizing('running')
        try {
          const sized = await sizePlantScene(jobId)
          if (!cancelled) {
            setScene(sized)
            setSizing('idle')
          }
        } catch (err) {
          if (!cancelled) {
            setSizing('failed')
            setSizingError(err instanceof Error ? err.message : String(err))
          }
        }
      } catch (err) {
        if (cancelled) return
        if (err instanceof SceneNotReadyError) setNotReady(err.message)
        else setError(err instanceof Error ? err.message : String(err))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [jobId])

  // The renderer lives as long as the window.
  useEffect(() => {
    if (!canvasRef.current) return
    const view = new PlantSceneView(canvasRef.current)
    view.onPick = setPicked
    viewRef.current = view
    return () => {
      view.dispose()
      viewRef.current = null
    }
  }, [])

  // A new page, or the same page re-sized by Claude (keep the camera then).
  const loadedPage = useRef<number | null>(null)
  useEffect(() => {
    if (!page || !viewRef.current) return
    viewRef.current.load(page, loadedPage.current === page.pageIndex)
    loadedPage.current = page.pageIndex
  }, [page])

  useEffect(() => {
    viewRef.current?.setNodes(groups.map((g): SceneNode => ({ key: g.key, color: g.color, memberIds: g.memberIds })))
  }, [groups, page])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const focus = (key: string | null) => {
    setFocused(key)
    viewRef.current?.focus(key)
  }

  const resize = async (force: boolean) => {
    if (!jobId) return
    setSizing('running')
    setSizingError(null)
    try {
      setScene(await sizePlantScene(jobId, force))
      setSizing('idle')
    } catch (err) {
      setSizing('failed')
      setSizingError(err instanceof Error ? err.message : String(err))
    }
  }

  const pickedItem = picked && page ? page.items.find((i) => i.id === picked.id) : null
  const pickedSpan = picked && page ? page.spans.find((s) => s.id === picked.id) : null
  const pickedGroups = picked ? groups.filter((g) => g.memberIds.includes(picked.id)) : []
  const unmapped = scene && groups.length === 0

  return (
    <div className="p3n-backdrop" style={{ right: rightOffset }}>
      <div className="p3n" role="dialog" aria-modal="true" aria-label="HAZOP nodes in 3D">
        <header className="p3n-header">
          <div className="p3n-title">
            <h2>Nodes in 3D</h2>
            <span className="p3n-muted">
              {fileName} · the drawing's own items and traced pipes, laid out as on the sheet;{' '}
              {scene?.forms ? `equipment sized by ${scene.forms.model}` : 'equipment sizes approximate'}
            </span>
          </div>
          {scene && scene.pages.length > 1 && (
            <select className="p3n-button" value={page?.pageIndex} onChange={(e) => setPageIndex(Number(e.target.value))} aria-label="Page">
              {scene.pages.map((p) => (
                <option key={p.pageIndex} value={p.pageIndex}>
                  Page {p.pageIndex + 1}
                </option>
              ))}
            </select>
          )}
          <button
            className="p3n-button"
            disabled={!scene || sizing === 'running'}
            onClick={() => window.confirm('Ask Claude to size the equipment again? This makes a new Claude call.') && void resize(true)}
          >
            Re-size equipment
          </button>
          <button className="p3n-close" onClick={onClose} aria-label="Close" title="Close (Esc)">
            ×
          </button>
        </header>

        {sizing === 'running' && <div className="p3n-banner">Claude is sizing the equipment… shapes and heights are approximate until it finishes.</div>}
        {sizing === 'failed' && (
          <div className="p3n-banner p3n-banner-error">
            Couldn't size the equipment ({sizingError}). Showing default sizes.{' '}
            <button className="p3n-link" onClick={() => void resize(false)}>
              Try again
            </button>
          </div>
        )}

        <div className="p3n-body">
          <aside className="p3n-side">
            <div className="p3n-side-head">
              <strong>Nodes</strong>
              <button className="p3n-link" onClick={() => focus(null)} disabled={focused === null}>
                Show all
              </button>
            </div>
            {unmapped && <p className="p3n-muted p3n-pad">No nodes on this page yet. Run the HAZOP pipeline to colour them here.</p>}
            <ul className="p3n-nodes">
              {groups.map((g) => (
                <li key={g.key}>
                  <button className={`p3n-node${focused === g.key ? ' p3n-node-active' : ''}`} onClick={() => focus(focused === g.key ? null : g.key)}>
                    <span className="p3n-swatch" style={{ background: g.color }} />
                    <span className="p3n-node-text">
                      <span className="p3n-node-name">Node {g.nodeNumber}</span>
                      <span className="p3n-muted">{g.label}</span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
            <p className="p3n-muted p3n-pad p3n-legend">Grey: not in any node. Drag to orbit, scroll to zoom, click an item for details.</p>
          </aside>

          <div className="p3n-stage">
            <div ref={canvasRef} className="p3n-canvas" />
            {notReady && <div className="p3n-overlay">{notReady}</div>}
            {error && <div className="p3n-overlay p3n-error">{error}</div>}
            {!scene && !notReady && !error && <div className="p3n-overlay">Loading the plant…</div>}
            {picked && (pickedItem || pickedSpan) && (
              <div className="p3n-info">
                <button className="p3n-close p3n-info-close" onClick={() => setPicked(null)} aria-label="Close details">
                  ×
                </button>
                {pickedItem ? (
                  <>
                    <strong>{pickedItem.tag ?? '(no tag)'}</strong>
                    <span>{pickedItem.type}</span>
                    <span className="p3n-muted">
                      {pickedItem.category}
                      {pickedItem.category === 'equipment' &&
                        ` · ~${pickedItem.diameterM.toFixed(1)} m × ${pickedItem.heightM.toFixed(1)} m (${pickedItem.sizedBy === 'claude' ? 'sized by Claude' : 'default size'})`}
                    </span>
                  </>
                ) : (
                  <>
                    <strong>Pipe {pickedSpan!.lineNumbers.join(', ') || '(no line number)'}</strong>
                    <span className="p3n-muted">traced span {pickedSpan!.id}</span>
                  </>
                )}
                <span>
                  {pickedGroups.length
                    ? pickedGroups.map((g) => (
                        <button key={g.key} className="p3n-chip" style={{ borderColor: g.color }} onClick={() => focus(g.key)}>
                          Node {g.nodeNumber}
                        </button>
                      ))
                    : <span className="p3n-muted">Not in any node</span>}
                </span>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
