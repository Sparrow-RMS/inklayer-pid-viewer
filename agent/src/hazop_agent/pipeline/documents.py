"""The study package's documents, as text the pipeline can use.

A study is a drawing (its job id); its documents are the files attached in
the viewer's study package (study_package.py). This module finds them and
turns each into plain text, tables kept as `cell | cell` rows:

- Word (.docx) and Excel (.xlsx / .xlsm) are zip files of XML, read directly;
- PDF through PyMuPDF's text layer (a scanned PDF has none and yields nothing);
- .txt / .csv as text.

Anything else (.doc, .xls, images, CAD) is listed as not readable yet rather
than silently ignored. The P&ID itself is never read from here: the pipeline
already reads the drawing it runs on.
"""
import hashlib
import json
import re
import sqlite3
import zipfile
from contextlib import closing
from xml.etree import ElementTree as ET

from . import jobs

# Document types the node pipeline reads, in the order they are given to the model.
USED_TYPES = ('process_description', 'interlocks', 'pfd', 'parameters', 'datasheets', 'layout')
TYPE_NAMES = {
    'process_description': 'Process description', 'interlocks': 'Interlock list / Cause & Effect matrix',
    'pfd': 'PFD', 'parameters': 'Process parameters with alarm and trip set values',
    'datasheets': 'Equipment data sheet', 'layout': 'Plant layout',
}
MAX_CHARS = 40_000  # per document, so one long file cannot crowd out the drawing

_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
_S = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'


def _docx_text(path):
    root = ET.fromstring(zipfile.ZipFile(path).read('word/document.xml'))
    out = []
    for el in root.find(f'{_W}body'):
        if el.tag == f'{_W}p':
            text = ''.join(t.text or '' for t in el.iter(f'{_W}t')).strip()
            if text:
                out.append(text)
        elif el.tag == f'{_W}tbl':
            for row in el.iter(f'{_W}tr'):
                cells = [' '.join(''.join(t.text or '' for t in p.iter(f'{_W}t')) for p in cell.iter(f'{_W}p')).strip()
                         for cell in row.iter(f'{_W}tc')]
                out.append(' | '.join(cells))
    return '\n'.join(out)


def _xlsx_text(path):
    z = zipfile.ZipFile(path)
    shared = []
    if 'xl/sharedStrings.xml' in z.namelist():
        for si in ET.fromstring(z.read('xl/sharedStrings.xml')).iter(f'{_S}si'):
            shared.append(''.join(t.text or '' for t in si.iter(f'{_S}t')))
    out = []
    sheets = sorted(n for n in z.namelist() if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n))
    for name in sheets:
        out.append(f'[{name.rsplit("/", 1)[-1][:-4]}]')
        for row in ET.fromstring(z.read(name)).iter(f'{_S}row'):
            cells = []
            for c in row.iter(f'{_S}c'):
                v = c.find(f'{_S}v')
                inline = c.find(f'{_S}is')
                if c.get('t') == 's' and v is not None:
                    cells.append(shared[int(v.text)])
                elif inline is not None:
                    cells.append(''.join(t.text or '' for t in inline.iter(f'{_S}t')))
                else:
                    cells.append(v.text if v is not None else '')
            if any(cells):
                out.append(' | '.join(cells))
    return '\n'.join(out)


def _pdf_text(path):
    import pymupdf
    with pymupdf.open(path) as doc:
        return '\n'.join(page.get_text() for page in doc).strip()


def extract_text(path):
    """Plain text of one file, or None when its format is not readable yet."""
    suffix = path.suffix.lower()
    if suffix == '.docx':
        return _docx_text(path)
    if suffix in ('.xlsx', '.xlsm'):
        return _xlsx_text(path)
    if suffix == '.pdf':
        return _pdf_text(path)
    if suffix in ('.txt', '.csv'):
        return path.read_text(errors='replace')
    return None


def study_documents(study_id):
    """The study package's documents the pipeline uses, with their text.
    Reads the worksheet database directly, so it works without the server."""
    from ..worksheets import db_path  # the package records live in the worksheet database
    path = db_path()
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute('SELECT * FROM study_documents WHERE study_id = ? ORDER BY uploaded_at, rowid', (study_id,)).fetchall()
        except sqlite3.OperationalError:
            return []  # no package table yet
    docs = []
    for r in rows:
        if r['doc_type'] not in USED_TYPES:
            continue
        file = jobs.jobs_root() / 'studies' / study_id / 'documents' / r['id'] / r['file_name']
        text = extract_text(file) if file.exists() else None
        docs.append({
            'id': r['id'], 'type': r['doc_type'], 'typeName': TYPE_NAMES[r['doc_type']], 'fileName': r['file_name'],
            'sha256': r['sha256'], 'readable': text is not None,
            'text': (text or '')[:MAX_CHARS], 'truncated': bool(text) and len(text) > MAX_CHARS,
        })
    docs.sort(key=lambda d: USED_TYPES.index(d['type']))
    return docs


def signature(docs):
    """Changes whenever a used document is added, removed, replaced or retyped."""
    return hashlib.sha256(json.dumps(sorted((d['type'], d['sha256']) for d in docs)).encode()).hexdigest()[:16]


def format_documents(docs, types=USED_TYPES):
    """The documents of the given types as one prompt section ('' if none)."""
    parts = []
    for d in docs:
        if d['type'] not in types:
            continue
        if not d['readable']:
            parts.append(f"### {d['typeName']}: {d['fileName']}\n(attached, but its format cannot be read yet)")
            continue
        more = '\n[... truncated]' if d['truncated'] else ''
        parts.append(f"### {d['typeName']}: {d['fileName']}\n{d['text']}{more}")
    if not parts:
        return ''
    return ('SUPPORTING DOCUMENTS from the study package. They describe the plant in words and tables: '
            'operations, operating modes, design intent, interlocks and data. Use them for what the drawing '
            'cannot show. Where a document covers several drawings, use only what concerns this sheet. '
            'Tags, lines and geometry come from the drawing; where a document disagrees with the drawing, '
            'follow the drawing and say so in your notes. A value a document states (a flow, a set point) '
            'replaces an UNVERIFIED value; name the document as its source.\n\n' + '\n\n'.join(parts))
