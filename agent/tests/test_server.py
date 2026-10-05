"""The viewer's HTTP endpoint: upload a PDF, stream progress, get the nodes document."""
import json

from fastapi.testclient import TestClient

from hazop_agent.pipeline import jobs
from hazop_agent.server import app

from .conftest import FIXTURE_PDF
from .test_graph import fake_model  # noqa: F401 — fixture

client = TestClient(app)


def events(response):
    return [json.loads(line) for line in response.text.splitlines() if line]


def test_upload_runs_the_whole_graph_and_returns_the_consolidated_document(jobs_dir, fake_model):  # noqa: F811
    response = client.post('/agent/run', params={'fileName': 'pid-sample.pdf', 'maxNodes': 1}, content=FIXTURE_PDF.read_bytes())

    assert response.status_code == 200
    stream = events(response)
    assert any(e['type'] == 'progress' for e in stream)
    assert [e['step'] for e in stream if e['type'] == 'step'] == ['step1', 'step2', 'sheet', 'step3', 'consolidate']
    result = stream[-1]
    assert result['type'] == 'result'
    assert result['jobId'] == 'pid-sample-8c299cdeb0b7'
    assert result['consolidated'] is True
    # Exactly what the viewer's file loader would read from disk.
    assert result['document'] == jobs.read_artifact(result['jobId'], 'step3-consolidated')


def test_without_max_nodes_the_step3_document_is_returned(jobs_dir, fake_model):  # noqa: F811
    result = events(client.post('/agent/run', params={'fileName': 'pid-sample.pdf'}, content=FIXTURE_PDF.read_bytes()))[-1]

    assert result['consolidated'] is False
    assert result['document'] == jobs.read_artifact(result['jobId'], 'step3')


def test_a_failed_run_ends_with_an_error_event(jobs_dir, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)

    last = events(client.post('/agent/run', params={'fileName': 'pid-sample.pdf'}, content=FIXTURE_PDF.read_bytes()))[-1]

    assert last['type'] == 'error'
    assert 'OPENAI_API_KEY is not set' in last['message']


def test_a_body_that_is_not_a_pdf_is_rejected(jobs_dir):
    assert client.post('/agent/run', content=b'hello').status_code == 400
