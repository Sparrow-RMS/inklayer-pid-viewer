"""PIPELINE_PROVIDER: Claude as the pipeline's model, its results kept apart, and the comparison."""
import json
from types import SimpleNamespace as NS

import pytest

from hazop_agent.pipeline import compare, jobs, llm


class FakeStream:
    def __init__(self, message):
        self.message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_final_message(self):
        return self.message


@pytest.fixture
def claude(monkeypatch):
    monkeypatch.setenv('PIPELINE_PROVIDER', 'anthropic')
    calls = []

    def install(text='{"ok": true}', stop_reason='end_turn'):
        message = NS(stop_reason=stop_reason, stop_details=None, usage=NS(output_tokens=5),
                     content=[NS(type='thinking', thinking=''), NS(type='text', text=text)])

        def stream(**kwargs):
            calls.append(kwargs)
            return FakeStream(message)
        monkeypatch.setattr(llm, 'get_anthropic_client', lambda: NS(beta=NS(messages=NS(stream=stream))))
        return calls

    return install


SCHEMA = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False}


async def test_structured_call_goes_to_claude_with_the_same_schema_and_content(claude):
    calls = claude()

    result = await llm.structured_call(system='sys', schema=SCHEMA, schema_name='s',
                                       content=[llm.text_part('hello'), llm.image_part('data:image/png;base64,AAAA')])

    assert result == {'ok': True}
    call = calls[0]
    assert call['model'] == 'claude-opus-5-5' and call['system'] == 'sys'
    assert call['output_config']['format'] == {'type': 'json_schema', 'schema': SCHEMA}
    assert call['messages'][0]['content'] == [
        {'type': 'text', 'text': 'hello'},
        {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AAAA'}},
    ]


@pytest.mark.parametrize('stop_reason, error', [('refusal', 'declined'), ('max_tokens', 'ANTHROPIC_PIPELINE_MAX_TOKENS')])
async def test_unusable_answers_raise(claude, stop_reason, error):
    claude(stop_reason=stop_reason)
    with pytest.raises(RuntimeError, match=error):
        await llm.structured_call(system='s', schema=SCHEMA, schema_name='s', content=[llm.text_part('x')])


def test_an_unknown_provider_is_an_error(monkeypatch):
    monkeypatch.setenv('PIPELINE_PROVIDER', 'gemini')
    with pytest.raises(ValueError, match='PIPELINE_PROVIDER'):
        llm.provider()


def test_each_provider_keeps_its_own_results(jobs_dir, monkeypatch):
    jobs.write_artifact('job-1', 'step3', {'by': 'openai'})
    monkeypatch.setenv('PIPELINE_PROVIDER', 'anthropic')
    assert not jobs.has_artifact('job-1', 'step3')

    jobs.write_artifact('job-1', 'step3', {'by': 'anthropic'})
    assert (jobs_dir / 'job-1' / 'anthropic' / 'step3.json').exists()
    monkeypatch.setenv('PIPELINE_PROVIDER', 'openai')
    assert jobs.read_artifact('job-1', 'step3') == {'by': 'openai'}


def _write(jobs_dir, provider_name, tags, nodes):
    d = jobs.results_dir('job-1', provider_name)
    d.mkdir(parents=True, exist_ok=True)
    entities = [{'id': f'e{i}', 'category': 'equipment', 'tag': t, 'anchored': True} for i, t in enumerate(tags)]
    ids = {t: f'e{i}' for i, t in enumerate(tags)}
    (d / 'step1.json').write_text(json.dumps({'pages': [{'pageIndex': 0, 'entities': entities}]}))
    (d / 'step3.json').write_text(json.dumps({'pages': [{'pageIndex': 0, 'validation': {'counts': {'error': 0, 'warning': 1}},
                                                         'nodes': [{'label': label, 'entityIds': [ids[t] for t in members]}
                                                                   for label, members in nodes]}]}))


def test_compare_pairs_nodes_by_tag_overlap(jobs_dir):
    # Same plant, ids minted differently: tags are what line up.
    _write(jobs_dir, 'openai', ['V-1', 'P-1', 'E-1', 'T-1'], [('A', ['V-1', 'P-1']), ('B', ['E-1', 'T-1'])])
    _write(jobs_dir, 'anthropic', ['T-1', 'E-1', 'P-1', 'V-1'], [('X', ['V-1']), ('Y', ['P-1', 'E-1', 'T-1'])])

    report = compare.compare('job-1')

    pairs = {(p['openai'], p['anthropic']): p for p in report['pages'][0]['pairs']}
    assert pairs[('B', 'Y')]['overlap'] == 0.67 and pairs[('B', 'Y')]['onlyAnthropic'] == ['P-1']
    assert pairs[('A', 'X')]['overlap'] == 0.5 and pairs[('A', 'X')]['onlyOpenai'] == ['P-1']
    assert report['agreement'] == 0.58
    assert 'only Claude: P-1' in compare.format_report(report)


def test_compare_says_which_provider_has_not_run(jobs_dir):
    _write(jobs_dir, 'openai', ['V-1'], [('A', ['V-1'])])
    report = compare.compare('job-1')
    assert report['missing'] == ['anthropic']
    assert '--provider <provider>' in compare.format_report(report)


def _too_large():
    import httpx
    from anthropic import BadRequestError
    request = httpx.Request('POST', 'https://api.anthropic.com/v1/messages')
    body = {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'The compiled grammar is too large, which would cause performance issues.'}}
    return BadRequestError(body['error']['message'], response=httpx.Response(400, request=request, json=body), body=body)


async def test_a_schema_too_large_to_compile_goes_unconstrained_and_is_validated(claude, monkeypatch):
    monkeypatch.setattr(llm, '_uncompilable_schemas', set())
    answers = iter([
        _too_large(),
        'Here you go: {"wrong": 1}',      # unconstrained, doesn't conform -> one repair turn
        '{"ok": true}',
    ])
    calls = []

    def stream(**kwargs):
        calls.append(kwargs)
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return FakeStream(NS(stop_reason='end_turn', stop_details=None, usage=NS(output_tokens=5),
                             content=[NS(type='text', text=answer)]))
    monkeypatch.setattr(llm, 'get_anthropic_client', lambda: NS(beta=NS(messages=NS(stream=stream))))

    result = await llm.structured_call(system='sys', schema=SCHEMA, schema_name='big', content=[llm.text_part('x')])

    assert result == {'ok': True}
    assert 'format' in calls[0]['output_config'] and 'format' not in calls[1]['output_config']
    assert '"required": ["ok"]' in calls[1]['system']
    assert "'ok' is a required property" in calls[2]['messages'][-1]['content']

    # Known now: the next call with this schema skips the constrained attempt.
    answers = iter(['{"ok": false}'])
    calls.clear()
    assert await llm.structured_call(system='sys', schema=SCHEMA, schema_name='big', content=[llm.text_part('x')]) == {'ok': False}
    assert len(calls) == 1 and 'format' not in calls[0]['output_config']


def test_echo_drops_declined_thinking_before_a_fallback():
    content = [NS(type='thinking'), NS(type='text'), NS(type='fallback'), NS(type='thinking'), NS(type='text')]
    assert [b.type for b in llm._echo(content)] == ['text', 'fallback', 'thinking', 'text']


@pytest.mark.parametrize('raw, expected', [(None, 64000), ('128000', 128000), ('1280004', 128000), ('abc', 64000), ('10', 1024)])
def test_max_tokens_is_capped_at_what_claude_accepts(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv('ANTHROPIC_PIPELINE_MAX_TOKENS', raising=False)
    else:
        monkeypatch.setenv('ANTHROPIC_PIPELINE_MAX_TOKENS', raw)
    assert llm.anthropic_max_tokens() == expected
