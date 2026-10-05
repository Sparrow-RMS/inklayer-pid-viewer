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
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .graph.builder import graph

# One run at a time: two runs of the same drawing would write the same job directory.
_run_lock = asyncio.Lock()

app = FastAPI(title='hazop-agent')


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

            final = {}
            try:
                async for mode, chunk in graph.astream(payload, stream_mode=['custom', 'updates', 'values']):
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
    yield _line({
        'type': 'result',
        'jobId': final.get('job_id'),
        'fileName': final.get('file_name'),
        'consolidated': document is not None,
        'document': document or final.get('step3'),
        # Step 7 validation of the rules-based breakdown, per page (the reduced
        # option is built from these nodes, so its report is this one).
        'validation': [{'pageIndex': p['pageIndex'], **p['validation']} for p in (final.get('step3') or {}).get('pages', [])],
        'summaries': final.get('summaries', {}),
        'issues': final.get('issues', []),
    })
