"""HTTP front door for the viewer: upload a PDF, get HAZOP nodes back.

    uv run hazop-agent serve            # http://127.0.0.1:4002

POST /agent/run?fileName=drawing.pdf&maxNodes=9 with the raw PDF as the body.
The response is newline-delimited JSON, one event per line, so a run that
takes minutes shows progress as it goes:

    {"type": "progress", "message": "[step1] page 0: 6 tiles, ..."}
    {"type": "step", "step": "step1", "status": "computed"}
    {"type": "result", "jobId": ..., "document": <step3 or step3-consolidated>, ...}
    {"type": "error", "message": ...}

`document` has the same shape as the step3.json / step3-consolidated.json the
viewer's file loader reads. Runs go through the same content-addressed job
directory as the CLI, so uploading a drawing again reuses its cached steps.

The HAZOP worksheet routes live in worksheets.py, the study package (supporting
documents and readiness checklist) in study_package.py; both under /agent/hazop/.

Threads: a drawing's job id is its thread id. A pipeline run and the chat
assistant (chat.py) run under the same thread, so they share one
conversation, and one thread in LangSmith:

    POST /agent/chat {"threadId": <job id>, "message": "..."}   newline-delimited events:
        {"type": "tool", "name": ..., "args": ...}   the assistant is using a tool
        {"type": "nodes", "document": ...}          the node breakdown changed (merge, rename, undo)
        {"type": "message", "text": ...}            the reply
        {"type": "error", "message": ...}
    GET  /agent/chat/{threadId}                     the conversation so far

The run result's document has the reviewer's saved edits applied (pipeline/edits.py).
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from . import chat as chat_agent
from .graph.builder import graph
from .pipeline import edits, jobs
from .study_package import router as study_package_router
from .worksheets import router as worksheets_router

# One run at a time: two runs of the same drawing would write the same job directory.
_run_lock = asyncio.Lock()

app = FastAPI(title='hazop-agent')
app.include_router(worksheets_router)
app.include_router(study_package_router)


@app.get('/agent/health')
async def health():
    return {'ok': True, 'hasApiKey': bool(os.environ.get('OPENAI_API_KEY')), 'busy': _run_lock.locked()}


@app.post('/agent/run')
async def run(request: Request, fileName: str = Query('document.pdf'), maxNodes: int | None = Query(None, ge=1)):
    data = await request.body()
    if not data.startswith(b'%PDF'):
        raise HTTPException(400, 'request body must be a PDF')
    if _run_lock.locked():
        raise HTTPException(409, 'a pipeline run is already in progress')
    # The job id hashes the file name as well as the bytes, so keep the
    # viewer's name (minus any directory part) for the temp copy.
    name = Path(fileName).name or 'document.pdf'
    if not name.lower().endswith('.pdf'):
        name += '.pdf'
    return StreamingResponse(_stream(data, name, maxNodes), media_type='application/x-ndjson')


def _line(event):
    return json.dumps(event) + '\n'


async def _stream(data, name, max_nodes):
    # Imported here so the CLI module can import this one for `serve`.
    from .cli import describe_progress

    async with _run_lock:
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / name
            pdf_path.write_bytes(data)
            payload = {'pdf_path': str(pdf_path)}
            if max_nodes:
                payload['max_nodes'] = max_nodes
            # The drawing's thread: the same id the viewer and the chat use.
            thread_id = jobs.job_id_for(name, data)
            config = {**chat_agent.thread_config(thread_id), 'run_name': 'hazop-pipeline'}

            final = {}
            try:
                async for mode, chunk in graph.astream(payload, config=config, stream_mode=['custom', 'updates', 'values']):
                    if mode == 'custom':
                        message = describe_progress(chunk)
                        if message:
                            yield _line({'type': 'progress', 'message': message})
                    elif mode == 'updates':
                        for update in chunk.values():
                            for step, status in ((update or {}).get('step_status') or {}).items():
                                yield _line({'type': 'step', 'step': step, 'status': status})
                    else:
                        final = chunk
            except Exception as err:  # noqa: BLE001 — reported to the viewer instead of a dropped stream
                yield _line({'type': 'error', 'message': f'{type(err).__name__}: {err}'})
                return

    if final.get('failure'):
        yield _line({'type': 'error', 'message': final['failure'], 'issues': final.get('issues', [])})
        return
    document = final.get('consolidated') if max_nodes else None
    base = 'step3-consolidated' if document is not None else 'step3'
    job_id = final.get('job_id')
    issues = list(final.get('issues', []))
    document = document or final.get('step3')
    if job_id:
        # The reviewer's merges and renames, kept across runs.
        edits.set_view_base(job_id, base)
        document, stale = edits.apply_edits(document, edits.load_edits(job_id, base))
        issues += [{'step': 'edits', 'problem': f"your {e['type']} of {', '.join(e['labels'])} no longer fits the nodes and was not applied"}
                   for e in stale]
    yield _line({
        'type': 'result',
        'jobId': job_id,
        'threadId': job_id,
        'fileName': final.get('file_name'),
        'consolidated': base == 'step3-consolidated',
        'document': document,
        # Step 7 validation of the rules-based breakdown, per page (the reduced
        # option is built from these nodes, so its report is this one).
        'validation': [{'pageIndex': p['pageIndex'], **p['validation']} for p in (final.get('step3') or {}).get('pages', [])],
        'summaries': final.get('summaries', {}),
        'issues': issues,
    })


@app.post('/agent/chat')
async def chat(body: dict):
    thread_id = (body or {}).get('threadId')
    message = ((body or {}).get('message') or '').strip()
    if not thread_id or not message:
        raise HTTPException(422, 'threadId and message are required')

    async def stream():
        try:
            async for event in chat_agent.chat(thread_id, message):
                yield _line(event)
        except Exception as err:  # noqa: BLE001 — reported to the viewer instead of a dropped stream
            yield _line({'type': 'error', 'message': f'{type(err).__name__}: {err}'})

    return StreamingResponse(stream(), media_type='application/x-ndjson')


@app.get('/agent/chat/{thread_id}')
async def chat_history(thread_id: str):
    return {'threadId': thread_id, 'messages': await chat_agent.history(thread_id)}
