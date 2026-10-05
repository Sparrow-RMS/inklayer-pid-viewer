"""Step 1 — exhaustive extraction.

Produces, per page: the exact PDF text layer, the exact stroked vector
geometry, a VLM-read inventory of every entity on the sheet, and the
drawing-level metadata. Entities are read tile by tile and then merged,
because the tiles deliberately overlap.

The coordinates that matter come from the text layer, not from the model: an
entity that anchored its tag to real text carries a `tagBbox` accurate to the
PDF's own precision. That is what makes the annotation land in the right place.

Split like every stage: `collect_page` is the paid half (renders tiles, calls
the model); `assemble_page` is the free, pure half (merging, anchoring,
de-duplication), replayable over the cached raw responses.
"""
import asyncio
import math
import re

from ..dsu import DisjointSet
from ..geometry import (
    bbox_center, bbox_from_points, bbox_gap, clamp_bbox, containment, distance, iou,
    point_in_bbox, sort_key_top_left, union_bbox, union_bboxes,
)
from ..jobs import now_iso
from ..llm import image_part, map_limit, structured_call, text_part
from ..pdf import extract_text_items, extract_vector_segments, page_geometry, render_region, to_data_url
from ..tiling import bbox_from_tile, bbox_to_tile, plan_tiles, point_from_tile, text_items_in_tile
from .prompts import PAGE_META_SYSTEM_PROMPT, TILE_SYSTEM_PROMPT, format_text_items
from .schema import PAGE_META_SCHEMA, TILE_SCHEMA

CATEGORY_ABBREV = {
    'equipment': 'eq',
    'instrument': 'in',
    'valve': 'vl',
    'line': 'ln',
    'fitting': 'ft',
    'connector': 'cn',
    'note': 'nt',
    'label': 'lb',
    'equipment_list_row': 'row',
    'interlock_ref': 'il',
}

# Two sightings of one tagged item can only be the same item if they are also
# near each other — otherwise a tag repeated as a cross-reference elsewhere on
# the sheet would drag both into one entity with a bbox spanning the gap.
# Sized a little above the tile overlap margin, which is the largest genuine
# separation two sightings of one symbol can have.
SAME_TAG_MAX_GAP = 0.06
CROSS_TILE_MERGE_IOU = 0.45
# IoU punishes a pair of boxes that disagree on extent even when one all but
# sits inside the other — which is exactly how two tiles describe one
# exchanger, one boxing the shell and the other the shell plus its nozzles.
CROSS_TILE_MERGE_CONTAINMENT = 0.6
# Below this the model is telling us it is guessing. Kept, never dropped —
# step 1's job is coverage — but flagged so a reviewer sees it first.
LOW_CONFIDENCE = 0.6

# Control valves, actuated valves and PSVs carry both a valve body and an
# instrument-like actuator/bubble on the same symbol, and different tiles
# were observed calling the identical tag "valve" in one sighting and
# "instrument" in another (TV-1257004, PSV-1257001, HV-1257022, ...).
AMBIGUOUS_CATEGORY_PAIRS = {'instrument|valve'}

# Text shaped like an equipment/instrument tag ("P-101A", "FT 1234") or a line
# number ('8"-CR-2203-A1'). Used to decide whether a text item nobody claimed
# is a real missed item or just prose.
TAG_PATTERN = re.compile(r'[A-Z]{1,5}[-\s]?[0-9]{2,5}[A-Z]?')
LINE_NUMBER_PATTERN = re.compile(r'[0-9]{1,2}"?\s*-\s*[A-Z]{1,4}\s*-\s*[0-9]{2,6}')
LETTERS_ONLY = re.compile(r'[A-Z]{1,5}')
DIGITS_ONLY = re.compile(r'[0-9]{1,5}[A-Z]?')


def normalize_tag(tag):
    """Tags are compared with separators and case stripped, so "P-101 A",
    "P-101A" and "p101a" are one pump. Distinct tags stay distinct: the only
    characters removed are spacing and punctuation."""
    return re.sub(r'[\s\-_./\\]', '', tag.upper()) if tag else None


def looks_like_tag(s):
    s = s.strip()
    return bool(TAG_PATTERN.fullmatch(s) or LINE_NUMBER_PATTERN.match(s))


def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _pair_key(a, b):
    return '|'.join(sorted([a, b]))


# ─────────────────────────────────────────────────────────────── paid half

async def extract_document(doc, options=None):
    options = options or {}
    file_name = options.get('fileName', 'document.pdf')
    page_indexes = options.get('pageIndexes')
    on_progress = options.get('onProgress') or (lambda event: None)

    targets = [i for i in range(doc.page_count) if not page_indexes or i in page_indexes]
    collected = []
    for page_index in targets:
        collected.append(await collect_page(doc[page_index], page_index, {**options, 'onProgress': on_progress}))

    # `raw` is the cache `assemble_document` replays; the caller writes it to a
    # separate artifact rather than inlining it, since it duplicates the text
    # and vector layers.
    return {**assemble_document(collected, file_name, doc.page_count), 'raw': collected}


async def collect_page(page, page_index, opts):
    """The paid half: everything that needs the PDF and the model.

    Kept separate from `assemble_page` so the raw tile responses can be cached
    and the deterministic half re-run against them. Merging, anchoring and
    de-duplication are exactly the parts worth tuning, and tuning them should
    not cost another full pass over the sheet.
    """
    concurrency = opts.get('concurrency', 4)
    tile_px = opts.get('tilePx', 1600)
    page_px = opts.get('pagePx', 2200)
    on_progress = opts['onProgress']

    geometry = page_geometry(page, page_index)
    text_items = extract_text_items(page, page_index)
    vector_segments = extract_vector_segments(page, page_index) if opts.get('includeVectors', True) else []

    plan = plan_tiles(geometry, opts.get('tiling') or {})
    on_progress({
        'phase': 'page-start', 'pageIndex': page_index, 'tiles': len(plan['tiles']),
        'textItems': len(text_items), 'vectorSegments': len(vector_segments),
    })

    # The whole-page metadata pass and the tile passes are independent, so the
    # page pass runs alongside the tiles rather than serially before them.
    async def meta():
        try:
            return await _read_page_meta(page, page_index, text_items, page_px)
        except Exception as err:  # noqa: BLE001 — one failed call must not sink the page
            on_progress({'phase': 'page-meta-failed', 'pageIndex': page_index, 'error': str(err)})
            return {'error': str(err)}

    async def tile_worker(tile, _index):
        try:
            result = await _read_tile(page, tile, text_items, tile_px)
            on_progress({'phase': 'tile-done', 'pageIndex': page_index, 'tile': tile['id'], 'entities': len(result['entities'])})
            return {'tile': tile, **result}
        except Exception as err:  # noqa: BLE001
            on_progress({'phase': 'tile-failed', 'pageIndex': page_index, 'tile': tile['id'], 'error': str(err)})
            return {'tile': tile, 'entities': [], 'tileNotes': '', 'error': str(err)}

    meta_task = asyncio.ensure_future(meta())
    tile_results = await map_limit(plan['tiles'], concurrency, tile_worker)
    drawing_meta = await meta_task

    return {
        'pageIndex': page_index, 'geometry': geometry, 'textItems': text_items, 'vectorSegments': vector_segments,
        'plan': plan, 'tileResults': tile_results, 'drawingMeta': drawing_meta,
    }


async def _read_tile(page, tile, text_items, tile_px):
    png = render_region(page, tile['region'], target_px=tile_px)['png']
    tile_text = text_items_in_tile(text_items, tile)
    listing = format_text_items(tile_text, lambda bbox: bbox_to_tile(bbox, tile))
    r = tile['region']

    parsed = await structured_call(
        system=TILE_SYSTEM_PROMPT,
        schema=TILE_SCHEMA,
        schema_name='pid_tile_inventory',
        content=[
            text_part(
                f"Tile {tile['id']} — row {tile['row']}, column {tile['col']} of the sheet.\n"
                f"It covers the page region x {r['x']:.3f}–{r['x'] + r['width']:.3f}, "
                f"y {r['y']:.3f}–{r['y'] + r['height']:.3f} "
                f'(fractions of the whole sheet, origin top-left).'
            ),
            image_part(to_data_url(png)),
            text_part(
                'PDF text layer inside this tile — ids are what "textAnchorIds" must reference, '
                f'positions are tile-relative fractions of this image:\n{listing}'
            ),
        ],
    )
    return {'entities': parsed.get('entities') or [], 'tileNotes': parsed.get('tileNotes') or ''}


async def _read_page_meta(page, page_index, text_items, page_px):
    png = render_region(page, {'x': 0, 'y': 0, 'width': 1, 'height': 1}, target_px=page_px)['png']
    listing = format_text_items(text_items, lambda bbox: bbox)
    return await structured_call(
        system=PAGE_META_SYSTEM_PROMPT,
        schema=PAGE_META_SCHEMA,
        schema_name='pid_page_metadata',
        content=[
            text_part(f'Full P&ID sheet, page index {page_index}.'),
            image_part(to_data_url(png)),
            text_part(f'PDF text layer for the whole sheet (positions are page fractions, origin top-left):\n{listing}'),
        ],
    )


# ─────────────────────────────────────────────────────────────── free half

def assemble_document(collected, file_name, page_count):
    """Rebuilds the step 1 artifact from cached per-page collections. Pure."""
    pages = [assemble_page(c) for c in collected]
    return {
        'schemaVersion': 1,
        'step': 'step1-extract',
        'fileName': file_name,
        'pageCount': page_count,
        'generatedAt': now_iso(),
        'pages': pages,
        'totals': {
            'entities': sum(len(p['entities']) for p in pages),
            'textItems': sum(len(p['textItems']) for p in pages),
            'vectorSegments': sum(len(p['vectorSegments']) for p in pages),
        },
    }


def assemble_page(collected):
    """The free half: tile sightings -> one clean, de-duplicated inventory.

    Pure and deterministic, so it can be replayed over cached raw responses
    as often as the rules need adjusting. The repair passes edit entities in
    place, but those are built fresh here; the cached input is never touched.
    """
    page_index = collected['pageIndex']
    text_items = collected['textItems']
    plan = collected['plan']
    tile_results = collected['tileResults']
    drawing_meta = collected['drawingMeta']
    text_by_id = {t['id']: t for t in text_items}

    sightings = []
    for result in tile_results:
        for i, raw in enumerate(result['entities']):
            sighting = normalize_sighting(raw, result['tile'], text_by_id, i)
            if sighting:
                sightings.append(sighting)

    # Order matters: anchors must be made exclusive BEFORE merging, since
    # merge rule 1 treats a shared anchor as proof two sightings are the same
    # entity — and a stolen anchor would then fuse two different ones.
    stripped_anchors = enforce_anchor_exclusivity(sightings, text_by_id)
    entities = merge_sightings(sightings, page_index, text_by_id)

    # Split bubble tags ("FT" over "202") are reassembled first, so the two
    # passes that follow can recognise them as the single tags they are.
    composites = find_composite_tags(text_items)
    released_tags = release_foreign_composite_tags(entities, composites, text_by_id)
    attached_tags = attach_unclaimed_tags(entities, text_items, composites, text_by_id)

    # Last, so it only fires on text that survived every attempt to give it a
    # real owner.
    synthesized = synthesize_unclaimed_tags(entities, text_items, composites, drawing_meta, page_index)
    entities.extend(synthesized)
    entities.sort(key=sort_key_top_left)

    _resolve_connections(entities)

    return {
        **collected['geometry'],
        'tiling': {'cols': plan['cols'], 'rows': plan['rows'], 'overlap': plan['overlap'], 'tileCount': len(plan['tiles'])},
        'drawingMeta': drawing_meta,
        'entities': entities,
        'textItems': text_items,
        'vectorSegments': collected['vectorSegments'],
        'diagnostics': {
            'tileSightings': len(sightings),
            'mergedEntities': len(entities),
            # Tags one entity took from another, resolved back to the nearer one.
            'strippedAnchors': stripped_anchors,
            # Bubble tags an entity had absorbed from its neighbour, handed back.
            'releasedTags': released_tags,
            # Untagged entities that got their tag back from unclaimed text.
            'attachedTags': attached_tags,
            # Tag-shaped text the model never accounted for — the honest measure
            # of what this page's extraction missed.
            'synthesizedFromText': [{'id': e['id'], 'tag': e['tag']} for e in synthesized],
            'failedTiles': [{'tile': r['tile']['id'], 'error': r['error']} for r in tile_results if r.get('error')],
            'tileNotes': [{'tile': r['tile']['id'], 'note': r['tileNotes']} for r in tile_results if r.get('tileNotes')],
        },
    }


def normalize_sighting(raw, tile, text_by_id, index):
    """One tile-level entity -> page coordinates, with its text anchors resolved.

    The model's own bbox is an estimate; the anchored text boxes are exact. We
    keep both rather than collapsing them, because they answer different
    questions — where the SYMBOL is drawn versus where its TAG is printed.
    """
    tile_bbox = raw.get('bbox')
    if not tile_bbox or not _finite(tile_bbox.get('x')) or not _finite(tile_bbox.get('y')):
        return None

    symbol_bbox = clamp_bbox(bbox_from_tile(clamp_bbox(tile_bbox), tile))

    anchor_ids = []
    missing_anchors = []
    for tid in raw.get('textAnchorIds') or []:
        (anchor_ids if tid in text_by_id else missing_anchors).append(tid)
    tag_bbox = union_bboxes(text_by_id[t]['bbox'] for t in anchor_ids) if anchor_ids else None

    # A tag the model typed that appears nowhere in the text it anchored is a
    # transcription it invented or misread. Flagged, not dropped — on a
    # graphics-drawn tag there is no text layer entry to match against.
    tag_mismatch = False
    if raw.get('tag') and anchor_ids:
        anchored = normalize_tag(''.join(text_by_id[t]['str'] for t in anchor_ids))
        wanted = normalize_tag(raw['tag'])
        tag_mismatch = bool(wanted and anchored and wanted not in anchored)

    polyline = raw.get('polyline')
    route = (
        [point_from_tile(p, tile) for p in polyline]
        if raw.get('category') == 'line' and isinstance(polyline, list) and len(polyline) >= 2
        else None
    )

    tag = (raw.get('tag') or '').strip() or None
    attributes = raw.get('attributes')
    connects_to = raw.get('connectsTo')
    return {
        'uid': f"{tile['id']}#{index}",
        'tileId': tile['id'],
        'localId': raw.get('localId') if raw.get('localId') is not None else f"{tile['id']}-{index}",
        'category': raw.get('category'),
        'tag': tag,
        'type': raw.get('type') if raw.get('type') is not None else '',
        'description': raw.get('description') if raw.get('description') is not None else '',
        'attributes': [a for a in attributes if a and a.get('key') and a.get('value')] if isinstance(attributes, list) else [],
        'textAnchorIds': anchor_ids,
        'missingAnchors': missing_anchors,
        'tagMismatch': tag_mismatch,
        'symbolBbox': symbol_bbox,
        'tagBbox': tag_bbox,
        'route': route,
        'connectsTo': [c for c in connects_to if c] if isinstance(connects_to, list) else [],
        'confidence': raw['confidence'] if _finite(raw.get('confidence')) else 0.5,
    }


def merge_sightings(sightings, page_index, text_by_id):
    """Collapses the duplicate sightings the overlapping tiles produce.

    Three independent signals are unioned, strongest first:
      1. two sightings anchored to the SAME text item are the same thing — the
         text layer is ground truth, so this needs no distance check;
      2. same tag, close together on the page, and the same category (or a
         known-ambiguous pair such as instrument/valve);
      3. same category, different tiles, boxes substantially overlapping.
    Lines are exempt from the distance check on rule 2: one line number names
    one line, however far its run stretches across the sheet.
    """
    dsu = DisjointSet()
    for s in sightings:
        dsu.add(s['uid'])

    # 1. Shared exact text anchors.
    by_anchor = {}
    for s in sightings:
        for tid in s['textAnchorIds']:
            by_anchor.setdefault(f"{s['category']}|{tid}", []).append(s['uid'])
    for uids in by_anchor.values():
        for uid in uids[1:]:
            dsu.union(uids[0], uid)

    # 2. Same tag, spatially plausible, same or ambiguous-pair category.
    by_tag = {}
    for s in sightings:
        norm = normalize_tag(s['tag'])
        if norm:
            by_tag.setdefault(norm, []).append(s)
    for group in by_tag.values():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a['category'] != b['category'] and _pair_key(a['category'], b['category']) not in AMBIGUOUS_CATEGORY_PAIRS:
                    continue
                is_line = a['category'] == 'line' and b['category'] == 'line'
                if is_line or bbox_gap(a['symbolBbox'], b['symbolBbox']) <= SAME_TAG_MAX_GAP:
                    dsu.union(a['uid'], b['uid'])

    # 3. Look-alikes in the overlap band. Only sightings from DIFFERENT tiles —
    #    two symbols the same tile reported separately are two real symbols.
    #    Two tiles that both named the item, differently, are describing
    #    different items and are left alone.
    for i in range(len(sightings)):
        for j in range(i + 1, len(sightings)):
            a, b = sightings[i], sightings[j]
            if a['tileId'] == b['tileId'] or a['category'] != b['category']:
                continue
            tag_a, tag_b = normalize_tag(a['tag']), normalize_tag(b['tag'])
            if tag_a and tag_b and tag_a != tag_b:
                continue
            overlaps = (
                iou(a['symbolBbox'], b['symbolBbox']) >= CROSS_TILE_MERGE_IOU
                or max(containment(a['symbolBbox'], b['symbolBbox']), containment(b['symbolBbox'], a['symbolBbox'])) >= CROSS_TILE_MERGE_CONTAINMENT
            )
            if overlaps:
                dsu.union(a['uid'], b['uid'])

    by_uid = {s['uid']: s for s in sightings}
    counters = {}
    entities = []
    for uids in dsu.groups():
        group = [by_uid[u] for u in uids if u in by_uid]
        if not group:
            continue
        abbrev = CATEGORY_ABBREV.get(group[0]['category'], 'x')
        counters[abbrev] = counters.get(abbrev, 0) + 1
        entities.append(_combine_group(group, f'p{page_index}-{abbrev}-{counters[abbrev]}', page_index, text_by_id))
    entities.sort(key=sort_key_top_left)
    return entities


def _combine_group(group, entity_id, page_index, text_by_id):
    ranked = sorted(group, key=lambda s: -s['confidence'])
    best = ranked[0]
    category = best['category']

    text_anchor_ids = list(dict.fromkeys(t for s in group for t in s['textAnchorIds']))
    tag_bbox = union_bboxes(text_by_id[t]['bbox'] for t in text_anchor_ids) if text_anchor_ids else None

    # A line is one entity whose run may cross several tiles, so its geometry
    # is every sighting's route kept as separate polylines — joining them into
    # one would invent connections between disjoint pieces of the run.
    routes = [s['route'] for s in group if s['route']] if category == 'line' else []

    if category == 'line' and routes:
        symbol_bbox = union_bbox(bbox_from_points(p for r in routes for p in r), union_bboxes(s['symbolBbox'] for s in group))
    else:
        symbol_bbox = best['symbolBbox']

    bbox = clamp_bbox(union_bbox(symbol_bbox, tag_bbox) or symbol_bbox)
    descriptions = sorted((s['description'] for s in ranked), key=len, reverse=True)
    confidence = max(s['confidence'] for s in group)

    return {
        'id': entity_id,
        'page': page_index,
        'category': category,
        'tag': next((s['tag'] for s in ranked if s['tag']), None),
        'type': best['type'],
        'description': descriptions[0] if descriptions else '',
        'attributes': _merge_attributes(ranked),
        # The entity's overall extent, and the two things it is made of.
        'bbox': bbox,
        'symbolBbox': symbol_bbox,
        'tagBbox': tag_bbox,
        'routes': routes,
        # True when at least one coordinate came from the PDF's own text layer
        # rather than the model's eye — i.e. this entity's position is exact.
        'anchored': len(text_anchor_ids) > 0,
        'textAnchorIds': text_anchor_ids,
        'connectsTo': list(dict.fromkeys(c for s in group for c in s['connectsTo'])),
        'resolvedConnections': [],
        'confidence': confidence,
        'sightings': len(group),
        'sources': list(dict.fromkeys(s['tileId'] for s in group)),
        'flags': {
            'tagMismatch': any(s['tagMismatch'] for s in group),
            'invalidAnchors': list(dict.fromkeys(m for s in group for m in s['missingAnchors'])),
            # Disagreement between tiles about what this is — worth a human look.
            'conflictingTypes': len({s['type'] for s in group if s['type']}) > 1,
            'lowConfidence': confidence < LOW_CONFIDENCE,
            # Set only on entities invented from unclaimed text layer content.
            'synthesizedFromText': False,
        },
    }


def enforce_anchor_exclusivity(sightings, text_by_id):
    """One text item belongs to one entity.

    Left unchecked, a confident sighting hoovers up its neighbour's tag too —
    observed exactly this way: the TT-204 bubble anchored "TIC", "204" AND its
    own "204", which erased TIC-204 from the inventory entirely. Contested
    anchors are resolved to the nearest entity and stripped from the rest.

    Scoped WITHIN a tile: the same text legitimately anchors one sighting per
    tile, and it is precisely those cross-tile repeats that merge rule 1 uses
    to recognise a duplicate.
    """
    contested = {}
    for s in sightings:
        for tid in s['textAnchorIds']:
            contested.setdefault((s['tileId'], tid), []).append(s)

    stripped = []
    for (_tile_id, text_id), claimants in contested.items():
        if len(claimants) < 2:
            continue
        center = bbox_center(text_by_id[text_id]['bbox'])
        winner = claimants[0]
        best_score = math.inf
        for s in claimants:
            # Distance from the text to the symbol, zero when the text sits
            # inside it; confidence breaks a tie between equally placed claimants.
            d = 0 if point_in_bbox(center, s['symbolBbox']) else distance(center, bbox_center(s['symbolBbox']))
            score = d - s['confidence'] * 1e-6
            if score < best_score:
                best_score = score
                winner = s
        for s in claimants:
            if s is winner:
                continue
            s['textAnchorIds'] = [t for t in s['textAnchorIds'] if t != text_id]
            stripped.append({
                'tile': s['tileId'], 'text': text_by_id[text_id]['str'],
                'from': s['tag'] if s['tag'] is not None else s['type'],
                'to': winner['tag'] if winner['tag'] is not None else winner['type'],
            })
    return stripped


def find_composite_tags(text_items):
    """Reassembles the split tags inside instrument bubbles.

    An ISA bubble prints its function letters above its loop number, and a
    PDF stores those as two unrelated text runs — "FT" and "202". Neither
    looks like a tag alone, so a whole instrument can disappear without
    anything noticing. This pairs a letters-only run with the digits run
    directly beneath it, yielding "FT-202" with the union of the two boxes.
    """
    letters = [t for t in text_items if LETTERS_ONLY.fullmatch(t['str'].strip())]
    digits = [t for t in text_items if DIGITS_ONLY.fullmatch(t['str'].strip())]
    composites = []
    consumed = set()

    for top in letters:
        tb = top['bbox']
        top_center = tb['x'] + tb['width'] / 2
        best = None
        best_gap = math.inf
        for bottom in digits:
            if bottom['id'] in consumed:
                continue
            bb = bottom['bbox']
            # Directly beneath, within about one line of space...
            gap = bb['y'] - (tb['y'] + tb['height'])
            if gap < -tb['height'] * 0.5 or gap > tb['height'] * 1.5:
                continue
            # ...roughly centred on the same axis, and the same size of text.
            bottom_center = bb['x'] + bb['width'] / 2
            if abs(bottom_center - top_center) > max(tb['width'], bb['width']):
                continue
            if abs(bottom['fontSize'] - top['fontSize']) > top['fontSize'] * 0.35:
                continue
            if gap < best_gap:
                best_gap = gap
                best = bottom
        if not best:
            continue
        consumed.add(best['id'])
        composites.append({
            'str': f"{top['str'].strip()}-{best['str'].strip()}",
            'parts': [top['id'], best['id']],
            'bbox': union_bbox(tb, best['bbox']),
        })
    return composites


def release_foreign_composite_tags(entities, composites, text_by_id):
    """Takes back a bubble tag an entity absorbed that is not its own.

    Seen in practice: the FIC-202 bubble anchored "FT", "202", "FIC" and "202"
    — all four fragments of two adjacent bubbles — which deleted FT-202 from
    the inventory. Exclusivity cannot catch this (only one entity ever claimed
    them); comparing the composite each anchor set spells against the
    entity's own tag can.
    """
    released = []
    for entity in entities:
        if not entity.get('tag'):
            continue
        own = normalize_tag(entity['tag'])
        anchors = set(entity['textAnchorIds'])
        for composite in composites:
            if normalize_tag(composite['str']) == own:
                continue
            if not all(p in anchors for p in composite['parts']):
                continue
            entity['textAnchorIds'] = [t for t in entity['textAnchorIds'] if t not in composite['parts']]
            released.append({'from': entity['id'], 'tag': composite['str']})
        if any(r['from'] == entity['id'] for r in released):
            _recompute_anchored_geometry(entity, text_by_id)
    return released


def _recompute_anchored_geometry(entity, text_by_id):
    """Re-derives the parts of an entity that came from its text anchors,
    after that anchor set has been edited."""
    if text_by_id is None:
        return
    boxes = [text_by_id[t]['bbox'] for t in entity['textAnchorIds'] if t in text_by_id]
    entity['tagBbox'] = union_bboxes(boxes) if boxes else None
    entity['anchored'] = len(boxes) > 0
    entity['bbox'] = clamp_bbox(union_bbox(entity['symbolBbox'], entity['tagBbox']) or entity['symbolBbox'])


def attach_unclaimed_tags(entities, text_items, composites, text_by_id):
    """Gives an untagged entity the tag printed on it.

    After the passes above, a tag nobody owns is free. If it sits on or beside
    an untagged entity, it is that entity's tag. Composite bubble tags are
    offered first: "FT-202" is a better answer than the bare fragment "FT".
    """
    claimed = {t for e in entities for t in e['textAnchorIds']}
    candidates = _tag_candidates(text_items, composites, claimed)
    attached = []

    for candidate in candidates:
        if any(p in claimed for p in candidate['parts']):
            continue
        center = bbox_center(candidate['bbox'])
        best = None
        best_distance = math.inf
        for entity in entities:
            if entity.get('tag') or entity['category'] in ('note', 'label'):
                continue
            d = 0 if containment(candidate['bbox'], entity['symbolBbox']) >= 0.5 else distance(center, bbox_center(entity['symbolBbox']))
            # Beyond roughly a symbol's width away it is somebody else's tag.
            if d > 0.04:
                continue
            if d < best_distance:
                best_distance = d
                best = entity
        if not best:
            continue
        best['tag'] = candidate['str']
        best['textAnchorIds'].extend(candidate['parts'])
        _recompute_anchored_geometry(best, text_by_id)
        claimed.update(candidate['parts'])
        attached.append({'entity': best['id'], 'tag': candidate['str']})
    return attached


def _tag_candidates(text_items, composites, claimed):
    """Unclaimed tag-shaped text, composites first so a split bubble tag is
    preferred over either of the fragments it is made of."""
    out = []
    in_composite = set()
    for composite in composites:
        if any(p in claimed for p in composite['parts']):
            continue
        out.append(composite)
        in_composite.update(composite['parts'])
    for text in text_items:
        if text['id'] in claimed or text['id'] in in_composite:
            continue
        if not looks_like_tag(text['str']):
            continue
        out.append({'str': text['str'], 'parts': [text['id']], 'bbox': text['bbox']})
    return out


def _js_string(v):
    return 'null' if v is None else str(v)


def synthesize_unclaimed_tags(entities, text_items, composites, drawing_meta, page_index):
    """Nothing in the text layer is allowed to vanish.

    Any tag-shaped text still unclaimed after the passes above is something
    the model did not report — a real miss. Since the PDF gives us its exact
    position, it is recorded as a low-confidence entity flagged
    `synthesizedFromText` rather than dropped: a reviewer would far rather see
    a stub they can correct than discover later that the sheet had an
    instrument nobody listed.

    Text the whole-sheet pass already transcribed (title block, notes, legend)
    is excluded — that content is catalogued in `drawingMeta`, not here.
    """
    claimed = {t for e in entities for t in e['textAnchorIds']}
    meta = drawing_meta or {}
    meta_values = [
        *(kv.get('value') for kv in meta.get('titleBlock') or []),
        *(n.get('text') for n in meta.get('notes') or []),
        *(v for kv in meta.get('legend') or [] for v in (kv.get('key'), kv.get('value'))),
        *(kv.get('value') for kv in meta.get('revisions') or []),
        *(meta.get('referencedDrawings') or []),
    ]
    meta_text = list(dict.fromkeys(_js_string(v).upper().strip() for v in meta_values))
    meta_set = set(meta_text)

    out = []
    for text in _tag_candidates(text_items, composites, claimed):
        upper = text['str'].upper().strip()
        if upper in meta_set or any(upper in m for m in meta_text):
            continue
        n = len(out) + 1
        out.append({
            'id': f'p{page_index}-syn-{n}',
            'page': page_index,
            'category': 'label',
            'tag': text['str'],
            'type': 'unidentified tagged item',
            'description': f'Tag "{text["str"]}" is printed on the sheet at this position but no symbol was reported for it. '
                           'Recorded from the PDF text layer so it is not lost; needs review.',
            'attributes': [],
            'bbox': text['bbox'],
            'symbolBbox': text['bbox'],
            'tagBbox': text['bbox'],
            'routes': [],
            'anchored': True,
            'textAnchorIds': list(text['parts']),
            'connectsTo': [],
            'resolvedConnections': [],
            'confidence': 0.3,
            'sightings': 0,
            'sources': [],
            'flags': {'tagMismatch': False, 'invalidAnchors': [], 'conflictingTypes': False, 'lowConfidence': True, 'synthesizedFromText': True},
        })
    return out


def _merge_attributes(ranked):
    """Later (lower-confidence) sightings fill gaps but never overwrite a value
    a more confident sighting already gave for the same key."""
    out = {}
    for sighting in ranked:
        for a in sighting['attributes']:
            out.setdefault(a['key'], a['value'])
    return [{'key': k, 'value': v} for k, v in out.items()]


def _resolve_connections(entities):
    """Turns the model's free-text `connectsTo` (tags, or tile-local ids) into
    real entity ids where they can be matched. Unresolvable references are
    left in `connectsTo` for step 2 to reason about rather than discarded."""
    by_tag = {}
    for e in entities:
        norm = normalize_tag(e.get('tag'))
        if norm and norm not in by_tag:
            by_tag[norm] = e['id']
    for entity in entities:
        resolved = []
        for ref in entity['connectsTo']:
            target = by_tag.get(normalize_tag(ref))
            if target and target != entity['id'] and target not in resolved:
                resolved.append(target)
        entity['resolvedConnections'] = resolved
