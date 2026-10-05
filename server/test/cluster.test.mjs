import { generateCandidatePairs } from '../src/pipeline/step3/candidates.js'
import { assemblePage } from '../src/pipeline/step3/cluster.js'

let pass = 0, fail = 0
const check = (name, cond, detail = '') => {
  if (cond) { pass++; console.log(`  PASS  ${name}`) }
  else { fail++; console.log(`  FAIL  ${name} ${detail}`) }
}

const process = (id, overrides = {}) => ({
  id, page: 0, name: id, category: 'transfer', description: 'd',
  entityIds: [], primaryEquipmentIds: [], inletEntityIds: [], outletEntityIds: [],
  bbox: { x: 0.5, y: 0.5, width: 0.05, height: 0.05 }, routes: [],
  flags: {}, ...overrides,
})
const entity = (id, overrides = {}) => ({ id, resolvedConnections: [], ...overrides })

// ============================================================ candidates.js

{
  const a = process('a', { entityIds: ['e1', 'e2'] })
  const b = process('b', { entityIds: ['e2', 'e3'] })
  const pairs = generateCandidatePairs([a, b], new Map())
  check('sharing an entity produces a candidate', pairs.length === 1)
  check('  the reason names the shared entity', pairs[0].reasons.some((r) => r.includes('entity')))
}

{
  const a = process('a', { primaryEquipmentIds: ['tank1'] })
  const b = process('b', { primaryEquipmentIds: ['tank1'] })
  const pairs = generateCandidatePairs([a, b], new Map())
  check('sharing primary equipment produces a candidate', pairs.length === 1)
  check('  the reason names the shared equipment', pairs[0].reasons.some((r) => r.includes('major equipment')))
}

{
  const a = process('a', { flags: { highOverlapWith: ['b'] } })
  const b = process('b')
  const pairs = generateCandidatePairs([a, b], new Map())
  check('a step2 high-overlap flag produces a candidate', pairs.length === 1)
}

{
  const byId = new Map([
    ['out1', entity('out1', { resolvedConnections: ['in1'] })],
    ['in1', entity('in1')],
  ])
  const a = process('a', { outletEntityIds: ['out1'] })
  const b = process('b', { inletEntityIds: ['in1'] })
  const pairs = generateCandidatePairs([a, b], byId)
  check('a resolved connection from one\'s outlet to the other\'s inlet produces a candidate', pairs.length === 1)
  check('  the reason names the connection', pairs[0].reasons.some((r) => r.includes('connects')))
}

{
  const a = process('a', { bbox: { x: 0.50, y: 0.50, width: 0.02, height: 0.02 } })
  const b = process('b', { bbox: { x: 0.53, y: 0.50, width: 0.02, height: 0.02 } })
  const pairs = generateCandidatePairs([a, b], new Map())
  check('close bboxes with no other signal still produce a (weak) candidate', pairs.length === 1)
}

{
  const a = process('a', { bbox: { x: 0.0, y: 0.0, width: 0.02, height: 0.02 } })
  const b = process('b', { bbox: { x: 0.9, y: 0.9, width: 0.02, height: 0.02 } })
  const pairs = generateCandidatePairs([a, b], new Map())
  check('two unrelated, distant processes produce NO candidate', pairs.length === 0)
}

// ================================================================ cluster.js

// Core mechanic the user asked for: DSU transitivity. (A,B) yes + (B,C) yes
// must produce ONE node containing all three, even though A and C were
// never directly compared.
{
  const processes = [process('a'), process('b'), process('c'), process('d')]
  const pairs = [
    { id: 'c1', aId: 'a', bId: 'b', reasons: ['x'] },
    { id: 'c2', aId: 'b', bId: 'c', reasons: ['x'] },
    { id: 'c3', aId: 'c', bId: 'd', reasons: ['x'] },
  ]
  const verdicts = [
    { pairId: 'c1', sameNode: true, confidence: 0.9, reasoning: 'r' },
    { pairId: 'c2', sameNode: true, confidence: 0.9, reasoning: 'r' },
    { pairId: 'c3', sameNode: false, confidence: 0.9, reasoning: 'r' },
  ]
  const page = assemblePage({ pageIndex: 0, processes, pairs, verdicts, error: null })
  check('exactly 2 nodes result: {a,b,c} and {d}', page.nodes.length === 2, `got ${page.nodes.length}`)
  const abc = page.nodes.find((n) => n.memberCount === 3)
  check('a transitively merges with c despite never being directly compared',
    abc && new Set(abc.processIds).size === 3 && ['a','b','c'].every(id => abc.processIds.includes(id)))
}

// A low-confidence "yes" must not be applied — same low-confidence handling
// as every other stage.
{
  const processes = [process('a'), process('b')]
  const pairs = [{ id: 'c1', aId: 'a', bId: 'b', reasons: ['x'] }]
  const verdicts = [{ pairId: 'c1', sameNode: true, confidence: 0.2, reasoning: 'r' }]
  const page = assemblePage({ pageIndex: 0, processes, pairs, verdicts, error: null })
  check('a low-confidence "yes" is not applied — processes stay separate', page.nodes.length === 2)
  check('  the verdict is still recorded on the audit trail, unapplied',
    page.diagnostics.appliedVerdicts[0].applied === false)
}

// A hallucinated pairId (referencing a pair never offered) is recorded, not trusted.
{
  const processes = [process('a'), process('b')]
  const pairs = [{ id: 'c1', aId: 'a', bId: 'b', reasons: ['x'] }]
  const verdicts = [{ pairId: 'c-DOES-NOT-EXIST', sameNode: true, confidence: 0.9, reasoning: 'r' }]
  const page = assemblePage({ pageIndex: 0, processes, pairs, verdicts, error: null })
  check('a and b stay separate — the bogus verdict changes nothing', page.nodes.length === 2)
  check('the invalid pairId is recorded for review', page.diagnostics.invalidPairIds.includes('c-DOES-NOT-EXIST'))
}

// A candidate pair the model never answered is recorded as missing, and
// defaults to NOT merging (never silently assumed true).
{
  const processes = [process('a'), process('b')]
  const pairs = [{ id: 'c1', aId: 'a', bId: 'b', reasons: ['x'] }]
  const page = assemblePage({ pageIndex: 0, processes, pairs, verdicts: [], error: null })
  check('an unanswered pair defaults to staying separate', page.nodes.length === 2)
  check('  it is recorded as a missing verdict', page.diagnostics.missingVerdicts.includes('c1'))
}

// A process with no candidate pairs at all still becomes its own node.
{
  const processes = [process('lonely')]
  const page = assemblePage({ pageIndex: 0, processes, pairs: [], verdicts: [], error: null })
  check('an unpaired process becomes a singleton node', page.nodes.length === 1 && page.nodes[0].memberCount === 1)
}

// Node geometry is derived, never invented: bbox is the union of member
// processes' real bboxes, and a route shared by two merged processes is not
// duplicated.
{
  const processes = [
    process('a', { bbox: { x: 0.10, y: 0.10, width: 0.10, height: 0.10 }, routes: [{ entityId: 'ln1', points: [] }] }),
    process('b', { bbox: { x: 0.30, y: 0.10, width: 0.10, height: 0.10 }, routes: [{ entityId: 'ln1', points: [] }, { entityId: 'ln2', points: [] }] }),
  ]
  const pairs = [{ id: 'c1', aId: 'a', bId: 'b', reasons: ['x'] }]
  const verdicts = [{ pairId: 'c1', sameNode: true, confidence: 0.9, reasoning: 'r' }]
  const page = assemblePage({ pageIndex: 0, processes, pairs, verdicts, error: null })
  const node = page.nodes[0]
  check('bbox is the union of both real process boxes', Math.abs(node.bbox.width - 0.30) < 1e-9, `got ${JSON.stringify(node.bbox)}`)
  check('a route shared by both merged processes is not duplicated', node.routes.length === 2, `got ${node.routes.length}`)
}

console.log(`\n${pass} passed, ${fail} failed`)
process.exitCode = fail ? 1 : 0
