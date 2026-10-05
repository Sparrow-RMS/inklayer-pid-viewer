/**
 * Step 3, consolidation pass — force the node count down to a target.
 *
 * The base pass (`cluster.js`) answers "is there a real HAZOP boundary
 * between A and B?" independently per pair, deliberately biased toward "no"
 * — it has no notion of a target count and no mechanism to guarantee one.
 * This pass exists for exactly the case where that isn't enough: it scores
 * EVERY pair's continuous relatedness, then greedily merges the strongest
 * remaining pair, re-checking the count after each merge, until the target is
 * met — classic agglomerative clustering. It only runs when the base pass
 * already left more nodes than the target permits.
 *
 * The guarantee this makes explicit: merges are applied strongest-first, so
 * whatever ends up combined is the best-related option available at that
 * point. If reaching the target requires going below a sane relatedness
 * floor, that merge is still made (the target is honored), but it is
 * flagged in `diagnostics.appliedMerges[].forced` rather than presented as
 * if it were a natural grouping — a hard cap and an honest audit trail are
 * not in tension, and this keeps both.
 */
import { structuredCall, mapLimit, textPart } from '../llm.js'
import { DisjointSet } from '../dsu.js'
import { unionBboxes, clampBbox } from '../geometry.js'
import { RELATEDNESS_SCHEMA } from './schema.js'
import { buildConsolidateSystemPrompt, formatNodePairs } from './prompts.js'

const DEFAULT_MAX_PAIRS_PER_CALL = 40
/** Below this relatedness, a merge is only made because the target forces
 *  it, never because the pair looks genuinely related. */
const DEFAULT_MIN_RELATEDNESS = 0.3

export async function consolidateDocument(step3Result, step1Result, options = {}) {
  const collected = []
  for (const page of step3Result.pages) {
    const step1Page = step1Result.pages.find((p) => p.pageIndex === page.pageIndex)
    const entityById = new Map(step1Page.entities.map((e) => [e.id, e]))
    collected.push(await collectPage(page, entityById, options))
  }
  return { ...assembleDocument(collected, step3Result.fileName, options), raw: collected }
}

/** The paid half: scores every pair among the current nodes. Skipped
 *  entirely (no model call) if already at or under target. */
export async function collectPage(page, entityById, opts = {}) {
  const {
    targetMaxNodes,
    maxPairsPerCall = DEFAULT_MAX_PAIRS_PER_CALL,
    concurrency = 3,
    onProgress = () => {},
  } = opts
  const pageIndex = page.pageIndex
  const nodes = page.nodes

  if (!targetMaxNodes || nodes.length <= targetMaxNodes) {
    onProgress({ phase: 'consolidate-skip', pageIndex, nodes: nodes.length, targetMaxNodes })
    return { pageIndex, nodes, pairs: [], scores: [], error: null }
  }

  const nodeById = new Map(nodes.map((n) => [n.id, n]))
  const pairs = []
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      pairs.push({ id: `k${pairs.length + 1}`, aId: nodes[i].id, bId: nodes[j].id })
    }
  }

  onProgress({ phase: 'consolidate-start', pageIndex, nodes: nodes.length, targetMaxNodes, pairs: pairs.length })

  const chunks = []
  for (let i = 0; i < pairs.length; i += maxPairsPerCall) chunks.push(pairs.slice(i, i + maxPairsPerCall))

  const scores = []
  let error = null
  const system = buildConsolidateSystemPrompt(nodes.length, targetMaxNodes)
  await mapLimit(chunks, concurrency, async (chunk, idx) => {
    try {
      const listing = formatNodePairs(chunk, nodeById, entityById)
      const parsed = await structuredCall({
        system,
        schema: RELATEDNESS_SCHEMA,
        schemaName: 'pid_node_relatedness',
        content: [
          textPart(`Page ${pageIndex}. ${nodes.length} nodes currently, target ${targetMaxNodes}. ${chunk.length} pairs follow.`),
          textPart(listing),
        ],
      })
      scores.push(...(parsed.scores ?? []))
      onProgress({ phase: 'consolidate-chunk-done', pageIndex, chunk: idx, scores: parsed.scores?.length ?? 0 })
    } catch (err) {
      error = err.message
      onProgress({ phase: 'consolidate-chunk-failed', pageIndex, chunk: idx, error: err.message })
    }
  })

  return { pageIndex, nodes, pairs, scores, error }
}

export function assembleDocument(collected, fileName, options = {}) {
  const { targetMaxNodes, minRelatedness = DEFAULT_MIN_RELATEDNESS } = options
  const pages = collected.map((c) => assemblePage(c, targetMaxNodes, minRelatedness))
  return {
    schemaVersion: 1,
    step: 'step3-consolidate',
    fileName,
    targetMaxNodes: targetMaxNodes ?? null,
    generatedAt: new Date().toISOString(),
    pages,
    totals: { nodes: pages.reduce((n, p) => n + p.nodes.length, 0) },
  }
}

/** The free half: relatedness scores -> greedy strongest-first merging via
 *  DisjointSet, stopping the instant the target is met. Pure. */
export function assemblePage(collected, targetMaxNodes, minRelatedness = DEFAULT_MIN_RELATEDNESS) {
  const { pageIndex, nodes, pairs, scores, error } = collected

  if (!targetMaxNodes || nodes.length <= targetMaxNodes) {
    return {
      pageIndex,
      nodes,
      diagnostics: { skipped: true, reason: `already at or under target (${nodes.length} <= ${targetMaxNodes ?? '∞'})` },
    }
  }

  const pairById = new Map(pairs.map((p) => [p.id, p]))
  const scoreByPairId = new Map()
  const invalidPairIds = []
  for (const s of scores) {
    if (!pairById.has(s.pairId)) {
      invalidPairIds.push(s.pairId)
      continue
    }
    const r = Number.isFinite(s.relatedness) ? Math.max(0, Math.min(1, s.relatedness)) : 0
    scoreByPairId.set(s.pairId, { relatedness: r, reasoning: s.reasoning })
  }
  const missingScores = pairs.map((p) => p.id).filter((id) => !scoreByPairId.has(id))

  const dsu = new DisjointSet()
  for (const n of nodes) dsu.add(n.id)

  // Strongest relation first — whatever gets combined is the best-related
  // option available at the point it was merged. A pair with no score
  // (model never answered it) is treated as relatedness 0: eligible only as
  // an absolute last resort, never preferred over a scored pair.
  const ranked = pairs
    .map((p) => ({ ...p, ...(scoreByPairId.get(p.id) ?? { relatedness: 0, reasoning: null }) }))
    .sort((a, b) => b.relatedness - a.relatedness)

  const appliedMerges = []
  let groupCount = nodes.length
  for (const p of ranked) {
    if (groupCount <= targetMaxNodes) break
    const joined = dsu.union(p.aId, p.bId) // false if already in the same group — costs nothing, just skip
    if (!joined) continue
    groupCount--
    appliedMerges.push({
      aId: p.aId,
      bId: p.bId,
      relatedness: p.relatedness,
      reasoning: p.reasoning,
      forced: p.relatedness < minRelatedness,
    })
  }

  const nodeById = new Map(nodes.map((n) => [n.id, n]))
  const consolidatedNodes = dsu
    .groups()
    .map((memberIds) => buildConsolidatedNode(memberIds, nodeById))
    .sort((a, b) => a.bbox.y - b.bbox.y || a.bbox.x - b.bbox.x)
    .map((n, i) => ({ id: `p${pageIndex}-cnode-${i + 1}`, page: pageIndex, ...n }))

  return {
    pageIndex,
    nodes: consolidatedNodes,
    diagnostics: {
      skipped: false,
      startingNodes: nodes.length,
      targetMaxNodes,
      finalNodes: consolidatedNodes.length,
      reachedTarget: consolidatedNodes.length <= targetMaxNodes,
      pairsScored: scores.length,
      invalidPairIds,
      missingScores,
      appliedMerges,
      forcedMergeCount: appliedMerges.filter((m) => m.forced).length,
      modelError: error,
    },
  }
}

function buildConsolidatedNode(memberNodeIds, nodeById) {
  const members = memberNodeIds.map((id) => nodeById.get(id))
  const entityIds = [...new Set(members.flatMap((n) => n.entityIds))]
  const primaryEquipmentIds = [...new Set(members.flatMap((n) => n.primaryEquipmentIds))]
  const processIds = [...new Set(members.flatMap((n) => n.processIds))]
  const bbox = clampBbox(unionBboxes(members.map((n) => n.bbox)))

  const routesByEntity = new Map()
  for (const n of members) for (const r of n.routes) if (!routesByEntity.has(r.entityId)) routesByEntity.set(r.entityId, r)

  const categories = [...new Set(members.flatMap((n) => n.categories))]
  const label = members.length === 1 ? members[0].label : members.map((n) => n.label).join(' + ')
  const description = members.map((n) => n.description).join(' ')

  return {
    label,
    description,
    categories,
    // The original fine-grained nodes folded into this one — nothing about
    // the detailed breakdown is lost, it's just grouped under one broader
    // review scope. Useful for an "expand this node" view later.
    subNodeIds: memberNodeIds,
    processIds,
    memberCount: processIds.length,
    entityIds,
    primaryEquipmentIds,
    bbox,
    routes: [...routesByEntity.values()],
  }
}
