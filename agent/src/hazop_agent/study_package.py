"""The study package: the documents a HAZOP team asks for before it starts,
attached to a drawing, with a readiness checklist.

    GET    /agent/hazop/studies/{study_id}/package                   checklist + every attached document
    POST   /agent/hazop/studies/{study_id}/documents?docType=&fileName=   attach a file (raw body)
    GET    /agent/hazop/documents/{doc_id}/file                      download it
    PATCH  /agent/hazop/documents/{doc_id}                           change its type or note
    DELETE /agent/hazop/documents/{doc_id}                           remove it

A study is one drawing, keyed like the worksheets (the pipeline's job id).
Files are stored under <jobs root>/studies/<study id>/documents/; their
records sit in the worksheet database. The checklist also says where the P&ID
itself partly covers a document (a C&E table printed on the sheet, equipment
data blocks), so the team sees what is genuinely missing.
"""
import hashlib
import os
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .pipeline import jobs, nomenclature
from .worksheets import db_path

# The consultant's list, in the order a team reviews it.
DOCUMENT_TYPES = [
    {'id': 'pid', 'name': 'P&ID', 'why': 'Equipment, lines, valves, instruments and boundaries: what is divided into nodes.'},
    {'id': 'pfd', 'name': 'PFD', 'why': 'Design flows, stream compositions and normal P/T: the design intent each deviation is measured from.'},
    {'id': 'layout', 'name': 'Plant layout', 'why': 'Where equipment sits (elevations, bunds, neighbours): consequences such as spill or fire spread.'},
    {'id': 'interlocks', 'name': 'Interlock list / Cause & Effect matrix', 'why': 'Every trip and permissive with its final action: the automatic safeguards.'},
    {'id': 'process_description', 'name': 'Process description', 'why': 'Operations, sequences and operating modes: how the plant is meant to run.'},
    {'id': 'parameters', 'name': 'Process parameters with alarm and trip set values', 'why': 'Normal, alarm and trip values per tag: how much margin each safeguard has.'},
    {'id': 'datasheets', 'name': 'Equipment data sheets', 'why': 'Design ratings of vessels, pumps, filters, compressors, motors, condensers and PSVs: shut-off head, relief set points and capacities.'},
]
TYPE_IDS = {t['id'] for t in DOCUMENT_TYPES}
ALLOWED_EXTENSIONS = {'.pdf', '.xlsx', '.xls', '.xlsm', '.csv', '.docx', '.doc', '.txt', '.png', '.jpg', '.jpeg', '.dwg', '.dxf'}
MAX_BYTES = 50 * 1024 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS study_documents (
    id           TEXT PRIMARY KEY,
    study_id     TEXT NOT NULL,
    doc_type     TEXT NOT NULL,
    file_name    TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT '',
    size         INTEGER NOT NULL,
    sha256       TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    uploaded_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS study_documents_study ON study_documents (study_id);
"""


def _connect():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _documents_dir(study_id):
    return jobs.jobs_root() / 'studies' / study_id / 'documents'


def _safe_name(name):
    name = Path(name or 'document').name
    return re.sub(r'[^A-Za-z0-9._ ()&-]+', '_', name)[:150] or 'document'


def _to_json(r):
    return {'id': r['id'], 'studyId': r['study_id'], 'docType': r['doc_type'], 'fileName': r['file_name'],
            'contentType': r['content_type'], 'size': r['size'], 'sha256': r['sha256'], 'note': r['note'],
            'uploadedAt': r['uploaded_at']}


def _drawing_coverage(study_id):
    """What the drawing itself already shows of each document, from the
    pipeline's artifacts for this study (empty when it hasn't run)."""
    out = {}
    if jobs.source_path(study_id).exists():
        out['pid'] = 'The drawing this study is for.'
    if jobs.has_artifact(study_id, 'step1b-sheet'):
        sheet = jobs.read_artifact(study_id, 'step1b-sheet')
        rows = sum(len(p['interlockTable']['interlocks']) for p in sheet['pages'] if p['interlockTable'].get('tablePresent'))
        if rows:
            out['interlocks'] = f'{rows} C&E rows are printed on the drawing; a full interlock list may have more.'
    if jobs.has_artifact(study_id, 'step1'):
        step1 = jobs.read_artifact(study_id, 'step1')
        entities = [e for p in step1['pages'] for e in p['entities']]
        blocks = [e for e in entities if e['category'] == 'equipment_list_row']
        if blocks:
            out['datasheets'] = f'{len(blocks)} equipment data blocks on the drawing give design P/T, MOC and capacity; datasheets add shut-off head, relief capacity and ratings.'
        alarms = [e for e in entities if e['category'] == 'instrument'
                  and re.search(r'alarm|switch', nomenclature.decode_instrument(e.get('tag')) or '')]
        set_points = [e for e in entities if any(a['key'] in ('setPoint', 'setPressure', 'alarmLevels') for a in e.get('attributes') or [])]
        if alarms or set_points:
            out['parameters'] = f'{len(alarms)} alarm / trip instruments and {len(set_points)} printed set points on the drawing; the set-value list gives the rest.'
    return out


def checklist(study_id, documents):
    coverage = _drawing_coverage(study_id)
    items = []
    for t in DOCUMENT_TYPES:
        files = [d for d in documents if d['docType'] == t['id']]
        if files or (t['id'] == 'pid' and 'pid' in coverage):
            status = 'provided'
        elif t['id'] in coverage:
            status = 'partial'
        else:
            status = 'missing'
        items.append({**t, 'status': status, 'fromDrawing': coverage.get(t['id']), 'documentIds': [d['id'] for d in files]})
    provided = sum(1 for i in items if i['status'] == 'provided')
    return {
        'items': items,
        'provided': provided,
        'partial': sum(1 for i in items if i['status'] == 'partial'),
        'missing': [i['name'] for i in items if i['status'] != 'provided'],
        'total': len(items),
        'ready': provided == len(items),
    }


router = APIRouter(prefix='/agent/hazop')


@router.get('/studies/{study_id}/package')
def get_package(study_id: str):
    with closing(_connect()) as conn:
        docs = [_to_json(r) for r in conn.execute(
            'SELECT * FROM study_documents WHERE study_id = ? ORDER BY uploaded_at, rowid', (study_id,))]
    return {'studyId': study_id, 'documentTypes': DOCUMENT_TYPES, 'documents': docs, 'checklist': checklist(study_id, docs)}


@router.post('/studies/{study_id}/documents', status_code=201,
             openapi_extra={'requestBody': {'required': True, 'content': {'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}}}}})
async def upload_document(study_id: str, request: Request, docType: str = Query(...), fileName: str = Query(...)):
    if docType not in TYPE_IDS:
        raise HTTPException(422, f'unknown document type {docType!r}; one of {sorted(TYPE_IDS)}')
    name = _safe_name(fileName)
    if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(415, f'{Path(name).suffix or "no extension"} files are not accepted; use one of {sorted(ALLOWED_EXTENSIONS)}')
    data = await request.body()
    if not data:
        raise HTTPException(400, 'empty file')
    if len(data) > MAX_BYTES:
        raise HTTPException(413, f'file is larger than {MAX_BYTES // (1024 * 1024)} MB')
    doc_id = uuid.uuid4().hex
    folder = _documents_dir(study_id) / doc_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)
    with closing(_connect()) as conn, conn:
        conn.execute('INSERT INTO study_documents (id, study_id, doc_type, file_name, content_type, size, sha256, uploaded_at) '
                     'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                     (doc_id, study_id, docType, name, request.headers.get('content-type', ''), len(data),
                      hashlib.sha256(data).hexdigest(), jobs.now_iso()))
        return _to_json(conn.execute('SELECT * FROM study_documents WHERE id = ?', (doc_id,)).fetchone())


def _get(conn, doc_id):
    row = conn.execute('SELECT * FROM study_documents WHERE id = ?', (doc_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f'no document {doc_id}')
    return row


@router.get('/documents/{doc_id}/file')
def download_document(doc_id: str):
    with closing(_connect()) as conn:
        row = _get(conn, doc_id)
    path = _documents_dir(row['study_id']) / doc_id / row['file_name']
    if not path.exists():
        raise HTTPException(410, 'the file is no longer on disk')
    return FileResponse(path, filename=row['file_name'], media_type=row['content_type'] or None)


class DocumentPatch(BaseModel):
    docType: str | None = None
    note: str | None = None


@router.patch('/documents/{doc_id}')
def update_document(doc_id: str, patch: DocumentPatch):
    if patch.docType is not None and patch.docType not in TYPE_IDS:
        raise HTTPException(422, f'unknown document type {patch.docType!r}')
    with closing(_connect()) as conn, conn:
        _get(conn, doc_id)
        if patch.docType is not None:
            conn.execute('UPDATE study_documents SET doc_type = ? WHERE id = ?', (patch.docType, doc_id))
        if patch.note is not None:
            conn.execute('UPDATE study_documents SET note = ? WHERE id = ?', (patch.note, doc_id))
        return _to_json(_get(conn, doc_id))


@router.delete('/documents/{doc_id}', status_code=204)
def delete_document(doc_id: str):
    with closing(_connect()) as conn, conn:
        row = _get(conn, doc_id)
        conn.execute('DELETE FROM study_documents WHERE id = ?', (doc_id,))
    folder = _documents_dir(row['study_id']) / doc_id
    for f in folder.glob('*'):
        f.unlink()
    if folder.exists():
        os.rmdir(folder)
