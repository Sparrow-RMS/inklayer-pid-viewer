"""Step 3 — HAZOP nodes by the node-creation rules (node_rules.md).

One whole-sheet model call per page proposes the node breakdown with every
Step 6 field, from everything earlier steps established: the entity
inventory, step 2's processes, the C&E table, the red (revised) scope, and
facts computed exactly in `rules.py`. The breakdown is then checked by code
against Step 7 (`rules.validate`). If any check fails with an error, the
model gets one repair pass with the findings.

A node carries no coordinates of its own: its bbox and routes are the union
of its entities' real geometry, as in step 2.

`collect_page` is the paid half; `assemble_page` the free, pure half, which
rebuilds nodes and the validation report from the cached responses.
"""
from ..geometry import clamp_bbox, union_bboxes
from ..jobs import now_iso
from ..llm import image_part, structured_call, text_part
from ..pdf import render_region, to_data_url
from ..step1.extract import normalize_tag
from . import rules
from .build_prompts import (NODE_BUILD_SYSTEM_PROMPT, REPAIR_INSTRUCTIONS, format_equipment_data, format_facts,
                            format_findings, format_interlocks, format_inventory, format_previous, format_processes)
from .build_schema import NODE_BUILD_SCHEMA


async def build_document(doc, step2_result, step1_result, sheet_result, options=None):
    options = options or {}
    step1_by_page = {p['pageIndex']: p for p in step1_result['pages']}
    sheet_by_page = {p['pageIndex']: p for p in sheet_result['pages']}
    collected = []
    for page in step2_result['pages']:
        i = page['pageIndex']
        collected.append(await collect_page(doc[i], page, step1_by_page[i], sheet_by_page[i], options))
    return {**assemble_document(collected, step2_result['fileName'], step1_result, sheet_result), 'raw': collected}


def _context(step1_page, sheet_page):
    entities = step1_page['entities']
    return {
        'entities': entities,
        'entity_by_id': {e['id']: e for e in entities},
        'interlocks': sheet_page['interlockTable']['interlocks'],
        'revised_ids': {r['entityId'] for r in sheet_page['revisedScope']['entities']},
        'modes': sheet_page['interlockTable']['modes'],
    }


def _inputs(page, step1_page, sheet_page, ctx):
    entities, by_id = ctx['entities'], ctx['entity_by_id']
    return [
        text_part(f"P&ID page index {page['pageIndex']}. {step1_page.get('drawingMeta', {}).get('overallDescription', '')}"),
        text_part('Equipment data blocks:\n' + format_equipment_data(entities)),
        text_part('Processes from the previous step (id [category] "name"):\n' + format_processes(page['processes'], by_id)),
        text_part('C&E / interlock table:\n' + format_interlocks(sheet_page['interlockTable'])),
        text_part('Computed facts:\n' + format_facts(rules.control_loops(entities), rules.relief_devices(entities),
                                                   rules.connectors(entities), rules.duplicate_trains(entities), by_id)),
        text_part('C&E consistency findings already computed (V3):\n' + format_findings(sheet_page['findings'])),
        text_part('Entity inventory (id [category] "tag" @x,y — type (attributes) connectsTo=[...]):\n'
                  + format_inventory(entities, ctx['revised_ids'])),
    ]


async def collect_page(pdf_page, page, step1_page, sheet_page, opts=None):
    opts = opts or {}
    on_progress = opts.get('onProgress') or (lambda event: None)
    page_px = opts.get('pagePx', 2400)
    page_index = page['pageIndex']
    ctx = _context(step1_page, sheet_page)
    on_progress({'phase': 'page-start', 'pageIndex': page_index, 'processes': len(page['processes']),
                 'interlocks': len(ctx['interlocks'])})

    png = render_region(pdf_page, {'x': 0, 'y': 0, 'width': 1, 'height': 1}, target_px=page_px)['png']
    inputs = [*_inputs(page, step1_page, sheet_page, ctx), image_part(to_data_url(png))]
    responses, error, initial_findings = [], None, []

    async def call(extra):
        return await structured_call(system=NODE_BUILD_SYSTEM_PROMPT, schema=NODE_BUILD_SCHEMA,
                                     schema_name='pid_hazop_nodes', content=[*inputs, *extra])

    try:
        responses.append(await call([]))
        initial_findings = _check(responses[0], page, ctx)
        errors = [f for f in initial_findings if f['severity'] == 'error']
        on_progress({'phase': 'nodes-proposed', 'pageIndex': page_index, 'nodes': len(responses[0]['nodes']), 'errors': len(errors)})
        if errors and opts.get('repair', True):
            responses.append(await call([
                text_part(REPAIR_INSTRUCTIONS),
                text_part('Validation findings:\n' + format_findings(initial_findings)),
                text_part('Your previous breakdown:\n' + format_previous(responses[0])),
            ]))
            on_progress({'phase': 'nodes-repaired', 'pageIndex': page_index, 'nodes': len(responses[-1]['nodes'])})
    except Exception as err:  # noqa: BLE001 — a failed page is recorded, not fatal
        error = str(err)
        on_progress({'phase': 'page-failed', 'pageIndex': page_index, 'error': error})

    return {'pageIndex': page_index, 'processes': page['processes'], 'responses': responses,
            'initialFindings': initial_findings, 'error': error}


# ─────────────────────────────────────────────────────────────── free half

def _expand(response, processes, ctx):
    """Model nodes -> nodes with their full entity sets, dropping ids that don't exist."""
    process_by_id = {p['id']: p for p in processes}
    by_id = ctx['entity_by_id']
    invalid_processes, invalid_entities, out = [], [], []
    for n in response.get('nodes') or []:
        members = [process_by_id[p] for p in n['processIds'] if p in process_by_id]
        invalid_processes += [p for p in n['processIds'] if p not in process_by_id]
        invalid_entities += [e for e in n['entityIds'] if e not in by_id]
        entity_ids = list(dict.fromkeys([*(e for p in members for e in p['entityIds']), *(e for e in n['entityIds'] if e in by_id)]))
        out.append({**n, 'members': members, 'entityIds': entity_ids})
    return out, invalid_processes, invalid_entities


def _out_of_scope(response, ctx):
    return [{'entityIds': [e for e in o['entityIds'] if e in ctx['entity_by_id']], 'reason': o['reason']}
            for o in response.get('outOfScope') or []]


def _check(response, page, ctx):
    nodes, _, _ = _expand(response, page['processes'], ctx)
    excluded = {e for o in _out_of_scope(response, ctx) for e in o['entityIds']}
    return rules.validate(nodes, ctx['entities'], ctx['interlocks'], ctx['revised_ids'], ctx['modes'], excluded)


def assemble_document(collected, file_name, step1_result, sheet_result):
    step1_by_page = {p['pageIndex']: p for p in step1_result['pages']}
    sheet_by_page = {p['pageIndex']: p for p in sheet_result['pages']}
    pages = [assemble_page(c, step1_by_page[c['pageIndex']], sheet_by_page[c['pageIndex']]) for c in collected]
    return {
        'schemaVersion': 2,
        'step': 'step3-nodes',
        'rules': 'node_rules.md',
        'fileName': file_name,
        'generatedAt': now_iso(),
        'pages': pages,
        'totals': {
            'nodes': sum(len(p['nodes']) for p in pages),
            'processesClustered': sum(len(p['processes']) for p in pages),
            'errors': sum(p['validation']['counts']['error'] for p in pages),
            'warnings': sum(p['validation']['counts']['warning'] for p in pages),
        },
    }


def assemble_page(collected, step1_page, sheet_page):
    page_index = collected['pageIndex']
    ctx = _context(step1_page, sheet_page)
    by_id = ctx['entity_by_id']
    response = collected['responses'][-1] if collected['responses'] else {'nodes': [], 'consistencyFindings': [], 'notes': ''}
    expanded, invalid_processes, invalid_entities = _expand(response, collected['processes'], ctx)
    out_of_scope = _out_of_scope(response, ctx)

    kept, dropped = [], []
    for n in expanded:
        (kept if n['entityIds'] else dropped).append(n)
    id_of = {n['localId']: f'p{page_index}-node-{i + 1}' for i, n in enumerate(kept)}
    name_of = {n['localId']: n['name'] for n in kept}
    tag_index = {}
    for e in ctx['entities']:
        if e.get('tag'):
            tag_index.setdefault(normalize_tag(e['tag']), e['id'])

    nodes = []
    for n in kept:
        members = [by_id[e] for e in n['entityIds']]
        primary = list(dict.fromkeys([*(e for p in n['members'] for e in p['primaryEquipmentIds']),
                                      *(tag_index[normalize_tag(t)] for t in n['equipmentTags'] if normalize_tag(t) in tag_index)]))
        nodes.append({
            'id': id_of[n['localId']],
            'page': page_index,
            'label': f"{n['localId']} {n['name']}",
            'description': n['designIntent'],
            'categories': list(dict.fromkeys(p['category'] for p in n['members'])) or ['other'],
            'processIds': [p['id'] for p in n['members']],
            'memberCount': len(n['members']),
            'entityIds': n['entityIds'],
            'primaryEquipmentIds': primary,
            'bbox': clamp_bbox(union_bboxes(e['bbox'] for e in members)),
            'routes': [{'entityId': e['id'], 'points': pts} for e in members if e['category'] == 'line' and e.get('routes') for pts in e['routes']],
            'hazop': {
                'localId': n['localId'],
                'name': n['name'],
                'boundaryFrom': n['boundaryFrom'],
                'boundaryTo': n['boundaryTo'],
                'equipmentTags': n['equipmentTags'],
                'designIntent': n['designIntent'],
                'designConditions': n['designConditions'],
                'controlLoops': n['controlLoops'],
                'interlockRefs': n['interlockRefs'],
                'reliefDevices': n['reliefDevices'],
                'protection': n['protection'],
                'keyDeviations': n['keyDeviations'],
                'operatingModes': n['operatingModes'],
                'utilitiesAsSafeguards': n['utilitiesAsSafeguards'],
                'typicalOf': id_of.get(n['typicalOf']) if n['typicalOf'] else None,
                'typicalOfName': name_of.get(n['typicalOf']) if n['typicalOf'] else None,
                'differencesFromTypical': n['differencesFromTypical'],
                'revisedScope': n['revisedScope'],
                'justification': n['justification'],
                'unverified': n['unverified'],
            },
        })

    findings = [
        *({**f, 'source': 'c&e-table'} for f in sheet_page['findings']),
        *({'check': f"V3-{f['check']}", 'severity': 'warning', 'detail': f['detail'], 'nodeIds': [],
           'entityIds': [], 'interlockRefs': f['interlockRefs'], 'source': 'model'} for f in response.get('consistencyFindings') or []),
        *({**f, 'source': 'rules'} for f in rules.validate(kept, ctx['entities'], ctx['interlocks'], ctx['revised_ids'], ctx['modes'],
                                                              {e for o in out_of_scope for e in o['entityIds']})),
    ]
    for f in findings:
        f['nodeIds'] = [id_of.get(x, x) for x in f.get('nodeIds') or []]
    counts = {s: sum(1 for f in findings if f['severity'] == s) for s in ('error', 'warning', 'info')}

    return {
        'pageIndex': page_index,
        'processes': collected['processes'],
        'sheetPatterns': response.get('sheetPatterns') or [],
        'nodes': nodes,
        'outOfScope': out_of_scope,
        'validation': {'counts': counts, 'findings': findings},
        'diagnostics': {
            'repaired': len(collected['responses']) > 1,
            'initialErrors': sum(1 for f in collected.get('initialFindings') or [] if f['severity'] == 'error'),
            'invalidProcessIds': invalid_processes,
            'invalidEntityIds': invalid_entities,
            'droppedEmptyNodes': [n['localId'] for n in dropped],
            'modelError': collected.get('error'),
            'notes': response.get('notes', ''),
        },
    }
