"""Port of server/test/consolidate.test.mjs — greedy consolidation to a target node count."""
import pytest

from hazop_agent.pipeline.step3.consolidate import assemble_page


def node(nid, **o):
    return {
        'id': nid, 'page': 0, 'label': nid, 'description': 'd', 'categories': ['transfer'],
        'processIds': [nid], 'entityIds': [], 'primaryEquipmentIds': [],
        'bbox': {'x': 0.5, 'y': 0.5, 'width': 0.02, 'height': 0.02}, 'routes': [], **o,
    }


def run(nodes, pairs, scores, target, floor=0.3):
    return assemble_page({'pageIndex': 0, 'nodes': nodes, 'pairs': pairs, 'scores': scores, 'error': None}, target, floor)


def test_at_or_under_target_is_a_no_op():
    page = run([node('a'), node('b')], [], [], 5)
    assert len(page['nodes']) == 2 and page['diagnostics']['skipped'] is True


def test_greedy_strongest_first_stops_exactly_at_target():
    nodes = [node(i) for i in 'abcde']
    pairs = [{'id': 'k1', 'aId': 'a', 'bId': 'b'}, {'id': 'k2', 'aId': 'c', 'bId': 'd'}, {'id': 'k3', 'aId': 'a', 'bId': 'c'},
             {'id': 'k4', 'aId': 'b', 'bId': 'e'}, {'id': 'k5', 'aId': 'd', 'bId': 'e'}]
    scores = [{'pairId': 'k1', 'relatedness': 0.95, 'reasoning': 'r'}, {'pairId': 'k2', 'relatedness': 0.90, 'reasoning': 'r'},
              {'pairId': 'k3', 'relatedness': 0.80, 'reasoning': 'r'}, {'pairId': 'k4', 'relatedness': 0.10, 'reasoning': 'r'},
              {'pairId': 'k5', 'relatedness': 0.05, 'reasoning': 'r'}]
    page = run(nodes, pairs, scores, 3)
    merges = page['diagnostics']['appliedMerges']
    assert len(page['nodes']) == 3
    assert len(merges) == 2 and all(m['relatedness'] >= 0.9 for m in merges)
    assert not any(m['relatedness'] == 0.8 for m in merges)


def test_forced_merges_below_the_floor_are_flagged():
    nodes = [node(i) for i in 'abc']
    pairs = [{'id': 'k1', 'aId': 'a', 'bId': 'b'}, {'id': 'k2', 'aId': 'a', 'bId': 'c'}, {'id': 'k3', 'aId': 'b', 'bId': 'c'}]
    scores = [{'pairId': 'k1', 'relatedness': 0.1, 'reasoning': 'weak'}, {'pairId': 'k2', 'relatedness': 0.05, 'reasoning': 'weak'},
              {'pairId': 'k3', 'relatedness': 0.02, 'reasoning': 'weak'}]
    page = run(nodes, pairs, scores, 1)
    d = page['diagnostics']
    assert len(page['nodes']) == 1
    assert all(m['forced'] for m in d['appliedMerges'])
    assert d['forcedMergeCount'] == len(d['appliedMerges'])


def test_hallucinated_and_unscored_pairs():
    nodes = [node(i) for i in 'abc']
    pairs = [{'id': 'k1', 'aId': 'a', 'bId': 'b'}, {'id': 'k2', 'aId': 'a', 'bId': 'c'}]
    scores = [{'pairId': 'k1', 'relatedness': 0.9, 'reasoning': 'r'}, {'pairId': 'k-BOGUS', 'relatedness': 0.99, 'reasoning': 'r'}]
    page = run(nodes, pairs, scores, 2)
    assert len(page['nodes']) == 2
    assert 'k-BOGUS' in page['diagnostics']['invalidPairIds']
    assert 'k2' in page['diagnostics']['missingScores']


def test_geometry_and_provenance_are_preserved():
    nodes = [
        node('a', bbox={'x': 0.1, 'y': 0.1, 'width': 0.1, 'height': 0.1}, routes=[{'entityId': 'ln1', 'points': []}], processIds=['proc-1']),
        node('b', bbox={'x': 0.3, 'y': 0.1, 'width': 0.1, 'height': 0.1}, routes=[{'entityId': 'ln1', 'points': []}, {'entityId': 'ln2', 'points': []}], processIds=['proc-2']),
    ]
    n = run(nodes, [{'id': 'k1', 'aId': 'a', 'bId': 'b'}], [{'pairId': 'k1', 'relatedness': 0.9, 'reasoning': 'r'}], 1)['nodes'][0]
    assert n['bbox']['width'] == pytest.approx(0.3)
    assert len(n['routes']) == 2
    assert sorted(n['processIds']) == ['proc-1', 'proc-2']
    assert sorted(n['subNodeIds']) == ['a', 'b']
