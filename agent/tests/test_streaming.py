"""Keep-alive lines on quiet NDJSON streams (streaming.py)."""
import asyncio

import pytest

from hazop_agent.streaming import HEARTBEAT, with_heartbeat


async def collect(stream):
    return [item async for item in stream]


async def test_a_quiet_stream_gets_empty_lines_between_events():
    async def slow():
        yield 'a\n'
        await asyncio.sleep(0.35)  # a long model call: nothing to send
        yield 'b\n'

    out = await collect(with_heartbeat(slow(), interval=0.1))

    assert out[0] == 'a\n' and out[-1] == 'b\n'
    assert out[1:-1] and set(out[1:-1]) == {HEARTBEAT}


async def test_a_busy_stream_gets_none():
    async def fast():
        for i in range(3):
            yield f'{i}\n'

    assert await collect(with_heartbeat(fast(), interval=0.1)) == ['0\n', '1\n', '2\n']


async def test_errors_reach_the_consumer():
    async def broken():
        yield 'a\n'
        raise RuntimeError('boom')

    with pytest.raises(RuntimeError, match='boom'):
        await collect(with_heartbeat(broken(), interval=0.1))


async def test_closing_early_stops_the_source_and_releases_its_lock():
    lock = asyncio.Lock()
    released = asyncio.Event()

    async def holds_lock():
        async with lock:
            try:
                while True:
                    await asyncio.sleep(0.01)
                    yield 'x\n'
            finally:
                released.set()

    stream = with_heartbeat(holds_lock(), interval=0.1)
    assert await stream.__anext__() == 'x\n'
    await stream.aclose()  # the browser went away
    await asyncio.wait_for(released.wait(), 1)
    assert not lock.locked()


def test_the_run_endpoint_keeps_a_quiet_step_alive(jobs_dir, monkeypatch):
    import json

    from fastapi.testclient import TestClient

    from hazop_agent import server

    from .conftest import FIXTURE_PDF

    class QuietGraph:
        async def astream(self, payload, config, stream_mode):
            yield 'updates', {'step1': {'step_status': {'step1': 'computed'}}}
            await asyncio.sleep(0.5)  # step 3's model call, with nothing to report
            yield 'values', {'job_id': 'j', 'file_name': 'pid-sample.pdf', 'step3': {'pages': []}, 'issues': []}

    monkeypatch.setattr(server, 'graph', QuietGraph())
    monkeypatch.setenv('AGENT_HEARTBEAT_SECONDS', '0.1')

    body = TestClient(server.app).post('/agent/run', params={'fileName': 'pid-sample.pdf'}, content=FIXTURE_PDF.read_bytes()).text

    lines = body.split('\n')
    assert '' in lines[1:-2]  # keep-alive lines while the step was quiet
    events = [json.loads(line) for line in lines if line]
    assert events[0]['type'] == 'run' and events[-1]['type'] == 'result'


def test_long_responses_tell_proxies_not_to_buffer_or_compress(jobs_dir, monkeypatch):
    from fastapi.testclient import TestClient

    from hazop_agent.server import app

    from .test_step4_worksheet import JOB, fake_model, make_job

    make_job()
    fake_model(monkeypatch)
    client = TestClient(app)
    responses = [
        client.post(f'/agent/hazop/studies/{JOB}/nodes/p0-node-1/generate'),
        client.post('/agent/chat', json={'threadId': JOB, 'message': 'hi'}),
    ]
    for response in responses:
        assert response.headers['x-accel-buffering'] == 'no'
        assert 'no-transform' in response.headers['cache-control']
