import { assemblePage } from '../src/pipeline/step3/consolidate.js'

let pass = 0, fail = 0
const check = (name, cond, detail = '') => {
  if (cond) { pass++; console.log(`  PASS  ${name}`) }
  else { fail++; console.log(`  FAIL  ${name} ${detail}`) }
}

const node = (id, overrides = {}) => ({
  id, page: 0, label: id, description: 'd', categories: ['transfer'],
  processIds: [id], entityIds: [], primaryEquipmentIds: [],
  bbox: { x: 0.5, y: 0.5, width: 0.02, height: 0.02 }, routes: [], ...overrides,
})

// --- 1. already at/under target: no model call needed, nodes pass through untouched
{
  const nodes = [node('a'), node('b')]
  const page = assemblePage({ pageIndex: 0, nodes, pairs: [], scores: [], error: null }, 5)
  check('at-or-under-target is a no-op', page.nodes.length === 2 && page.diagnostics.skipped === true)
}

// --- 2. core mechanic: greedy strongest-first merging stops the INSTANT the target is met
{
  const nodes = ['a', 'b', 'c', 'd', 'e'].map((id) => node(id))
  const pairs = [
    { id: 'k1', aId: 'a', bId: 'b' }, { id: 'k2', aId: 'c', bId: 'd' },
    { id: 'k3', aId: 'a', bId: 'c' }, { id: 'k4', aId: 'b', bId: 'e' },
    { id: 'k5', aId: 'd', bId: 'e' },
  ]
  const scores = [
    { pairId: 'k1', relatedness: 0.95, reasoning: 'r' }, // strongest — merge first
    { pairId: 'k2', relatedness: 0.90, reasoning: 'r' }, // 2nd — merge next -> now at target of 3
    { pairId: 'k3', relatedness: 0.80, reasoning: 'r' }, // would merge {a,b} with {c,d} — must NOT apply once target is hit
    { pairId: 'k4', relatedness: 0.10, reasoning: 'r' },
    { pairId: 'k5', relatedness: 0.05, reasoning: 'r' },
  ]
  const page = assemblePage({ pageIndex: 0, nodes, pairs, scores, error: null }, 3, 0.3)
  check('stops exactly at the target, not below it', page.nodes.length === 3, `got ${page.nodes.length}`)
  check('the two strongest pairs were the ones applied',
    page.diagnostics.appliedMerges.length === 2 &&
    page.diagnostics.appliedMerges.every((m) => m.relatedness >= 0.9))
  check('a weaker pair that would over-merge past target is not applied',
    !page.diagnostics.appliedMerges.some((m) => m.relatedness === 0.8))
}

// --- 3. forced merges (below the relatedness floor) are flagged, not hidden
{
  const nodes = ['a', 'b', 'c'].map((id) => node(id))
  const pairs = [
    { id: 'k1', aId: 'a', bId: 'b' },
    { id: 'k2', aId: 'a', bId: 'c' },
    { id: 'k3', aId: 'b', bId: 'c' },
  ]
  // Nothing is genuinely related, but target=1 forces full consolidation anyway.
  const scores = [
    { pairId: 'k1', relatedness: 0.1, reasoning: 'weak' },
    { pairId: 'k2', relatedness: 0.05, reasoning: 'weak' },
    { pairId: 'k3', relatedness: 0.02, reasoning: 'weak' },
  ]
  const page = assemblePage({ pageIndex: 0, nodes, pairs, scores, error: null }, 1, 0.3)
  check('the hard target is still honored even with no good relations', page.nodes.length === 1)
  check('every applied merge below the floor is flagged forced', page.diagnostics.appliedMerges.every((m) => m.forced === true))
  check('forcedMergeCount reflects this', page.diagnostics.forcedMergeCount === page.diagnostics.appliedMerges.length)
}

// --- 4. a hallucinated pairId in the scores is recorded, not trusted; an
// unscored pair defaults to relatedness 0 (last resort, never preferred).
{
  const nodes = ['a', 'b', 'c'].map((id) => node(id))
  const pairs = [
    { id: 'k1', aId: 'a', bId: 'b' },
    { id: 'k2', aId: 'a', bId: 'c' }, // deliberately never scored
  ]
  const scores = [
    { pairId: 'k1', relatedness: 0.9, reasoning: 'r' },
    { pairId: 'k-BOGUS', relatedness: 0.99, reasoning: 'r' },
  ]
  const page = assemblePage({ pageIndex: 0, nodes, pairs, scores, error: null }, 2, 0.3)
  check('the real, scored pair is the one that merges', page.nodes.length === 2)
  check('the hallucinated pairId is recorded', page.diagnostics.invalidPairIds.includes('k-BOGUS'))
  check('the never-scored pair is recorded as missing', page.diagnostics.missingScores.includes('k2'))
}

// --- 5. node geometry/provenance: bbox union, routes deduped, sub-node ids preserved
{
  const nodes = [
    node('a', { bbox: { x: 0.1, y: 0.1, width: 0.1, height: 0.1 }, routes: [{ entityId: 'ln1', points: [] }], processIds: ['proc-1'] }),
    node('b', { bbox: { x: 0.3, y: 0.1, width: 0.1, height: 0.1 }, routes: [{ entityId: 'ln1', points: [] }, { entityId: 'ln2', points: [] }], processIds: ['proc-2'] }),
  ]
  const pairs = [{ id: 'k1', aId: 'a', bId: 'b' }]
  const scores = [{ pairId: 'k1', relatedness: 0.9, reasoning: 'r' }]
  const page = assemblePage({ pageIndex: 0, nodes, pairs, scores, error: null }, 1, 0.3)
  const n = page.nodes[0]
  check('bbox is the union of both real node boxes', Math.abs(n.bbox.width - 0.3) < 1e-9, `got ${JSON.stringify(n.bbox)}`)
  check('a route shared by both is not duplicated', n.routes.length === 2)
  check('original process ids are preserved through consolidation', n.processIds.sort().join(',') === 'proc-1,proc-2')
  check('sub-node provenance is recorded', n.subNodeIds.sort().join(',') === 'a,b')
}

console.log(`\n${pass} passed, ${fail} failed`)
process.exitCode = fail ? 1 : 0
