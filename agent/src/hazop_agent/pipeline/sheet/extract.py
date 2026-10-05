"""Sheet data — the drawing-level facts the HAZOP node rules depend on that
step 1's tile inventory doesn't carry:

- the cause-and-effect / interlock table, transcribed row by row (Step 1
  item 5; used by G2 and V2);
- the new or modified scope, read from the red markup (Step 1 item 6; R6);
- the table-vs-drawing consistency checks that can be done exactly (V3).

Split like the other steps: `collect_page` is the paid half (the red-markup
scan plus one model call), `assemble_page` the free, pure half.
"""
import re

from ..jobs import now_iso
from ..llm import image_part, structured_call, text_part
from ..pdf import render_region, to_data_url
from ..step1.extract import normalize_tag
from .prompts import INTERLOCK_TABLE_SYSTEM_PROMPT, format_text_listing
from .revisions import red_text_ids, revised_scope, scan_page
from .schema import INTERLOCK_TABLE_SCHEMA

# Tags an interlock checks by position (valves and their limit switches).
POSITION_TAG = re.compile(r'^(XV|HV|TV|SDV|ESDV|MOV|ZS|ZSO|ZSC)', re.I)
# Tag prefixes of automated valves, for the "on the drawing but not in the table" check.
AUTOMATED_VALVE_TAG = re.compile(r'^(XV|TV|SDV|ESDV|MOV|FV|LV|PV)', re.I)
AUTOMATED_ACTUATORS = {'diaphragm', 'piston', 'motor', 'solenoid', 'pneumatic', 'electric'}
# Text runs longer than this are sentences (notes, table rows), not tags drawn on the sheet.
MAX_TAG_TEXT = 24


async def extract_sheet_document(doc, step1_result, options=None):
    options = options or {}
    collected = []
    for step1_page in step1_result['pages']:
        collected.append(await collect_page(doc[step1_page['pageIndex']], step1_page, options))
    return {**assemble_document(collected, step1_result['fileName'], step1_result), 'raw': collected}


async def collect_page(page, step1_page, opts=None):
    opts = opts or {}
    on_progress = opts.get('onProgress') or (lambda event: None)
    page_px = opts.get('pagePx', 2400)
    page_index = step1_page['pageIndex']

    marks = scan_page(page, page_index)
    red_ids = red_text_ids(marks, step1_page['textItems'])
    on_progress({'phase': 'page-start', 'pageIndex': page_index, 'redSegments': len(marks['segments']), 'redText': len(marks['texts'])})

    png = render_region(page, {'x': 0, 'y': 0, 'width': 1, 'height': 1}, target_px=page_px)['png']
    try:
        parsed = await structured_call(
            system=INTERLOCK_TABLE_SYSTEM_PROMPT,
            schema=INTERLOCK_TABLE_SCHEMA,
            schema_name='pid_interlock_table',
            content=[
                text_part(f'Full P&ID sheet, page index {page_index}.'),
                image_part(to_data_url(png)),
                text_part('PDF text layer (positions are page fractions, origin top-left; "(red)" = printed in red):\n'
                          + format_text_listing(step1_page['textItems'], red_ids)),
            ],
        )
        error = None
        on_progress({'phase': 'page-done', 'pageIndex': page_index, 'interlocks': len(parsed.get('interlocks') or [])})
    except Exception as err:  # noqa: BLE001 — a failed page is recorded, not fatal
        parsed, error = {'tablePresent': False, 'tableTitle': '', 'modes': [], 'interlocks': [], 'notes': ''}, str(err)
        on_progress({'phase': 'page-failed', 'pageIndex': page_index, 'error': error})

    return {'pageIndex': page_index, 'marks': marks, 'raw': parsed, 'error': error}


# ─────────────────────────────────────────────────────────────── free half

def assemble_document(collected, file_name, step1_result):
    step1_by_page = {p['pageIndex']: p for p in step1_result['pages']}
    pages = [assemble_page(c, step1_by_page[c['pageIndex']]) for c in collected]
    return {
        'schemaVersion': 1,
        'step': 'step1b-sheet',
        'fileName': file_name,
        'generatedAt': now_iso(),
        'pages': pages,
        'totals': {
            'interlocks': sum(len(p['interlockTable']['interlocks']) for p in pages),
            'revisedEntities': sum(len(p['revisedScope']['entities']) for p in pages),
            'findings': sum(len(p['findings']) for p in pages),
        },
    }


def _tag_index(entities, text_items):
    by_tag = {}
    for e in entities:
        key = normalize_tag(e.get('tag'))
        if key:
            by_tag.setdefault(key, []).append(e['id'])
    drawn_text = {normalize_tag(t['str']) for t in text_items if len(t['str']) <= MAX_TAG_TEXT}
    return by_tag, drawn_text


def _row_tags(row):
    tags = [*row['causeTags'], *row['finalElementTags'], *(s['tag'] for s in row['requiredStates'])]
    return list(dict.fromkeys(t.strip() for t in tags if t and t.strip()))


def assemble_page(collected, step1_page):
    page_index = collected['pageIndex']
    raw = collected['raw']
    entities = step1_page['entities']
    by_tag, drawn_text = _tag_index(entities, step1_page['textItems'])
    scope = revised_scope(collected['marks'], entities, step1_page['textItems'])
    red_ids = set(scope['redTextItemIds'])
    red_strings = {normalize_tag(t['str']) for t in step1_page['textItems'] if t['id'] in red_ids}

    interlocks = []
    for i, row in enumerate(raw.get('interlocks') or []):
        resolved, unresolved = {}, []
        for tag in _row_tags(row):
            ids = by_tag.get(normalize_tag(tag))
            if ids:
                resolved[tag] = ids
            elif normalize_tag(tag) not in drawn_text:
                unresolved.append(tag)
        interlocks.append({
            'id': f'p{page_index}-il-{i + 1}',
            **row,
            # The printed colour is ground truth; the model's reading of it is a fallback.
            'revised': row['markedRevised'] or normalize_tag(row['ref']) in red_strings,
            'entityIds': list(dict.fromkeys(eid for ids in resolved.values() for eid in ids)),
            'resolvedTags': resolved,
            'tagsNotOnDrawing': unresolved,
        })

    findings = consistency_findings(interlocks, entities, scope)
    return {
        'pageIndex': page_index,
        'interlockTable': {
            'tablePresent': raw.get('tablePresent', False),
            'title': raw.get('tableTitle', ''),
            'modes': raw.get('modes') or [],
            'notes': raw.get('notes', ''),
            'interlocks': interlocks,
        },
        'revisedScope': scope,
        'findings': findings,
        'diagnostics': {'modelError': collected.get('error')},
    }


def is_automated_valve(e):
    attrs = {a['key']: a['value'].lower() for a in e.get('attributes') or []}
    return e['category'] == 'valve' and bool(
        AUTOMATED_VALVE_TAG.match(e.get('tag') or '') or attrs.get('actuator') in AUTOMATED_ACTUATORS)


def _finding(check, severity, detail, refs=(), tags=()):
    return {'check': check, 'severity': severity, 'interlockRefs': list(refs), 'tags': list(tags), 'detail': detail}


def consistency_findings(interlocks, entities, scope):
    """Rule V3, the parts that can be checked exactly. The model is asked for
    the judgement calls (copy-paste between trains) in the node step."""
    out = []

    for row in interlocks:
        if row['tagsNotOnDrawing']:
            out.append(_finding('tag-not-on-drawing', 'warning',
                                f"Row {row['ref']} names {', '.join(row['tagsNotOnDrawing'])}, not found on the drawing.",
                                [row['ref']], row['tagsNotOnDrawing']))
        states = {}
        for s in row['requiredStates']:
            states.setdefault(normalize_tag(s['tag']), (s['tag'], set()))[1].add(s['state'])
        for tag, found in states.values():
            if {'open', 'closed'} <= found:
                out.append(_finding('open-and-closed', 'error',
                                    f"Row {row['ref']} requires {tag} to be both OPEN and CLOSED.", [row['ref']], [tag]))
        if row['unverified']:
            out.append(_finding('unverified', 'warning', f"Row {row['ref']}: {row['unverifiedReason'] or 'illegible'} (UNVERIFIED).", [row['ref']]))

    # A permissive and the trip that protects the same running device should
    # check the same valves: the permissive's open/in-line set against the
    # trip's "not in desired position" set.
    for permissive in (r for r in interlocks if r['kind'] == 'permissive'):
        required = {normalize_tag(s['tag']): s['tag'] for s in permissive['requiredStates']
                    if s['state'] in ('open', 'inline') and POSITION_TAG.match(s['tag'])}
        targets = {normalize_tag(t) for t in permissive['finalElementTags']}
        for trip in interlocks:
            if trip['kind'] != 'trip' or not targets & {normalize_tag(t) for t in trip['finalElementTags']}:
                continue
            checked = {normalize_tag(t): t for t in trip['causeTags'] if POSITION_TAG.match(t)}
            if len(checked) < 2 or not required:
                continue  # a single-sensor trip (pressure, level) isn't a position check
            only_permissive = sorted(required[k] for k in required.keys() - checked.keys())
            only_trip = sorted(checked[k] for k in checked.keys() - required.keys())
            if only_permissive or only_trip:
                parts = []
                if only_permissive:
                    parts.append(f"permissive only: {', '.join(only_permissive)}")
                if only_trip:
                    parts.append(f"trip only: {', '.join(only_trip)}")
                out.append(_finding('permissive-trip-mismatch', 'error',
                                    f"Permissive {permissive['ref']} and trip {trip['ref']} check different valves ({'; '.join(parts)}).",
                                    [permissive['ref'], trip['ref']], only_permissive + only_trip))

    in_table = {eid for row in interlocks for eid in row['entityIds']}
    if interlocks:
        for e in entities:
            if is_automated_valve(e) and e['id'] not in in_table:
                out.append(_finding('tag-not-in-table', 'info', f"Automated valve {e.get('tag') or e['id']} is in no C&E row.", tags=[e.get('tag') or e['id']]))

    by_id = {e['id']: e for e in entities}
    unprotected = [r for r in scope['entities'] if r['entityId'] not in in_table
                   and (by_id[r['entityId']]['category'] == 'equipment' or is_automated_valve(by_id[r['entityId']]))]
    if unprotected:
        tags = [r['tag'] or r['entityId'] for r in unprotected]
        out.append(_finding('unprotected-new-scope', 'warning',
                            f"New/modified equipment and valves with no C&E row: {', '.join(tags)}. Confirm what protects them.",
                            tags=tags))
    return out
