"""Reopening analysed drawings without uploading them again (library.py)."""
from fastapi.testclient import TestClient

from hazop_agent.pipeline import edits, jobs
from hazop_agent.server import app

from .conftest import FIXTURE_PDF

client = TestClient(app)


def node(nid, label):
    return {'id': nid, 'label': label, 'description': '', 'categories': [], 'entityIds': [nid + '-e'],
            'bbox': {'x': 0, 'y': 0, 'width': 0.1, 'height': 0.1}, 'routes': []}


def make_job(job_id, file_name, nodes, when):
    jobs.stash_source(job_id, FIXTURE_PDF.read_bytes())
    jobs.ensure_job(job_id, fileName=file_name)
    manifest = jobs.read_manifest(job_id)
    (jobs.results_dir(job_id) / 'job.json').write_text(__import__('json').dumps({**manifest, 'updatedAt': when}))
    if nodes is not None:
        jobs.write_artifact(job_id, 'step3', {'pages': [{'pageIndex': 0, 'nodes': nodes,
                                                         'validation': {'counts': {'error': 0}, 'findings': []}}]})


def test_lists_drawings_with_nodes_newest_first(jobs_dir):
    make_job('old-aaaaaaaaaaaa', 'Old.pdf', [node('p0-node-1', 'N1 Tank')], '2026-10-01T00:00:00Z')
    make_job('new-bbbbbbbbbbbb', 'New.pdf', [node('p0-node-1', 'N1 A'), node('p0-node-2', 'N2 B')], '2026-10-20T00:00:00Z')
    make_job('failed-cccccccccccc', 'Failed.pdf', [], '2026-10-25T00:00:00Z')        # no nodes
    make_job('pending-dddddddddddd', 'Pending.pdf', None, '2026-10-26T00:00:00Z')    # never finished
    client.post('/agent/hazop/studies/new-bbbbbbbbbbbb/deviations',
                json={'nodeId': 'p0-node-2', 'nodeLabel': 'N2 B', 'parameter': 'flow', 'guideword': 'NO', 'deviation': 'No flow'})

    listed = client.get('/agent/jobs').json()

    assert [d['fileName'] for d in listed] == ['New.pdf', 'Old.pdf']
    assert listed[0] | {'updatedAt': None} == {
        'jobId': 'new-bbbbbbbbbbbb', 'fileName': 'New.pdf', 'updatedAt': None, 'pages': 1, 'nodes': 2,
        'consolidated': False, 'deviations': 1, 'nodesWithWorksheet': 1, 'has3dView': False}


def test_reopening_returns_the_pdf_and_the_nodes_as_last_edited(jobs_dir):
    make_job('plant-aaaaaaaaaaaa', 'Plant.pdf', [node('p0-node-1', 'N1 Tank'), node('p0-node-2', 'N2 Pump')], '2026-10-20T00:00:00Z')
    edits.rename_node('plant-aaaaaaaaaaaa', 'p0-node-1', 'Storage tank')

    pdf = client.get('/agent/jobs/plant-aaaaaaaaaaaa/source.pdf')
    result = client.get('/agent/jobs/plant-aaaaaaaaaaaa/nodes').json()

    assert pdf.status_code == 200 and pdf.content == FIXTURE_PDF.read_bytes()
    assert result['type'] == 'result' and result['fileName'] == 'Plant.pdf' and result['consolidated'] is False
    labels = [n['label'] for n in result['document']['pages'][0]['nodes']]
    assert 'Storage tank' in labels[0]  # the reviewer's rename, as the viewer showed it
    assert result['validation'] == [{'pageIndex': 0, 'counts': {'error': 0}, 'findings': []}]


def test_unknown_or_unfinished_drawings_are_404(jobs_dir):
    make_job('pending-dddddddddddd', 'Pending.pdf', None, '2026-10-26T00:00:00Z')
    assert client.get('/agent/jobs/nope-000000000000/nodes').status_code == 404
    assert client.get('/agent/jobs/pending-dddddddddddd/nodes').status_code == 404
    assert client.get('/agent/jobs/..%2Fsecret/source.pdf').status_code == 404


def test_the_list_needs_a_key_when_keys_are_set(jobs_dir, monkeypatch):
    monkeypatch.setenv('AGENT_API_KEYS', 'k1')
    assert client.get('/agent/jobs').status_code == 401
    assert client.get('/agent/jobs', headers={'X-API-Key': 'k1'}).status_code == 200
