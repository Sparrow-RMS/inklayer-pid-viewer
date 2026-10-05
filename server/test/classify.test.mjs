import { assemblePage, isFunctional } from '../src/pipeline/step2/classify.js'

let pass = 0, fail = 0
const check = (name, cond, detail = '') => {
  if (cond) { pass++; console.log(`  PASS  ${name}`) }
  else { fail++; console.log(`  FAIL  ${name} ${detail}`) }
}

const entity = (id, category, bbox, extra = {}) => ({
  id, category, tag: id, type: 't', attributes: [], bbox, routes: null, connectsTo: [], ...extra,
})

const step1Page = {
  pageIndex: 0,
  entities: [
    entity('e-tank', 'equipment', { x: 0.10, y: 0.10, width: 0.10, height: 0.20 }),
    entity('e-pump', 'equipment', { x: 0.30, y: 0.15, width: 0.05, height: 0.05 }),
    entity('e-valve', 'valve', { x: 0.25, y: 0.16, width: 0.02, height: 0.02 }),
    entity('e-line', 'line', { x: 0.20, y: 0.16, width: 0.10, height: 0.01 }, {
      routes: [[{ x: 0.20, y: 0.165 }, { x: 0.30, y: 0.165 }]],
    }),
    entity('e-note', 'note', { x: 0.50, y: 0.50, width: 0.05, height: 0.02 }),
    entity('e-label', 'label', { x: 0.55, y: 0.55, width: 0.05, height: 0.02 }),
    entity('e-orphan-instr', 'instrument', { x: 0.80, y: 0.80, width: 0.02, height: 0.02 }),
  ],
}
const classifiable = step1Page.entities.filter((e) => !['note', 'label', 'equipment_list_row'].includes(e.category))

// --- 1. a well-formed process: geometry derived correctly, no coordinates trusted from the model
{
  const raw = { processes: [{
    localId: 'proc1', name: 'Tank to pump', category: 'transfer', description: 'd',
    entityIds: ['e-tank', 'e-valve', 'e-line', 'e-pump'],
    primaryEquipmentIds: ['e-tank', 'e-pump'], inletEntityIds: ['e-tank'], outletEntityIds: ['e-pump'],
    flowDirection: 'e-tank -> e-valve -> e-line -> e-pump', boundaryReasoning: [], confidence: 0.9,
  }], pageNotes: '' }
  const page = assemblePage({ pageIndex: 0, step1Page, classifiable, raw })
  check('one valid process is kept', page.processes.length === 1, `got ${page.processes.length}`)
  const p = page.processes[0]
  check('bbox is the union of its real member entities, not invented', 
    Math.abs(p.bbox.x - 0.10) < 1e-9 && Math.abs(p.bbox.width - 0.25) < 1e-9,
    `got ${JSON.stringify(p.bbox)}`)
  check('the line\'s real traced route is carried through', p.routes.length === 1 && p.routes[0].entityId === 'e-line')
  check('primaryEquipmentIds passed through validated', p.primaryEquipmentIds.sort().join(',') === 'e-pump,e-tank')
}

// --- 2. a hallucinated entity id is dropped and recorded, not trusted
{
  const raw = { processes: [{
    localId: 'proc1', name: 'x', category: 'transfer', description: 'd',
    entityIds: ['e-tank', 'e-DOES-NOT-EXIST'], primaryEquipmentIds: [], inletEntityIds: [], outletEntityIds: [],
    flowDirection: '', boundaryReasoning: [], confidence: 0.8,
  }], pageNotes: '' }
  const page = assemblePage({ pageIndex: 0, step1Page, classifiable, raw })
  check('the real entity is still kept', page.processes[0].entityIds.includes('e-tank'))
  check('the hallucinated id is excluded from entityIds', !page.processes[0].entityIds.includes('e-DOES-NOT-EXIST'))
  check('the hallucinated id is recorded on flags, not silently dropped', page.processes[0].flags.hallucinatedIds.includes('e-DOES-NOT-EXIST'))
}

// --- 3. a process left with zero real entities is dropped entirely, not kept as an empty phantom
{
  const raw = { processes: [{
    localId: 'proc1', name: 'x', category: 'transfer', description: 'd',
    entityIds: ['e-FAKE-1', 'e-FAKE-2'], primaryEquipmentIds: [], inletEntityIds: [], outletEntityIds: [],
    flowDirection: '', boundaryReasoning: [], confidence: 0.8,
  }], pageNotes: '' }
  const page = assemblePage({ pageIndex: 0, step1Page, classifiable, raw })
  check('a process anchored entirely on fake ids is dropped', page.processes.length === 0, `got ${page.processes.length}`)
}

// --- 4. coverage diagnostics: notes/labels never count as gaps; a real uncovered entity does
{
  const raw = { processes: [{
    localId: 'proc1', name: 'x', category: 'transfer', description: 'd',
    entityIds: ['e-tank', 'e-pump'], primaryEquipmentIds: [], inletEntityIds: [], outletEntityIds: [],
    flowDirection: '', boundaryReasoning: [], confidence: 0.8,
  }], pageNotes: '' }
  const page = assemblePage({ pageIndex: 0, step1Page, classifiable, raw })
  check('note/label are excluded from the coverage universe entirely',
    !page.diagnostics.uncoveredEntityIds.includes('e-note') && !page.diagnostics.uncoveredEntityIds.includes('e-label'))
  check('a real entity nothing claimed shows up as an uncovered gap',
    page.diagnostics.uncoveredEntityIds.includes('e-valve') &&
    page.diagnostics.uncoveredEntityIds.includes('e-line') &&
    page.diagnostics.uncoveredEntityIds.includes('e-orphan-instr'))
  check('covered entities are recorded too', page.diagnostics.coveredEntityIds.sort().join(',') === 'e-pump,e-tank')
}

// --- 5. two processes describing almost the same thing are flagged, not silently duplicated
{
  const raw = { processes: [
    { localId: 'proc1', name: 'A', category: 'transfer', description: 'd',
      entityIds: ['e-tank', 'e-valve', 'e-line'], primaryEquipmentIds: [], inletEntityIds: [], outletEntityIds: [],
      flowDirection: '', boundaryReasoning: [], confidence: 0.9 },
    { localId: 'proc2', name: 'B', category: 'transfer', description: 'd',
      entityIds: ['e-tank', 'e-valve', 'e-line', 'e-pump'], primaryEquipmentIds: [], inletEntityIds: [], outletEntityIds: [],
      flowDirection: '', boundaryReasoning: [], confidence: 0.9 },
  ], pageNotes: '' }
  const page = assemblePage({ pageIndex: 0, step1Page, classifiable, raw })
  check('heavily overlapping processes are flagged on both sides',
    page.processes[0].flags.highOverlapWith?.includes(page.processes[1].id) &&
    page.processes[1].flags.highOverlapWith?.includes(page.processes[0].id))
}

// --- 6. distinct, lightly-overlapping processes (shared boundary equipment only) are NOT flagged
{
  const raw = { processes: [
    { localId: 'proc1', name: 'A', category: 'agitation', description: 'd',
      entityIds: ['e-tank'], primaryEquipmentIds: [], inletEntityIds: [], outletEntityIds: [],
      flowDirection: '', boundaryReasoning: [], confidence: 0.9 },
    { localId: 'proc2', name: 'B', category: 'transfer', description: 'd',
      entityIds: ['e-tank', 'e-valve', 'e-line', 'e-pump'], primaryEquipmentIds: [], inletEntityIds: [], outletEntityIds: [],
      flowDirection: '', boundaryReasoning: [], confidence: 0.9 },
  ], pageNotes: '' }
  const page = assemblePage({ pageIndex: 0, step1Page, classifiable, raw })
  check('a small shared-boundary overlap is not flagged as a duplicate',
    !page.processes[0].flags.highOverlapWith && !page.processes[1].flags.highOverlapWith)
}


// --- 7. a synthesized stub (real coordinates, unidentified) must still be
// classifiable, even though step 1 stores it as category "label" for lack of
// anywhere else to put it — this is the exact FT-202 case seen on a real run.
{
  const genuineLabel = entity('e-real-label', 'label', { x: 0, y: 0, width: 0.01, height: 0.01 })
  const synthesizedStub = entity('e-synth', 'label', { x: 0, y: 0, width: 0.01, height: 0.01 },
    { flags: { synthesizedFromText: true } })
  check('a genuine label/annotation is excluded from classification', !isFunctional(genuineLabel))
  check('a synthesized stub is NOT excluded, despite sharing the "label" category', isFunctional(synthesizedStub))
}


// --- 8. duplicate ids within one process's own list are collapsed, not
// double-counted — observed on a real sheet ("TV-1257004, TV-1257004").
{
  const raw = { processes: [{
    localId: 'proc1', name: 'x', category: 'transfer', description: 'd',
    entityIds: ['e-tank', 'e-tank', 'e-pump'], primaryEquipmentIds: [], inletEntityIds: [], outletEntityIds: [],
    flowDirection: '', boundaryReasoning: [], confidence: 0.9,
  }], pageNotes: '' }
  const page = assemblePage({ pageIndex: 0, step1Page, classifiable, raw })
  check('a repeated id collapses to one', page.processes[0].entityIds.length === 2, `got ${page.processes[0].entityIds.length}`)
  check('  category counts are not inflated by the duplicate', page.processes[0].entityCategoryCounts.equipment === 2)
}

console.log(`\n${pass} passed, ${fail} failed`)
process.exitCode = fail ? 1 : 0
