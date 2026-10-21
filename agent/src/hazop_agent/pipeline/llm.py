"""Thin wrapper over the model APIs for the pipeline: structured-output calls
with bounded concurrency.

PIPELINE_PROVIDER picks the API: `openai` (default, the Responses API) or
`anthropic` (Claude's Messages API, with structured outputs). The steps build
their content with text_part / image_part and never see the difference. Each
provider's results are kept in their own place in the job (jobs.py), so the two
can be run on the same drawing and compared.

Retries on the transient failures a long tiled run will inevitably hit (rate
limits, 408/409/5xx, connection resets) are handled by the SDK's own
`max_retries`, which backs off exponentially and honours Retry-After — the
same policy the JS pipeline implements by hand.

The client is wrapped for LangSmith, so each call shows up as a child run of
the graph node that made it. Tracing is off unless LANGSMITH_TRACING=true.
"""
import asyncio
import json
import os

import jsonschema
from anthropic import AsyncAnthropic, BadRequestError
from langsmith.wrappers import wrap_openai
from openai import AsyncOpenAI

_client = None
_anthropic_client = None

PROVIDERS = ('openai', 'anthropic')


def provider():
    """`openai` or `anthropic`, read at call time like PIPELINE_JOBS_DIR."""
    value = (os.environ.get('PIPELINE_PROVIDER') or 'openai').strip().lower()
    if value not in PROVIDERS:
        raise ValueError(f'PIPELINE_PROVIDER must be one of {", ".join(PROVIDERS)}, not {value!r}')
    return value


def has_api_key():
    if provider() == 'anthropic':
        # An `ant auth login` profile also works, but can't be seen from here
        # without a call; the key is what .env sets.
        return bool(os.environ.get('ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_AUTH_TOKEN'))
    return bool(os.environ.get('OPENAI_API_KEY'))


def api_key_name():
    return 'ANTHROPIC_API_KEY' if provider() == 'anthropic' else 'OPENAI_API_KEY'


def get_client():
    global _client
    if _client is None:
        # PIPELINE_TIMEOUT (seconds) lifts the SDK's 10-minute default for slow
        # reasoning models; a call that times out is retried from scratch.
        timeout = os.environ.get('PIPELINE_TIMEOUT')
        options = {'timeout': float(timeout)} if timeout else {}
        _client = wrap_openai(AsyncOpenAI(max_retries=4, **options))
    return _client


def openai_model_name():
    return os.environ.get('PIPELINE_MODEL') or os.environ.get('OPENAI_MODEL') or 'gpt-5.6'


def anthropic_model_name():
    return os.environ.get('ANTHROPIC_PIPELINE_MODEL') or 'claude-opus-5-5'


def model_name():
    return anthropic_model_name() if provider() == 'anthropic' else openai_model_name()


def get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        timeout = os.environ.get('PIPELINE_TIMEOUT')
        options = {'timeout': float(timeout)} if timeout else {}
        _anthropic_client = AsyncAnthropic(max_retries=4, **options)
    return _anthropic_client


def _anthropic_part(part):
    """An OpenAI Responses content part as a Claude content block."""
    if part['type'] == 'input_text':
        return {'type': 'text', 'text': part['text']}
    if part['type'] == 'input_image':
        header, _, data = part['image_url'].partition(',')
        media_type = header.removeprefix('data:').removesuffix(';base64')
        return {'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': data}}
    raise ValueError(f"no Claude equivalent for content part {part['type']!r}")


# Schemas Claude's structured outputs can't compile ("grammar is too large",
# e.g. step 3's node breakdown). Calls with them go unconstrained instead.
_uncompilable_schemas = set()

SCHEMA_INSTRUCTIONS = (
    'Answer with a single JSON object that conforms to the JSON schema below, and nothing else: '
    'no Markdown fence, no text before or after it. Every property listed in "required" must be present.\n\n'
)


async def _anthropic_message(*, system, messages, model, schema=None):
    # Streamed: a whole-sheet extraction can run long, and a non-streaming call
    # with this max_tokens would trip the SDK's timeout guard.
    output_config = {'effort': os.environ.get('ANTHROPIC_PIPELINE_EFFORT') or 'high'}
    if schema is not None:
        output_config['format'] = {'type': 'json_schema', 'schema': schema}
    async with get_anthropic_client().beta.messages.stream(
        model=model,
        max_tokens=int(os.environ.get('ANTHROPIC_PIPELINE_MAX_TOKENS') or 64000),
        system=system,
        messages=messages,
        output_config=output_config,
        # A declined request is re-run on Anthropic's recommended fallback model.
        betas=['server-side-fallback-2026-07-01'],
        fallbacks='default',
    ) as stream:
        message = await stream.get_final_message()
    if message.stop_reason == 'refusal':
        details = message.stop_details
        raise RuntimeError(f"Claude declined the request{f': {details.explanation}' if details and details.explanation else ''}")
    if message.stop_reason == 'max_tokens':
        raise RuntimeError(f'Claude ran out of output tokens ({message.usage.output_tokens}) before finishing; '
                           'raise ANTHROPIC_PIPELINE_MAX_TOKENS (up to 128000)')
    return message


def _answer_text(message):
    return ''.join(b.text for b in message.content if b.type == 'text')


def _echo(content):
    """An answer as it goes back in the conversation: unchanged, except that after a
    mid-answer fallback the declined model's thinking before the switch is left out."""
    switches = [i for i, b in enumerate(content) if b.type == 'fallback']
    if not switches:
        return content
    return [b for i, b in enumerate(content) if i > switches[-1] or b.type not in ('thinking', 'redacted_thinking')]


def _parse_unconstrained(text, schema):
    """The JSON object in an unconstrained answer, checked against the schema.
    Returns (value, problem); problem is None when it conforms."""
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end < start:
        return None, 'the answer contains no JSON object'
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError as err:
        return None, f'the JSON does not parse: {err}'
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.absolute_path))
    if errors:
        listed = '\n'.join(f"- at {'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message[:300]}" for e in errors[:20])
        return value, f'{len(errors)} schema violation(s):\n{listed}'
    return value, None


async def anthropic_structured_call(*, system, content, schema, schema_name, model=None):
    """structured_call on Claude, whatever PIPELINE_PROVIDER says."""
    model = model or anthropic_model_name()
    messages = [{'role': 'user', 'content': [_anthropic_part(p) for p in content]}]
    if schema_name not in _uncompilable_schemas:
        try:
            message = await _anthropic_message(system=system, messages=messages, model=model, schema=schema)
            return json.loads(_answer_text(message))
        except BadRequestError as err:
            # The API has no distinct error type for this; it is a 400 with this message.
            if 'grammar is too large' not in str(err):
                raise
            _uncompilable_schemas.add(schema_name)

    # Unconstrained: the schema goes in the system prompt and the answer is
    # validated here, with one repair turn if it doesn't conform.
    system = f'{system}\n\n{SCHEMA_INSTRUCTIONS}{json.dumps(schema)}'
    message = await _anthropic_message(system=system, messages=messages, model=model)
    value, problem = _parse_unconstrained(_answer_text(message), schema)
    if problem is None:
        return value
    messages += [
        {'role': 'assistant', 'content': _echo(message.content)},
        {'role': 'user', 'content': f'Your answer does not conform to the schema: {problem}\n\nSend the whole corrected JSON object.'},
    ]
    message = await _anthropic_message(system=system, messages=messages, model=model)
    value, problem = _parse_unconstrained(_answer_text(message), schema)
    if problem is not None:
        raise RuntimeError(f'Claude\'s {schema_name} answer did not conform to its schema after a repair turn: {problem}')
    return value


async def structured_call(*, system, content, schema, schema_name, model=None):
    """One structured-output call. Returns the parsed object.

    `schema` must satisfy the strict JSON-schema subset: every property listed
    in `required`, and `additionalProperties: false` on every object. That
    subset has no free-form maps, which is why open-ended fields in the step
    schemas are modelled as {key, value} arrays rather than objects. Claude's
    structured outputs take the same subset.
    """
    if provider() == 'anthropic':
        return await anthropic_structured_call(system=system, content=content, schema=schema, schema_name=schema_name,
                                                model=model or model_name())
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
