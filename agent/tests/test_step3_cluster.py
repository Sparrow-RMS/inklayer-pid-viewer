"""Port of server/test/cluster.test.mjs — candidate generation and DSU clustering."""
import pytest

from hazop_agent.pipeline.step3.candidates import generate_candidate_pairs
from hazop_agent.pipeline.step3.cluster import assemble_page


def box(x, y, w, h):
    return {'x': x, 'y': y, 'width': w, 'height': h}


def process(pid, **o):
    return {
        'id': pid, 'page': 0, 'name': pid, 'category': 'transfer', 'description': 'd',
        'entityIds': [], 'primaryEquipmentIds': [], 'inletEntityIds': [], 'outletEntityIds': [],
        'bbox': box(0.5, 0.5, 0.05, 0.05), 'routes': [], 'flags': {}, **o,
    }


def pair(cid, a, b):
    return {'id': cid, 'aId': a, 'bId': b, 'reasons': ['x']}


def verdict(cid, same, confidence=0.9):
    return {'pairId': cid, 'sameNode': same, 'confidence': confidence, 'reasoning': 'r'}


# ------------------------------------------------------------ candidates

def test_sharing_an_entity_produces_a_candidate():
    pairs = generate_candidate_pairs([process('a', entityIds=['e1', 'e2']), process('b', entityIds=['e2', 'e3'])], {})
    assert len(pairs) == 1
    assert any('entity' in r for r in pairs[0]['reasons'])


def test_sharing_primary_equipment_produces_a_candidate():
    pairs = generate_candidate_pairs([process('a', primaryEquipmentIds=['tank1']), process('b', primaryEquipmentIds=['tank1'])], {})
    assert len(pairs) == 1
    assert any('major equipment' in r for r in pairs[0]['reasons'])


def test_step2_high_overlap_flag_produces_a_candidate():
    assert len(generate_candidate_pairs([process('a', flags={'highOverlapWith': ['b']}), process('b')], {})) == 1


def test_resolved_connection_outlet_to_inlet_produces_a_candidate():
    by_id = {'out1': {'id': 'out1', 'resolvedConnections': ['in1']}, 'in1': {'id': 'in1', 'resolvedConnections': []}}
    pairs = generate_candidate_pairs([process('a', outletEntityIds=['out1']), process('b', inletEntityIds=['in1'])], by_id)
    assert len(pairs) == 1
    assert any('connects' in r for r in pairs[0]['reasons'])


def test_close_bboxes_alone_still_produce_a_weak_candidate():
    pairs = generate_candidate_pairs([process('a', bbox=box(0.50, 0.50, 0.02, 0.02)), process('b', bbox=box(0.53, 0.50, 0.02, 0.02))], {})
    assert len(pairs) == 1


def test_unrelated_distant_processes_produce_no_candidate():
    assert generate_candidate_pairs([process('a', bbox=box(0, 0, 0.02, 0.02)), process('b', bbox=box(0.9, 0.9, 0.02, 0.02))], {}) == []


# ------------------------------------------------------------ clustering

def cluster(processes, pairs, verdicts):
    return assemble_page({'pageIndex': 0, 'processes': processes, 'pairs': pairs, 'verdicts': verdicts, 'error': None})


def test_dsu_transitivity():
    # (A,B) yes + (B,C) yes -> one node {a,b,c}, though A and C were never compared.
    page = cluster(
        [process('a'), process('b'), process('c'), process('d')],
        [pair('c1', 'a', 'b'), pair('c2', 'b', 'c'), pair('c3', 'c', 'd')],
        [verdict('c1', True), verdict('c2', True), verdict('c3', False)],
    )
    assert len(page['nodes']) == 2
    abc = next(n for n in page['nodes'] if n['memberCount'] == 3)
    assert set(abc['processIds']) == {'a', 'b', 'c'}


def test_low_confidence_yes_is_not_applied_but_is_recorded():
    page = cluster([process('a'), process('b')], [pair('c1', 'a', 'b')], [verdict('c1', True, 0.2)])
    assert len(page['nodes']) == 2
    assert page['diagnostics']['appliedVerdicts'][0]['applied'] is False


def test_hallucinated_pair_id_is_recorded_not_trusted():
    page = cluster([process('a'), process('b')], [pair('c1', 'a', 'b')], [verdict('c-DOES-NOT-EXIST', True)])
    assert len(page['nodes']) == 2
    assert 'c-DOES-NOT-EXIST' in page['diagnostics']['invalidPairIds']


def test_unanswered_pair_defaults_to_separate():
    page = cluster([process('a'), process('b')], [pair('c1', 'a', 'b')], [])
    assert len(page['nodes']) == 2
    assert 'c1' in page['diagnostics']['missingVerdicts']


def test_unpaired_process_becomes_a_singleton_node():
    page = cluster([process('lonely')], [], [])
    assert len(page['nodes']) == 1 and page['nodes'][0]['memberCount'] == 1


def test_node_geometry_is_derived_and_shared_routes_deduplicated():
    page = cluster(
        [process('a', bbox=box(0.10, 0.10, 0.10, 0.10), routes=[{'entityId': 'ln1', 'points': []}]),
         process('b', bbox=box(0.30, 0.10, 0.10, 0.10), routes=[{'entityId': 'ln1', 'points': []}, {'entityId': 'ln2', 'points': []}])],
        [pair('c1', 'a', 'b')], [verdict('c1', True)],
    )
    node = page['nodes'][0]
    assert node['bbox']['width'] == pytest.approx(0.30)
    assert len(node['routes']) == 2
