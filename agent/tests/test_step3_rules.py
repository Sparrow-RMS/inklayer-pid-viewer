"""The node rules computed by code: C&E consistency (V3), Step 7 validation,
and the reduced option never merging revised scope (R6)."""
from hazop_agent.pipeline.sheet.extract import consistency_findings
from hazop_agent.pipeline.step3 import consolidate, rules


def entity(eid, category, tag=None, x=0.1, y=0.1, **extra):
    return {'id': eid, 'category': category, 'tag': tag, 'type': category, 'attributes': [],
            'bbox': {'x': x, 'y': y, 'width': 0.01, 'height': 0.01}, 'textAnchorIds': [], 'connectsTo': [],
            'resolvedConnections': [], 'flags': {}, **extra}


def row(ref, kind, final, cause=(), states=()):
    return {'ref': ref, 'kind': kind, 'finalElementTags': list(final), 'causeTags': list(cause),
            'requiredStates': [{'tag': t, 'state': s} for t, s in states], 'unverified': False, 'unverifiedReason': '',
            'tagsNotOnDrawing': [], 'entityIds': []}


# Rows 1257022/23 and 1257026/27 of the tank-farm sheet's C&E table.
P303A_PERMISSIVE = row('1257022', 'permissive', ['P-V303A'], states=[
    ('XV1-1257020', 'open'), ('HV-1257042', 'open'), ('HV-1257021', 'open'), ('XV1-1257014', 'open'),
    ('XV1-1257013', 'open'), ('ZS-1257033', 'inline'), ('XV1-1257048', 'closed')])
P303A_TRIP = row('1257023', 'trip', ['P-V303A'], cause=[
    'XV1-1257020', 'XV1-1257013', 'XV1-1257014', 'HV-1257021', 'HV-1257042', 'ZS-1257033'])
P303B_PERMISSIVE = row('1257026', 'permissive', ['P-V303B'], states=[
    ('XV1-1257020', 'open'), ('HV-1257042', 'open'), ('HV-1257021', 'open'), ('XV1-1257014', 'open'),
    ('XV1-1257013', 'open'), ('ZS-1257046', 'inline'), ('XV1-1257020', 'closed')])
P303B_TRIP = row('1257027', 'trip', ['P-V303B'], cause=[
    'XV1-1257020', 'XV1-1257013', 'XV1-1257014', 'HV-1257050', 'HV-1257051', 'ZS-1257046'])

NO_SCOPE = {'entities': []}
HAZOP_FIELDS = {
    'boundaryFrom': [], 'boundaryTo': [], 'equipmentTags': [], 'designIntent': '',
    'designConditions': {'pressure': 'ATM', 'temperature': '65 C', 'material': 'SS304', 'source': 'data block'},
    'controlLoops': [], 'interlockRefs': [], 'reliefDevices': [],
    'protection': {'criticalParameter': 'level', 'highSafeguards': [], 'lowSafeguards': [], 'vacuumProtection': None},
    'keyDeviations': [], 'operatingModes': [], 'utilitiesAsSafeguards': [], 'typicalOf': None, 'typicalOfName': None,
    'differencesFromTypical': [], 'justification': [], 'unverified': [],
}


def test_matching_permissive_and_trip_raise_nothing():
    assert consistency_findings([P303A_PERMISSIVE, P303A_TRIP], [], NO_SCOPE) == []


def test_the_real_p_v303b_rows_are_caught():
    findings = consistency_findings([P303B_PERMISSIVE, P303B_TRIP], [], NO_SCOPE)
    by_check = {f['check']: f for f in findings}

    assert by_check['open-and-closed']['tags'] == ['XV1-1257020']
    mismatch = by_check['permissive-trip-mismatch']
    assert mismatch['interlockRefs'] == ['1257026', '1257027']
    assert set(mismatch['tags']) == {'HV-1257042', 'HV-1257021', 'HV-1257050', 'HV-1257051'}


def test_new_equipment_with_no_interlock_is_flagged():
    pot = entity('e1', 'equipment', 'STPOT-NS302')
    findings = consistency_findings([P303A_TRIP], [pot], {'entities': [{'entityId': 'e1', 'tag': 'STPOT-NS302', 'reasons': []}]})
    assert [f['check'] for f in findings] == ['unprotected-new-scope']


def node(local_id, entity_ids, refs=(), revised=False, high=('LSHH',), low=('LSL',), boundaries=()):
    return {'localId': local_id, 'name': local_id, 'entityIds': list(entity_ids), 'interlockRefs': list(refs),
            'protection': {'criticalParameter': 'level', 'highSafeguards': list(high), 'lowSafeguards': list(low), 'vacuumProtection': None},
            'boundaryFrom': list(boundaries), 'boundaryTo': [], 'revisedScope': revised, 'operatingModes': [], 'equipmentTags': []}


def checks(findings):
    return {f['check']: f for f in findings}


def test_validation_catches_orphans_unmapped_interlocks_split_loops_and_separated_psvs():
    entities = [
        entity('tank', 'equipment', 'T-101', x=0.1),
        entity('psv', 'valve', 'PSV-101', x=0.11),
        entity('lt', 'instrument', 'LT-101', x=0.3),
        entity('lv', 'valve', 'LV-101', x=0.315),
        entity('pump', 'equipment', 'P-101', x=0.6),
        entity('orphan', 'line', '2"-P-100', x=0.9),
    ]
    nodes = [node('N1', ['tank', 'lt'], refs=['IL-1']), node('N2', ['pump', 'lv', 'psv'], refs=['IL-1'], high=())]
    found = checks(rules.validate(nodes, entities, [row('IL-1', 'trip', ['P-101']), row('IL-2', 'trip', ['P-101'])], set()))

    assert found['V1-orphan']['entityIds'] == ['orphan']
    assert found['V2-unassigned']['interlockRefs'] == ['IL-2']
    assert found['V2-duplicate']['nodeIds'] == ['N1', 'N2']
    assert found['V5-split-loop']['entityIds'] == ['lt', 'lv']
    assert found['V5-relief-separated']['entityIds'] == ['psv', 'tank']
    assert 'HIGH' in found['V4-protection-gap']['detail']


def test_revised_scope_merged_into_an_existing_node_is_an_error():
    entities = [entity('old', 'equipment', 'T-101'), entity('new1', 'valve', 'XV-1'), entity('new2', 'line', 'P-100')]
    found = checks(rules.validate([node('N1', ['old', 'new1', 'new2'])], entities, [], {'new1', 'new2'}))
    assert found['R6-merged']['entityIds'] == ['new1', 'new2']

    ok = rules.validate([node('N1', ['old']), node('N2', ['new1', 'new2'], revised=True)], entities, [], {'new1', 'new2'})
    assert not [f for f in ok if f['check'].startswith('R6')]


def test_a_boundary_on_a_line_is_flagged():
    found = checks(rules.validate([node('N1', [], boundaries=[{'tag': 'P-100', 'kind': 'line', 'note': ''}])], [], [], set()))
    assert 'P-100' in found['V5-mid-line']['detail']


def test_the_reduced_option_never_merges_revised_scope():
    def n(nid, revised):
        return {'id': nid, 'label': nid, 'description': '', 'categories': ['transfer'], 'processIds': [nid], 'entityIds': [nid],
                'primaryEquipmentIds': [], 'bbox': {'x': 0, 'y': 0, 'width': 0.1, 'height': 0.1}, 'routes': [],
                'hazop': {**HAZOP_FIELDS, 'localId': nid, 'name': nid, 'revisedScope': revised}}

    nodes = [n('a', False), n('b', False), n('new', True)]
    pairs = [{'id': 'k1', 'aId': 'a', 'bId': 'new'}, {'id': 'k2', 'aId': 'a', 'bId': 'b'}, {'id': 'k3', 'aId': 'b', 'bId': 'new'}]
    scores = [{'pairId': 'k1', 'relatedness': 0.99, 'reasoning': ''}, {'pairId': 'k2', 'relatedness': 0.5, 'reasoning': ''},
              {'pairId': 'k3', 'relatedness': 0.9, 'reasoning': ''}]

    page = consolidate.assemble_page({'pageIndex': 0, 'nodes': nodes, 'pairs': pairs, 'scores': scores, 'error': None}, 1)

    assert [sorted(x['subNodeIds']) for x in page['nodes']] in ([['a', 'b'], ['new']], [['new'], ['a', 'b']])
    assert page['diagnostics']['reachedTarget'] is False
    assert {(m['aId'], m['bId']) for m in page['diagnostics']['blockedMerges']} == {('a', 'new'), ('b', 'new')}


def test_same_number_devices_far_apart_are_not_one_loop():
    # On the tank-farm sheet "TV-1257002" is a T-valve on the filling header
    # and "TIT-1257002" a transmitter on the transfer train.
    tv = entity('tv', 'valve', 'TV-1257002', x=0.2)
    tit = entity('tit', 'instrument', 'TIT-1257002', x=0.7)
    te = entity('te', 'instrument', 'TE-1257002', x=0.705)
    assert rules.control_loops([tv, tit, te]) == {'T-1257002': ['tit', 'te']}


def test_step5_two_modes_is_an_error_and_many_interlocks_a_warning():
    n = node('N1', [], refs=[f'IL-{i}' for i in range(12)])
    n['operatingModes'] = ['ST-V305 FILLING MODE', 'P-V305A/B TRANSFER MODE', 'RAIN EVENT']
    found = checks(rules.validate([n], [], [], set(), modes=['ST-V305 FILLING MODE', 'P-V305A/B TRANSFER MODE']))
    assert found['S-too-many-interlocks']['severity'] == 'warning'
    assert found['S-two-modes']['severity'] == 'error' and 'RAIN EVENT' not in found['S-two-modes']['detail']


def test_out_of_scope_items_are_not_orphans():
    entities = [entity('ia', 'line', 'IA-100'), entity('tank', 'equipment', 'T-101')]
    found = checks(rules.validate([node('N1', ['tank'])], entities, [], set(), out_of_scope={'ia'}))
    assert 'V1-orphan' not in found
    assert 'IA-100' in checks(rules.validate([node('N1', ['tank'])], entities, [], set()))['V1-orphan']['detail']


def test_too_many_nodes_is_a_warning():
    found = checks(rules.validate([node(f'N{i}', []) for i in range(8)], [], [], set()))
    assert found['S-node-count']['severity'] == 'warning'
