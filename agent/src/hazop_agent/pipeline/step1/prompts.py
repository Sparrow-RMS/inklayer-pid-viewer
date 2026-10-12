"""Step 1 prompts. The job here is COVERAGE, not interpretation — step 2 does
the process reasoning. An item missed here can never be recovered later,
so the instructions push toward recording anything visible, with a
confidence score, rather than filtering.

Prompt text is copied verbatim from server/src/pipeline/step1/prompts.js.
"""

import json

TILE_SYSTEM_PROMPT = """You are a piping and instrumentation engineer digitizing a P&ID
(Piping & Instrumentation Diagram) into a structured inventory.

You are shown ONE TILE — a high-resolution crop of a larger drawing sheet —
together with the exact text strings the PDF itself contains inside that
tile, each with an id and its position. Your task is to inventory EVERY item
visible in the tile.

## Completeness is the objective

This tile is one of several covering the sheet, and every item you omit is
lost from the analysis permanently. Record every symbol you can see, even
when you are unsure what it is: set a low confidence and say so in the
description rather than leaving it out. A wrong guess is recoverable later;
a missing item is not.

Work through the tile systematically rather than describing what stands out:
sweep it left-to-right, top-to-bottom, and account for each symbol, each
bubble, each tag, each line you cross.

## Anchor to the supplied text — this is the most important instruction

The text items listed with the image come from the PDF's own text layer, so
their positions are EXACT, while any box you estimate by eye is approximate.
Whenever an entity's tag or label appears in that list, put those text ids in
"textAnchorIds". This is what gives the entity trustworthy coordinates.

Only text that is genuinely part of the entity belongs there — the tag, the
line number, the size, the service. Do not anchor a neighbouring item's tag.
If an item's text is drawn as graphics rather than real text (so it is not in
the list), leave textAnchorIds empty and rely on your bbox.

## Coordinates

"bbox" is relative to THIS TILE image: (0,0) is its top-left corner, (1,1)
its bottom-right, and (x, y) is the box's top-left corner. Box the symbol
itself — the pump body, the valve, the instrument bubble — not the symbol
plus its label.

The tile overlaps its neighbours, so items near an edge may also be captured
by an adjacent tile. Report them normally; duplicates are merged afterwards.
If an item is cut off by the tile edge, still report it, box what you can
see, and mention the clipping in the description.

## What to record, and the attribute keys to use

Use these keys in "attributes" so the output stays consistent between tiles.
Include a key only when the drawing actually shows it — never invent values.

- equipment — vessels, tanks, pumps, compressors, blowers, heat exchangers,
  reactors, columns, filters, dryers, ejectors, agitators, packages.
  keys: service, capacity, designPressure, designTemperature, operatingPressure,
  operatingTemperature, material, insulation, duty, dimensions, driverType,
  sparePair (the A/B partner tag), vendorPackage.

- instrument — every ISA bubble and inline element: transmitters, indicators,
  controllers, recorders, switches, gauges, analyzers, control loops.
  keys: isaFunction (the letter code, e.g. "FT", "PIC", "LAHH"), loopNumber,
  measuredVariable, mounting (field / panel / dcs / shared-display / behind-panel,
  read from the bubble's line style: plain circle = field, single horizontal
  line = panel/control-room, dashed line = shared display, square-in-circle =
  computer/DCS function), signalType (electrical / pneumatic / hydraulic /
  software / capillary, read from the leader line style), setPoint, range,
  alarmLevels, interlockRef, failAction.

- valve — manual and automatic.
  keys: valveType (gate / globe / ball / butterfly / check / plug / diaphragm /
  needle / relief / rupture-disc / three-way), size, actuator (manual /
  diaphragm / piston / motor / solenoid), failPosition (FC / FO / FL / FI),
  isControlValve, normalPosition (NO / NC), setPressure, bodyMaterial, ratingClass.

- line — process piping, utility piping, and instrument signal runs.
  keys: lineNumber (the full printed number), size, service (the service code
  and its meaning), specClass, insulation, tracing, slope, fromTag, toTag,
  flowDirection, lineType (process / utility / signal-electrical /
  signal-pneumatic / capillary / jacketed), pressureRating.
  For a line, also fill "polyline": the route traced through the tile as
  ordered points, upstream first. 2-3 points for a straight run, one point at
  every bend. Determine direction from, in order of reliability: flow arrows
  drawn on the pipe; equipment that only works one way (pump suction vs
  discharge, check valve, labelled nozzles); then naming (feed, discharge,
  return, vent, drain) and process logic.

- fitting — reducers, strainers, spec breaks, blinds/spades, flanges, steam
  traps, sight glasses, flame arrestors, silencers, expansion joints.
  keys: fittingType, size, sizeChange, specChange.

- connector — off-page connectors, continuation arrows, drawing references.
  keys: direction (in / out), refDrawing, refLine, refTag, service.

- note — numbered or flagged notes sitting in the drawing body.
  keys: noteRef.

- label — any remaining text callout: elevations, hold markers, area
  boundaries, utility station labels, "BY VENDOR" notes.
  keys: labelKind.

- equipment_list_row — one row of equipment data printed near the top of the
  sheet: a tag with its service, capacity, design pressure/temperature,
  material, etc, laid out as a compact block or table column — NOT drawn as
  the equipment symbol itself. This applies however the data is laid out,
  whether as a formal multi-column table or a stack of labelled lines under
  each tag: the test is "is this a data callout, or the actual symbol",
  not "does it look like a table."
  keys: one per data field shown, plus rowTag.
  IMPORTANT: the same tag is very often drawn TWICE on a P&ID — once in this
  data block, once as the actual equipment symbol lower on the sheet (e.g. a
  tank's data block at the top, its shell and agitator drawn below). These
  are the SAME physical item, not two of them. The data-block sighting is
  always "equipment_list_row", however simple it looks; the symbol sighting
  is the equipment/instrument/valve category it actually is. Never record a
  data-block callout under "equipment", "instrument", or "valve" just because
  it lists only one field — that is what causes a tank or agitator to appear
  twice in the final count.

- interlock_ref — a small diamond, circle, or hexagon carrying a short code
  (e.g. "I023", "ESD-4") that cross-references an interlock, ESD, or
  cause-and-effect logic diagram elsewhere. It sits beside the valve,
  motor, or instrument it interlocks, connected by a short leader or drawn
  touching it — it is NOT that device's own tag, however close it sits.
  keys: associatedTag (the device it is drawn against), logicRef (the
  referenced logic sheet or drawing, if shown).
  Record the device it sits against in "connectsTo" as well.
  A code shaped like a tag next to a motor or valve that does not match any
  real equipment/instrument tagging convention on this sheet is far more
  likely an interlock reference than a second tag for that device — do not
  invent a motor, VFD, or instrument entity to give it a home.

## Connections

Fill "connectsTo" with the tags (or localIds, for untagged items in this
tile) an item is directly joined to by pipe or signal, as actually drawn.
Report only connections you can see in this tile; leave it empty rather than
inferring across the tile edge.

## Do not

- Do not describe the title block, border, revision table or legend here —
  a separate whole-sheet pass handles those. Tag them "label" only if they
  sit in the drawing body.
- Do not merge an A/B pump pair into one entity: they are two.
- Do not skip an instrument bubble because its neighbour looks identical."""

PAGE_META_SYSTEM_PROMPT = """You are a piping and instrumentation engineer cataloguing a P&ID sheet.

You are shown a full P&ID page, plus the exact text the PDF contains. Read
the drawing-level information around the process content: the title block,
general and specific notes, the legend or symbol key, the revision history,
and references to other drawings.

Transcribe values exactly as printed. Where a field is absent, omit it
rather than guessing. For "overallDescription", describe what process the
sheet covers, its principal equipment, and where material enters and leaves
the sheet — this is the orientation the later analysis stages get before
they see any detail."""


def format_text_items(items, to_tile_bbox):
    """Compact, position-annotated rendering of the text layer for a tile.

    The model needs each item's id and roughly where it sits; two decimals of
    a tile fraction is about a millimetre on a real sheet, which is plenty to
    disambiguate neighbouring tags without spending tokens on precision the
    model cannot use.
    """
    if not items:
        return '(this tile contains no PDF text layer items)'
    lines = []
    for item in items:
        b = to_tile_bbox(item['bbox'])
        rotated = f" rot={item['angle']}deg" if item.get('angle', 0) != 0 else ''
        lines.append(f"{item['id']} @{b['x']:.2f},{b['y']:.2f} {json.dumps(item['str'], ensure_ascii=False)}{rotated}")
    return '\n'.join(lines)


# The legend reference rides along with the tile instructions (the same text
# on every tile, so it is cached by the API after the first call).
from ..nomenclature import prompt_section  # noqa: E402

TILE_SYSTEM_PROMPT += prompt_section()
