"""Step 3 node routes: every stretch of pipe is marked by one node only."""
from hazop_agent.pipeline.step3.build import _node_routes

LINE = {'id': 'ln', 'category': 'line', 'routes': [[{'x': 0.1, 'y': 0.5}, {'x': 0.9, 'y': 0.5}]]}
PUMP = {'id': 'pm', 'category': 'equipment', 'routes': None}
BY_ID = {e['id']: e for e in (LINE, PUMP)}


def node(local_id, *entity_ids):
    return {'localId': local_id, 'entityIds': list(entity_ids)}


def test_a_line_two_nodes_list_is_drawn_by_the_first_only():
    routes = _node_routes([node('N1', 'ln', 'pm'), node('N2', 'ln')], None, list(BY_ID.values()), BY_ID)
    assert [r['entityId'] for r in routes['N1']] == ['ln']
    assert 'N2' not in routes


def test_traced_spans_replace_the_model_lines_and_a_node_without_spans_gets_none():
    network = {'spans': [{'id': 'sp', 'points': LINE['routes'][0], 'ends': [], 'through': [], 'vertices': [], 'red': False, 'flow': None}]}
    routes = _node_routes([node('N1', 'ln'), node('N2', 'ln', 'pm')], network, list(BY_ID.values()), BY_ID)
    assert [r['entityId'] for rs in routes.values() for r in rs] == ['sp']
    assert len(routes) == 1
