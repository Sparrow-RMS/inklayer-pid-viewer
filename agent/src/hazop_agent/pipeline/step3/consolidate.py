"""Step 3, consolidation pass — force the node count down to a target.

The base pass (`cluster.py`) answers "is there a real HAZOP boundary between A
and B?" per pair, deliberately biased toward "no", with no notion of a target
count. This pass scores EVERY pair's continuous relatedness, then greedily
merges the strongest remaining pair until the target is met — classic
agglomerative clustering. It only runs when the base pass left more nodes
than the target permits.

If reaching the target requires going below a sane relatedness floor, that
merge is still made (the target is honored), but it is flagged
`diagnostics.appliedMerges[].forced` rather than presented as natural.

This is the node rules' "reduced option" (Step 5): new/modified (revised)
scope is never merged with existing scope, even if that leaves the target
unmet (R6), and each applied merge is listed with its reason.
"""
import hashlib
import json

from ..dsu import DisjointSet
from ..geometry import clamp_bbox, sort_key_top_left, union_bboxes
from ..jobs import now_iso
from ..llm import map_limit, structured_call, text_part
from .cluster import chunked, dedupe_routes
from .prompts import build_consolidate_system_prompt, format_node_pairs
from .rules import normalize_hazop_fields
from .schema import RELATEDNESS_SCHEMA

DEFAULT_MAX_PAIRS_PER_CALL = 40
# Below this relatedness, a merge is only made because the target forces it.
DEFAULT_MIN_RELATEDNESS = 0.3


async def consolidate_document(step3_result, step1_result, options=None):
    options = options or {}
    collected = []
    for page in step3_result['pages']:
        step1_page = next(p for p in step1_result['pages'] if p['pageIndex'] == page['pageIndex'])
        entity_by_id = {e['id']: e for e in step1_page['entities']}
        collected.append(await collect_page(page, entity_by_id, options))
    return {**assemble_document(collected, step3_result['fileName'], options), 'raw': collected}


async def collect_page(page, entity_by_id, opts=None):
    """The paid half: scores every pair among the current nodes. Skipped
    entirely (no model call) if already at or under target."""
    opts = opts or {}
    target_max_nodes = opts.get('targetMaxNodes')
    max_pairs_per_call = opts.get('maxPairsPerCall', DEFAULT_MAX_PAIRS_PER_CALL)
    concurrency = opts.get('concurrency', 3)
    on_progress = opts.get('onProgress') or (lambda event: None)
    page_index = page['pageIndex']
    nodes = page['nodes']

    if not target_max_nodes or len(nodes) <= target_max_nodes:
        on_progress({'phase': 'consolidate-skip', 'pageIndex': page_index, 'nodes': len(nodes), 'targetMaxNodes': target_max_nodes})
        return {'pageIndex': page_index, 'nodes': nodes, 'pairs': [], 'scores': [], 'error': None}

    node_by_id = {n['id']: n for n in nodes}
    pairs = []
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            pairs.append({'id': f'k{len(pairs) + 1}', 'aId': nodes[i]['id'], 'bId': nodes[j]['id']})

    on_progress({'phase': 'consolidate-start', 'pageIndex': page_index, 'nodes': len(nodes), 'targetMaxNodes': target_max_nodes, 'pairs': len(pairs)})

    scores = []
    error = None
    system = build_consolidate_system_prompt(len(nodes), target_max_nodes)

    async def worker(chunk, idx):
        nonlocal error
        try:
            parsed = await structured_call(
                system=system,
                schema=RELATEDNESS_SCHEMA,
                schema_name='pid_node_relatedness',
                content=[
                    text_part(f'Page {page_index}. {len(nodes)} nodes currently, target {target_max_nodes}. {len(chunk)} pairs follow.'),
                    text_part(format_node_pairs(chunk, node_by_id, entity_by_id)),
                ],
            )
            scores.extend(parsed.get('scores') or [])
            on_progress({'phase': 'consolidate-chunk-done', 'pageIndex': page_index, 'chunk': idx, 'scores': len(parsed.get('scores') or [])})
        except Exception as err:  # noqa: BLE001
            error = str(err)
            on_progress({'phase': 'consolidate-chunk-failed', 'pageIndex': page_index, 'chunk': idx, 'error': str(err)})

    await map_limit(chunked(pairs, max_pairs_per_call), concurrency, worker)
    return {'pageIndex': page_index, 'nodes': nodes, 'pairs': pairs, 'scores': scores, 'error': error}


def nodes_signature(pages):
    """Identifies the step 3 breakdown a consolidation was built from. Node
    ids repeat across breakdowns (p0-node-1, ...), so it covers their content."""
    key = [(p['pageIndex'], n['id'], sorted(n['entityIds'])) for p in pages for n in p['nodes']]
    return hashlib.sha1(json.dumps(key).encode()).hexdigest()


def assemble_document(collected, file_name, options=None):
    options = options or {}
    target_max_nodes = options.get('targetMaxNodes')
    min_relatedness = options.get('minRelatedness', DEFAULT_MIN_RELATEDNESS)
    pages = [assemble_page(c, target_max_nodes, min_relatedness) for c in collected]
    return {
        'schemaVersion': 1,
        'step': 'step3-consolidate',
        'fileName': file_name,
        'targetMaxNodes': target_max_nodes,
        'sourceNodesSignature': nodes_signature(collected),
        'generatedAt': now_iso(),
        'pages': pages,
        'totals': {'nodes': sum(len(p['nodes']) for p in pages)},
    }


def assemble_page(collected, target_max_nodes, min_relatedness=DEFAULT_MIN_RELATEDNESS):
    """The free half: relatedness scores -> greedy strongest-first merging via
    DisjointSet, stopping the instant the target is met. Pure."""
    page_index = collected['pageIndex']
    nodes = collected['nodes']
    pairs = collected['pairs']
    scores = collected['scores']

    if not target_max_nodes or len(nodes) <= target_max_nodes:
        target_label = target_max_nodes if target_max_nodes is not None else '∞'
        return {
            'pageIndex': page_index,
            'nodes': nodes,
            'diagnostics': {'skipped': True, 'reason': f'already at or under target ({len(nodes)} <= {target_label})'},
        }

    pair_by_id = {p['id']: p for p in pairs}
    score_by_pair_id = {}
    invalid_pair_ids = []
    for s in scores:
        if s.get('pairId') not in pair_by_id:
            invalid_pair_ids.append(s.get('pairId'))
            continue
        r = s.get('relatedness')
        r = max(0, min(1, r)) if isinstance(r, (int, float)) and not isinstance(r, bool) else 0
        score_by_pair_id[s['pairId']] = {'relatedness': r, 'reasoning': s.get('reasoning')}
    missing_scores = [p['id'] for p in pairs if p['id'] not in score_by_pair_id]

    dsu = DisjointSet()
    for n in nodes:
        dsu.add(n['id'])

    # Strongest relation first. A pair the model never scored is treated as
    # relatedness 0: eligible only as an absolute last resort.
    ranked = sorted(
        ({**p, **score_by_pair_id.get(p['id'], {'relatedness': 0, 'reasoning': None})} for p in pairs),
        key=lambda p: -p['relatedness'],
    )

    # Groups stay homogeneous, so a node's own flag is its whole group's.
    revised = {n['id']: bool((n.get('hazop') or {}).get('revisedScope')) for n in nodes}
    applied_merges = []
    blocked_merges = []
    group_count = len(nodes)
    for p in ranked:
        if group_count <= target_max_nodes:
            break
        if dsu.connected(p['aId'], p['bId']):
            continue  # already in the same group
        if revised[p['aId']] != revised[p['bId']]:
            blocked_merges.append({'aId': p['aId'], 'bId': p['bId'], 'relatedness': p['relatedness'],
                                   'reason': 'R6: new/modified scope is never merged into existing scope'})
            continue
        dsu.union(p['aId'], p['bId'])
        group_count -= 1
        applied_merges.append({
            'aId': p['aId'], 'bId': p['bId'], 'relatedness': p['relatedness'], 'reasoning': p['reasoning'],
            'forced': p['relatedness'] < min_relatedness,
        })

    node_by_id = {n['id']: n for n in nodes}
    built = sorted((_build_consolidated_node(members, node_by_id) for members in dsu.groups()), key=sort_key_top_left)
    consolidated = [{'id': f'p{page_index}-cnode-{i + 1}', 'page': page_index, **n} for i, n in enumerate(built)]

    return {
        'pageIndex': page_index,
        'nodes': consolidated,
        'diagnostics': {
            'skipped': False,
            'startingNodes': len(nodes),
            'targetMaxNodes': target_max_nodes,
            'finalNodes': len(consolidated),
            'reachedTarget': len(consolidated) <= target_max_nodes,
            'pairsScored': len(scores),
            'invalidPairIds': invalid_pair_ids,
            'missingScores': missing_scores,
            'appliedMerges': applied_merges,
            'blockedMerges': blocked_merges,
            'forcedMergeCount': sum(1 for m in applied_merges if m['forced']),
            'modelError': collected.get('error'),
        },
    }


def _build_consolidated_node(member_node_ids, node_by_id):
    members = [node_by_id[nid] for nid in member_node_ids]
    process_ids = list(dict.fromkeys(pid for n in members for pid in n['processIds']))
    return {
        'label': members[0]['label'] if len(members) == 1 else ' + '.join(n['label'] for n in members),
        'description': ' '.join(n['description'] for n in members),
        'categories': list(dict.fromkeys(c for n in members for c in n['categories'])),
        # The fine-grained nodes folded into this one — nothing about the
        # detailed breakdown is lost, it's grouped under one broader scope.
        'subNodeIds': member_node_ids,
        'processIds': process_ids,
        'memberCount': len(process_ids),
        'entityIds': list(dict.fromkeys(eid for n in members for eid in n['entityIds'])),
        'primaryEquipmentIds': list(dict.fromkeys(eid for n in members for eid in n['primaryEquipmentIds'])),
        'bbox': clamp_bbox(union_bboxes(n['bbox'] for n in members)),
        'routes': dedupe_routes(members),
        **({'hazop': merge_hazop(members)} if any(n.get('hazop') for n in members) else {}),
    }


def _union(lists):
    return list(dict.fromkeys(x for items in lists for x in items))


def _union_by(lists, key):
    out = {}
    for items in lists:
        for x in items:
            out.setdefault(key(x), x)
    return list(out.values())


def _merge_parameters(lists):
    """One entry per parameter name, its safeguards unioned across the merged nodes."""
    out = {}
    for p in (p for ps in lists for p in ps):
        key = (p['parameter'], p['name'].lower())
        if key in out:
            out[key] = {**out[key], 'highSafeguards': _union([out[key]['highSafeguards'], p['highSafeguards']]),
                        'lowSafeguards': _union([out[key]['lowSafeguards'], p['lowSafeguards']])}
        else:
            out[key] = p
    return list(out.values())


def merge_hazop(members):
    """The Step 6 fields of a reduced-option node: everything its sub-nodes
    covered, plus which nodes were merged (the rules ask to state it)."""
    hs = [normalize_hazop_fields(n['hazop']) for n in members if n.get('hazop')]
    if len(hs) == 1:
        return hs[0]
    typical = {h['typicalOf'] for h in hs}
    conditions = _union_by([[h['designConditions']] for h in hs], lambda c: (c['pressure'], c['temperature'], c['material']))
    return {
        'localId': '+'.join(h['localId'] for h in hs),
        'name': ' + '.join(h['name'] for h in hs),
        'boundaryFrom': _union_by([h['boundaryFrom'] for h in hs], lambda b: b['tag']),
        'boundaryTo': _union_by([h['boundaryTo'] for h in hs], lambda b: b['tag']),
        'equipmentTags': _union([h['equipmentTags'] for h in hs]),
        'designIntent': ' '.join(h['designIntent'] for h in hs),
        'designConditions': conditions[0] if len(conditions) == 1 else {
            k: ' / '.join(dict.fromkeys(c[k] for c in conditions)) for k in ('pressure', 'temperature', 'material', 'source')},
        'controlLoops': _union([h['controlLoops'] for h in hs]),
        'interlockRefs': _union([h['interlockRefs'] for h in hs]),
        'reliefDevices': _union_by([h['reliefDevices'] for h in hs], lambda r: r['tag']),
        'parameters': _merge_parameters([h['parameters'] for h in hs]),
        'vacuumProtection': next((h['vacuumProtection'] for h in hs if h['vacuumProtection']), None),
        'keyDeviations': _union_by([h['keyDeviations'] for h in hs], lambda d: (d['guideword'].upper(), d['parameter'], d['detail'].lower())),
        'operatingModes': _union([h['operatingModes'] for h in hs]),
        'utilitiesAsSafeguards': _union([h['utilitiesAsSafeguards'] for h in hs]),
        'typicalOf': typical.pop() if len(typical) == 1 else None,
        'typicalOfName': hs[0]['typicalOfName'] if len({h['typicalOf'] for h in hs}) == 1 else None,
        'differencesFromTypical': _union([h['differencesFromTypical'] for h in hs]),
        'revisedScope': all(h['revisedScope'] for h in hs),
        'justification': [*_union_by([h['justification'] for h in hs], lambda j: (j['rule'], j['reason'])),
                          {'rule': 'S', 'reason': 'Reduced option: merged ' + ', '.join(f"{h['localId']} {h['name']}" for h in hs)}],
        'unverified': _union([h['unverified'] for h in hs]),
    }
