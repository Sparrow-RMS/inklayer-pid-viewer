import { useRef, useState, type DragEvent } from 'react'
import './UploadScreen.css'

const STEPS = [
  { icon: 'upload_file', title: 'Open a P&ID', text: 'Any PDF drawing, one sheet or many.' },
  { icon: 'account_tree', title: 'Divide into nodes', text: 'The agent paints whole pipe runs as HAZOP nodes.' },
  { icon: 'table_chart', title: 'Draft worksheets', text: 'Deviations, causes, consequences and safeguards.' },
  { icon: 'view_in_ar', title: 'See it in 3D', text: 'Your shop floor as a 3D model, every node in colour.', isNew: true },
]

function Icon({ name }: { name: string }) {
  return <span aria-hidden="true" className="upload-symbol">{name}</span>
}

// Shown in place of the viewer until a PDF is opened.
export default function UploadScreen({ onFile }: { onFile: (file: File) => void }) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)

  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    setDragging(false)
    const file = e.dataTransfer.files[0]
    if (file) onFile(file)
  }

  return (
    <div className="upload-screen" onDragOver={(e) => e.preventDefault()} onDrop={onDrop}>
      <div aria-hidden="true" className="upload-backdrop" />
      <div className="upload-inner">
        <header className="upload-heading">
          <span className="upload-eyebrow"><i />New study</span>
          <h1>Start with a P&amp;ID</h1>
          <p>Drop a drawing to divide it into HAZOP nodes, draft every worksheet and see your shop floor in 3D.</p>
        </header>

        <div
          className={`upload-dropzone${dragging ? ' dragging' : ''}`}
          onClick={() => inputRef.current?.click()}
          onDragEnter={() => setDragging(true)}
          onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragging(false) }}
          onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); inputRef.current?.click() } }}
          role="button"
          tabIndex={0}
        >
          <div aria-hidden="true" className="upload-art">
            <span className="upload-sheet sheet-back" />
            <span className="upload-sheet sheet-mid" />
            <span className="upload-sheet sheet-front"><Icon name="picture_as_pdf" /></span>
          </div>
          <strong>{dragging ? 'Release to open' : 'Drag & drop your P&ID here'}</strong>
          <span className="upload-hint">or <u>browse your files</u> · PDF only</span>
          <button className="upload-button" onClick={(e) => { e.stopPropagation(); inputRef.current?.click() }} type="button">
            <Icon name="folder_open" />Choose PDF
          </button>
        </div>

        <ol className="upload-steps">
          {STEPS.map((step, i) => (
            <li key={step.title}>
              <span className={`upload-step-icon step-${i}`}><Icon name={step.icon} /></span>
              <span>
                <small>Step {i + 1}{'isNew' in step && <em className="upload-new">New</em>}</small>
                <strong>{step.title}</strong>
                {step.text}
              </span>
            </li>
          ))}
        </ol>
      </div>

      <input
        ref={inputRef}
        type="file"
        accept="application/pdf"
        hidden
        onChange={(e) => {
          const file = e.target.files?.[0]
          if (file) onFile(file)
          e.target.value = ''
        }}
      />
    </div>
  )
}
