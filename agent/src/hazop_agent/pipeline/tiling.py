"""Tile planning for exhaustive extraction.

A P&ID page handed to a VLM whole loses its small content — instrument
bubbles, valve tags, line numbers are only a few pixels tall once an A1 sheet
is squeezed into one image. So each page is cut into overlapping tiles, each
rendered at high resolution and read separately.

Tiles OVERLAP on purpose: an entity straddling a cut would otherwise be seen
only in halves and described wrongly by both neighbours. The duplicates this
creates are the extractor's problem to merge, which is the cheaper trade.
"""
import math

# Tiles are sized so each one covers roughly this fraction of the page's
# longest side; the grid is derived from the page's own aspect ratio so a wide
# A0 sheet gets more columns than rows rather than square-ish tiles.
DEFAULT_TILE_SPAN = 1 / 3


def js_round(v):
    """Math.round semantics (halves round up), not Python's banker's rounding."""
    return math.floor(v + 0.5)


def plan_tiles(page_geom, options=None):
    options = options or {}
    overlap = options.get('overlap') if options.get('overlap') is not None else 0.12
    tile_span = options.get('tileSpan') or DEFAULT_TILE_SPAN
    aspect = page_geom['width'] / page_geom['height']

    # Aim for tiles of similar on-page area whichever way the sheet is turned.
    cols = options.get('cols') or max(1, js_round((1 if aspect >= 1 else aspect) / tile_span))
    rows = options.get('rows') or max(1, js_round((1 / aspect if aspect >= 1 else 1) / tile_span))
    cols = max(1, min(8, int(cols)))
    rows = max(1, min(8, int(rows)))

    # Step between tile origins; each tile is one step wide plus the overlap
    # margin, so neighbours share `overlap` of a step on every shared edge.
    step_x = 1 / cols
    step_y = 1 / rows
    pad_x = step_x * overlap
    pad_y = step_y * overlap

    tiles = []
    for row in range(rows):
        for col in range(cols):
            x = max(0, col * step_x - pad_x)
            y = max(0, row * step_y - pad_y)
            right = min(1, (col + 1) * step_x + pad_x)
            bottom = min(1, (row + 1) * step_y + pad_y)
            tiles.append({
                'id': f"p{page_geom['pageIndex']}-tile-{row}-{col}",
                'page': page_geom['pageIndex'],
                'row': row,
                'col': col,
                # Region of the page this tile shows, normalized page fractions.
                'region': {'x': x, 'y': y, 'width': right - x, 'height': bottom - y},
            })

    return {'cols': cols, 'rows': rows, 'overlap': overlap, 'tiles': tiles}


def bbox_to_tile(bbox, tile):
    """Page-normalized bbox -> tile-relative (0..1 within the tile)."""
    r = tile['region']
    return {
        'x': (bbox['x'] - r['x']) / r['width'],
        'y': (bbox['y'] - r['y']) / r['height'],
        'width': bbox['width'] / r['width'],
        'height': bbox['height'] / r['height'],
    }


def bbox_from_tile(bbox, tile):
    """Tile-relative bbox -> page-normalized. The inverse of bbox_to_tile."""
    r = tile['region']
    return {
        'x': r['x'] + bbox['x'] * r['width'],
        'y': r['y'] + bbox['y'] * r['height'],
        'width': bbox['width'] * r['width'],
        'height': bbox['height'] * r['height'],
    }


def point_from_tile(point, tile):
    r = tile['region']
    return {'x': r['x'] + point['x'] * r['width'], 'y': r['y'] + point['y'] * r['height']}


def text_items_in_tile(text_items, tile, min_containment=0.5):
    """Text items visible in this tile. `min_containment` keeps a label that is
    mostly inside; one clipped at the very edge belongs to the neighbour."""
    r = tile['region']
    rx1 = r['x'] + r['width']
    ry1 = r['y'] + r['height']
    out = []
    for item in text_items:
        b = item['bbox']
        w = min(b['x'] + b['width'], rx1) - max(b['x'], r['x'])
        h = min(b['y'] + b['height'], ry1) - max(b['y'], r['y'])
        if w <= 0 or h <= 0:
            continue
        area = max(b['width'] * b['height'], 1e-9)
        if (w * h) / area >= min_containment:
            out.append(item)
    return out
