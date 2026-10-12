"""Pipe tracing from drawn geometry (pipeline/network.py), on small synthetic sheets."""
from hazop_agent.pipeline.network import build_network

W = H = 1000.0


def h(y, x0, x1, **kw):
    return {'o': 'H', 'c': y, 'lo': x0, 'hi': x1, 'w': 0.48, 'red': False, 'symbol': False, 'path': kw.pop('path', None), **kw}


def v(x, y0, y1, **kw):
    return {'o': 'V', 'c': x, 'lo': y0, 'hi': y1, 'w': 0.48, 'red': False, 'symbol': False, 'path': kw.pop('path', None), **kw}


def ent(eid, category, x0, y0, x1, y1, tag=None):
    box = {'x': x0 / W, 'y': y0 / H, 'width': (x1 - x0) / W, 'height': (y1 - y0) / H}
    return {'id': eid, 'category': category, 'tag': tag or eid, 'type': category, 'bbox': box, 'symbolBbox': box, 'attributes': []}


def trace(straight, entities=(), diagonals=(), triangles=(), text=(), outlines=()):
    return build_network(0, W, H, list(straight), list(diagonals), list(triangles), list(entities), list(text), list(outlines))


def ends(net):
    return sorted(tuple(e['kind'] for e in sp['ends']) for sp in net['spans'])


def test_a_pipe_broken_at_a_crossing_is_one_pipe_and_the_crossing_is_not_a_junction():
    # The horizontal is drawn with a gap where the vertical crosses it.
    net = trace([h(500, 100, 395), h(500, 405, 900), v(400, 100, 900)])
    assert len(net['spans']) == 2
    assert ends(net) == [('end', 'end'), ('end', 'end')]


def test_a_run_ending_on_another_runs_middle_is_a_junction():
    net = trace([h(500, 100, 900), v(400, 500, 900)])
    assert len(net['spans']) == 3  # the horizontal is split at the junction
    assert sum(e['kind'] == 'junction' for sp in net['spans'] for e in sp['ends']) == 3


def test_a_gap_holding_valve_strokes_is_bridged_and_the_valve_is_on_the_pipe():
    valve = ent('vl', 'valve', 480, 490, 520, 510)
    marks = [(490, 495, 4), (510, 505, 14)]  # bowtie diagonals in the gap
    net = trace([h(500, 100, 482), h(500, 518, 900)], [valve], diagonals=marks)
    assert len(net['spans']) == 1
    assert net['spans'][0]['through'] == ['vl']


def test_a_wide_gap_with_nothing_in_it_is_not_bridged():
    net = trace([h(500, 100, 450), h(500, 480, 900)])
    assert len(net['spans']) == 2


def test_dash_dot_boundaries_are_not_pipes():
    dashes = [h(300, x, x + 12) for x in range(100, 600, 15)]
    net = trace(dashes)
    assert net['spans'] == [] and net['diagnostics']['rejectedRuns']['dashed'] == 1


def test_small_closed_symbol_outlines_are_not_pipes():
    box = [h(300, 100, 160, symbol=True), h(320, 100, 160, symbol=True), v(100, 300, 320, symbol=True), v(160, 300, 320, symbol=True)]
    assert trace(box)['spans'] == []


def test_a_drawn_vessel_shell_is_not_pipe_and_a_pipe_on_its_nozzle_connects_to_it():
    tank = ent('tk', 'equipment', 210, 210, 290, 290, tag='T-1')  # the model's box is smaller than the shell
    shell = [h(200, 200, 300, path=7), h(300, 200, 300, path=7), v(200, 200, 300, path=7), v(300, 200, 300, path=7)]
    net = trace([*shell, h(250, 304, 700)], [tank], outlines=[{'path': 7, 'box': (200, 200, 300, 300), 'thin': False}])
    assert len(net['spans']) == 1
    assert net['spans'][0]['ends'][0]['entityId'] == 'tk'


def test_an_arrowhead_on_the_pipe_gives_its_direction():
    arrow = {'pts': [(600, 500), (590, 496), (590, 504)], 'filled': True}  # points +x
    net = trace([h(500, 100, 900)], triangles=[arrow])
    sp = net['spans'][0]
    first, last = sp['points'][0]['x'], sp['points'][-1]['x']
    assert sp['flowSource'] == 'arrow'
    assert (sp['flow'] == 'forward') == (last > first)


def test_a_mark_beside_the_pipe_is_not_a_flow_arrow():
    drain = {'pts': [(600, 500), (596, 490), (604, 490)], 'filled': False}  # touches with its tip only
    net = trace([h(500, 100, 900)], triangles=[drain])
    assert net['spans'][0]['flow'] is None


def test_a_pipe_at_the_point_of_a_connector_is_an_inlet():
    connector = ent('cn', 'connector', 20, 490, 100, 510, tag='4001')
    net = trace([h(500, 101, 600)], [connector])
    sp = net['spans'][0]
    k = [e['entityId'] for e in sp['ends']].index('cn')
    assert sp['ends'][k]['connectorRole'] == 'inlet'
    # flow runs away from the connector
    assert sp['flow'] == ('forward' if k == 0 else 'reverse')


def test_direction_is_inferred_at_a_junction_where_every_other_branch_flows_in():
    left = ent('a', 'connector', 0, 490, 80, 510)
    top = ent('b', 'connector', 380, 0, 420, 80)
    net = trace([h(500, 81, 400), v(400, 81, 500), h(500, 400, 900)], [left, top])
    out = next(sp for sp in net['spans'] if max(p['x'] for p in sp['points']) > 0.8)
    assert out['flowSource'] == 'inferred'
    # flows away from the junction at x = 400
    start_x = out['points'][0]['x'] if out['flow'] == 'forward' else out['points'][-1]['x']
    assert abs(start_x - 0.4) < 1e-3


def test_links_join_equipment_through_valves_with_their_direction():
    tank = ent('tk', 'equipment', 50, 450, 100, 550, tag='T-1')
    filt = ent('fl', 'equipment', 800, 450, 850, 550, tag='F-1')
    valve = ent('vl', 'valve', 480, 490, 520, 510)
    arrow = {'pts': [(300, 500), (290, 496), (290, 504)], 'filled': True}
    net = trace([h(500, 104, 482), h(500, 518, 796)], [tank, filt, valve],
                diagonals=[(490, 495, 4), (510, 505, 14)], triangles=[arrow])
    (link,) = net['links']
    assert {link['from'], link['to']} == {'tk', 'fl'} and link['via'] == ['vl']
    assert link['flow'] == ('from->to' if link['from'] == 'tk' else 'to->from')

