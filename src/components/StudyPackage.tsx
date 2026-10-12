import { useEffect, useRef, useState } from 'react'
import {
  ACCEPTED_FILES,
  deleteDocument,
  documentUrl,
  formatSize,
  getPackage,
  updateDocument,
  uploadDocument,
  type ChecklistItem,
  type StudyPackage as Package,
} from '../lib/studyApi'
import './StudyPackage.css'

export interface StudyPackageProps {
  /** Null while the PDF's hash is still being computed. */
  studyId: string | null
  fileName: string
  /** Room to leave on the right for the chat panel, in px. */
  rightOffset: number
  onClose: () => void
  /** Called with the fresh package after every load or change. */
  onChanged: (pkg: Package) => void
}

const STATUS_LABEL = { provided: 'Provided', partial: 'Partly on the P&ID', missing: 'Missing' } as const

function ChecklistRow({
  item,
  pkg,
  busy,
  onUpload,
  onDelete,
  onRetype,
}: {
  item: ChecklistItem
  pkg: Package
  busy: boolean
  onUpload: (docType: string, files: FileList) => void
  onDelete: (id: string, name: string) => void
  onRetype: (id: string, docType: string) => void
}) {
  const input = useRef<HTMLInputElement>(null)
  const [dragOver, setDragOver] = useState(false)
  const files = pkg.documents.filter((d) => d.docType === item.id)
  return (
    <li
      className={`pkg-item pkg-item-${item.status}${dragOver ? ' pkg-item-drag' : ''}`}
      onDragOver={(e) => {
        e.preventDefault()
        setDragOver(true)
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault()
        setDragOver(false)
        if (e.dataTransfer.files.length) onUpload(item.id, e.dataTransfer.files)
      }}
    >
      <div className="pkg-item-head">
        <span className={`pkg-status pkg-status-${item.status}`}>{STATUS_LABEL[item.status]}</span>
        <span className="pkg-item-name">{item.name}</span>
        <button className="pkg-attach" onClick={() => input.current?.click()} disabled={busy}>
          Attach…
        </button>
        <input
          ref={input}
          type="file"
          multiple
          hidden
          accept={ACCEPTED_FILES}
          onChange={(e) => {
            if (e.target.files?.length) onUpload(item.id, e.target.files)
            e.target.value = ''
          }}
        />
      </div>
      <p className="pkg-why">{item.why}</p>
      {item.fromDrawing && <p className="pkg-from-drawing">From the drawing: {item.fromDrawing}</p>}
      {files.length > 0 && (
        <ul className="pkg-files">
          {files.map((d) => (
            <li key={d.id}>
              <a href={documentUrl(d.id)} target="_blank" rel="noreferrer">
                {d.fileName}
              </a>
              <span className="pkg-muted">
                {formatSize(d.size)} · {new Date(d.uploadedAt).toLocaleDateString()}
              </span>
              <select aria-label={`Document type of ${d.fileName}`} value={d.docType} onChange={(e) => onRetype(d.id, e.target.value)} disabled={busy}>
                {pkg.documentTypes.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
              <button onClick={() => onDelete(d.id, d.fileName)} disabled={busy} aria-label={`Remove ${d.fileName}`}>
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

export default function StudyPackage({ studyId, fileName, rightOffset, onClose, onChanged }: StudyPackageProps) {
  const [pkg, setPkg] = useState<Package | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const refresh = async (id: string) => {
    const fresh = await getPackage(id)
    setPkg(fresh)
    onChanged(fresh)
  }

  useEffect(() => {
    if (!studyId) return
    let cancelled = false
    getPackage(studyId)
      .then((fresh) => {
        if (cancelled) return
        setPkg(fresh)
        onChanged(fresh)
        setError(null)
      })
      .catch((err: Error) => !cancelled && setError(`${err.message}. Is the agent server running (\`uv run hazop-agent serve\` in agent/)?`))
    return () => {
      cancelled = true
    }
  }, [studyId, onChanged])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  const run = async (action: () => Promise<unknown>) => {
    if (!studyId) return
    setBusy(true)
    try {
      await action()
      await refresh(studyId)
      setError(null)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const upload = (docType: string, files: FileList) =>
    run(async () => {
      for (const file of Array.from(files)) await uploadDocument(studyId!, docType, file)
    })

  const checklist = pkg?.checklist

  return (
    <div className="pkg-backdrop" style={{ right: rightOffset }} onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="pkg" role="dialog" aria-modal="true" aria-label="Study package">
        <header className="pkg-header">
          <div className="pkg-title">
            <h2>Study package</h2>
            <span className="pkg-muted">
              {fileName} · study {studyId ?? '…'}
            </span>
          </div>
          <button className="pkg-close" onClick={onClose} aria-label="Close study package" title="Close (Esc)">
            ×
          </button>
        </header>

        {checklist && (
          <div className={`pkg-summary ${checklist.ready ? 'pkg-summary-ready' : ''}`}>
            <strong>
              {checklist.provided} of {checklist.total} documents provided
            </strong>
            {checklist.ready ? (
              <span>Ready for the HAZOP study.</span>
            ) : (
              <span>
                {checklist.partial > 0 && `${checklist.partial} partly covered by the P&ID. `}
                Still needed: {checklist.missing.join(', ')}.
              </span>
            )}
          </div>
        )}
        {error && <div className="pkg-error">{error}</div>}

        <div className="pkg-body">
          {!pkg && !error && <p className="pkg-muted">Loading…</p>}
          {pkg && checklist && (
            <ul className="pkg-list">
              {checklist.items.map((item) => (
                <ChecklistRow
                  key={item.id}
                  item={item}
                  pkg={pkg}
                  busy={busy}
                  onUpload={upload}
                  onDelete={(id, name) => window.confirm(`Remove ${name} from the study package?`) && void run(() => deleteDocument(id))}
                  onRetype={(id, docType) => void run(() => updateDocument(id, { docType }))}
                />
              ))}
            </ul>
          )}
          <p className="pkg-muted pkg-foot">
            Drop files on a row or use Attach. PDF, Excel, Word, CSV, images and CAD files up to 50 MB. Files are stored with the drawing on
            the agent server.
          </p>
        </div>
      </div>
    </div>
  )
}
