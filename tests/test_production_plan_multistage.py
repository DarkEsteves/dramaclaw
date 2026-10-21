"""Independent story and arrangement tasks each make one model request."""

from __future__ import annotations

import json

import pytest


def _outline() -> dict:
    return {
        "creative_direction": "一段带声对话",
        "output": {"width": 720, "height": 1280, "fps_num": 30, "fps_den": 1},
        "beats": [{"purpose": "问答", "duration_frames": 120,
                   "turns": [{"speaker": "甲", "text": "咖啡？"},
                             {"speaker": "乙", "text": "黑咖啡。"}]}],
    }


def _arrangement() -> dict:
    return {
        "selections": [],
        "placements": [{
            "id": "performance", "kind": "visual", "role": "primary", "track_id": "visual:main",
            "source_audio": True, "description": "两人连续问答",
            "start": {"kind": "beat", "ref_id": "beat-1", "edge": "start"},
            "end": {"kind": "beat", "ref_id": "beat-1", "edge": "end"},
        }],
        "speech_bindings": {"turn-1": "performance", "turn-2": "performance"},
    }


@pytest.mark.asyncio
async def test_independent_stages_preserve_saved_edits_and_get_fresh_budget(monkeypatch):
    from novelvideo.freezone.remix import production_plan
    calls = []
    clock = [1000.0]
    async def model(**kwargs):
        calls.append(kwargs)
        return json.dumps(_outline() if kwargs['production_plan_stage'] == 'outline' else _arrangement())
    monkeypatch.setattr(production_plan, 'generate_recipe_text', model)
    monkeypatch.setattr(production_plan.time, 'monotonic', lambda: clock[0])
    saved = await production_plan.generate_production_plan_outline(topic='目标', blueprint=None, username='alice')
    assert [row['production_plan_stage'] for row in calls] == ['outline']
    assert saved['authoring_stage'] == 'outline'
    saved['beats'][0]['turns'][0]['text'] = '人工修改'
    saved['provenance'] = {'author': 'alice'}
    clock[0] += 3600
    result = await production_plan.generate_production_plan_arrangement(plan=saved, blueprint=None, username='alice')
    assert [row['production_plan_stage'] for row in calls] == ['outline', 'arrangement']
    assert '发声素材自身不得直接或经 selection' in calls[1]['node_prompt']
    assert calls[1]['plan_deadline'] - clock[0] == calls[0]['plan_deadline'] - 1000
    assert [row['plan_policy'].max_output_tokens for row in calls] == [4096, 12288]
    assert result['beats'][0]['turns'][0]['text'] == '人工修改'
    assert result['provenance']['author'] == 'alice'
    assert result['authoring_stage'] == 'arranged'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['use_confirmed_text', 'preserve_source'])
async def test_arrangement_preserves_saved_manual_timing_and_locked_dialogue(monkeypatch, mode):
    from novelvideo.freezone.remix import production_plan
    from novelvideo.freezone.remix.production_plan_draft_stages import outline_to_plan, prepare_outline
    saved = outline_to_plan('目标', prepare_outline(_outline()))
    saved['beats'][0]['turns'][0].update(timing_basis='manual', start_frame=0, end_frame=45)
    locked = [{'speaker': t['speaker'], 'text': t['text']}
              for b in saved['beats'] for t in b['turns']]
    async def model(**kwargs):
        return json.dumps(_arrangement())
    monkeypatch.setattr(production_plan, 'generate_recipe_text', model)
    result = await production_plan.generate_production_plan_arrangement(
        plan=saved, blueprint=None, username='alice', locked_turns=locked,
        requirements={'dialogue': {'mode': mode}})
    assert result['beats'][0]['turns'][0]['timing_basis'] == 'manual'


@pytest.mark.asyncio
async def test_outline_failure_does_not_submit_arrangement(monkeypatch):
    from novelvideo.freezone.remix import production_plan

    calls = []

    async def model(**kwargs):
        calls.append(kwargs["production_plan_stage"])
        return "not json"

    monkeypatch.setattr(production_plan, "generate_recipe_text", model)
    with pytest.raises(ValueError, match="json_parse_failed"):
        await production_plan.generate_production_plan_outline(
            topic="中文黑咖啡", blueprint=None, username="alice",
        )
    assert calls == ["outline"]


@pytest.mark.asyncio
async def test_arrangement_failure_does_not_retry_or_publish_partial_plan(monkeypatch):
    from novelvideo.freezone.remix import production_plan

    calls = []
    events = []
    from novelvideo.freezone.remix.production_plan_draft_stages import outline_to_plan, prepare_outline

    async def model(**kwargs):
        calls.append(kwargs["production_plan_stage"])
        return "not json"

    async def on_stage(event):
        events.append((event.stage, event.status))

    monkeypatch.setattr(production_plan, "generate_recipe_text", model)
    with pytest.raises(ValueError, match="json_parse_failed"):
        await production_plan.generate_production_plan_arrangement(
            plan=outline_to_plan("中文黑咖啡", prepare_outline(_outline())), blueprint=None, username="alice", on_stage=on_stage,
        )
    assert calls == ["arrangement"]
    assert ("finalize", "completed") not in events


@pytest.mark.asyncio
async def test_outline_schema_failure_logs_stage_and_safe_field_path(monkeypatch):
    from novelvideo.freezone.remix import production_plan
    from novelvideo.llm_instrumentation import production_plan_diagnostic_sink

    bad = _outline()
    bad["beats"][0]["duration_frames"] = 0
    bad["creative_direction"] = "PRIVATE DIALOGUE"
    calls = []
    records = []

    async def model(**kwargs):
        calls.append(kwargs["production_plan_stage"])
        return json.dumps(bad, ensure_ascii=False)

    monkeypatch.setattr(production_plan, "generate_recipe_text", model)
    with production_plan_diagnostic_sink(records.append):
        with pytest.raises(ValueError, match="production_plan_stage_validation_failed"):
            await production_plan.generate_production_plan_outline(
                topic="中文黑咖啡", blueprint=None, username="alice")
    assert calls == ["outline"]
    failure = next(record for record in records if record["event"] == "production_plan.validation_failed")
    assert failure["draft_stage"] == "outline"
    assert failure["validation_issues"] == [{"path": "/beats/0/duration_frames", "code": "exclusiveMinimum"}]
    assert "PRIVATE DIALOGUE" not in json.dumps(failure)


@pytest.mark.asyncio
async def test_cancel_after_outline_does_not_start_arrangement(monkeypatch):
    from novelvideo.freezone.remix import production_plan
    from novelvideo.task_backend.cancel import TaskCancelled

    calls = []

    async def model(**kwargs):
        calls.append(kwargs["production_plan_stage"])
        return json.dumps(_outline(), ensure_ascii=False)

    async def on_stage(event):
        if event.stage == "outline" and event.status == "completed":
            raise TaskCancelled()

    monkeypatch.setattr(production_plan, "generate_recipe_text", model)
    with pytest.raises(TaskCancelled):
        await production_plan.generate_production_plan_outline(
            topic="中文黑咖啡", blueprint=None, username="alice", on_stage=on_stage)
    assert calls == ["outline"]


@pytest.mark.asyncio
async def test_outline_does_not_gate_on_duration_without_requiring_arrangement(monkeypatch):
    from novelvideo.freezone.remix import production_plan
    calls = []
    async def model(**kwargs):
        calls.append(kwargs['production_plan_stage'])
        return json.dumps(_outline())
    monkeypatch.setattr(production_plan, 'generate_recipe_text', model)
    drafted = await production_plan.generate_production_plan_outline(
        topic='目标', blueprint=None, username='alice',
        requirements={'duration_us': {'min': 10_000_000}})
    assert calls == ['outline']
    assert drafted  # duration target is surfaced but does not gate the draft


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['outline', 'arrangement'])
@pytest.mark.parametrize('error', [TimeoutError('request_timeout'), ValueError('response_truncated')])
async def test_stage_error_never_retries_model(monkeypatch, stage, error):
    from novelvideo.freezone.remix import production_plan
    from novelvideo.freezone.remix.production_plan_draft_stages import outline_to_plan, prepare_outline
    calls = []
    async def model(**kwargs):
        calls.append(kwargs['production_plan_stage'])
        raise error
    monkeypatch.setattr(production_plan, 'generate_recipe_text', model)
    with pytest.raises(type(error), match=str(error)):
        if stage == 'outline':
            await production_plan.generate_production_plan_outline(topic='目标', blueprint=None, username='alice')
        else:
            await production_plan.generate_production_plan_arrangement(
                plan=outline_to_plan('目标', prepare_outline(_outline())), blueprint=None, username='alice')
    assert calls == [stage]
