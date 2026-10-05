import Konva from 'konva'
import type { PdfAnnotatorProps } from 'inklayer-react'
import type { ProposedNode } from '../components/ChatPanel'

export type Annotation = NonNullable<PdfAnnotatorProps['initialAnnotations']>[number]

export type NodeReviewState = 'pending' | 'accepted'

export interface PageDimensions {
  /** pdfjs viewport size at scale 1 (CSS pixels) — this is also the
   *  coordinate space InkLayer's Konva canvas uses internally, top-left
   *  origin, no zoom baked in. */
  width: number
  height: number
}

// Must match inklayer-react's internal SHAPE_GROUP_NAME
// (src/extensions/annotator/painter/const.ts) — annotations are only
// recognized as its shape-group type if the wrapping Konva.Group has this
// exact name.
const SHAPE_GROUP_NAME = 'InkLayer_Annotator_shape_group'

const STROKE_WIDTH = 2
// Paths are the primary way nodes get marked (tracing the pipe route, like a
// highlighter), so they read better a bit bolder than a thin rect outline.
const PATH_STROKE_WIDTH = 3
const OPACITY = 1
// A node not yet accepted is drawn dashed; accepting it swaps to a solid
// stroke, same color/position/id — a visual state change, not a new shape.
const PENDING_DASH = [10, 6]

// Validated categorical palette (dataviz skill, references/palette.md) — 8
// slots pass adjacent-pair CVD/contrast gates in both modes. Past 8 nodes
// this cycles, which trades strict distinguishability for "good enough to
// segregate a handful of shapes that already carry their own text label."
const NODE_COLORS_LIGHT = [
  '#2a78d6', // blue
  '#eb6834', // orange
  '#1baf7a', // aqua
  '#eda100', // yellow
  '#e87ba4', // magenta
  '#008300', // green
  '#4a3aa7', // violet
  '#e34948', // red
]
const NODE_COLORS_DARK = [
  '#3987e5',
  '#d95926',
  '#199e70',
  '#c98500',
  '#d55181',
  '#008300',
  '#9085e9',
  '#e66767',
]

export function pickNodeColor(colorIndex: number): string {
  const isDark = typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches
  const palette = isDark ? NODE_COLORS_DARK : NODE_COLORS_LIGHT
  return palette[colorIndex % palette.length]
}

function hexToRgba(hex: string, alpha: number): string {
  const n = parseInt(hex.slice(1), 16)
  const r = (n >> 16) & 255
  const g = (n >> 8) & 255
  const b = n & 255
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

/**
 * Builds the same Konva.Group/Konva.Rect serialization InkLayer's own
 * rectangle tool produces when a human draws one (see
 * EditorRectangle.mouseDownHandler / Editor.dispatchAddEvent upstream).
 * initialAnnotations round-trips through the legacy IAnnotationStore shape
 * internally (features/annotator/index.tsx → annotationsToStores), which
 * reads `extensions.konva.serialized` for what to actually draw — without a
 * real Konva group there, nothing renders even though the annotation is
 * otherwise valid.
 */
function buildRectGroup(
  id: string,
  rect: { x: number; y: number; width: number; height: number },
  color: string,
  review: NodeReviewState,
) {
  const group = new Konva.Group({ draggable: false, name: SHAPE_GROUP_NAME, id })
  group.add(
    new Konva.Rect({
      x: rect.x,
      y: rect.y,
      width: rect.width,
      height: rect.height,
      strokeScaleEnabled: false,
      visible: true,
      stroke: color,
      strokeWidth: STROKE_WIDTH,
      dash: review === 'pending' ? PENDING_DASH : undefined,
      opacity: OPACITY,
    }),
  )
  return group
}

/**
 * Same idea, but for a node traced along its process flow path — points
 * ordered upstream-to-downstream, with an arrowhead at the end showing flow
 * direction. Konva.Arrow accepts a multi-point `points` array just like
 * Konva.Line (it draws the pointer off the final segment's direction), so
 * this is EditorArrow's shape with more than two points, no smoothing (no
 * `tension`) so it stays sharp through 90° pipe bends instead of rounding
 * them off.
 */
function buildPathGroup(
  id: string,
  points: { x: number; y: number }[],
  color: string,
  review: NodeReviewState,
) {
  const group = new Konva.Group({ draggable: false, name: SHAPE_GROUP_NAME, id })
  group.add(
    new Konva.Arrow({
      points: points.flatMap((p) => [p.x, p.y]),
      stroke: color,
      fill: color,
      strokeWidth: PATH_STROKE_WIDTH,
      dash: review === 'pending' ? PENDING_DASH : undefined,
      pointerLength: 12,
      pointerWidth: 12,
      lineCap: 'round',
      lineJoin: 'round',
      hitStrokeWidth: 20,
      strokeScaleEnabled: false,
      visible: true,
      opacity: OPACITY,
    }),
  )
  return group
}

/**
 * A traced route as an Annotation. kind 'line' (InkLayer's own arrow tool,
 * P.ARROW / pdfjs LINE) rather than 'ink': Export PDF picks its writer from
 * the kind, and the Ink writer only reads Konva.Line children, so it silently
 * dropped these Konva.Arrow shapes and exported empty markups. The Line
 * writer handles multi-point arrows, arrowhead included.
 */
function routeAnnotation(
  id: string,
  pageIndex: number,
  points: { x: number; y: number }[],
  color: string,
  review: NodeReviewState,
  meta: Annotation['meta'],
  hazopNode: Record<string, unknown>,
): Annotation {
  const konvaGroup = buildPathGroup(id, points, color, review)
  const serialized = konvaGroup.toJSON()
  const clientRect = Konva.Node.create(serialized).getClientRect()

  return {
    id,
    kind: 'line',
    target: {
      pageIndex,
      geometry: { type: 'path', points, closed: false },
      coordinateSystem: 'pdf-user-space',
    },
    payload: { kind: 'line', arrowStart: false, arrowEnd: true },
    appearance: { strokeColor: color, strokeWidth: PATH_STROKE_WIDTH, opacity: OPACITY },
    meta,
    extensions: { konva: { serialized, clientRect }, hazopNode },
  } as Annotation
}

/**
 * Converts a VLM-proposed node (normalized, top-left-origin image fractions)
 * into an InkLayer shape or path Annotation.
 *
 * `id`, `colorIndex`, and `nodeNumber` are supplied by the caller and must
 * stay the same across calls for the same node (e.g. pending → accepted):
 * reusing `id` means the shape already on canvas gets restyled in place on
 * the next remount rather than a second copy being drawn; a stable, unique
 * `nodeNumber` is what lets InkLayer's own hover/selection label ("#N ·
 * Author") read as "#2 · Node 2" instead of an arbitrary, ever-climbing
 * number — its numbering system only overrides numbers it hasn't seen
 * before or that collide, so a number we've already used for this node
 * stays put (see references/annotation_numbering.ts upstream).
 */
export function nodeToAnnotation(
  node: ProposedNode,
  pageDims: PageDimensions,
  author: { id: string; name: string },
  colorIndex: number,
  id: string,
  review: NodeReviewState,
  nodeNumber: number,
): Annotation {
  const color = pickNodeColor(colorIndex)
  const meta = {
    authorId: { id: author.id, name: `Node ${nodeNumber}` },
    referenceNumber: nodeNumber,
    createdAt: new Date().toISOString(),
    isNative: false,
    source: 'inklayer' as const,
  }
  const hazopNode = { label: node.label, description: node.description, review }

  if (node.shapeType === 'path') {
    const points = node.path.map((p) => ({ x: p.x * pageDims.width, y: p.y * pageDims.height }))
    return routeAnnotation(id, node.pageIndex, points, color, review, meta, hazopNode)
  }

  const rect = {
    x: node.bbox.x * pageDims.width,
    y: node.bbox.y * pageDims.height,
    width: node.bbox.width * pageDims.width,
    height: node.bbox.height * pageDims.height,
  }

  const konvaGroup = buildRectGroup(id, rect, color, review)
  const serialized = konvaGroup.toJSON()
  const clientRect = Konva.Node.create(serialized).getClientRect()

  return {
    id,
    kind: 'shape',
    target: {
      pageIndex: node.pageIndex,
      geometry: { type: 'rect', rect },
      coordinateSystem: 'pdf-user-space',
    },
    payload: { kind: 'shape', shape: 'rect' },
    appearance: { strokeColor: color, fillColor: hexToRgba(color, 0.15), strokeWidth: STROKE_WIDTH, opacity: OPACITY },
    meta,
    extensions: { konva: { serialized, clientRect }, hazopNode },
  } as Annotation
}

/**
 * Same idea as `nodeToAnnotation`, but for a node produced by the
 * step1→2→3 pipeline (`server/src/pipeline`) rather than the single-shot
 * analyze endpoint. The key difference: a clustered HAZOP node can span many
 * lines — step 3 unions its member processes' traced routes, and a
 * consolidated node (e.g. "the whole ST-V305 train") can carry dozens — so
 * one node maps to an ARRAY of annotations here, not one. They share a
 * common `idPrefix` (so pending → accepted restyles the same shapes rather
 * than drawing new ones), the same color, and the same `nodeNumber`, so they
 * read as one logical node on canvas and in the merge menu despite being
 * several underlying Konva shapes.
 *
 * A node with no traced line at all (a pure equipment/utility function, e.g.
 * "instrument air supply") falls back to a single rect over its overall
 * extent — the same fallback `nodeToAnnotation` uses, so every node is
 * guaranteed at least one visible shape.
 */
export function hazopNodeToAnnotations(
  node: Extract<ProposedNode, { shapeType: 'hazop' }>,
  pageDims: PageDimensions,
  author: { id: string; name: string },
  colorIndex: number,
  idPrefix: string,
  review: NodeReviewState,
  nodeNumber: number,
): Annotation[] {
  const color = pickNodeColor(colorIndex)
  const meta = {
    authorId: { id: author.id, name: `Node ${nodeNumber}` },
    referenceNumber: nodeNumber,
    createdAt: new Date().toISOString(),
    isNative: false,
    source: 'inklayer' as const,
  }
  const hazopNode = { label: node.label, description: node.description, review, categories: node.categories }

  if (node.routes.length > 0) {
    return node.routes.map((route, i) => {
      const id = `${idPrefix}-r${i}`
      const points = route.points.map((p) => ({ x: p.x * pageDims.width, y: p.y * pageDims.height }))
      return routeAnnotation(id, node.pageIndex, points, color, review, meta, { ...hazopNode, entityId: route.entityId })
    })
  }

  const id = `${idPrefix}-rect`
  const rect = {
    x: node.bbox.x * pageDims.width,
    y: node.bbox.y * pageDims.height,
    width: node.bbox.width * pageDims.width,
    height: node.bbox.height * pageDims.height,
  }

  const konvaGroup = buildRectGroup(id, rect, color, review)
  const serialized = konvaGroup.toJSON()
  const clientRect = Konva.Node.create(serialized).getClientRect()

  return [
    {
      id,
      kind: 'shape',
      target: {
        pageIndex: node.pageIndex,
        geometry: { type: 'rect', rect },
        coordinateSystem: 'pdf-user-space',
      },
      payload: { kind: 'shape', shape: 'rect' },
      appearance: { strokeColor: color, fillColor: hexToRgba(color, 0.15), strokeWidth: STROKE_WIDTH, opacity: OPACITY },
      meta,
      extensions: { konva: { serialized, clientRect }, hazopNode },
    } as Annotation,
  ]
}
