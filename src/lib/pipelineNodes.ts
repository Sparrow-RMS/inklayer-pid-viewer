import type { HazopCause, HazopConsequence, HazopDetails, HazopDeviation, ProposedNode } from '../components/ChatPanel'

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
  routes: { entityId: string; points: { x: number; y: number }[]; flow?: boolean }[]
  /** Step 6 fields, on nodes from the agent's rules-based step 3. */
  hazop?: HazopDetails | LegacyHazop
}

/** A deviation written before causes were listed one by one: one
 * "credibleCause" sentence with the deviation's safeguards. */
type LegacyDeviation = { guideword: string; parameter: string; detail?: string; credibleCause: string; safeguards?: string[] }

/** A node written before per-parameter protection (one critical parameter,
 * deviations without safeguards). Files on disk may still be in this shape. */
type LegacyHazop = Omit<HazopDetails, 'parameters' | 'vacuumProtection' | 'keyDeviations'> & {
  protection?: { criticalParameter: string; highSafeguards: string[]; lowSafeguards: string[]; vacuumProtection: string | null }
  keyDeviations: LegacyDeviation[]
}

/** A cause written before consequences were listed: safeguards only. */
type LegacyCause = { cause: string; tags?: string[]; safeguards?: string[] }

/** A cause's consequences; safeguards alone go under one consequence still to be described. */
function consequencesOf(c: HazopCause | LegacyCause | LegacyDeviation): HazopConsequence[] {
  if ('consequences' in c) return c.consequences
  return c.safeguards?.length ? [{ consequence: '', safeguards: c.safeguards }] : []
}

function causesOf(d: HazopDeviation | LegacyDeviation): HazopCause[] {
  if ('causes' in d) return (d.causes as (HazopCause | LegacyCause)[]).map((c) => ({ cause: c.cause, tags: c.tags ?? [], consequences: consequencesOf(c) }))
  return [{ cause: d.credibleCause, tags: [], consequences: consequencesOf(d) }]
}

/** Same reading as the agent's rules.normalize_hazop_fields. */
function normalizeHazop(h: HazopDetails | LegacyHazop): HazopDetails {
  if ('parameters' in h) {
    const devs = h.keyDeviations as (HazopDeviation | LegacyDeviation)[]
    return { ...(h as HazopDetails), keyDeviations: devs.map((d) => ({ guideword: d.guideword, parameter: d.parameter, detail: d.detail ?? d.parameter, causes: causesOf(d) })) }
  }
  const { protection, ...rest } = h
  return {
    ...rest,
    parameters: protection
      ? [{ parameter: protection.criticalParameter, name: protection.criticalParameter, highSafeguards: protection.highSafeguards, lowSafeguards: protection.lowSafeguards }]
      : [],
    vacuumProtection: protection?.vacuumProtection ?? null,
    keyDeviations: h.keyDeviations.map((d) => ({
      guideword: d.guideword.toUpperCase() === 'MISDIRECTED' ? 'OTHER THAN' : d.guideword,
      parameter: d.parameter,
      detail: d.detail ?? d.parameter,
      causes: causesOf(d),
    })),
  }
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
        nodeId: node.id,
        label: node.label,
        description: node.description,
        shapeType: 'hazop',
        categories: node.categories,
        bbox: node.bbox,
        routes: node.routes,
        entityCount: node.entityIds.length,
        processCount: node.memberCount ?? node.processIds?.length ?? 1,
        hazop: node.hazop && normalizeHazop(node.hazop),
      }),
    ),
  )
}
