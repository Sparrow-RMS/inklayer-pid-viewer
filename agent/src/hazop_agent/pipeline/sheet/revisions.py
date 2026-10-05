"""New or modified scope, read from the drawing's red markup (rule R6).

Revised scope on a P&ID is drawn in a highlight colour — red pipework, red
tags and red revision clouds around what changed. That colour is in the PDF
itself, so the scope is found here from exact geometry rather than asked of
the model:

- `scan_page` (needs the PDF) collects every red stroked segment, with
  curves kept apart because revision clouds are drawn as chains of arcs,
  and every red text run;
- `revised_scope` (pure) turns those marks into revision-cloud regions and
  the step 1 entities and text items they touch.
"""
import math
import re

from ..dsu import DisjointSet
from ..geometry import bbox_center, clamp_bbox, point_in_bbox
from ..pdf import _normalizer, span_box

# Colour channels are 0-1. "Red" is a strong red channel with little green or
# blue, which keeps orange-ish CAD layers and dark maroon text distinct.
RED_MIN, OTHER_MAX = 0.6, 0.35
# How close (page fractions) a red mark must come to an entity to count as touching it.
TOUCH = 0.003
# Revision clouds: red arcs whose ends meet within this gap chain into one
# cloud, and a chain needs at least this many arcs to be a cloud rather than
# a stray curved stroke (a bend, an arrowhead).
CLOUD_JOIN = 0.004
CLOUD_MIN_ARCS = 6
# ...and must enclose something: a flexible-hose zigzag or an instrument
# bubble is also a chain of red arcs, just a tiny one.
CLOUD_MIN_SPAN = 0.015
# Equipment symbols are big, and new red pipework often runs up to an
# existing vessel's nozzle. The vessel itself only counts as revised when red
# covers at least this share of its outline.
EQUIPMENT_RED_OUTLINE = 0.3
# A line entity is revised when at least this much of its route runs along red.
LINE_RED_FRACTION = 0.5


def is_red(color):
    return bool(color) and len(color) >= 3 and color[0] >= RED_MIN and color[1] <= OTHER_MAX and color[2] <= OTHER_MAX


def scan_page(page, page_index):
    """Red strokes and red text on one page, in normalized page fractions."""
    to_norm, matrix = _normalizer(page)
    segments = []
    for path in page.get_drawings():
        if not (is_red(path.get('color')) or is_red(path.get('fill'))):
            continue
        for item in path['items']:
            kind = item[0]
            if kind == 'l':
                pairs = [(item[1], item[2], False)]
            elif kind == 'c':
                pairs = [(item[1], item[4], True)]
            elif kind == 're':
                r = item[1]
                pts = [r.tl, r.tr, r.br, r.bl]
                pairs = [(pts[k], pts[(k + 1) % 4], False) for k in range(4)]
            elif kind == 'qu':
                q = item[1]
                pts = [q.ul, q.ur, q.lr, q.ll]
                pairs = [(pts[k], pts[(k + 1) % 4], False) for k in range(4)]
            else:
                continue
            for a, b, curve in pairs:
                segments.append({'a': to_norm(a.x, a.y), 'b': to_norm(b.x, b.y), 'curve': curve})

    texts = []
    for span in page.get_texttrace():
        if not is_red(span.get('color')):
            continue
        box = span_box(span, to_norm, matrix)
        if box is not None:
            texts.append({'str': re.sub(r'\s+', ' ', box[0].strip()), 'bbox': box[1]})

    return {'pageIndex': page_index, 'segments': segments, 'texts': texts}


def _point_segment_distance(p, a, b):
    dx, dy = b['x'] - a['x'], b['y'] - a['y']
    length_sq = dx * dx + dy * dy
    t = 0 if length_sq == 0 else max(0, min(1, ((p['x'] - a['x']) * dx + (p['y'] - a['y']) * dy) / length_sq))
    return math.hypot(p['x'] - (a['x'] + t * dx), p['y'] - (a['y'] + t * dy))


def _near_red(p, segments, tol=TOUCH):
    return any(_point_segment_distance(p, s['a'], s['b']) <= tol for s in segments)


def _sample(points, step=0.004):
    """Points every `step` along a polyline, so a long run is judged along its
    whole length rather than by its vertices alone."""
    out = []
    for a, b in zip(points, points[1:]):
        n = max(1, math.ceil(math.hypot(b['x'] - a['x'], b['y'] - a['y']) / step))
        out.extend({'x': a['x'] + (b['x'] - a['x']) * k / n, 'y': a['y'] + (b['y'] - a['y']) * k / n} for k in range(n))
    return out + points[-1:]


def _red_length_inside(box, segments, step=0.002):
    inside = 0.0
    for s in segments:
        pts = _sample([s['a'], s['b']], step)
        seg_len = math.hypot(s['b']['x'] - s['a']['x'], s['b']['y'] - s['a']['y'])
        if len(pts) > 1:
            inside += seg_len * sum(point_in_bbox(p, box) for p in pts) / len(pts)
    return inside


def cloud_regions(segments):
    """Chains of red arcs, joined end to end, big enough to be revision clouds."""
    arcs = [s for s in segments if s['curve']]
    dsu = DisjointSet()
    for i in range(len(arcs)):
        dsu.add(i)
    for i in range(len(arcs)):
        for j in range(i + 1, len(arcs)):
            if any(math.hypot(p['x'] - q['x'], p['y'] - q['y']) <= CLOUD_JOIN
                   for p in (arcs[i]['a'], arcs[i]['b']) for q in (arcs[j]['a'], arcs[j]['b'])):
                dsu.union(i, j)
    regions = []
    for group in dsu.groups():
        if len(group) < CLOUD_MIN_ARCS:
            continue
        pts = [p for i in group for p in (arcs[i]['a'], arcs[i]['b'])]
        xs, ys = [p['x'] for p in pts], [p['y'] for p in pts]
        if max(xs) - min(xs) < CLOUD_MIN_SPAN or max(ys) - min(ys) < CLOUD_MIN_SPAN:
            continue
        regions.append(clamp_bbox({'x': min(xs), 'y': min(ys), 'width': max(xs) - min(xs), 'height': max(ys) - min(ys)}))
    return sorted(regions, key=lambda b: (round(b['y'], 3), b['x']))


def red_text_ids(marks, text_items):
    """Step 1 text items printed in red, matched by string and position (ids
    differ between the JS and Python text layers, positions do not)."""
    by_str = {}
    for t in marks['texts']:
        by_str.setdefault(t['str'].upper(), []).append(bbox_center(t['bbox']))
    out = []
    for item in text_items:
        centers = by_str.get(item['str'].upper())
        if centers and any(math.hypot(c['x'] - m['x'], c['y'] - m['y']) <= TOUCH for c in [bbox_center(item['bbox'])] for m in centers):
            out.append(item['id'])
    return out


def revised_scope(marks, entities, text_items):
    """Which step 1 entities the red markup marks as new or modified. Pure."""
    segments = marks['segments']
    clouds = cloud_regions(segments)
    red_texts = set(red_text_ids(marks, text_items))

    revised = []
    for e in entities:
        reasons = []
        if red_texts.intersection(e.get('textAnchorIds') or []):
            reasons.append('tag printed in red')
        if e['category'] == 'line' and e.get('routes'):
            samples = [p for route in e['routes'] for p in _sample(route)]
            if samples and sum(_near_red(p, segments) for p in samples) / len(samples) >= LINE_RED_FRACTION:
                reasons.append('route drawn in red')
        elif e['category'] == 'equipment':
            box = e.get('symbolBbox') or e['bbox']
            perimeter = 2 * (box['width'] + box['height'])
            if perimeter > 0 and _red_length_inside(box, segments) / perimeter >= EQUIPMENT_RED_OUTLINE:
                reasons.append('symbol drawn in red')
        elif any(point_in_bbox(p, e.get('symbolBbox') or e['bbox'], pad=TOUCH) for s in segments for p in (s['a'], s['b'])):
            reasons.append('red markup on the symbol')
        center = bbox_center(e['bbox'])
        if any(point_in_bbox(center, c) for c in clouds):
            reasons.append('inside a revision cloud')
        if reasons:
            revised.append({'entityId': e['id'], 'tag': e.get('tag'), 'reasons': reasons})

    return {
        'redSegments': len(segments),
        'redTextItemIds': sorted(red_texts),
        'clouds': clouds,
        'entities': revised,
    }
