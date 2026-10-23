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
