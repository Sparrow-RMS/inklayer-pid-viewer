"""Geometry helpers for the P&ID pipeline.

Coordinate convention used EVERYWHERE downstream of `pdf.py`:
  - normalized fractions (0..1) of the page's visible (rotation-applied) size
  - top-left origin, y growing downward
This is the same space `src/lib/annotationFromNode.ts` already consumes on the
frontend, so the annotation step needs no further conversion.

A bbox is {"x", "y", "width", "height"}; a point is {"x", "y"}. Plain dicts,
not classes, because every artifact is JSON on disk and shared with the JS
pipeline and the frontend.
"""
import math


def clamp01(v):
    return 0 if v < 0 else 1 if v > 1 else v


def clamp_bbox(b):
    x = clamp01(b['x'])
    y = clamp01(b['y'])
    return {'x': x, 'y': y, 'width': clamp01(b['x'] + b['width']) - x, 'height': clamp01(b['y'] + b['height']) - y}


def bbox_from_points(points):
    points = list(points)
    if not points:
        return None
    min_x = min(p['x'] for p in points)
    min_y = min(p['y'] for p in points)
    max_x = max(p['x'] for p in points)
    max_y = max(p['y'] for p in points)
    if not math.isfinite(min_x):
        return None
    return {'x': min_x, 'y': min_y, 'width': max_x - min_x, 'height': max_y - min_y}


def union_bbox(a, b):
    if not a:
        return b
    if not b:
        return a
    x = min(a['x'], b['x'])
    y = min(a['y'], b['y'])
    return {
        'x': x,
        'y': y,
        'width': max(a['x'] + a['width'], b['x'] + b['width']) - x,
        'height': max(a['y'] + a['height'], b['y'] + b['height']) - y,
    }


def union_bboxes(boxes):
    acc = None
    for b in boxes:
        acc = union_bbox(acc, b)
    return acc


# Sheet data, not drawn process: equipment-list rows and table cells sit in
# the sheet's tables, and labels / notes are loose text whose box can be a
# table cell. They stay members of a process, but a process or node box
# grown over them covers the title block instead of the pipework.
SHEET_DATA_CATEGORIES = frozenset({'equipment_list_row', 'label', 'note'})


def drawn_bbox(entities):
    """Union of the entities' boxes, leaving out sheet data; falls back to
    every box when the members are all sheet data."""
    entities = list(entities)
    drawn = [e for e in entities if e['category'] not in SHEET_DATA_CATEGORIES]
    return union_bboxes(e['bbox'] for e in (drawn or entities))


def bbox_area(b):
    return max(0, b['width']) * max(0, b['height']) if b else 0


def intersection_area(a, b):
    if not a or not b:
        return 0
    w = min(a['x'] + a['width'], b['x'] + b['width']) - max(a['x'], b['x'])
    h = min(a['y'] + a['height'], b['y'] + b['height']) - max(a['y'], b['y'])
    return w * h if w > 0 and h > 0 else 0


def iou(a, b):
    inter = intersection_area(a, b)
    if inter == 0:
        return 0
    return inter / (bbox_area(a) + bbox_area(b) - inter)


def containment(inner, outer):
    """How much of `inner` lies inside `outer` (0..1). Better than IoU when the
    two boxes are very different sizes — e.g. a tag label inside an equipment
    outline, where IoU stays near zero however well-contained it is."""
    a = bbox_area(inner)
    return 0 if a == 0 else intersection_area(inner, outer) / a


def bbox_center(b):
    return {'x': b['x'] + b['width'] / 2, 'y': b['y'] + b['height'] / 2}


def bbox_gap(a, b):
    """Edge-to-edge gap between two boxes; 0 when they touch or overlap."""
    dx = max(0, max(a['x'], b['x']) - min(a['x'] + a['width'], b['x'] + b['width']))
    dy = max(0, max(a['y'], b['y']) - min(a['y'] + a['height'], b['y'] + b['height']))
    return math.hypot(dx, dy)


def distance(a, b):
    return math.hypot(a['x'] - b['x'], a['y'] - b['y'])


def point_in_bbox(p, b, pad=0):
    return b['x'] - pad <= p['x'] <= b['x'] + b['width'] + pad and b['y'] - pad <= p['y'] <= b['y'] + b['height'] + pad


def sort_key_top_left(item):
    """Reading order the JS pipeline uses everywhere: by bbox top, then left."""
    return (item['bbox']['y'], item['bbox']['x'])
