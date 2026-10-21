from __future__ import annotations

from pathlib import Path

import pytest

from novelvideo.freezone.remix.production_plan_v2 import (
    compile_production_plan_v2,
    migrate_production_plan_v1,
)
from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.freezone.remix.operations import RemixOperationStore
from novelvideo.freezone.agent_workflows import catalog
from novelvideo.freezone.workflow_contract_generated import WORKFLOW_PLAN_SCHEMA_VERSION
from novelvideo.freezone.workflow_runs import _processing_operation_proof, _validate_action_artifact


def test_workflow_plan_draft_requires_saved_v2_artifact(tmp_path: Path) -> None:
    store = RemixArtifactStore(tmp_path, "project-a")
    draft = {
        "schema_version": 1,
        "goal": "无台词横屏短片",
        "output": {"width": 1920, "height": 1080, "fps": 24},
        "beats": [{
            "id": "opening",
            "purpose": "引出主题",
            "duration_frames": 120,
            "turns": [],
            "visuals": [{
                "id": "title",
                "role": "graphic",
                "description": "标题",
                "graphic": {"template": "title_bar", "text": "新的开始"},
            }],
            "audio": [],
        }],
    }
    old = store.create("production_plan.v1", draft, [])
    old_ref = {"artifact_id": old["id"], "revision": old["revision"]}
    assert _validate_action_artifact(
        action="generate_production_plan_draft",
        task_result={"artifact_ref": old_ref},
        history_record=None,
        project_dir=tmp_path,
        project_id="project-a",
    )[0] == "invalid_schema"

    current = store.create("production_plan.v2", migrate_production_plan_v1(draft), [])
    plan_ref = {"artifact_id": current["id"], "revision": current["revision"]}
    assert _validate_action_artifact(
        action="generate_production_plan_draft",
        task_result={"artifact_ref": plan_ref},
        history_record=None,
        project_dir=tmp_path,
        project_id="project-a",
    ) == ("valid", None, plan_ref)
    manifest = compile_production_plan_v2(current["payload"])
    manifest["source_versions"] = [plan_ref]
    rendered = store.create("composition_manifest.v2", manifest, [plan_ref])
    manifest_ref = {"artifact_id": rendered["id"], "revision": rendered["revision"]}
    assert _validate_action_artifact(
        action="compile_production_plan",
        task_result={"artifact_ref": manifest_ref},
        history_record=None,
        project_dir=tmp_path,
        project_id="project-a",
    ) == ("valid", None, manifest_ref)


def test_reference_skill_accepts_native_plan_processing_without_text_recipe() -> None:
    plan = {
        "schema_version": WORKFLOW_PLAN_SCHEMA_VERSION,
        "skill": {"id": "reference-video-remix", "version": "2.1.0"},
        "nodes": [{
            "id": "draft",
            "node_type": "scriptNode",
            "data": {
                "remixRole": "production_plan",
                "processingAction": "generate_production_plan_draft",
                "processingPayload": {"topic": "无台词横屏短片"},
            },
        }],
        "edges": [],
    }
    validated = catalog.validate_agent_workflow_plan(plan)
    assert validated["ok"] is True, validated


def test_native_plan_topic_contains_creative_request_not_generation_controls() -> None:
    from novelvideo.freezone.agent_workflows.reference_production import compile_reference_production_plan

    skill = {"id": "reference-video-remix", "version": "2.1.0", "name": "复刻"}
    intent = {"external_inputs": [{"id": "analysis", "node_id": "real-analysis", "media_kind": "reference_analysis"}]}
    inputs = {"plan_count": 1, "new_topic": "保持原片叙事，产品换成咖啡", "video_aspect_ratio": "9:16",
              "video_duration_seconds": 5, "preserve_replace": "保留镜头节奏，替换产品"}
    result = compile_reference_production_plan(intent, skill, inputs, "原片约 18 秒，按原节奏复刻")
    assert result["ok"] is True, result
    payload = result["plan"]["nodes"][0]["data"]["processingPayload"]
    assert "产品换成咖啡" in payload["topic"]
    assert "原片约 18 秒" in payload["topic"]
    assert "video_duration_seconds" not in payload["topic"]
    assert "video_aspect_ratio" not in payload["topic"]
    assert payload["output_preferences"] == {"video_aspect_ratio": "9:16", "source_clip_duration_seconds": 5}
    assert catalog.validate_agent_workflow_plan(result["plan"])["ok"] is True


def test_native_plan_keeps_default_speech_mode_out_of_the_creative_goal() -> None:
    """A resolved default is not creative input; the signed goal must stay exact."""
    from novelvideo.freezone.agent_workflows.reference_production import compile_reference_production_plan

    skill = {"id": "reference-video-remix", "version": "2.1.0", "name": "复刻",
             "input_parameters": [
                 {"id": "speech_mode", "label": "声音方式", "type": "single_select",
                  "default": "自动判断", "options": ["自动判断", "视频原声"]}]}
    intent = {"external_inputs": [{"id": "analysis", "node_id": "real-analysis",
                                   "media_kind": "reference_analysis"}]}
    goal = "保持原片叙事，产品换成咖啡"
    defaulted = compile_reference_production_plan(
        intent, skill, {"plan_count": 1, "new_topic": goal, "speech_mode": "自动判断"}, goal)
    assert defaulted["ok"] is True, defaulted
    assert defaulted["plan"]["nodes"][0]["data"]["processingPayload"]["topic"] == goal

    chosen = compile_reference_production_plan(
        intent, skill, {"plan_count": 1, "new_topic": goal, "speech_mode": "视频原声"}, goal)
    payload = chosen["plan"]["nodes"][0]["data"]["processingPayload"]
    assert payload["topic"].startswith(goal)
    assert "声音方式：视频原声" in payload["topic"]


@pytest.mark.parametrize(("extra", "expected_path"), [
    ({"planner": {"mode": "standard", "deliverable": "video"}}, "planner.deliverable"),
    ({"inputs": {"final_video_duration_seconds": 5}}, "inputs.final_video_duration_seconds"),
    ({"unexpected_goal": "replace the user goal"}, "unexpected_goal"),
    ({"assumptions": ["replace the entire plot"]}, "assumptions"),
])
def test_native_compact_intent_rejects_unknown_fields_with_copyable_contract(extra, expected_path) -> None:
    intent = {
        "schema_version": "freezone_workflow_intent.v1", "skill_id": "reference-video-remix",
        "user_goal": "按分析原片结构复刻", "inputs": {"plan_count": 1},
        "external_inputs": [{"id": "analysis", "node_id": "actual-analysis", "media_kind": "reference_analysis"}],
        **extra,
    }
    result = catalog.compile_workflow_intent(intent)
    assert result["ok"] is False
    assert result["status"] == "invalid_workflow_intent"
    assert result["field_path"] == expected_path
    assert result["errors"][0]["path"] == expected_path
    assert result["allowed_fields"]
    assert result["compact_example"]["skill_id"] == "reference-video-remix"
    assert "planner" not in result["compact_example"]


def test_native_compact_intent_lists_all_known_unsupported_fields_at_once() -> None:
    intent = {
        'schema_version': 'freezone_workflow_intent.v1',
        'skill_id': 'reference-video-remix', 'user_goal': '复刻原片',
        'assumptions': ['guess'], 'summary': 'duplicate',
        'inputs': {'plan_count': 1, 'video_shot_durations_seconds': [4, 5]},
        'external_inputs': [{'id': 'analysis', 'node_id': 'actual-analysis',
                             'media_kind': 'reference_analysis'}],
    }
    result = catalog.compile_workflow_intent(intent)
    assert [row['path'] for row in result['errors']] == [
        'assumptions', 'summary', 'inputs.video_shot_durations_seconds',
    ]
    assert result['compact_example']['external_inputs'][0]['media_kind'] == 'reference_analysis'


def test_native_plan_stage_uses_saved_video_preference_without_source_clip_duration() -> None:
    intent = {
        'schema_version': 'freezone_workflow_intent.v1',
        'skill_id': 'reference-video-remix', 'user_goal': '参考原片节奏制作',
        'inputs': {'plan_count': 1, 'video_model': 'fixture-model',
                   'video_aspect_ratio': '9:16'},
        'external_inputs': [{'id': 'analysis', 'node_id': 'saved-analysis',
                             'media_kind': 'reference_analysis'}],
    }
    result = catalog.compile_workflow_intent(intent)
    assert result['ok'] is True
    assert [node['node_type'] for node in result['plan']['nodes']] == [
        'scriptNode', 'videoComposeNode',
    ]
    assert 'source_clip_duration_seconds' not in result['plan']['nodes'][0]['data']['processingPayload'].get('output_preferences', {})


def test_saved_completed_analysis_points_to_current_production_stage_not_new_plan() -> None:
    from novelvideo.freezone.canvas_context_reader import read_saved_context

    canvas = {"revision": 1, "nodes": [{"id": "analysis", "type": "videoStoryNode",
              "data": {"rawResult": {"video_story": {"summary": "提问后反转"}}}}], "edges": []}
    result = read_saved_context(canvas, project_id="p", canvas_id="c",
                                requests=[{"type": "node_detail", "node_id": "analysis"}])
    analysis = result["responses"][0]["data"]["nodes"][0]["reference_analysis"]
    assert analysis["next_action"] == "freezone_get_production_progress"


def test_native_invalid_plan_count_names_field_and_shows_compact_intent() -> None:
    result = catalog.compile_workflow_intent({
        "skill_id": "reference-video-remix", "user_goal": "按原片改编", "inputs": {"plan_count": 0},
        "external_inputs": [{"id": "analysis", "node_id": "actual-analysis", "media_kind": "reference_analysis"}],
    })
    assert result["status"] == "invalid_workflow_intent"
    assert result["field_path"] == "inputs.plan_count"
    assert result["errors"][0]["path"] == "inputs.plan_count"
    assert "plan_count" in result["allowed_fields"]
    assert "planner" not in result["compact_example"]


@pytest.mark.parametrize(("node_type", "remix_role", "action"), [
    ("videoNode", "production_plan", "generate_production_plan_draft"),
    ("scriptNode", "", "generate_production_plan_draft"),
    ("scriptNode", "production_plan", "compile_production_plan"),
])
def test_reference_processing_action_requires_its_native_node(
    node_type: str, remix_role: str, action: str,
) -> None:
    plan = {
        "schema_version": WORKFLOW_PLAN_SCHEMA_VERSION,
        "skill": {"id": "reference-video-remix", "version": "2.1.0"},
        "nodes": [{
            "id": "wrong", "node_type": node_type,
            "data": {"remixRole": remix_role, "processingAction": action,
                     "processingPayload": {"topic": "横屏短片"}},
        }],
        "edges": [],
    }
    assert catalog.validate_agent_workflow_plan(plan)["ok"] is False


def test_compile_processing_proof_includes_explicit_plan_input(tmp_path: Path) -> None:
    store = RemixArtifactStore(tmp_path, "project-a")
    plan = store.create("production_plan.v2", migrate_production_plan_v1({
        "schema_version": 1,
        "goal": "横屏短片",
        "output": {"width": 1920, "height": 1080, "fps": 24},
        "beats": [{"id": "opening", "purpose": "开场", "duration_frames": 48,
                   "turns": [], "visuals": [], "audio": []}],
    }), [])
    plan_ref = {"artifact_id": plan["id"], "revision": plan["revision"]}
    manifest = compile_production_plan_v2(plan["payload"])
    manifest["source_versions"] = [plan_ref]
    result = store.create("composition_manifest.v2", manifest, [plan_ref])
    result_ref = {"artifact_id": result["id"], "revision": result["revision"]}
    payload = {"plan_ref": plan_ref}
    operations = RemixOperationStore(tmp_path, "project-a")
    operation = operations.start(
        idempotency_key="compile-plan-proof",
        input_refs=[{**plan_ref, "project_id": "project-a"}],
        producer_version="remix-process.v1",
        normalized_parameters={"action": "compile_production_plan", "payload": payload},
        node_revision=1, canvas_id="default", node_id="compose",
    )
    token = operations.claim_submission(operation["operation_id"], owner="test")
    assert token is not None
    operations.bind_task(operation["operation_id"], task_id="task-1", owner="test", fencing_token=token)
    operations.publish_result_unverified(operation["operation_id"], output_refs=[result_ref])

    assert _processing_operation_proof(
        item={"node_id": "compose", "node_revision": 1,
              "processing_operation_id": operation["operation_id"],
              "action": "compile_production_plan", "payload": payload},
        task_id="task-1", task_result={"artifact_ref": result_ref},
        project_dir=tmp_path, project_id="project-a",
    ) == ("valid", None, result_ref)


def test_workflow_draft_proof_accepts_only_internal_v2_selection(tmp_path: Path) -> None:
    store = RemixArtifactStore(tmp_path, "project-a")
    plan = store.create("production_plan.v2", migrate_production_plan_v1({
        "schema_version": 1, "goal": "横屏短片",
        "output": {"width": 1920, "height": 1080, "fps": 24},
        "beats": [{"id": "opening", "purpose": "开场", "duration_frames": 48,
                   "turns": [], "visuals": [], "audio": []}],
    }), [])
    result_ref = {"artifact_id": plan["id"], "revision": plan["revision"]}
    payload = {"topic": "横屏短片"}
    operations = RemixOperationStore(tmp_path, "project-a")
    operation = operations.start(
        idempotency_key="draft-v2-proof", input_refs=[], producer_version="remix-process.v1",
        normalized_parameters={"action": "generate_production_plan_draft",
                               "payload": payload},
        node_revision=1, canvas_id="default", node_id="draft",
    )
    token = operations.claim_submission(operation["operation_id"], owner="test")
    assert token is not None
    operations.bind_task(operation["operation_id"], task_id="task-1", owner="test", fencing_token=token)
    operations.publish_result_unverified(operation["operation_id"], output_refs=[result_ref])
    item = {"node_id": "draft", "node_revision": 1,
            "processing_operation_id": operation["operation_id"],
            "action": "generate_production_plan_draft", "payload": payload}
    assert _processing_operation_proof(
        item=item, task_id="task-1", task_result={"artifact_ref": result_ref},
        project_dir=tmp_path, project_id="project-a",
    ) == ("valid", None, result_ref)
    altered = {**item, "payload": {"topic": "另一部片"}}
    assert _processing_operation_proof(
        item=altered, task_id="task-1", task_result={"artifact_ref": result_ref},
        project_dir=tmp_path, project_id="project-a",
    )[0] == "invalid"
