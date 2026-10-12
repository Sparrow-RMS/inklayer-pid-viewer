"""The Python port against the JS pipeline it replaces.

Replays every cached raw model response under data/jobs through BOTH
implementations' deterministic "assemble" halves (the JS side via
js_reference.mjs) and requires identical artifacts, ignoring only
`generatedAt`/`durationMs`. No model calls. Skipped when node or the JS
pipeline isn't available.
"""
import json
import math
import shutil
import subprocess

import pytest

from hazop_agent.pipeline.step1.extract import assemble_document as assemble_step1
from hazop_agent.pipeline.step2.classify import assemble_document as assemble_step2
from hazop_agent.pipeline.step3.cluster import assemble_document as assemble_step3
from hazop_agent.pipeline.step3.consolidate import assemble_document as assemble_consolidate

from .conftest import REPO_ROOT, SERVER_JOBS

JS_PIPELINE = REPO_ROOT / 'server' / 'src' / 'pipeline'
JS_REFERENCE = REPO_ROOT / 'agent' / 'tests' / 'js_reference.mjs'
# Written outside its job directory by an earlier JS run; belongs to pid-f9daf1158400.
STRAY_CONSOLIDATE_RAW = REPO_ROOT / 'server' / 'step3-consolidate-raw.json'

pytestmark = pytest.mark.skipif(not shutil.which('node') or not JS_PIPELINE.exists(), reason='node or server/src/pipeline unavailable')

REPLAY = {
    'step1': ('step1-raw', lambda raw: assemble_step1(raw['pages'], raw['fileName'], raw['pageCount'])),
    'step2': ('step2-raw', lambda raw: assemble_step2(raw['pages'], raw['fileName'])),
    'step3': ('step3-raw', lambda raw: assemble_step3(raw['pages'], raw['fileName'])),
}


def differences(py, js, path='', out=None):
    out = [] if out is None else out
    if len(out) > 10:
        return out
    if isinstance(py, dict) and isinstance(js, dict):
        for k in py.keys() | js.keys():
            # sourceNodesSignature/blockedMerges: Python-only additions (the
            # node rules' reduced option); the merge arithmetic is still shared.
            if k in ('generatedAt', 'durationMs', 'sourceNodesSignature', 'blockedMerges'):
                continue
            if k not in py or k not in js:
                out.append(f"{path}.{k} missing in {'py' if k not in py else 'js'}")
            else:
                differences(py[k], js[k], f'{path}.{k}', out)
    elif isinstance(py, list) and isinstance(js, list):
        if len(py) != len(js):
            out.append(f'{path}: length {len(py)} vs {len(js)}')
        for i, (a, b) in enumerate(zip(py, js)):
            differences(a, b, f'{path}[{i}]', out)
    elif isinstance(py, (int, float)) and isinstance(js, (int, float)) and not isinstance(py, bool) and not isinstance(js, bool):
        if not math.isclose(py, js, rel_tol=1e-12, abs_tol=1e-12):
            out.append(f'{path}: {py} vs {js}')
    elif py != js:
        out.append(f'{path}: {py!r} vs {js!r}')
    return out


def js_assemble(job_dir, step, target=None):
    args = ['node', str(JS_REFERENCE), str(JS_PIPELINE), str(job_dir), step] + ([str(target)] if target else [])
    return json.loads(subprocess.run(args, check=True, capture_output=True, text=True).stdout)


def _cases():
    if not SERVER_JOBS.exists():
        return []
    return [
        pytest.param(job.name, step, id=f'{job.name}-{step}')
        for job in sorted(SERVER_JOBS.iterdir()) if job.is_dir()
        for step, (raw_name, _) in REPLAY.items() if (job / f'{raw_name}.json').exists()
    ]


@pytest.mark.parametrize('job_id,step', _cases())
def test_assemble_matches_js(job_id, step):
    raw_name, replay = REPLAY[step]
    job_dir = SERVER_JOBS / job_id
    py = replay(json.loads((job_dir / f'{raw_name}.json').read_text()))
    assert differences(py, js_assemble(job_dir, step)) == []


@pytest.mark.parametrize('target', [5, 9, 12])
def test_consolidation_matches_js(tmp_path, target):
    raw = next((p for p in [*SERVER_JOBS.glob('*/step3-consolidate-raw.json'), STRAY_CONSOLIDATE_RAW] if p.exists()), None)
    if raw is None:
        pytest.skip('no cached consolidation scores')
    shutil.copy(raw, tmp_path / 'step3-consolidate-raw.json')
    cached = json.loads(raw.read_text())
    py = assemble_consolidate(cached['pages'], cached['fileName'], {'targetMaxNodes': target, 'minRelatedness': 0.3})
    assert differences(py, js_assemble(tmp_path, 'consolidate', target)) == []
