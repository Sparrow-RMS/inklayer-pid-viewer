"""The pipe network, traced from the PDF's own drawn geometry.

Step 1's model reads symbols tile by tile, so the pipes it reports stop at
tile edges, skip dense areas and follow approximate routes. The PDF already
holds every pipe exactly, as stroked segments; this module follows them
across the whole page with no model:

1. keep straight, orthogonal strokes of the pipe weights, outside the drawing
   frame and the notes column, that belong to OPEN paths: symbol outlines
   (connector pentagons, instrument boxes, vessel bodies) use the same stroke
   as pipes but are drawn as closed shapes. Equipment boxes from step 1 are a
   padded backstop for vessel walls drawn open;
2. join collinear pieces across small gaps into RUNS: a pipe is drawn broken
   where another crosses it, around labels and around inline valves;
3. drop runs that are dash-dot boundaries (a gap every few points) or hatched
   walls (a bund drawn as double lines with diagonal hatching);
4. connect runs at corners, at T-junctions (a run's end on another run's
   middle; a plain crossing is not a connection) and through the symbols
   they meet, into SPANS between stops (a valve, equipment, connector,
   junction or dead end) and PIPES: spans chained through valves and fittings
   up to equipment, connectors, junctions or dead ends;
5. read flow direction from the drawn arrowheads (a lone slender triangle on
   a run; valve bowties are triangle pairs) and from off-page connectors
   (a pipe meeting the point of the pentagon is an inlet), and line numbers
   from the text layer beside each pipe.

Pure apart from `trace_page`, which reads the PDF page. Coordinates in the
output are page fractions, top-left origin, like everything else.
"""
import math
import re
from collections import defaultdict

from .dsu import DisjointSet
from .pdf import _normalizer

# Stroke widths (pt) of pipes on the sheets seen so far: 0.48 normal, 0.72 revised.
PIPE_WIDTHS = (0.48, 0.72)
# A sheet with no stroke wider than a hairline (some AutoCAD plots) cannot
# tell pipe from detail by weight; its pipes are the hairlines themselves.
HAIRLINE_WIDTHS = (0.0,)
WIDTH_TOL = 0.03
AXIS_TOL = 0.6       # pt off-axis a stroke may be and still count as straight
JOIN_GAP = 14.0      # pt: largest straight-ahead gap bridged unconditionally (crossings, labels)
SYMBOL_GAP = 40.0    # pt: a longer gap is bridged when symbol strokes sit in it, on the line
SYMBOL_MARKS = 2     # strokes needed in such a gap (valve bowtie, flanges, strainer)
SYMBOL_ACROSS = 8.0  # pt either side of the line a symbol stroke may sit
LANE_TOL = 1.0       # pt sideways two collinear pieces may sit and still be one run
TOUCH = 2.5          # pt: end-to-run or end-to-symbol contact
NOZZLE_REACH = 8.0   # pt: a pipe end or valve this close to equipment is on its nozzle (stub + flange)
MIN_RUN = 12.0       # pt: shorter runs are symbol detail, not pipe
DASH_GAPS = 4        # a run broken this many times...
DASH_DENSITY = 2.5   # ...at more than this many gaps per 100 pt is a dash-dot line
HATCH_REACH = 20.0   # pt: diagonal hatching this close to a run makes it a wall
HATCH_DENSITY = 3.0  # parallel hatch strokes per 100 pt of run...
HATCH_MIN_STROKES = 10 # ...and at least this many, on a run at least
HATCH_MIN_RUN = 120.0   # this long; short pipe pieces between valves sit beside bowties
STUB_MAX = 25.0      # pt: an isolated span shorter than this, touching nothing, is symbol detail
DRAIN_MAX = 40.0     # pt: a dead-end span shorter than this is a drain / vent / capped stub
HATCH_STROKE_MAX = 40.0  # pt: diagonal strokes up to this long can be hatching
SHELL_MAX = 0.25      # share of the page an equipment shell may span
SYMBOL_OUTLINE_MAX = 30.0  # pt: a closed shape thinner than this is a symbol, not pipe
FRAME_SPAN = 0.8     # a rule spanning this share of the page is frame / column furniture
ARROW_SIZE = (1.5, 9.0)  # pt: arrowhead extent
LINE_LABEL_REACH = 16.0  # pt from a pipe a line-number label may sit
LINE_MATCH = 0.004   # page fractions: a traced point this close to a model line runs along it

# Step 1 boxes used as a backstop against symbol strokes drawn as open paths,
# with how far (pt) to pad each: vessel walls sit just outside the model's box.
# Notes are left out: they are text whose boxes are loose estimates, and a
# note placed beside a pipe would otherwise cut it.
BODY_PADS = {'equipment': 8.0, 'instrument': 2.0, 'equipment_list_row': 2.0}
# A pipe ends at these; it continues through anything else it meets (valves, fittings).
TERMINAL_CATEGORIES = {'equipment', 'connector'}
LINE_NUMBER = re.compile(r'^[A-Z]{1,4}-\d{2,3}-[A-Z0-9]{2,4}(-[A-Za-z0-9]{1,3})?-[A-Za-z]?-?\d{4,}$|^[A-Z]{1,4}-\d{2,3}-[A-Z0-9]{2,4}-[a-z]-\d{4,}$')


# ─────────────────────────────────────────────────────────── reading the PDF

def _visible(path, matrix):
    """A `get_drawings()` path moved from unrotated page space into the
    visible (rotation-applied) page, the space W, H and step 1's boxes use."""
    items = []
    for it in path['items']:
        if it[0] in ('l', 'c'):
            items.append((it[0], *(p * matrix for p in it[1:])))
        elif it[0] == 're':
            items.append((it[0], (it[1] * matrix).normalize(), *it[2:]))
        elif it[0] == 'qu':
            items.append((it[0], it[1] * matrix, *it[2:]))
        else:
            items.append(it)
    return {**path, 'items': items, 'rect': (path['rect'] * matrix).normalize()}


def trace_page(page, page_index, entities, text_items):
    """Reads the page's strokes and builds the network. The only impure call."""
    _, matrix = _normalizer(page)
    W, H = page.rect.width, page.rect.height
    # AutoCAD plots SHX text as strokes and marks each run with a hidden
    # "AutoCAD SHX Text" annotation; strokes inside one are lettering, not pipe.
    lettering = [(a.rect * matrix).normalize() + (-1, -1, 1, 1) for a in page.annots() or []
                 if (a.info.get('title') or '').startswith('AutoCAD SHX Text')]

    def in_lettering(*pts):
        return any(all(r.contains(p) for p in pts) for r in lettering)

    straight, diagonals, triangles, outlines = [], [], [], []
    for n_path, path in enumerate(page.get_drawings()):
        # get_drawings() is in unrotated space; on a /Rotate page it would
        # otherwise be traced transposed against everything else.
        path = _visible(path, matrix)
        if lettering and in_lettering(path['rect'].tl, path['rect'].br):
            continue
        width = path.get('width') or 0
        color = path.get('color') or ()
        red = len(color) >= 3 and color[0] > 0.6 and color[1] < 0.35 and color[2] < 0.35
        lines = [it for it in path['items'] if it[0] == 'l']
        closed = bool(path.get('closePath')) or any(it[0] in ('re', 'qu') for it in path['items']) or (
            len(lines) >= 3 and math.hypot(lines[0][1].x - lines[-1][2].x, lines[0][1].y - lines[-1][2].y) < 0.5)
        # A symbol outline is closed AND thin or small (connector pentagons,
        # instrument and tag boxes). A large closed polyline can be pipework
        # drawn as one CAD object.
        symbol = closed and min(path['rect'].width, path['rect'].height) < SYMBOL_OUTLINE_MAX
        if closed:
            r = path['rect']
            poly = [(it[1].x, it[1].y) for it in path['items'] if it[0] in ('l', 'c')]
            outlines.append({'path': n_path, 'box': (r.x0, r.y0, r.x1, r.y1), 'thin': symbol, 'poly': poly})
        if 2 <= len(lines) <= 3 and len(lines) == len(path['items']):
            r = path['rect']
            if ARROW_SIZE[0] <= max(r.width, r.height) <= ARROW_SIZE[1]:
                pts = [(lines[0][1].x, lines[0][1].y), (lines[0][2].x, lines[0][2].y), (lines[1][2].x, lines[1][2].y)]
                triangles.append({'pts': pts, 'filled': bool(path.get('fill'))})
        for it in path['items']:
            if it[0] == 'c':  # curve: symbol detail (ball valve, bellows, bubble), never pipe
                diagonals.append(((it[1].x + it[4].x) / 2, (it[1].y + it[4].y) / 2, None))
        for it in lines:
            a, b = it[1], it[2]
            if abs(a.y - b.y) <= AXIS_TOL and abs(a.x - b.x) > 0.5:
                straight.append({'o': 'H', 'c': (a.y + b.y) / 2, 'lo': min(a.x, b.x), 'hi': max(a.x, b.x), 'w': width, 'red': red, 'symbol': symbol, 'path': n_path})
            elif abs(a.x - b.x) <= AXIS_TOL and abs(a.y - b.y) > 0.5:
                straight.append({'o': 'V', 'c': (a.x + b.x) / 2, 'lo': min(a.y, b.y), 'hi': max(a.y, b.y), 'w': width, 'red': red, 'symbol': symbol, 'path': n_path})
            elif math.hypot(a.x - b.x, a.y - b.y) < HATCH_STROKE_MAX:
                angle = round(math.degrees(math.atan2(b.y - a.y, b.x - a.x)) % 180 / 10)
                diagonals.append(((a.x + b.x) / 2, (a.y + b.y) / 2, angle))
    return build_network(page_index, W, H, straight, diagonals, triangles, entities, text_items, outlines)


# ─────────────────────────────────────────────────────────────── pure core

def _box(e, W, H, pad=0.0):
    b = e.get('symbolBbox') or e['bbox']
    return (b['x'] * W - pad, b['y'] * H - pad, (b['x'] + b['width']) * W + pad, (b['y'] + b['height']) * H + pad)


def _run_points(r):
    return ((r['lo'], r['c']), (r['hi'], r['c'])) if r['o'] == 'H' else ((r['c'], r['lo']), (r['c'], r['hi']))


def _in_box(pt, bx):
    return bx[0] <= pt[0] <= bx[2] and bx[1] <= pt[1] <= bx[3]


def _seg_inside(s, bx):
    a, b = _run_points(s)
    return _in_box(a, bx) and _in_box(b, bx)


def _frame(straight, W, H):
    """The usable drawing area: inside the frame and left of a full-height
    notes / title-block column rule, if the sheet has one."""
    tall = sorted(s['c'] for s in straight if s['o'] == 'V' and s['hi'] - s['lo'] > FRAME_SPAN * H)
    wide = sorted(s['c'] for s in straight if s['o'] == 'H' and s['hi'] - s['lo'] > FRAME_SPAN * W)
    x0 = max([x for x in tall if x < 0.1 * W], default=0.0)
    x1 = min([x for x in tall if x > 0.6 * W], default=W)
    y0 = max([y for y in wide if y < 0.1 * H], default=0.0)
    y1 = min([y for y in wide if y > 0.9 * H], default=H)
    return (x0 + 1, y0 + 1, x1 - 1, y1 - 1)


def _join(straight):
    """Collinear pieces -> runs, bridging gaps up to JOIN_GAP; records the gaps."""
    runs = []
    for o in ('H', 'V'):
        pieces = sorted((s for s in straight if s['o'] == o), key=lambda s: s['c'])
        lanes, cur = [], []
        for s in pieces:
            if cur and s['c'] - cur[-1]['c'] > LANE_TOL:
                lanes.append(cur)
                cur = []
            cur.append(s)
        if cur:
            lanes.append(cur)
        for lane in lanes:
            lane.sort(key=lambda s: s['lo'])
            c = sum(s['c'] for s in lane) / len(lane)
            run = None
            for s in lane:
                if run and s['lo'] - run['hi'] <= JOIN_GAP:
                    if s['lo'] > run['hi'] + 0.5:
                        run['gaps'].append((run['hi'], s['lo']))
                    run['hi'] = max(run['hi'], s['hi'])
                    run['redLen'] += (s['hi'] - s['lo']) if s['red'] else 0
                else:
                    if run:
                        runs.append(run)
                    run = {'o': o, 'c': c, 'lo': s['lo'], 'hi': s['hi'], 'redLen': (s['hi'] - s['lo']) if s['red'] else 0, 'gaps': []}
            if run:
                runs.append(run)
    return runs


def _bridge_symbols(runs, marks):
    """Joins collinear pipe runs across a gap that holds an inline symbol: a
    valve bowtie, flanges, a strainer. The drawing says where a valve is more
    reliably than a model's box does. Returns the merged runs and where each
    bridge was made (for naming the symbol afterwards)."""
    lanes = defaultdict(list)
    for r in runs:
        lanes[(r['o'], round(r['c'] / LANE_TOL))].append(r)
    keys = sorted(lanes)
    merged, bridges = [], []
    done = set()
    for key in keys:
        if key in done:
            continue
        lane = [r for k in (key, (key[0], key[1] + 1)) for r in lanes.get(k, []) if (k not in done or k == key)]
        done.update({key, (key[0], key[1] + 1)})
        lane.sort(key=lambda r: r['lo'])
        cur = None
        for r in lane:
            if cur is not None and JOIN_GAP < r['lo'] - cur['hi'] <= SYMBOL_GAP:
                a, b = cur['hi'], r['lo']
                inside = sum(1 for (x, y) in marks
                             if (a < (x if r['o'] == 'H' else y) < b) and abs((y if r['o'] == 'H' else x) - cur['c']) <= SYMBOL_ACROSS)
                if inside >= SYMBOL_MARKS:
                    bridges.append({'o': r['o'], 'c': cur['c'], 'lo': a, 'hi': b})
                    cur = {**cur, 'hi': max(cur['hi'], r['hi']), 'redLen': cur['redLen'] + r['redLen'], 'gaps': cur['gaps'] + r['gaps'],
                           'bridged': cur.get('bridged', 0) + 1 + r.get('bridged', 0)}
                    continue
            if cur is not None and r['lo'] - cur['hi'] <= 0.5:
                cur = {**cur, 'hi': max(cur['hi'], r['hi']), 'redLen': cur['redLen'] + r['redLen'], 'gaps': cur['gaps'] + r['gaps']}
                continue
            if cur is not None:
                merged.append(cur)
            cur = dict(r)
        if cur is not None:
            merged.append(cur)
    return merged, bridges


def _mostly_red(runs):
    """Drawn in the revision colour along most of its length: a red valve on an
    existing pipe does not make the pipe new."""
    total = sum(r['hi'] - r['lo'] for r in runs)
    return total > 0 and sum(min(r['redLen'], r['hi'] - r['lo']) for r in runs) / total > 0.5


def _drop_short(runs, entities, W, H, shell_boxes):
    """Drops runs too short to be pipe, except a stub that CONNECTS: from a
    pipe to a valve, fitting or vessel (a drop from a header into the valve on
    a nozzle), or between two different such symbols. Returns (kept, dropped count)."""
    long_runs = [r for r in runs if r['hi'] - r['lo'] >= MIN_RUN]
    boxes = []
    for e in entities:
        if e['category'] not in ('valve', 'fitting', 'equipment', 'connector'):
            continue
        if e['id'] in shell_boxes:
            b = shell_boxes[e['id']]
            pad = NOZZLE_REACH if e['category'] == 'equipment' else TOUCH
            boxes.append((e['id'], (b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad)))
        else:
            boxes.append((e['id'], _box(e, W, H, NOZZLE_REACH if e['category'] == 'equipment' else TOUCH)))

    def on_pipe(p, own):
        for r in long_runs:
            if r is own or r['o'] == own['o']:
                continue
            along, across = (p[0], p[1]) if r['o'] == 'H' else (p[1], p[0])
            if abs(across - r['c']) <= TOUCH and r['lo'] - TOUCH <= along <= r['hi'] + TOUCH:
                return True
        return False

    def symbol(p):
        return next((eid for eid, bx in boxes if _in_box(p, bx)), None)

    kept, dropped = list(long_runs), 0
    for r in runs:
        if r['hi'] - r['lo'] >= MIN_RUN:
            continue
        a, b = _run_points(r)
        sa, sb = symbol(a), symbol(b)
        if (on_pipe(a, r) and sb) or (on_pipe(b, r) and sa) or (sa and sb and sa != sb):
            kept.append(r)
        else:
            dropped += 1
    return kept, dropped


def _hatch_density(run, diagonals):
    """Parallel diagonal strokes beside a run, per 100 pt. Hatching is many
    strokes at ONE angle; arrowheads, drain triangles and valve bowties beside
    a pipe mix angles, so only the most common angle counts."""
    (ax, ay), (bx, by) = _run_points(run)
    by_angle = defaultdict(int)
    for x, y, angle in diagonals:
        if angle is None:
            continue
        if run['o'] == 'H' and ax <= x <= bx and abs(y - run['c']) <= HATCH_REACH:
            by_angle[angle] += 1
        elif run['o'] == 'V' and ay <= y <= by and abs(x - run['c']) <= HATCH_REACH:
            by_angle[angle] += 1
    return 100 * max(by_angle.values(), default=0) / max(run['hi'] - run['lo'], 1)


def _classify(runs, diagonals):
    """Drops dash-dot boundaries and hatched walls. Short runs are kept here:
    the pipe stubs between valves in a dense train are only a few points long,
    and only symbol bridging can tell them from symbol detail."""
    pipes, rejected = [], {'short': 0, 'dashed': 0, 'hatched': 0}
    for r in runs:
        length = r['hi'] - r['lo']
        if length < MIN_RUN and not r['gaps']:
            pipes.append(r)
        elif len(r['gaps']) >= DASH_GAPS and 100 * len(r['gaps']) / length > DASH_DENSITY:
            rejected['dashed'] += 1
        elif length >= HATCH_MIN_RUN and _hatch_density(r, diagonals) > HATCH_DENSITY \
                and _hatch_density(r, diagonals) * length / 100 >= HATCH_MIN_STROKES:
            rejected['hatched'] += 1
        else:
            pipes.append(r)
    return pipes, rejected


def _arrow_axes(triangles):
    """The base-to-tip axis of every slender lone triangle, as a straight
    stroke. An arrowhead sits ON its pipe, and some sheets draw a short jog or
    a pipe end only as the arrowhead, so its axis is a piece of the pipe."""
    out = []
    heads = []
    for t in triangles:
        pts = t['pts']
        sides = sorted((math.dist(pts[(k + 1) % 3], pts[(k + 2) % 3]), k) for k in range(3))
        base, tip_k = sides[0]
        if sides[1][0] < 1.2 * base:
            continue
        tip = pts[tip_k]
        b0, b1 = pts[(tip_k + 1) % 3], pts[(tip_k + 2) % 3]
        heads.append((tip, ((b0[0] + b1[0]) / 2, (b0[1] + b1[1]) / 2)))
    tips = [h[0] for h in heads]
    for tip, mid in heads:
        if sum(1 for t in tips if math.dist(t, tip) < 1.0) > 1:
            continue  # valve bowtie
        dx, dy = tip[0] - mid[0], tip[1] - mid[1]
        if abs(dy) <= AXIS_TOL and abs(dx) > 0.5:
            out.append({'o': 'H', 'c': (tip[1] + mid[1]) / 2, 'lo': min(tip[0], mid[0]), 'hi': max(tip[0], mid[0]), 'w': PIPE_WIDTHS[0], 'red': False})
        elif abs(dx) <= AXIS_TOL and abs(dy) > 0.5:
            out.append({'o': 'V', 'c': (tip[0] + mid[0]) / 2, 'lo': min(tip[1], mid[1]), 'hi': max(tip[1], mid[1]), 'w': PIPE_WIDTHS[0], 'red': False})
    return out


def _arrowheads(triangles, runs):
    """Lone slender triangles lying on a run, pointing along it. Valve bowties
    are two triangles tip to tip; scope-limit ticks are not on a pipe."""
    heads = []
    for t in triangles:
        pts = t['pts']
        sides = sorted((math.dist(pts[(k + 1) % 3], pts[(k + 2) % 3]), k) for k in range(3))
        base, tip_k = sides[0]
        if sides[1][0] < 1.2 * base:
            continue  # not slender enough to be an arrowhead
        tip = pts[tip_k]
        b0, b1 = pts[(tip_k + 1) % 3], pts[(tip_k + 2) % 3]
        mid = ((b0[0] + b1[0]) / 2, (b0[1] + b1[1]) / 2)
        heads.append({'tip': tip, 'mid': mid})
    tips = [h['tip'] for h in heads]
    out = []
    for h in heads:
        if sum(1 for t in tips if math.dist(t, h['tip']) < 1.0) > 1:
            continue  # bowtie
        dx, dy = h['tip'][0] - h['mid'][0], h['tip'][1] - h['mid'][1]
        o = 'H' if abs(dx) > abs(dy) else 'V'
        for i, r in enumerate(runs):
            if r['o'] != o:
                continue
            along, across = (h['tip'][0], h['tip'][1]) if o == 'H' else (h['tip'][1], h['tip'][0])
            base_across = h['mid'][1] if o == 'H' else h['mid'][0]
            # A flow arrow is drawn ON the pipe: tip and base both on its line.
            # Drain / vent marks beside a pipe touch it with the tip only.
            if abs(across - r['c']) <= TOUCH and abs(base_across - r['c']) <= TOUCH and r['lo'] - TOUCH <= along <= r['hi'] + TOUCH:
                out.append({'run': i, 'sign': 1 if (dx if o == 'H' else dy) > 0 else -1, 'at': h['tip']})
                break
    return out


def _equipment_outlines(outlines, entities, W, H):
    """Each equipment item's and connector's DRAWN outline: path -> entity, and
    the entity's true box. Step 1's box is the model's estimate: often smaller
    than the vessel drawn, and for a connector often wide enough to cover the
    label and the start of its pipe.

    A vessel is the smallest large closed outline around its centre. A
    connector is the widest thin closed outline (its pentagon) whose centre
    lies in the model's box."""
    paths, boxes = {}, {}
    polys = {}
    for e in entities:
        if e['category'] == 'connector':
            mb = _box(e, W, H)
            inner = [o for o in outlines if o['thin']
                     and mb[0] <= (o['box'][0] + o['box'][2]) / 2 <= mb[2] and mb[1] <= (o['box'][1] + o['box'][3]) / 2 <= mb[3]]
            if inner:
                o = max(inner, key=lambda o: o['box'][2] - o['box'][0])
                paths[o['path']] = e['id']
                boxes[e['id']] = o['box']
            continue
        if e['category'] != 'equipment':
            continue
        b = e.get('symbolBbox') or e['bbox']
        cx, cy = (b['x'] + b['width'] / 2) * W, (b['y'] + b['height'] / 2) * H
        hits = [o for o in outlines if not o['thin'] and o['box'][0] <= cx <= o['box'][2] and o['box'][1] <= cy <= o['box'][3]
                # not the frame, a package boundary or a big pipe loop around it
                and o['box'][2] - o['box'][0] <= SHELL_MAX * W and o['box'][3] - o['box'][1] <= SHELL_MAX * H]
        if hits:
            o = min(hits, key=lambda o: (o['box'][2] - o['box'][0]) * (o['box'][3] - o['box'][1]))
            paths[o['path']] = e['id']
            mb = _box(e, W, H)
            boxes[e['id']] = (min(mb[0], o['box'][0]), min(mb[1], o['box'][1]), max(mb[2], o['box'][2]), max(mb[3], o['box'][3]))
            if len(o.get('poly') or []) >= 3:
                polys[e['id']] = o['poly']
    return paths, boxes, polys


def _in_poly(p, poly):
    """Ray casting: is point p inside the polygon?"""
    x, y = p
    inside = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _propagate_flow(spans, touches, by_symbol, by_id):
    """Fills in direction where the drawing has no arrow, by conservation: at a
    junction, valve or fitting, if every connected span but one is known to
    flow IN, the last one flows OUT, and the reverse. Equipment and connectors
    can be sources or sinks, so nothing is inferred across them. Repeats until
    nothing changes."""
    def node_of(v):
        e = touches.get(v)
        if e is None:
            return ('v', v)
        return ('e', e['id']) if e['category'] not in TERMINAL_CATEGORIES else None
    at = defaultdict(list)  # node -> [(span, which end)]
    for sp in spans:
        if any(end['kind'] == 'end' for end in sp['ends']) and not sp['flow'] and sp['lengthPt'] < DRAIN_MAX:
            continue  # a drain, vent or capped stub: no through-flow to balance
        for k, v in enumerate(sp['vertices']):
            node = node_of(v)
            if node is not None:
                at[node].append((sp, k))

    def direction(sp, k):
        """+1 flowing into the node at end k, -1 out of it, 0 unknown."""
        if not sp['flow']:
            return 0
        into_end1 = sp['flow'] == 'forward'
        return 1 if (k == 1) == into_end1 else -1

    changed = True
    while changed:
        changed = False
        for node, members in at.items():
            if len(members) < 2:
                continue
            known = [direction(sp, k) for sp, k in members if sp['flow']]
            unknown = [(sp, k) for sp, k in members if not sp['flow']]
            if len(unknown) != 1 or not known or (all(d > 0 for d in known) is False and all(d < 0 for d in known) is False):
                continue
            sp, k = unknown[0]
            leaving = all(d > 0 for d in known)  # everything else flows in, so this one flows out
            # out of the node at end k means flowing from end k to the other end
            sp['flow'] = ('forward' if k == 0 else 'reverse') if leaving else ('reverse' if k == 0 else 'forward')
            sp['flowSource'] = 'inferred'
            changed = True


def terminal_links(spans, contacts, by_id):
    """Equipment / connector pairs the drawn pipework joins directly (through
    valves, fittings and junctions, never through other equipment), each with
    the valves on the way, the spans used, line numbers and the flow direction
    where every directed span on the way agrees."""
    graph = defaultdict(list)  # node -> [(next node, span or None)]
    for sp in spans:
        a, b = sp['vertices']
        graph[a].append((b, sp))
        graph[b].append((a, sp))
        for v, end in zip(sp['vertices'], sp['ends']):
            if end['entityId']:
                graph[v].append((end['entityId'], None))
                graph[end['entityId']].append((v, None))
    for a, b in contacts:
        graph[a].append((b, None))
        graph[b].append((a, None))

    def terminal(n):
        e = by_id.get(n)
        return e is not None and e['category'] in TERMINAL_CATEGORIES

    links = {}
    for start in [n for n in graph if terminal(n)]:
        # breadth-first, remembering the path, stopping at other terminals
        prev = {start: (None, None)}
        queue = [start]
        while queue:
            cur = queue.pop(0)
            for nxt, sp in graph[cur]:
                if nxt in prev:
                    continue
                prev[nxt] = (cur, sp)
                if terminal(nxt):
                    key = tuple(sorted((start, nxt)))
                    if key in links or nxt == start:
                        continue
                    if by_id[start]['category'] == 'connector' and by_id[nxt]['category'] == 'connector' and not any(
                            by_id[t]['category'] == 'equipment' for t in (start, nxt)):
                        pass  # kept: a header passing straight across the sheet is still a link
                    # walk back
                    path_spans, symbols, node = [], [], nxt
                    while node != start:
                        back, via = prev[node]
                        if via is not None:
                            path_spans.append(via)
                        if back in by_id and back != start and not terminal(back):
                            symbols.append(back)
                        node = back
                    path_spans.reverse(); symbols.reverse()
                    # direction: +1 if every directed span flows start -> nxt
                    votes = set()
                    node = start
                    for sp_ in path_spans:
                        symbols += [t for t in sp_['through'] if t not in symbols]
                    links[key] = {
                        'from': start, 'to': nxt, 'via': list(dict.fromkeys(symbols)),
                        'spanIds': [x['id'] for x in path_spans],
                        'lineNumbers': list(dict.fromkeys(n for x in path_spans for n in x.get('lineNumbers', []))),
                        'red': any(x['red'] for x in path_spans),
                    }
                elif not terminal(nxt):
                    queue.append(nxt)
    # Direction of a link: from its span flows, read in walking order.
    span_by_id = {sp['id']: sp for sp in spans}
    for link in links.values():
        link['flow'] = _link_flow(link, span_by_id)
    return sorted(links.values(), key=lambda l: (l['from'], l['to']))


def _link_flow(link, span_by_id):
    """'from->to', 'to->from', 'mixed' or None, from the directed spans on the way.
    A span's direction is relative to its own point order, so compare each
    with the order the walk crosses it: by which of its ends is nearer the
    previous span (or the start)."""
    spans = [span_by_id[i] for i in link['spanIds']]
    if not spans:
        return None
    votes = []
    for k, sp in enumerate(spans):
        if not sp['flow']:
            continue
        if len(spans) == 1:
            ends = [e['entityId'] for e in sp['ends']]
            forward_walk = ends[0] == link['from'] or ends[1] == link['to']
        else:
            other = spans[k - 1] if k > 0 else spans[k + 1]
            shared = set(sp['vertices']) & set(other['vertices'])
            if not shared:
                continue
            v = shared.pop()
            # the walk enters at the shared vertex when the other span came before
            enters_at_start = (sp['vertices'][0] == v) == (k > 0)
            forward_walk = enters_at_start
        votes.append(forward_walk == (sp['flow'] == 'forward'))
    if not votes:
        return None
    if all(votes):
        return 'from->to'
    if not any(votes):
        return 'to->from'
    return 'mixed'


def build_network(page_index, W, H, straight, diagonals, triangles, entities, text_items, outlines=()):
    frame = _frame(straight, W, H)
    shell_paths, shell_boxes, shell_polys = _equipment_outlines(outlines, entities, W, H)
    straight = [s for s in straight if s.get('path') not in shell_paths]
    def padded(e, pad):
        if e['id'] in shell_boxes:
            b = shell_boxes[e['id']]
            return (b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad)
        return _box(e, W, H, pad)
    # A vessel whose drawn outline was matched is excluded stroke by stroke
    # (shell_paths); its bounding box would also cut the pipes drawn in the
    # empty corners of the box, like a branch dropping onto a conical roof.
    bodies = [(e, padded(e, BODY_PADS[e['category']])) for e in entities
              if e['category'] in BODY_PADS and e['id'] not in shell_boxes]
    def in_shell(s):
        # strokes inside a drawn vessel outline: internals (dip pipes, agitator), not pipework
        a, b = _run_points(s)
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        return any(_in_poly(mid, poly) and _in_poly(a, poly) and _in_poly(b, poly) for poly in shell_polys.values())
    pipe_widths = PIPE_WIDTHS if any(s['w'] > WIDTH_TOL for s in straight) else HAIRLINE_WIDTHS
    keep = [s for s in straight
            if any(abs(s['w'] - w) <= WIDTH_TOL for w in pipe_widths) and not s.get('symbol')
            and _seg_inside(s, frame) and not any(_seg_inside(s, bx) for _, bx in bodies) and not in_shell(s)]
    keep += [s for s in _arrow_axes(triangles) if _seg_inside(s, frame)]
    runs, rejected = _classify(_join(keep), diagonals)
    # Symbol strokes: everything that is not itself a pipe run, by midpoint.
    marks = [(x, y) for x, y, _ in diagonals] + [(sum(p[0] for p in t['pts']) / 3, sum(p[1] for p in t['pts']) / 3) for t in triangles] + [
        (((s['lo'] + s['hi']) / 2, s['c']) if s['o'] == 'H' else (s['c'], (s['lo'] + s['hi']) / 2))
        for s in straight if s['hi'] - s['lo'] < MIN_RUN]
    runs, bridges = _bridge_symbols(runs, marks)
    runs, rejected['short'] = _drop_short(runs, entities, W, H, shell_boxes)
    arrows = _arrowheads(triangles, runs)

    # ── graph: vertices are run ends; split runs where another run's end meets their middle.
    cuts = defaultdict(list)
    ends = [(i, k, p) for i, r in enumerate(runs) for k, p in enumerate(_run_points(r))]
    for i, k, p in ends:
        for j, r in enumerate(runs):
            if j == i:
                continue
            along, across = (p[0], p[1]) if r['o'] == 'H' else (p[1], p[0])
            if abs(across - r['c']) <= TOUCH and r['lo'] + TOUCH < along < r['hi'] - TOUCH:
                cuts[j].append(along)
    pieces = []  # (run index, lo, hi)
    for i, r in enumerate(runs):
        stops = sorted({r['lo'], r['hi'], *cuts[i]})
        pieces += [(i, a, b) for a, b in zip(stops, stops[1:]) if b - a > 0.5]

    def pt(i, v):
        r = runs[i]
        return (v, r['c']) if r['o'] == 'H' else (r['c'], v)

    # Vertices: piece ends that coincide (corners, junction points) are one vertex.
    dsu = DisjointSet()
    vkeys = []
    for n, (i, a, b) in enumerate(pieces):
        for side, v in ((0, a), (1, b)):
            key = (n, side)
            dsu.add(key)
            vkeys.append((key, pt(i, v)))
    grid = defaultdict(list)
    for key, p in vkeys:
        grid[(round(p[0] / 5), round(p[1] / 5))].append((key, p))
    for (gx, gy), items in list(grid.items()):
        neighbours = [kp for dx in (-1, 0, 1) for dy in (-1, 0, 1) for kp in grid.get((gx + dx, gy + dy), [])]
        for key, p in items:
            for k2, p2 in neighbours:
                if k2 != key and math.dist(p, p2) <= TOUCH:
                    dsu.union(key, k2)
    vertex_of = {key: dsu.find(key) for key, _ in vkeys}
    vertex_pt = {}
    for key, p in vkeys:
        vertex_pt.setdefault(vertex_of[key], p)

    # Symbols a vertex touches (a pipe end at a nozzle, valve, instrument, connector).
    all_boxes = [(e, padded(e, NOZZLE_REACH if e['category'] == 'equipment' else TOUCH))
                 for e in entities if e['category'] in ('equipment', 'valve', 'fitting', 'instrument', 'connector')]
    touches = {}
    for v, p in vertex_pt.items():
        hit = [(e, bx) for e, bx in all_boxes if _in_box(p, bx)]
        # Equipment drawn inside other equipment (an agitator in its tank) is
        # never what a pipe connects to: keep the vessel around it.
        hit = [e for e, bx in hit if not (e['category'] == 'equipment' and any(
            o is not e and o['category'] == 'equipment' and ob[0] <= bx[0] and ob[1] <= bx[1] and ob[2] >= bx[2] and ob[3] >= bx[3]
            for o, ob in hit))]
        if hit:
            # The smallest box: a valve on a tank nozzle belongs to the valve.
            touches[v] = min(hit, key=lambda e: (e['bbox']['width'] * e['bbox']['height']))
    # Symbols a piece passes through (inline valves bridged by the run join).
    def through(i, a, b):
        r = runs[i]
        out = []
        for e, bx in all_boxes:
            if e['category'] in TERMINAL_CATEGORIES:
                continue
            if r['o'] == 'H' and bx[1] <= r['c'] <= bx[3] and a < (bx[0] + bx[2]) / 2 < b:
                out.append(((bx[0] + bx[2]) / 2, e['id']))
            elif r['o'] == 'V' and bx[0] <= r['c'] <= bx[2] and a < (bx[1] + bx[3]) / 2 < b:
                out.append(((bx[1] + bx[3]) / 2, e['id']))
        return [eid for _, eid in sorted(out)]

    # Adjacency between vertices; a symbol two vertices both touch joins them (a valve in a gap).
    adj = defaultdict(list)
    for n, (i, a, b) in enumerate(pieces):
        va, vb = vertex_of[(n, 0)], vertex_of[(n, 1)]
        adj[va].append((vb, n))
        adj[vb].append((va, n))
    by_symbol = defaultdict(list)
    for v, e in touches.items():
        by_symbol[e['id']].append(v)

    # ── spans: maximal piece chains between stops.
    def is_stop(v):
        return len(adj[v]) != 2 or v in touches
    seen, spans = set(), []
    for n in range(len(pieces)):
        if n in seen:
            continue
        # walk to one end
        chain = [n]
        seen.add(n)
        for direction in (0, 1):
            v = vertex_of[(n, 1 if direction else 0)]
            cur = n
            while not is_stop(v):
                nxt = [(w, m) for w, m in adj[v] if m != cur][0]
                if nxt[1] in seen:
                    break
                seen.add(nxt[1])
                if direction:
                    chain.append(nxt[1])
                else:
                    chain.insert(0, nxt[1])
                cur = nxt[1]
                v = nxt[0]
        spans.append(chain)

    def ordered_points(chain):
        """The chain's polyline from one end to the other, merged straight runs."""
        if len(chain) == 1:
            i, a, b = pieces[chain[0]]
            return [pt(i, a), pt(i, b)], (vertex_of[(chain[0], 0)], vertex_of[(chain[0], 1)])
        # orient each piece so consecutive pieces share a vertex
        first = chain[0]
        nxt = chain[1]
        shared = {vertex_of[(nxt, 0)], vertex_of[(nxt, 1)]}
        start_side = 0 if vertex_of[(first, 1)] in shared else 1
        pts, cur_v = [], vertex_of[(first, start_side)]
        start_v = cur_v
        for m in chain:
            i, a, b = pieces[m]
            if vertex_of[(m, 0)] == cur_v:
                seq, cur_v = [pt(i, a), pt(i, b)], vertex_of[(m, 1)]
            else:
                seq, cur_v = [pt(i, b), pt(i, a)], vertex_of[(m, 0)]
            pts += seq if not pts else seq[1:]
        # drop collinear midpoints
        clean = [pts[0]]
        for k in range(1, len(pts) - 1):
            q, p, r_ = clean[-1], pts[k], pts[k + 1]
            if not ((abs(q[0] - p[0]) < 0.6 and abs(p[0] - r_[0]) < 0.6) or (abs(q[1] - p[1]) < 0.6 and abs(p[1] - r_[1]) < 0.6)):
                clean.append(p)
        clean.append(pts[-1])
        return clean, (start_v, cur_v)

    to_norm = lambda p: {'x': round(p[0] / W, 5), 'y': round(p[1] / H, 5)}
    span_out = []
    span_of_piece = {}
    for s_i, chain in enumerate(spans):
        pts, (v0, v1) = ordered_points(chain)
        span_len = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
        isolated = all(len(adj[v]) == 1 and (v not in touches or touches[v]['category'] == 'connector') for v in (v0, v1))
        if isolated and span_len < STUB_MAX:
            continue  # a divider inside a connector box, a tick: connects nothing
        thru = []
        for m in chain:
            thru += through(*pieces[m])
        ends = []
        for v in (v0, v1):
            e = touches.get(v)
            ends.append({'entityId': e['id'] if e else None, 'kind': (e['category'] if e else ('junction' if len(adj[v]) > 2 else 'end'))})
        span_out.append({'id': f'p{page_index}-span-{len(span_out) + 1}', 'points': pts, 'ends': ends, 'vertices': [v0, v1], 'lengthPt': round(span_len, 1),
                         'through': list(dict.fromkeys(thru)), 'red': _mostly_red([runs[pieces[m][0]] for m in chain]),
                         'runs': sorted({pieces[m][0] for m in chain})})
        for m in chain:
            span_of_piece[m] = s_i

    # ── direction per span from arrowheads: +1 = in the order of its points.
    for sp in span_out:
        votes = 0
        for ar in arrows:
            if ar['run'] not in sp['runs']:
                continue
            p = sp['points']
            for a, b in zip(p, p[1:]):
                horiz = abs(a[1] - b[1]) < 0.6
                if horiz and abs(ar['at'][1] - a[1]) <= TOUCH and min(a[0], b[0]) - TOUCH <= ar['at'][0] <= max(a[0], b[0]) + TOUCH:
                    votes += ar['sign'] * (1 if b[0] > a[0] else -1)
                elif not horiz and abs(ar['at'][0] - a[0]) <= TOUCH and min(a[1], b[1]) - TOUCH <= ar['at'][1] <= max(a[1], b[1]) + TOUCH:
                    votes += ar['sign'] * (1 if b[1] > a[1] else -1)
        sp['flow'] = 'forward' if votes > 0 else 'reverse' if votes < 0 else None
        sp['flowSource'] = 'arrow' if votes else None

    # Off-page connectors: a span ending at the point of the pentagon is an inlet.
    by_id = {e['id']: e for e in entities}
    for sp in span_out:
        if sp['flow']:
            continue
        for k, end in enumerate(sp['ends']):
            e = by_id.get(end['entityId'])
            if not e or e['category'] != 'connector':
                continue
            bx = _box(e, W, H)
            p = sp['points'][0 if k == 0 else -1]
            cx = (bx[0] + bx[2]) / 2
            # The pentagon's point is on its narrow side; on this kind of sheet the
            # boxes point left-to-right, so the point is the right end.
            at_point = p[0] >= cx
            inlet = at_point
            # inlet: flow runs away from the connector end
            sp['flow'] = ('forward' if k == 0 else 'reverse') if inlet else ('reverse' if k == 0 else 'forward')
            sp['flowSource'] = 'connector'
            end['connectorRole'] = 'inlet' if inlet else 'outlet'
            break

    # ── line numbers from the text layer beside each span.
    for t in text_items:
        s = t['str'].strip()
        if not LINE_NUMBER.match(s):
            continue
        b = t['bbox']
        cx, cy = (b['x'] + b['width'] / 2) * W, (b['y'] + b['height'] / 2) * H
        vertical = b['height'] * H > b['width'] * W
        best, best_d = None, LINE_LABEL_REACH
        for sp in span_out:
            for a, c in zip(sp['points'], sp['points'][1:]):
                horiz = abs(a[1] - c[1]) < 0.6
                if horiz == vertical:
                    continue
                if horiz and min(a[0], c[0]) <= cx <= max(a[0], c[0]):
                    d = abs(cy - a[1])
                elif not horiz and min(a[1], c[1]) <= cy <= max(a[1], c[1]):
                    d = abs(cx - a[0])
                else:
                    continue
                if d < best_d:
                    best, best_d = sp, d
        if best is not None:
            best.setdefault('lineNumbers', []).append(s)

    # ── symbols bolted to each other with no pipe stroke between (a valve on a
    # nozzle, back-to-back valves, a strainer on a pump): process symbols only,
    # so an instrument bubble drawn beside a valve is not a process link.
    process = [(e, bx) for e, bx in all_boxes if e['category'] in ('equipment', 'valve', 'fitting', 'connector')]
    contacts = []
    for a in range(len(process)):
        ea, ba = process[a]
        for b in range(a + 1, len(process)):
            eb, bb = process[b]
            if {ea['category'], eb['category']} <= TERMINAL_CATEGORIES:
                continue  # vessels and connectors are not bolted to each other; boxes just touch
            if ba[0] <= bb[2] and bb[0] <= ba[2] and ba[1] <= bb[3] and bb[1] <= ba[3]:
                contacts.append([ea['id'], eb['id']])

    # ── pipes: spans chained through valves / fittings / inline instruments.
    pipe_dsu = DisjointSet()
    for sp in span_out:
        pipe_dsu.add(sp['id'])
    at_vertex = defaultdict(list)
    for sp in span_out:
        for v in sp['vertices']:
            at_vertex[v].append(sp['id'])
    for v, ids in at_vertex.items():
        e = touches.get(v)
        passable = (e is None and len(adj[v]) == 2) or (e is not None and e['category'] not in TERMINAL_CATEGORIES)
        if passable:
            for a in ids[1:]:
                pipe_dsu.union(ids[0], a)
    for eid, vs in by_symbol.items():
        if by_id[eid]['category'] in TERMINAL_CATEGORIES:
            continue
        ids = [s for v in vs for s in at_vertex[v]]
        for a in ids[1:]:
            pipe_dsu.union(ids[0], a)
    pipes = []
    span_by_id = {sp['id']: sp for sp in span_out}
    for n, group in enumerate(sorted(pipe_dsu.groups(), key=lambda g: g[0])):
        members = [span_by_id[s] for s in group]
        numbers = list(dict.fromkeys(x for sp in members for x in sp.get('lineNumbers', [])))
        terminals = list(dict.fromkeys(end['entityId'] for sp in members for end in sp['ends']
                                       if end['entityId'] and by_id[end['entityId']]['category'] in TERMINAL_CATEGORIES))
        pid = f'p{page_index}-pipe-{n + 1}'
        for sp in members:
            sp['pipeId'] = pid
        pipes.append({'id': pid, 'spanIds': [sp['id'] for sp in members], 'lineNumbers': numbers, 'terminals': terminals,
                      'through': list(dict.fromkeys(x for sp in members for x in sp['through'])),
                      'red': any(sp['red'] for sp in members),
                      'length': round(sum(math.dist(a, b) for sp in members for a, b in zip(sp['points'], sp['points'][1:])) / W, 4)})

    _propagate_flow(span_out, touches, by_symbol, by_id)

    vertex_ids = {}
    for sp in span_out:
        sp['points'] = [to_norm(p) for p in sp['points']]
        # Stable ids for the span's end points, so callers can follow the network.
        sp['vertices'] = [f"p{page_index}-v{vertex_ids.setdefault(v, len(vertex_ids) + 1)}" for v in sp['vertices']]
        del sp['runs']
    links = terminal_links(span_out, contacts, by_id)
    return {
        'pageIndex': page_index,
        'links': links,
        'spans': span_out,
        'pipes': pipes,
        'contacts': contacts,
        'diagnostics': {
            'strokes': len(straight), 'pipeStrokes': len(keep), 'runs': len(runs), 'rejectedRuns': rejected,
            'arrows': len(arrows), 'spans': len(span_out), 'pipes': len(pipes),
            'spansWithFlow': sum(1 for s in span_out if s['flow']),
            'frame': [round(frame[0] / W, 4), round(frame[1] / H, 4), round(frame[2] / W, 4), round(frame[3] / H, 4)],
        },
    }


# ──────────────────────────────────────────────── document, prompts, nodes

def trace_document(doc, step1_result):
    """The network for every page step 1 extracted. Free and deterministic."""
    pages = []
    for page in step1_result['pages']:
        i = page['pageIndex']
        pages.append(trace_page(doc[i], i, page['entities'], page['textItems']))
    return {
        'step': 'network',
        'fileName': step1_result['fileName'],
        'pages': pages,
        'totals': {k: sum(p['diagnostics'][k] for p in pages) for k in ('spans', 'pipes', 'spansWithFlow')},
    }


def format_links(network_page, entities):
    """The traced equipment-to-equipment links as prompt lines."""
    by_id = {e['id']: e for e in entities}

    def name(i):
        e = by_id.get(i) or {}
        return f"{e.get('tag') or e.get('type') or 'untagged'} ({i})"

    lines = []
    for link in network_page['links']:
        a, b = link['from'], link['to']
        if link['flow'] == 'to->from':
            a, b = b, a
        arrow = '->' if link['flow'] in ('from->to', 'to->from') else '--'
        via = ', '.join(name(v) for v in link['via'][:8]) or 'direct'
        numbers = f" [{', '.join(link['lineNumbers'][:3])}]" if link['lineNumbers'] else ''
        red = ' [REVISED]' if link['red'] else ''
        lines.append(f"{name(a)} {arrow} {name(b)} via {via}{numbers}{red}")
    return '\n'.join(lines) or '(no links traced)'


def node_routes(nodes, network_page, entities):
    """Gives every traced span to one node, so a node is painted with whole
    pipes rather than the model's fragments. Votes, strongest first:
    - the model's own LINE items the span runs along (each line carries the
      node the model put it in, and is the pipe itself);
    - equipment and connectors at the span's ends;
    - valves and fittings on it or at its ends (a new valve on an existing
      pipe must not drag the pipe into the revised node);
    - and pipe DRAWN red always goes to the revised node (R6).
    A span with no votes (a run between two junctions) takes the node of the
    spans it meets. Returns {node local id: [route]}, each route ordered with
    the flow where known."""
    owner = {}
    for n in nodes:
        for eid in n['entityIds']:
            owner.setdefault(eid, n['localId'])
    by_id = {e['id']: e for e in entities}
    line_segments = []  # (node, a, b) from the model's line routes
    for e in entities:
        if e['category'] == 'line' and e['id'] in owner:
            for route in e.get('routes') or []:
                line_segments += [(owner[e['id']], a, b) for a, b in zip(route, route[1:])]

    def near_line(p):
        best = None
        for node, a, b in line_segments:
            dx, dy = b['x'] - a['x'], b['y'] - a['y']
            L2 = dx * dx + dy * dy
            t = 0 if L2 == 0 else max(0, min(1, ((p['x'] - a['x']) * dx + (p['y'] - a['y']) * dy) / L2))
            d = math.hypot(p['x'] - (a['x'] + t * dx), p['y'] - (a['y'] + t * dy))
            if d <= LINE_MATCH and (best is None or d < best[0]):
                best = (d, node)
        return best[1] if best else None

    def samples(points, step=0.004):
        out = []
        for a, b in zip(points, points[1:]):
            n = max(1, int(math.hypot(b['x'] - a['x'], b['y'] - a['y']) / step))
            out += [{'x': a['x'] + (b['x'] - a['x']) * k / n, 'y': a['y'] + (b['y'] - a['y']) * k / n} for k in range(n + 1)]
        return out

    revised = [n['localId'] for n in nodes if n.get('revisedScope')]
    span_node = {}
    for sp in network_page['spans']:
        votes = defaultdict(float)
        pts = samples(sp['points'])
        for p in pts:
            node = near_line(p)
            if node:
                votes[node] += 3.0 / len(pts)
        for end in sp['ends']:
            eid = end['entityId']
            if eid in owner:
                votes[owner[eid]] += 2.0 if by_id[eid]['category'] in TERMINAL_CATEGORIES else 0.5
        for eid in sp['through']:
            if eid in owner:
                votes[owner[eid]] += 0.5
        # Pipe drawn red is new or modified scope: it belongs in the revised node (R6).
        if sp['red'] and revised:
            touching = [n for n in revised if votes.get(n)]
            votes[(touching or revised)[0]] += 5.0
        if votes:
            span_node[sp['id']] = max(votes, key=lambda k: (votes[k], k))
    # spread to spans with no symbols, through shared junctions
    at_vertex = defaultdict(list)
    for sp in network_page['spans']:
        for v in sp['vertices']:
            at_vertex[v].append(sp['id'])
    changed = True
    while changed:
        changed = False
        for sp in network_page['spans']:
            if sp['id'] in span_node:
                continue
            neighbours = [span_node[o] for v in sp['vertices'] for o in at_vertex[v] if o in span_node]
            if neighbours:
                span_node[sp['id']] = max(set(neighbours), key=neighbours.count)
                changed = True
    routes = defaultdict(list)
    for sp in network_page['spans']:
        node = span_node.get(sp['id'])
        if node is None:
            continue
        points = sp['points'] if sp['flow'] != 'reverse' else sp['points'][::-1]
        routes[node].append({'entityId': sp['id'], 'points': points, 'flow': bool(sp['flow']), 'pipeId': sp.get('pipeId')})
    return dict(routes)
