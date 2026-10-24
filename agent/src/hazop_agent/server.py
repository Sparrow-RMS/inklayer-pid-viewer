"""HTTP front door for the viewer: upload a PDF, get HAZOP nodes back.

    uv run hazop-agent serve            # http://127.0.0.1:4002

POST /agent/run?fileName=drawing.pdf&maxNodes=9 with the raw PDF as the body.
Interactive API docs: /agent/docs (OpenAPI at /agent/openapi.json). Access
(API keys, allowed browser origins) is set in access.py.
The response is newline-delimited JSON, one event per line, so a run that
takes minutes shows progress as it goes:

    {"type": "run", "jobId": ..., "cached": true}    first; cached = the nodes are already on disk
    {"type": "progress", "message": "[step1] page 0: 6 tiles, ..."}
    {"type": "step", "step": "step1", "status": "computed"}
    {"type": "result", "jobId": ..., "document": <step3 or step3-consolidated>, ...}
    {"type": "error", "message": ...}

While a step is quiet, an empty line is sent every 15 seconds (streaming.py)
so proxies don't close the connection as idle; skip empty lines.

`document` has the same shape as the step3.json / step3-consolidated.json the
viewer's file loader reads. Runs go through the same content-addressed job
directory as the CLI, so uploading a drawing again reuses its cached steps.

The HAZOP worksheet routes live in worksheets.py, the study package (supporting
documents and readiness checklist) in study_package.py; both under /agent/hazop/.
The 3D plant view (Claude draws the P&ID as a Three.js page) is in plant3d.py,
under /agent/plant3d. The 3D nodes view (the pipeline's own entities and
pipes in 3D, so nodes map onto it) is in plant_scene.py, under /agent/plant-scene.

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
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from . import access
from . import chat as chat_agent
from .graph.builder import graph
from .pipeline import edits, jobs, llm
from .plant3d import router as plant3d_router
from .plant_scene import router as plant_scene_router
from .streaming import STREAM_HEADERS, with_heartbeat
from .study_package import router as study_package_router
from .worksheets import router as worksheets_router

# One run at a time: two runs of the same drawing would write the same job directory.
_run_lock = asyncio.Lock()

app = FastAPI(
    title='hazop-agent',
    description='P&ID to HAZOP nodes, worksheets, study package, chat and 3D plant views. '
                'Send an API key as `Authorization: Bearer <key>` or `X-API-Key` when the server has AGENT_API_KEYS set.',
    # Under /agent, like every route, so one proxy rule serves the docs too.
    docs_url='/agent/docs',
    openapi_url='/agent/openapi.json',
    redoc_url=None,
)
access.install(app)
app.include_router(worksheets_router)
app.include_router(study_package_router)
app.include_router(plant3d_router)
app.include_router(plant_scene_router)


@app.get('/agent/health')
async def health():
    return {'ok': True, 'hasApiKey': llm.has_api_key(), 'provider': llm.provider(), 'model': llm.model_name(),
            'busy': _run_lock.locked()}


# The raw PDF is the body; described here because FastAPI can't infer it from Request.
@app.post('/agent/run', openapi_extra={'requestBody': {'required': True, 'content': {'application/pdf': {'schema': {'type': 'string', 'format': 'binary'}}}}})
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
    return StreamingResponse(with_heartbeat(_stream(data, name, maxNodes)), media_type='application/x-ndjson', headers=STREAM_HEADERS)


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
            # Lets the viewer tell a fresh run (minutes of model calls) from a replay of
            # this drawing's saved nodes before any step reports.
            artifact = 'step3-consolidated' if max_nodes else 'step3'
            yield _line({'type': 'run', 'jobId': thread_id, 'cached': jobs.has_artifact(thread_id, artifact)})

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

    return StreamingResponse(with_heartbeat(stream()), media_type='application/x-ndjson', headers=STREAM_HEADERS)


@app.get('/agent/chat/{thread_id}')
async def chat_history(thread_id: str):
    return {'threadId': thread_id, 'messages': await chat_agent.history(thread_id)}
