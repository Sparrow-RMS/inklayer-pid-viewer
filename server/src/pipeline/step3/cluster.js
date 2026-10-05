/**
 * Step 3 — cluster processes into HAZOP nodes.
 *
 * The model is only ever asked "do processes A and B belong in the same
 * node?" for a bounded set of candidate pairs (`candidates.js`, no model
 * call). The actual grouping is arithmetic: "yes" verdicts are unioned with
 * the same `DisjointSet` step 1 uses to merge duplicate tile sightings, so
 * the transitive closure (A~B, B~C => A~B~C) is guaranteed correct rather
 * than re-derived by the model — which also means it can never accidentally
 * contradict itself between two overlapping calls.
 *
 * A node carries no coordinates of its own, same rule as every stage before
 * it: bbox and routes are the union of its member processes' already-real
 * geometry, which is itself the union of step 1's already-real entity
 * geometry.
 */
import { generateCandidatePairs } from './candidates.js'
import { PAIR_VERDICT_SCHEMA } from './schema.js'
import { CLUSTER_SYSTEM_PROMPT, formatCandidatePairs } from './prompts.js'
import { structuredCall, mapLimit, textPart } from '../llm.js'
import { DisjointSet } from '../dsu.js'
import { unionBboxes, clampBbox } from '../geometry.js'

/** Caps how many pairs go in one call's prompt — a page with many processes
 *  can generate more candidates than comfortably fits one structured-output
 *  call, so beyond this it's chunked (mapLimit) rather than truncated. */
const MAX_PAIRS_PER_CALL = 40
/** A "yes" below this confidence doesn't get applied — same spirit as every
 *  other stage's low-confidence handling: recorded, not silently trusted. */
const SAME_NODE_CONFIDENCE_THRESHOLD = 0.5

export async function clusterDocument(step2Result, step1Result, options = {}) {
  const collected = []
  for (const page of step2Result.pages) {
    const step1Page = step1Result.pages.find((p) => p.pageIndex === page.pageIndex)
    collected.push(await collectPage(page, step1Page, options))
  }
  return { ...assembleDocument(collected, step2Result.fileName), raw: collected }
}

/** The paid half: candidate generation is free; only the pairwise verdict
 *  call(s) touch the model. */
export async function collectPage(page, step1Page, opts = {}) {
  const { onProgress = () => {}, maxPairsPerCall = MAX_PAIRS_PER_CALL, concurrency = 3 } = opts
  const pageIndex = page.pageIndex
  const entityById = new Map(step1Page.entities.map((e) => [e.id, e]))
  const processById = new Map(page.processes.map((p) => [p.id, p]))

  const pairs = generateCandidatePairs(page.processes, entityById)
  onProgress({ phase: 'page-start', pageIndex, processes: page.processes.length, candidatePairs: pairs.length })

  if (pairs.length === 0) {
    return { pageIndex, processes: page.processes, pairs, verdicts: [], error: null }
  }

  const chunks = []
  for (let i = 0; i < pairs.length; i += maxPairsPerCall) chunks.push(pairs.slice(i, i + maxPairsPerCall))

  const verdicts = []
  let error = null
  await mapLimit(chunks, concurrency, async (chunk, idx) => {
    try {
      const listing = formatCandidatePairs(chunk, processById, entityById)
      const parsed = await structuredCall({
        system: CLUSTER_SYSTEM_PROMPT,
        schema: PAIR_VERDICT_SCHEMA,
        schemaName: 'pid_node_cluster_verdicts',
        content: [
          textPart(`Page index ${pageIndex}. ${chunk.length} candidate process pairs follow.`),
          textPart(listing),
        ],
      })
      verdicts.push(...(parsed.verdicts ?? []))
      onProgress({ phase: 'chunk-done', pageIndex, chunk: idx, verdicts: parsed.verdicts?.length ?? 0 })
    } catch (err) {
      error = err.message
      onProgress({ phase: 'chunk-failed', pageIndex, chunk: idx, error: err.message })
    }
  })

  return { pageIndex, processes: page.processes, pairs, verdicts, error }
}

export function assembleDocument(collected, fileName) {
  const pages = collected.map(assemblePage)
  return {
    schemaVersion: 1,
    step: 'step3-cluster',
    fileName,
    generatedAt: new Date().toISOString(),
    pages,
    totals: {
      nodes: pages.reduce((n, p) => n + p.nodes.length, 0),
      processesClustered: pages.reduce((n, p) => n + p.processes.length, 0),
    },
  }
}

/** The free half: pairwise verdicts -> disjoint-set union -> final nodes
 *  with derived geometry. Pure. */
export function assemblePage(collected) {
  const { pageIndex, processes, pairs, verdicts, error } = collected
  const processById = new Map(processes.map((p) => [p.id, p]))
  const pairById = new Map((pairs ?? []).map((p) => [p.id, p]))

  const dsu = new DisjointSet()
  for (const p of processes) dsu.add(p.id)

  const appliedVerdicts = []
  const invalidPairIds = []
  const seenPairIds = new Set()

  for (const v of verdicts ?? []) {
    const pair = pairById.get(v.pairId)
    if (!pair) {
      invalidPairIds.push(v.pairId)
      continue
    }
    seenPairIds.add(v.pairId)
    const confidence = Number.isFinite(v.confidence) ? v.confidence : 0.5
    const applied = v.sameNode === true && confidence >= SAME_NODE_CONFIDENCE_THRESHOLD
    appliedVerdicts.push({ pairId: v.pairId, aId: pair.aId, bId: pair.bId, sameNode: v.sameNode, confidence, reasoning: v.reasoning, applied })
    if (applied) dsu.union(pair.aId, pair.bId)
  }

  const missingVerdicts = (pairs ?? []).map((p) => p.id).filter((id) => !seenPairIds.has(id))

  const nodes = dsu
    .groups()
    .map((memberProcessIds) => buildNode(memberProcessIds, processById))
    .sort((a, b) => a.bbox.y - b.bbox.y || a.bbox.x - b.bbox.x)
    .map((node, i) => ({ id: `p${pageIndex}-node-${i + 1}`, page: pageIndex, ...node }))

  return {
    pageIndex,
    processes,
    nodes,
    diagnostics: {
      candidatePairs: (pairs ?? []).length,
      verdictsReceived: (verdicts ?? []).length,
      verdictsApplied: appliedVerdicts.filter((v) => v.applied).length,
      invalidPairIds,
      missingVerdicts,
      modelError: error,
      // Full audit trail — which pairs merged, on what reasoning, and which
      // "yes" verdicts were withheld for low confidence. What a reviewer
      // checks first if a node looks wrong.
      appliedVerdicts,
    },
  }
}

function buildNode(memberProcessIds, processById) {
  const members = memberProcessIds.map((id) => processById.get(id))
  const entityIds = [...new Set(members.flatMap((p) => p.entityIds))]
  const primaryEquipmentIds = [...new Set(members.flatMap((p) => p.primaryEquipmentIds))]
  const bbox = clampBbox(unionBboxes(members.map((p) => p.bbox)))

  // A boundary entity's line can legitimately appear in two merged
  // processes' own route lists — deduplicated by entityId so step 4 doesn't
  // draw the same traced pipe run twice.
  const routesByEntity = new Map()
  for (const p of members) for (const r of p.routes) if (!routesByEntity.has(r.entityId)) routesByEntity.set(r.entityId, r)

  const categories = [...new Set(members.map((p) => p.category))]

  // Deterministic composition for v1 — good enough to review and correct,
  // not styled prose. An LLM polish pass per final node is a natural
  // follow-up once the clustering itself is trusted; see the README.
  const label = members.length === 1 ? members[0].name : members.map((p) => p.name).join(' + ')
  const description = members.map((p) => p.description).join(' ')

  return {
    label,
    description,
    categories,
    processIds: memberProcessIds,
    memberCount: members.length,
    entityIds,
    primaryEquipmentIds,
    bbox,
    routes: [...routesByEntity.values()],
  }
}
