/**
 * Step 1 output contract.
 *
 * Two schemas: one for a tile read (the entity inventory) and one for the
 * whole-page read (drawing-level metadata that only makes sense in context —
 * title block, notes, legend, revisions).
 *
 * Both are written for the strict JSON-schema subset: every property appears
 * in `required`, and `additionalProperties` is false throughout. That subset
 * has no free-form object, so anything open-ended is an array of
 * {key, value} pairs; `prompts.js` fixes which keys to emit per category so
 * the result stays consistent across tiles anyway.
 */

/** The discipline categories an entity can fall into. Deliberately broad —
 *  step 2 does the process-level reasoning, step 1 only has to see things. */
export const ENTITY_CATEGORIES = [
  'equipment',   // vessels, pumps, compressors, exchangers, reactors, tanks, filters
  'instrument',  // ISA bubbles: transmitters, indicators, controllers, switches, analyzers
  'valve',       // manual, control, relief, check, on/off
  'line',        // process / utility piping runs and signal lines
  'fitting',     // reducers, strainers, spec breaks, blinds, flanges, steam traps
  'connector',   // off-page connectors, continuation arrows, drawing-to-drawing refs
  'note',        // numbered or flagged notes attached to the drawing body
  'label',       // free text callouts not covered above
  'equipment_list_row', // a row of an on-sheet equipment/line schedule table
  'interlock_ref',      // a diamond/circle cross-reference to an interlock or cause-and-effect logic diagram
]

const FRACTION = { type: 'number' }

const BBOX = {
  type: 'object',
  description: 'Tile-relative box, fractions 0-1 of the tile image, (x,y) = top-left corner.',
  properties: { x: FRACTION, y: FRACTION, width: FRACTION, height: FRACTION },
  required: ['x', 'y', 'width', 'height'],
  additionalProperties: false,
}

const POINT = {
  type: 'object',
  properties: { x: FRACTION, y: FRACTION },
  required: ['x', 'y'],
  additionalProperties: false,
}

const KEY_VALUE = {
  type: 'object',
  properties: {
    key: { type: 'string', description: 'Attribute name, lowerCamelCase, from the list given in the instructions.' },
    value: { type: 'string', description: 'Attribute value exactly as written on the drawing.' },
  },
  required: ['key', 'value'],
  additionalProperties: false,
}

const ENTITY = {
  type: 'object',
  properties: {
    localId: {
      type: 'string',
      description: 'Short id unique within this tile (e.g. "e1", "v3"). Used by other entities in this tile to refer to this one.',
    },
    category: { type: 'string', enum: ENTITY_CATEGORIES },
    tag: {
      type: ['string', 'null'],
      description: 'The equipment/instrument/line tag exactly as printed (e.g. "P-101A", "FT-1234", "6\\"-P-1234-A1"). Null if the item carries no tag.',
    },
    type: {
      type: 'string',
      description: 'Specific discipline type, e.g. "centrifugal pump", "shell-and-tube exchanger", "globe control valve", "pressure transmitter", "process line".',
    },
    description: { type: 'string', description: 'One sentence: what it is and what it does here.' },
    attributes: { type: 'array', items: KEY_VALUE, description: 'Everything else legible about this item.' },
    textAnchorIds: {
      type: 'array',
      items: { type: 'string' },
      description: 'Ids of the supplied text items that make up this entity\'s tag and labels. Anchor whenever the text exists in the list — this is what gives the entity exact coordinates.',
    },
    bbox: BBOX,
    polyline: {
      type: ['array', 'null'],
      items: POINT,
      description: 'For category "line" only: the traced route, ordered upstream to downstream. Null otherwise.',
    },
    connectsTo: {
      type: 'array',
      items: { type: 'string' },
      description: 'Tags or localIds this item is directly connected to by pipe or signal, as read off the drawing.',
    },
    confidence: { type: 'number', description: '0-1. Lower it when the symbol is ambiguous or the text is unclear.' },
  },
  required: ['localId', 'category', 'tag', 'type', 'description', 'attributes', 'textAnchorIds', 'bbox', 'polyline', 'connectsTo', 'confidence'],
  additionalProperties: false,
}

export const TILE_SCHEMA = {
  type: 'object',
  properties: {
    entities: { type: 'array', items: ENTITY },
    tileNotes: {
      type: 'string',
      description: 'Anything about this tile worth passing on: illegible regions, cut-off items, apparent drawing errors. Empty string if none.',
    },
  },
  required: ['entities', 'tileNotes'],
  additionalProperties: false,
}

const NOTE = {
  type: 'object',
  properties: {
    ref: { type: ['string', 'null'], description: 'Note number or flag, e.g. "1", "A". Null if unnumbered.' },
    text: { type: 'string' },
  },
  required: ['ref', 'text'],
  additionalProperties: false,
}

export const PAGE_META_SCHEMA = {
  type: 'object',
  properties: {
    titleBlock: {
      type: 'array',
      items: KEY_VALUE,
      description: 'Title block fields: drawingNumber, title, sheet, revision, plant, unit, client, contractor, scale, date, approvedBy — whichever are present.',
    },
    notes: { type: 'array', items: NOTE, description: 'General/specific notes printed on the sheet.' },
    legend: {
      type: 'array',
      items: KEY_VALUE,
      description: 'Legend/symbol key entries, key = symbol or abbreviation, value = its meaning.',
    },
    revisions: {
      type: 'array',
      items: KEY_VALUE,
      description: 'Revision history rows, key = revision id, value = date + description + issuer.',
    },
    referencedDrawings: {
      type: 'array',
      items: { type: 'string' },
      description: 'Other drawing numbers this sheet refers to (continuations, related P&IDs).',
    },
    overallDescription: {
      type: 'string',
      description: '2-4 sentences: what process this sheet covers, its main equipment, and where material enters and leaves.',
    },
  },
  required: ['titleBlock', 'notes', 'legend', 'revisions', 'referencedDrawings', 'overallDescription'],
  additionalProperties: false,
}
