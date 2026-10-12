/**
 * Step 1 — exhaustive extraction.
 *
 * Produces, per page: the exact PDF text layer, the exact stroked vector
 * geometry, a VLM-read inventory of every entity on the sheet, and the
 * drawing-level metadata. Entities are read tile by tile and then merged,
 * because the tiles deliberately overlap.
 *
 * The coordinates that matter come from the text layer, not from the model:
 * an entity that anchored its tag to real text carries a `tagBbox` accurate
 * to the PDF's own precision. That is what makes step 4's annotation land in
 * the right place.
 */
import { pageGeometry, extractTextItems, extractVectorSegments, renderRegion, toDataUrl } from '../pdf.js'
import { planTiles, bboxToTile, bboxFromTile, pointFromTile, textItemsInTile } from '../tiling.js'
import { structuredCall, mapLimit, textPart, imagePart } from '../llm.js'
import { DisjointSet } from '../dsu.js'
import { unionBbox, unionBboxes, clampBbox, iou, bboxGap, bboxFromPoints, containment, bboxCenter, distance, pointInBbox } from '../geometry.js'
import { TILE_SCHEMA, PAGE_META_SCHEMA } from './schema.js'
import { TILE_SYSTEM_PROMPT, PAGE_META_SYSTEM_PROMPT, formatTextItems } from './prompts.js'

const CATEGORY_ABBREV = {
  equipment: 'eq',
  instrument: 'in',
  valve: 'vl',
  line: 'ln',
  fitting: 'ft',
  connector: 'cn',
  note: 'nt',
  label: 'lb',
  equipment_list_row: 'row',
  interlock_ref: 'il',
}

/** Tags are compared with separators and case stripped, so "P-101 A",
 *  "P-101A" and "p101a" are one pump. Distinct tags stay distinct: the only
 *  characters removed are spacing and punctuation. */
const normalizeTag = (tag) => (tag ? tag.toUpperCase().replace(/[\s\-_./\\]/g, '') : null)

/** Two sightings of one tagged item can only be the same item if they are
 *  also near each other — otherwise a tag repeated as a cross-reference
 *  elsewhere on the sheet would drag both into one entity with a bbox
 *  spanning the gap. Sized a little above the tile overlap margin, which is
 *  the largest genuine separation two sightings of one symbol can have. */
const SAME_TAG_MAX_GAP = 0.06
const CROSS_TILE_MERGE_IOU = 0.45
/** IoU punishes a pair of boxes that disagree on extent even when one all
 *  but sits inside the other — which is exactly how two tiles describe one
 *  exchanger, one boxing the shell and the other the shell plus its nozzles.
 *  Containment catches that pair; IoU alone left it as two entities. */
const CROSS_TILE_MERGE_CONTAINMENT = 0.6
/** Below this the model is telling us it is guessing. Kept, never dropped —
 *  step 1's job is coverage — but flagged so a reviewer sees it first. */
const LOW_CONFIDENCE = 0.6

/** Text shaped like an equipment/instrument tag ("P-101A", "FT 1234") or a
 *  line number ('8"-CR-2203-A1'). Used to decide whether a text item nobody
 *  claimed is a real missed item or just prose. */
const TAG_PATTERN = /^[A-Z]{1,5}[-\s]?\d{2,5}[A-Z]?$/
const LINE_NUMBER_PATTERN = /^\d{1,2}"?\s*-\s*[A-Z]{1,4}\s*-\s*\d{2,6}/
const looksLikeTag = (str) => TAG_PATTERN.test(str.trim()) || LINE_NUMBER_PATTERN.test(str.trim())

export async function extractDocument(doc, options = {}) {
  const {
    fileName = 'document.pdf',
    pageIndexes = null,
    concurrency = 4,
    tiling = {},
    tilePx = 1600,
    pagePx = 2200,
    includeVectors = true,
    onProgress = () => {},
  } = options

  const allPages = Array.from({ length: doc.numPages }, (_, i) => i)
  const targets = pageIndexes?.length ? allPages.filter((i) => pageIndexes.includes(i)) : allPages

  const collected = []
  for (const pageIndex of targets) {
    const page = await doc.getPage(pageIndex + 1)
    collected.push(await collectPage(page, pageIndex, { concurrency, tiling, tilePx, pagePx, includeVectors, onProgress }))
  }

  // `raw` is the cache `assembleDocument` replays; the caller writes it to a
  // separate artifact rather than inlining it here, since it duplicates the
  // text and vector layers.
  return { ...assembleDocument(collected, fileName, doc.numPages), raw: collected }
}

/** Rebuilds the step 1 artifact from cached per-page collections. Pure. */
export function assembleDocument(collected, fileName, pageCount) {
  const pages = collected.map(assemblePage)
  return {
    schemaVersion: 1,
    step: 'step1-extract',
    fileName,
    pageCount,
    generatedAt: new Date().toISOString(),
    pages,
    totals: {
      entities: pages.reduce((n, p) => n + p.entities.length, 0),
      textItems: pages.reduce((n, p) => n + p.textItems.length, 0),
      vectorSegments: pages.reduce((n, p) => n + p.vectorSegments.length, 0),
    },
  }
}

/**
 * The paid half: everything that needs the PDF and the model.
 *
 * Kept separate from `assemblePage` so the raw tile responses can be cached
 * and the deterministic half re-run against them. Merging, anchoring and
 * de-duplication are exactly the parts worth tuning, and tuning them should
 * not cost another full pass over the sheet.
 */
export async function collectPage(page, pageIndex, opts) {
  const { concurrency, tiling, tilePx, pagePx, includeVectors, onProgress } = opts

  const geometry = pageGeometry(page, pageIndex)
  const textItems = await extractTextItems(page, pageIndex)
  const vectorSegments = includeVectors ? await extractVectorSegments(page, pageIndex) : []

  const plan = planTiles(geometry, tiling)
  onProgress({
    phase: 'page-start',
    pageIndex,
    tiles: plan.tiles.length,
    textItems: textItems.length,
    vectorSegments: vectorSegments.length,
  })

  // The whole-page metadata pass and the tile passes are independent, so the
  // page pass runs alongside the tiles rather than serially before them.
  const metaPromise = readPageMeta(page, pageIndex, textItems, pagePx).catch((err) => {
    onProgress({ phase: 'page-meta-failed', pageIndex, error: err.message })
    return { error: err.message }
  })

  const tileResults = await mapLimit(plan.tiles, concurrency, async (tile) => {
    try {
      const result = await readTile(page, tile, textItems, tilePx)
      onProgress({ phase: 'tile-done', pageIndex, tile: tile.id, entities: result.entities.length })
      return { tile, ...result }
    } catch (err) {
      onProgress({ phase: 'tile-failed', pageIndex, tile: tile.id, error: err.message })
      return { tile, entities: [], tileNotes: '', error: err.message }
    }
  })

  return { pageIndex, geometry, textItems, vectorSegments, plan, tileResults, drawingMeta: await metaPromise }
}

/**
 * The free half: tile sightings -> one clean, de-duplicated inventory.
 *
 * Pure and deterministic, so `cli.js remerge` can replay it over cached raw
 * responses as often as the rules need adjusting.
 */
export function assemblePage(collected) {
  const { pageIndex, geometry, textItems, vectorSegments, plan, tileResults, drawingMeta } = collected
  const textById = new Map(textItems.map((t) => [t.id, t]))

  const sightings = []
  for (const { tile, entities } of tileResults) {
    entities.forEach((raw, i) => {
      const sighting = normalizeSighting(raw, tile, textById, i)
      if (sighting) sightings.push(sighting)
    })
  }

  // Order matters: anchors must be made exclusive BEFORE merging, since
  // merge rule 1 treats a shared anchor as proof two sightings are the same
  // entity — and a stolen anchor would then fuse two different ones.
  const strippedAnchors = enforceAnchorExclusivity(sightings, textById)
  // Tile-local ids ("v3") only mean something inside their own tile, and
  // every tile reuses them, so they are pinned to the sighting they name
  // BEFORE merging pools connectsTo across tiles.
  qualifyLocalRefs(sightings, tileResults)
  const owner = new Map()
  const entities = mergeSightings(sightings, pageIndex, textById, owner)
  localizeConnections(entities, owner)

  // Split bubble tags ("FT" over "202") are reassembled first, so the two
  // passes that follow can recognise them as the single tags they are.
  const composites = findCompositeTags(textItems)
  const releasedTags = releaseForeignCompositeTags(entities, composites, textById)
  const attachedTags = attachUnclaimedTags(entities, textItems, composites, textById)

  // Last, so it only fires on text that survived every attempt to give it a
  // real owner.
  const synthesized = synthesizeUnclaimedTags(entities, textItems, composites, drawingMeta, pageIndex)
  entities.push(...synthesized)
  entities.sort((a, b) => a.bbox.y - b.bbox.y || a.bbox.x - b.bbox.x)

  resolveConnections(entities)

  return {
    ...geometry,
    tiling: { cols: plan.cols, rows: plan.rows, overlap: plan.overlap, tileCount: plan.tiles.length },
    drawingMeta,
    entities,
    textItems,
    vectorSegments,
    diagnostics: {
      tileSightings: sightings.length,
      mergedEntities: entities.length,
      // Tags one entity took from another, resolved back to the nearer one.
      strippedAnchors,
      // Bubble tags an entity had absorbed from its neighbour, handed back.
      releasedTags,
      // Untagged entities that got their tag back from unclaimed text.
      attachedTags,
      // Tag-shaped text the model never accounted for — the honest measure
      // of what this page's extraction missed.
      synthesizedFromText: synthesized.map((e) => ({ id: e.id, tag: e.tag })),
      failedTiles: tileResults.filter((r) => r.error).map((r) => ({ tile: r.tile.id, error: r.error })),
      tileNotes: tileResults.filter((r) => r.tileNotes).map((r) => ({ tile: r.tile.id, note: r.tileNotes })),
    },
  }
}

async function readTile(page, tile, textItems, tilePx) {
  const { buffer } = await renderRegion(page, tile.region, { targetPx: tilePx })
  const tileText = textItemsInTile(textItems, tile)
  const listing = formatTextItems(tileText, (bbox) => bboxToTile(bbox, tile))

  const parsed = await structuredCall({
    system: TILE_SYSTEM_PROMPT,
    schema: TILE_SCHEMA,
    schemaName: 'pid_tile_inventory',
    content: [
      textPart(
        `Tile ${tile.id} — row ${tile.row}, column ${tile.col} of the sheet.\n` +
          `It covers the page region x ${tile.region.x.toFixed(3)}–${(tile.region.x + tile.region.width).toFixed(3)}, ` +
          `y ${tile.region.y.toFixed(3)}–${(tile.region.y + tile.region.height).toFixed(3)} ` +
          `(fractions of the whole sheet, origin top-left).`,
      ),
      imagePart(toDataUrl(buffer)),
      textPart(
        `PDF text layer inside this tile — ids are what "textAnchorIds" must reference, ` +
          `positions are tile-relative fractions of this image:\n${listing}`,
      ),
    ],
  })

  return { entities: parsed.entities ?? [], tileNotes: parsed.tileNotes ?? '' }
}

async function readPageMeta(page, pageIndex, textItems, pagePx) {
  const { buffer } = await renderRegion(page, { x: 0, y: 0, width: 1, height: 1 }, { targetPx: pagePx })
  const listing = formatTextItems(textItems, (bbox) => bbox)

  return structuredCall({
    system: PAGE_META_SYSTEM_PROMPT,
    schema: PAGE_META_SCHEMA,
    schemaName: 'pid_page_metadata',
    content: [
      textPart(`Full P&ID sheet, page index ${pageIndex}.`),
      imagePart(toDataUrl(buffer)),
      textPart(`PDF text layer for the whole sheet (positions are page fractions, origin top-left):\n${listing}`),
    ],
  })
}

/**
 * One tile-level entity -> page coordinates, with its text anchors resolved.
 *
 * The model's own bbox is an estimate; the anchored text boxes are exact. We
 * keep both rather than collapsing them, because they answer different
 * questions — where the SYMBOL is drawn versus where its TAG is printed —
 * and step 2 needs the symbol while step 4 benefits from the precision.
 */
export function normalizeSighting(raw, tile, textById, index) {
  const tileBbox = raw.bbox
  if (!tileBbox || !Number.isFinite(tileBbox.x) || !Number.isFinite(tileBbox.y)) return null

  const symbolBbox = clampBbox(bboxFromTile(clampBbox(tileBbox), tile))

  const anchorIds = []
  const missingAnchors = []
  for (const id of raw.textAnchorIds ?? []) {
    if (textById.has(id)) anchorIds.push(id)
    else missingAnchors.push(id)
  }
  const tagBbox = anchorIds.length ? unionBboxes(anchorIds.map((id) => textById.get(id).bbox)) : null

  // A tag the model typed that appears nowhere in the text it anchored is a
  // transcription it invented or misread. Flagged, not dropped — on a
  // graphics-drawn tag there is no text layer entry to match against.
  let tagMismatch = false
  if (raw.tag && anchorIds.length) {
    const anchored = normalizeTag(anchorIds.map((id) => textById.get(id).str).join(''))
    const wanted = normalizeTag(raw.tag)
    tagMismatch = Boolean(wanted && anchored && !anchored.includes(wanted))
  }

  const route =
    raw.category === 'line' && Array.isArray(raw.polyline) && raw.polyline.length >= 2
      ? raw.polyline.map((p) => pointFromTile(p, tile))
      : null

  return {
    uid: `${tile.id}#${index}`,
    tileId: tile.id,
    localId: raw.localId ?? `${tile.id}-${index}`,
    category: raw.category,
    tag: raw.tag?.trim() || null,
    type: raw.type ?? '',
    description: raw.description ?? '',
    attributes: Array.isArray(raw.attributes) ? raw.attributes.filter((a) => a?.key && a?.value) : [],
    textAnchorIds: anchorIds,
    missingAnchors,
    tagMismatch,
    symbolBbox,
    tagBbox,
    route,
    connectsTo: Array.isArray(raw.connectsTo) ? raw.connectsTo.filter(Boolean) : [],
    confidence: Number.isFinite(raw.confidence) ? raw.confidence : 0.5,
  }
}

/**
 * Collapses the duplicate sightings the overlapping tiles produce.
 *
 * Three independent signals are unioned, strongest first:
 *   1. two sightings anchored to the SAME text item are the same thing — the
 *      text layer is ground truth, so this needs no distance check;
 *   2. same category and same tag, close together on the page;
 *   3. same category, both untagged, boxes substantially overlapping.
 * Lines are exempt from the distance check on rule 2: one line number names
 * one line, however far its run stretches across the sheet.
 *
 * `owner`, when given, is filled with sighting uid -> merged entity id.
 */
export function mergeSightings(sightings, pageIndex, textById, owner = null) {
  const dsu = new DisjointSet()
  for (const s of sightings) dsu.add(s.uid)

  // 1. Shared exact text anchors.
  const byAnchor = new Map()
  for (const s of sightings) {
    for (const id of s.textAnchorIds) {
      const key = `${s.category}|${id}`
      const bucket = byAnchor.get(key)
      if (bucket) bucket.push(s.uid)
      else byAnchor.set(key, [s.uid])
    }
  }
  for (const uids of byAnchor.values()) {
    for (let i = 1; i < uids.length; i++) dsu.union(uids[0], uids[i])
  }

  // 2. Same tag, spatially plausible, and either the same category or a
  //    recognized ambiguous pair. Control valves, actuated valves, and PSVs
  //    carry both a valve body and an instrument-like actuator/bubble on the
  //    same symbol, and different tiles were observed calling the identical
  //    tag "valve" in one sighting and "instrument" in another (TV-1257004,
  //    PSV-1257001, HV-1257022, ...) — bucketing strictly by category meant
  //    these two sightings of one physical device could never even be
  //    compared, let alone merged, so the device appeared twice.
  const AMBIGUOUS_CATEGORY_PAIRS = new Set(['instrument|valve'])
  const pairKey = (a, b) => [a, b].sort().join('|')

  const byTag = new Map()
  for (const s of sightings) {
    const norm = normalizeTag(s.tag)
    if (!norm) continue
    const bucket = byTag.get(norm)
    if (bucket) bucket.push(s)
    else byTag.set(norm, [s])
  }
  for (const group of byTag.values()) {
    for (let i = 0; i < group.length; i++) {
      for (let j = i + 1; j < group.length; j++) {
        const a = group[i]
        const b = group[j]
        if (a.category !== b.category && !AMBIGUOUS_CATEGORY_PAIRS.has(pairKey(a.category, b.category))) continue
        const isLine = a.category === 'line' && b.category === 'line'
        if (isLine || bboxGap(a.symbolBbox, b.symbolBbox) <= SAME_TAG_MAX_GAP) dsu.union(a.uid, b.uid)
      }
    }
  }

  // 3. Look-alikes in the overlap band. Only sightings from DIFFERENT tiles
  //    are considered — two symbols the same tile reported separately are
  //    two real symbols, not one seen twice, however much they overlap.
  //    Two tiles that both named the item, differently, are describing
  //    different items and are left alone; anything else is fair game, which
  //    is what lets a tagged sighting absorb the untagged sighting of the
  //    same equipment from the neighbouring tile.
  for (let i = 0; i < sightings.length; i++) {
    for (let j = i + 1; j < sightings.length; j++) {
      const a = sightings[i]
      const b = sightings[j]
      if (a.tileId === b.tileId || a.category !== b.category) continue
      const tagA = normalizeTag(a.tag)
      const tagB = normalizeTag(b.tag)
      if (tagA && tagB && tagA !== tagB) continue
      const overlaps =
        iou(a.symbolBbox, b.symbolBbox) >= CROSS_TILE_MERGE_IOU ||
        Math.max(containment(a.symbolBbox, b.symbolBbox), containment(b.symbolBbox, a.symbolBbox)) >= CROSS_TILE_MERGE_CONTAINMENT
      if (overlaps) dsu.union(a.uid, b.uid)
    }
  }

  const byUid = new Map(sightings.map((s) => [s.uid, s]))
  const counters = new Map()

  return dsu
    .groups()
    .map((uids) => uids.map((uid) => byUid.get(uid)).filter(Boolean))
    .filter((group) => group.length > 0)
    .map((group) => {
      const category = group[0].category
      const abbrev = CATEGORY_ABBREV[category] ?? 'x'
      const n = (counters.get(abbrev) ?? 0) + 1
      counters.set(abbrev, n)
      const id = `p${pageIndex}-${abbrev}-${n}`
      if (owner) for (const s of group) owner.set(s.uid, id)
      return combineGroup(group, id, pageIndex, textById)
    })
    .sort((a, b) => a.bbox.y - b.bbox.y || a.bbox.x - b.bbox.x)
}

function combineGroup(group, id, pageIndex, textById) {
  const ranked = [...group].sort((a, b) => b.confidence - a.confidence)
  const best = ranked[0]
  const category = best.category

  const textAnchorIds = [...new Set(group.flatMap((s) => s.textAnchorIds))]
  const tagBbox = textAnchorIds.length ? unionBboxes(textAnchorIds.map((tid) => textById.get(tid).bbox)) : null

  // A line is one entity whose run may cross several tiles, so its geometry
  // is every sighting's route kept as separate polylines — joining them into
  // one would invent connections between disjoint pieces of the run.
  const routes = category === 'line' ? group.map((s) => s.route).filter(Boolean) : []

  const symbolBbox =
    category === 'line' && routes.length
      ? unionBbox(bboxFromPoints(routes.flat()), unionBboxes(group.map((s) => s.symbolBbox)))
      : best.symbolBbox

  const bbox = clampBbox(unionBbox(symbolBbox, tagBbox) ?? symbolBbox)

  return {
    id,
    page: pageIndex,
    category,
    tag: ranked.find((s) => s.tag)?.tag ?? null,
    type: best.type,
    description: ranked.map((s) => s.description).sort((a, b) => b.length - a.length)[0] ?? '',
    attributes: mergeAttributes(ranked),
    // The entity's overall extent, and the two things it is made of.
    bbox,
    symbolBbox,
    tagBbox,
    routes,
    // True when at least one coordinate came from the PDF's own text layer
    // rather than the model's eye — i.e. this entity's position is exact.
    anchored: textAnchorIds.length > 0,
    textAnchorIds,
    connectsTo: [...new Set(group.flatMap((s) => s.connectsTo))],
    resolvedConnections: [],
    confidence: Math.max(...group.map((s) => s.confidence)),
    sightings: group.length,
    sources: [...new Set(group.map((s) => s.tileId))],
    flags: {
      tagMismatch: group.some((s) => s.tagMismatch),
      invalidAnchors: [...new Set(group.flatMap((s) => s.missingAnchors))],
      // Disagreement between tiles about what this is — worth a human look.
      conflictingTypes: [...new Set(group.map((s) => s.type).filter(Boolean))].length > 1,
      lowConfidence: Math.max(...group.map((s) => s.confidence)) < LOW_CONFIDENCE,
      // Set only on entities this module invented from unclaimed text layer
      // content, rather than ones the model actually reported seeing.
      synthesizedFromText: false,
    },
  }
}

/**
 * One text item belongs to one entity.
 *
 * Left unchecked, a confident sighting hoovers up its neighbour's tag too —
 * observed exactly this way: the TT-204 bubble anchored "TIC", "204" AND its
 * own "204", which erased TIC-204 from the inventory entirely, and the
 * P-101A pump claimed the text "P-101B". Contested anchors are therefore
 * resolved to the nearest entity and stripped from the rest.
 *
 * Scoped WITHIN a tile: the same text legitimately anchors one sighting per
 * tile, and it is precisely those cross-tile repeats that merge rule 1 uses
 * to recognise a duplicate.
 */
export function enforceAnchorExclusivity(sightings, textById) {
  const contested = new Map()
  for (const s of sightings) {
    for (const id of s.textAnchorIds) {
      const key = `${s.tileId}|${id}`
      const bucket = contested.get(key)
      if (bucket) bucket.push(s)
      else contested.set(key, [s])
    }
  }

  const stripped = []
  for (const [key, claimants] of contested) {
    if (claimants.length < 2) continue
    const textId = key.slice(key.indexOf('|') + 1)
    const center = bboxCenter(textById.get(textId).bbox)
    let winner = claimants[0]
    let bestScore = Infinity
    for (const s of claimants) {
      // Distance from the text to the symbol, zero when the text sits inside
      // it; confidence breaks a tie between two equally placed claimants.
      const d = pointInBbox(center, s.symbolBbox) ? 0 : distance(center, bboxCenter(s.symbolBbox))
      const score = d - s.confidence * 1e-6
      if (score < bestScore) {
        bestScore = score
        winner = s
      }
    }
    for (const s of claimants) {
      if (s === winner) continue
      s.textAnchorIds = s.textAnchorIds.filter((id) => id !== textId)
      stripped.push({ tile: s.tileId, text: textById.get(textId).str, from: s.tag ?? s.type, to: winner.tag ?? winner.type })
    }
  }
  return stripped
}

/**
 * Reassembles the split tags inside instrument bubbles.
 *
 * An ISA bubble prints its function letters above its loop number, and a PDF
 * stores those as two unrelated text runs — "FT" and "202". Neither looks
 * like a tag alone, so every downstream check that asks "is this text a tag?"
 * answers no, and a whole instrument can disappear without anything noticing.
 *
 * This pairs a letters-only run with the digits run directly beneath it,
 * yielding the composite "FT-202" with the union of the two boxes. Bubbles
 * are the most numerous thing on a P&ID, so this pass does a lot of work.
 */
export function findCompositeTags(textItems) {
  const letters = textItems.filter((t) => /^[A-Z]{1,5}$/.test(t.str.trim()))
  const digits = textItems.filter((t) => /^\d{1,5}[A-Z]?$/.test(t.str.trim()))
  const composites = []
  const consumed = new Set()

  for (const top of letters) {
    const topCenter = top.bbox.x + top.bbox.width / 2
    let best = null
    let bestGap = Infinity
    for (const bottom of digits) {
      if (consumed.has(bottom.id)) continue
      // Directly beneath, within about one line of space...
      const gap = bottom.bbox.y - (top.bbox.y + top.bbox.height)
      if (gap < -top.bbox.height * 0.5 || gap > top.bbox.height * 1.5) continue
      // ...roughly centred on the same axis, and the same size of text.
      const bottomCenter = bottom.bbox.x + bottom.bbox.width / 2
      if (Math.abs(bottomCenter - topCenter) > Math.max(top.bbox.width, bottom.bbox.width)) continue
      if (Math.abs(bottom.fontSize - top.fontSize) > top.fontSize * 0.35) continue
      if (gap < bestGap) {
        bestGap = gap
        best = bottom
      }
    }
    if (!best) continue
    consumed.add(best.id)
    composites.push({
      str: `${top.str.trim()}-${best.str.trim()}`,
      parts: [top.id, best.id],
      bbox: unionBbox(top.bbox, best.bbox),
    })
  }
  return composites
}

/**
 * Takes back a bubble tag an entity absorbed that is not its own.
 *
 * Seen in practice: the FIC-202 bubble anchored "FT", "202", "FIC" and "202"
 * — all four fragments of two adjacent bubbles — which deleted FT-202 from
 * the inventory. Exclusivity cannot catch this, because only one entity ever
 * claimed them, so there was no competing claim to resolve. Comparing the
 * composite each anchor set spells against the entity's own tag can.
 */
export function releaseForeignCompositeTags(entities, composites, textById) {
  const released = []
  for (const entity of entities) {
    if (!entity.tag) continue
    const own = normalizeTag(entity.tag)
    const anchors = new Set(entity.textAnchorIds)
    for (const composite of composites) {
      if (normalizeTag(composite.str) === own) continue
      if (!composite.parts.every((id) => anchors.has(id))) continue
      entity.textAnchorIds = entity.textAnchorIds.filter((id) => !composite.parts.includes(id))
      released.push({ from: entity.id, tag: composite.str })
    }
    if (released.some((r) => r.from === entity.id)) recomputeAnchoredGeometry(entity, textById)
  }
  return released
}

/** Re-derives the parts of an entity that came from its text anchors, after
 *  that anchor set has been edited. */
function recomputeAnchoredGeometry(entity, textById) {
  if (!textById) return
  const boxes = entity.textAnchorIds.map((id) => textById.get(id)?.bbox).filter(Boolean)
  entity.tagBbox = boxes.length ? unionBboxes(boxes) : null
  entity.anchored = boxes.length > 0
  entity.bbox = clampBbox(unionBbox(entity.symbolBbox, entity.tagBbox) ?? entity.symbolBbox)
}

/**
 * Gives an untagged entity the tag printed on it.
 *
 * After the passes above, a tag nobody owns is free. If it sits on or beside
 * an untagged entity, it is that entity's tag — this is what recovers, for
 * instance, a pump reported as an anonymous "pump" because its neighbour had
 * taken the text, and an instrument whose split bubble tag nothing matched.
 *
 * Composite bubble tags are offered first: "FT-202" is a better answer than
 * the bare fragment "FT" that would otherwise be considered on its own.
 */
export function attachUnclaimedTags(entities, textItems, composites, textById) {
  const claimed = new Set(entities.flatMap((e) => e.textAnchorIds))
  const candidates = tagCandidates(textItems, composites, claimed)
  const attached = []

  for (const candidate of candidates) {
    if (candidate.parts.some((id) => claimed.has(id))) continue
    const center = bboxCenter(candidate.bbox)
    let best = null
    let bestDistance = Infinity
    for (const entity of entities) {
      if (entity.tag || entity.category === 'note' || entity.category === 'label') continue
      const d = containment(candidate.bbox, entity.symbolBbox) >= 0.5 ? 0 : distance(center, bboxCenter(entity.symbolBbox))
      // Beyond roughly a symbol's width away it is somebody else's tag.
      if (d > 0.04) continue
      if (d < bestDistance) {
        bestDistance = d
        best = entity
      }
    }
    if (!best) continue
    best.tag = candidate.str
    best.textAnchorIds.push(...candidate.parts)
    recomputeAnchoredGeometry(best, textById)
    for (const id of candidate.parts) claimed.add(id)
    attached.push({ entity: best.id, tag: candidate.str })
  }
  return attached
}

/** Unclaimed tag-shaped text, composites first so a split bubble tag is
 *  preferred over either of the fragments it is made of. */
function tagCandidates(textItems, composites, claimed) {
  const out = []
  const inComposite = new Set()
  for (const composite of composites) {
    if (composite.parts.some((id) => claimed.has(id))) continue
    out.push(composite)
    for (const id of composite.parts) inComposite.add(id)
  }
  for (const text of textItems) {
    if (claimed.has(text.id) || inComposite.has(text.id)) continue
    if (!looksLikeTag(text.str)) continue
    out.push({ str: text.str, parts: [text.id], bbox: text.bbox })
  }
  return out
}

/**
 * Nothing in the text layer is allowed to vanish.
 *
 * Any tag-shaped text still unclaimed after the passes above is something
 * the model did not report — a real miss. Since the PDF gives us its exact
 * position, it is recorded as a low-confidence entity flagged
 * `synthesizedFromText` rather than dropped: step 1's contract is coverage,
 * and a reviewer would far rather see a stub they can correct than discover
 * later that the sheet had an instrument nobody listed.
 *
 * Text the whole-sheet pass already transcribed (title block, notes, legend)
 * is excluded — that content is catalogued in `drawingMeta`, not here.
 */
export function synthesizeUnclaimedTags(entities, textItems, composites, drawingMeta, pageIndex) {
  const claimed = new Set(entities.flatMap((e) => e.textAnchorIds))
  const metaText = new Set(
    [
      ...(drawingMeta?.titleBlock ?? []).map((kv) => kv.value),
      ...(drawingMeta?.notes ?? []).map((n) => n.text),
      ...(drawingMeta?.legend ?? []).flatMap((kv) => [kv.key, kv.value]),
      ...(drawingMeta?.revisions ?? []).map((kv) => kv.value),
      ...(drawingMeta?.referencedDrawings ?? []),
    ].map((v) => String(v).toUpperCase().trim()),
  )

  const out = []
  let n = 0
  for (const text of tagCandidates(textItems, composites, claimed)) {
    const upper = text.str.toUpperCase().trim()
    if (metaText.has(upper) || [...metaText].some((m) => m.includes(upper))) continue
    n++
    out.push({
      id: `p${pageIndex}-syn-${n}`,
      page: pageIndex,
      category: 'label',
      tag: text.str,
      type: 'unidentified tagged item',
      description: `Tag "${text.str}" is printed on the sheet at this position but no symbol was reported for it. Recorded from the PDF text layer so it is not lost; needs review.`,
      attributes: [],
      bbox: text.bbox,
      symbolBbox: text.bbox,
      tagBbox: text.bbox,
      routes: [],
      anchored: true,
      textAnchorIds: [...text.parts],
      connectsTo: [],
      resolvedConnections: [],
      confidence: 0.3,
      sightings: 0,
      sources: [],
      flags: { tagMismatch: false, invalidAnchors: [], conflictingTypes: false, lowConfidence: true, synthesizedFromText: true },
    })
  }
  return out
}

/** Later (lower-confidence) sightings fill gaps but never overwrite a value
 *  a more confident sighting already gave for the same key. */
function mergeAttributes(ranked) {
  const out = new Map()
  for (const sighting of ranked) {
    for (const { key, value } of sighting.attributes) {
      if (!out.has(key)) out.set(key, value)
    }
  }
  return [...out.entries()].map(([key, value]) => ({ key, value }))
}

/** Rewrites each sighting's tile-local `connectsTo` ids to the uid of the
 *  sighting they name in that same tile. A local id whose sighting was
 *  dropped is removed: outside its tile it names nothing. Tags pass through. */
export function qualifyLocalRefs(sightings, tileResults) {
  const uidByLocal = new Map()
  for (const s of sightings) {
    const key = `${s.tileId}|${s.localId}`
    if (!uidByLocal.has(key)) uidByLocal.set(key, s.uid)
  }
  const knownLocal = new Map(
    tileResults.map((r) => [r.tile.id, new Set(r.entities.filter((e) => e.localId != null).map((e) => String(e.localId)))]),
  )
  for (const s of sightings) {
    const refs = []
    for (const ref of s.connectsTo) {
      const uid = uidByLocal.get(`${s.tileId}|${ref}`)
      if (uid) {
        if (uid !== s.uid) refs.push(uid)
      } else if (!knownLocal.get(s.tileId)?.has(ref)) {
        refs.push(ref)
      }
    }
    s.connectsTo = refs
  }
}

/** Maps the sighting uids `qualifyLocalRefs` left in `connectsTo` to the
 *  entity each sighting merged into, so every reference is a tag or an entity id. */
export function localizeConnections(entities, owner) {
  for (const e of entities) {
    e.connectsTo = [...new Set(e.connectsTo.map((c) => owner.get(c) ?? c))].filter((c) => c !== e.id)
  }
}

/** Turns `connectsTo` (tags, or entity ids from resolved tile-local ids)
 *  into real entity ids where they can be matched. Unresolvable references
 *  are left in `connectsTo` for step 2 to reason about rather than discarded. */
function resolveConnections(entities) {
  const ids = new Set(entities.map((e) => e.id))
  const byTag = new Map()
  for (const e of entities) {
    const norm = normalizeTag(e.tag)
    if (norm && !byTag.has(norm)) byTag.set(norm, e.id)
  }

  for (const entity of entities) {
    const resolved = new Set()
    for (const ref of entity.connectsTo) {
      const target = ids.has(ref) ? ref : byTag.get(normalizeTag(ref))
      if (target && target !== entity.id) resolved.add(target)
    }
    entity.resolvedConnections = [...resolved]
  }
}
