/**
 * Step 3 output contract — pairwise same-node verdicts.
 *
 * The model is never asked to invent groupings directly. It judges each
 * candidate pair independently ("do A and B belong in the same HAZOP node?")
 * and `cluster.js` unions the "yes" verdicts with the same `DisjointSet`
 * used in step 1 — the actual grouping is arithmetic, not a model guess.
 */
const VERDICT = {
  type: 'object',
  properties: {
    pairId: { type: 'string', description: 'The id of the candidate pair being judged, exactly as given.' },
    sameNode: {
      type: 'boolean',
      description: 'true if these two processes belong in one HAZOP node, false if they should stay separate nodes.',
    },
    reasoning: { type: 'string', description: 'One or two sentences: the HAZOP boundary logic behind this verdict.' },
    confidence: { type: 'number', description: '0-1.' },
  },
  required: ['pairId', 'sameNode', 'reasoning', 'confidence'],
  additionalProperties: false,
}

export const PAIR_VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    verdicts: { type: 'array', items: VERDICT },
  },
  required: ['verdicts'],
  additionalProperties: false,
}

/**
 * Consolidation pass output contract — continuous relatedness, not a binary
 * verdict. Used only when the base clustering leaves more nodes than a
 * requested target permits; `consolidate.js` greedily merges pairs in
 * descending relatedness order until the target is met.
 */
const RELATEDNESS_ITEM = {
  type: 'object',
  properties: {
    pairId: { type: 'string', description: 'The id of the pair being scored, exactly as given.' },
    relatedness: {
      type: 'number',
      description: '0-1: how strongly these two nodes belong under one broader review node. 0 = no reason to ever combine, 1 = obviously one scope.',
    },
    reasoning: { type: 'string', description: 'One or two sentences grounding the score in real equipment/piping.' },
  },
  required: ['pairId', 'relatedness', 'reasoning'],
  additionalProperties: false,
}

export const RELATEDNESS_SCHEMA = {
  type: 'object',
  properties: { scores: { type: 'array', items: RELATEDNESS_ITEM } },
  required: ['scores'],
  additionalProperties: false,
}
