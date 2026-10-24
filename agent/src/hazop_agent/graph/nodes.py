"""One graph node per pipeline step.

Every step node follows the same decision, mirroring how the JS CLI treats
its on-disk caches, made explicit:

  1. stale (forced, or an earlier step changed this run) -> compute (paid)
  2. replay requested, or the artifact is missing but its raw cache exists
     -> replay the free half over the cached model responses
  3. artifact exists -> reuse it
  4. otherwise -> compute

Each node returns its output document in state, and the next node reads its
input from there rather than from disk.

Progress events from the step modules go to LangGraph's custom stream
(`stream_mode="custom"`), so a caller sees tile-by-tile progress live.
"""
import time
from pathlib import Path

from langgraph.config import get_stream_writer

from ..pipeline import documents as study_docs
from ..pipeline import jobs, llm, network
from ..pipeline.pdf import load_pdf
from ..pipeline.step1 import extract as step1
from ..pipeline.sheet import extract as sheet
from ..pipeline.step2 import classify as step2
from ..pipeline.step3 import build as step3
from ..pipeline.step3 import consolidate
from .state import STEPS, HazopState

_VOLATILE_KEYS = {'generatedAt', 'durationMs'}
# Steps whose prompts include the study package's documents.
DOCUMENT_STEPS = ('step2', 'sheet', 'step3')


def _stable(doc):
    return {k: v for k, v in doc.items() if k not in _VOLATILE_KEYS}


def _progress(step):
    try:
        writer = get_stream_writer()
    except Exception:  # noqa: BLE001 — called outside a graph run (e.g. a unit test)
        return lambda event: None
    return lambda event: writer({'step': step, **event})


def _forced(state: HazopState, step):
    force_from = state.get('force_from')
    return bool(force_from) and STEPS.index(step) >= STEPS.index(force_from)


def _opts(state: HazopState, step):
    return (state.get('options') or {}).get(step) or {}


async def _run_step(state: HazopState, *, step, artifact, compute, replay, summarize, check,
                    output_key=None, raw_artifact=None, can_replay=None, cache_valid=None):
    """Shared cache/replay/compute logic. Returns the state update for `step`,
    including its output document under `output_key` (default: the step name)
    for the next node to consume.

    `can_replay` narrows when the raw cache may be replayed; `cache_valid`
    narrows when an existing artifact may be reused as-is.
    """
    job_id = state['job_id']
    raw_artifact = raw_artifact or f'{artifact}-raw'
    stale = _forced(state, step) or state.get('upstream_changed', False) or (
        step in DOCUMENT_STEPS and state.get('documents_changed', False))
    raw_ok = jobs.has_artifact(job_id, raw_artifact) and (can_replay is None or can_replay())
    have = jobs.has_artifact(job_id, artifact)
    previous = jobs.read_artifact(job_id, artifact) if have else None

    # A result that fails the step's own check (step 1 with no entities, step 3
    # with no nodes) is never kept, and one already kept is recomputed: a failed
    # run (a bad key, a mistyped setting) would otherwise be reused forever.
    def usable(doc):
        return check(doc)[1] is None

    result = None
    if not stale and raw_ok and (state.get('replay') or not have or can_replay is not None):
        started = time.monotonic()
        replayed = replay(jobs.read_artifact(job_id, raw_artifact))
        replayed['durationMs'] = round((time.monotonic() - started) * 1000)
        if usable(replayed):
            changed = previous is None or _stable(previous) != _stable(replayed)
            if changed:
                jobs.write_artifact(job_id, artifact, replayed)
                result = replayed
            else:
                result = previous
            status = 'replayed'
    elif not stale and have and usable(previous) and (cache_valid is None or cache_valid(previous)):
        result, changed, status = previous, False, 'cached'

    if result is None:
        if not llm.has_api_key():
            return {'failure': f'{step} needs a model call but {llm.api_key_name()} is not set (agent/.env)'}
        started = time.monotonic()
        full = await compute()
        raw = full.pop('raw')
        full['durationMs'] = round((time.monotonic() - started) * 1000)
        if usable(full):
            # Same raw-cache shape the JS CLI writes, so either implementation can replay it.
            raw_doc = {'fileName': full['fileName'], 'pages': raw}
            if 'pageCount' in full:
                raw_doc = {'fileName': full['fileName'], 'pageCount': full['pageCount'], 'pages': raw}
            jobs.write_artifact(job_id, raw_artifact, raw_doc)
            jobs.write_artifact(job_id, artifact, full)
            # model: which one produced this provider's results, for pipeline/compare.py.
            jobs.ensure_job(job_id, lastStep=step if step != 'consolidate' else 'step3-consolidate', model=llm.model_name())
        result, changed, status = full, True, 'computed'

    issues, failure = check(result)
    return {
        output_key or step: result,
        'step_status': {step: status},
        'summaries': {step: summarize(result)},
        'issues': [{'step': step, **i} for i in issues],
        'upstream_changed': state.get('upstream_changed', False) or changed,
        'failure': failure,
    }


# ─────────────────────────────────────────────────────────────────── nodes

def _with_documents(job_id, update):
    """Loads the study package's documents for this drawing and notes whether
    they changed since the job last ran (their signature is kept in job.json)."""
    docs = study_docs.study_documents(job_id)
    signature = study_docs.signature(docs)
    previous = jobs.read_manifest(job_id).get('documentsSignature', study_docs.signature([]))
    jobs.ensure_job(job_id, documentsSignature=signature)
    return {**update, 'documents': docs, 'documents_changed': signature != previous,
            'summaries': {'documents': [f"{d['typeName']}: {d['fileName']}" + ('' if d['readable'] else ' (not readable yet)') for d in docs]}}


async def ingest(state: HazopState):
    """Resolves the job: hashes a new PDF into its content-addressed job
    directory, or checks an existing job has what the run needs. Also loads
    the study package's documents for the drawing."""
    if state.get('pdf_path'):
        path = Path(state['pdf_path']).expanduser()
        if not path.exists():
            return {'failure': f'no such file: {path}'}
        data = path.read_bytes()
        job_id = jobs.job_id_for(path.name, data)
        jobs.ensure_job(job_id, fileName=path.name, source=str(path.resolve()))
        jobs.stash_source(job_id, data)
        return _with_documents(job_id, {'job_id': job_id, 'file_name': path.name, 'upstream_changed': False})

    job_id = state.get('job_id')
    if not job_id:
        return {'failure': 'pass pdf_path (a PDF) or job_id (an existing job)'}
    if not jobs.job_dir(job_id).exists():
        return {'failure': f'no job {job_id} under {jobs.jobs_root()}'}
    # A provider's first run on a job starts with no manifest of its own; the name is the drawing's either way.
    file_name = jobs.read_manifest(job_id).get('fileName') or jobs.read_manifest(job_id, 'openai').get('fileName', 'document.pdf')
    return _with_documents(job_id, {'file_name': file_name, 'upstream_changed': False})


async def extract(state: HazopState):
    """Step 1: tiled VLM extraction anchored to the PDF text layer."""
    job_id = state['job_id']
    opts = _opts(state, 'step1')

    async def compute():
        doc = load_pdf(jobs.source_path(job_id))
        return await step1.extract_document(doc, {**opts, 'fileName': state['file_name'], 'onProgress': _progress('step1')})

    def check(result):
        issues = []
        for page in result['pages']:
            for failed in page['diagnostics']['failedTiles']:
                issues.append({'page': page['pageIndex'], 'problem': f"tile {failed['tile']} failed: {failed['error']}"})
            if (page.get('drawingMeta') or {}).get('error'):
                issues.append({'page': page['pageIndex'], 'problem': f"page metadata pass failed: {page['drawingMeta']['error']}"})
        failure = None if result['totals']['entities'] else 'step1 extracted no entities — nothing to classify'
        return issues, failure

    def summarize(result):
        pages = []
        for page in result['pages']:
            entities = page['entities']
            anchored = sum(1 for e in entities if e['anchored'])
            by_category = {}
            for e in entities:
                by_category[e['category']] = by_category.get(e['category'], 0) + 1
            pages.append({
                'page': page['pageIndex'], 'entities': len(entities),
                'anchoredPct': round(100 * anchored / len(entities)) if entities else 0,
                'byCategory': by_category, 'failedTiles': len(page['diagnostics']['failedTiles']),
            })
        return {'entities': result['totals']['entities'], 'pages': pages}

    return await _run_step(
        state, step='step1', artifact='step1', compute=compute, check=check, summarize=summarize,
        replay=lambda raw: step1.assemble_document(raw['pages'], raw['fileName'], raw['pageCount']),
    )


async def trace(state: HazopState):
    """The pipe network, traced from the PDF's drawn geometry. Free and
    deterministic, so it is simply recomputed whenever it is stale or missing;
    later steps recompute only if the traced network actually changed."""
    job_id = state['job_id']
    artifact = 'step1-network'
    stale = _forced(state, 'network') or state.get('upstream_changed', False)
    previous = jobs.read_artifact(job_id, artifact) if jobs.has_artifact(job_id, artifact) else None
    if previous is not None and not stale:
        result, changed, status = previous, False, 'cached'
    else:
        started = time.monotonic()
        result = network.trace_document(load_pdf(jobs.source_path(job_id)), state['step1'])
        result['durationMs'] = round((time.monotonic() - started) * 1000)
        changed = previous is None or _stable(previous) != _stable(result)
        if changed:
            jobs.write_artifact(job_id, artifact, result)
        else:
            result = previous
        status = 'computed'
    return {
        'network': result,
        'step_status': {'network': status},
        'summaries': {'network': result['totals']},
        'upstream_changed': state.get('upstream_changed', False) or changed,
    }


async def classify(state: HazopState):
    """Step 2: group entities into named process functions, by id only."""
    job_id = state['job_id']
    opts = _opts(state, 'step2')

    async def compute():
        doc = load_pdf(jobs.source_path(job_id))
        return await step2.classify_document(doc, state['step1'], {**opts, 'network': state.get('network'), 'documents': state.get('documents') or [], 'onProgress': _progress('step2')})

    def check(result):
        issues = []
        for page in result['pages']:
            d = page['diagnostics']
            if d['uncoveredEntityIds']:
                issues.append({'page': page['pageIndex'], 'problem': f"{len(d['uncoveredEntityIds'])} entities are in no process (diagnostics.uncoveredEntityIds)"})
            hallucinated = [p['id'] for p in page['processes'] if p['flags']['hallucinatedIds']]
            if hallucinated:
                issues.append({'page': page['pageIndex'], 'problem': f'{len(hallucinated)} processes referenced ids that do not exist (dropped)'})
            if d['modelError']:
                issues.append({'page': page['pageIndex'], 'problem': f"model error: {d['modelError']}"})
        failure = None if result['totals']['processes'] else 'step2 produced no processes — nothing to cluster'
        return issues, failure

    def summarize(result):
        return {k: result['totals'][k] for k in ('processes', 'coveredEntities', 'uncoveredEntities')}

    return await _run_step(
        state, step='step2', artifact='step2', compute=compute, check=check, summarize=summarize,
        replay=lambda raw: step2.assemble_document(raw['pages'], raw['fileName']),
    )


async def sheet_data(state: HazopState):
    """Sheet data: the C&E / interlock table and the red (revised) scope,
    with the table-vs-drawing consistency checks (node rules Step 1, V3)."""
    job_id = state['job_id']
    opts = _opts(state, 'sheet')

    async def compute():
        doc = load_pdf(jobs.source_path(job_id))
        return await sheet.extract_sheet_document(doc, state['step1'], {**opts, 'documents': state.get('documents') or [], 'onProgress': _progress('sheet')})

    def check(result):
        issues = []
        for page in result['pages']:
            if page['diagnostics']['modelError']:
                issues.append({'page': page['pageIndex'], 'problem': f"C&E table read failed: {page['diagnostics']['modelError']}"})
            if not page['interlockTable']['tablePresent']:
                issues.append({'page': page['pageIndex'], 'problem': 'no C&E / interlock table found on this sheet'})
        return issues, None

    def summarize(result):
        return {k: result['totals'][k] for k in ('interlocks', 'revisedEntities', 'findings')}

    return await _run_step(
        state, step='sheet', artifact='step1b-sheet', compute=compute, check=check, summarize=summarize,
        replay=lambda raw: sheet.assemble_document(raw['pages'], raw['fileName'], state['step1']),
    )


async def cluster(state: HazopState):
    """Step 3: HAZOP nodes by the node-creation rules (step3/node_rules.md):
    one whole-sheet proposal, validated by code, repaired once if it fails."""
    job_id = state['job_id']
    opts = _opts(state, 'step3')

    async def compute():
        doc = load_pdf(jobs.source_path(job_id))
        return await step3.build_document(doc, state['step2'], state['step1'], state['sheet'],
                                          {**opts, 'network': state.get('network'), 'documents': state.get('documents') or [], 'onProgress': _progress('step3')})

    def check(result):
        issues = []
        for page in result['pages']:
            for f in page['validation']['findings']:
                if f['severity'] in ('error', 'warning'):
                    issues.append({'page': page['pageIndex'], 'problem': f"[{f['severity']}] {f['check']}: {f['detail']}"})
            if page['diagnostics']['modelError']:
                issues.append({'page': page['pageIndex'], 'problem': f"model error: {page['diagnostics']['modelError']}"})
        return issues, None if result['totals']['nodes'] else 'step3 produced no nodes'

    def summarize(result):
        return {
            'nodes': result['totals']['nodes'],
            'errors': result['totals']['errors'],
            'warnings': result['totals']['warnings'],
            'pages': [{
                'page': p['pageIndex'], 'nodes': len(p['nodes']), 'repaired': p['diagnostics']['repaired'],
                **p['validation']['counts'],
            } for p in result['pages']],
        }

    def rules_raw():
        # Raw caches from the earlier pairwise step 3 have no model responses to replay.
        return all('responses' in p for p in jobs.read_artifact(job_id, 'step3-nodes-raw')['pages'])

    return await _run_step(
        state, step='step3', artifact='step3', raw_artifact='step3-nodes-raw', compute=compute, check=check, summarize=summarize,
        replay=lambda raw: step3.assemble_document(raw['pages'], raw['fileName'], state['step1'], state['sheet'], state.get('network')),
        can_replay=rules_raw,
        # A step3.json from the earlier pairwise method is not a rules-based breakdown.
        cache_valid=lambda previous: previous.get('step') == 'step3-nodes',
    )


async def consolidate_nodes(state: HazopState):
    """Step 3b: fold nodes into at most `max_nodes` per page, strongest
    relations first. Only reached when `max_nodes` is set.

    The relatedness scores don't depend on the target — only where the greedy
    merge stops does — so once scored, any retarget replays from cache for
    free, as long as every page now over target was scored before.
    """
    job_id = state['job_id']
    target = state['max_nodes']
    min_relatedness = state.get('min_relatedness', consolidate.DEFAULT_MIN_RELATEDNESS)
    opts = {**_opts(state, 'consolidate'), 'targetMaxNodes': target, 'minRelatedness': min_relatedness}

    def scores_cover_target():
        # Replayable only if the cached scores were computed for the nodes
        # step 3 has NOW, and every page over the new target was scored.
        raw = jobs.read_artifact(job_id, 'step3-consolidate-raw')
        if consolidate.nodes_signature(raw['pages']) != consolidate.nodes_signature(state['step3']['pages']):
            return False
        return all(page['pairs'] or len(page['nodes']) <= target for page in raw['pages'])

    async def compute():
        return await consolidate.consolidate_document(
            state['step3'], state['step1'], {**opts, 'onProgress': _progress('consolidate')},
        )

    def check(result):
        issues = []
        for page in result['pages']:
            d = page['diagnostics']
            if d.get('skipped'):
                continue
            if not d['reachedTarget']:
                issues.append({'page': page['pageIndex'], 'problem': f"target {target} not reached ({d['finalNodes']} nodes)"})
            for m in (m for m in d['appliedMerges'] if m['forced']):
                issues.append({'page': page['pageIndex'], 'problem': f"forced merge {m['aId']} + {m['bId']} (relatedness {m['relatedness']:.2f}) — review"})
            if d['modelError']:
                issues.append({'page': page['pageIndex'], 'problem': f"model error: {d['modelError']}"})
        return issues, None

    def summarize(result):
        return {
            'targetMaxNodes': result['targetMaxNodes'], 'nodes': result['totals']['nodes'],
            'forcedMerges': sum(p['diagnostics'].get('forcedMergeCount', 0) for p in result['pages']),
        }

    return await _run_step(
        state, step='consolidate', artifact='step3-consolidated', output_key='consolidated', raw_artifact='step3-consolidate-raw', compute=compute, check=check, summarize=summarize,
        replay=lambda raw: consolidate.assemble_document(raw['pages'], raw['fileName'], opts),
        can_replay=scores_cover_target,
        cache_valid=lambda previous: previous.get('targetMaxNodes') == target
        and previous.get('sourceNodesSignature') == consolidate.nodes_signature(state['step3']['pages']),
    )
