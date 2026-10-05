import { useCallback, useMemo, useRef, useState } from 'react'
import { PdfAnnotator } from 'inklayer-react'
import 'inklayer-react/style'
import ChatPanel, { makeMessageId, type ChatMessage, type NodeStatus, type ProposedNode, type ReviewedNode } from './components/ChatPanel'
import NodeMergeMenu, { type MergeTarget } from './components/NodeMergeMenu'
import { renderPdfPagesToImages } from './lib/renderPdfPages'
import { nodeToAnnotation, hazopNodeToAnnotations, type Annotation, type PageDimensions } from './lib/annotationFromNode'
import { pipelineNodesToProposedNodes, isPipelineNodeFile } from './lib/pipelineNodes'
import { formatValidation, runPipeline } from './lib/runPipeline'

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

const ANALYZE_URL = import.meta.env.VITE_ANALYZE_API_URL || '/api/analyze'

const STUB_REPLY =
  "This is a placeholder reply — I'm not connected to an agent yet beyond the HAZOP node analysis. Try \"Analyze for HAZOP nodes\" above the drawing."

function Toolbar(props: {
  save: () => void
  getAnnotations: () => Annotation[]
  exportToExcel: (fileName?: string) => void
  exportToPdf: (fileName?: string) => Promise<void>
  onNewFile: () => void
  onAnalyze: () => void
  onInsertTestNode: () => void
  onLoadPipelineNodes: () => void
  onRunPipeline: () => void
  maxNodes: number | null
  onMaxNodesChange: (maxNodes: number | null) => void
  isAnalyzing: boolean
  fileName: string
  maxWidth: string
}) {
  return (
    // flexWrap so this survives adding buttons: without it, a toolbar this
    // full silently overflows PdfAnnotator's fixed-width actions container
    // and the last button(s) get clipped off-screen behind the chat panel
    // with no scrollbar or other indication they still exist — exactly what
    // happened to "Load pipeline nodes" once "Insert test node" pushed the
    // row past the available width. flexWrap alone isn't enough: PdfAnnotator
    // sizes the actions container to its content (flex: 0 0 auto), so the row
    // never runs out of room to wrap. maxWidth gives it a real limit.
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', rowGap: 4, maxWidth: props.maxWidth }}>
      <span style={{ fontSize: 13, opacity: 0.7, marginRight: 4 }}>{props.fileName}</span>
      <button onClick={props.onNewFile}>New file</button>
      <button onClick={() => props.save()}>Save</button>
      <button onClick={() => props.exportToExcel()}>Export Excel</button>
      <button onClick={() => props.exportToPdf()}>Export PDF</button>
      <button
        onClick={() => {
          const annotations = props.getAnnotations()
          console.log('[pid-viewer] current annotations', annotations)
        }}
      >
        Log annotations
      </button>
      <button onClick={props.onAnalyze} disabled={props.isAnalyzing}>
        {props.isAnalyzing ? 'Analyzing…' : 'Analyze for HAZOP nodes'}
      </button>
      {/* Dev-only: exercises the Accept → draw-a-shape pipeline without
          needing a working OPENAI_API_KEY. Remove once that path is trusted. */}
      <button onClick={props.onInsertTestNode}>Insert test node</button>
      {/* Runs the step1→2→3 LangGraph agent (agent/, `uv run hazop-agent
          serve`) on the open PDF and paints its nodes when it finishes.
          Max nodes per page drives consolidation; leave it empty to skip it. */}
      <label style={{ fontSize: 13, display: 'flex', alignItems: 'center', gap: 4 }} title="Reduced option: merge each page down to at most this many nodes (empty = the rules' own breakdown, typically 4–5)">
        Max nodes
        <input
          type="number"
          min={1}
          value={props.maxNodes ?? ''}
          onChange={(e) => props.onMaxNodesChange(e.target.value ? Math.max(1, Math.floor(Number(e.target.value))) : null)}
          style={{ width: 48 }}
        />
      </label>
      <button onClick={props.onRunPipeline} disabled={props.isAnalyzing}>
        {props.isAnalyzing ? 'Running…' : 'Run HAZOP pipeline'}
      </button>
      {/* Paints a step3.json / step3-consolidated.json already on disk, e.g.
          from a CLI run (`uv run hazop-agent run` or `npm run pipeline step3`). */}
      <button onClick={props.onLoadPipelineNodes} title="Load a step3.json / step3-consolidated.json exported by the pipeline CLI">
        Load pipeline nodes
      </button>
    </div>
  )
}

export default function App() {
  const [pdfData, setPdfData] = useState<ArrayBuffer | null>(null)
  const [fileName, setFileName] = useState('')
  const [chatCollapsed, setChatCollapsed] = useState(false)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [isBusy, setIsBusy] = useState(false)
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

  const fileInputRef = useRef<HTMLInputElement>(null)
  const pipelineFileInputRef = useRef<HTMLInputElement>(null)

  // PdfAnnotator's `data` prop is handed to pdfjs, which may transfer/detach
  // the underlying ArrayBuffer. Remounting (for a new accepted annotation)
  // needs a fresh, un-detached copy each time.
  const viewerData = useMemo(() => pdfData?.slice(0) ?? null, [pdfData, annotatorKey])

  const handleFile = useCallback((file: File) => {
    file.arrayBuffer().then((buf) => {
      setPdfData(buf)
      setFileName(file.name)
    })
  }, [])

  const onDrop = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault()
      const file = e.dataTransfer.files[0]
      if (file) handleFile(file)
    },
    [handleFile],
  )

  const handleSend = useCallback(async (text: string) => {
    setMessages((prev) => [...prev, { id: makeMessageId(), role: 'user', kind: 'text', text }])
    setIsBusy(true)
    await new Promise((resolve) => setTimeout(resolve, 500 + Math.random() * 400))
    setMessages((prev) => [...prev, { id: makeMessageId(), role: 'assistant', kind: 'text', text: STUB_REPLY }])
    setIsBusy(false)
  }, [])

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
  // Shared by handleAnalyze, handleInsertTestNode, and handleLoadPipelineNodes.
  const paintProposedNodes = useCallback(
    (nodes: ProposedNode[], messageId: string, dimsMap: Record<number, PageDimensions>): ReviewedNode[] => {
      const reviewed: ReviewedNode[] = nodes.map((node, i) => {
        const colorIndex = nextColorIndexRef.current++
        const nodeNumber = nextNodeNumberRef.current++
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

  const handleAnalyze = useCallback(async () => {
    if (!pdfData || isBusy) return
    setIsBusy(true)
    setChatCollapsed(false)

    try {
      const pages = await renderPdfPagesToImages(pdfData)
      const dimsMap: Record<number, PageDimensions> = {}
      for (const p of pages) dimsMap[p.pageIndex] = { width: p.pdfWidth, height: p.pdfHeight }
      setPageDims((prev) => ({ ...prev, ...dimsMap }))

      const response = await fetch(ANALYZE_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          fileName,
          pages: pages.map((p) => ({ pageIndex: p.pageIndex, image: p.image })),
        }),
      })

      if (!response.ok) {
        const body = await response.json().catch(() => null)
        throw new Error(body?.error || `Analysis request failed (${response.status})`)
      }

      const { nodes }: { nodes: ProposedNode[] } = await response.json()

      const messageId = makeMessageId()
      const reviewedNodes = paintProposedNodes(nodes, messageId, dimsMap)
      setMessages((prev) => [...prev, { id: messageId, role: 'assistant', kind: 'nodes', nodes: reviewedNodes }])
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        {
          id: makeMessageId(),
          role: 'assistant',
          kind: 'text',
          text: `HAZOP node analysis failed: ${(err as Error).message}. Is the backend server running?`,
        },
      ])
    } finally {
      setIsBusy(false)
    }
  }, [pdfData, fileName, isBusy, paintProposedNodes])

  const handleInsertTestNode = useCallback(async () => {
    if (!pdfData) return
    setChatCollapsed(false)

    // Same page-dimension lookup handleAnalyze does, so Accept has what it
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

  // Paints a step3.json / step3-consolidated.json document exactly the way
  // handleAnalyze paints a VLM response — same paintProposedNodes call, same
  // chat-card review flow. Every node's bbox/routes are already normalized
  // page fractions computed from step 1's real entity geometry, so — same as
  // the single-shot flow — nothing here re-derives or re-guesses a
  // coordinate; this only maps the pipeline's JSON shape onto the frontend's
  // existing node type. Shared by the file loader and the agent run.
  const paintPipelineDocument = useCallback(
    async (document: unknown, source: string) => {
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

      const messageId = makeMessageId()
      const reviewedNodes = paintProposedNodes(nodes, messageId, dimsMap)
      setMessages((prev) => [
        ...prev,
        { id: makeMessageId(), role: 'assistant', kind: 'text', text: `Loaded ${nodes.length} node${nodes.length === 1 ? '' : 's'} from ${source}.` },
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

    try {
      const outcome: { result?: { document: unknown; source: string; report: string }; failure?: string } = {}
      await runPipeline(pdfData.slice(0), fileName, maxNodes, (event) => {
        if (event.type === 'progress') setStatus(`Pipeline running… ${event.message.trim()}`)
        else if (event.type === 'step') setStatus(`Pipeline running… ${event.step} ${event.status}`)
        else if (event.type === 'error') outcome.failure = event.message
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
    } catch (err) {
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

  if (!pdfData) {
    return (
      <div
        onDragOver={(e) => e.preventDefault()}
        onDrop={onDrop}
        style={{
          height: '100%',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 16,
        }}
      >
        <h1 style={{ margin: 0 }}>P&ID Annotator</h1>
        <p style={{ margin: 0, opacity: 0.7 }}>Drop a PDF here, or choose a file to open it.</p>
        <button onClick={() => fileInputRef.current?.click()}>Choose PDF</button>
        <input
          ref={fileInputRef}
          type="file"
          accept="application/pdf"
          hidden
          onChange={(e) => {
            const file = e.target.files?.[0]
            if (file) handleFile(file)
          }}
        />
      </div>
    )
  }

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
            width: chatCollapsed ? '100vw' : `calc(100vw - ${CHAT_WIDTH_PX}px)`,
            height: '100vh',
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
                onNewFile={() => setPdfData(null)}
                onAnalyze={handleAnalyze}
                onInsertTestNode={handleInsertTestNode}
                onLoadPipelineNodes={() => pipelineFileInputRef.current?.click()}
                onRunPipeline={handleRunPipeline}
                maxNodes={maxNodes}
                onMaxNodesChange={setMaxNodes}
                isAnalyzing={isBusy}
                // Viewer width minus the search/comment icons on the header's left.
                maxWidth={`calc(${chatCollapsed ? '100vw' : `100vw - ${CHAT_WIDTH_PX}px`} - ${TOOLBAR_LEADING_PX}px)`}
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
      {mergeMenu && (
        <NodeMergeMenu
          x={mergeMenu.x}
          y={mergeMenu.y}
          sourceLabel={`Node ${getNodeByKey(mergeMenu.key)?.nodeNumber ?? '?'}`}
          targets={getMergeTargets(mergeMenu.key)}
          onSelect={(target) => handleMergeInto(mergeMenu.key, target)}
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
      />
    </>
  )
}
