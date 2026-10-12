import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { PdfAnnotator } from 'inklayer-react'
import 'inklayer-react/style'
import ChatPanel, { makeMessageId, type ChatMessage, type NodeStatus, type ProposedNode, type ReviewedNode } from './components/ChatPanel'
import NodeMergeMenu, { type MergeTarget } from './components/NodeMergeMenu'
import HazopWorksheet from './components/HazopWorksheet'
import StudyPackage from './components/StudyPackage'
import UploadScreen from './components/UploadScreen'
import PipelineRunDialog, { type PipelineRun } from './components/PipelineRunDialog'
import './components/ViewerToolbar.css'
import { getPackage, type StudyPackage as Package } from './lib/studyApi'
import { getSummary, studyIdFor, worksheetNodeId } from './lib/hazopApi'
import { renderPdfPagesToImages } from './lib/renderPdfPages'
import { nodeToAnnotation, hazopNodeToAnnotations, type Annotation, type PageDimensions } from './lib/annotationFromNode'
import { pipelineNodesToProposedNodes, isPipelineNodeFile } from './lib/pipelineNodes'
import { formatValidation, runPipeline } from './lib/runPipeline'
import { describeTool, getChatHistory, sendChat } from './lib/chatApi'

// Local single-user identity for now. Once this viewer is wired up behind the
// MCP server, this will come from whoever is running the human-in-the-loop
// review step instead of being hardcoded.
const REVIEWER = { id: 'local-reviewer', name: 'Reviewer' }

// Must match the width baked into ChatPanel.css (.chat-panel). PdfAnnotator's
// own layout math needs a concrete size up front, so this can't be handed to
// it as a flex/percentage size — see the layoutStyle comment below.
const CHAT_WIDTH_PX = 360
// Room taken by PdfAnnotator's own header controls left of our toolbar.
const TOOLBAR_LEADING_PX = 120


function Toolbar(props: {
  save: () => void
  getAnnotations: () => Annotation[]
  exportToExcel: (fileName?: string) => void
  exportToPdf: (fileName?: string) => Promise<void>
  onNewFile: () => void
  onInsertTestNode: () => void
  onLoadPipelineNodes: () => void
  onRunPipeline: () => void
  onOpenPackage: () => void
  /** e.g. "3/7"; null until the package has loaded. */
  packageProgress: { provided: number; total: number } | null
  maxNodes: number | null
  onMaxNodesChange: (maxNodes: number | null) => void
  isAnalyzing: boolean
  /** Set while a run's progress dialog exists: the busy Run button reopens it. */
  onShowProgress?: () => void
  fileName: string
  maxWidth: string
}) {
  return (
    // One row that scrolls sideways (ViewerToolbar.css), so it survives
    // adding buttons: without it, a toolbar this full silently overflows
    // PdfAnnotator's fixed-width actions container and the last button(s) get
    // clipped off-screen behind the chat panel. Wrapping isn't an option
    // either: PdfAnnotator's header is a fixed height, so a third row gets
    // clipped. maxWidth gives the row a real limit to scroll within, since
    // PdfAnnotator sizes the actions container to its content (flex: 0 0 auto).
    <div className="viewer-toolbar" style={{ maxWidth: props.maxWidth }}>
      <span className="viewer-toolbar-file" title={props.fileName}>{props.fileName}</span>
      {/* Runs the step1→2→3 LangGraph agent (agent/, `uv run hazop-agent
          serve`) on the open PDF and paints its nodes when it finishes.
          Max nodes per page drives consolidation; leave it empty to skip it. */}
      <label className="viewer-toolbar-field" title="Reduced option: merge each page down to at most this many nodes (empty = the rules' own breakdown, typically 4–5)">
        Max nodes
        <input
          type="number"
          min={1}
          value={props.maxNodes ?? ''}
          onChange={(e) => props.onMaxNodesChange(e.target.value ? Math.max(1, Math.floor(Number(e.target.value))) : null)}
          placeholder="auto"
        />
      </label>
      {props.isAnalyzing && props.onShowProgress ? (
        <button className="primary" onClick={props.onShowProgress} title="Show the run's progress">
          Running… view progress
        </button>
      ) : (
        <button className="primary" onClick={props.onRunPipeline} disabled={props.isAnalyzing}>
          {props.isAnalyzing ? 'Running…' : 'Run HAZOP pipeline'}
        </button>
      )}
      {/* The documents a HAZOP team asks for before it starts, and how many are in. */}
      <button
        onClick={props.onOpenPackage}
        title="Attach the PFD, layout, C&E, process description, set values and data sheets for this drawing"
        className={props.packageProgress && props.packageProgress.provided < props.packageProgress.total ? 'warn' : undefined}
      >
        Study package{props.packageProgress ? ` (${props.packageProgress.provided}/${props.packageProgress.total})` : ''}
      </button>
      <button onClick={props.onNewFile}>New file</button>
      <button onClick={() => props.save()}>Save</button>
      <button onClick={() => props.exportToExcel()}>Export Excel</button>
      <button onClick={() => props.exportToPdf()}>Export PDF</button>
      <button
        className="subtle"
        onClick={() => {
          const annotations = props.getAnnotations()
          console.log('[pid-viewer] current annotations', annotations)
        }}
      >
        Log annotations
      </button>
      {/* Dev-only: exercises the Accept → draw-a-shape pipeline without
          needing a working OPENAI_API_KEY. Remove once that path is trusted. */}
      <button className="subtle" onClick={props.onInsertTestNode}>Insert test node</button>
      {/* Paints a step3.json / step3-consolidated.json already on disk, e.g.
          from a CLI run (`uv run hazop-agent run` or `npm run pipeline step3`). */}
      <button onClick={props.onLoadPipelineNodes} title="Load a step3.json / step3-consolidated.json exported by the pipeline CLI">
        Load pipeline nodes
      </button>
    </div>
  )
}

export default function App({ embedded = false }: { embedded?: boolean }) {
  const [pdfData, setPdfData] = useState<ArrayBuffer | null>(null)
  const [fileName, setFileName] = useState('')
  const [chatCollapsed, setChatCollapsed] = useState(false)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [isBusy, setIsBusy] = useState(false)
  // The progress dialog for a fresh (uncached) pipeline run; null for cached runs.
  const [pipelineRun, setPipelineRun] = useState<PipelineRun | null>(null)
  const [pageDims, setPageDims] = useState<Record<number, PageDimensions>>({})
  // Empty by default: the node rules already target 4–5 nodes per sheet. A
  // number here asks for the rules' "reduced option" (fewer, merged nodes).
  const [maxNodes, setMaxNodes] = useState<number | null>(null)

  // Every proposed node is painted onto the PDF the moment it's proposed —
  // pending (dashed) until accepted (solid) or rejected (removed) — fed to
  // PdfAnnotator via initialAnnotations. That prop is only read at mount, so
  // any change means remounting (bumping annotatorKey); annotatorApiRef lets
  // us pull whatever the human already drew first, so a remount doesn't lose it.
  const [canvasAnnotations, setCanvasAnnotations] = useState<Annotation[]>([])
  const [annotatorKey, setAnnotatorKey] = useState(0)
  const annotatorApiRef = useRef<{ getAnnotations: () => Annotation[] } | null>(null)
  // messageId:nodeIndex -> that node's current annotation(s). A single-shot
  // 'path'/'rect' node paints exactly one; a pipeline-loaded 'hazop' node can
  // paint several (one per traced route it clustered), which is why this is
  // an array rather than one annotation per key.
  const paintedNodes = useRef(new Map<string, Annotation[]>())
  // messageId:nodeIndex -> the stable id (or id prefix, for a multi-shape
  // hazop node) assigned the FIRST time that node was painted. Every later
  // repaint (accept/reject/undo/merge) reuses it, so InkLayer restyles the
  // same underlying shape(s) in place instead of drawing new ones.
  const paintedNodeIdPrefix = useRef(new Map<string, string>())
  // Monotonically increasing, never reused within a session — so undoing and
  // re-accepting a node doesn't collide with another node's color.
  const nextColorIndexRef = useRef(0)
  // Same idea, starting at 1 — this becomes both the "Node N" name shown on
  // InkLayer's own hover label and its stable reference number.
  const nextNodeNumberRef = useRef(1)
  // Captured on every mousedown inside the viewer, in plain screen
  // coordinates — this is what lets the merge menu open right at the click,
  // with no Konva/zoom coordinate math involved.
  const lastClickPos = useRef({ x: 0, y: 0 })
  const [mergeMenu, setMergeMenu] = useState<{ key: string; x: number; y: number } | null>(null)

  // HAZOP worksheets are stored per drawing under the pipeline's job id for
  // this PDF (name slug + content hash), so they come back with the file.
  const [studyId, setStudyId] = useState<string | null>(null)
  const [worksheetNode, setWorksheetNode] = useState<ReviewedNode | null>(null)
  // node id -> row count, for the "HAZOP worksheet (n)" buttons.
  const [worksheetCounts, setWorksheetCounts] = useState<Record<string, number>>({})
  // Supporting documents for this drawing and their readiness checklist.
  const [studyPackage, setStudyPackage] = useState<Package | null>(null)
  const [packageOpen, setPackageOpen] = useState(false)

  // Called wherever the open PDF changes, so nothing from the last drawing lingers.
  const resetWorksheets = useCallback(() => {
    setStudyId(null)
    setWorksheetNode(null)
    setWorksheetCounts({})
    setStudyPackage(null)
    setPackageOpen(false)
  }, [])

  useEffect(() => {
    if (!pdfData) return
    let cancelled = false
    void (async () => {
      const id = await studyIdFor(fileName, pdfData)
      if (cancelled) return
      setStudyId(id)
      try {
        const turns = await getChatHistory(id)
        if (cancelled) return
        if (turns.length > 0)
          setMessages((prev) => [
            ...prev,
            { id: makeMessageId(), role: 'assistant', kind: 'text', text: `Earlier conversation on ${fileName}:` },
            ...turns.map((t) => ({ id: makeMessageId(), role: t.role, kind: 'text' as const, text: t.text })),
          ])
      } catch (err) {
        console.warn('[pid-viewer] could not load the chat history', err)
      }
      try {
        const counts = await getSummary(id)
        if (cancelled) return
        setWorksheetCounts(counts)
      } catch (err) {
        console.warn('[pid-viewer] could not load HAZOP worksheet counts', err)
      }
      try {
        const pkg = await getPackage(id)
        if (!cancelled) setStudyPackage(pkg)
      } catch (err) {
        console.warn('[pid-viewer] could not load the study package', err)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [pdfData, fileName])

  const handleWorksheetRowsChanged = useCallback((nodeId: string, count: number) => {
    setWorksheetCounts((prev) => (prev[nodeId] === count ? prev : { ...prev, [nodeId]: count }))
  }, [])

  const openWorksheet = useCallback((node: ReviewedNode) => {
    setMergeMenu(null)
    setWorksheetNode(node)
  }, [])

  // Whether an annotation id belongs to one of our own painted nodes. This
  // is NOT the same as checking annotation.extensions.hazopNode — InkLayer's
  // internal round-trip (Annotation -> IAnnotationStore -> Annotation, which
  // happens on every getAnnotations() call and every onAnnotationSelected
  // event) only reconstructs extensions from its own known keys (konva,
  // pdfjs, legacy) and silently drops any custom key we set, including ours.
  // paintedNodes is data we own directly, so it's the only reliable source
  // of "is this ours" — never round-tripped through that lossy conversion.
  const findKeyForAnnotationId = useCallback((id: string): string | null => {
    for (const [key, annotations] of paintedNodes.current.entries()) {
      if (annotations.some((a) => a.id === id)) return key
    }
    return null
  }, [])

  // Re-derives canvasAnnotations from paintedNodes + whatever the human has
  // drawn by hand (identified by id NOT being one of ours, so we don't
  // double-count our own shapes), then remounts to show it.
  const syncCanvas = useCallback(() => {
    const humanDrawn = (annotatorApiRef.current?.getAnnotations() ?? []).filter((a) => !findKeyForAnnotationId(a.id))
    setCanvasAnnotations([...humanDrawn, ...Array.from(paintedNodes.current.values()).flat()])
    setAnnotatorKey((k) => k + 1)
  }, [findKeyForAnnotationId])

  const pipelineFileInputRef = useRef<HTMLInputElement>(null)

  // PdfAnnotator's `data` prop is handed to pdfjs, which may transfer/detach
  // the underlying ArrayBuffer. Remounting (for a new accepted annotation)
  // needs a fresh, un-detached copy each time.
  const viewerData = useMemo(() => pdfData?.slice(0) ?? null, [pdfData, annotatorKey])

  const handleFile = useCallback((file: File) => {
    file.arrayBuffer().then((buf) => {
      resetWorksheets()
      setPdfData(buf)
      setFileName(file.name)
    })
  }, [resetWorksheets])

  // node.shapeType picks the converter: 'hazop' can produce several
  // annotations (one per traced route it clustered), 'path'/'rect' always
  // produce exactly one — wrapped in an array so paintedNodes stays one
  // shape regardless of which kind of node this is.
  const annotationsForNode = useCallback(
    (node: ProposedNode, dims: PageDimensions, idPrefix: string, review: 'pending' | 'accepted', colorIndex: number, nodeNumber: number): Annotation[] =>
      node.shapeType === 'hazop'
        ? hazopNodeToAnnotations(node, dims, REVIEWER, colorIndex, idPrefix, review, nodeNumber)
        : [nodeToAnnotation(node, dims, REVIEWER, colorIndex, idPrefix, review, nodeNumber)],
    [],
  )

  // Assigns each new node a color + id, paints it onto the PDF immediately
  // as pending (dashed), and returns the chat-card versions of the nodes.
  // Shared by handleInsertTestNode and paintPipelineDocument.
  const paintProposedNodes = useCallback(
    (nodes: ProposedNode[], messageId: string, dimsMap: Record<number, PageDimensions>): ReviewedNode[] => {
      const reviewed: ReviewedNode[] = nodes.map((node, i) => {
        // A pipeline node's label carries its number ("N3 …"), kept stable
        // through merges, so the drawing and the cards use the same numbers,
        // and its colour follows the number: after a merge every node keeps
        // its colour, and the merged node keeps the one of the node it
        // survives as.
        const labelled = /^N(\d+)\b/.exec(node.label)?.[1]
        const nodeNumber = labelled ? Number(labelled) : nextNodeNumberRef.current
        nextNodeNumberRef.current = Math.max(nextNodeNumberRef.current, nodeNumber) + 1
        const colorIndex = labelled ? nodeNumber - 1 : nextColorIndexRef.current++
        const dims = dimsMap[node.pageIndex]
        if (dims) {
          const key = `${messageId}:${i}`
          const idPrefix = crypto.randomUUID()
          paintedNodeIdPrefix.current.set(key, idPrefix)
          paintedNodes.current.set(key, annotationsForNode(node, dims, idPrefix, 'pending', colorIndex, nodeNumber))
        } else {
          console.warn('[pid-viewer] no page dimensions recorded for page', node.pageIndex, '— cannot paint this node')
        }
        return { ...node, status: 'pending', colorIndex, nodeNumber }
      })
      syncCanvas()
      return reviewed
    },
    [syncCanvas, annotationsForNode],
  )

  const handleInsertTestNode = useCallback(async () => {
    if (!pdfData) return
    setChatCollapsed(false)

    // Page dimensions from the PDF, so Accept has what it
    // needs — this button exists specifically to test the pipeline without a VLM.
    const pages = await renderPdfPagesToImages(pdfData)
    const dimsMap: Record<number, PageDimensions> = {}
    for (const p of pages) dimsMap[p.pageIndex] = { width: p.pdfWidth, height: p.pdfHeight }
    setPageDims((prev) => ({ ...prev, ...dimsMap }))

    const testNodes: ProposedNode[] = [
      {
        pageIndex: 0,
        label: 'Test node (rect)',
        description: 'Manually inserted rectangle covering the middle of page 1, for testing accept/reject.',
        shapeType: 'rect',
        bbox: { x: 0.3, y: 0.3, width: 0.4, height: 0.4 },
      },
      {
        pageIndex: 0,
        label: 'Test node (path)',
        description: 'Manually inserted zigzag path across page 1, for testing the path shape.',
        shapeType: 'path',
        path: [
          { x: 0.1, y: 0.1 },
          { x: 0.35, y: 0.1 },
          { x: 0.35, y: 0.3 },
          { x: 0.6, y: 0.3 },
          { x: 0.6, y: 0.5 },
        ],
      },
    ]

    const messageId = makeMessageId()
    const reviewedNodes = paintProposedNodes(testNodes, messageId, dimsMap)
    setMessages((prev) => [...prev, { id: messageId, role: 'assistant', kind: 'nodes', nodes: reviewedNodes }])
  }, [pdfData, paintProposedNodes])

  const addAssistantText = useCallback((text: string) => {
    setMessages((prev) => [...prev, { id: makeMessageId(), role: 'assistant', kind: 'text', text }])
  }, [])

  // Paints a step3.json / step3-consolidated.json document: one card and one
  // set of shapes per node. Every node's bbox/routes are already normalized
  // page fractions computed from step 1's real entity geometry, so nothing
  // here re-derives or re-guesses a coordinate; this only maps the pipeline's
  // JSON shape onto the frontend's node type. Shared by the file loader, the
  // agent run and the chat.
  const paintPipelineDocument = useCallback(
    async (document: unknown, source: string, replace = false) => {
      if (!pdfData) return
      if (!isPipelineNodeFile(document)) {
        addAssistantText(`${source} doesn't look like a step3.json / step3-consolidated.json export (expected { pages: [{ pageIndex, nodes }] }).`)
        return
      }

      const nodes = pipelineNodesToProposedNodes(document)
      if (nodes.length === 0) {
        addAssistantText(`${source} loaded, but it has no nodes to paint.`)
        return
      }

      // Same page-dimension lookup every other painting path does, so
      // Accept/Reject/Merge have what they need for whichever pages this
      // document's nodes actually reference.
      const pages = await renderPdfPagesToImages(pdfData)
      const dimsMap: Record<number, PageDimensions> = {}
      for (const p of pages) dimsMap[p.pageIndex] = { width: p.pdfWidth, height: p.pdfHeight }
      setPageDims((prev) => ({ ...prev, ...dimsMap }))

      if (replace) {
        // The breakdown changed (an edit from the chat): the new one replaces
        // every node card and painted node, numbered from 1 again.
        paintedNodes.current.clear()
        paintedNodeIdPrefix.current.clear()
        nextColorIndexRef.current = 0
        nextNodeNumberRef.current = 1
      }
      const messageId = makeMessageId()
      const reviewedNodes = paintProposedNodes(nodes, messageId, dimsMap)
      setMessages((prev) => [
        ...(replace ? prev.filter((m) => m.kind !== 'nodes') : prev),
        ...(replace
          ? []
          : [{ id: makeMessageId(), role: 'assistant' as const, kind: 'text' as const, text: `Loaded ${nodes.length} node${nodes.length === 1 ? '' : 's'} from ${source}.` }]),
        { id: messageId, role: 'assistant', kind: 'nodes', nodes: reviewedNodes },
      ])
    },
    [pdfData, paintProposedNodes, addAssistantText],
  )

  const handleLoadPipelineNodes = useCallback(
    async (file: File) => {
      if (!pdfData) return
      setChatCollapsed(false)

      let parsed: unknown
      try {
        parsed = JSON.parse(await file.text())
      } catch (err) {
        addAssistantText(`Couldn't parse ${file.name} as JSON: ${(err as Error).message}`)
        return
      }
      await paintPipelineDocument(parsed, file.name)
    },
    [pdfData, paintPipelineDocument, addAssistantText],
  )

  // The chat assistant, on the drawing's thread (its study / job id, the same
  // thread the pipeline runs under). It can merge, rename and undo nodes; the
  // new breakdown replaces the cards and the painting.
  const handleSend = useCallback(
    async (text: string) => {
      setMessages((prev) => [...prev, { id: makeMessageId(), role: 'user', kind: 'text', text }])
      if (!studyId) {
        addAssistantText('Open a drawing first: the conversation belongs to the drawing.')
        return
      }
      const threadId = studyId
      setIsBusy(true)
      const statusId = makeMessageId()
      setMessages((prev) => [...prev, { id: statusId, role: 'assistant', kind: 'text', text: 'Thinking…' }])
      const setStatus = (status: string) =>
        setMessages((prev) => prev.map((m) => (m.id === statusId && m.kind === 'text' ? { ...m, text: status } : m)))
      const reply = (answer: string) =>
        setMessages((prev) => [...prev.filter((m) => m.id !== statusId), { id: makeMessageId(), role: 'assistant', kind: 'text', text: answer }])
      let replied = false
      try {
        await sendChat(threadId, text, (event) => {
          if (event.type === 'tool') setStatus(describeTool(event.name))
          else if (event.type === 'nodes') {
            void paintPipelineDocument(event.document, 'the assistant', true)
            getSummary(threadId).then(setWorksheetCounts, () => {})
          } else if (event.type === 'message') {
            replied = true
            reply(event.text)
          } else if (event.type === 'error') {
            replied = true
            reply(`The assistant failed: ${event.message}`)
          }
        })
        if (!replied) reply('(no reply)')
      } catch (err) {
        reply(`The assistant isn't reachable: ${(err as Error).message}. Is the agent server running (\`uv run hazop-agent serve\` in agent/)?`)
      } finally {
        setIsBusy(false)
      }
    },
    [studyId, paintPipelineDocument, addAssistantText],
  )

  // Sends the open PDF to the LangGraph agent and runs every step in one
  // request. A single chat message is updated in place with the latest
  // progress line, then the result is painted like a loaded file.
  const handleRunPipeline = useCallback(async () => {
    if (!pdfData || isBusy) return
    setIsBusy(true)
    setChatCollapsed(false)

    const statusId = makeMessageId()
    const setStatus = (text: string) =>
      setMessages((prev) => prev.map((m) => (m.id === statusId && m.kind === 'text' ? { ...m, text } : m)))
    setMessages((prev) => [...prev, { id: statusId, role: 'assistant', kind: 'text', text: 'Starting the HAZOP pipeline…' }])
    setPipelineRun(null)
    const updateRun = (update: (run: PipelineRun) => Partial<PipelineRun>) =>
      setPipelineRun((run) => (run ? { ...run, ...update(run) } : run))

    try {
      const outcome: { result?: { document: unknown; source: string; report: string }; failure?: string } = {}
      await runPipeline(pdfData.slice(0), fileName, maxNodes, (event) => {
        if (event.type === 'run') {
          if (!event.cached)
            setPipelineRun({ open: true, phase: 'running', startedAt: Date.now(), lines: [], steps: {}, consolidating: maxNodes !== null })
        } else if (event.type === 'progress') {
          setStatus(`Pipeline running… ${event.message.trim()}`)
          updateRun((run) => ({ lines: [...run.lines, event.message].slice(-500) }))
        } else if (event.type === 'step') {
          setStatus(`Pipeline running… ${event.step} ${event.status}`)
          updateRun((run) => ({ steps: { ...run.steps, [event.step]: event.status } }))
        } else if (event.type === 'error') outcome.failure = event.message
        else if (event.type === 'result')
          outcome.result = {
            document: event.document,
            source: `job ${event.jobId} (${event.consolidated ? 'step3-consolidated' : 'step3'})`,
            report: event.validation?.length
              ? formatValidation(event.validation)
              : event.issues.map((i) => `${i.step}${i.page !== undefined ? ` page ${i.page}` : ''}: ${i.problem}`).join('\n'),
          }
      })

      if (outcome.failure) throw new Error(outcome.failure)
      if (!outcome.result) throw new Error('the run ended without a result')
      const { document, source, report } = outcome.result
      setStatus(report ? `Pipeline finished.\n${report}` : 'Pipeline finished.')
      await paintPipelineDocument(document, source)
      updateRun(() => ({ phase: 'done', finishedAt: Date.now() }))
    } catch (err) {
      updateRun(() => ({ phase: 'failed', finishedAt: Date.now(), error: (err as Error).message }))
      setStatus(`HAZOP pipeline failed: ${(err as Error).message}. Is the agent server running (\`uv run hazop-agent serve\` in agent/)?`)
    } finally {
      setIsBusy(false)
    }
  }, [pdfData, fileName, maxNodes, isBusy, paintPipelineDocument])

  const handleNodeStatusChange = useCallback(
    (messageId: string, nodeIndex: number, node: ReviewedNode, status: NodeStatus) => {
      setMessages((prev) =>
        prev.map((m) => {
          if (m.id !== messageId || m.kind !== 'nodes') return m
          return { ...m, nodes: m.nodes.map((n, i) => (i === nodeIndex ? { ...n, status } : n)) }
        }),
      )

      const key = `${messageId}:${nodeIndex}`

      if (status === 'rejected') {
        paintedNodes.current.delete(key)
        syncCanvas()
        return
      }

      const dims = pageDims[node.pageIndex]
      if (!dims) {
        console.warn('[pid-viewer] no page dimensions recorded for page', node.pageIndex, '— cannot draw this node')
        return
      }
      const idPrefix = paintedNodeIdPrefix.current.get(key) ?? crypto.randomUUID()
      paintedNodeIdPrefix.current.set(key, idPrefix)
      paintedNodes.current.set(
        key,
        annotationsForNode(node, dims, idPrefix, status === 'accepted' ? 'accepted' : 'pending', node.colorIndex, node.nodeNumber),
      )
      syncCanvas()
    },
    [pageDims, syncCanvas, annotationsForNode],
  )

  const getNodeByKey = useCallback(
    (key: string): ReviewedNode | undefined => {
      const [msgId, nodeIndexStr] = key.split(':')
      const message = messages.find((m) => m.id === msgId)
      return message?.kind === 'nodes' ? message.nodes[Number(nodeIndexStr)] : undefined
    },
    [messages],
  )

  // Every other currently-visible node (excluding the one being merged and
  // anything already rejected), deduplicated by nodeNumber — merging into an
  // already-merged node just points at the same target again.
  const getMergeTargets = useCallback(
    (excludeKey: string): MergeTarget[] => {
      const seen = new Map<number, MergeTarget>()
      for (const m of messages) {
        if (m.kind !== 'nodes') continue
        m.nodes.forEach((n, i) => {
          const key = `${m.id}:${i}`
          if (key === excludeKey || n.status === 'rejected') return
          if (!seen.has(n.nodeNumber)) seen.set(n.nodeNumber, { nodeNumber: n.nodeNumber, colorIndex: n.colorIndex, label: n.label })
        })
      }
      return Array.from(seen.values()).sort((a, b) => a.nodeNumber - b.nodeNumber)
    },
    [messages],
  )

  const handleAnnotationClicked = useCallback(
    (annotation: Annotation | null, isClick: boolean) => {
      const key = annotation && findKeyForAnnotationId(annotation.id)
      if (!isClick || !key) {
        setMergeMenu(null)
        return
      }
      setMergeMenu({ key, x: lastClickPos.current.x, y: lastClickPos.current.y })
    },
    [findKeyForAnnotationId],
  )

  const handleMergeInto = useCallback(
    (sourceKey: string, target: MergeTarget) => {
      const [msgId, nodeIndexStr] = sourceKey.split(':')
      const nodeIndex = Number(nodeIndexStr)
      const node = getNodeByKey(sourceKey)
      if (!node) return

      const dims = pageDims[node.pageIndex]
      if (!dims) {
        console.warn('[pid-viewer] no page dimensions recorded for page', node.pageIndex, '— cannot merge this node')
        return
      }

      const updatedNode: ReviewedNode = {
        ...node,
        colorIndex: target.colorIndex,
        nodeNumber: target.nodeNumber,
        mergedInto: target.nodeNumber,
      }
      setMessages((prev) =>
        prev.map((m) =>
          m.id === msgId && m.kind === 'nodes' ? { ...m, nodes: m.nodes.map((n, i) => (i === nodeIndex ? updatedNode : n)) } : m,
        ),
      )

      const idPrefix = paintedNodeIdPrefix.current.get(sourceKey) ?? crypto.randomUUID()
      paintedNodeIdPrefix.current.set(sourceKey, idPrefix)
      paintedNodes.current.set(
        sourceKey,
        annotationsForNode(
          updatedNode,
          dims,
          idPrefix,
          node.status === 'accepted' ? 'accepted' : 'pending',
          target.colorIndex,
          target.nodeNumber,
        ),
      )
      syncCanvas()
      setMergeMenu(null)
    },
    [getNodeByKey, pageDims, syncCanvas, annotationsForNode],
  )

  if (!pdfData) return <UploadScreen onFile={handleFile} />

  return (
    <>
      {/* onMouseDownCapture, not onClick: it needs to fire before InkLayer's
          own stage handlers so the coordinates are ready by the time
          onAnnotationSelected fires in the same interaction. */}
      <div onMouseDownCapture={(e) => (lastClickPos.current = { x: e.clientX, y: e.clientY })}>
        <PdfAnnotator
          // Bumped whenever a node is proposed/accepted/rejected/undone:
          // PdfAnnotator only reads initialAnnotations at mount, so any change
          // means remounting (see canvasAnnotations/syncCanvas above).
          key={annotatorKey}
          title={fileName}
          data={viewerData!}
          locale="en-US"
          user={REVIEWER}
          initialAnnotations={canvasAnnotations}
          // PdfAnnotator computes its internal page-fit scale from this size
          // directly, so it needs a real viewport-based size rather than a
          // percentage of a flex parent (percentages here produced a bad
          // initial scale). The chat panel is fixed-position and overlays on
          // top, so this just needs to leave room for it on the right.
          layoutStyle={{
            width: embedded
              ? chatCollapsed
                ? 'calc(100vw - var(--viewer-sidebar-width, 0px))'
                : `calc(100vw - var(--viewer-sidebar-width, 0px) - ${CHAT_WIDTH_PX}px)`
              : chatCollapsed ? '100vw' : `calc(100vw - ${CHAT_WIDTH_PX}px)`,
            height: embedded ? 'calc(100vh - var(--viewer-top-offset, 0px))' : '100vh',
          }}
          enableNativeAnnotations
          // Our own click-to-merge menu is the only interaction hazop nodes
          // get; InkLayer's native move/resize/delete would otherwise fight
          // it for the same click and could desync from paintedNodes.
          annotationPermissions={{
            can: ({ action, annotation }) => {
              if (!annotation || !findKeyForAnnotationId(annotation.id)) return undefined
              if (action === 'annotation.transform' || action === 'annotation.edit' || action === 'annotation.delete') {
                return false
              }
              return undefined
            },
          }}
          actions={(props) => {
            annotatorApiRef.current = { getAnnotations: props.getAnnotations }
            return (
              <Toolbar
                {...props}
                fileName={fileName}
                onNewFile={() => {
                  resetWorksheets()
                  setPdfData(null)
                }}
                onInsertTestNode={handleInsertTestNode}
                onLoadPipelineNodes={() => pipelineFileInputRef.current?.click()}
                onRunPipeline={handleRunPipeline}
                onOpenPackage={() => setPackageOpen(true)}
                packageProgress={studyPackage && { provided: studyPackage.checklist.provided, total: studyPackage.checklist.total }}
                maxNodes={maxNodes}
                onMaxNodesChange={setMaxNodes}
                isAnalyzing={isBusy}
                onShowProgress={pipelineRun ? () => setPipelineRun((run) => run && { ...run, open: true }) : undefined}
                // Viewer width minus the search/comment icons on the header's left.
                maxWidth={`calc(${chatCollapsed ? '100vw' : `100vw - ${CHAT_WIDTH_PX}px`} - var(--viewer-sidebar-width, 0px) - ${TOOLBAR_LEADING_PX}px)`}
              />
            )
          }}
          // These callbacks are the seam this app will hand to the review agent
          // later: every create/update/delete/select event, plus the final saved
          // set, is already surfaced here.
          onLoad={() => console.log('[pid-viewer] loaded', fileName)}
          onSave={(annotations) => console.log('[pid-viewer] saved', annotations)}
          onAnnotationAdded={(a) => console.log('[pid-viewer] annotation added', a)}
          onAnnotationUpdated={(a) => console.log('[pid-viewer] annotation updated', a)}
          onAnnotationDeleted={(id) => console.log('[pid-viewer] annotation deleted', id)}
          onAnnotationSelected={handleAnnotationClicked}
        />
      </div>
      <input
        ref={pipelineFileInputRef}
        type="file"
        accept="application/json"
        hidden
        onChange={(e) => {
          const file = e.target.files?.[0]
          if (file) void handleLoadPipelineNodes(file)
          e.target.value = '' // allow re-selecting the same file next time
        }}
      />
      {pipelineRun?.open && (
        <PipelineRunDialog run={pipelineRun} onClose={() => setPipelineRun((run) => run && { ...run, open: false })} />
      )}
      {mergeMenu && (
        <NodeMergeMenu
          x={mergeMenu.x}
          y={mergeMenu.y}
          sourceLabel={`Node ${getNodeByKey(mergeMenu.key)?.nodeNumber ?? '?'}`}
          targets={getMergeTargets(mergeMenu.key)}
          onSelect={(target) => handleMergeInto(mergeMenu.key, target)}
          onOpenWorksheet={() => {
            const node = getNodeByKey(mergeMenu.key)
            if (node) openWorksheet(node)
          }}
          onClose={() => setMergeMenu(null)}
        />
      )}
      <ChatPanel
        messages={messages}
        isSending={isBusy}
        onSend={handleSend}
        onNodeStatusChange={handleNodeStatusChange}
        collapsed={chatCollapsed}
        onToggleCollapsed={() => setChatCollapsed((c) => !c)}
        onOpenWorksheet={openWorksheet}
        worksheetCounts={worksheetCounts}
      />
      {worksheetNode && (
        <HazopWorksheet
          // Remount per node and study, so the worksheet starts in its loading state.
          key={`${studyId}|${worksheetNodeId(worksheetNode)}`}
          node={worksheetNode}
          studyId={studyId}
          rightOffset={chatCollapsed ? 0 : CHAT_WIDTH_PX}
          onClose={() => setWorksheetNode(null)}
          onRowsChanged={handleWorksheetRowsChanged}
          packageMissing={studyPackage ? studyPackage.checklist.missing : null}
          onOpenPackage={() => setPackageOpen(true)}
        />
      )}
      {packageOpen && (
        <StudyPackage
          studyId={studyId}
          fileName={fileName}
          rightOffset={chatCollapsed ? 0 : CHAT_WIDTH_PX}
          onClose={() => setPackageOpen(false)}
          onChanged={setStudyPackage}
        />
      )}
    </>
  )
}
