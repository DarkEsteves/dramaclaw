"""One complete draft request tolerates unknown model fields, not invalid content."""
from copy import deepcopy
import json

import pytest

from novelvideo.freezone.remix import production_plan
from novelvideo.freezone.recipe_runtime import get_recipe_for_runtime
from test_production_plan import native_plan


def validate(payload):
    return production_plan.validate_generated_production_plan(
        payload, topic='中文咖啡短片',
        recipe=get_recipe_for_runtime(username='alice', recipe_id='reference-production-plan'))


def test_generated_extra_fields_are_ignored_at_each_schema_object():
    payload = native_plan()
    payload['commentary'] = 'only a model note'
    payload['provenance'] = {'notes': 'model annotation', 'source_evidence_ref': {'artifact_id': 'fake'},
                             'transcript_ref': {'artifact_id': 'fake'}, 'operation_id': 'a' * 32}
    payload['output']['quality_note'] = 'vertical'
    payload['beats'][0]['summary'] = 'extra'
    payload['beats'][0]['turns'] = [{'id': 't1', 'speaker': '甲', 'text': '黑咖啡',
                                   'emotion': 'happy', 'speech_placement_id': 'hook-picture'}]
    payload['placements'][0]['source_audio'] = True
    payload['placements'][0]['camera_hint'] = 'extra'
    payload['placements'][0]['start']['note'] = 'extra'
    payload['placements'].append({
        'id': 'g1', 'kind': 'graphic', 'role': 'effect', 'track_id': 'graphics',
        'start': {'kind': 'beat', 'ref_id': 'hook', 'edge': 'start'},
        'end': {'kind': 'beat', 'ref_id': 'hook', 'edge': 'end'},
        'graphic': {'template': 'keyword', 'text': '黑咖啡', 'extra_style': 'bold'},
    })
    before = deepcopy(payload)
    result = validate(payload)
    assert payload == before
    assert result['provenance'] == {}
    assert result['beats'][0]['turns'][0]['text'] == '黑咖啡'
    assert 'commentary' not in result and 'quality_note' not in result['output']
    assert 'summary' not in result['beats'][0]
    assert 'emotion' not in result['beats'][0]['turns'][0]
    assert 'camera_hint' not in result['placements'][0]
    assert 'note' not in result['placements'][0]['start']
    assert 'extra_style' not in result['placements'][1]['graphic']


@pytest.mark.parametrize('defect', ['missing', 'type', 'reference'])
def test_ignoring_extras_does_not_hide_invalid_known_fields(defect):
    payload = native_plan()
    payload['provenance'] = {'notes': 'extra'}
    if defect == 'missing':
        payload['output'].pop('width')
    elif defect == 'type':
        payload['beats'][0]['duration_frames'] = 'ninety'
    else:
        payload['placements'][0]['start']['ref_id'] = 'unknown-beat'
    with pytest.raises(ValueError, match='plan_validation_failed'):
        validate(payload)


def test_saved_plan_validation_stays_strict():
    from novelvideo.freezone.remix.contracts import validate_payload
    payload = native_plan()
    payload['commentary'] = 'not a model ingestion'
    with pytest.raises(ValueError):
        validate_payload('production_plan.v2', payload)


def test_spoken_draft_can_omit_estimated_duration_and_keeps_pace_intent():
    payload = native_plan()
    beat = payload['beats'][0]
    del beat['duration_frames']
    beat['speech_pace'] = 'fast'
    beat['extra_pause_seconds'] = 0.5
    beat['turns'] = [{'id': 'line', 'speaker': '甲', 'text': '你好世界'}]
    result = validate(payload)
    assert result['beats'][0]['duration_frames'] == 34
    assert result['beats'][0]['speech_pace'] == 'fast'
    assert result['beats'][0]['extra_pause_seconds'] == 0.5


def test_valid_new_draft_remains_editable_when_total_duration_needs_revision():
    payload = native_plan()
    result = production_plan.validate_generated_production_plan(
        payload, topic='中文咖啡短片',
        recipe=get_recipe_for_runtime(username='alice', recipe_id='reference-production-plan'),
        requirements={'duration_us': {'min': 10_000_000}},
    )
    assert result['beats'][0]['duration_frames'] == 90


def test_complete_draft_cycle_diagnostic_keeps_specific_issue_without_text():
    from novelvideo.llm_instrumentation import production_plan_diagnostic_sink
    from test_production_audio_sources import planned_video_dialogue

    payload = planned_video_dialogue()
    payload['placements'][0]['start'] = {'kind': 'turn', 'ref_id': 'ask', 'edge': 'start'}
    records = []
    with production_plan_diagnostic_sink(records.append):
        with pytest.raises(ValueError, match='plan_validation_failed'):
            production_plan.validate_generated_production_plan(
                payload, topic=payload['goal'],
                recipe=get_recipe_for_runtime(username='alice', recipe_id='reference-production-plan'),
            )
    failure = next(row for row in records if row['event'] == 'production_plan.validation_failed')
    assert failure['validation_issues'][0] == {
        'code': 'speech_source_timing_cycle', 'path': '/placements/0/start',
        'placement_id': 'demo-video', 'turn_id': 'ask', 'beat_id': 'demo',
    }
    assert '这个怎么用' not in json.dumps(failure, ensure_ascii=False)


def test_full_draft_diagnostics_record_only_known_paths_for_ignored_fields():
    from novelvideo.llm_instrumentation import (
        production_plan_diagnostic_context, production_plan_diagnostic_sink,
    )
    records = []
    payload = native_plan()
    payload['private-unrecognized-key'] = 'private-value'
    with production_plan_diagnostic_sink(records.append):
        with production_plan_diagnostic_context(draft_stage='full'):
            validate(payload)
    normalized = next(row for row in records
                      if row['event'] == 'production_plan.draft_normalized')
    assert normalized['draft_stage'] == 'full'
    assert normalized['validation_issues'] == [
        {'path': '/', 'code': 'extra_draft_fields_ignored'}]
    assert 'private-unrecognized-key' not in json.dumps(records)
    assert 'private-value' not in json.dumps(records)


@pytest.mark.asyncio
async def test_one_call_returns_complete_editable_plan_with_full_budget(monkeypatch):
    calls, events = [], []
    async def generate(**kwargs):
        calls.append(kwargs)
        return json.dumps(native_plan())
    async def on_stage(event):
        events.append((event.stage, event.status))
    monkeypatch.setattr(production_plan, 'generate_recipe_text', generate)
    result = await production_plan.generate_production_plan_draft(
        topic='中文咖啡短片', blueprint=None, username='alice', on_stage=on_stage)
    assert len(calls) == 1
    assert 'production_plan_stage' not in calls[0]
    assert '哪句台词或哪个原片事件触发动作' in calls[0]['node_prompt']
    assert '听到反问后切采访者' in calls[0]['node_prompt']
    assert '明确选择 speech_pace 和 extra_pause_seconds' in calls[0]['node_prompt']
    assert '发声素材自身不得直接或经 selection' in calls[0]['node_prompt']
    assert '字幕、图形及不负责这些台词发声的画面可以跟随台词' in calls[0]['node_prompt']
    assert calls[0]['plan_policy'].max_output_tokens == 16384
    assert result['authoring_stage'] == 'arranged'
    assert result['placements'][0]['id'] == 'hook-picture'
    assert events == [('full', 'started'), ('finalize', 'started'), ('full', 'completed')]


@pytest.mark.asyncio
async def test_draft_reports_local_timing_defaults_without_persisting_extra_schema_fields(monkeypatch):
    async def generate(**kwargs):
        payload = native_plan()
        payload['beats'][0].pop('duration_frames')
        payload['beats'][0]['turns'] = [{'id': 'line', 'speaker': '甲', 'text': '黑咖啡'}]
        return json.dumps(payload)
    monkeypatch.setattr(production_plan, 'generate_recipe_text', generate)
    assumptions = []
    result = await production_plan.generate_production_plan_draft(
        topic='中文咖啡短片', blueprint=None, username='alice', timing_assumptions=assumptions)
    assert assumptions == [{'beat_id': 'hook', 'duration_basis': 'local_speech_estimate',
                            'pace_basis': 'default_normal', 'pause_basis': 'default_zero'}]
    assert all(not key.startswith('_') for key in result)


@pytest.mark.asyncio
async def test_model_boundary_receives_first_middle_and_last_reference_events(monkeypatch):
    from novelvideo.llm_instrumentation import production_plan_diagnostic_sink
    calls = []
    diagnostics = []
    async def generate(**kwargs):
        calls.append(kwargs)
        return json.dumps(native_plan())
    monkeypatch.setattr(production_plan, 'generate_recipe_text', generate)
    blueprint = {'events': [
        {'event_id': 'first', 'start_us': 0, 'end_us': 2_000_000,
         'spoken_range': {'text': '开场问句'}, 'model': {'visual_observation': '采访者举起产品'}},
        {'event_id': 'middle', 'start_us': 8_000_000, 'end_us': 10_000_000,
         'spoken_range': {'text': '中段回答'}, 'model': {'visual_observation': '受访者展示杯子'}},
        {'event_id': 'ending', 'start_us': 16_000_000, 'end_us': 18_000_000,
         'spoken_range': {'text': '结尾反问'}, 'model': {'visual_observation': '看向对方手臂'}},
    ]}
    with production_plan_diagnostic_sink(diagnostics.append):
        await production_plan.generate_production_plan_draft(
            topic='替换产品但保留反转', blueprint=blueprint, username='alice', requirements={})
    assert len(calls) == 1
    events = calls[0]['structured_upstream']['source_evidence']['events']
    assert [(row['event_id'], row['spoken_range']['text']) for row in events] == [
        ('first', '开场问句'), ('middle', '中段回答'), ('ending', '结尾反问')]
    assert '看向对方手臂' in calls[0]['upstream_text']
    summary = next(row for row in diagnostics if row['event'] == 'production_plan.context_ready')
    assert summary['source_event_count'] == 3
    assert summary['passed_event_count'] == 3
    assert summary['spoken_event_count'] == 3
    assert summary['context_characters'] == len(calls[0]['upstream_text'])
    assert '开场问句' not in json.dumps(summary, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('content', ['{invalid', json.dumps({**native_plan(), 'output': {}})])
async def test_failed_single_call_does_not_retry_or_complete(monkeypatch, content):
    calls, events = [], []
    async def generate(**kwargs):
        calls.append(kwargs)
        return content
    async def on_stage(event):
        events.append(event.status)
    monkeypatch.setattr(production_plan, 'generate_recipe_text', generate)
    with pytest.raises(ValueError):
        await production_plan.generate_production_plan_draft(
            topic='中文咖啡短片', blueprint=None, username='alice', on_stage=on_stage)
    assert len(calls) == 1 and 'completed' not in events


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_content', [False, True])
async def test_full_draft_wire_uses_none_and_one_request_even_on_validation_failure(
    monkeypatch, invalid_content,
):
    import httpx
    import novelvideo.config as config

    monkeypatch.setenv('FREEZONE_PRODUCTION_PLAN_REASONING_EFFORT', 'high')
    monkeypatch.delenv('FREEZONE_PRODUCTION_PLAN_OUTPUT_TOKENS', raising=False)
    monkeypatch.delenv('FREEZONE_PRODUCTION_PLAN_STREAM_DIAGNOSTICS', raising=False)
    requests = []
    payload = native_plan()
    payload['provenance'] = {'notes': 'extra annotation'}
    if invalid_content:
        payload['output'].pop('width')

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            'id': 'chatcmpl-draft', 'object': 'chat.completion', 'created': 1,
            'model': 'brainclaw', 'choices': [{'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': json.dumps(payload)}}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
        })

    monkeypatch.setattr(config, '_newapi_text_http_client_factory',
        lambda *, timeout_seconds: lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(respond), timeout=timeout_seconds))
    model = config._newapi_text_openai_model(
        'brainclaw', api_key='test-only', base_url='https://example.test/v1',
        timeout_seconds=3, profile=None)
    monkeypatch.setattr(config, 'get_newapi_text_pydantic_model', lambda *a, **kw: model)
    call = production_plan.generate_production_plan_draft(
        topic='中文咖啡短片', blueprint=None, username='alice')
    if invalid_content:
        with pytest.raises(ValueError, match='plan_validation_failed'):
            await call
    else:
        result = await call
        assert result['authoring_stage'] == 'arranged'
        assert result['provenance'] == {}
        assert result['placements'][0]['id'] == 'hook-picture'
    assert len(requests) == 1
    assert requests[0]['reasoning_effort'] == 'none'
    assert requests[0]['max_completion_tokens'] == 16384
