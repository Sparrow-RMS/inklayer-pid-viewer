"""Drawings already analysed, so the viewer can reopen one without uploading it again.

    GET /agent/jobs                      drawings whose pipeline produced nodes, newest first
    GET /agent/jobs/{job_id}/source.pdf  the drawing as uploaded
    GET /agent/jobs/{job_id}/nodes       its nodes as the viewer last showed them (edits applied),
                                         in the shape of /agent/run's result event

A job is listed for the current provider (jobs.results_dir): results from the
other provider aren't the nodes this server's runs and worksheets work with.
Reopening uses the drawing's own file name, so its job id, worksheets, study
package and chat come back with it, exactly as after an upload.
"""
import re
import sqlite3
from contextlib import closing

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .pipeline import edits, jobs
from .worksheets import db_path

router = APIRouter()


def _check_id(job_id):
    # Job ids are <slug>-<12 hex>; anything else could walk out of the jobs root.
    if not re.fullmatch(r'[a-z0-9-]+', job_id) or not jobs.job_dir(job_id).is_dir():
        raise HTTPException(404, 'no such drawing')


def _worksheet_counts():
    """{study id: (deviations, nodes with a worksheet)} for every study."""
    if not db_path().exists():
        return {}
    with closing(sqlite3.connect(db_path())) as conn:
        try:
            rows = conn.execute('SELECT study_id, COUNT(*), COUNT(DISTINCT node_id) FROM hazop_deviations GROUP BY study_id').fetchall()
        except sqlite3.OperationalError:  # no worksheets table yet
            return {}
    return {study: (deviations, nodes) for study, deviations, nodes in rows}


def _summary(job_id, counts):
    if not jobs.source_path(job_id).exists() or not jobs.has_artifact(job_id, 'step3'):
        return None
    try:
        document = edits.current_document(job_id)
    except (OSError, ValueError, KeyError):
        return None
    pages = document.get('pages', [])
    nodes = sum(len(p.get('nodes', [])) for p in pages)
    if not nodes:
        return None
    manifest = jobs.read_manifest(job_id) or jobs.read_manifest(job_id, 'openai')
    deviations, worksheet_nodes = counts.get(job_id, (0, 0))
    return {
        'jobId': job_id,
        'fileName': manifest.get('fileName') or f'{job_id}.pdf',
        'updatedAt': manifest.get('updatedAt') or manifest.get('createdAt'),
        'pages': len(pages),
        'nodes': nodes,
        'consolidated': edits.view_base(job_id) == 'step3-consolidated',
        'deviations': deviations,
        'nodesWithWorksheet': worksheet_nodes,
        'has3dView': (jobs.job_dir(job_id) / 'plant3d.html').exists(),
    }


@router.get('/agent/jobs')
async def list_drawings():
    root = jobs.jobs_root()
    if not root.exists():
        return []
    counts = _worksheet_counts()
    found = [_summary(d.name, counts) for d in root.iterdir() if d.is_dir() and d.name != 'studies']
    return sorted((f for f in found if f), key=lambda f: f['updatedAt'] or '', reverse=True)


@router.get('/agent/jobs/{job_id}/source.pdf')
async def source_pdf(job_id: str):
    _check_id(job_id)
    path = jobs.source_path(job_id)
    if not path.exists():
        raise HTTPException(404, 'the drawing is no longer stored')
    return FileResponse(path, media_type='application/pdf')


@router.get('/agent/jobs/{job_id}/nodes')
async def saved_nodes(job_id: str):
    _check_id(job_id)
    if not jobs.has_artifact(job_id, 'step3'):
        raise HTTPException(404, 'the HAZOP pipeline has not finished on this drawing')
    base = edits.view_base(job_id)
    step3 = jobs.read_artifact(job_id, 'step3')
    return {
        'type': 'result',
        'jobId': job_id,
        'fileName': (jobs.read_manifest(job_id) or {}).get('fileName'),
        'consolidated': base == 'step3-consolidated',
        'document': edits.current_document(job_id, base),
        'validation': [{'pageIndex': p['pageIndex'], **p['validation']} for p in step3.get('pages', []) if 'validation' in p],
        'issues': [],
    }
