"""PDF layer: text runs, vector segments and rendering, via PyMuPDF.

The synthetic fixture (server/test/fixtures/make-pid.mjs) has a known ground
truth, and its expected text boxes were checked against the pdfjs output the
JS pipeline stored for the same file (identical to ~1e-8 of the page).
"""
import json

import pymupdf
import pytest

from hazop_agent.pipeline.pdf import extract_text_items, extract_vector_segments, load_pdf, page_geometry, render_region

from .conftest import FIXTURE_PDF, SERVER_JOBS


@pytest.fixture
def page():
    return load_pdf(FIXTURE_PDF)[0]


def test_geometry_is_the_visible_page_size(page):
    assert page_geometry(page, 0) == {'pageIndex': 0, 'width': 1190.0, 'height': 842.0, 'rotation': 0}


def test_text_layer_contains_the_ground_truth_tags(page):
    strs = {t['str'] for t in extract_text_items(page, 0)}
    for tag in ('T-101', 'P-101A', 'P-101B', 'E-201', 'HV-201', 'HV-215', 'FCV-202', '8"-CR-2203-A1'):
        assert tag in strs
    # ISA bubbles store letters and loop number as separate runs.
    assert {'FT', '202', 'LIC', '101', 'PSV', '201'} <= strs
    # Runs of whitespace collapse, as pdfjs does.
    assert 'DWG No. PID-2201-001' in strs


def test_text_boxes_are_normalized_and_inside_the_page(page):
    items = extract_text_items(page, 0)
    assert items and all(t['id'].startswith('p0-t') for t in items)
    for t in items:
        b = t['bbox']
        assert 0 <= b['x'] <= 1 and 0 <= b['y'] <= 1 and b['width'] > 0 and b['height'] > 0


def test_text_boxes_match_the_js_pipeline_for_the_same_pdf(page):
    reference = SERVER_JOBS / 'pid-sample-8c299cdeb0b7' / 'step1-raw.json'
    if not reference.exists():
        pytest.skip('JS reference job not present')
    js_items = json.loads(reference.read_text())['pages'][0]['textItems']
    by_str = {}
    for t in extract_text_items(page, 0):
        by_str.setdefault(t['str'], []).append(t)
    matched = 0
    for js in js_items:
        candidates = by_str.get(js['str'])
        if not candidates:
            continue
        best = min(candidates, key=lambda t: abs(t['bbox']['x'] - js['bbox']['x']) + abs(t['bbox']['y'] - js['bbox']['y']))
        for k in ('x', 'y', 'width', 'height'):
            assert best['bbox'][k] == pytest.approx(js['bbox'][k], abs=1e-6)
        assert best['angle'] == js['angle']
        matched += 1
    assert matched >= len(js_items) - 6  # the rest differ only by whitespace inside legend strings


def test_rotated_page_keeps_boxes_in_visible_page_space(tmp_path):
    doc = load_pdf(FIXTURE_PDF)
    upright = {t['str']: t for t in extract_text_items(doc[0], 0)}
    doc[0].set_rotation(90)
    path = tmp_path / 'rotated.pdf'
    doc.save(path)
    page = load_pdf(path)[0]
    geom = page_geometry(page, 0)
    assert (geom['width'], geom['height'], geom['rotation']) == (842.0, 1190.0, 90)
    rotated = {t['str']: t for t in extract_text_items(page, 0)}
    # Rotating the page 90 deg clockwise maps upright (x, y) to (1 - y - h, x).
    a, b = upright['T-101']['bbox'], rotated['T-101']['bbox']
    assert b['x'] == pytest.approx(1 - a['y'] - a['height'], abs=1e-6)
    assert b['y'] == pytest.approx(a['x'], abs=1e-6)
    assert rotated['T-101']['angle'] == 90


def test_vector_segments_are_stroked_geometry(page):
    segments = extract_vector_segments(page, 0)
    assert len(segments) == 101  # same count pdfjs yields for this file
    assert all(s['length'] >= 0.002 for s in segments)
    assert any(s.get('approx') for s in segments)  # instrument bubbles are bezier curves


def test_render_region_scales_the_longest_side_to_target(page):
    out = render_region(page, {'x': 0, 'y': 0, 'width': 0.5, 'height': 0.5}, target_px=800)
    assert out['png'][:8] == b'\x89PNG\r\n\x1a\n'
    assert max(out['width'], out['height']) == pytest.approx(800, abs=1)
    pix = pymupdf.Pixmap(out['png'])
    assert (pix.width, pix.height) == (out['width'], out['height'])
