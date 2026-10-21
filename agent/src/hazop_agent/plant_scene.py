"""The plant as a 3D scene built from the pipeline's own results, so HAZOP nodes
map onto it exactly. The viewer draws it with its own Three.js renderer
(src/lib/plantScene3d.ts) and colours each node's items and pipes.

    GET  /agent/plant-scene/{job_id}             the scene (404 until the pipeline has run)
    POST /agent/plant-scene/{job_id}[?force=true] ask Claude for the equipment's 3D forms, then the scene

Separate from plant3d.py, where Claude writes a whole free-form page: here
every object is a step 1 entity or a traced pipe span, keyed by the same ids
the nodes list, so a node's members are its objects.

Layout: items stay where the drawing puts them, as a floor plan (page x -> east,
page y -> south), and the traced spans are the pipes, along their drawn routes.
Moving equipment to a guessed site layout would break the pipes and the node
boundaries along them. What the drawing doesn't give is the third dimension:
Claude reads the equipment list and the sheet and says, per item, what shape it
is, how big it really is and how high it stands (`forms`). Items it says nothing
about get a form from their type text, so the scene is drawable before Claude
has run, and the forms only refine it.

The forms are cached as `plant-scene-forms` beside the provider's step 1
(jobs.results_dir), keyed to the step 1 run they were made from; the scene is
composed on every GET, so it follows the latest step 1 and network without
another call.
"""
import asyncio
import json
import os
import re
import statistics

from fastapi import APIRouter, HTTPException, Query

from .pipeline import jobs, llm
from .pipeline.pdf import load_pdf, render_region, to_data_url

router = APIRouter()
_lock = asyncio.Lock()

FORMS = 'plant-scene-forms'
# Drawn in 3D; notes, labels, line-number labels, C&E references and data
# callouts are drawing furniture.
DRAWN = ('equipment', 'valve', 'instrument', 'connector', 'fitting')
SHAPES = ('vertical_vessel', 'column', 'tank', 'horizontal_vessel', 'heat_exchanger', 'pump', 'compressor',
          'filter', 'agitator', 'motor', 'heater', 'skid', 'other')
# Metres across the sheet when nothing better is known.
DEFAULT_PAGE_WIDTH_M = 80.0

FORMS_SCHEMA = {
    'type': 'object',
    'properties': {
        'items': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'id': {'type': 'string'},
                    'shape': {'type': 'string', 'enum': list(SHAPES)},
                    'diameterM': {'type': 'number', 'description': 'real outside diameter, or width for a box-like item, in metres'},
                    'heightM': {'type': 'number', 'description': 'real height of the body (length for a horizontal vessel), in metres'},
                    'elevationM': {'type': 'number', 'description': 'height of the bottom of the body above grade, in metres'},
                },
                'required': ['id', 'shape', 'diameterM', 'heightM', 'elevationM'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['items'],
    'additionalProperties': False,
}

FORMS_SYSTEM = """You are a process plant layout engineer. You are given the equipment read from a P&ID sheet (id, tag, \
type, description and any data such as capacity), and the sheet itself. For each item, say what it physically is and how \
big it is on site, so it can be drawn in 3D:
- shape: the closest of the listed shapes;
- diameterM: real outside diameter (or width, for box-like items), in metres;
- heightM: real body height (tangent to tangent; the length for a horizontal vessel or exchanger), in metres;
- elevationM: height of the bottom of the body above grade, in metres (0 for items at grade; a vessel on legs or a skirt \
sits above 0; an agitator or a motor mounted on a vessel sits at that vessel's top).
Use the data given (capacity, dimensions) where there is any; otherwise typical sizes for that service. Answer for every \
id given and only those ids."""


def _anthropic_model():
    return os.environ.get('PLANT_SCENE_MODEL') or llm.anthropic_model_name()


def _page_of(doc, index):
    return next((p for p in (doc or {}).get('pages', []) if p['pageIndex'] == index), None)


def _box(entity):
    return entity.get('symbolBbox') or entity['bbox']


def _default_form(entity):
    """A form from the type text alone, for items Claude hasn't sized (or before it has run)."""
    text = f"{entity.get('type', '')} {entity.get('description', '')}".lower()
    category = entity['category']
    if category == 'valve':
        return {'shape': 'valve', 'diameterM': 0.4, 'heightM': 0.5, 'elevationM': None}
    if category == 'instrument':
        return {'shape': 'instrument', 'diameterM': 0.3, 'heightM': 0.3, 'elevationM': None}
    if category == 'connector':
        return {'shape': 'connector', 'diameterM': 0.6, 'heightM': 0.6, 'elevationM': None}
    if category == 'fitting':
        return {'shape': 'fitting', 'diameterM': 0.3, 'heightM': 0.4, 'elevationM': None}
    rules = (
        ('column', ('column', 'tower', 'scrubber', 'absorber', 'stripper'), 1.5, 15, 1),
        ('tank', ('tank',), 6, 8, 0),
        ('heat_exchanger', ('exchanger', 'cooler', 'condenser', 'reboiler'), 0.8, 5, 1),
        ('horizontal_vessel', ('horizontal', 'drum', 'separator'), 2, 6, 1),
        ('pump', ('pump',), 0.8, 0.8, 0.3),
        ('compressor', ('compressor', 'blower', 'fan'), 2, 2, 0.3),
        ('filter', ('filter', 'strainer'), 0.6, 1.5, 0.5),
        ('agitator', ('agitator', 'mixer'), 0.5, 2, None),
        ('motor', ('motor',), 0.5, 0.6, 0.3),
        ('heater', ('heater', 'furnace', 'boiler'), 3, 4, 0),
        ('vertical_vessel', ('vessel', 'reactor', 'receiver', 'accumulator'), 2, 4, 1),
    )
    for shape, words, diameter, height, elevation in rules:
        if any(w in text for w in words):
            return {'shape': shape, 'diameterM': diameter, 'heightM': height, 'elevationM': elevation}
    return {'shape': 'other', 'diameterM': 1.5, 'heightM': 1.5, 'elevationM': 0}


def _page_width_m(entities, forms, width, height):
    """Metres across the sheet: the median, over the equipment Claude sized, of
    real size over drawn symbol size. Symbols aren't drawn to scale, so this is
    an average that keeps the plant's overall proportions plausible."""
    ratios = []
    for e in entities:
        form = forms.get(e['id'])
        if e['category'] != 'equipment' or not form or form['diameterM'] <= 0:
            continue
        box = _box(e)
        drawn = min(box['width'], box['height'] * height / width)
        if drawn > 0:
            ratios.append(form['diameterM'] / drawn)
    if not ratios:
        return DEFAULT_PAGE_WIDTH_M
    return min(400.0, max(20.0, statistics.median(ratios)))


def compose_page(step1_page, network_page, forms):
    """One page of the scene, in page fractions plus the metres the renderer needs."""
    width, height = step1_page['width'], step1_page['height']
    entities = [e for e in step1_page['entities'] if e['category'] in DRAWN]
    items = []
    for e in entities:
        form = forms.get(e['id']) or _default_form(e)
        box = _box(e)
        items.append({
            'id': e['id'],
            'category': e['category'],
            'tag': e.get('tag'),
            'type': e.get('type', ''),
            'x': box['x'] + box['width'] / 2,
            'y': box['y'] + box['height'] / 2,
            'w': box['width'],
            'h': box['height'],
            **form,
            'sizedBy': 'claude' if e['id'] in forms else 'type',
        })
    spans = [{'id': s['id'], 'points': s['points'], 'lineNumbers': s.get('lineNumbers', [])}
             for s in (network_page or {}).get('spans', []) if len(s.get('points', [])) >= 2]
    return {
        'pageIndex': step1_page['pageIndex'],
        'aspect': height / width,
        'pageWidthM': _page_width_m(entities, forms, width, height),
        'items': items,
        'spans': spans,
    }


def _require(job_id):
    if not re.fullmatch(r'[a-z0-9-]+', job_id) or not jobs.has_artifact(job_id, 'step1'):
        raise HTTPException(404, 'run the HAZOP pipeline on this drawing first: the 3D nodes view is built from its results')
    return jobs.read_artifact(job_id, 'step1')


def compose(job_id):
    step1 = _require(job_id)
    network = jobs.read_artifact(job_id, 'step1-network') if jobs.has_artifact(job_id, 'step1-network') else None
    cached = jobs.read_artifact(job_id, FORMS) if jobs.has_artifact(job_id, FORMS) else None
    # Forms from an earlier step 1 name ids that may now mean other items.
    current = cached if cached and cached.get('step1GeneratedAt') == step1.get('generatedAt') else None
    forms = {} if current is None else current['forms']
    return {
        'jobId': job_id,
        'forms': None if current is None else {'model': current['model'], 'generatedAt': current['generatedAt']},
        'formsStale': cached is not None and current is None,
        'pages': [compose_page(page, _page_of(network, page['pageIndex']), forms) for page in step1['pages']],
    }


def _equipment_listing(step1_page):
    # Data callouts (capacity, design conditions) live on their own rows, keyed by tag.
    data = {}
    for e in step1_page['entities']:
        if e['category'] == 'equipment_list_row' and e.get('tag'):
            data[e['tag']] = {a['key']: a['value'] for a in e.get('attributes', [])}
    lines = []
    for e in step1_page['entities']:
        if e['category'] != 'equipment':
            continue
        attributes = {a['key']: a['value'] for a in e.get('attributes', [])} | data.get(e.get('tag') or '', {})
        lines.append(json.dumps({'id': e['id'], 'tag': e.get('tag'), 'type': e.get('type'),
                                 'description': e.get('description'), 'data': attributes}, ensure_ascii=False))
    return lines


async def _forms_for_page(pdf, step1_page):
    listing = _equipment_listing(step1_page)
    if not listing:
        return {}
    png = render_region(pdf[step1_page['pageIndex']], {'x': 0, 'y': 0, 'width': 1, 'height': 1}, target_px=1600)['png']
    content = [
        llm.image_part(to_data_url(png)),
        llm.text_part(f'P&ID page index {step1_page["pageIndex"]}. Equipment ({len(listing)} items), one JSON object per line:\n'
                      + '\n'.join(listing)),
    ]
    answer = await llm.anthropic_structured_call(system=FORMS_SYSTEM, content=content, schema=FORMS_SCHEMA,
                                                 schema_name='plant_scene_forms', model=_anthropic_model())
    known = {e['id'] for e in step1_page['entities'] if e['category'] == 'equipment'}
    return {f['id']: {k: f[k] for k in ('shape', 'diameterM', 'heightM', 'elevationM')}
            for f in answer['items'] if f['id'] in known and f['diameterM'] > 0 and f['heightM'] > 0}


@router.get('/agent/plant-scene/{job_id}')
async def get_scene(job_id: str):
    return compose(job_id)


@router.post('/agent/plant-scene/{job_id}')
async def generate_forms(job_id: str, force: bool = Query(False)):
    step1 = _require(job_id)
    if _lock.locked():
        raise HTTPException(409, 'the 3D forms are already being generated')
    async with _lock:
        cached = jobs.read_artifact(job_id, FORMS) if jobs.has_artifact(job_id, FORMS) else None
        if force or not cached or cached.get('step1GeneratedAt') != step1.get('generatedAt'):
            pdf = load_pdf(jobs.source_path(job_id))
            forms = {}
            try:
                for page in step1['pages']:
                    forms |= await _forms_for_page(pdf, page)
            except Exception as err:  # noqa: BLE001 — the viewer shows it and keeps the default forms
                raise HTTPException(502, f'Claude could not size the equipment: {type(err).__name__}: {err}') from err
            jobs.write_artifact(job_id, FORMS, {'step1GeneratedAt': step1.get('generatedAt'), 'model': _anthropic_model(),
                                                'generatedAt': jobs.now_iso(), 'forms': forms})
    return compose(job_id)
