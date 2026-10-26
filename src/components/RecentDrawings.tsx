import { useEffect, useState } from 'react'
import { listSavedDrawings, type SavedDrawing } from '../lib/libraryApi'
import './RecentDrawings.css'

export interface RecentDrawingsProps {
  onOpen: (drawing: SavedDrawing) => void
  /** The drawing being opened, to show its row as busy. */
  opening: string | null
  /** Why the last open failed, shown under the list. */
  openError: string | null
}

// Past this many, a search box appears.
const SEARCH_FROM = 6

function Icon({ name }: { name: string }) {
  return <span aria-hidden="true" className="upload-symbol">{name}</span>
}

function formatDate(iso: string | null) {
  if (!iso) return ''
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
}

function worksheetLine(d: SavedDrawing) {
  if (!d.deviations) return 'no worksheets yet'
  return `${d.deviations} worksheet row${d.deviations === 1 ? '' : 's'} on ${d.nodesWithWorksheet} of ${d.nodes} node${d.nodes === 1 ? '' : 's'}`
}

// Drawings whose HAZOP pipeline has finished, on the upload screen: one click
// reopens a drawing with its nodes, worksheets and chat, no upload needed.
export default function RecentDrawings({ onOpen, opening, openError }: RecentDrawingsProps) {
  const [drawings, setDrawings] = useState<SavedDrawing[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')

  useEffect(() => {
    let cancelled = false
    listSavedDrawings().then(
      (list) => !cancelled && setDrawings(list),
      (err) => !cancelled && setError(err instanceof Error ? err.message : String(err)),
    )
    return () => {
      cancelled = true
    }
  }, [])

  // Nothing analysed yet (or the agent can't be reached): the upload screen stands alone.
  if (error || (drawings && drawings.length === 0)) return null

  const shown = (drawings ?? []).filter((d) => d.fileName.toLowerCase().includes(query.trim().toLowerCase()))

  return (
    <section className="recent" aria-labelledby="recent-title">
      <header className="recent-head">
        <h2 id="recent-title">
          <Icon name="history" />
          Your analysed drawings
        </h2>
        {drawings && <span className="recent-count">{drawings.length}</span>}
        {drawings && drawings.length >= SEARCH_FROM && (
          <label className="recent-search">
            <Icon name="search" />
            <input type="search" placeholder="Find a drawing" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Find a drawing" />
          </label>
        )}
      </header>

      {!drawings ? (
        <p className="recent-muted">Loading your drawings…</p>
      ) : shown.length === 0 ? (
        <p className="recent-muted">No drawing matches “{query}”.</p>
      ) : (
        <ul className="recent-list">
          {shown.map((d) => {
            const busy = opening === d.jobId
            return (
              <li key={d.jobId}>
                <button className="recent-row" onClick={() => onOpen(d)} disabled={opening !== null} aria-busy={busy} title={`Open ${d.fileName} with its nodes and worksheets`}>
                  <span className="recent-icon">
                    <Icon name="picture_as_pdf" />
                  </span>
                  <span className="recent-text">
                    <strong>{d.fileName}</strong>
                    <small>
                      {d.nodes} node{d.nodes === 1 ? '' : 's'}
                      {d.consolidated ? ' (reduced)' : ''} · {worksheetLine(d)}
                      {d.updatedAt ? ` · ${formatDate(d.updatedAt)}` : ''}
                    </small>
                  </span>
                  {d.has3dView && <span className="recent-badge">3D</span>}
                  <span className="recent-open">{busy ? <span className="recent-spinner" aria-label="Opening" /> : <Icon name="arrow_forward" />}</span>
                </button>
              </li>
            )
          })}
        </ul>
      )}
      {openError && <p className="recent-error">{openError}</p>}
    </section>
  )
}
