"""HAZOP worksheets, in the hierarchy HAZOP teams record them:

    node -> deviation (parameter, guide word, text)
         -> cause
         -> consequence (severity, likelihood: the risk is rated per consequence)
         -> safeguards, recommendations (each consequence has its own lists)

Numbered 1 / 1.1 / 1.1.1 / 1.1.1.1 by position, as worksheets are read.

    GET    /agent/hazop/studies/{study}/worksheet[?nodeId=]       the tree (one node or the whole drawing)
    GET    /agent/hazop/studies/{study}/summary                   deviation counts per node
    POST   /agent/hazop/studies/{study}/deviations                add a deviation
    POST   /agent/hazop/studies/{study}/deviations/bulk           add deviations with their whole subtree
    POST   /agent/hazop/studies/{study}/nodes/{nodeId}/generate   draft a node's worksheet with the model (step 4)
    POST   /agent/hazop/deviations/{id}/causes                    add a cause
    POST   /agent/hazop/causes/{id}/consequences                  add a consequence
    POST   /agent/hazop/consequences/{id}/safeguards              add a safeguard
    POST   /agent/hazop/consequences/{id}/recommendations         add a recommendation
    PATCH  /agent/hazop/{deviations|causes|consequences|safeguards|recommendations}/{id}
    DELETE /agent/hazop/{deviations|causes|consequences|safeguards|recommendations}/{id}   (and everything under it)

A study is one drawing, keyed by the pipeline's job id. Worksheets saved in
the earlier flat format (one row per cause) are migrated on first use and the
old table is kept as hazop_rows_legacy.
"""
import asyncio
import os
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .pipeline import jobs
from .streaming import STREAM_HEADERS, json_when_done

RecommendationStatus = Literal['open', 'in progress', 'closed']

_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS hazop_deviations (
    id TEXT PRIMARY KEY, study_id TEXT NOT NULL, node_id TEXT NOT NULL, node_label TEXT NOT NULL DEFAULT '',
    parameter TEXT NOT NULL DEFAULT '', guideword TEXT NOT NULL DEFAULT '', deviation TEXT NOT NULL DEFAULT '',
    position REAL NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS hazop_deviations_node ON hazop_deviations (study_id, node_id);
CREATE TABLE IF NOT EXISTS hazop_causes (
    id TEXT PRIMARY KEY, deviation_id TEXT NOT NULL REFERENCES hazop_deviations(id) ON DELETE CASCADE,
    text TEXT NOT NULL DEFAULT '', position REAL NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hazop_consequences (
    id TEXT PRIMARY KEY, cause_id TEXT NOT NULL REFERENCES hazop_causes(id) ON DELETE CASCADE,
    text TEXT NOT NULL DEFAULT '', severity INTEGER, likelihood INTEGER,
    position REAL NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hazop_safeguards (
    id TEXT PRIMARY KEY, consequence_id TEXT NOT NULL REFERENCES hazop_consequences(id) ON DELETE CASCADE,
    text TEXT NOT NULL DEFAULT '', position REAL NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hazop_recommendations (
    id TEXT PRIMARY KEY, consequence_id TEXT NOT NULL REFERENCES hazop_consequences(id) ON DELETE CASCADE,
    text TEXT NOT NULL DEFAULT '', owner TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open',
    position REAL NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""

# kind -> (table, parent column, parent kind, editable fields)
KINDS = {
    'deviations': ('hazop_deviations', None, None, ('parameter', 'guideword', 'deviation', 'node_label')),
    'causes': ('hazop_causes', 'deviation_id', 'deviations', ('text',)),
    'consequences': ('hazop_consequences', 'cause_id', 'causes', ('text', 'severity', 'likelihood')),
    'safeguards': ('hazop_safeguards', 'consequence_id', 'consequences', ('text',)),
    'recommendations': ('hazop_recommendations', 'consequence_id', 'consequences', ('text', 'owner', 'status')),
}

# The standard parameters, for deriving one from free text (migration, suggestions).
PARAMETER_WORDS = (
    ('Pressure', ('pressure', 'vacuum')), ('Temperature', ('temperature', 'temp')),
    ('Level', ('level', 'inventory', 'overflow', 'overfill', 'empty')), ('Mixing', ('agitation', 'mixing')),
    ('Composition', ('composition', 'material', 'concentration', 'contamina', 'solids', 'component', 'inhibitor', 'batch', 'foreign')),
    ('Flow', ('flow', 'route', 'routing', 'backflow', 'transfer', 'delivery', 'unloading')), ('Phase', ('phase',)),
    ('Reaction', ('reaction',)),
)


def parameter_of(text):
    t = (text or '').lower()
    for name, words in PARAMETER_WORDS:
        if any(w in t for w in words):
            return name
    return 'Other'


def db_path():
    # Read at call time, like jobs_root(), so tests can redirect it.
    return Path(os.environ.get('HAZOP_DB') or jobs.jobs_root() / 'hazop.sqlite3')


def _connect():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute('PRAGMA foreign_keys = ON')
    _migrate_flat_rows(conn)
    return conn


def _migrate_flat_rows(conn):
    """One-time move of the earlier flat worksheet (one row = guide word +
    parameter text + cause + consequence + safeguards + rating + one
    recommendation) into the hierarchy. The old table is kept, renamed."""
    has_old = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hazop_rows'").fetchone()
    if not has_old:
        return
    with conn:
        for k, r in enumerate(conn.execute('SELECT * FROM hazop_rows ORDER BY created_at, rowid').fetchall()):
            stamp = r['created_at']
            dev_id, cause_id = uuid.uuid4().hex, uuid.uuid4().hex
            detail = r['parameter'].strip()
            conn.execute('INSERT INTO hazop_deviations VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (dev_id, r['study_id'], r['node_id'], r['node_label'], parameter_of(detail), r['guideword'],
                          f"{r['guideword']} {detail}".strip(), k + 1, stamp, r['updated_at']))
            conn.execute('INSERT INTO hazop_causes VALUES (?,?,?,?,?,?)', (cause_id, dev_id, r['cause'], 1, stamp, r['updated_at']))
            safeguards = [s.strip() for s in (r['safeguards'] or '').splitlines() if s.strip() and s.strip().lower() != 'none shown']
            if r['consequence'] or safeguards or r['severity'] or r['likelihood'] or r['recommendation']:
                cons_id = uuid.uuid4().hex
                conn.execute('INSERT INTO hazop_consequences VALUES (?,?,?,?,?,?,?,?)',
                             (cons_id, cause_id, r['consequence'], r['severity'], r['likelihood'], 1, stamp, r['updated_at']))
                for i, s in enumerate(safeguards):
                    conn.execute('INSERT INTO hazop_safeguards VALUES (?,?,?,?,?,?)', (uuid.uuid4().hex, cons_id, s, i + 1, stamp, stamp))
                if r['recommendation']:
                    conn.execute('INSERT INTO hazop_recommendations VALUES (?,?,?,?,?,?,?,?)',
                                 (uuid.uuid4().hex, cons_id, r['recommendation'], r['owner'], r['status'], 1, stamp, stamp))
        conn.execute('ALTER TABLE hazop_rows RENAME TO hazop_rows_legacy')


# ───────────────────────────────────────────────────────────── reading

def _tree(conn, study_id, node_id=None):
    q, args = 'SELECT * FROM hazop_deviations WHERE study_id = ?', [study_id]
    if node_id:
        q += ' AND node_id = ?'
        args.append(node_id)
    deviations = [dict(r) for r in conn.execute(q + ' ORDER BY position, created_at', args)]
    by_dev = {d['id']: d for d in deviations}
    out = []
    for d in deviations:
        d['causes'] = []
    if not deviations:
        return out
    marks = ','.join('?' * len(by_dev))
    causes = [dict(r) for r in conn.execute(f'SELECT * FROM hazop_causes WHERE deviation_id IN ({marks}) ORDER BY position, created_at', list(by_dev))]
    by_cause = {}
    for c in causes:
        c['consequences'] = []
        by_cause[c['id']] = c
        by_dev[c['deviation_id']]['causes'].append(c)
    if by_cause:
        marks = ','.join('?' * len(by_cause))
        cons = [dict(r) for r in conn.execute(f'SELECT * FROM hazop_consequences WHERE cause_id IN ({marks}) ORDER BY position, created_at', list(by_cause))]
        by_cons = {}
        for c in cons:
            c['safeguards'], c['recommendations'] = [], []
            by_cons[c['id']] = c
            by_cause[c['cause_id']]['consequences'].append(c)
        if by_cons:
            marks = ','.join('?' * len(by_cons))
            for s in conn.execute(f'SELECT * FROM hazop_safeguards WHERE consequence_id IN ({marks}) ORDER BY position, created_at', list(by_cons)):
                by_cons[s['consequence_id']]['safeguards'].append(dict(s))
            for s in conn.execute(f'SELECT * FROM hazop_recommendations WHERE consequence_id IN ({marks}) ORDER BY position, created_at', list(by_cons)):
                by_cons[s['consequence_id']]['recommendations'].append(dict(s))
    return [_to_json(d) for d in deviations]


def _to_json(item):
    """snake_case columns -> camelCase for the viewer, recursively."""
    out = {}
    for k, v in item.items():
        key = re.sub(r'_([a-z])', lambda m: m.group(1).upper(), k)
        out[key] = [_to_json(x) for x in v] if isinstance(v, list) else v
    return out


def _get(conn, kind, item_id):
    table = KINDS[kind][0]
    row = conn.execute(f'SELECT * FROM {table} WHERE id = ?', (item_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f'no {kind[:-1]} {item_id}')
    return row


def _next_position(conn, kind, parent_id=None, study_id=None, node_id=None):
    table, parent_col, _, _ = KINDS[kind]
    if parent_col:
        row = conn.execute(f'SELECT MAX(position) FROM {table} WHERE {parent_col} = ?', (parent_id,)).fetchone()
    else:
        row = conn.execute(f'SELECT MAX(position) FROM {table} WHERE study_id = ? AND node_id = ?', (study_id, node_id)).fetchone()
    return (row[0] or 0) + 1


# ───────────────────────────────────────────────────────────── writing

class SafeguardIn(BaseModel):
    text: str = ''


class RecommendationIn(BaseModel):
    text: str = ''
    owner: str = ''
    status: RecommendationStatus = 'open'


class ConsequenceIn(BaseModel):
    text: str = ''
    severity: int | None = Field(None, ge=1, le=5)
    likelihood: int | None = Field(None, ge=1, le=5)
    safeguards: list[SafeguardIn] = []
    recommendations: list[RecommendationIn] = []


class CauseIn(BaseModel):
    text: str = ''
    consequences: list[ConsequenceIn] = []


class DeviationIn(BaseModel):
    nodeId: str = Field(min_length=1)
    nodeLabel: str = ''
    parameter: str = ''
    guideword: str = ''
    deviation: str = ''
    causes: list[CauseIn] = []


class Patch(BaseModel):
    parameter: str | None = None
    guideword: str | None = None
    deviation: str | None = None
    nodeLabel: str | None = None
    text: str | None = None
    severity: int | None = Field(None, ge=1, le=5)
    likelihood: int | None = Field(None, ge=1, le=5)
    owner: str | None = None
    status: RecommendationStatus | None = None


def _insert(conn, kind, values):
    table = KINDS[kind][0]
    now = jobs.now_iso()
    values = {'id': uuid.uuid4().hex, **values, 'created_at': now, 'updated_at': now}
    cols = ', '.join(values)
    conn.execute(f'INSERT INTO {table} ({cols}) VALUES ({", ".join("?" * len(values))})', list(values.values()))
    return values['id']


def _insert_deviation(conn, study_id, d: DeviationIn):
    dev_id = _insert(conn, 'deviations', {
        'study_id': study_id, 'node_id': d.nodeId, 'node_label': d.nodeLabel, 'parameter': d.parameter,
        'guideword': d.guideword, 'deviation': d.deviation,
        'position': _next_position(conn, 'deviations', study_id=study_id, node_id=d.nodeId)})
    for i, c in enumerate(d.causes):
        _insert_cause(conn, dev_id, c, i + 1)
    return dev_id


def _insert_cause(conn, deviation_id, c: CauseIn, position):
    cause_id = _insert(conn, 'causes', {'deviation_id': deviation_id, 'text': c.text, 'position': position})
    for i, q in enumerate(c.consequences):
        _insert_consequence(conn, cause_id, q, i + 1)
    return cause_id


def _insert_consequence(conn, cause_id, q: ConsequenceIn, position):
    cons_id = _insert(conn, 'consequences', {'cause_id': cause_id, 'text': q.text, 'severity': q.severity,
                                             'likelihood': q.likelihood, 'position': position})
    for i, s in enumerate(q.safeguards):
        _insert(conn, 'safeguards', {'consequence_id': cons_id, 'text': s.text, 'position': i + 1})
    for i, r in enumerate(q.recommendations):
        _insert(conn, 'recommendations', {'consequence_id': cons_id, 'text': r.text, 'owner': r.owner, 'status': r.status, 'position': i + 1})
    return cons_id


def _deviation_tree(conn, dev_id):
    row = _get(conn, 'deviations', dev_id)
    return next(d for d in _tree(conn, row['study_id'], row['node_id']) if d['id'] == dev_id)


router = APIRouter(prefix='/agent/hazop')

# Plain `def` endpoints: FastAPI runs them in its threadpool, so the blocking
# sqlite calls never stall the event loop a pipeline run is streaming on.


@router.get('/studies/{study_id}/worksheet')
def get_worksheet(study_id: str, nodeId: str | None = None):
    with closing(_connect()) as conn:
        return _tree(conn, study_id, nodeId)


@router.get('/studies/{study_id}/summary')
def get_summary(study_id: str):
    """Deviations per node, for the "HAZOP worksheet (n)" buttons."""
    with closing(_connect()) as conn:
        rows = conn.execute('SELECT node_id, COUNT(*) n FROM hazop_deviations WHERE study_id = ? GROUP BY node_id', (study_id,))
        return {r['node_id']: r['n'] for r in rows}


@router.post('/studies/{study_id}/deviations', status_code=201)
def create_deviation(study_id: str, d: DeviationIn):
    with closing(_connect()) as conn, conn:
        return _deviation_tree(conn, _insert_deviation(conn, study_id, d))


@router.post('/studies/{study_id}/deviations/bulk', status_code=201)
def create_deviations(study_id: str, items: list[DeviationIn]):
    with closing(_connect()) as conn, conn:
        ids = [_insert_deviation(conn, study_id, d) for d in items]
        return [_deviation_tree(conn, i) for i in ids]


class GenerateIn(BaseModel):
    replace: bool = False  # delete the node's existing deviations first


# Drafts in progress, by (study, node): a second click or a retry waits for the
# draft already running instead of paying for another.
_drafts = {}


@router.post('/studies/{study_id}/nodes/{node_id}/generate', response_class=StreamingResponse)
async def generate_node_worksheet(study_id: str, node_id: str, body: GenerateIn | None = None):
    """Step 4: drafts the node's worksheet (one model call, ~1-3 min) and adds
    it. A drafted deviation already in the worksheet (same guide word and
    text) is skipped unless `replace` clears the node first.

    The response is JSON, preceded by a space every few seconds while the
    model works (proxies close a request that is silent for 60 s); a failure
    after that point comes as {"error": {"status", "detail"}} with status 200.
    The draft runs in the background: it is saved even if the client goes away."""
    from .pipeline.step4.generate import find_node
    replace = bool(body and body.replace)
    # The quick checks first, while a real status code can still be sent.
    try:
        find_node(study_id, node_id)
    except KeyError as err:
        raise HTTPException(404, str(err.args[0])) from err
    except FileNotFoundError as err:
        raise HTTPException(404, f'no pipeline results for {study_id}; run the pipeline first') from err

    key = (study_id, node_id)
    task = _drafts.get(key)
    if task is None:
        task = _drafts[key] = asyncio.create_task(_draft_and_save(study_id, node_id, replace))
        task.add_done_callback(lambda t: _drafts.pop(key, None) if _drafts.get(key) is t else None)
    return StreamingResponse(json_when_done(task), media_type='application/json', headers=STREAM_HEADERS)


async def _draft_and_save(study_id, node_id, replace):
    from .pipeline.step4.generate import generate
    result = await generate(study_id, node_id)

    def save():
        with closing(_connect()) as conn, conn:
            if replace:
                conn.execute('DELETE FROM hazop_deviations WHERE study_id = ? AND node_id = ?', (study_id, node_id))
            have = {(r['guideword'].upper(), r['deviation'].strip().lower()) for r in conn.execute(
                'SELECT guideword, deviation FROM hazop_deviations WHERE study_id = ? AND node_id = ?', (study_id, node_id))}
            added = skipped = 0
            for row in result['rows']:
                if (row['guideword'], row['deviation'].lower()) in have:
                    skipped += 1
                    continue
                _insert_deviation(conn, study_id, DeviationIn(**row))
                added += 1
            return {'deviations': _tree(conn, study_id, node_id), 'added': added, 'skipped': skipped,
                    'warnings': result['warnings'], 'notes': result['draft']['notes']}

    return await asyncio.to_thread(save)


@router.post('/deviations/{dev_id}/causes', status_code=201)
def create_cause(dev_id: str, c: CauseIn):
    with closing(_connect()) as conn, conn:
        _get(conn, 'deviations', dev_id)
        _insert_cause(conn, dev_id, c, _next_position(conn, 'causes', dev_id))
        return _deviation_tree(conn, dev_id)


@router.post('/causes/{cause_id}/consequences', status_code=201)
def create_consequence(cause_id: str, q: ConsequenceIn):
    with closing(_connect()) as conn, conn:
        cause = _get(conn, 'causes', cause_id)
        _insert_consequence(conn, cause_id, q, _next_position(conn, 'consequences', cause_id))
        return _deviation_tree(conn, cause['deviation_id'])


def _deviation_of(conn, kind, item_id):
    """Walks up from any item to its deviation id."""
    row = _get(conn, kind, item_id)
    while kind != 'deviations':
        _, parent_col, parent_kind, _ = KINDS[kind]
        row, kind = _get(conn, parent_kind, row[parent_col]), parent_kind
    return row['id']


@router.post('/consequences/{cons_id}/safeguards', status_code=201)
def create_safeguard(cons_id: str, s: SafeguardIn):
    with closing(_connect()) as conn, conn:
        _get(conn, 'consequences', cons_id)
        _insert(conn, 'safeguards', {'consequence_id': cons_id, 'text': s.text, 'position': _next_position(conn, 'safeguards', cons_id)})
        return _deviation_tree(conn, _deviation_of(conn, 'consequences', cons_id))


@router.post('/consequences/{cons_id}/recommendations', status_code=201)
def create_recommendation(cons_id: str, r: RecommendationIn):
    with closing(_connect()) as conn, conn:
        _get(conn, 'consequences', cons_id)
        _insert(conn, 'recommendations', {'consequence_id': cons_id, 'text': r.text, 'owner': r.owner, 'status': r.status,
                                          'position': _next_position(conn, 'recommendations', cons_id)})
        return _deviation_tree(conn, _deviation_of(conn, 'consequences', cons_id))


COLUMN = {'nodeLabel': 'node_label'}


def _update(kind, item_id, patch: Patch):
    table, _, _, fields = KINDS[kind]
    nullable = {'severity', 'likelihood'}
    sent = patch.model_dump(exclude_unset=True)
    unknown = [k for k in sent if COLUMN.get(k, k) not in fields]
    if unknown:
        raise HTTPException(422, f"{kind} have no field {', '.join(unknown)}")
    changes = {COLUMN.get(k, k): v for k, v in sent.items() if v is not None or k in nullable}
    with closing(_connect()) as conn, conn:
        _get(conn, kind, item_id)
        if changes:
            sets = ', '.join(f'{c} = ?' for c in changes)
            conn.execute(f'UPDATE {table} SET {sets}, updated_at = ? WHERE id = ?', (*changes.values(), jobs.now_iso(), item_id))
        return _deviation_tree(conn, _deviation_of(conn, kind, item_id))


def _delete(kind, item_id):
    with closing(_connect()) as conn, conn:
        _get(conn, kind, item_id)
        conn.execute(f'DELETE FROM {KINDS[kind][0]} WHERE id = ?', (item_id,))  # children go with it (ON DELETE CASCADE)


# One explicit route per kind (not /{kind}/{id}), so other /agent/hazop routes
# such as the study package's /documents/{id} are never shadowed.
for _kind in KINDS:
    def _patch(item_id: str, patch: Patch, kind=_kind):
        return _update(kind, item_id, patch)

    def _remove(item_id: str, kind=_kind):
        _delete(kind, item_id)

    router.add_api_route(f'/{_kind}/{{item_id}}', _patch, methods=['PATCH'])
    router.add_api_route(f'/{_kind}/{{item_id}}', _remove, methods=['DELETE'], status_code=204)
