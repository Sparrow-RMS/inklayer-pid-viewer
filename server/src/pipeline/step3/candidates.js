/**
 * Step 3 — candidate pair generation.
 *
 * Deciding whether every pair of processes belongs in one HAZOP node is an
 * O(n^2) question, and asking a model to judge a pair with no real relation
 * (opposite corners of the sheet, nothing shared) wastes tokens and invites
 * a spurious merge. This narrows the field to pairs with a REAL reason to
 * suspect they belong together, computed entirely from step 1/2's already
 * grounded data — no model call.
 *
 * Each candidate carries its `reasons`, which are shown to the model (so its
 * judgment is grounded in why the pair was even asked about) and are also
 * what let a reviewer audit a clustering decision afterward.
 */
import { bboxGap } from '../geometry.js'

/** Two processes physically closer than this (page fractions) are worth
 *  asking about even with no other signal — HAZOP boundaries are sometimes
 *  purely spatial (adjacent equipment with no drawn connection captured as
 *  an entity). Tight enough that "just somewhere on a dense sheet" doesn't
 *  qualify. */
const SPATIAL_ADJACENCY_GAP = 0.03

export function generateCandidatePairs(processes, entityById) {
  const pairs = []

  for (let i = 0; i < processes.length; i++) {
    for (let j = i + 1; j < processes.length; j++) {
      const a = processes[i]
      const b = processes[j]
      const reasons = pairReasons(a, b, entityById)
      if (reasons.length === 0) continue
      pairs.push({ id: `c${pairs.length + 1}`, aId: a.id, bId: b.id, reasons })
    }
  }

  return pairs
}

function pairReasons(a, b, entityById) {
  const reasons = []

  const bEntities = new Set(b.entityIds)
  const sharedEntities = a.entityIds.filter((id) => bEntities.has(id))
  if (sharedEntities.length) reasons.push(`shares ${sharedEntities.length} entity/ies directly`)

  const aPrimary = new Set(a.primaryEquipmentIds)
  const sharedPrimary = b.primaryEquipmentIds.filter((id) => aPrimary.has(id))
  if (sharedPrimary.length) reasons.push(`shares major equipment (${sharedPrimary.join(', ')})`)

  if (a.flags.highOverlapWith?.includes(b.id)) reasons.push('flagged in step 2 as heavily overlapping')

  if (connects(a.outletEntityIds, b, entityById) || connects(b.outletEntityIds, a, entityById)) {
    reasons.push('one\'s outlet connects to the other\'s piping')
  }

  if (!reasons.length && bboxGap(a.bbox, b.bbox) <= SPATIAL_ADJACENCY_GAP) {
    reasons.push('physically adjacent on the sheet')
  }

  return reasons

  // True if any entity in `outletIds` is directly connected (per step 1's
  // resolved connectsTo graph) to any entity belonging to `target`, or sits
  // among target's own entities/inlets.
  function connects(outletIds, target, byId) {
    const targetEntities = new Set([...target.entityIds, ...target.inletEntityIds])
    for (const outletId of outletIds ?? []) {
      if (targetEntities.has(outletId)) return true
      const resolved = byId.get(outletId)?.resolvedConnections ?? []
      if (resolved.some((id) => targetEntities.has(id))) return true
    }
    return false
  }
}
