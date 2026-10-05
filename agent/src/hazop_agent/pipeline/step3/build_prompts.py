"""Prompts for the rules-driven node builder.

The rules themselves live in node_rules.md next to this file, so they can be
edited as a document. This module adds only how the pipeline's inputs map
onto them: what the listings are, how to reference them, and what Step 1
has already been done for the model.
"""
import json
from pathlib import Path

NODE_RULES = (Path(__file__).parent / 'node_rules.md').read_text(encoding='utf-8')

NODE_BUILD_SYSTEM_PROMPT = NODE_RULES + """

---

## HOW THIS PIPELINE APPLIES THE RULES

Step 1 of the rules has already been done for you. You are given, for one
P&ID sheet:
- the sheet image;
- the equipment data blocks (tag, service, design P/T, MOC, capacity);
- the full entity inventory, each item with a stable id. Items marked
  [REVISED] are printed in red or inside a revision cloud: the new or
  modified scope (R6), found from the drawing's own colours;
- the PROCESSES a previous step identified: small, single-function runs of
  entities, each with an id;
- the transcribed C&E / interlock table, grouped by mode, with each row's
  final elements;
- facts computed exactly: control loops, relief devices and the equipment
  they protect, off-page connectors, duplicate-train candidates;
- consistency findings already computed from the C&E table (V3). Do not
  repeat these; add to "consistencyFindings" only what they miss, especially
  copy-paste errors between duplicate trains, and new scope with no
  protection.

## Building a node

Start from Step 0: set "sheetPatterns", then make one node per operation the
sheet manages (O1-O4). The PROCESSES listed are small building blocks, not
nodes: a typical node gathers several of them (a tank with its filling
headers, vents and sump; a filter-and-pump transfer train with its A/B spare).

A node is defined ONLY by ids. Put every process that lies inside it in
"processIds". Put in "entityIds" any item that belongs to the node but is in
no process, or that you are moving out of a process that straddles your
boundary. Never invent an id; use ids exactly as listed. Geometry is derived
from these ids afterwards, so never give coordinates.

Every equipment, valve, line and connector item should end up in exactly one
node, or in "outOfScope" (O5) if it only passes across the sheet. The inlets
and outlets of an operation are never out of scope: they go in its node (O2).
The only item that may sit in two nodes is the valve, flange or nozzle you
name as the boundary between them.

"interlockRefs" use the C&E row refs exactly as listed. Assign each row to
the node where its FINAL ACTION happens (G2), once.

For a duplicate train (D1, D2), still give the typical node its own ids so
it is marked on the drawing; set "typicalOf" to the full node's localId and
list only the differences. Shared headers and cross-overs are never typical
(D3).

"operatingModes" lists only modes named in the C&E table. A node that spans
two of them, or carries more than ~8-10 interlocks, must be split (Step 5).
Duplicate trains are the usual way to keep nodes a reviewable size: study
one train in full and declare the other typical.

The computed facts are exact about what is drawn, but a loop found by tag
number can still be wrong. If one looks wrong, say so in "notes" rather than
distorting the nodes to fit it.

Write "UNVERIFIED" for any value the drawing does not show legibly, and list
it in "unverified". Do not produce a reduced option here; a separate step
does that on request.
"""

REPAIR_INSTRUCTIONS = """Your previous node breakdown for this sheet failed the validation checks listed
below. Return the COMPLETE corrected breakdown, every node, not only the
changed ones. Fix every "error". Fix a "warning" where the rules allow; where
the drawing itself is at fault (a real protection gap, a C&E inconsistency),
keep the node and report it in "consistencyFindings" instead."""


def _attrs(e, limit=None):
    items = e.get('attributes') or []
    return ', '.join(f"{a['key']}={a['value']}" for a in (items[:limit] if limit else items))


def format_equipment_data(entities):
    rows = [e for e in entities if e['category'] in ('equipment', 'equipment_list_row')]
    if not rows:
        return '(no equipment data found)'
    return '\n'.join(f"{e['id']} [{e['category']}] \"{e.get('tag') or 'untagged'}\" — {e['type']} ({_attrs(e) or 'no data'})" for e in rows)


def format_inventory(entities, revised_ids):
    lines = []
    for e in entities:
        b = e['bbox']
        tag = f' "{e["tag"]}"' if e.get('tag') else ''
        attrs = _attrs(e, 4)
        conn = f" connectsTo=[{','.join(e['connectsTo'])}]" if e.get('connectsTo') else ''
        revised = ' [REVISED]' if e['id'] in revised_ids else ''
        lines.append(f"{e['id']} [{e['category']}]{tag}{revised} @{b['x'] + b['width'] / 2:.3f},{b['y'] + b['height'] / 2:.3f} — {e['type']}"
                     f"{' (' + attrs + ')' if attrs else ''}{conn}")
    return '\n'.join(lines) or '(no entities)'


def format_processes(processes, entity_by_id):
    def tags(ids):
        named = [entity_by_id[i].get('tag') for i in ids if i in entity_by_id and entity_by_id[i].get('tag')]
        return ', '.join(dict.fromkeys(named)) or 'none'

    return '\n'.join(
        f"{p['id']} [{p['category']}] \"{p['name']}\" — {p['description']}\n"
        f"    flow: {p.get('flowDirection') or '—'}; primary: {tags(p['primaryEquipmentIds'])}; tags: {tags(p['entityIds'])}; "
        f"{len(p['entityIds'])} entities"
        for p in processes
    ) or '(no processes)'


def format_interlocks(table):
    if not table['interlocks']:
        return '(no C&E / interlock table on this sheet)'
    lines = [f"Table: {table['title'] or 'untitled'}; modes: {', '.join(table['modes']) or 'none'}"]
    for r in table['interlocks']:
        flags = ''.join([' [REVISED]' if r['revised'] else '', ' [UNVERIFIED]' if r['unverified'] else ''])
        lines.append(f"{r['ref']} ({r['mode'] or 'no mode'}; {r['kind']}){flags}: {r['text']}  "
                     f"=> final elements: {', '.join(r['finalElementTags']) or '—'}")
    return '\n'.join(lines)


def format_facts(loops, reliefs, connectors_, trains, entity_by_id):
    def tag(i):
        e = entity_by_id.get(i) or {}
        return e.get('tag') or i

    parts = ['Control loops (keep whole, G1):']
    parts += [f"  {k}: {', '.join(tag(m) for m in v)}" for k, v in loops.items()] or ['  none found']
    parts.append('Relief devices and what they protect (G3, G4):')
    parts += [f"  {r['tag'] or r['id']} (set {r['setPressure'] or 'UNVERIFIED'}) protects {', '.join(tag(p) for p in r['protects']) or 'UNKNOWN'}" for r in reliefs] or ['  none found']
    parts.append('Off-page connectors / battery limits (R5):')
    parts += [f"  {c['id']} \"{c.get('tag') or ''}\" — {c['description']}" for c in connectors_] or ['  none found']
    parts.append('Duplicate-train candidates (D1-D4):')
    parts += [f"  {', '.join(t['tags'])}: {t['why']}" for t in trains] or ['  none found']
    return '\n'.join(parts)


def format_findings(findings):
    return '\n'.join(f"- [{f['severity']}] {f['check']}: {f['detail']}" for f in findings) or '(none)'


def format_previous(response):
    return json.dumps(response, ensure_ascii=False)
