"""Step 4: drafting one node's worksheet (the model call is replaced)."""
from fastapi.testclient import TestClient

from hazop_agent.pipeline import jobs
from hazop_agent.pipeline.step4 import generate as step4
from hazop_agent.server import app

from .conftest import FIXTURE_PDF

client = TestClient(app)
JOB = 'pid-sample-8c299cdeb0b7'
API = '/agent/hazop'


def _entity(eid, category, tag):
    return {'id': eid, 'category': category, 'tag': tag, 'type': category, 'description': '', 'attributes': [],
            'bbox': {'x': 0.2, 'y': 0.2, 'width': 0.05, 'height': 0.05}}


def make_job():
    jobs.stash_source(JOB, FIXTURE_PDF.read_bytes())
    jobs.write_artifact(JOB, 'step1', {'pages': [{'pageIndex': 0, 'entities': [
        _entity('e1', 'equipment', 'TK-01'), _entity('v1', 'valve', 'XV-001'), _entity('l1', 'line', '6"-HSD-054-A21A'),
        _entity('i1', 'instrument', 'LAHH-001')]}]})
    jobs.write_artifact(JOB, 'step1b-sheet', {'pages': [{'pageIndex': 0, 'revisedScope': {'entities': []},
        'interlockTable': {'title': 'C&E', 'modes': [], 'interlocks': [
            {'ref': 'I-02', 'mode': '', 'kind': 'trip', 'revised': False, 'unverified': False, 'text': 'LAHH-001 closes XV-001',
             'finalElementTags': ['XV-001']}]}}]})
    hazop = {'name': 'TK-01 receipt', 'designIntent': 'receive HSD', 'interlockRefs': ['I-02'],
             'keyDeviations': [{'guideword': 'NO', 'parameter': 'flow', 'detail': 'flow into TK-01', 'causes': []},
                               {'guideword': 'MORE', 'parameter': 'level', 'detail': 'level in TK-01', 'causes': []}]}
    jobs.write_artifact(JOB, 'step3', {'pages': [{'pageIndex': 0, 'nodes': [
        {'id': 'p0-node-1', 'label': 'N1 TK-01 receipt', 'entityIds': ['e1', 'v1', 'l1', 'i1'],
         'bbox': {'x': 0.1, 'y': 0.1, 'width': 0.3, 'height': 0.3}, 'routes': [], 'hazop': hazop}]}]})


DRAFT = {'notes': '', 'deviations': [
    {'parameter': 'flow', 'guideword': 'NO', 'deviation': 'No receipt flow into TK-01', 'causes': [
        {'cause': 'XV-001 fails closed', 'tags': ['XV-001', '6"-HSD-054-A21A'], 'consequences': [
            {'consequence': 'Receipt backs up in the header; supplier trips', 'safeguards': [], 'recommendation': 'Add flow indication'}]},
        {'cause': 'Line 6"-HSD-054-A21A isolated', 'tags': ['6"-HSD-054-A21A'], 'consequences': []}]},
    {'parameter': 'level', 'guideword': 'MORE', 'deviation': 'High level in TK-01', 'causes': [
        {'cause': 'Receipt continues after the tank is full', 'tags': [], 'consequences': [
            {'consequence': 'TK-01 overfills; HSD spill to the bund', 'safeguards': ['LAHH-001 / C&E I-02 — closes XV-001'],
             'recommendation': ''}]},
        {'cause': 'Supply from XV-999 stays open', 'tags': ['XV-999'], 'consequences': []}]},
]}


def fake_model(monkeypatch, seen=None):
    async def call(**kwargs):
        if seen is not None:
            seen.append(kwargs)
        return DRAFT
    monkeypatch.setattr(step4, 'structured_call', call)


def test_each_cause_is_its_own_row_with_its_tags_consequences_safeguards_and_recommendation(jobs_dir, monkeypatch):
    make_job()
    seen = []
    fake_model(monkeypatch, seen)
    r = client.post(f'{API}/studies/{JOB}/nodes/p0-node-1/generate')
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['added'] == 2 and body['skipped'] == 0
    no_flow = next(d for d in body['deviations'] if d['guideword'] == 'NO')
    assert [c['text'] for c in no_flow['causes']] == ['XV-001 fails closed (6"-HSD-054-A21A)', 'Line 6"-HSD-054-A21A isolated']
    q = no_flow['causes'][0]['consequences'][0]
    assert q['text'].startswith('Receipt backs up') and q['recommendations'][0]['text'] == 'Add flow indication'
    high = next(d for d in body['deviations'] if d['guideword'] == 'MORE')
    assert high['causes'][0]['consequences'][0]['safeguards'][0]['text'].startswith('LAHH-001')
    # the model saw the node, its required deviations, its C&E rows and an image of it
    content = seen[0]['content']
    text = '\n'.join(p.get('text', '') for p in content)
    assert 'N1 TK-01 receipt' in text and 'flow / NO' in text and 'I-02' in text
    assert any(p.get('type') == 'input_image' for p in content)
    # the draft is kept with the job, warnings included
    kept = jobs.read_artifact(JOB, 'step4-worksheet-p0-node-1')
    assert any('XV-999' in w for w in kept['warnings']) and any('name no valve' in w for w in kept['warnings'])


def test_generating_again_skips_what_is_there_and_replace_starts_over(jobs_dir, monkeypatch):
    make_job()
    fake_model(monkeypatch)
    client.post(f'{API}/studies/{JOB}/nodes/p0-node-1/generate')
    again = client.post(f'{API}/studies/{JOB}/nodes/p0-node-1/generate').json()
    assert (again['added'], again['skipped']) == (0, 2) and len(again['deviations']) == 2
    replaced = client.post(f'{API}/studies/{JOB}/nodes/p0-node-1/generate', json={'replace': True}).json()
    assert (replaced['added'], replaced['skipped']) == (2, 0) and len(replaced['deviations']) == 2


def test_check_reports_missing_required_deviations(jobs_dir):
    make_job()
    node, page = step4.find_node(JOB, 'p0-node-1')
    only_flow = {'notes': '', 'deviations': DRAFT['deviations'][:1]}
    assert any('MORE level' in w for w in step4.check(only_flow, node, JOB, page))


def test_an_unknown_node_or_job_is_404(jobs_dir, monkeypatch):
    fake_model(monkeypatch)
    assert client.post(f'{API}/studies/{JOB}/nodes/p0-node-1/generate').status_code == 404
    make_job()
    assert client.post(f'{API}/studies/{JOB}/nodes/p0-node-9/generate').status_code == 404
