"""Keep-alive for the NDJSON streams (pipeline run, chat, 3D view).

A pipeline step can spend minutes in one model call without an event to
send, and proxies close a response that sits idle: an AWS load balancer and
nginx both default to 60 seconds, which ends the run with a network error in
the browser. While a stream is quiet, `with_heartbeat` sends an empty line
every HEARTBEAT_SECONDS; NDJSON readers skip empty lines (the viewer's do).
"""
import asyncio
import os
from contextlib import suppress

HEARTBEAT = '\n'

# Headers for every long response. A proxy that buffers or compresses holds the
# tiny keep-alives back (nginx gzip collects them until the response ends), so
# the load balancer behind it still sees silence and answers 504.
# X-Accel-Buffering: no makes nginx pass each chunk straight on, which also
# makes its gzip flush per chunk; no-transform asks other proxies not to
# recompress. no-cache: these are live answers.
STREAM_HEADERS = {'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache, no-transform'}


def heartbeat_seconds():
    # Well under the 60 s idle limit common to load balancers and proxies.
    return float(os.environ.get('AGENT_HEARTBEAT_SECONDS') or 15)


class _Failed:
    def __init__(self, error):
        self.error = error


async def with_heartbeat(events, interval=None):
    """Yields what `events` yields, plus HEARTBEAT whenever it is quiet for `interval` seconds.

    `events` runs to completion in ONE background task: LangGraph keeps run
    state in context variables, which wouldn't follow the generator from task
    to task. If the client goes away, the task is cancelled, which closes
    `events` (and releases any lock it holds).
    """
    interval = interval or heartbeat_seconds()
    queue = asyncio.Queue()
    finished = object()

    async def pump():
        try:
            async for item in events:
                await queue.put(item)
        except Exception as error:  # noqa: BLE001 — re-raised in the consumer
            await queue.put(_Failed(error))
        finally:
            await queue.put(finished)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), interval)
            except TimeoutError:
                yield HEARTBEAT
                continue
            if item is finished:
                return
            if isinstance(item, _Failed):
                raise item.error
            yield item
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def json_when_done(task, interval=None):
    """A JSON response for a slow task, kept alive with spaces until it's done.

    For plain request/response endpoints whose work takes minutes (drafting a
    worksheet): the response starts at once, a space goes out every `interval`
    seconds (JSON allows whitespace before a value, so any client still reads
    it as JSON), then the task's result. The status is 200 by then, so a
    failure comes as {"error": {"status": ..., "detail": ...}}.

    Waiting doesn't cancel `task`: if the client goes away, the work finishes.
    """
    import json

    from fastapi import HTTPException

    interval = interval or heartbeat_seconds()
    while not task.done():
        done, _ = await asyncio.wait({task}, timeout=interval)
        if not done:
            yield ' '
    try:
        yield json.dumps(task.result())
    except HTTPException as err:
        yield json.dumps({'error': {'status': err.status_code, 'detail': err.detail}})
    except Exception as err:  # noqa: BLE001 — reported in the body: the status line has gone
        yield json.dumps({'error': {'status': 502, 'detail': f'{type(err).__name__}: {err}'}})
