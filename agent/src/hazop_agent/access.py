"""Who may call the agent: API keys, allowed browser origins, and the OpenAPI docs.

Set in agent/.env (read when the server starts):

    AGENT_API_KEYS=key-for-viewer,key-for-erp     # comma-separated; empty = no key needed (local dev only)
    AGENT_CORS_ORIGINS=https://hazop.example.com  # comma-separated origins allowed to call from a browser

With keys set, every /agent/* request must carry one, as either header:

    Authorization: Bearer <key>
    X-API-Key: <key>

except the health check and the API docs (/agent/docs, /agent/openapi.json),
so a load balancer and an integrator can reach those without one. Browser
preflights (OPTIONS) are answered by the CORS layer before any key check.

A key handed to a browser app is readable by anyone who can load that app, so
it identifies the app, not the person; keep the viewer behind your sign-in.
"""
import hmac
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

OPEN_PATHS = frozenset({'/agent/health', '/agent/docs', '/agent/docs/oauth2-redirect', '/agent/openapi.json'})


def _split(value):
    return [v.strip() for v in (value or '').split(',') if v.strip()]


def api_keys():
    # Read per request, so a test (or a restart-free key rotation script) can change it.
    return _split(os.environ.get('AGENT_API_KEYS'))


def cors_origins():
    return _split(os.environ.get('AGENT_CORS_ORIGINS'))


def presented_key(headers):
    auth = headers.get('authorization', '')
    if auth[:7].lower() == 'bearer ':
        return auth[7:].strip()
    return headers.get('x-api-key', '').strip()


def key_is_valid(key, keys):
    # Compare against every key, in constant time, so timing reveals nothing.
    return bool(key) and any([hmac.compare_digest(key.encode(), k.encode()) for k in keys])


class ApiKeyMiddleware:
    """Plain ASGI, so the pipeline's long NDJSON streams pass through untouched."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            keys = api_keys()
            path = scope['path']
            if keys and path.startswith('/agent') and path not in OPEN_PATHS and scope['method'] != 'OPTIONS':
                if not key_is_valid(presented_key(Headers(scope=scope)), keys):
                    response = JSONResponse({'detail': 'missing or invalid API key'}, status_code=401,
                                            headers={'WWW-Authenticate': 'Bearer'})
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def install(app: FastAPI):
    """Adds the key check, then CORS around it, so a 401 still carries CORS headers."""
    app.add_middleware(ApiKeyMiddleware)
    origins = cors_origins()
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'],
            allow_headers=['Authorization', 'X-API-Key', 'Content-Type'],
            # Keys travel in headers, never cookies.
            allow_credentials=False,
            max_age=3600,
        )

    def openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
        schema.setdefault('components', {})['securitySchemes'] = {
            'bearer': {'type': 'http', 'scheme': 'bearer', 'description': 'Authorization: Bearer <AGENT_API_KEYS entry>'},
            'apiKey': {'type': 'apiKey', 'in': 'header', 'name': 'X-API-Key'},
        }
        schema['security'] = [{'bearer': []}, {'apiKey': []}]
        app.openapi_schema = schema
        return schema

    app.openapi = openapi
