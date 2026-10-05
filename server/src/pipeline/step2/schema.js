/**
 * Step 2 output contract — process classification.
 *
 * A "process" here is a coherent, functionally-named stretch of the P&ID:
 * one or more directly-related entities from step 1 (equipment, valves,
 * instruments, lines) that together perform one job — a transfer, a storage
 * function, an agitation duty, a utility supply, a relief path.
 *
 * The critical constraint: a process NEVER carries its own coordinates. It
 * only references entityIds from step 1's inventory. Geometry is derived by
 * `classify.js` as the union of those entities' real (often text-anchored,
 * exact) geometry — so a process's position is always exactly as accurate as
 * step 1 already made it, and step 3's clustering inherits that for free.
 */

export const PROCESS_CATEGORIES = [
  'transfer',        // moving material from one point to another
  'storage',         // a vessel/tank holding material
  'agitation',       // mixing/agitating duty
  'filtration',      // filtering, straining, backwashing
  'heat-transfer',   // heating, cooling, exchange
  'utility-supply',  // steam, water, air, nitrogen, electrical supply to the process
  'relief-safety',   // PSVs, rupture discs, vents, ESD-related piping
  'drain',           // drains, sumps, blowdown
  'sampling',        // sample points and their piping
  'unloading',       // tanker/drum/rail unloading or loading connections
  'control-loop',    // an instrument loop's own signal wiring, when it isn't
                      // naturally part of the process line it measures/controls
  'other',
]

const REASON_KEY_VALUE = {
  type: 'object',
  properties: {
    key: { type: 'string' },
    value: { type: 'string' },
  },
  required: ['key', 'value'],
  additionalProperties: false,
}

const PROCESS = {
  type: 'object',
  properties: {
    localId: {
      type: 'string',
      description: 'Short id unique on this page (e.g. "proc1"). Not shown to the user; used only to catch duplicates.',
    },
    name: {
      type: 'string',
      description: 'Short functional label, e.g. "ST-V303 transfer to distribution manifold via P-V303A/B".',
    },
    category: { type: 'string', enum: PROCESS_CATEGORIES },
    description: {
      type: 'string',
      description: '2-4 sentences: what happens, naming the real equipment/line tags involved, in flow order where relevant.',
    },
    entityIds: {
      type: 'array',
      items: { type: 'string' },
      description: 'Every entity id from the supplied inventory that is part of this process. Reference ids exactly as given — never invent one.',
    },
    primaryEquipmentIds: {
      type: 'array',
      items: { type: 'string' },
      description: 'The subset of entityIds that anchor this process — its major equipment/vessels/pumps, not every valve and fitting on the way. Used later to judge whether two processes share the same equipment.',
    },
    inletEntityIds: {
      type: 'array',
      items: { type: 'string' },
      description: 'Entity id(s) marking where material/signal ENTERS this process (an upstream connector, line, or nozzle). Empty if this process is a self-contained function with no clear inlet (e.g. a vessel\'s agitation duty).',
    },
    outletEntityIds: {
      type: 'array',
      items: { type: 'string' },
      description: 'Entity id(s) marking where material/signal LEAVES this process. Empty if none.',
    },
    flowDirection: {
      type: 'string',
      description: 'One line, upstream to downstream, naming tags in order, e.g. "ST-V303 -> P-V303A/B -> F-V303A/B -> distribution manifold". Empty string if not applicable.',
    },
    boundaryReasoning: {
      type: 'array',
      items: REASON_KEY_VALUE,
      description: 'Optional: why the boundary was drawn where it was, e.g. {key: "upstream", value: "block valve HV-201 taken as the boundary, per the standard HAZOP convention of cutting at isolation valves"}.',
    },
    confidence: { type: 'number', description: '0-1.' },
  },
  required: [
    'localId', 'name', 'category', 'description', 'entityIds', 'primaryEquipmentIds',
    'inletEntityIds', 'outletEntityIds', 'flowDirection', 'boundaryReasoning', 'confidence',
  ],
  additionalProperties: false,
}

export const PAGE_PROCESS_SCHEMA = {
  type: 'object',
  properties: {
    processes: { type: 'array', items: PROCESS },
    pageNotes: {
      type: 'string',
      description: 'Anything worth flagging about this page\'s process breakdown: ambiguous boundaries, entities that genuinely fit no process. Empty string if none.',
    },
  },
  required: ['processes', 'pageNotes'],
  additionalProperties: false,
}
