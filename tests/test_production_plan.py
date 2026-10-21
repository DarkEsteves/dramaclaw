"""A production plan is useful before speech or media has been produced."""

from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from novelvideo.freezone.remix.contracts import validate_payload
from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.freezone.remix.production_plan import compile_production_plan
from novelvideo.freezone.remix.render import render_manifest, validate_manifest


def plan(*, beats: list[dict] | None = None) -> dict:
    return {
        "schema_version": 1,
        "goal": "把原片改成一则咖啡短片",
        "output": {"width": 1080, "height": 1920, "fps": 30},
        "beats": beats if beats is not None else [{
            "id": "hook", "purpose": "先展示问题", "duration_frames": 90,
            "turns": [],
            "visuals": [{"id": "hook-picture", "role": "primary", "description": "清晨桌面"}],
            "audio": [],
        }],
    }


def native_plan() -> dict:
    return {"schema_version": 2, "goal": "把原片改成一则咖啡短片",
            "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
            "beats": [{"id": "hook", "purpose": "先展示问题", "duration_frames": 90, "turns": []}],
            "resources": [], "placements": [{"id": "hook-picture", "kind": "visual", "role": "primary",
                "track_id": "visual:primary", "description": "清晨桌面",
                "start": {"kind": "beat", "ref_id": "hook", "edge": "start"},
            "end": {"kind": "beat", "ref_id": "hook", "edge": "end"}}], "provenance": {}}


def _draft_response(payload: dict, kwargs: dict) -> str:
    """Return the complete authored candidate from the one model request."""
    assert "production_plan_stage" not in kwargs
    return json.dumps(payload, ensure_ascii=False)


async def _generate_reviewed_plan(**kwargs):
    from novelvideo.freezone.remix import production_plan
    return await production_plan.generate_production_plan_draft(**kwargs)


@pytest.mark.asyncio
async def test_ai_draft_defaults_incomplete_subtitle_positions_without_model_retry(monkeypatch):
    from novelvideo.freezone.remix import production_plan

    payload = native_plan()
    for index in range(10):
        payload["placements"].append({
            "id": f"s{index}", "kind": "subtitle", "role": "dialogue",
            "track_id": "subtitles", "text": f"第{index}句", "subtitle_x": 20,
            "start": {"kind": "beat", "ref_id": "hook", "edge": "start"},
            "end": {"kind": "beat", "ref_id": "hook", "edge": "end"},
        })
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        return _draft_response(payload, kwargs)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    drafted = await _generate_reviewed_plan(
        topic="把原片改成一则咖啡短片", blueprint=None, username="alice")
    assert len(calls) == 1
    assert [row["text"] for row in drafted["placements"][1:]] == [f"第{i}句" for i in range(10)]
    assert all("subtitle_x" not in row and "subtitle_y" not in row
               for row in drafted["placements"][1:])


@pytest.mark.asyncio
async def test_ai_draft_normalizes_authoring_shorthand_in_single_pass(monkeypatch):
    from novelvideo.freezone.remix import production_plan
    from novelvideo.freezone.remix.production_plan_v2 import resolve_production_placement_timing

    payload = native_plan()
    payload["beats"][0]["turns"] = [
        {"id": "t1", "speaker": "甲", "text": "第一句", "start_frame": 0, "end_frame": 30},
        {"id": "t2", "speaker": "乙", "text": "第二句", "start_frame": 120, "end_frame": 150},
    ]
    payload["placements"][0]["start"] = {"kind": "beat", "ref_id": "hook", "offset_frames": 12}
    payload["placements"][0]["end"] = {"kind": "beat", "ref_id": "hook", "offset_frames": 72}
    payload["placements"].append({
        "id": "s1", "kind": "subtitle", "role": "primary", "track_id": "subtitles",
        "turn_id": "t1", "start": {"kind": "turn", "ref_id": "t1", "edge": "start"},
        "end": {"kind": "turn", "ref_id": "t1", "edge": "end"},
    })
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        return _draft_response(payload, kwargs)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    drafted = await _generate_reviewed_plan(
        topic="把原片改成一则咖啡短片", blueprint=None, username="alice",
        locked_turns=[{"speaker": "甲", "text": "第一句"}, {"speaker": "乙", "text": "第二句"}],
    )
    assert len(calls) == 1
    assert [turn["text"] for turn in drafted["beats"][0]["turns"]] == ["第一句", "第二句"]
    assert all(turn.get("start_frame") is None and turn.get("end_frame") is None
               for turn in drafted["beats"][0]["turns"])
    assert drafted["placements"][1]["role"] == "dialogue"
    _, rows = resolve_production_placement_timing(drafted)
    assert (rows[0]["start_frame"], rows[0]["end_frame"]) == (12, 72)


@pytest.mark.asyncio
async def test_ai_draft_does_not_repair_fixed_beat_overflow(monkeypatch):
    from novelvideo.freezone.remix import production_plan

    payload = native_plan()
    payload["beats"][0]["timing_mode"] = "fixed"
    payload["beats"][0]["turns"] = [
        {"id": "t1", "text": "必须固定", "start_frame": 90, "end_frame": 120},
    ]
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        return _draft_response(payload, kwargs)

    recipe = production_plan.get_recipe_for_runtime(
        username="alice", recipe_id="reference-production-plan")
    with pytest.raises(ValueError, match="plan_validation_failed"):
        production_plan.validate_generated_production_plan(
            payload, topic="把原片改成一则咖啡短片", recipe=recipe,
        )
    assert calls == []


@pytest.mark.asyncio
async def test_ai_draft_repairs_one_local_speech_source_timing_error(monkeypatch):
    from novelvideo.freezone.remix import production_plan
    from novelvideo.freezone.remix.production_plan_repair import build_plan_repair_scope

    payload = native_plan()
    payload["beats"][0]["turns"] = [{
        "id": "line", "speaker": "甲", "text": "你好",
        "speech_placement_id": "hook-picture",
    }]
    payload["placements"][0]["source_audio"] = True
    payload["placements"][0]["start"] = {"kind": "program_frame", "frame": 100}
    payload["placements"][0]["end"] = {"kind": "program_frame", "frame": 110}
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _draft_response(payload, kwargs)
        scope = build_plan_repair_scope(payload, [{
            "code": "speech_source_timing_invalid",
            "path": "/placements/0",
            "placement_id": "hook-picture",
            "beat_id": "hook",
        }])
        return json.dumps({
            "base_digest": scope["base_digest"],
            "changes": [
                {"path": "/placements/0/start",
                 "value": {"kind": "beat", "ref_id": "hook", "edge": "start"}},
                {"path": "/placements/0/end",
                 "value": {"kind": "beat", "ref_id": "hook", "edge": "end"}},
            ],
        }, ensure_ascii=False)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    drafted = await _generate_reviewed_plan(
        topic="把原片改成一则咖啡短片", blueprint=None, username="alice")

    assert len(calls) == 2
    assert isinstance(calls[1]["structured_output_contract"], dict)
    assert drafted["placements"][0]["start"] == {
        "kind": "beat", "ref_id": "hook", "edge": "start", "offset_frames": 0,
    }


@pytest.mark.asyncio
async def test_ai_draft_rejects_reversed_local_offsets_with_entity_diagnostic(monkeypatch):
    from novelvideo.freezone.remix import production_plan
    from novelvideo.llm_instrumentation import production_plan_diagnostic_sink

    payload = native_plan()
    payload["placements"][0]["start"] = {"kind": "beat", "ref_id": "hook", "offset_frames": 72}
    payload["placements"][0]["end"] = {"kind": "beat", "ref_id": "hook", "offset_frames": 12}

    async def generate(**kwargs):
        return _draft_response(payload, kwargs)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    records = []
    with production_plan_diagnostic_sink(records.append):
        with pytest.raises(ValueError, match="plan_validation_failed"):
            await _generate_reviewed_plan(
                topic="把原片改成一则咖啡短片", blueprint=None, username="alice",
            )
    failure = next(row for row in records if row["event"] == "production_plan.validation_failed")
    assert failure["validation_issues"] == [{
        "path": "/placements/0", "code": "draft_anchor_interval_invalid",
        "placement_id": "hook-picture",
    }]


@pytest.mark.asyncio
async def test_ai_draft_rejects_reversed_selection_offsets_after_defaulting(monkeypatch):
    from novelvideo.freezone.remix import production_plan
    from novelvideo.llm_instrumentation import production_plan_diagnostic_sink

    payload = native_plan()
    payload["selections"] = [{
        "id": "selected-line", "label": "错序片段",
        "start": {"kind": "beat", "ref_id": "hook", "offset_frames": 72},
        "end": {"kind": "beat", "ref_id": "hook", "offset_frames": 12},
    }]

    recipe = production_plan.get_recipe_for_runtime(
        username="alice", recipe_id="reference-production-plan")
    records = []
    with production_plan_diagnostic_sink(records.append):
        with pytest.raises(ValueError, match="plan_validation_failed"):
            production_plan.validate_generated_production_plan(
                payload, topic="咖啡短片", recipe=recipe)
    failure = next(row for row in records if row["event"] == "production_plan.validation_failed")
    assert failure["validation_issues"] == [{
        "path": "/selections/0", "code": "draft_anchor_interval_invalid",
        "selection_id": "selected-line",
    }]


@pytest.mark.asyncio
async def test_ai_draft_accepts_valid_selection_offsets_after_defaulting(monkeypatch):
    from novelvideo.freezone.remix import production_plan

    payload = native_plan()
    payload["selections"] = [{
        "id": "selected-line", "label": "有效片段",
        "start": {"kind": "beat", "ref_id": "hook", "offset_frames": 12},
        "end": {"kind": "beat", "ref_id": "hook", "offset_frames": 72},
    }]

    async def generate(**kwargs):
        return _draft_response(payload, kwargs)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    drafted = await _generate_reviewed_plan(
        topic="咖啡短片", blueprint=None, username="alice",
    )
    assert drafted["selections"][0]["start"]["edge"] == "start"
    assert drafted["selections"][0]["end"]["edge"] == "start"


@pytest.mark.asyncio
async def test_ai_draft_counts_all_schema_issues(monkeypatch):
    from jsonschema import Draft202012Validator
    from novelvideo.freezone.remix import production_plan
    from novelvideo.freezone.remix.production_plan_v2 import production_plan_draft_json_schema
    from novelvideo.llm_instrumentation import production_plan_diagnostic_sink

    payload = native_plan()
    payload["placements"][0]["subtitle_x"] = -1
    payload["placements"][0]["subtitle_y"] = -2
    expected = list(Draft202012Validator(production_plan_draft_json_schema()).iter_errors(payload))
    assert len(expected) > 1

    recipe = production_plan.get_recipe_for_runtime(
        username="alice", recipe_id="reference-production-plan")
    records = []
    with production_plan_diagnostic_sink(records.append):
        with pytest.raises(ValueError, match="plan_validation_failed"):
            production_plan.validate_generated_production_plan(
                payload, topic="咖啡短片", recipe=recipe)
    failure = next(row for row in records if row["event"] == "production_plan.validation_failed")
    assert failure["validation_issue_count"] == len(expected)
    assert len(failure["validation_issues"]) == min(20, len(expected))


@pytest.mark.asyncio
async def test_ai_draft_validation_log_keeps_response_received_and_safe_issue(monkeypatch):
    from novelvideo.freezone.remix import production_plan
    from novelvideo.llm_instrumentation import production_plan_diagnostic_sink

    payload = native_plan()
    payload["placements"].append({
        "id": "bad-subtitle", "kind": "subtitle", "role": "dialogue",
        "track_id": "subtitles", "text": "PRIVATE DIALOGUE", "subtitle_x": -1,
        "start": {"kind": "beat", "ref_id": "hook", "edge": "start"},
        "end": {"kind": "beat", "ref_id": "hook", "edge": "end"},
    })

    recipe = production_plan.get_recipe_for_runtime(
        username="alice", recipe_id="reference-production-plan")
    records = []
    with production_plan_diagnostic_sink(records.append):
        with pytest.raises(ValueError, match="plan_validation_failed"):
            production_plan.validate_generated_production_plan(
                payload, topic="咖啡短片", recipe=recipe)
    failed = next(row for row in records if row["event"] == "production_plan.validation_failed")
    assert failed["stage"] == "validating"
    assert failed["response_received"] is True
    assert failed["error_code"] == "plan_validation_failed"
    assert failed["validation_issue_count"] == 2
    assert {
        "path": "/placements/1/subtitle_x",
        "code": "anyOf",
        "placement_id": "bad-subtitle",
    } in failed["validation_issues"]
    assert "PRIVATE DIALOGUE" not in json.dumps(failed)


@pytest.mark.asyncio
async def test_draft_can_author_text_selection_before_any_media(monkeypatch):
    from novelvideo.freezone.remix import production_plan

    payload = native_plan()
    payload["beats"][0]["turns"] = [{"id": "line", "speaker": "甲", "text": "甲🙂乙"}]
    payload["selections"] = [{"id": "smile", "label": "表情后", "start": {
        "kind": "text", "ref_id": "line", "char_offset": 2, "affinity": "after"},
        "end": {"kind": "text", "ref_id": "line", "char_offset": 3,
                "affinity": "before"}}]

    async def generate(**kwargs):
        return _draft_response(payload, kwargs)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    drafted = await _generate_reviewed_plan(
        topic="甲🙂乙", blueprint=None, username="alice")
    assert drafted["selections"][0]["start"]["text_hash"] == hashlib.sha256(
        "甲🙂乙".encode()).hexdigest()


@pytest.mark.asyncio
async def test_draft_ignores_primary_shot_mismatch_without_retry(monkeypatch):
    from novelvideo.freezone.remix import production_plan
    from test_production_requirements import independent_visual_plan
    calls = []
    async def generate(**kwargs):
        calls.append(kwargs)
        return _draft_response(independent_visual_plan(1), kwargs)
    monkeypatch.setattr(production_plan, 'generate_recipe_text', generate)
    drafted = await _generate_reviewed_plan(topic='Three independent shots',
        blueprint=None, username='alice', requirements={'primary_shots': {'min': 3, 'max': 3}})
    assert drafted  # shot count is surfaced but does not gate the draft
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_draft_does_not_record_primary_shot_mismatch_as_failure(monkeypatch):
    from novelvideo.freezone.remix import production_plan
    from novelvideo.llm_instrumentation import production_plan_diagnostic_sink
    from test_production_requirements import independent_visual_plan

    async def generate(**kwargs):
        return _draft_response(independent_visual_plan(1), kwargs)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    records = []
    with production_plan_diagnostic_sink(records.append):
        drafted = await _generate_reviewed_plan(
            topic="Three independent shots", blueprint=None, username="alice",
            requirements={"primary_shots": {"min": 3, "max": 3}},
        )

    assert drafted
    assert not any(
        row["event"] == "production_plan.validation_failed" for row in records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["{invalid", ""])
async def test_unparseable_output_is_retried_only_up_to_the_call_bound(monkeypatch, content):
    from novelvideo.freezone.remix import production_plan
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        return content if len(calls) == 1 else _draft_response(native_plan(), kwargs)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    drafted = await _generate_reviewed_plan(
        topic="A short film", blueprint=None, username="alice")
    assert len(calls) == 2
    assert drafted["authoring_stage"] == "arranged"


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["{invalid", ""])
async def test_unparseable_output_that_repeats_stops_at_two_calls(monkeypatch, content):
    from novelvideo.freezone.remix import production_plan
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        return content

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    with pytest.raises(ValueError, match="json_parse_failed"):
        await _generate_reviewed_plan(
            topic="A short film", blueprint=None, username="alice")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_draft_cannot_publish_invented_runtime_resource(monkeypatch):
    from novelvideo.freezone.remix import production_plan
    from test_production_requirements import independent_visual_plan

    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        draft = independent_visual_plan(1)
        draft["placements"][0]["resource_id"] = "imaginary-media"
        return _draft_response(draft, kwargs)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    drafted = await _generate_reviewed_plan(
        topic="一个独立镜头", blueprint=None, username="alice")
    assert len(calls) == 1
    assert drafted["placements"][0].get("resource_id") is None
    assert drafted["resources"] == []


@pytest.mark.asyncio
async def test_preservation_prompt_includes_actual_timed_evidence(monkeypatch):
    from novelvideo.freezone.remix import production_plan
    calls = []
    payload = native_plan()
    payload['beats'][0]['turns'] = [{'id': 'spoken', 'text': 'Actual speech.'}]
    async def generate(**kwargs):
        calls.append(kwargs)
        return _draft_response(payload, kwargs)
    monkeypatch.setattr(production_plan, 'generate_recipe_text', generate)
    evidence = {'start_us': 0, 'end_us': 1_000_000, 'visual': 'Actual interview closeup',
        'spoken_range': {'text': 'Actual speech.'}, 'screen_subtitles': ['Different screen text']}
    await _generate_reviewed_plan(topic='Preserve the source',
        blueprint={'summary': 'Interview', 'events': [evidence]}, username='alice',
        requirements={'dialogue': {'mode': 'preserve_source'}},
        locked_turns=[{'speaker': '', 'text': 'Actual speech.'}])
    context = json.loads(calls[0]['upstream_text'])
    assert context['source_evidence']['events'] == [evidence]
    assert '勿照抄原片台词' not in calls[0]['node_prompt']
    assert 'offset_frames' in calls[0]['node_prompt']


@pytest.mark.asyncio
async def test_draft_uses_verified_source_duration_as_story_guidance(monkeypatch):
    from novelvideo.freezone.remix import production_plan

    calls = []
    async def generate(**kwargs):
        calls.append(kwargs)
        return _draft_response(native_plan(), kwargs)
    monkeypatch.setattr(production_plan, 'generate_recipe_text', generate)
    await _generate_reviewed_plan(
        topic='A film with the source rhythm', blueprint={'summary': 'An interview'},
        username='alice', source_timing={'duration_us': 18_100_000, 'event_ranges': []},
        output_preferences={'source_clip_duration_seconds': 4},
    )
    context = json.loads(calls[0]['upstream_text'])
    assert context['source_timing']['duration_us'] == 18_100_000
    assert context['output_preferences']['source_clip_duration_seconds'] == 4
    assert 'duration_us' not in context.get('requirements', {})


def test_model_draft_schema_rejects_unowned_provenance_fields():
    from jsonschema import Draft202012Validator
    from novelvideo.freezone.remix.production_plan_v2 import production_plan_draft_json_schema

    schema = production_plan_draft_json_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(native_plan())
    for field in ("remake_mode", "reference_duration_sec", "reference_shots", "notes"):
        invalid = native_plan()
        invalid["provenance"][field] = "invented"
        assert any(list(error.path) == ["provenance"] for error in
                   Draft202012Validator(schema).iter_errors(invalid))


def test_model_draft_schema_rejects_role_for_another_kind():
    from jsonschema import Draft202012Validator
    from novelvideo.freezone.remix.production_plan_v2 import production_plan_draft_json_schema

    candidate = native_plan()
    candidate["placements"][0]["role"] = "bgm"
    assert list(Draft202012Validator(production_plan_draft_json_schema()).iter_errors(candidate))


def test_model_draft_schema_rejects_ambiguous_beat_anchor():
    from jsonschema import Draft202012Validator
    from novelvideo.freezone.remix.production_plan_v2 import production_plan_draft_json_schema

    candidate = native_plan()
    candidate["placements"][0]["start"].pop("edge")
    assert list(Draft202012Validator(production_plan_draft_json_schema()).iter_errors(candidate))


@pytest.mark.parametrize("anchor", [
    {"kind": "turn", "ref_id": None, "edge": "start"},
    {"kind": "turn", "ref_id": "line", "edge": None},
    {"kind": "text", "ref_id": None, "char_offset": 0, "affinity": "after"},
    {"kind": "text", "ref_id": "line", "char_offset": None, "affinity": "after"},
    {"kind": "text", "ref_id": "line", "char_offset": 0, "affinity": None},
])
def test_model_draft_schema_rejects_null_semantic_anchor_fields(anchor):
    from jsonschema import Draft202012Validator
    from novelvideo.freezone.remix.production_plan_v2 import production_plan_draft_json_schema

    candidate = native_plan()
    candidate["beats"][0]["turns"] = [{"id": "line", "text": "台词"}]
    candidate["placements"][0]["start"] = anchor
    assert list(Draft202012Validator(production_plan_draft_json_schema()).iter_errors(candidate))


def test_draft_schema_allows_planned_speech_but_blocks_runtime_speech_evidence():
    from jsonschema import Draft202012Validator
    from novelvideo.freezone.remix.production_plan_v2 import production_plan_draft_json_schema
    from test_production_audio_sources import planned_video_dialogue

    schema = production_plan_draft_json_schema()
    candidate = planned_video_dialogue()
    Draft202012Validator(schema).validate(candidate)
    candidate["beats"][0]["turns"][0]["speech_timing"] = {"text": "invented"}
    assert list(Draft202012Validator(schema).iter_errors(candidate))
    candidate["beats"][0]["turns"][0].pop("speech_timing")
    candidate["placements"][0]["resource_id"] = "invented"
    assert list(Draft202012Validator(schema).iter_errors(candidate))


def test_draft_schema_keeps_authoring_and_excludes_measured_media():
    from jsonschema import Draft202012Validator
    from novelvideo.freezone.remix.production_plan_v2 import production_plan_draft_json_schema

    schema = production_plan_draft_json_schema()
    Draft202012Validator.check_schema(schema)
    payload = native_plan()
    payload["beats"][0]["turns"] = [
        {"id": "a", "speaker": "甲", "text": "你好🙂"},
        {"id": "b", "speaker": "乙", "text": "再见"},
    ]
    payload["selections"] = [{"id": "sel", "label": "重点", "start": {
        "kind": "text", "ref_id": "a", "char_offset": 1, "affinity": "after"}, "end": {
        "kind": "text", "ref_id": "a", "char_offset": 2, "affinity": "before"}}]
    payload["placements"].append({"id": "graphic", "kind": "graphic", "role": "effect",
        "track_id": "graphic", "graphic": {"template": "keyword", "text": "你好"},
        "start": {"kind": "beat", "ref_id": "hook", "edge": "start"},
        "end": {"kind": "beat", "ref_id": "hook", "edge": "end"}})
    Draft202012Validator(schema).validate(payload)
    assert len(json.dumps(schema, ensure_ascii=False)) <= 8000
    assert "SpeechTimingV2" not in schema.get("$defs", {})
    for field, value in (("resources", [{"id": "invented"}]),
                         ("speech_takes", [{"id": "invented"}]),
                         ("speech_cues", [{"id": "invented"}])):
        invalid = dict(payload, **{field: value})
        assert list(Draft202012Validator(schema).iter_errors(invalid))
    invalid = json.loads(json.dumps(payload))
    invalid["beats"][0]["turns"][0]["speech_timing"] = {"text": "fake"}
    assert list(Draft202012Validator(schema).iter_errors(invalid))


def test_runtime_recipe_uses_same_draft_schema_and_retains_custom_conditions(monkeypatch):
    from jsonschema import Draft202012Validator
    from novelvideo.freezone import recipe_runtime

    builtin = json.loads((Path(__file__).parents[1] /
        "src/novelvideo/freezone/agent_catalog/builtins/recipes/reference-production-plan.json").read_text())
    customized = {**builtin, "output_schema": {"type": "object", "properties": {
        "goal": {"const": "必须匹配的目标"}}}}
    monkeypatch.setattr(recipe_runtime, "list_user_agent_config_items", lambda *_args: [customized])
    recipe = recipe_runtime.get_recipe_for_runtime(username="alice", recipe_id="reference-production-plan")
    schema = recipe["output_schema"]
    assert "provenance" in recipe_runtime.append_structured_output_contract("draft", recipe)
    good = native_plan()
    good["goal"] = "必须匹配的目标"
    Draft202012Validator(schema).validate(good)
    bad = dict(good, goal="其他目标")
    assert list(Draft202012Validator(schema).iter_errors(bad))
    bad = dict(good, provenance={"remake_mode": "invented"})
    assert list(Draft202012Validator(schema).iter_errors(bad))


@pytest.mark.asyncio
async def test_authoritative_goal_is_checked_against_custom_recipe(monkeypatch):
    from novelvideo.freezone.remix import production_plan
    from novelvideo.freezone.recipe_runtime import get_recipe_for_runtime

    base = get_recipe_for_runtime(username="alice", recipe_id="reference-production-plan")
    recipe = {**base, "output_schema": {**base["output_schema"], "allOf": [
        {"properties": {"goal": {"const": "用户目标"}}}]}}
    monkeypatch.setattr(production_plan, "get_recipe_for_runtime", lambda **_kwargs: recipe)

    async def generate(**kwargs):
        return _draft_response(native_plan(), kwargs)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    result = await _generate_reviewed_plan(
        topic="用户目标", blueprint=None, username="alice")
    assert result["goal"] == "用户目标"


@pytest.mark.asyncio
async def test_conflicting_custom_goal_is_rejected_before_model(monkeypatch):
    from novelvideo.freezone.remix import production_plan
    from novelvideo.freezone.recipe_runtime import get_recipe_for_runtime

    base = get_recipe_for_runtime(username="alice", recipe_id="reference-production-plan")
    recipe = {**base, "output_schema": {**base["output_schema"], "allOf": [
        {"properties": {"goal": {"const": "另一目标"}}}]}}
    monkeypatch.setattr(production_plan, "get_recipe_for_runtime", lambda **_kwargs: recipe)

    async def unexpected(**_kwargs):
        raise AssertionError("conflicting goal must fail before a paid request")

    monkeypatch.setattr(production_plan, "generate_recipe_text", unexpected)
    with pytest.raises(ValueError, match="production_plan_recipe_contract_conflict"):
        await _generate_reviewed_plan(
            topic="用户目标", blueprint=None, username="alice")


@pytest.mark.asyncio
async def test_unsatisfiable_runtime_field_customization_fails_before_model(monkeypatch):
    from novelvideo.freezone.remix import production_plan
    from novelvideo.freezone.recipe_runtime import get_recipe_for_runtime

    base = get_recipe_for_runtime(username="alice", recipe_id="reference-production-plan")
    incompatible = {**base, "output_schema": {**base["output_schema"], "allOf": [
        {"type": "object", "properties": {"resources": {"minItems": 1}}}]}}
    monkeypatch.setattr(production_plan, "get_recipe_for_runtime", lambda **_kwargs: incompatible)

    async def unexpected(**_kwargs):
        raise AssertionError("conflicting contract must fail before a paid request")

    monkeypatch.setattr(production_plan, "generate_recipe_text", unexpected)
    with pytest.raises(ValueError, match="production_plan_recipe_contract_conflict"):
        await _generate_reviewed_plan(
            topic="新短片", blueprint=None, username="alice")


def test_silent_unbound_plan_can_be_saved_and_read(tmp_path) -> None:
    store = RemixArtifactStore(tmp_path, "demo")
    saved = store.create("production_plan.v1", plan(), [])
    assert store.read(saved["id"], saved["revision"])["payload"]["beats"][0]["turns"] == []


def test_one_beat_can_have_multiple_speakers_and_separate_audio() -> None:
    payload = plan(beats=[{
        "id": "conversation", "purpose": "提出问题并回答", "duration_frames": 150,
        "turns": [{"speaker": "甲", "text": "你每天喝什么？"}, {"speaker": "乙", "text": "黑咖啡。"}],
        "visuals": [{"id": "picture", "role": "primary", "description": "咖啡桌"}],
        "audio": [{"id": "voice", "role": "narration", "description": "独立配音"}],
    }])
    saved = validate_payload("production_plan.v1", payload)
    assert len(saved["beats"]) == 1
    assert [turn["speaker"] for turn in saved["beats"][0]["turns"]] == ["甲", "乙"]


@pytest.mark.parametrize("change", [
    lambda p: p["beats"].append({**p["beats"][0]}),
    lambda p: p["beats"][0]["visuals"].append({**p["beats"][0]["visuals"][0]}),
    lambda p: p["beats"][0]["visuals"][0].update({"media": {"node_id": "x"}}),
])
def test_plan_rejects_duplicate_ids_and_partial_media_references(change) -> None:
    payload = plan()
    change(payload)
    with pytest.raises(ValueError):
        validate_payload("production_plan.v1", payload)


@pytest.mark.asyncio
async def test_ai_draft_uses_same_plan_contract_without_forcing_dialogue(monkeypatch) -> None:
    from novelvideo.freezone.remix import production_plan

    async def generate(**kwargs):
        return _draft_response(native_plan(), kwargs)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    result = await _generate_reviewed_plan(
        topic="把产品换成黑咖啡", blueprint=None, username="alice"
    )
    assert result["beats"][0]["turns"] == []


@pytest.mark.asyncio
async def test_ai_draft_separates_reference_event_count_from_target_shot_count(monkeypatch):
    from novelvideo.freezone.remix import production_plan

    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        return _draft_response(native_plan(), kwargs)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    await _generate_reviewed_plan(
        topic="保留原片节奏但重新安排产品展示",
        blueprint={
            "summary": "先提问，再展示产品，最后回应",
            "events": [
                {"start_us": 0, "end_us": 1_000_000, "visual": "提问"},
                {"start_us": 1_000_000, "end_us": 2_000_000, "visual": "展示"},
                {"start_us": 2_000_000, "end_us": 3_000_000, "visual": "回应"},
            ],
        },
        username="alice",
    )

    prompt = calls[0]["node_prompt"]
    assert "source_evidence.events" in prompt
    assert "不是目标视频必须照搬的镜头清单" in prompt
    assert "原片分析事件（包括抽帧/关键帧窗口）" in prompt
    assert "不等于复刻视频必须生成同样数量的镜头" in prompt
    assert "镜头数量和编排由 Agent 根据新目标" in prompt
    assert "1.5–2.5 秒等原片时长只作为节奏观察" in prompt
    assert "只有 requirements.primary_shots 明确指定时才校验镜头数量" in prompt


@pytest.mark.asyncio
async def test_ai_draft_rejects_rewritten_locked_dialogue(monkeypatch) -> None:
    from novelvideo.freezone.remix import production_plan

    calls = 0
    rewritten = native_plan()
    rewritten["beats"][0]["turns"] = [{"id": "line", "speaker": "女", "text": "被模型改写的台词"}]

    async def generate(**kwargs):
        nonlocal calls
        calls += 1
        return _draft_response(rewritten, kwargs)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    with pytest.raises(ValueError, match="plan_validation_failed"):
        await _generate_reviewed_plan(
            topic="黑咖啡", blueprint=None, username="alice",
            locked_turns=[{"speaker": "女", "text": "晚上喝咖啡吗？"}],
        )
    # Locked dialogue is not repairable, so the identical candidate is not re-sent.
    assert calls == 2


@pytest.mark.asyncio
async def test_ai_draft_uses_story_structure_without_event_transcript_or_timing(monkeypatch) -> None:
    from novelvideo.freezone.remix import production_plan

    calls: list[dict] = []

    async def generate(**kwargs):
        calls.append(kwargs)
        return _draft_response(native_plan(), kwargs)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    await _generate_reviewed_plan(
        topic="把产品换成钙片",
        blueprint={
            "narrative_role": "以问答引出需求，再通过例子和反转收束",
            "preserve_replace_rules": [
                {"type": "preserve", "text": "保留提问、举例、反转的叙事顺序"},
                {"type": "replace", "text": "替换具体产品"},
            ],
            "events": [
                {"start_us": 0, "end_us": 2_000_000, "spoken_range": {"text": "原片第一句台词"}},
                {"start_us": 2_000_000, "end_us": 4_000_000, "spoken_range": {"text": "原片第二句台词"}},
            ],
            "observations": [{"start_time": "0.0", "description": "原片逐镜头细节"}],
        },
        username="alice",
    )

    upstream = json.loads(calls[0]["upstream_text"])
    assert upstream["narrative_role"] == "以问答引出需求，再通过例子和反转收束"
    assert upstream["preserve_replace_rules"] == [
        {"type": "preserve", "text": "保留提问、举例、反转的叙事顺序"},
        {"type": "replace", "text": "替换具体产品"},
    ]
    assert "原片第一句台词" not in calls[0]["upstream_text"]
    assert "原片逐镜头细节" not in calls[0]["upstream_text"]
    assert "start_us" not in calls[0]["upstream_text"]


@pytest.mark.asyncio
async def test_ai_draft_uses_analyzed_story_arc_without_mapping_its_events(monkeypatch) -> None:
    from novelvideo.freezone.remix import production_plan

    calls: list[dict] = []

    async def generate(**kwargs):
        calls.append(kwargs)
        return _draft_response(native_plan(), kwargs)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    await _generate_reviewed_plan(
        topic="改编成无对白的品牌片",
        blueprint={
            "summary": "从日常困境走向轻松解决",
            "adaptation_structure": {
                "narrative_role": "展示困境、寻找办法、获得改变",
                "reusable_beats": ["困境", "寻找", "改变"],
            },
            "events": [{"start_us": 0, "dialogue": "原片采访台词", "visual": "特写镜头"}],
        },
        username="alice",
    )

    upstream = json.loads(calls[0]["upstream_text"])
    assert upstream["summary"] == "从日常困境走向轻松解决"
    assert upstream["adaptation_structure"]["reusable_beats"] == ["困境", "寻找", "改变"]
    assert "原片采访台词" not in calls[0]["upstream_text"]


@pytest.mark.asyncio
async def test_ai_draft_ignores_malformed_optional_rules(monkeypatch) -> None:
    from novelvideo.freezone.remix import production_plan

    async def generate(**kwargs):
        return _draft_response(native_plan(), kwargs)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    result = await _generate_reviewed_plan(
        topic="制作无对白短片",
        blueprint={"user_choices": [{"type": ["preserve"], "text": "不可信的规则"}]},
        username="alice",
    )
    assert result["goal"] == "制作无对白短片"


def media(node_id: str, *, kind: str = "image", seconds: int = 3) -> dict:
    return {"node_id": node_id, "media_hash": "a" * 64,
            "media_path": f"assets/{node_id}.png", "media_kind": kind,
            "duration_us": seconds * 1_000_000, "has_audio": False}


def test_compiler_accepts_silent_visual_story() -> None:
    payload = plan()
    payload["beats"][0]["visuals"][0]["media"] = media("still")
    result = compile_production_plan(payload)
    assert validate_manifest(result) == result
    assert result["audio_events"] == []
    assert result["layers"][0]["start_frame"] == 0


def test_compiler_handles_multiple_voices_and_visual_overlay() -> None:
    payload = plan()
    beat = payload["beats"][0]
    beat["turns"] = [{"speaker": "甲", "text": "提问"}, {"speaker": "乙", "text": "回答"}]
    beat["visuals"][0]["media"] = media("base")
    beat["visuals"].append({"id": "detail", "role": "overlay", "description": "特写",
                            "start_frame": 30, "end_frame": 60, "media": media("detail")})
    beat["audio"] = [{"id": "voice", "role": "narration", "media": media("voice", kind="audio") }]
    result = compile_production_plan(payload)
    assert validate_manifest(result) == result
    assert [row["text"] for row in result["subtitle_events"]] == ["甲：提问", "乙：回答"]
    assert result["layers"][1]["z_order"] == 1


def test_compiler_identifies_missing_primary_media() -> None:
    with pytest.raises(ValueError, match="hook-picture"):
        compile_production_plan(plan())


@pytest.mark.parametrize("path", ["../outside.png", "/private/file.png", "assets//image.png", "assets\\image.png"])
def test_plan_rejects_non_project_media_paths(path: str) -> None:
    payload = plan()
    reference = media("still")
    reference["media_path"] = path
    payload["beats"][0]["visuals"][0]["media"] = reference
    with pytest.raises(ValueError, match="project-relative"):
        validate_payload("production_plan.v1", payload)


def test_compiler_supports_contiguous_primary_shots_with_native_video_speech() -> None:
    clip = media("speaker", kind="video", seconds=2)
    clip["has_audio"] = True
    payload = plan(beats=[{
        "id": "mixed", "purpose": "先静物再人物作答", "duration_frames": 120,
        "turns": [{"speaker": "甲", "text": "回答", "start_frame": 60, "end_frame": 120}],
        "visuals": [
            {"id": "opening", "role": "primary", "media": media("still"), "start_frame": 0, "end_frame": 60},
            {"id": "speaker", "role": "primary", "media": clip, "start_frame": 60, "end_frame": 120},
        ],
        "audio": [{"id": "voice", "role": "speech", "media": clip, "start_frame": 60, "end_frame": 120}],
    }])
    result = compile_production_plan(payload)
    assert validate_manifest(result) == result
    assert [row["start_frame"] for row in result["layers"]] == [0, 60]
    assert result["audio_events"][0]["media_id"] == result["layers"][1]["media_id"]


def test_compiler_rejects_speech_shorter_than_planned_span_instead_of_looping_it() -> None:
    payload = plan()
    payload["beats"][0]["visuals"][0]["media"] = media("still")
    payload["beats"][0]["audio"] = [
        {"id": "voice", "role": "narration", "media": media("voice", kind="audio", seconds=2)}
    ]
    with pytest.raises(ValueError, match="短于编排时长"):
        compile_production_plan(payload)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_saved_silent_plan_compiles_and_renders(tmp_path: Path) -> None:
    still = tmp_path / "still.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=blue:s=180x320", "-frames:v", "1", str(still)], check=True)
    payload = plan()
    payload["beats"][0]["duration_frames"] = 30
    payload["beats"][0]["visuals"][0]["media"] = {
        **media("still"), "media_hash": hashlib.sha256(still.read_bytes()).hexdigest(),
    }
    store = RemixArtifactStore(tmp_path, "demo")
    saved = store.create("production_plan.v1", payload, [])
    manifest = compile_production_plan(store.read(saved["id"], saved["revision"])["payload"])
    rendered = render_manifest(manifest, media_paths={"node:still": still}, output_path=tmp_path / "finished.mp4")
    assert rendered.is_file() and rendered.stat().st_size > 0


@pytest.mark.asyncio
async def test_graphic_draft_invalid_field_is_not_repaired(monkeypatch):
    from novelvideo.freezone.remix import production_plan
    calls=[]
    bad=native_plan()
    bad['placements'].append({'id':'title','kind':'graphic','role':'effect','track_id':'graphics','description':'标题强调',
        'start':{'kind':'beat','ref_id':'hook','edge':'start'},'end':{'kind':'beat','ref_id':'hook','edge':'end'},
        'graphic':{'template':'keyword','text':'咖啡','enter':'slide_up'}})
    async def generate(**kwargs):
        calls.append(kwargs)
        return _draft_response(bad, kwargs)
    monkeypatch.setattr(production_plan,'generate_recipe_text',generate)
    with pytest.raises(ValueError, match="plan_validation_failed"):
        await _generate_reviewed_plan(topic='咖啡',blueprint=None,username='alice')
    # One same-mode retry, never a repair of an unsupported graphic field.
    assert len(calls)==2
    assert all('structured_output_contract' not in row for row in calls)


def assert_emphasis_guidance(prompt: str) -> None:
    assert "graphic.emphasis 可选" in prompt
    assert "graphic.text 中逐字一致的一个连续子串" in prompt
    assert "语义描述、多个不连续关键词或样式指令" in prompt
    assert "每天喝黑咖啡" not in prompt
    assert "仅在不需要强调时省略 emphasis" in prompt


@pytest.mark.asyncio
async def test_native_draft_prompt_explains_literal_emphasis(monkeypatch) -> None:
    from novelvideo.freezone.remix import production_plan

    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        return _draft_response(native_plan(), kwargs)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    await _generate_reviewed_plan(topic="咖啡", blueprint=None, username="alice")
    assert_emphasis_guidance(calls[0]["node_prompt"])


def test_builtin_draft_prompt_explains_literal_emphasis() -> None:
    recipe_path = Path(__file__).parents[1] / "src/novelvideo/freezone/agent_catalog/builtins/recipes/reference-production-plan.json"
    instructions = json.loads(recipe_path.read_text())["system_prompt"]
    assert "graphic.emphasis" in instructions
    assert "连续子串" in instructions
    assert "每天喝黑咖啡" not in instructions


@pytest.mark.asyncio
async def test_draft_guides_actual_audio_source_choice(monkeypatch):
    from novelvideo.freezone.remix import production_plan

    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        return _draft_response(native_plan(), kwargs)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    await _generate_reviewed_plan(topic="咖啡", blueprint=None, username="alice")
    prompt = calls[0]["node_prompt"]
    assert "speech_placement_id" in prompt
    assert "source_audio" in prompt
    assert "独立旁白" in prompt
    assert "模型能力" in prompt


def test_builtin_recipe_requests_actual_audio_sources():
    recipe_path = Path(__file__).parents[1] / "src/novelvideo/freezone/agent_catalog/builtins/recipes/reference-production-plan.json"
    recipe = json.loads(recipe_path.read_text())
    assert "实际所需" in "".join(recipe["must_have_items"])
    assert "带声视频" in recipe["system_prompt"]
    assert "独立旁白" in recipe["system_prompt"]
    assert "模型能力" in recipe["system_prompt"]


def invalid_emphasis_plan() -> dict:
    # Synthetic candidate: live failed candidate values were not persisted.
    payload = native_plan()
    for index in (1, 2):
        beat_id = f"beat-{index}"
        beat = {"id": beat_id, "purpose": "展示咖啡", "duration_frames": 90,
                "turns": [{"id": f"turn-{index}", "speaker": "甲", "text": f"确认台词{index}"}]}
        def placement(id, kind, role, description='', graphic=None):
            row = {'id': f'{id}-{index}', 'kind': kind, 'role': role, 'track_id': kind,
                   'description': description, 'start': {'kind': 'beat', 'ref_id': beat_id, 'edge': 'start'},
                   'end': {'kind': 'beat', 'ref_id': beat_id, 'edge': 'end'}}
            if graphic is not None:
                row['graphic'] = graphic
            return row
        payload['placements'].extend([placement('primary', 'visual', 'primary', '清晨桌面'),
                                      placement('music', 'audio', 'bgm', '轻柔音乐')])
        if index == 2:
            payload['placements'].append(placement('valid-title', 'graphic', 'effect', graphic={"template": "title_bar", "text": "黑咖啡", "emphasis": "黑咖啡"}))
        payload['placements'].append(placement('invalid-title', 'graphic', 'effect', graphic={"template": "keyword", "text": "每天喝黑咖啡", "emphasis": "每天、咖啡"}))
        payload["beats"].append(beat)
    return payload


@pytest.mark.asyncio
async def test_invalid_emphasis_stops_after_one_bounded_repair(monkeypatch) -> None:
    from novelvideo.freezone.remix import production_plan

    bad = invalid_emphasis_plan()
    locked = [{"speaker": turn["speaker"], "text": turn["text"]}
              for beat in bad["beats"] for turn in beat["turns"]]
    calls = []
    async def generate(**kwargs):
        calls.append(kwargs)
        return _draft_response(bad, kwargs)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    with pytest.raises(ValueError, match="plan_validation_failed"):
        await _generate_reviewed_plan(
            topic=bad["goal"], blueprint=None, username="alice", locked_turns=locked)
    assert len(calls) == 2
    assert isinstance(calls[1]["structured_output_contract"], dict)


@pytest.mark.asyncio
async def test_invalid_emphasis_has_no_third_attempt(monkeypatch) -> None:
    from novelvideo.freezone.remix import production_plan

    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        return _draft_response(invalid_emphasis_plan(), kwargs)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    with pytest.raises(ValueError, match="plan_validation_failed"):
        await _generate_reviewed_plan(topic="咖啡", blueprint=None, username="alice")
    assert len(calls) == 2
    assert isinstance(calls[1]["structured_output_contract"], dict)


@pytest.mark.asyncio
async def test_ai_draft_repairs_graphic_emphasis_once(monkeypatch) -> None:
    from novelvideo.freezone.remix import production_plan
    from novelvideo.freezone.remix.production_plan_repair import build_plan_repair_scope

    bad = invalid_emphasis_plan()
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _draft_response(bad, kwargs)
        issues = [
            {"path": "/placements/3/graphic/emphasis", "code": "value_error",
             "message": "Value error, graphic emphasis must occur in its text"},
            {"path": "/placements/7/graphic/emphasis", "code": "value_error",
             "message": "Value error, graphic emphasis must occur in its text"},
        ]
        scope = build_plan_repair_scope(bad, issues)
        return json.dumps({
            "base_digest": scope["base_digest"],
            "changes": [
                {"path": "/placements/3/graphic/emphasis", "value": "咖啡"},
                {"path": "/placements/7/graphic/emphasis", "value": "咖啡"},
            ],
        }, ensure_ascii=False)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    drafted = await _generate_reviewed_plan(
        topic=bad["goal"], blueprint=None, username="alice")

    assert len(calls) == 2
    assert drafted["placements"][3]["graphic"]["emphasis"] == "咖啡"
    assert drafted["placements"][7]["graphic"]["emphasis"] == "咖啡"


@pytest.mark.asyncio
async def test_ai_draft_repairs_a_cross_beat_speech_source_once(monkeypatch) -> None:
    from novelvideo.freezone.remix import production_plan
    from novelvideo.freezone.remix.production_plan_repair import build_plan_repair_scope
    from novelvideo.llm_instrumentation import production_plan_diagnostic_sink

    bad = planned_dialogue_plan()
    calls = []
    records = []

    async def generate(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _draft_response(bad, kwargs)
        repair = json.loads(kwargs["upstream_text"])
        assert repair["issues"][0]["code"] == "speech_source_spans_beats"
        scope = build_plan_repair_scope(bad, repair["issues"])
        return json.dumps({
            "base_digest": scope["base_digest"],
            "changes": [{"path": path, "value": None}
                        for path in scope["allowed_paths"]
                        if path.endswith("/speech_placement_id")],
        }, ensure_ascii=False)

    monkeypatch.setattr(production_plan, "generate_recipe_text", generate)
    with production_plan_diagnostic_sink(records.append):
        drafted = await _generate_reviewed_plan(
            topic=bad["goal"], blueprint=None, username="alice")

    assert len(calls) == 2
    failed = next(row for row in records
                  if row["event"] == "production_plan.validation_failed")
    assert failed["validation_issues"][0]["code"] == "speech_source_spans_beats"
    assert failed["validation_issues"][0]["turn_id"] == "later"
    assert not any(turn.get("speech_placement_id") == "hook-picture"
                   for beat in drafted["beats"] for turn in beat["turns"])


def planned_dialogue_plan() -> dict:
    plan = native_plan()
    plan["beats"][0]["turns"] = [{"id": "line", "speaker": "甲", "text": "你好",
                                  "speech_placement_id": "hook-picture"}]
    plan["beats"].append({"id": "second", "purpose": "反转", "duration_frames": 60,
                          "turns": [{"id": "later", "speaker": "乙", "text": "再见",
                                     "speech_placement_id": "hook-picture"}]})
    return plan
