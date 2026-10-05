import type { HazopDetails, ProposedNode } from '../components/ChatPanel'

/**
 * Loader for the step1→2→3 pipeline's node output.
 *
 * `server/src/pipeline/step3/{cluster,consolidate}.js` write step3.json
 * (fine-grained, one node per un-merged HAZOP scope) and
 * step3-consolidated.json (target-driven, e.g. "at most 9 nodes") — both are
 * this same shape, just with a different node breakdown. There is no live
 * HTTP wiring from this frontend to that (still CLI-only) pipeline yet, so
 * this is loaded via a file picker (see App.tsx's "Load pipeline nodes"
 * button) rather than fetched — the natural next step once that plumbing
 * exists is to fetch it from a backend route instead of a local file.
 */
export interface PipelineNodeFile {
  fileName?: string
  targetMaxNodes?: number | null
  pages: PipelinePage[]
}

interface PipelinePage {
  pageIndex: number
  nodes: PipelineNode[]
}

interface PipelineNode {
  id: string
  label: string
  description: string
  categories: string[]
  processIds?: string[]
  memberCount?: number
  entityIds: string[]
  bbox: { x: number; y: number; width: number; height: number }
  routes: { entityId: string; points: { x: number; y: number }[] }[]
  /** Step 6 fields, on nodes from the agent's rules-based step 3. */
  hazop?: HazopDetails
}

/** Loose shape check — enough to catch "wrong file picked" (e.g. a
 * step1.json or step2.json export) without re-implementing the server's own
 * schema validation on the frontend. */
export function isPipelineNodeFile(value: unknown): value is PipelineNodeFile {
  if (!value || typeof value !== 'object') return false
  const v = value as Record<string, unknown>
  if (!Array.isArray(v.pages)) return false
  return v.pages.every((p) => p && typeof p === 'object' && Array.isArray((p as PipelinePage).nodes))
}

/**
 * Converts every node on every page into the frontend's ProposedNode shape.
 * Geometry (bbox, routes) is used exactly as the pipeline computed it —
 * normalized page fractions, top-left origin, same convention the rest of
 * this file's annotation code already expects — nothing is re-derived or
 * re-guessed here.
 */
export function pipelineNodesToProposedNodes(file: PipelineNodeFile): ProposedNode[] {
  return file.pages.flatMap((page) =>
    page.nodes.map(
      (node): ProposedNode => ({
        pageIndex: page.pageIndex,
        label: node.label,
        description: node.description,
        shapeType: 'hazop',
        categories: node.categories,
        bbox: node.bbox,
        routes: node.routes,
        entityCount: node.entityIds.length,
        processCount: node.memberCount ?? node.processIds?.length ?? 1,
        hazop: node.hazop,
      }),
    ),
  )
}
