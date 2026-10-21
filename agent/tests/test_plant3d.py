"""The 3D plant view endpoint, with Claude's stream faked."""
import json
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from hazop_agent import plant3d
from hazop_agent.server import app

from .conftest import FIXTURE_PDF

client = TestClient(app)
JOB_ID = 'pid-sample-8c299cdeb0b7'
PAGE = '<!doctype html><html><body><script type="module">import * as THREE from "three"</script></body></html>'


def events(response):
    return [json.loads(line) for line in response.text.splitlines() if line]


class FakeStream:
    def __init__(self, text, stop_reason='end_turn'):
        self.text, self.stop_reason = text, stop_reason

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        yield NS(type='content_block_delta', delta=NS(type='thinking_delta', thinking='Laying out the column first.'))
        for i in range(0, len(self.text), 1000):
            yield NS(type='content_block_delta', delta=NS(type='text_delta', text=self.text[i:i + 1000]))

    async def get_final_message(self):
        return NS(stop_reason=self.stop_reason, stop_details=None, model='claude-opus-5-5',
                  content=[NS(type='text', text=self.text)], usage=NS(input_tokens=10, output_tokens=20))


@pytest.fixture
def fake_claude(monkeypatch):
    calls = []

    def install(text, stop_reason='end_turn'):
        def stream(**kwargs):
            calls.append(kwargs)
            return FakeStream(text, stop_reason)
        monkeypatch.setattr(plant3d, '_get_client', lambda: NS(beta=NS(messages=NS(stream=stream))))
        return calls

    return install


def post():
    return client.post('/agent/plant3d', params={'fileName': 'pid-sample.pdf'}, content=FIXTURE_PDF.read_bytes())


def test_generates_caches_and_serves_the_page(jobs_dir, fake_claude):
    calls = fake_claude('Here it is:\n```html\n' + PAGE + '\n```')

    stream = events(post())

    assert stream[0] == {'type': 'start', 'jobId': JOB_ID, 'cached': False}
    assert {'type': 'thinking', 'text': 'Laying out the column first.'} in stream
    assert stream[-1] == {'type': 'result', 'jobId': JOB_ID, 'url': f'/agent/plant3d/{JOB_ID}'}
    assert calls[0]['messages'][0]['content'][0]['type'] == 'document'
    # The fence and the sentence around it are dropped.
    page = client.get(f'/agent/plant3d/{JOB_ID}')
    assert page.status_code == 200 and page.text == PAGE

    # Opening the drawing again reuses the page without another call.
    assert events(post())[0]['cached'] is True
    assert len(calls) == 1


def test_force_regenerates(jobs_dir, fake_claude):
    calls = fake_claude(PAGE)
    post()
    client.post('/agent/plant3d', params={'fileName': 'pid-sample.pdf', 'force': True}, content=FIXTURE_PDF.read_bytes())
    assert len(calls) == 2


def test_an_answer_without_html_is_an_error_and_nothing_is_cached(jobs_dir, fake_claude):
    fake_claude('I could not read this drawing.')

    assert events(post())[-1]['type'] == 'error'
    assert client.get(f'/agent/plant3d/{JOB_ID}').status_code == 404


def test_a_truncated_page_is_not_cached(jobs_dir, fake_claude):
    fake_claude(PAGE, stop_reason='max_tokens')

    assert 'cut off' in events(post())[-1]['message']
    assert client.get(f'/agent/plant3d/{JOB_ID}').status_code == 404


def test_rejects_non_pdfs_and_bad_ids(jobs_dir):
    assert client.post('/agent/plant3d', content=b'hello').status_code == 400
    assert client.get('/agent/plant3d/..%2Fjob').status_code == 404
