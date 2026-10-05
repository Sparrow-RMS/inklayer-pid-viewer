"""Port of server/test/classify.test.mjs — step 2's id validation and derived geometry."""
import pytest

from hazop_agent.pipeline.step2.classify import assemble_page, is_functional


def box(x, y, w, h):
    return {'x': x, 'y': y, 'width': w, 'height': h}


def entity(eid, category, bbox, **extra):
    return {'id': eid, 'category': category, 'tag': eid, 'type': 't', 'attributes': [], 'bbox': bbox, 'routes': None, 'connectsTo': [], **extra}


STEP1_PAGE = {
    'pageIndex': 0,
    'entities': [
        entity('e-tank', 'equipment', box(0.10, 0.10, 0.10, 0.20)),
        entity('e-pump', 'equipment', box(0.30, 0.15, 0.05, 0.05)),
        entity('e-valve', 'valve', box(0.25, 0.16, 0.02, 0.02)),
        entity('e-line', 'line', box(0.20, 0.16, 0.10, 0.01), routes=[[{'x': 0.20, 'y': 0.165}, {'x': 0.30, 'y': 0.165}]]),
        entity('e-note', 'note', box(0.50, 0.50, 0.05, 0.02)),
        entity('e-label', 'label', box(0.55, 0.55, 0.05, 0.02)),
        entity('e-orphan-instr', 'instrument', box(0.80, 0.80, 0.02, 0.02)),
    ],
}
CLASSIFIABLE = [e for e in STEP1_PAGE['entities'] if e['category'] not in ('note', 'label', 'equipment_list_row')]


def proc(entity_ids, local_id='proc1', name='x', category='transfer', confidence=0.8, **o):
    return {
        'localId': local_id, 'name': name, 'category': category, 'description': 'd', 'entityIds': entity_ids,
        'primaryEquipmentIds': [], 'inletEntityIds': [], 'outletEntityIds': [], 'flowDirection': '',
        'boundaryReasoning': [], 'confidence': confidence, **o,
    }


def run(*processes):
    return assemble_page({'pageIndex': 0, 'step1Page': STEP1_PAGE, 'classifiable': CLASSIFIABLE, 'raw': {'processes': list(processes), 'pageNotes': ''}})


def test_well_formed_process_derives_geometry_from_real_entities():
    page = run(proc(['e-tank', 'e-valve', 'e-line', 'e-pump'], name='Tank to pump', confidence=0.9,
                    primaryEquipmentIds=['e-tank', 'e-pump'], inletEntityIds=['e-tank'], outletEntityIds=['e-pump']))
    assert len(page['processes']) == 1
    p = page['processes'][0]
    assert p['bbox']['x'] == pytest.approx(0.10) and p['bbox']['width'] == pytest.approx(0.25)
    assert len(p['routes']) == 1 and p['routes'][0]['entityId'] == 'e-line'
    assert sorted(p['primaryEquipmentIds']) == ['e-pump', 'e-tank']


def test_hallucinated_entity_id_is_dropped_and_recorded():
    p = run(proc(['e-tank', 'e-DOES-NOT-EXIST']))['processes'][0]
    assert 'e-tank' in p['entityIds']
    assert 'e-DOES-NOT-EXIST' not in p['entityIds']
    assert 'e-DOES-NOT-EXIST' in p['flags']['hallucinatedIds']


def test_process_anchored_entirely_on_fake_ids_is_dropped():
    assert run(proc(['e-FAKE-1', 'e-FAKE-2']))['processes'] == []


def test_coverage_diagnostics():
    d = run(proc(['e-tank', 'e-pump']))['diagnostics']
    assert 'e-note' not in d['uncoveredEntityIds'] and 'e-label' not in d['uncoveredEntityIds']
    assert {'e-valve', 'e-line', 'e-orphan-instr'} <= set(d['uncoveredEntityIds'])
    assert sorted(d['coveredEntityIds']) == ['e-pump', 'e-tank']


def test_heavily_overlapping_processes_are_flagged_on_both_sides():
    page = run(proc(['e-tank', 'e-valve', 'e-line'], 'proc1', 'A', confidence=0.9),
               proc(['e-tank', 'e-valve', 'e-line', 'e-pump'], 'proc2', 'B', confidence=0.9))
    a, b = page['processes']
    assert b['id'] in a['flags']['highOverlapWith'] and a['id'] in b['flags']['highOverlapWith']


def test_small_shared_boundary_overlap_is_not_flagged():
    page = run(proc(['e-tank'], 'proc1', 'A', 'agitation', 0.9),
               proc(['e-tank', 'e-valve', 'e-line', 'e-pump'], 'proc2', 'B', confidence=0.9))
    assert all('highOverlapWith' not in p['flags'] for p in page['processes'])


def test_synthesized_stub_stays_classifiable_despite_label_category():
    assert not is_functional(entity('e-real-label', 'label', box(0, 0, 0.01, 0.01)))
    assert is_functional(entity('e-synth', 'label', box(0, 0, 0.01, 0.01), flags={'synthesizedFromText': True}))


def test_repeated_id_collapses_and_counts_are_not_inflated():
    p = run(proc(['e-tank', 'e-tank', 'e-pump'], confidence=0.9))['processes'][0]
    assert len(p['entityIds']) == 2
    assert p['entityCategoryCounts']['equipment'] == 2
