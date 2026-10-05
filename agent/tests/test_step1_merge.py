"""Port of server/test/merge.test.mjs — step 1's deterministic merge and repair passes."""
import pytest

from hazop_agent.pipeline.step1.extract import (
    attach_unclaimed_tags, find_composite_tags, merge_sightings, normalize_sighting,
    release_foreign_composite_tags, synthesize_unclaimed_tags,
)


def tile(tid, x, y, w=0.4, h=0.6):
    return {'id': tid, 'page': 0, 'row': 0, 'col': 0, 'region': {'x': x, 'y': y, 'width': w, 'height': h}}


TILE_A = tile('tile-A', 0.0, 0.0)
TILE_B = tile('tile-B', 0.3, 0.0)  # overlaps tile A
TILE_C = tile('tile-C', 0.6, 0.0)  # far from tile A

TEXT_BY_ID = {
    'p0-t1': {'id': 'p0-t1', 'str': 'P-101A', 'bbox': {'x': 0.31, 'y': 0.20, 'width': 0.03, 'height': 0.01}},
    'p0-t2': {'id': 'p0-t2', 'str': 'E-201', 'bbox': {'x': 0.70, 'y': 0.20, 'width': 0.03, 'height': 0.01}},
}


def raw(**o):
    base = {
        'localId': 'x', 'category': 'equipment', 'tag': None, 'type': 't', 'description': 'd',
        'attributes': [], 'textAnchorIds': [], 'bbox': {'x': 0.5, 'y': 0.3, 'width': 0.1, 'height': 0.1},
        'polyline': None, 'connectsTo': [], 'confidence': 0.8,
    }
    return {**base, **o}


def box(x, y, w, h):
    return {'x': x, 'y': y, 'width': w, 'height': h}


def sight(tile_, index=0, **o):
    return normalize_sighting(raw(**o), tile_, TEXT_BY_ID, index)


def test_duplicate_tagged_sighting_across_overlapping_tiles_merges():
    m = merge_sightings([
        sight(TILE_A, tag='P-101A', bbox=box(0.80, 0.33, 0.08, 0.10)),
        sight(TILE_B, tag='P-101 A', bbox=box(0.07, 0.33, 0.08, 0.10)),
    ], 0, TEXT_BY_ID)
    assert len(m) == 1
    assert m[0]['tag'] == 'P-101A'
    assert m[0]['sightings'] == 2


def test_same_tag_far_apart_stays_separate():
    m = merge_sightings([
        sight(TILE_A, tag='P-101A', bbox=box(0.1, 0.3, 0.05, 0.05)),
        sight(TILE_C, tag='P-101A', bbox=box(0.8, 0.8, 0.05, 0.05)),
    ], 0, TEXT_BY_ID)
    assert len(m) == 2


def test_shared_text_anchor_merges_even_with_distant_boxes():
    m = merge_sightings([
        sight(TILE_A, tag='P-101A', textAnchorIds=['p0-t1'], bbox=box(0.1, 0.1, 0.05, 0.05)),
        sight(TILE_C, tag=None, textAnchorIds=['p0-t1'], bbox=box(0.9, 0.9, 0.05, 0.05)),
    ], 0, TEXT_BY_ID)
    assert len(m) == 1
    assert m[0]['anchored'] is True
    assert m[0]['tagBbox']['x'] == pytest.approx(0.31)


def test_overlapping_untagged_symbols_in_one_tile_stay_separate():
    m = merge_sightings([
        sight(TILE_A, 0, bbox=box(0.50, 0.30, 0.10, 0.10)),
        sight(TILE_A, 1, bbox=box(0.51, 0.31, 0.10, 0.10)),
    ], 0, TEXT_BY_ID)
    assert len(m) == 2


def test_untagged_look_alikes_across_tiles_merge():
    m = merge_sightings([
        sight(TILE_A, bbox=box(0.80, 0.30, 0.10, 0.10)),
        sight(TILE_B, bbox=box(0.0666, 0.30, 0.10, 0.10)),
    ], 0, TEXT_BY_ID)
    assert len(m) == 1


def test_one_line_number_is_one_line_however_far_it_runs():
    def mk(t, x):
        return sight(t, category='line', tag='8"-CR-2203-A1', bbox=box(x, 0.5, 0.1, 0.02),
                     polyline=[{'x': x, 'y': 0.5}, {'x': x + 0.1, 'y': 0.5}])
    m = merge_sightings([mk(TILE_A, 0.1), mk(TILE_C, 0.8)], 0, TEXT_BY_ID)
    assert len(m) == 1
    assert len(m[0]['routes']) == 2  # both traced routes kept separately


def test_same_tag_in_different_categories_stays_separate():
    m = merge_sightings([
        sight(TILE_A, 0, category='equipment', tag='P-101A', bbox=box(0.5, 0.3, 0.05, 0.05)),
        sight(TILE_A, 1, category='label', tag='P-101A', bbox=box(0.5, 0.3, 0.05, 0.05)),
    ], 0, TEXT_BY_ID)
    assert len(m) == 2


def test_tag_not_present_in_its_anchored_text_is_flagged():
    m = merge_sightings([sight(TILE_A, tag='X-999', textAnchorIds=['p0-t1'])], 0, TEXT_BY_ID)
    assert m[0]['flags']['tagMismatch'] is True


def test_invalid_anchor_ids_are_recorded():
    m = merge_sightings([sight(TILE_A, tag='P-101A', textAnchorIds=['p0-t1', 'nope-99'])], 0, TEXT_BY_ID)
    assert 'nope-99' in m[0]['flags']['invalidAnchors']
    assert m[0]['anchored'] is True


# ---------------------------------------------------------------- bubbles
# An ISA bubble stores its function letters and loop number as two separate
# text runs, so neither half looks like a tag on its own.
BUBBLE_TEXT = [
    {'id': 'b1', 'str': 'FT', 'bbox': box(0.440, 0.430, 0.012, 0.008), 'fontSize': 7},
    {'id': 'b2', 'str': '202', 'bbox': box(0.441, 0.441, 0.014, 0.008), 'fontSize': 7},
    {'id': 'b3', 'str': 'FIC', 'bbox': box(0.512, 0.430, 0.014, 0.008), 'fontSize': 7},
    {'id': 'b4', 'str': '202', 'bbox': box(0.513, 0.441, 0.014, 0.008), 'fontSize': 7},
    {'id': 'b5', 'str': 'NOTES:', 'bbox': box(0.05, 0.69, 0.04, 0.009), 'fontSize': 9},
    {'id': 'b6', 'str': 'PID-2201-001', 'bbox': box(0.80, 0.90, 0.06, 0.008), 'fontSize': 8},
]
BUBBLE_BY_ID = {t['id']: t for t in BUBBLE_TEXT}


def test_split_bubble_tags_are_reassembled():
    c = find_composite_tags(BUBBLE_TEXT)
    assert sorted(x['str'] for x in c) == ['FIC-202', 'FT-202']
    assert next(x for x in c if x['str'] == 'FT-202')['bbox']['height'] > 0.015
    far = {**BUBBLE_TEXT[1], 'id': 'z', 'bbox': {**BUBBLE_TEXT[1]['bbox'], 'y': 0.9}}
    assert find_composite_tags([BUBBLE_TEXT[0], far]) == []


def test_a_bubble_tag_that_is_not_the_entitys_own_is_handed_back():
    entity = {
        'id': 'p0-in-3', 'tag': 'FIC-202', 'category': 'instrument',
        'textAnchorIds': ['b1', 'b2', 'b3', 'b4'], 'symbolBbox': box(0.505, 0.425, 0.03, 0.03),
        'bbox': box(0.505, 0.425, 0.03, 0.03), 'tagBbox': None, 'anchored': True,
    }
    released = release_foreign_composite_tags([entity], find_composite_tags(BUBBLE_TEXT), BUBBLE_BY_ID)
    assert len(released) == 1 and released[0]['tag'] == 'FT-202'
    assert sorted(entity['textAnchorIds']) == ['b3', 'b4']


def test_a_freed_bubble_tag_attaches_to_the_untagged_symbol_under_it():
    entities = [{
        'id': 'p0-in-9', 'tag': None, 'category': 'instrument', 'textAnchorIds': [],
        'symbolBbox': box(0.438, 0.426, 0.024, 0.026), 'bbox': box(0.438, 0.426, 0.024, 0.026),
        'tagBbox': None, 'anchored': False,
    }]
    attached = attach_unclaimed_tags(entities, BUBBLE_TEXT, find_composite_tags(BUBBLE_TEXT), BUBBLE_BY_ID)
    assert entities[0]['tag'] == 'FT-202'
    assert entities[0]['anchored'] is True and len(attached) >= 1


def test_orphan_bubble_tags_are_synthesized_rather_than_lost():
    meta = {'titleBlock': [{'key': 'drawingNumber', 'value': 'PID-2201-001'}], 'notes': [], 'legend': [], 'revisions': [], 'referencedDrawings': []}
    syn = synthesize_unclaimed_tags([], BUBBLE_TEXT, find_composite_tags(BUBBLE_TEXT), meta, 0)
    tags = sorted(e['tag'] for e in syn)
    assert 'FT-202' in tags and 'FIC-202' in tags
    assert all(e['flags']['synthesizedFromText'] and e['flags']['lowConfidence'] for e in syn)
    assert all(e['anchored'] and e['tagBbox'] for e in syn)
    assert 'PID-2201-001' not in tags  # already catalogued in drawingMeta
    assert 'NOTES:' not in tags  # prose is never mistaken for a tag


# -------------------------------------------------- ambiguous category pairs
# A control valve / PSV's body and its actuator-instrument are often typed
# inconsistently across tiles — observed for TV-1257004, PSV-1257001, HV-1257022.

def test_valve_instrument_pair_of_same_tag_close_together_merges():
    m = merge_sightings([
        sight(TILE_A, category='valve', tag='PSV-1257001', bbox=box(0.80, 0.667, 0.08, 0.05)),
        sight(TILE_B, category='instrument', tag='PSV-1257001', bbox=box(0.05, 0.667, 0.02, 0.03)),
    ], 0, TEXT_BY_ID)
    assert len(m) == 1
    assert m[0]['tag'] == 'PSV-1257001'


def test_valve_instrument_pair_far_apart_stays_separate():
    m = merge_sightings([
        sight(TILE_A, category='valve', tag='PSV-1257001', bbox=box(0.1, 0.1, 0.03, 0.03)),
        sight(TILE_C, category='instrument', tag='PSV-1257001', bbox=box(0.9, 0.9, 0.03, 0.03)),
    ], 0, TEXT_BY_ID)
    assert len(m) == 2


def test_unrelated_category_pair_does_not_merge_even_when_close():
    m = merge_sightings([
        sight(TILE_A, category='equipment', tag='X-01', bbox=box(0.80, 0.667, 0.08, 0.05)),
        sight(TILE_B, category='instrument', tag='X-01', bbox=box(0.05, 0.667, 0.02, 0.03)),
    ], 0, TEXT_BY_ID)
    assert len(m) == 2
