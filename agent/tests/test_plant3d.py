"""The 3D plant view endpoint, with Claude's stream faked."""
import asyncio
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

    assert stream[0] == {'type': 'start', 'jobId': JOB_ID, 'cached': False, 'joined': False}
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


class GatedStream(FakeStream):
    """A Claude stream that holds after its first event until `gate` opens: a generation in progress."""

    def __init__(self, text, gate):
        super().__init__(text)
        self.gate = gate

    async def __aiter__(self):
        yield NS(type='content_block_delta', delta=NS(type='thinking_delta', thinking='Planning.'))
        await self.gate.wait()
        yield NS(type='content_block_delta', delta=NS(type='text_delta', text=self.text))


@pytest.fixture
def gated_claude(monkeypatch):
    gate = asyncio.Event()
    calls = []

    def stream(**kwargs):
        calls.append(kwargs)
        return GatedStream(PAGE, gate)
    monkeypatch.setattr(plant3d, '_get_client', lambda: NS(beta=NS(messages=NS(stream=stream))))
    return gate, calls


async def _post(http, file_name, data):
    response = await http.post('/agent/plant3d', params={'fileName': file_name}, content=data)
    return [json.loads(line) for line in response.text.splitlines() if line]


async def test_a_second_request_for_the_same_drawing_joins_the_first(jobs_dir, gated_claude):
    import httpx
    gate, calls = gated_claude
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as http:
        first = asyncio.create_task(_post(http, 'pid-sample.pdf', FIXTURE_PDF.read_bytes()))
        while not plant3d._generations:
            await asyncio.sleep(0.01)
        second = asyncio.create_task(_post(http, 'pid-sample.pdf', FIXTURE_PDF.read_bytes()))
        await asyncio.sleep(0.05)
        gate.set()
        a, b = await first, await second

    assert len(calls) == 1  # one paid call for both
    assert a[0]['joined'] is False and b[0]['joined'] is True
    # The joiner replays what it missed, then gets the same result.
    assert {'type': 'thinking', 'text': 'Planning.'} in b
    assert a[-1]['type'] == b[-1]['type'] == 'result'
    assert not plant3d._generations


async def test_different_drawings_are_drawn_at_the_same_time(jobs_dir, gated_claude):
    import httpx
    gate, calls = gated_claude
    other = FIXTURE_PDF.read_bytes() + b'%another drawing'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as http:
        first = asyncio.create_task(_post(http, 'pid-sample.pdf', FIXTURE_PDF.read_bytes()))
        second = asyncio.create_task(_post(http, 'other.pdf', other))
        while len(calls) < 2:
            await asyncio.sleep(0.01)
        gate.set()
        a, b = await first, await second

    assert a[-1]['type'] == b[-1]['type'] == 'result'


async def test_a_request_that_goes_away_does_not_stop_the_generation(jobs_dir, gated_claude):
    gate, calls = gated_claude
    stream = plant3d._stream(JOB_ID, 'pid-sample.pdf', FIXTURE_PDF.read_bytes(), False)
    assert json.loads(await stream.__anext__())['type'] == 'start'
    generation = plant3d._generations[JOB_ID]
    await stream.aclose()  # the browser went away

    gate.set()
    await generation.task
    assert plant3d.page_path(JOB_ID).read_text() == PAGE
    assert not plant3d._generations
