import { useEffect, useRef } from 'react'
import { pickNodeColor } from '../lib/annotationFromNode'
import './NodeMergeMenu.css'

export interface MergeTarget {
  nodeNumber: number
  colorIndex: number
  label: string
}

export interface NodeMergeMenuProps {
  x: number
  y: number
  sourceLabel: string
  targets: MergeTarget[]
  onSelect: (target: MergeTarget) => void
  onClose: () => void
}

export default function NodeMergeMenu({ x, y, sourceLabel, targets, onSelect, onClose }: NodeMergeMenuProps) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const handlePointerDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('mousedown', handlePointerDown, true)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('mousedown', handlePointerDown, true)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [onClose])

  // Keep the menu on-screen near the click point rather than off the edge.
  const clampedLeft = Math.min(x, window.innerWidth - 260)
  const clampedTop = Math.min(y, window.innerHeight - 40 - targets.length * 36)

  return (
    <div ref={ref} className="node-merge-menu" style={{ left: clampedLeft, top: clampedTop }}>
      <div className="node-merge-menu-header">Merge {sourceLabel} into…</div>
      {targets.length === 0 ? (
        <div className="node-merge-menu-empty">No other nodes to merge into yet.</div>
      ) : (
        targets.map((t) => (
          <button key={t.nodeNumber} className="node-merge-menu-item" onClick={() => onSelect(t)}>
            <span className="node-merge-menu-swatch" style={{ background: pickNodeColor(t.colorIndex) }} />
            <span className="node-merge-menu-label">
              Node {t.nodeNumber} — {t.label}
            </span>
          </button>
        ))
      )}
    </div>
  )
}
