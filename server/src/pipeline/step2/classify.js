/**
 * Step 2 — process classification.
 *
 * Groups step 1's entity inventory into named processes. A process carries
 * NO coordinates of its own: it references entity ids, and its geometry —
 * bbox and member routes — is derived here as the union of those entities'
 * real, already-anchored geometry. This is what keeps step 4's eventual
 * annotation exactly as accurate as step 1 made it, however many stages of
 * classification and clustering sit in between.
 *
 * Split the same way as step 1: `collectPage` is the paid half (one model
 * call per page); `assemblePage` is the free, pure half (id validation,
 * geometry derivation, coverage diagnostics) — cached separately so
 * `cli.js remerge2` can replay it for free after a validation-logic change.
 */
import { renderRegion, toDataUrl } from '../pdf.js'
import { structuredCall, textPart, imagePart } from '../llm.js'
import { unionBboxes, clampBbox } from '../geometry.js'
import { PAGE_PROCESS_SCHEMA } from './schema.js'
import { PROCESS_SYSTEM_PROMPT, formatEntityInventory } from './prompts.js'

/** These categories describe or annotate other entities rather than doing
 *  anything themselves, so they add noise without helping the model group
 *  process functions — excluded from what it's shown, and from the coverage
 *  count (an unclassified note is not a gap; an unclassified pump is).
 *
 *  Exception: a step 1 entity synthesized from unclaimed tag-shaped text
 *  (`flags.synthesizedFromText`) is stored as category "label" purely
 *  because step 1 had nowhere else to put an item it couldn't identify —
 *  it is exactly the kind of real-but-uncertain item this stage must not
 *  silently drop. Filtering it out here would defeat the reason step 1
 *  keeps it at all: FT-202 was seen missing this way on the very first real
 *  run, still present in step 1's output, invisible to step 2 regardless. */
const NON_FUNCTIONAL_CATEGORIES = new Set(['note', 'label', 'equipment_list_row'])
export const isFunctional = (e) => !NON_FUNCTIONAL_CATEGORIES.has(e.category) || e.flags?.synthesizedFromText === true

/** Above this fraction of shared entityIds, two processes are probably
 *  describing the same thing rather than two related-but-distinct functions
 *  — surfaced as a hint for review, not auto-merged (that judgment, and the
 *  full candidate-pair graph, is step 3's job). */
const HIGH_OVERLAP_JACCARD = 0.6

export async function classifyDocument(doc, step1Result, options = {}) {
  const { pagePx = 2400, onProgress = () => {} } = options

  const collected = []
  for (const step1Page of step1Result.pages) {
    const page = await doc.getPage(step1Page.pageIndex + 1)
    collected.push(await collectPage(page, step1Page, { pagePx, onProgress }))
  }

  return { ...assembleDocument(collected, step1Result.fileName), raw: collected }
}

/** The paid half: one structured-output call per page. */
export async function collectPage(page, step1Page, opts) {
  const { pagePx, onProgress } = opts
  const pageIndex = step1Page.pageIndex

  const classifiable = step1Page.entities.filter(isFunctional)
  const { buffer } = await renderRegion(page, { x: 0, y: 0, width: 1, height: 1 }, { targetPx: pagePx })
  const listing = formatEntityInventory(classifiable)

  onProgress({ phase: 'page-start', pageIndex, classifiableEntities: classifiable.length })

  let parsed
  try {
    parsed = await structuredCall({
      system: PROCESS_SYSTEM_PROMPT,
      schema: PAGE_PROCESS_SCHEMA,
      schemaName: 'pid_page_processes',
      content: [
        textPart(`P&ID page index ${pageIndex}. ${classifiable.length} entities to classify follow, each with its id.`),
        imagePart(toDataUrl(buffer)),
        textPart(`Entity inventory (id [category] "tag" @x,y — type (attributes) connectsTo=[...]):\n${listing}`),
      ],
    })
    onProgress({ phase: 'page-done', pageIndex, processes: parsed.processes.length })
  } catch (err) {
    onProgress({ phase: 'page-failed', pageIndex, error: err.message })
    parsed = { processes: [], pageNotes: '', error: err.message }
  }

  return { pageIndex, step1Page, classifiable, raw: parsed }
}

/** Rebuilds the whole step 2 artifact from cached per-page collections. Pure. */
export function assembleDocument(collected, fileName) {
  const pages = collected.map(assemblePage)
  return {
    schemaVersion: 1,
    step: 'step2-classify',
    fileName,
    generatedAt: new Date().toISOString(),
    pages,
    totals: {
      processes: pages.reduce((n, p) => n + p.processes.length, 0),
      coveredEntities: pages.reduce((n, p) => n + p.diagnostics.coveredEntityIds.length, 0),
      uncoveredEntities: pages.reduce((n, p) => n + p.diagnostics.uncoveredEntityIds.length, 0),
    },
  }
}

/** The free half: raw model output -> validated processes with derived geometry. Pure. */
export function assemblePage(collected) {
  const { pageIndex, step1Page, classifiable, raw } = collected
  const entityById = new Map(step1Page.entities.map((e) => [e.id, e]))

  const processes = []
  const seenLocalIds = new Set()
  let n = 0

  for (const p of raw.processes ?? []) {
    const { validIds: entityIds, invalidIds: invalidEntityIds } = filterKnownIds(p.entityIds, entityById)
    if (entityIds.length === 0) continue // nothing real to anchor this process — drop it, not a phantom process

    n++
    const id = `p${pageIndex}-proc-${n}`
    const { validIds: primaryEquipmentIds } = filterKnownIds(p.primaryEquipmentIds, entityById)
    const { validIds: inletEntityIds } = filterKnownIds(p.inletEntityIds, entityById)
    const { validIds: outletEntityIds } = filterKnownIds(p.outletEntityIds, entityById)

    const members = entityIds.map((eid) => entityById.get(eid))
    const bbox = clampBbox(unionBboxes(members.map((e) => e.bbox)))
    const routes = members
      .filter((e) => e.category === 'line' && e.routes?.length)
      .flatMap((e) => e.routes.map((points) => ({ entityId: e.id, points })))

    const byCategory = {}
    for (const e of members) byCategory[e.category] = (byCategory[e.category] ?? 0) + 1

    processes.push({
      id,
      page: pageIndex,
      name: p.name,
      category: p.category,
      description: p.description,
      entityIds,
      primaryEquipmentIds: primaryEquipmentIds.length ? primaryEquipmentIds : entityIds,
      inletEntityIds,
      outletEntityIds,
      flowDirection: p.flowDirection ?? '',
      boundaryReasoning: p.boundaryReasoning ?? [],
      bbox,
      routes,
      entityCategoryCounts: byCategory,
      confidence: Number.isFinite(p.confidence) ? p.confidence : 0.5,
      flags: {
        hallucinatedIds: invalidEntityIds,
        lowConfidence: (Number.isFinite(p.confidence) ? p.confidence : 0.5) < 0.6,
        duplicateLocalId: seenLocalIds.has(p.localId),
      },
    })
    seenLocalIds.add(p.localId)
  }

  flagHighOverlapPairs(processes)

  const coveredEntityIds = [...new Set(processes.flatMap((p) => p.entityIds))]
  const coveredSet = new Set(coveredEntityIds)
  const uncoveredEntityIds = classifiable.filter((e) => !coveredSet.has(e.id)).map((e) => e.id)

  return {
    pageIndex,
    processes,
    diagnostics: {
      classifiableEntities: classifiable.length,
      coveredEntityIds,
      uncoveredEntityIds,
      pageNotes: raw.pageNotes ?? '',
      modelError: raw.error ?? null,
    },
  }

  // Observed on a real dense sheet: the model repeats an id within one
  // process's own list (e.g. "TV-1257004, TV-1257004") — harmless to bbox
  // union, but it inflates the displayed entity count and would double-count
  // entityCategoryCounts, so both valid and invalid ids are deduplicated,
  // order preserved.
  function filterKnownIds(ids, byId) {
    const validIds = []
    const invalidIds = []
    const seen = new Set()
    for (const eid of ids ?? []) {
      if (seen.has(eid)) continue
      seen.add(eid)
      if (byId.has(eid)) validIds.push(eid)
      else invalidIds.push(eid)
    }
    return { validIds, invalidIds }
  }
}

/** Marks pairs of processes that overlap heavily enough to likely be one
 *  thing described twice — a review hint, set on both processes involved. */
function flagHighOverlapPairs(processes) {
  for (let i = 0; i < processes.length; i++) {
    for (let j = i + 1; j < processes.length; j++) {
      const a = new Set(processes[i].entityIds)
      const b = new Set(processes[j].entityIds)
      const shared = [...a].filter((id) => b.has(id)).length
      if (shared === 0) continue
      const jaccard = shared / (a.size + b.size - shared)
      if (jaccard >= HIGH_OVERLAP_JACCARD) {
        processes[i].flags.highOverlapWith ??= []
        processes[j].flags.highOverlapWith ??= []
        processes[i].flags.highOverlapWith.push(processes[j].id)
        processes[j].flags.highOverlapWith.push(processes[i].id)
      }
    }
  }
}
