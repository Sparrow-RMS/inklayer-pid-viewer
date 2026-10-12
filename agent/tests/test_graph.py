"""The LangGraph agent end to end: routing, caching, replay and failure gates.

Model calls are replaced by a deterministic fake keyed on the structured
output schema, so every compute path runs for real — PDF rendering, text and
vector extraction, the red-markup scan, assembly, validation, artifacts on
disk — without an API key.
"""
import re

import pymupdf
import pytest

from hazop_agent.graph.builder import build_graph
from hazop_agent.pipeline import jobs
from hazop_agent.pipeline.sheet import extract as sheet
from hazop_agent.pipeline.step1 import extract
from hazop_agent.pipeline.step2 import classify
from hazop_agent.pipeline.step3 import build, consolidate

from .conftest import FIXTURE_PDF, REPO_ROOT, copy_job

STRAY_CONSOLIDATE_RAW = REPO_ROOT / 'server' / 'step3-consolidate-raw.json'
MODEL_MODULES = (extract, classify, sheet, build, consolidate)


def _node(i, process_ids, refs):
    return {
        'localId': f'N{i}', 'name': f'node {i}', 'processIds': process_ids, 'entityIds': [],
        'boundaryFrom': [{'tag': 'HV-201', 'kind': 'valve', 'note': ''}], 'boundaryTo': [{'tag': 'FCV-202', 'kind': 'valve', 'note': ''}],
        'equipmentTags': ['T-101'], 'designIntent': f'To transfer product for node {i}.',
        'designConditions': {'pressure': 'UNVERIFIED', 'temperature': 'UNVERIFIED', 'material': 'UNVERIFIED', 'source': 'UNVERIFIED'},
        'controlLoops': ['LIC-101'], 'interlockRefs': refs, 'reliefDevices': [{'tag': 'PSV-201', 'setPressure': 'UNVERIFIED'}],
        'protection': {'criticalParameter': 'level', 'highSafeguards': ['LIC-101'], 'lowSafeguards': ['LIC-101'], 'vacuumProtection': None},
        'keyDeviations': [{'guideword': 'NO', 'parameter': 'flow', 'credibleCause': 'valve closed'}],
        'operatingModes': [], 'utilitiesAsSafeguards': ['instrument air'],
        'typicalOf': 'N1' if i == 2 else None, 'differencesFromTypical': ['different tags'] if i == 2 else [],
        'revisedScope': False, 'justification': [{'rule': 'R4', 'reason': 'isolation valve'}], 'unverified': ['design pressure'],
    }


class FakeModel:
    """Answers each pipeline call shape plausibly, and counts calls.

    `forget_interlocks_first`: the first node proposal leaves the C&E rows
    unassigned, so validation fails (V2) and the repair pass must run."""

    def __init__(self, fail=False, forget_interlocks_first=False):
        self.calls = []
        self.prompts = []  # (schema name, prompt text) per call
        self.fail = fail
        self.forget_interlocks_first = forget_interlocks_first

    async def __call__(self, *, system, content, schema, schema_name, model=None):
        self.calls.append(schema_name)
        if self.fail:
            raise RuntimeError('model unavailable')
        text = '\n'.join(part.get('text', '') for part in content)
        self.prompts.append((schema_name, text))
        if schema_name == 'pid_tile_inventory':
            # Report nothing: step 1 must still recover every tag-shaped text
            # run from the PDF text layer (synthesized stubs).
            return {'entities': [], 'tileNotes': ''}
        if schema_name == 'pid_page_metadata':
            return {'titleBlock': [{'key': 'drawingNumber', 'value': 'PID-2201-001'}], 'notes': [], 'legend': [],
                    'revisions': [], 'referencedDrawings': [], 'overallDescription': 'fake'}
        if schema_name == 'pid_page_processes':
            ids = re.findall(r'^(p\d+-[a-z]+-\d+) \[', text, flags=re.M)
            half = len(ids) // 2
            return {'pageNotes': '', 'processes': [
                {'localId': f'proc{i}', 'name': f'process {i}', 'category': 'transfer', 'description': 'd',
                 'entityIds': chunk, 'primaryEquipmentIds': chunk[:1], 'inletEntityIds': [], 'outletEntityIds': [],
                 'flowDirection': '', 'boundaryReasoning': [], 'confidence': 0.9}
                for i, chunk in enumerate([ids[:half], ids[half:], ids[:2]], 1) if chunk
            ]}
        if schema_name == 'pid_interlock_table':
            return {'tablePresent': True, 'tableTitle': 'INTERLOCKS', 'modes': ['TRANSFER MODE'], 'notes': '', 'interlocks': [
                {'ref': 'IL-1', 'mode': 'TRANSFER MODE', 'kind': 'trip', 'text': 'IF LIC-101 LOW, P-101A SHOULD TRIP.',
                 'causeTags': ['LIC-101'], 'finalElementTags': ['P-101A'], 'requiredStates': [],
                 'markedRevised': False, 'unverified': False, 'unverifiedReason': ''},
                {'ref': 'IL-2', 'mode': 'TRANSFER MODE', 'kind': 'permissive', 'text': 'P-101B MAY START IF HV-201 OPEN. HV-201 CLOSED.',
                 'causeTags': ['HV-201'], 'finalElementTags': ['P-101B'],
                 'requiredStates': [{'tag': 'HV-201', 'state': 'open'}, {'tag': 'HV-201', 'state': 'closed'}],
                 'markedRevised': False, 'unverified': False, 'unverifiedReason': ''},
            ]}
        if schema_name == 'pid_hazop_nodes':
            process_ids = list(dict.fromkeys(re.findall(r'^(p\d+-proc-\d+) \[', text, flags=re.M)))
            refs = re.findall(r'^(IL-\d+) \(', text, flags=re.M)
            first = 'Your previous breakdown' not in text
            if first and self.forget_interlocks_first:
                refs = []
            return {'notes': '', 'consistencyFindings': [
                {'check': 'copy-paste', 'interlockRefs': ['IL-2'], 'tags': ['P-101B'], 'detail': 'fake finding'}],
                'nodes': [_node(i, [pid], refs if i == 1 else []) for i, pid in enumerate(process_ids, 1)]}
        if schema_name == 'pid_node_relatedness':
            pair_ids = re.findall(r'^(k\d+):', text, flags=re.M)
            return {'scores': [{'pairId': p, 'relatedness': 0.8, 'reasoning': 'fake'} for p in pair_ids]}
        raise AssertionError(f'unexpected call {schema_name}')


def use_model(monkeypatch, fake):
    for module in MODEL_MODULES:
        monkeypatch.setattr(module, 'structured_call', fake)
    monkeypatch.setenv('OPENAI_API_KEY', 'test-not-a-real-key')
    return fake


@pytest.fixture
def fake_model(monkeypatch):
    return use_model(monkeypatch, FakeModel())


@pytest.fixture
def no_model(monkeypatch):
    """Any model call is a test failure."""
    return use_model(monkeypatch, FakeModel(fail=True))


async def run(payload):
    return await build_graph().ainvoke(payload)


ALL_COMPUTED = {'step1': 'computed', 'network': 'computed', 'step2': 'computed', 'sheet': 'computed', 'step3': 'computed', 'consolidate': 'computed'}


async def test_new_pdf_runs_every_step_and_builds_rules_based_nodes(jobs_dir, fake_model):
    state = await run({'pdf_path': str(FIXTURE_PDF), 'max_nodes': 1})

    assert state.get('failure') is None
    assert state['step_status'] == ALL_COMPUTED
    job_id = state['job_id']
    assert job_id == jobs.job_id_for('pid-sample.pdf', FIXTURE_PDF.read_bytes()) == 'pid-sample-8c299cdeb0b7'
    for name in ('step1', 'step1-raw', 'step2', 'step2-raw', 'step1b-sheet', 'step1b-sheet-raw', 'step3', 'step3-nodes-raw',
                 'step3-consolidated', 'step3-consolidate-raw'):
        assert jobs.has_artifact(job_id, name), name
    assert (jobs.job_dir(job_id) / 'source.pdf').exists()

    # The fake model reported no entities; step 1 still recovered every tag from the text layer.
    tags = {e['tag'] for e in state['step1']['pages'][0]['entities']}
    assert {'T-101', 'P-101A', 'FT-202', 'PSV-201'} <= tags

    # The C&E table and its exact consistency checks.
    table = state['sheet']['pages'][0]['interlockTable']
    assert [r['ref'] for r in table['interlocks']] == ['IL-1', 'IL-2']
    checks = {f['check'] for f in state['sheet']['pages'][0]['findings']}
    assert 'open-and-closed' in checks

    # Rules-based nodes with every Step 6 field, typical-of resolved to a node id.
    page = state['step3']['pages'][0]
    assert state['step3']['step'] == 'step3-nodes'
    first, second = page['nodes'][0], page['nodes'][1]
    assert first['label'].startswith('N1 ') and first['hazop']['interlockRefs'] == ['IL-1', 'IL-2']
    assert second['hazop']['typicalOf'] == first['id'] and second['hazop']['typicalOfName'] == 'node 1'
    assert first['routes'] is not None and first['bbox']['width'] > 0
    sources = {f['source'] for f in page['validation']['findings']}
    assert {'c&e-table', 'model'} <= sources
    assert page['diagnostics']['repaired'] is False

    # The reduced option carries the merged Step 6 fields.
    merged = state['consolidated']['pages'][0]['nodes'][0]
    assert state['summaries']['consolidate']['nodes'] == 1
    assert merged['hazop']['justification'][-1]['rule'] == 'S'
    assert set(merged['hazop']['interlockRefs']) == {'IL-1', 'IL-2'}
    for key, artifact in (('step1', 'step1'), ('step2', 'step2'), ('sheet', 'step1b-sheet'), ('step3', 'step3'), ('consolidated', 'step3-consolidated')):
        assert state[key] == jobs.read_artifact(job_id, artifact), key


async def test_validation_errors_trigger_one_repair_pass(jobs_dir, monkeypatch):
    fake = use_model(monkeypatch, FakeModel(forget_interlocks_first=True))

    state = await run({'pdf_path': str(FIXTURE_PDF)})

    assert fake.calls.count('pid_hazop_nodes') == 2
    page = state['step3']['pages'][0]
    assert page['diagnostics']['repaired'] is True and page['diagnostics']['initialErrors'] >= 1
    assert not [f for f in page['validation']['findings'] if f['check'] == 'V2-unassigned']


async def test_each_step_takes_its_input_from_the_previous_node_not_disk(jobs_dir, fake_model, monkeypatch):
    original = jobs.read_artifact
    read = []
    monkeypatch.setattr(jobs, 'read_artifact', lambda job_id, name: read.append(name) or original(job_id, name))

    state = await run({'pdf_path': str(FIXTURE_PDF), 'max_nodes': 1})

    assert state.get('failure') is None
    # A fresh job computes every step, so nothing upstream is ever read back.
    assert not {'step1', 'step2', 'step1b-sheet', 'step3'} & set(read)


async def test_second_run_reuses_every_artifact_without_model_calls(jobs_dir, fake_model):
    first = await run({'pdf_path': str(FIXTURE_PDF), 'max_nodes': 1})
    calls = len(fake_model.calls)

    second = await run({'job_id': first['job_id'], 'max_nodes': 1})

    assert len(fake_model.calls) == calls
    assert second['step_status'] == {'step1': 'cached', 'network': 'cached', 'step2': 'cached', 'sheet': 'cached', 'step3': 'replayed', 'consolidate': 'replayed'}
    assert second['upstream_changed'] is False


async def test_force_from_recomputes_that_step_and_everything_after(jobs_dir, fake_model):
    first = await run({'pdf_path': str(FIXTURE_PDF)})
    fake_model.calls.clear()

    state = await run({'job_id': first['job_id'], 'force_from': 'sheet'})

    assert state['step_status'] == {'step1': 'cached', 'network': 'cached', 'step2': 'cached', 'sheet': 'computed', 'step3': 'computed'}
    assert set(fake_model.calls) == {'pid_interlock_table', 'pid_hazop_nodes'}
    assert 'consolidate' not in state['step_status']  # no max_nodes -> routed straight to END


async def test_an_old_pairwise_job_gets_rules_based_nodes_then_replays_free(jobs_dir, monkeypatch):
    # A job the earlier pipeline built: step 1-3 and a 9-node consolidation,
    # plus cached relatedness scores for those OLD nodes.
    extra = {'step3-consolidate-raw.json': STRAY_CONSOLIDATE_RAW} if STRAY_CONSOLIDATE_RAW.exists() else None
    copy_job('pid-f9daf1158400', jobs_dir, extra=extra)
    fake = use_model(monkeypatch, FakeModel())

    state = await run({'job_id': 'pid-f9daf1158400', 'max_nodes': 9})

    # Step 1 is reused. The job predates pipe tracing, so the network is traced
    # now and step 2 onward recompute once to use it.
    assert state['step_status'] == {'step1': 'cached', 'network': 'computed', 'step2': 'computed', 'sheet': 'computed', 'step3': 'computed', 'consolidate': 'computed'}
    assert state['step3']['step'] == 'step3-nodes'
    assert 'pid_node_relatedness' in fake.calls  # old scores were for old nodes, never replayed
    assert state['summaries']['consolidate']['nodes'] == 9
    assert state['consolidated']['sourceNodesSignature'] == consolidate.nodes_signature(state['step3']['pages'])

    # The drawing's red markup is found without the model.
    revised = {r['tag'] for r in state['sheet']['pages'][0]['revisedScope']['entities']}
    assert {'STPOT-NS302', 'P-100'} <= revised
    assert 'ST-V303' not in revised  # a new line running into an old tank doesn't make the tank new

    # Replay after that is free, and retargeting reuses the new scores.
    blocked = use_model(monkeypatch, FakeModel(fail=True))
    again = await run({'job_id': 'pid-f9daf1158400', 'replay': True, 'max_nodes': 9})
    assert again['step_status'] == {'step1': 'replayed', 'network': 'cached', 'step2': 'replayed', 'sheet': 'replayed', 'step3': 'replayed', 'consolidate': 'replayed'}
    assert again['upstream_changed'] is False  # the rules reproduce every stored artifact exactly
    retarget = await run({'job_id': 'pid-f9daf1158400', 'max_nodes': 5})
    assert retarget['step_status']['consolidate'] == 'replayed'
    assert retarget['summaries']['consolidate']['nodes'] == 5
    assert blocked.calls == []


async def test_a_step_that_needs_the_model_without_a_key_fails_cleanly(jobs_dir, monkeypatch):
    copy_job('pid-sample-8c299cdeb0b7', jobs_dir)  # has steps 1-3 but no sheet data or traced network
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)

    state = await run({'job_id': 'pid-sample-8c299cdeb0b7'})

    # Tracing is free, so it runs; the network is new, so step 2 must recompute and needs the key.
    assert 'step2 needs a model call' in state['failure'] and 'OPENAI_API_KEY is not set' in state['failure']
    assert state['step_status'] == {'step1': 'cached', 'network': 'computed'}


async def test_unusable_step1_stops_the_run_before_paying_for_step2(jobs_dir, tmp_path, fake_model):
    blank = tmp_path / 'blank.pdf'
    doc = pymupdf.open()
    doc.new_page(width=842, height=595)
    doc.save(blank)

    state = await run({'pdf_path': str(blank)})

    assert 'no entities' in state['failure']
    assert set(state['step_status']) == {'step1'}
    assert 'pid_page_processes' not in fake_model.calls


async def test_failed_model_calls_are_reported_as_issues_not_crashes(jobs_dir, monkeypatch):
    use_model(monkeypatch, FakeModel(fail=True))

    state = await run({'pdf_path': str(FIXTURE_PDF)})

    # Every call failed, yet the text layer alone still yields entities, so
    # step 1 completes with its failures surfaced as issues; the run stops
    # once step 2 has nothing to build nodes from.
    problems = [i['problem'] for i in state['issues'] if i['step'] == 'step1']
    assert any('failed: model unavailable' in p for p in problems)
    assert any('metadata pass failed' in p for p in problems)
    assert state['failure'] == 'step2 produced no processes — nothing to cluster'


async def test_missing_input_fails_at_ingest(jobs_dir):
    state = await run({})
    assert 'pass pdf_path' in state['failure']
    assert 'step_status' not in state or not state['step_status']


def test_graph_shape():
    g = build_graph().get_graph()
    assert set(g.nodes) == {'__start__', 'ingest', 'extract', 'trace', 'classify', 'sheet_data', 'cluster', 'consolidate', '__end__'}
