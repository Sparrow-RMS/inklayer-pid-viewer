"""Builds the legend knowledge base from a P&ID legend sheet (PDF).

    uv run python -m hazop_agent.docs.build_nomenclature src/hazop_agent/docs/Nomenclature.pdf

Writes next to this file:
- nomenclature.json: every code table (abbreviations, instrument identification,
  service fluids, insulation / tracing, equipment classes) and every symbol
  label, by section, with where it sits on the sheet;
- nomenclature.md: the same, compact, for prompts;
- nomenclature/<section>.png: an image of each section, so a model or an
  agent can compare a symbol with the legend's own drawing.

The legend is a drawing, so sections are boxes on the sheet. SECTIONS below
gives them for the legend sheet in this folder (HW037-350-E-PR-DG-002, the
Kanech POL storage terminal legend), in points, read off its table frames.
A different legend sheet needs its own boxes; everything else is generic.
"""
import json
import re
import sys
from pathlib import Path

import pymupdf

HERE = Path(__file__).parent

# name -> (kind, (x0, y0, x1, y1) in pt). kind: 'codes' = CODE - Description
# rows; 'symbols' = a label beside each drawn symbol; 'table' = class rows.
SECTIONS = {
    'Line Legend': ('symbols', (181, 101, 392, 215)),
    'Instrument Line Legend': ('symbols', (181, 218, 392, 435)),
    'Service Fluid Designation': ('codes', (181, 436, 392, 1413)),
    'Valves': ('symbols', (393, 95, 556, 471)),
    'Piping Specialty Items': ('symbols', (393, 483, 556, 1413)),
    'Piping Fittings': ('symbols', (557, 95, 751, 435)),
    'Primary Element Symbol (Flow)': ('symbols', (557, 436, 751, 1085)),
    'Self-Actuated Devices': ('symbols', (557, 1087, 751, 1431)),
    'Control Valve Actuator Symbols': ('symbols', (751, 95, 980, 525)),
    'Instruments Identification': ('codes', (769, 530, 924, 1061)),
    'Lines Identification': ('symbols', (924, 1067, 1138, 1210)),
    'Insulation / Tracing Type': ('codes', (1138, 1065, 1368, 1210)),
    'Utility Connector': ('symbols', (751, 1210, 924, 1431)),
    'Close Drain': ('symbols', (924, 1210, 1138, 1431)),
    'Open Drain': ('symbols', (1138, 1210, 1368, 1431)),
    'Instrument Location': ('symbols', (980, 95, 1368, 334)),
    'Abbreviation (General)': ('codes', (980, 340, 1368, 1061)),
    'Equipment Symbol': ('symbols', (1368, 95, 1955, 954)),
    'Classification of Equipment': ('table', (1368, 954, 1702, 1311)),
    'Miscellaneous': ('symbols', (1702, 954, 1955, 1335)),
    'Primary / Secondary Line / Drain': ('symbols', (1368, 1330, 1702, 1431)),
}
HEADING_SIZE = 13.0  # pt; section and sub-section titles are set larger than entries
SAME_ROW = 3.0       # pt between baselines of one row
CODE = re.compile(r'^[A-Z0-9(][A-Z0-9/()&.\-]{0,7}$')


def _lines(page):
    out = []
    for block in page.get_text('dict')['blocks']:
        for line in block.get('lines', []):
            text = ''.join(s['text'] for s in line['spans']).strip()
            if text:
                x0, y0, x1, y1 = line['bbox']
                out.append({'text': re.sub(r'\s+', ' ', text), 'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1,
                            'size': line['spans'][0]['size']})
    return out


def _inside(line, box):
    cx, cy = (line['x0'] + line['x1']) / 2, (line['y0'] + line['y1']) / 2
    return box[0] <= cx <= box[2] and box[1] <= cy <= box[3]


def _codes(lines):
    """CODE - Description rows: a code, a dash and a description on one baseline."""
    rows, group = [], None
    dashes = [l for l in lines if l['text'] in ('-', '–', '~')]
    for d in sorted(dashes, key=lambda l: (l['y0'], l['x0'])):
        left = [l for l in lines if abs(l['y0'] - d['y0']) <= SAME_ROW and l['x1'] <= d['x0'] + 1 and d['x0'] - l['x1'] < 40]
        right = [l for l in lines if abs(l['y0'] - d['y0']) <= SAME_ROW and l['x0'] >= d['x1'] - 1 and l['x0'] - d['x1'] < 30]
        if not left or not right:
            continue
        code = max(left, key=lambda l: l['x1'])['text']
        desc = min(right, key=lambda l: l['x0'])['text']
        if CODE.match(code):
            rows.append({'code': code, 'meaning': desc, 'y': round(d['y0'], 1), 'x': round(d['x0'], 1)})
    # a sub-heading (e.g. Pressure / Temperature in instrument identification) names the rows under it
    subheads = sorted((l for l in lines if l['size'] >= HEADING_SIZE), key=lambda l: l['y0'])
    for r in rows:
        above = [h for h in subheads if h['y0'] < r['y'] and abs(h['x0'] - r['x']) < 120]
        if above:
            group = above[-1]['text']
            r['group'] = group
    return sorted(rows, key=lambda r: (r['x'], r['y']))


def _symbols(lines):
    """Symbol labels: entry text, with a label set on two lines joined."""
    # one- and two-letter text inside symbol drawings ("L", "T", "M") is part of the symbol, not a label
    entries = [l for l in lines if l['size'] < HEADING_SIZE and len(l['text']) > 2]
    entries.sort(key=lambda l: (round(l['x0'] / 6), l['y0']))
    labels = []
    for l in entries:
        prev = labels[-1] if labels else None
        if prev and abs(prev['x0'] - l['x0']) < 6 and 0 < l['y0'] - prev['y1'] < 4:
            prev['text'] += ' ' + l['text']
            prev['y1'] = l['y1']
        else:
            labels.append(dict(l))
    return [{'label': l['text'], 'x': round(l['x0'], 1), 'y': round(l['y0'], 1)} for l in labels]


def _table(lines):
    """Classification rows: a class letter, a subject and a description."""
    rows = []
    letters = [l for l in lines if re.fullmatch(r'[A-Z]{1,2}', l['text'])]
    for c in sorted(letters, key=lambda l: l['y0']):
        same = sorted((l for l in lines if abs(l['y0'] - c['y0']) <= SAME_ROW and l is not c and l['x0'] > c['x1']), key=lambda l: l['x0'])
        if not same:
            continue
        follow = [l for l in lines if same and 0 < l['y0'] - c['y0'] < 45 and l['x0'] >= same[-1]['x0'] - 2 and abs(l['x0'] - same[-1]['x0']) < 4]
        desc = ' '.join([same[-1]['text']] + [l['text'] for l in sorted(follow, key=lambda l: l['y0'])]) if len(same) > 1 else ''
        rows.append({'class': c['text'], 'subject': same[0]['text'], 'description': desc})
    return rows


def build(pdf_path, out_dir=HERE):
    doc = pymupdf.open(pdf_path)
    page = doc[0]
    lines = _lines(page)
    title = [l['text'] for l in lines if l['x0'] > 1700 and l['y0'] > 1400]
    sections = {}
    crops = out_dir / 'nomenclature'
    crops.mkdir(exist_ok=True)
    for name, (kind, box) in SECTIONS.items():
        inside = [l for l in lines if _inside(l, box)]
        entries = _codes(inside) if kind == 'codes' else _table(inside) if kind == 'table' else _symbols(inside)
        slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
        page.get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=pymupdf.Rect(*box)).save(crops / f'{slug}.png')
        sections[name] = {'kind': kind, 'box': box, 'image': f'nomenclature/{slug}.png', 'entries': entries}
    data = {
        'source': {'file': Path(pdf_path).name, 'titleBlock': title,
                   'note': 'Generic legend from another project (Kanech POL storage terminal). '
                           "A drawing's own legend sheet takes precedence over it."},
        'sections': sections,
    }
    (out_dir / 'nomenclature.json').write_text(json.dumps(data, indent=2, ensure_ascii=False))
    (out_dir / 'nomenclature.md').write_text(to_markdown(data))
    return data


def to_markdown(data):
    out = ['# P&ID legend reference', '',
           f"Source: {data['source']['file']}. {data['source']['note']}", '']
    for name, sec in data['sections'].items():
        if not sec['entries']:
            continue
        out.append(f'## {name}')
        if sec['kind'] == 'codes':
            groups = {}
            for r in sec['entries']:
                groups.setdefault(r.get('group', ''), []).append(r)
            for g, rows in groups.items():
                if g and g != name:
                    out.append(f'{g}:')
                out.append('; '.join(f"{r['code']} = {r['meaning']}" for r in rows))
        elif sec['kind'] == 'table':
            out += [f"- {r['class']}: {r['subject']}" + (f" ({r['description']})" if r['description'] else '') for r in sec['entries']]
        else:
            out.append('; '.join(e['label'] for e in sec['entries']))
        out.append('')
    return '\n'.join(out)


if __name__ == '__main__':
    result = build(sys.argv[1] if len(sys.argv) > 1 else HERE / 'Nomenclature.pdf')
    for name, sec in result['sections'].items():
        print(f"{name:<34} {sec['kind']:<8} {len(sec['entries'])} entries")
