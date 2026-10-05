"""Step 2 — process classification.

Groups step 1's entity inventory into named processes. A process carries NO
coordinates of its own: it references entity ids, and its geometry — bbox and
member routes — is derived here as the union of those entities' real,
already-anchored geometry. That keeps the eventual annotation exactly as
accurate as step 1 made it, however many stages sit in between.

Split the same way as step 1: `collect_page` is the paid half (one model call
per page); `assemble_page` is the free, pure half (id validation, geometry
derivation, coverage diagnostics).
"""
from ..geometry import clamp_bbox, union_bboxes
from ..jobs import now_iso
from ..llm import image_part, structured_call, text_part
from ..pdf import render_region, to_data_url
from .prompts import PROCESS_SYSTEM_PROMPT, format_entity_inventory
from .schema import PAGE_PROCESS_SCHEMA

# These categories describe or annotate other entities rather than doing
# anything themselves, so they add noise without helping the model group
# process functions — excluded from what it's shown, and from the coverage
# count (an unclassified note is not a gap; an unclassified pump is).
#
# Exception: a step 1 entity synthesized from unclaimed tag-shaped text is
# stored as category "label" purely because step 1 had nowhere else to put
# it — exactly the kind of real-but-uncertain item this stage must not drop.
NON_FUNCTIONAL_CATEGORIES = {'note', 'label', 'equipment_list_row'}

# Above this fraction of shared entityIds, two processes are probably
# describing the same thing rather than two related-but-distinct functions —
# surfaced as a hint for review, not auto-merged (that is step 3's job).
HIGH_OVERLAP_JACCARD = 0.6


def is_functional(e):
    return e['category'] not in NON_FUNCTIONAL_CATEGORIES or (e.get('flags') or {}).get('synthesizedFromText') is True


def _num_or(v, fallback):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else fallback


async def classify_document(doc, step1_result, options=None):
    options = options or {}
    collected = []
    for step1_page in step1_result['pages']:
        page = doc[step1_page['pageIndex']]
        collected.append(await collect_page(page, step1_page, options))
    return {**assemble_document(collected, step1_result['fileName']), 'raw': collected}


async def collect_page(page, step1_page, opts):
    """The paid half: one structured-output call per page."""
    page_px = opts.get('pagePx', 2400)
    on_progress = opts.get('onProgress') or (lambda event: None)
    page_index = step1_page['pageIndex']

    classifiable = [e for e in step1_page['entities'] if is_functional(e)]
    png = render_region(page, {'x': 0, 'y': 0, 'width': 1, 'height': 1}, target_px=page_px)['png']
    listing = format_entity_inventory(classifiable)

    on_progress({'phase': 'page-start', 'pageIndex': page_index, 'classifiableEntities': len(classifiable)})

    try:
        parsed = await structured_call(
            system=PROCESS_SYSTEM_PROMPT,
            schema=PAGE_PROCESS_SCHEMA,
            schema_name='pid_page_processes',
            content=[
                text_part(f'P&ID page index {page_index}. {len(classifiable)} entities to classify follow, each with its id.'),
                image_part(to_data_url(png)),
                text_part(f'Entity inventory (id [category] "tag" @x,y — type (attributes) connectsTo=[...]):\n{listing}'),
            ],
        )
        on_progress({'phase': 'page-done', 'pageIndex': page_index, 'processes': len(parsed['processes'])})
    except Exception as err:  # noqa: BLE001 — a failed page is recorded, not fatal
        on_progress({'phase': 'page-failed', 'pageIndex': page_index, 'error': str(err)})
        parsed = {'processes': [], 'pageNotes': '', 'error': str(err)}

    return {'pageIndex': page_index, 'step1Page': step1_page, 'classifiable': classifiable, 'raw': parsed}


def assemble_document(collected, file_name):
    """Rebuilds the whole step 2 artifact from cached per-page collections. Pure."""
    pages = [assemble_page(c) for c in collected]
    return {
        'schemaVersion': 1,
        'step': 'step2-classify',
        'fileName': file_name,
        'generatedAt': now_iso(),
        'pages': pages,
        'totals': {
            'processes': sum(len(p['processes']) for p in pages),
            'coveredEntities': sum(len(p['diagnostics']['coveredEntityIds']) for p in pages),
            'uncoveredEntities': sum(len(p['diagnostics']['uncoveredEntityIds']) for p in pages),
        },
    }


def _filter_known_ids(ids, by_id):
    """Observed on a real dense sheet: the model repeats an id within one
    process's own list ("TV-1257004, TV-1257004") — harmless to the bbox
    union, but it inflates counts, so ids are deduplicated, order preserved."""
    valid, invalid, seen = [], [], set()
    for eid in ids or []:
        if eid in seen:
            continue
        seen.add(eid)
        (valid if eid in by_id else invalid).append(eid)
    return valid, invalid


def assemble_page(collected):
    """The free half: raw model output -> validated processes with derived geometry. Pure."""
    page_index = collected['pageIndex']
    step1_page = collected['step1Page']
    classifiable = collected['classifiable']
    raw = collected['raw']
    entity_by_id = {e['id']: e for e in step1_page['entities']}

    processes = []
    seen_local_ids = set()
    n = 0

    for p in raw.get('processes') or []:
        entity_ids, invalid_entity_ids = _filter_known_ids(p.get('entityIds'), entity_by_id)
        if not entity_ids:
            continue  # nothing real to anchor this process — drop it, not a phantom process

        n += 1
        primary_equipment_ids, _ = _filter_known_ids(p.get('primaryEquipmentIds'), entity_by_id)
        inlet_entity_ids, _ = _filter_known_ids(p.get('inletEntityIds'), entity_by_id)
        outlet_entity_ids, _ = _filter_known_ids(p.get('outletEntityIds'), entity_by_id)

        members = [entity_by_id[eid] for eid in entity_ids]
        bbox = clamp_bbox(union_bboxes(e['bbox'] for e in members))
        routes = [
            {'entityId': e['id'], 'points': points}
            for e in members
            if e['category'] == 'line' and e.get('routes')
            for points in e['routes']
        ]

        by_category = {}
        for e in members:
            by_category[e['category']] = by_category.get(e['category'], 0) + 1

        confidence = _num_or(p.get('confidence'), 0.5)
        processes.append({
            'id': f'p{page_index}-proc-{n}',
            'page': page_index,
            'name': p.get('name'),
            'category': p.get('category'),
            'description': p.get('description'),
            'entityIds': entity_ids,
            'primaryEquipmentIds': primary_equipment_ids or entity_ids,
            'inletEntityIds': inlet_entity_ids,
            'outletEntityIds': outlet_entity_ids,
            'flowDirection': p.get('flowDirection') if p.get('flowDirection') is not None else '',
            'boundaryReasoning': p.get('boundaryReasoning') if p.get('boundaryReasoning') is not None else [],
            'bbox': bbox,
            'routes': routes,
            'entityCategoryCounts': by_category,
            'confidence': confidence,
            'flags': {
                'hallucinatedIds': invalid_entity_ids,
                'lowConfidence': confidence < 0.6,
                'duplicateLocalId': p.get('localId') in seen_local_ids,
            },
        })
        seen_local_ids.add(p.get('localId'))

    _flag_high_overlap_pairs(processes)

    covered_entity_ids = list(dict.fromkeys(eid for p in processes for eid in p['entityIds']))
    covered = set(covered_entity_ids)
    uncovered_entity_ids = [e['id'] for e in classifiable if e['id'] not in covered]

    return {
        'pageIndex': page_index,
        'processes': processes,
        'diagnostics': {
            'classifiableEntities': len(classifiable),
            'coveredEntityIds': covered_entity_ids,
            'uncoveredEntityIds': uncovered_entity_ids,
            'pageNotes': raw.get('pageNotes') if raw.get('pageNotes') is not None else '',
            'modelError': raw.get('error'),
        },
    }


def _flag_high_overlap_pairs(processes):
    """Marks pairs of processes that overlap heavily enough to likely be one
    thing described twice — a review hint, set on both processes involved."""
    for i in range(len(processes)):
        for j in range(i + 1, len(processes)):
            a = set(processes[i]['entityIds'])
            b = set(processes[j]['entityIds'])
            shared = len(a & b)
            if shared == 0:
                continue
            if shared / (len(a) + len(b) - shared) >= HIGH_OVERLAP_JACCARD:
                processes[i]['flags'].setdefault('highOverlapWith', []).append(processes[j]['id'])
                processes[j]['flags'].setdefault('highOverlapWith', []).append(processes[i]['id'])
