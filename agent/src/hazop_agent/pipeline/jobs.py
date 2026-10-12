"""Job artifacts on disk.

Each pipeline stage writes its output as JSON under a per-document job
directory, so a later stage can be re-run — and re-tuned — without paying for
the earlier ones again.

The job id is derived from the PDF's CONTENT, not the time or the file name:
re-running the same drawing lands in the same directory and reuses what is
already there, while an edited drawing gets a fresh one.

By default this is `data/jobs/` at the repo root (PIPELINE_JOBS_DIR moves it).
The id scheme and artifact names are the old JS pipeline's (server/src/pipeline),
which defaults to the same directory, so either can still read the other's jobs.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

# agent/src/hazop_agent/pipeline/jobs.py -> repo root
DEFAULT_JOBS_ROOT = Path(__file__).resolve().parents[4] / 'data' / 'jobs'


def jobs_root():
    # PIPELINE_JOBS_DIR is read at call time so tests and callers can redirect it.
    return Path(os.environ.get('PIPELINE_JOBS_DIR') or DEFAULT_JOBS_ROOT)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _slug(name):
    stem = Path(name).stem.lower()
    s = re.sub(r'[^a-z0-9]+', '-', stem).strip('-')[:40]
    return s or 'document'


def job_id_for(file_name, data: bytes):
    return f'{_slug(file_name)}-{hashlib.sha256(data).hexdigest()[:12]}'


def job_dir(job_id):
    return jobs_root() / job_id


def ensure_job(job_id, **meta):
    d = job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    existing = read_manifest(job_id)
    manifest = {'jobId': job_id, 'createdAt': existing.get('createdAt', now_iso()), **existing, **meta, 'updatedAt': now_iso()}
    (d / 'job.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    return d


def read_manifest(job_id):
    path = job_dir(job_id) / 'job.json'
    return json.loads(path.read_text()) if path.exists() else {}


def artifact_path(job_id, name):
    return job_dir(job_id) / f'{name}.json'


def has_artifact(job_id, name):
    return artifact_path(job_id, name).exists()


def write_artifact(job_id, name, data):
    job_dir(job_id).mkdir(parents=True, exist_ok=True)
    path = artifact_path(job_id, name)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return path


def read_artifact(job_id, name):
    return json.loads(artifact_path(job_id, name).read_text())


def source_path(job_id):
    return job_dir(job_id) / 'source.pdf'


def stash_source(job_id, data: bytes):
    """Keeps the source PDF beside its artifacts so a re-run needs only the job id."""
    path = source_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(data)
    return path


def list_jobs():
    root = jobs_root()
    if not root.exists():
        return []
    jobs = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        manifest_path = d / 'job.json'
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {'jobId': d.name}
        artifacts = sorted(p.stem for p in d.glob('*.json') if p.name != 'job.json')
        jobs.append({**manifest, 'artifacts': artifacts})
    return sorted(jobs, key=lambda j: str(j.get('updatedAt', '')), reverse=True)
