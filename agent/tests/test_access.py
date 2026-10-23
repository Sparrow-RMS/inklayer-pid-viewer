"""API keys, allowed browser origins and the API docs (access.py)."""
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from hazop_agent import access
from hazop_agent.server import app as agent_app


def small_app():
    app = FastAPI(docs_url='/agent/docs', openapi_url='/agent/openapi.json')

    @app.get('/agent/health')
    def health():
        return {'ok': True}

    @app.get('/agent/thing')
    def thing():
        return {'thing': 1}

    @app.get('/agent/stream')
    def stream():
        return StreamingResponse((f'{i}\n' for i in range(3)), media_type='application/x-ndjson')

    access.install(app)
    return TestClient(app)


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv('AGENT_API_KEYS', 'key-one, key-two')


def test_without_keys_configured_everything_is_open(monkeypatch):
    monkeypatch.delenv('AGENT_API_KEYS', raising=False)
    assert small_app().get('/agent/thing').status_code == 200


def test_a_key_is_required_once_keys_are_configured(keys):
    client = small_app()
    response = client.get('/agent/thing')
    assert response.status_code == 401 and response.headers['www-authenticate'] == 'Bearer'
    assert client.get('/agent/thing', headers={'Authorization': 'Bearer nope'}).status_code == 401
    assert client.get('/agent/thing', headers={'Authorization': 'Bearer key-one'}).json() == {'thing': 1}
    assert client.get('/agent/thing', headers={'X-API-Key': 'key-two'}).status_code == 200


def test_health_and_docs_stay_open(keys):
    client = small_app()
    assert client.get('/agent/health').status_code == 200
    assert client.get('/agent/openapi.json').status_code == 200
    assert client.get('/agent/docs').status_code == 200


def test_streams_pass_through_the_key_check(keys):
    response = small_app().get('/agent/stream', headers={'X-API-Key': 'key-one'})
    assert response.text == '0\n1\n2\n'


def test_allowed_origins_get_cors_headers_even_on_a_401(keys, monkeypatch):
    monkeypatch.setenv('AGENT_CORS_ORIGINS', 'https://hazop.example.com')
    client = small_app()

    preflight = client.options('/agent/thing', headers={
        'Origin': 'https://hazop.example.com',
        'Access-Control-Request-Method': 'POST',
        'Access-Control-Request-Headers': 'authorization, content-type',
    })
    assert preflight.status_code == 200
    assert preflight.headers['access-control-allow-origin'] == 'https://hazop.example.com'

    denied = client.get('/agent/thing', headers={'Origin': 'https://hazop.example.com'})
    # The browser can read the 401 instead of reporting a CORS failure.
    assert denied.status_code == 401 and denied.headers['access-control-allow-origin'] == 'https://hazop.example.com'

    other = client.get('/agent/thing', headers={'Origin': 'https://evil.example', 'X-API-Key': 'key-one'})
    assert 'access-control-allow-origin' not in other.headers


def test_the_real_server_checks_keys_and_documents_them(keys):
    client = TestClient(agent_app)
    assert client.get('/agent/chat/some-thread').status_code == 401
    schema = client.get('/agent/openapi.json').json()
    assert set(schema['components']['securitySchemes']) == {'bearer', 'apiKey'}
    assert 'application/pdf' in schema['paths']['/agent/run']['post']['requestBody']['content']
