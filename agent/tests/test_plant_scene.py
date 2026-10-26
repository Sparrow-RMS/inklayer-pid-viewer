"""The 3D nodes scene: the pipeline's entities and pipes, with Claude's forms."""
import pytest
from fastapi.testclient import TestClient

from hazop_agent import plant_scene
from hazop_agent.pipeline import jobs, llm
from hazop_agent.server import app

from .conftest import FIXTURE_PDF

client = TestClient(app)
JOB = 'job-1'


def _entity(id_, category, tag, type_, x, attributes=()):
    box = {'x': x, 'y': 0.2, 'width': 0.1, 'height': 0.1}
    return {'id': id_, 'category': category, 'tag': tag, 'type': type_, 'description': '', 'bbox': box,
            'attributes': list(attributes)}


@pytest.fixture
def job(jobs_dir):
    jobs.write_artifact(JOB, 'step1', {'generatedAt': 'run-1', 'pages': [{'pageIndex': 0, 'width': 2000, 'height': 1000, 'entities': [
        _entity('p0-eq-1', 'equipment', 'T-1', 'vertical storage tank', 0.1),
        _entity('p0-eq-2', 'equipment', 'P-1', 'centrifugal pump', 0.5),
        _entity('p0-vl-1', 'valve', None, 'gate valve', 0.3),
        _entity('p0-row-1', 'equipment_list_row', 'T-1', 'data callout', 0.9, [{'key': 'capacity', 'value': '250 m3'}]),
        _entity('p0-nt-1', 'note', None, 'note', 0.7),
    ]}]})
    jobs.write_artifact(JOB, 'step1-network', {'pages': [{'pageIndex': 0, 'spans': [
        {'id': 'p0-span-1', 'points': [{'x': 0.15, 'y': 0.25}, {'x': 0.55, 'y': 0.25}], 'lineNumbers': ['L-1']},
        {'id': 'p0-span-2', 'points': [{'x': 0.1, 'y': 0.1}]},  # a single point is no pipe
    ]}]})
    jobs.stash_source(JOB, FIXTURE_PDF.read_bytes())


def test_scene_keeps_pipeline_ids_and_the_drawn_layout(job):
    scene = client.get(f'/agent/plant-scene/{JOB}').json()

    page = scene['pages'][0]
    assert [i['id'] for i in page['items']] == ['p0-eq-1', 'p0-eq-2', 'p0-vl-1']  # no notes or data callouts
    assert [s['id'] for s in page['spans']] == ['p0-span-1']
    tank = page['items'][0]
    assert (tank['x'], tank['y']) == pytest.approx((0.15, 0.25))
    # Before Claude: forms from the type text.
    assert tank['shape'] == 'tank' and tank['sizedBy'] == 'type'
    assert page['items'][1]['shape'] == 'pump' and page['items'][2]['shape'] == 'valve'
    assert scene['forms'] is None and page['aspect'] == 0.5


def test_claude_sizes_the_equipment_and_sets_the_scale(job, monkeypatch):
    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        return {'items': [
            {'id': 'p0-eq-1', 'shape': 'tank', 'diameterM': 7, 'heightM': 8, 'elevationM': 0},
            {'id': 'p0-eq-2', 'shape': 'pump', 'diameterM': 0.7, 'heightM': 0.9, 'elevationM': 0.3},
            {'id': 'p0-vl-1', 'shape': 'other', 'diameterM': 1, 'heightM': 1, 'elevationM': 0},  # not equipment: ignored
        ]}
    monkeypatch.setattr(llm, 'anthropic_structured_call', fake)

    scene = client.post(f'/agent/plant-scene/{JOB}').json()

    assert '"capacity": "250 m3"' in seen['content'][1]['text']  # the data callout is joined by tag
    items = {i['id']: i for i in scene['pages'][0]['items']}
    assert items['p0-eq-1']['heightM'] == 8 and items['p0-eq-1']['sizedBy'] == 'model'
    assert items['p0-vl-1']['sizedBy'] == 'type'
    # Median of real / drawn diameter: 7 / 0.05 and 0.7 / 0.05 -> 77, within the clamp.
    assert scene['pages'][0]['pageWidthM'] == pytest.approx(77)

    # Cached: a second POST makes no call; a new step 1 run makes the forms stale.
    seen.clear()
    client.post(f'/agent/plant-scene/{JOB}')
    assert not seen
    step1 = jobs.read_artifact(JOB, 'step1')
    jobs.write_artifact(JOB, 'step1', {**step1, 'generatedAt': 'run-2'})
    stale = client.get(f'/agent/plant-scene/{JOB}').json()
    assert stale['formsStale'] is True and stale['pages'][0]['items'][0]['sizedBy'] == 'type'


def test_a_claude_failure_is_reported_and_nothing_is_cached(job, monkeypatch):
    async def fail(**kwargs):
        raise RuntimeError('overloaded')
    monkeypatch.setattr(llm, 'anthropic_structured_call', fail)

    response = client.post(f'/agent/plant-scene/{JOB}')
    assert response.status_code == 502 and 'overloaded' in response.json()['detail']
    assert not jobs.has_artifact(JOB, plant_scene.FORMS)


def test_needs_the_pipeline_first(jobs_dir):
    assert client.get('/agent/plant-scene/never-run').status_code == 404
    assert client.get('/agent/plant-scene/..%2Fx').status_code == 404


async def test_two_sizing_requests_for_one_drawing_make_one_call(job, monkeypatch):
    import asyncio

    import httpx

    calls = []
    gate = asyncio.Event()

    async def slow(**kwargs):
        calls.append(kwargs)
        await gate.wait()
        return {'items': [{'id': 'p0-eq-1', 'shape': 'tank', 'diameterM': 7, 'heightM': 8, 'elevationM': 0}]}
    monkeypatch.setattr(llm, 'anthropic_structured_call', slow)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as http:
        first = asyncio.create_task(http.post(f'/agent/plant-scene/{JOB}'))
        while not calls:
            await asyncio.sleep(0.01)
        second = asyncio.create_task(http.post(f'/agent/plant-scene/{JOB}', params={'force': True}))
        await asyncio.sleep(0.05)
        gate.set()
        a, b = await first, await second

    assert a.status_code == b.status_code == 200  # no 409: the second waited
    assert len(calls) == 1  # and took the first one's answer
    assert b.json()['pages'][0]['items'][0]['sizedBy'] == 'model'
