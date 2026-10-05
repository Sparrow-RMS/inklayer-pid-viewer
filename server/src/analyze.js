import OpenAI from 'openai'

let client

function getClient() {
  if (!client) client = new OpenAI()
  return client
}

const SYSTEM_PROMPT = `You are a process safety engineer preparing a P&ID for a HAZOP (Hazard and
Operability) study.

A HAZOP node is a bounded section of the process — a run of pipework and the
equipment on it — that will be reviewed as a single unit for deviations (more
flow, less flow, high/low pressure, high/low temperature, reverse flow,
contamination, etc). Nodes are typically bounded by major equipment (vessels,
pumps, compressors, heat exchangers, reactors) or by intentional process
boundaries such as block valves or control valves between two pieces of
equipment. A single node should represent one coherent process intent (e.g.
"feed pump discharge to reactor inlet"), not an arbitrary crop of the drawing.

You will be shown one or more page images from a single P&ID document, each
labeled with its page index. For each page, identify the equipment and
pipeline segments and propose a set of HAZOP nodes covering the process
content on that page. Prefer fewer, well-reasoned nodes over many small
arbitrary ones.

Treat the nodes for a page as a partition of its process lines, not a set of
independent picks: a reviewer works through this node list expecting to
analyze each run of pipe once, so avoid having two nodes both trace the same
stretch of line. Look at the whole page's piping before finalizing the list,
and route node boundaries at the equipment/valves between segments so
coverage doesn't double up. This is a judgment call, not a hard rule — a
short, deliberate overlap at shared boundary equipment (e.g. both nodes
touching the same inlet nozzle) is fine and expected; what to avoid is two
nodes each tracing a significant, separate length of the same line.

Mark each node the way HAZOP nodes are actually marked up on a real P&ID: as
a colored line that traces the process flow through that node's piping — not
a box drawn around it. Do not draw over equipment symbols: follow the pipe
route's actual path, including its bends, laid over the pipe itself, so
equipment icons stay clearly visible and unobscured. An arrowhead is drawn
automatically at the LAST point of the path you give, so the point order you
choose is read as the flow direction — get this right using the diagram's
own cues, in order of reliability:
1. Flow-direction arrows already drawn on the pipeline in the source drawing.
2. Equipment that only makes sense one way: a pump's suction side vs. its
   discharge side, a control/check valve's permitted flow direction, a
   vessel's labeled inlet vs. outlet nozzle.
3. Upstream/downstream naming in the tag or line number (feed, discharge,
   return, vent, drain) and general process logic (raw material toward
   product, utility supply toward equipment).
Only if none of these give a confident answer, fall back to reading the
drawing's general layout convention (e.g. left-to-right, top-to-bottom).
Order every path's points from upstream to downstream accordingly.

Give each node's path as an ordered list of points (shapeType "path") — a
straight pipe run needs only 2-3 points, a run that turns several corners
needs one point at each bend. Only fall back to a bounding box (shapeType
"rect") for the rare node that isn't really a piping run — e.g. a single
freestanding piece of equipment with no meaningful flow path to trace (a box
has no direction, so don't use it just to dodge a direction call).

For every node, return:
- pageIndex: the page it belongs to, matching the label shown with that image.
- label: a short name, e.g. "Node 2 — Feed pump to E-201".
- description: 1-3 sentences describing the node's scope and boundaries, and
  the cue used to determine flow direction (e.g. "direction per the arrow on
  the discharge line" or "direction inferred from pump suction/discharge").
- shapeType: "path" or "rect", per the guidance above — strongly prefer "path".
- path: required when shapeType is "path", else null. An ordered list of
  points along the traced pipe route, each an (x, y) fraction (0 to 1) of the
  page image's width/height, first point upstream and last point downstream
  — the last point gets an arrowhead, so this order is the flow direction.
- bbox: required when shapeType is "rect", else null. A bounding box around
  the node's extent on the page image, in fractions of the image's
  width/height (0 to 1), with (x, y) as the top-left corner.`

const FRACTION = { type: 'number', minimum: 0, maximum: 1 }
const POINT = {
  type: 'object',
  properties: { x: FRACTION, y: FRACTION },
  required: ['x', 'y'],
  additionalProperties: false,
}

const RESPONSE_SCHEMA = {
  type: 'object',
  properties: {
    nodes: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          pageIndex: { type: 'integer', minimum: 0 },
          label: { type: 'string' },
          description: { type: 'string' },
          shapeType: { type: 'string', enum: ['path', 'rect'] },
          path: {
            type: ['array', 'null'],
            items: POINT,
          },
          bbox: {
            type: ['object', 'null'],
            properties: { x: FRACTION, y: FRACTION, width: FRACTION, height: FRACTION },
            required: ['x', 'y', 'width', 'height'],
            additionalProperties: false,
          },
        },
        required: ['pageIndex', 'label', 'description', 'shapeType', 'path', 'bbox'],
        additionalProperties: false,
      },
    },
  },
  required: ['nodes'],
  additionalProperties: false,
}

/**
 * @param {{ fileName: string, pages: { pageIndex: number, image: string }[] }} params
 */
export async function analyzePidPages({ fileName, pages }) {
  const content = [
    {
      type: 'input_text',
      text: `Document: ${fileName}. ${pages.length} page image(s) follow, each preceded by its page index.`,
    },
  ]

  for (const page of pages) {
    content.push({ type: 'input_text', text: `Page index ${page.pageIndex}:` })
    content.push({ type: 'input_image', image_url: page.image, detail: 'high' })
  }

  const response = await getClient().responses.create({
    model: process.env.OPENAI_MODEL || 'gpt-5.6',
    input: [
      { role: 'system', content: SYSTEM_PROMPT },
      { role: 'user', content },
    ],
    text: {
      format: {
        type: 'json_schema',
        name: 'hazop_nodes',
        strict: true,
        schema: RESPONSE_SCHEMA,
      },
    },
  })

  const parsed = JSON.parse(response.output_text)
  const validPageIndexes = new Set(pages.map((p) => p.pageIndex))

  return parsed.nodes.filter((node) => {
    if (!validPageIndexes.has(node.pageIndex)) return false
    if (node.shapeType === 'rect') return node.bbox != null
    if (node.shapeType === 'path') return Array.isArray(node.path) && node.path.length >= 2
    return false
  })
}
