"""Thin wrapper over the OpenAI Responses API for the pipeline: structured-
output calls with bounded concurrency.

Retries on the transient failures a long tiled run will inevitably hit (rate
limits, 408/409/5xx, connection resets) are handled by the SDK's own
`max_retries`, which backs off exponentially and honours Retry-After — the
same policy the JS pipeline implements by hand.
"""
import asyncio
import json
import os

from openai import AsyncOpenAI

_client = None


def get_client():
    global _client
    if _client is None:
        _client = AsyncOpenAI(max_retries=4)
    return _client


def model_name():
    return os.environ.get('PIPELINE_MODEL') or os.environ.get('OPENAI_MODEL') or 'gpt-5.6'


async def structured_call(*, system, content, schema, schema_name, model=None):
    """One structured-output call. Returns the parsed object.

    `schema` must satisfy the strict JSON-schema subset: every property listed
    in `required`, and `additionalProperties: false` on every object. That
    subset has no free-form maps, which is why open-ended fields in the step
    schemas are modelled as {key, value} arrays rather than objects.
    """
    response = await get_client().responses.create(
        model=model or model_name(),
        input=[
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': content},
        ],
        text={'format': {'type': 'json_schema', 'name': schema_name, 'strict': True, 'schema': schema}},
    )
    return json.loads(response.output_text)


async def map_limit(items, limit, worker):
    """Runs `worker(item, index)` with at most `limit` in flight, preserving result order."""
    semaphore = asyncio.Semaphore(max(1, limit))

    async def run(item, index):
        async with semaphore:
            return await worker(item, index)

    return await asyncio.gather(*(run(item, i) for i, item in enumerate(items)))


def text_part(text):
    return {'type': 'input_text', 'text': text}


def image_part(data_url, detail='high'):
    return {'type': 'input_image', 'image_url': data_url, 'detail': detail}
