"""Step 3 — cluster processes into HAZOP nodes.

The model is only ever asked "do processes A and B belong in the same node?"
for a bounded set of candidate pairs (`candidates.py`, no model call). The
actual grouping is arithmetic: "yes" verdicts are unioned with the same
DisjointSet step 1 uses to merge duplicate tile sightings, so the transitive
closure (A~B, B~C => A~B~C) is guaranteed rather than re-derived by the model.

A node carries no coordinates of its own: bbox and routes are the union of its
member processes' already-real geometry.
"""
from ..dsu import DisjointSet
from ..geometry import clamp_bbox, sort_key_top_left, union_bboxes
from ..jobs import now_iso
from ..llm import map_limit, structured_call, text_part
from .candidates import generate_candidate_pairs
from .prompts import CLUSTER_SYSTEM_PROMPT, format_candidate_pairs
from .schema import PAIR_VERDICT_SCHEMA

# Caps how many pairs go in one call's prompt — beyond this it's chunked, not truncated.
MAX_PAIRS_PER_CALL = 40
# A "yes" below this confidence doesn't get applied — recorded, not silently trusted.
SAME_NODE_CONFIDENCE_THRESHOLD = 0.5


def _num_or(v, fallback):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else fallback


def chunked(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


async def cluster_document(step2_result, step1_result, options=None):
    options = options or {}
    collected = []
    for page in step2_result['pages']:
        step1_page = next(p for p in step1_result['pages'] if p['pageIndex'] == page['pageIndex'])
        collected.append(await collect_page(page, step1_page, options))
    return {**assemble_document(collected, step2_result['fileName']), 'raw': collected}


async def collect_page(page, step1_page, opts=None):
    """The paid half: candidate generation is free; only the pairwise verdict
    call(s) touch the model."""
    opts = opts or {}
    on_progress = opts.get('onProgress') or (lambda event: None)
    max_pairs_per_call = opts.get('maxPairsPerCall', MAX_PAIRS_PER_CALL)
    concurrency = opts.get('concurrency', 3)
    page_index = page['pageIndex']
    entity_by_id = {e['id']: e for e in step1_page['entities']}
    process_by_id = {p['id']: p for p in page['processes']}

    pairs = generate_candidate_pairs(page['processes'], entity_by_id)
    on_progress({'phase': 'page-start', 'pageIndex': page_index, 'processes': len(page['processes']), 'candidatePairs': len(pairs)})

    if not pairs:
        return {'pageIndex': page_index, 'processes': page['processes'], 'pairs': pairs, 'verdicts': [], 'error': None}

    verdicts = []
    error = None

    async def worker(chunk, idx):
        nonlocal error
        try:
            parsed = await structured_call(
                system=CLUSTER_SYSTEM_PROMPT,
                schema=PAIR_VERDICT_SCHEMA,
                schema_name='pid_node_cluster_verdicts',
                content=[
                    text_part(f'Page index {page_index}. {len(chunk)} candidate process pairs follow.'),
                    text_part(format_candidate_pairs(chunk, process_by_id, entity_by_id)),
                ],
            )
            verdicts.extend(parsed.get('verdicts') or [])
            on_progress({'phase': 'chunk-done', 'pageIndex': page_index, 'chunk': idx, 'verdicts': len(parsed.get('verdicts') or [])})
        except Exception as err:  # noqa: BLE001 — a failed chunk leaves its pairs unanswered, i.e. separate
            error = str(err)
            on_progress({'phase': 'chunk-failed', 'pageIndex': page_index, 'chunk': idx, 'error': str(err)})

    await map_limit(chunked(pairs, max_pairs_per_call), concurrency, worker)
    return {'pageIndex': page_index, 'processes': page['processes'], 'pairs': pairs, 'verdicts': verdicts, 'error': error}


def assemble_document(collected, file_name):
    pages = [assemble_page(c) for c in collected]
    return {
        'schemaVersion': 1,
        'step': 'step3-cluster',
        'fileName': file_name,
        'generatedAt': now_iso(),
        'pages': pages,
        'totals': {
            'nodes': sum(len(p['nodes']) for p in pages),
            'processesClustered': sum(len(p['processes']) for p in pages),
        },
    }


def assemble_page(collected):
    """The free half: pairwise verdicts -> disjoint-set union -> final nodes
    with derived geometry. Pure."""
    page_index = collected['pageIndex']
    processes = collected['processes']
    pairs = collected.get('pairs') or []
    verdicts = collected.get('verdicts') or []
    process_by_id = {p['id']: p for p in processes}
    pair_by_id = {p['id']: p for p in pairs}

    dsu = DisjointSet()
    for p in processes:
        dsu.add(p['id'])

    applied_verdicts = []
    invalid_pair_ids = []
    seen_pair_ids = set()

    for v in verdicts:
        pair = pair_by_id.get(v.get('pairId'))
        if not pair:
            invalid_pair_ids.append(v.get('pairId'))
            continue
        seen_pair_ids.add(v['pairId'])
        confidence = _num_or(v.get('confidence'), 0.5)
        applied = v.get('sameNode') is True and confidence >= SAME_NODE_CONFIDENCE_THRESHOLD
        applied_verdicts.append({
            'pairId': v['pairId'], 'aId': pair['aId'], 'bId': pair['bId'], 'sameNode': v.get('sameNode'),
            'confidence': confidence, 'reasoning': v.get('reasoning'), 'applied': applied,
        })
        if applied:
            dsu.union(pair['aId'], pair['bId'])

    missing_verdicts = [p['id'] for p in pairs if p['id'] not in seen_pair_ids]

    built = sorted((_build_node(members, process_by_id) for members in dsu.groups()), key=sort_key_top_left)
    nodes = [{'id': f'p{page_index}-node-{i + 1}', 'page': page_index, **node} for i, node in enumerate(built)]

    return {
        'pageIndex': page_index,
        'processes': processes,
        'nodes': nodes,
        'diagnostics': {
            'candidatePairs': len(pairs),
            'verdictsReceived': len(verdicts),
            'verdictsApplied': sum(1 for v in applied_verdicts if v['applied']),
            'invalidPairIds': invalid_pair_ids,
            'missingVerdicts': missing_verdicts,
            'modelError': collected.get('error'),
            # Full audit trail — which pairs merged, on what reasoning, and which
            # "yes" verdicts were withheld for low confidence. What a reviewer
            # checks first if a node looks wrong.
            'appliedVerdicts': applied_verdicts,
        },
    }


def dedupe_routes(members):
    """A boundary entity's line can legitimately appear in two merged members'
    route lists — deduplicated by entityId so the same pipe run isn't drawn twice."""
    routes_by_entity = {}
    for m in members:
        for r in m['routes']:
            routes_by_entity.setdefault(r['entityId'], r)
    return list(routes_by_entity.values())


def _build_node(member_process_ids, process_by_id):
    members = [process_by_id[pid] for pid in member_process_ids]
    # Deterministic composition for v1 — good enough to review and correct, not styled prose.
    return {
        'label': members[0]['name'] if len(members) == 1 else ' + '.join(p['name'] for p in members),
        'description': ' '.join(p['description'] for p in members),
        'categories': list(dict.fromkeys(p['category'] for p in members)),
        'processIds': member_process_ids,
        'memberCount': len(members),
        'entityIds': list(dict.fromkeys(eid for p in members for eid in p['entityIds'])),
        'primaryEquipmentIds': list(dict.fromkeys(eid for p in members for eid in p['primaryEquipmentIds'])),
        'bbox': clamp_bbox(union_bboxes(p['bbox'] for p in members)),
        'routes': dedupe_routes(members),
    }
