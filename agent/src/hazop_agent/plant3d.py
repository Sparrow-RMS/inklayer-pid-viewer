"""P&ID -> 3D plant view, written by Claude as a self-contained Three.js page.

    POST /agent/plant3d?fileName=drawing.pdf[&force=true]   raw PDF body; newline-delimited events:
        {"type": "start", "jobId": ..., "cached": true}   first; cached = the page is already on disk
        {"type": "thinking", "text": ...}                   a piece of Claude's reasoning summary
        {"type": "progress", "chars": 12000}                how much of the page has been written
        {"type": "result", "jobId": ..., "url": "/agent/plant3d/<jobId>"}
        {"type": "error", "message": ...}
    GET  /agent/plant3d/{job_id}                           the page itself (404 until generated)

The PDF goes to Claude as a document block, and the answer is one HTML file
that loads Three.js from a CDN and draws the plant: equipment as primitives at
plausible positions, the drawn lines as piping between them, tags as labels.
It is cached as <job dir>/plant3d.html, keyed like every other artifact by the
drawing's content, so reopening the drawing shows it without another call.

The viewer shows the page in an iframe sandboxed to scripts only: it is model
output, so it never runs with the app's origin.

Uses the Anthropic API (ANTHROPIC_API_KEY or an `ant auth login` profile);
the HAZOP pipeline itself stays on OpenAI (pipeline/llm.py).
"""
import asyncio
import base64
import json
import os
import re
from pathlib import Path

from anthropic import AsyncAnthropic
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from .pipeline import jobs

router = APIRouter()

# One generation at a time; independent of the pipeline's lock, so a 3D view
# can be drawn while the HAZOP pipeline runs.
_lock = asyncio.Lock()
_client = None

PAGE_NAME = 'plant3d.html'
# The API's request limit is 32 MB, and base64 adds a third.
MAX_PDF_BYTES = 22 * 1024 * 1024
THREE_VERSION = '0.170.0'
# Thinking and the page share this budget; 128K is the model's maximum.
MAX_TOKENS = 128000

SYSTEM = f"""You turn piping and instrumentation diagrams (P&IDs) into an interactive 3D model of the plant they describe.

Read the whole drawing: equipment (vessels, columns, tanks, pumps, compressors, exchangers, filters, heaters), their tags and \
descriptions, the process and utility lines between them, valves, and the main instruments. Then lay the plant out in 3D the \
way it would plausibly stand on site: equipment on a ground plane at realistic relative sizes and elevations (columns tall, \
pumps small and at grade, vessels on legs or skirts), spaced so the piping is readable, and the flow reading roughly in the \
same direction as on the drawing.

Output exactly one complete HTML document and nothing else: start with <!doctype html>, end with </html>, no Markdown fence, \
no commentary. The page must:
- use Three.js {THREE_VERSION} only, through this import map, and no other network resources (no fonts, images or other scripts):
  <script type="importmap">{{"imports": {{"three": "https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}/build/three.module.js", "three/addons/": "https://cdn.jsdelivr.net/npm/three@{THREE_VERSION}/examples/jsm/"}}}}</script>
  then one <script type="module">;
- build every piece of equipment from Three.js primitives shaped like the real thing, coloured by type;
- draw lines as tube geometry routed orthogonally along pipe racks or at grade, coloured by service (process, utility, \
  instrument air, drain, ...), with a small arrow or cone showing flow direction; mark valves on the lines;
- label each equipment item with its tag (CSS2DRenderer is fine) and keep labels legible at any zoom;
- use OrbitControls, frame the whole plant on load, fill the window and handle resize;
- show a compact legend of the colours, and a panel that lists a picked item's tag, type and connections when it is clicked;
- reset the camera with a button.

Keep the code compact so the whole page fits in one answer: describe the plant as data (an array of equipment with tag, \
type, position and size; an array of lines with from, to, service and waypoints; an array of valves) and draw it with a \
handful of builder functions, one per equipment type, rather than writing out each item's geometry by hand. No comments.

Only draw what the drawing shows; where it does not say (sizes, elevations, routing), choose something sensible. When a \
connection goes off the sheet, end the line at a labelled off-sheet marker."""

PROMPT = 'Build the 3D plant model for this P&ID.'


def _get_client():
    global _client
    if _client is None:
        # A long page at high effort can take several minutes; streaming keeps
        # the connection alive, this just bounds a stalled one.
        _client = AsyncAnthropic(max_retries=3, timeout=float(os.environ.get('PLANT3D_TIMEOUT') or 1200))
    return _client


def model_name():
    return os.environ.get('PLANT3D_MODEL') or 'claude-opus-5-5'


def page_path(job_id):
    return jobs.job_dir(job_id) / PAGE_NAME


def extract_html(text):
    """The HTML document in Claude's answer, even if it was wrapped in a fence or a sentence."""
    start = re.search(r'<!doctype html|<html', text, re.IGNORECASE)
    end = text.lower().rfind('</html>')
    if not start or end < start.start():
        return None
    return text[start.start():end + len('</html>')]


def _line(event):
    return json.dumps(event) + '\n'


@router.get('/agent/plant3d/{job_id}')
async def get_page(job_id: str):
    # Job ids are <slug>-<12 hex>; anything else could walk out of the jobs root.
    if not re.fullmatch(r'[a-z0-9-]+', job_id):
        raise HTTPException(404, 'no such 3D view')
    path = page_path(job_id)
    if not path.exists():
        raise HTTPException(404, 'no 3D view has been generated for this drawing yet')
    return FileResponse(path, media_type='text/html', headers={'Cache-Control': 'no-store'})


@router.post('/agent/plant3d', openapi_extra={'requestBody': {'required': True, 'content': {'application/pdf': {'schema': {'type': 'string', 'format': 'binary'}}}}})
async def generate(request: Request, fileName: str = Query('document.pdf'), force: bool = Query(False)):
    data = await request.body()
    if not data.startswith(b'%PDF'):
        raise HTTPException(400, 'request body must be a PDF')
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(413, f'the PDF is {len(data) // (1024 * 1024)} MB; the 3D view takes up to {MAX_PDF_BYTES // (1024 * 1024)} MB')
    if _lock.locked():
        raise HTTPException(409, 'a 3D view is already being generated')
    name = Path(fileName).name or 'document.pdf'
    if not name.lower().endswith('.pdf'):
        name += '.pdf'
    # Same id as the pipeline and the viewer's studyIdFor.
    job_id = jobs.job_id_for(name, data)
    return StreamingResponse(_stream(job_id, name, data, force), media_type='application/x-ndjson')


async def _stream(job_id, name, data, force):
    url = f'/agent/plant3d/{job_id}'
    async with _lock:
        cached = page_path(job_id).exists() and not force
        yield _line({'type': 'start', 'jobId': job_id, 'cached': cached})
        if cached:
            yield _line({'type': 'result', 'jobId': job_id, 'url': url})
            return

        jobs.ensure_job(job_id, fileName=name)
        jobs.stash_source(job_id, data)
        try:
            async with _get_client().beta.messages.stream(
                model=model_name(),
                max_tokens=MAX_TOKENS,
                system=SYSTEM,
                messages=[{
                    'role': 'user',
                    'content': [
                        {'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf',
                                                        'data': base64.standard_b64encode(data).decode()}},
                        {'type': 'text', 'text': PROMPT},
                    ],
                }],
                thinking={'type': 'adaptive', 'display': 'summarized'},
                output_config={'effort': 'high'},
                # A declined request is re-run on Anthropic's recommended fallback model.
                betas=['server-side-fallback-2026-07-01'],
                fallbacks='default',
            ) as stream:
                written = reported = 0
                async for event in stream:
                    if event.type != 'content_block_delta':
                        continue
                    if event.delta.type == 'thinking_delta' and event.delta.thinking:
                        yield _line({'type': 'thinking', 'text': event.delta.thinking})
                    elif event.delta.type == 'text_delta':
                        written += len(event.delta.text)
                        if written - reported >= 2000:
                            reported = written
                            yield _line({'type': 'progress', 'chars': written})
                message = await stream.get_final_message()
        except Exception as err:  # noqa: BLE001 — reported to the viewer instead of a dropped stream
            yield _line({'type': 'error', 'message': f'{type(err).__name__}: {err}'})
            return

        if message.stop_reason == 'refusal':
            details = message.stop_details
            yield _line({'type': 'error', 'message': f'Claude declined to draw this drawing{f" ({details.explanation})" if details and details.explanation else ""}.'})
            return
        if message.stop_reason == 'max_tokens':
            yield _line({'type': 'error', 'message': (
                f'The 3D page was cut off before it was finished: Claude used all {MAX_TOKENS:,} output tokens '
                f'({written:,} characters of page written, the rest on reasoning). Try again, or set PLANT3D_MODEL to a different model.')})
            return
        html = extract_html(''.join(b.text for b in message.content if b.type == 'text'))
        if not html:
            yield _line({'type': 'error', 'message': "Claude's answer did not contain an HTML page."})
            return
        page_path(job_id).write_text(html)
        jobs.ensure_job(job_id, plant3d={'model': message.model, 'generatedAt': jobs.now_iso(),
                                         'inputTokens': message.usage.input_tokens, 'outputTokens': message.usage.output_tokens})
        yield _line({'type': 'result', 'jobId': job_id, 'url': url})
