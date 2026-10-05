import {
  normalizeSighting, mergeSightings, findCompositeTags,
  releaseForeignCompositeTags, attachUnclaimedTags, synthesizeUnclaimedTags,
} from '../src/pipeline/step1/extract.js'

const tile = (id, x, y, w = 0.4, h = 0.6) => ({ id, page: 0, row: 0, col: 0, region: { x, y, width: w, height: h } })
const tileA = tile('tile-A', 0.0, 0.0)
const tileB = tile('tile-B', 0.3, 0.0)   // overlaps tileA
const tileC = tile('tile-C', 0.6, 0.0)   // far from tileA

const textById = new Map([
  ['p0-t1', { id: 'p0-t1', str: 'P-101A', bbox: { x: 0.31, y: 0.20, width: 0.03, height: 0.01 } }],
  ['p0-t2', { id: 'p0-t2', str: 'E-201',  bbox: { x: 0.70, y: 0.20, width: 0.03, height: 0.01 } }],
])

// helper to build a raw tile entity
const raw = (o) => ({
  localId: 'x', category: 'equipment', tag: null, type: 't', description: 'd',
  attributes: [], textAnchorIds: [], bbox: { x: 0.5, y: 0.3, width: 0.1, height: 0.1 },
  polyline: null, connectsTo: [], confidence: 0.8, ...o,
})

let pass = 0, fail = 0
const check = (name, cond, detail = '') => {
  if (cond) { pass++; console.log(`  PASS  ${name}`) }
  else { fail++; console.log(`  FAIL  ${name} ${detail}`) }
}

// --- 1. same tag, overlapping tiles, close together -> ONE entity
{
  const s = [
    normalizeSighting(raw({ tag: 'P-101A', bbox: { x: 0.80, y: 0.33, width: 0.08, height: 0.10 } }), tileA, textById, 0),
    normalizeSighting(raw({ tag: 'P-101 A', bbox: { x: 0.07, y: 0.33, width: 0.08, height: 0.10 } }), tileB, textById, 0),
  ]
  const m = mergeSightings(s, 0, textById)
  check('duplicate tagged sighting across overlapping tiles merges', m.length === 1, `got ${m.length}`)
  check('  merged entity keeps the tag', m[0]?.tag === 'P-101A')
  check('  records both sightings', m[0]?.sightings === 2)
}

// --- 2. same tag but far apart -> TWO entities (cross-reference, not a duplicate)
{
  const s = [
    normalizeSighting(raw({ tag: 'P-101A', bbox: { x: 0.1, y: 0.3, width: 0.05, height: 0.05 } }), tileA, textById, 0),
    normalizeSighting(raw({ tag: 'P-101A', bbox: { x: 0.8, y: 0.8, width: 0.05, height: 0.05 } }), tileC, textById, 0),
  ]
  const m = mergeSightings(s, 0, textById)
  check('same tag far apart stays separate', m.length === 2, `got ${m.length}`)
}

// --- 3. shared exact text anchor -> merge regardless of bbox disagreement
{
  const s = [
    normalizeSighting(raw({ tag: 'P-101A', textAnchorIds: ['p0-t1'], bbox: { x: 0.1, y: 0.1, width: 0.05, height: 0.05 } }), tileA, textById, 0),
    normalizeSighting(raw({ tag: null,     textAnchorIds: ['p0-t1'], bbox: { x: 0.9, y: 0.9, width: 0.05, height: 0.05 } }), tileC, textById, 0),
  ]
  const m = mergeSightings(s, 0, textById)
  check('shared text anchor merges even with distant boxes', m.length === 1, `got ${m.length}`)
  check('  merged entity is marked anchored', m[0]?.anchored === true)
  check('  tagBbox comes from the text layer', Math.abs((m[0]?.tagBbox?.x ?? 0) - 0.31) < 1e-9)
}

// --- 4. two untagged symbols in the SAME tile are two things, never merged
{
  const s = [
    normalizeSighting(raw({ bbox: { x: 0.50, y: 0.30, width: 0.10, height: 0.10 } }), tileA, textById, 0),
    normalizeSighting(raw({ bbox: { x: 0.51, y: 0.31, width: 0.10, height: 0.10 } }), tileA, textById, 1),
  ]
  const m = mergeSightings(s, 0, textById)
  check('overlapping untagged symbols in one tile stay separate', m.length === 2, `got ${m.length}`)
}

// --- 5. untagged look-alikes across different tiles DO merge
{
  const s = [
    normalizeSighting(raw({ bbox: { x: 0.80, y: 0.30, width: 0.10, height: 0.10 } }), tileA, textById, 0),
    normalizeSighting(raw({ bbox: { x: 0.0666, y: 0.30, width: 0.10, height: 0.10 } }), tileB, textById, 0),
  ]
  const m = mergeSightings(s, 0, textById)
  check('untagged look-alikes across tiles merge', m.length === 1, `got ${m.length}`)
}

// --- 6. a line with one number merges across the whole sheet
{
  const mk = (t, x) => normalizeSighting(raw({
    category: 'line', tag: '8"-CR-2203-A1', bbox: { x, y: 0.5, width: 0.1, height: 0.02 },
    polyline: [{ x, y: 0.5 }, { x: x + 0.1, y: 0.5 }],
  }), t, textById, 0)
  const m = mergeSightings([mk(tileA, 0.1), mk(tileC, 0.8)], 0, textById)
  check('one line number = one line entity however far it runs', m.length === 1, `got ${m.length}`)
  check('  keeps both traced routes separately', m[0]?.routes.length === 2, `routes=${m[0]?.routes.length}`)
}

// --- 7. different categories never merge, even on an identical tag
{
  const s = [
    normalizeSighting(raw({ category: 'equipment', tag: 'P-101A', bbox: { x: 0.5, y: 0.3, width: 0.05, height: 0.05 } }), tileA, textById, 0),
    normalizeSighting(raw({ category: 'label',     tag: 'P-101A', bbox: { x: 0.5, y: 0.3, width: 0.05, height: 0.05 } }), tileA, textById, 1),
  ]
  check('same tag in different categories stays separate', mergeSightings(s, 0, textById).length === 2)
}

// --- 8. a tag the model invented is flagged, not silently trusted
{
  const s = [normalizeSighting(raw({ tag: 'X-999', textAnchorIds: ['p0-t1'] }), tileA, textById, 0)]
  const m = mergeSightings(s, 0, textById)
  check('tag not present in its anchored text is flagged', m[0]?.flags.tagMismatch === true)
}

// --- 9. a bogus anchor id is recorded rather than crashing
{
  const s = [normalizeSighting(raw({ tag: 'P-101A', textAnchorIds: ['p0-t1', 'nope-99'] }), tileA, textById, 0)]
  const m = mergeSightings(s, 0, textById)
  check('invalid anchor ids are recorded', m[0]?.flags.invalidAnchors.includes('nope-99'))
  check('  valid anchor still used', m[0]?.anchored === true)
}


// ---------------------------------------------------------------- bubbles
// An ISA bubble stores its function letters and loop number as two separate
// text runs, so neither half looks like a tag on its own.
const bubbleText = [
  { id: 'b1', str: 'FT',  bbox: { x: 0.440, y: 0.430, width: 0.012, height: 0.008 }, fontSize: 7 },
  { id: 'b2', str: '202', bbox: { x: 0.441, y: 0.441, width: 0.014, height: 0.008 }, fontSize: 7 },
  { id: 'b3', str: 'FIC', bbox: { x: 0.512, y: 0.430, width: 0.014, height: 0.008 }, fontSize: 7 },
  { id: 'b4', str: '202', bbox: { x: 0.513, y: 0.441, width: 0.014, height: 0.008 }, fontSize: 7 },
  { id: 'b5', str: 'NOTES:', bbox: { x: 0.05, y: 0.69, width: 0.04, height: 0.009 }, fontSize: 9 },
  { id: 'b6', str: 'PID-2201-001', bbox: { x: 0.80, y: 0.90, width: 0.06, height: 0.008 }, fontSize: 8 },
]
const bubbleById = new Map(bubbleText.map((t) => [t.id, t]))

{
  const c = findCompositeTags(bubbleText)
  const strs = c.map((x) => x.str).sort()
  check('split bubble tags are reassembled', strs.join(',') === 'FIC-202,FT-202', `got ${strs.join(',')}`)
  check('  composite box spans both fragments', c.find((x) => x.str === 'FT-202').bbox.height > 0.015)
  const far = findCompositeTags([bubbleText[0], { ...bubbleText[1], id: 'z', bbox: { ...bubbleText[1].bbox, y: 0.9 } }])
  check('  digits far below the letters are not paired', far.length === 0)
}

// The real defect this fixes: one bubble absorbing its neighbour's text,
// which silently deleted FT-202 from the inventory.
{
  const entity = {
    id: 'p0-in-3', tag: 'FIC-202', category: 'instrument',
    textAnchorIds: ['b1', 'b2', 'b3', 'b4'], symbolBbox: { x: 0.505, y: 0.425, width: 0.03, height: 0.03 },
    bbox: { x: 0.505, y: 0.425, width: 0.03, height: 0.03 }, tagBbox: null, anchored: true,
  }
  const released = releaseForeignCompositeTags([entity], findCompositeTags(bubbleText), bubbleById)
  check('a bubble tag that is not the entity\'s own is handed back', released.length === 1 && released[0].tag === 'FT-202')
  check('  the entity keeps its own bubble tag', entity.textAnchorIds.sort().join(',') === 'b3,b4')
}

// Freed tags should land on a real neighbouring symbol when there is one.
{
  const entities = [{
    id: 'p0-in-9', tag: null, category: 'instrument', textAnchorIds: [],
    symbolBbox: { x: 0.438, y: 0.426, width: 0.024, height: 0.026 },
    bbox: { x: 0.438, y: 0.426, width: 0.024, height: 0.026 }, tagBbox: null, anchored: false,
  }]
  const attached = attachUnclaimedTags(entities, bubbleText, findCompositeTags(bubbleText), bubbleById)
  check('a freed bubble tag attaches to the untagged symbol under it', entities[0].tag === 'FT-202', `got ${entities[0].tag}`)
  check('  and the entity becomes exactly anchored', entities[0].anchored === true && attached.length >= 1)
}

// Nothing in the text layer may vanish: with no owner, a stub is emitted.
{
  const meta = { titleBlock: [{ key: 'drawingNumber', value: 'PID-2201-001' }], notes: [], legend: [], revisions: [], referencedDrawings: [] }
  const syn = synthesizeUnclaimedTags([], bubbleText, findCompositeTags(bubbleText), meta, 0)
  const tags = syn.map((e) => e.tag).sort()
  check('orphan bubble tags are recorded rather than lost', tags.includes('FT-202') && tags.includes('FIC-202'), `got ${tags.join(',')}`)
  check('  stubs are flagged as synthesized', syn.every((e) => e.flags.synthesizedFromText && e.flags.lowConfidence))
  check('  stubs carry exact text-layer coordinates', syn.every((e) => e.anchored && e.tagBbox))
  check('  title-block text already in drawingMeta is not duplicated', !tags.includes('PID-2201-001'), `got ${tags.join(',')}`)
  check('  prose is never mistaken for a tag', !tags.includes('NOTES:'))
}


// -------------------------------------------------- ambiguous category pairs
// A control valve / PSV's body and its own actuator-instrument read are
// often typed inconsistently across tiles — same tag, same symbol, but one
// tile calls it "valve" and another calls it "instrument". Observed on a
// real sheet for TV-1257004, PSV-1257001, HV-1257022 and eight others.
{
  // Tile-relative coordinates chosen so both sightings land near page (0.32,
  // 0.40) once transformed through their own tile's region — mirroring how
  // the real PSV-1257002 valve/instrument pair sat close together on the
  // actual sheet.
  const s = [
    normalizeSighting(raw({ category: 'valve', tag: 'PSV-1257001', bbox: { x: 0.80, y: 0.667, width: 0.08, height: 0.05 } }), tileA, textById, 0),
    normalizeSighting(raw({ category: 'instrument', tag: 'PSV-1257001', bbox: { x: 0.05, y: 0.667, width: 0.02, height: 0.03 } }), tileB, textById, 0),
  ]
  const m = mergeSightings(s, 0, textById)
  check('a valve/instrument sighting pair of the SAME tag, close together, merges', m.length === 1, `got ${m.length}`)
  check('  the merged entity keeps its tag', m[0]?.tag === 'PSV-1257001')
}

// The proximity requirement still applies: a genuine valve and a genuine,
// unrelated instrument sharing a tag by coincidence, far apart, stay separate.
{
  const s = [
    normalizeSighting(raw({ category: 'valve', tag: 'PSV-1257001', bbox: { x: 0.1, y: 0.1, width: 0.03, height: 0.03 } }), tileA, textById, 0),
    normalizeSighting(raw({ category: 'instrument', tag: 'PSV-1257001', bbox: { x: 0.9, y: 0.9, width: 0.03, height: 0.03 } }), tileC, textById, 0),
  ]
  const m = mergeSightings(s, 0, textById)
  check('a same-tag valve/instrument pair far apart still stays separate', m.length === 2, `got ${m.length}`)
}

// The ambiguous-pair allowance is narrow: an unrelated category combination
// (equipment vs instrument) sharing a tag must NOT merge just because they
// are close together — only the specific pair(s) known to be one symbol
// read two ways are relaxed.
{
  // Same close-together placement as the valve/instrument case above — this
  // confirms it's the CATEGORY pair that blocks the merge, not distance.
  const s = [
    normalizeSighting(raw({ category: 'equipment', tag: 'X-01', bbox: { x: 0.80, y: 0.667, width: 0.08, height: 0.05 } }), tileA, textById, 0),
    normalizeSighting(raw({ category: 'instrument', tag: 'X-01', bbox: { x: 0.05, y: 0.667, width: 0.02, height: 0.03 } }), tileB, textById, 0),
  ]
  const m = mergeSightings(s, 0, textById)
  check('an unrelated category pair does NOT merge even when close', m.length === 2, `got ${m.length}`)
}

console.log(`\n${pass} passed, ${fail} failed`)
process.exitCode = fail ? 1 : 0
