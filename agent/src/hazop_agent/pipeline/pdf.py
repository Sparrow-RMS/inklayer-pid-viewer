"""PDF access for the pipeline: page rasterization plus the two exact-geometry
sources a vector (CAD-exported) P&ID gives us for free — the text layer and
the stroked vector paths.

Everything returned here is in NORMALIZED, TOP-LEFT-ORIGIN page fractions of
the visible (rotation-applied) page (see geometry.py), so nothing downstream
has to know about PDF user space.

Port of server/src/pipeline/pdf.js from pdfjs to PyMuPDF. The shapes and id
schemes are the same; the text boxes follow pdfjs' convention (baseline up by
the font size, along the run's own direction) so tag positions agree with
artifacts the JS pipeline already produced.
"""
import base64
import math
import re

import pymupdf


def load_pdf(source):
    """`source` is a path or the PDF's bytes."""
    if isinstance(source, (bytes, bytearray)):
        return pymupdf.open(stream=bytes(source), filetype='pdf')
    return pymupdf.open(str(source))


def page_geometry(page, page_index):
    """Visible page size in points, with any /Rotate already applied."""
    return {'pageIndex': page_index, 'width': page.rect.width, 'height': page.rect.height, 'rotation': page.rotation}


def _normalizer(page):
    """Unrotated page space -> normalized visible-page fractions."""
    matrix = page.rotation_matrix
    width, height = page.rect.width, page.rect.height

    def to_norm(x, y):
        p = pymupdf.Point(x, y) * matrix
        return {'x': p.x / width, 'y': p.y / height}

    return to_norm, matrix


def span_box(span, to_norm, matrix):
    """(text, bbox, font size, angle) for one `get_texttrace()` span, or None
    for a blank one. Shared with the revision-markup scan, which needs the
    same boxes to match red text back to the step 1 text layer."""
    chars = span.get('chars') or []
    text = ''.join(chr(c[0]) for c in chars)
    if not text.strip():
        return None

    dx, dy = span['dir']
    # Perpendicular pointing from the baseline toward the glyph tops, in
    # y-down space: (1, 0) -> (0, -1).
    ux, uy = dy, -dx
    ox, oy = chars[0][2]

    # Advance length: how far the run's glyph boxes reach along its direction.
    advance = 0.0
    for c in chars:
        x0, y0, x1, y1 = c[3]
        for cx, cy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
            advance = max(advance, (cx - ox) * dx + (cy - oy) * dy)

    size = span['size']
    corners = [
        to_norm(ox, oy),
        to_norm(ox + dx * advance, oy + dy * advance),
        to_norm(ox + ux * size, oy + uy * size),
        to_norm(ox + dx * advance + ux * size, oy + dy * advance + uy * size),
    ]
    xs = [c['x'] for c in corners]
    ys = [c['y'] for c in corners]
    bbox = {'x': min(xs), 'y': min(ys), 'width': max(xs) - min(xs), 'height': max(ys) - min(ys)}

    # Rotation of the run in visible-page space, degrees clockwise from
    # left-to-right. Kept so later stages can tell a vertical line label
    # apart from a horizontal equipment tag.
    rdx, rdy = pymupdf.Point(dx, dy) * pymupdf.Matrix(matrix.a, matrix.b, matrix.c, matrix.d, 0, 0)
    angle = math.floor(math.degrees(math.atan2(rdy, rdx)) + 0.5)
    return text, bbox, size, angle


def extract_text_items(page, page_index):
    """Every text run on the page with its exact box.

    `get_texttrace()` reports one span per text-showing run in the content
    stream, the closest PyMuPDF equivalent of pdfjs' text items. The box is
    built the way pdfjs builds it — from the baseline origin, along the run's
    advance direction, and up by the font size — then walked through the page
    rotation, so rotated tags (vertical line labels are common on P&IDs) keep
    a real width instead of collapsing to a zero-width box.
    """
    to_norm, matrix = _normalizer(page)
    out = []

    for i, span in enumerate(page.get_texttrace()):
        box = span_box(span, to_norm, matrix)
        if box is None:
            continue
        text, bbox, size, angle = box

        out.append({
            'id': f'p{page_index}-t{i}',
            'page': page_index,
            # pdfjs collapses runs of whitespace inside an item; match it so tags
            # like "DWG No.  PID-2201-001" compare equal across implementations.
            'str': re.sub(r'\s+', ' ', text.strip()),
            'bbox': bbox,
            'fontSize': size,
            'angle': angle,
            'fontName': span.get('font'),
        })

    return out


def extract_vector_segments(page, page_index, min_length=0.002):
    """Stroked straight-line segments from the page's vector content.

    On a CAD-exported P&ID this is the actual drawn geometry — pipe runs,
    symbol outlines, leader lines, border rules — at full precision. Only
    stroked paths are kept (a filled-only shape is a symbol body, not a
    pipe), curves are reduced to their chord, and rectangles and quads are
    emitted as their four sides. Segments carry the stroke width so a
    consumer can separate heavy process lines from light instrument leaders.
    """
    to_norm, _ = _normalizer(page)
    segments = []

    for path in page.get_drawings():
        if path.get('type') not in ('s', 'fs'):
            continue
        width = path.get('width') or 0
        dashed = (path.get('dashes') or '[] 0').strip() not in ('[] 0', '[]', '')
        pending = []
        start = None
        cur = None

        def push(a, b, approx=False):
            seg = {'a': to_norm(a.x, a.y), 'b': to_norm(b.x, b.y), 'width': width, 'dashed': dashed}
            if approx:
                seg['approx'] = True
            pending.append(seg)

        for item in path['items']:
            kind = item[0]
            if kind == 'l':
                a, b = item[1], item[2]
                if cur is None or abs(cur.x - a.x) > 1e-6 or abs(cur.y - a.y) > 1e-6:
                    start = a
                push(a, b)
                cur = b
            elif kind == 'c':
                # Approximate a bezier by its chord: pipe routing on a P&ID is
                # overwhelmingly orthogonal, so curves here are almost always
                # symbol detail we only need the extent of.
                a, b = item[1], item[4]
                if cur is None or abs(cur.x - a.x) > 1e-6 or abs(cur.y - a.y) > 1e-6:
                    start = a
                push(a, b, approx=True)
                cur = b
            elif kind == 're':
                r = item[1]
                pts = [r.tl, r.tr, r.br, r.bl]
                for k in range(4):
                    push(pts[k], pts[(k + 1) % 4])
                start = cur = r.tl
            elif kind == 'qu':
                q = item[1]
                pts = [q.ul, q.ur, q.lr, q.ll]
                for k in range(4):
                    push(pts[k], pts[(k + 1) % 4])
                start = cur = q.ul

        if path.get('closePath') and cur is not None and start is not None and (cur.x, cur.y) != (start.x, start.y):
            push(cur, start)

        for seg in pending:
            length = math.hypot(seg['b']['x'] - seg['a']['x'], seg['b']['y'] - seg['a']['y'])
            if length >= min_length:
                seg['length'] = length
                segments.append(seg)

    return [{'id': f'p{page_index}-s{i}', 'page': page_index, **s} for i, s in enumerate(segments)]


def render_region(page, region, target_px=1600):
    """Rasterizes an axis-aligned region of the page, given in normalized
    visible-page fractions, to PNG bytes. `target_px` caps the longest
    rendered side — the region is scaled up as far as that allows, which is
    what makes small instrument bubbles legible inside a tile."""
    width, height = page.rect.width, page.rect.height
    x0 = region['x'] * width
    y0 = region['y'] * height
    region_w = region['width'] * width
    region_h = region['height'] * height
    scale = target_px / max(region_w, region_h)

    # `clip` is in visible (rotation-applied) page space, the same space as
    # `region`: get_pixmap applies the page rotation before clipping. Passing
    # it through derotation_matrix rendered the wrong part of a /Rotate page.
    clip = pymupdf.Rect(x0, y0, x0 + region_w, y0 + region_h)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False)
    return {'png': pix.tobytes('png'), 'width': pix.width, 'height': pix.height, 'scale': scale}


def to_data_url(png: bytes):
    return 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')
