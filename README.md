# InkLayer P&ID Viewer

A React PDF viewer built on [inklayer-react](https://github.com/Laomai-codefee/inklayer-react)
for reviewing P&ID drawings, with a chat panel that can call out to a VLM to
propose HAZOP nodes for human review.

## Layout

```
inklayer-pid-viewer/
├── src/       React + Vite frontend (the viewer + chat panel)
└── server/    Express backend that calls the VLM
```

## Running it

In two terminals:

```bash
cd server
cp .env.example .env   # then fill in OPENAI_API_KEY
npm install
npm run dev            # http://127.0.0.1:4001
```

```bash
npm install             # from the repo root
npm run dev              # http://127.0.0.1:5175 (or whatever Vite picks)
```

If the frontend's backend URL ever differs from the default, copy
`.env.example` to `.env` at the repo root and set `VITE_ANALYZE_API_URL`.

## The four-stage analysis pipeline

`server/src/pipeline/` is a staged replacement for the single-shot analyze
call, built so each stage's output is inspectable and re-runnable on its own.
See [`server/src/pipeline/README.md`](server/src/pipeline/README.md) for the
design.

1. **Extract** every detail on the sheet — *implemented*
2. **Classify** the processes involved — *implemented*
3. **Cluster** related processes into HAZOP nodes (union-find), optionally
   consolidated to a target count — *implemented*
4. **Annotate** them in the editor — *implemented (file-based bridge; no
   live HTTP wiring from this frontend to the pipeline yet)*

```bash
cd server
node test/fixtures/make-pid.mjs /tmp/pid.pdf   # synthetic P&ID, no API key needed
npm run pipeline step1 /tmp/pid.pdf
npm run pipeline inspect <jobId>
npm test                                       # deterministic logic, no API key
```

The single-shot `Analyze for HAZOP nodes` button described below still works
and is untouched — the pipeline is a separate, additive path.

To see the pipeline's own output in the viewer: run the CLI through step 3
(optionally with `--max-nodes N`, see `server/src/pipeline/README.md`), open
a PDF in the viewer, and use the toolbar's **Load pipeline nodes** button to
pick the resulting `step3.json` or `step3-consolidated.json` — it paints
straight onto the canvas through the same Accept/Reject/Merge review flow
the single-shot button uses. There's no live HTTP route from this frontend
to the pipeline yet (it's still CLI-only), so this is a file picker rather
than a fetch — the natural next step once that plumbing exists.

## What's here

- Drop or choose a PDF to open it in a full `PdfAnnotator` (every InkLayer
  annotation tool: shapes, ink, stamps, signatures, comments, sidebar, export
  to PDF/Excel).
- A chat panel on the right. It's a stub (no live agent) except for one real
  action: **Analyze for HAZOP nodes**, in the toolbar above the drawing. This
  renders each PDF page to an image client-side, sends them to the backend,
  which calls a VLM to propose HAZOP nodes (bounded process sections), and
  shows them back as cards in the chat panel with Accept/Reject/Undo.
- Accepting a node draws it as a real rectangle shape annotation on the PDF
  (`src/lib/annotationFromNode.ts`), by constructing `inklayer-react`'s own
  documented `Annotation` format directly (a plain object — `kind: 'shape'`,
  `target.geometry`, `appearance` — no simulated drawing or private internals
  involved) and feeding it back in via `initialAnnotations`. Because
  `PdfAnnotator` only reads that prop at mount, accepting a node forces a
  remount (`App.tsx`'s `annotatorKey`), first pulling out whatever the human
  already drew via `getAnnotations()` so nothing is lost.

## Not yet verified in a real browser

Everything above type-checks and builds, but this session never got a
working Claude-in-Chrome connection, so the actual on-canvas result of
accepting a node hasn't been visually confirmed — please try it (the
"Insert test node" dev button in the toolbar is the fastest way, no API key
needed) and check that the rectangle lands in the right place, sized right.

One correction already made this way: `initialAnnotations` doesn't render
through inklayer-react's newer "Core" geometry path directly —
`features/annotator/index.tsx` converts everything back into its legacy
`IAnnotationStore` shape, which needs a real serialized Konva group
(`extensions.konva.serialized`) to draw anything at all. A first attempt
without that produced no visible shape even though the annotation was
otherwise valid. `annotationFromNode.ts` now builds a real `Konva.Group` /
`Konva.Rect` (matching what the rectangle tool itself produces) and attaches
its serialization. If a shape still doesn't appear, check the browser
console for `[pid-viewer]` warnings first, then share what's there.

## Next step: exposing this to an agent as a tool

Right now "Analyze" and "Accept" are both driven by clicks in this tab. To
let an actual agent call shape-drawing as an MCP tool, something needs to
carry that command into this specific open browser session — e.g. a
WebSocket between the backend and the frontend, with the MCP tool handler
relaying "draw this node" over it. That plumbing doesn't exist yet.
