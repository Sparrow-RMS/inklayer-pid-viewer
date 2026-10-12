import shutil
from pathlib import Path

import pytest

AGENT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = AGENT_ROOT.parent
SERVER_JOBS = REPO_ROOT / 'data' / 'jobs'  # the real jobs, for tests that replay them
FIXTURE_PDF = Path(__file__).parent / 'fixtures' / 'pid-sample.pdf'


@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    """An isolated PIPELINE_JOBS_DIR, so tests never touch data/jobs."""
    d = tmp_path / 'jobs'
    d.mkdir()
    monkeypatch.setenv('PIPELINE_JOBS_DIR', str(d))
    return d


def copy_job(job_id, dest_root, extra=None):
    """Copies a real job from data/jobs into a test jobs dir."""
    src = SERVER_JOBS / job_id
    if not src.exists():
        pytest.skip(f'{job_id} not present in data/jobs')
    shutil.copytree(src, dest_root / job_id)
    for name, path in (extra or {}).items():
        shutil.copy(path, dest_root / job_id / name)
    return dest_root / job_id
