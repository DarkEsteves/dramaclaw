"""Each explicit plan stage owns its task and keeps saved human edits."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from novelvideo.api.routes.remix import prepare_registered_action
from novelvideo.freezone.remix.store import RemixArtifactStore, RemixArtifactConflict


def saved_outline(tmp_path):
    store = RemixArtifactStore(tmp_path, "p", created_by="u")
    plan = store.create("production_plan.v2", {
        "schema_version": 2, "goal": "保留结构，中文对话",
        "output": {"width": 720, "height": 1280, "fps_num": 30, "fps_den": 1},
        "beats": [{"id": "user-beat", "purpose": "问答", "duration_frames": 120,
                   "turns": [{"id": "user-turn", "speaker": "甲", "text": "修改后的台词"}]}],
    }, [])
    ref = {"artifact_id": plan["id"], "revision": plan["revision"]}
    ctx = SimpleNamespace(output_dir=tmp_path, project_id="p", requester_user_id="u",
                          requester_username="alice")
    return store, plan, ref, ctx


def test_arrangement_is_a_separate_registered_task_using_exact_saved_plan(tmp_path):
    store, plan, ref, _ = saved_outline(tmp_path)
    prepared = prepare_registered_action(store, "generate_production_plan_arrangement",
        {"plan_ref": ref}, idempotency_key="arrange", node_revision=1)
    assert prepared["task_type"] == "remix_generate_production_plan_arrangement"
    assert prepared["task_payload"]["plan_ref"] == ref
    assert prepared["input_refs"] == [ref]
    assert prepared["normalized_parameters"]["payload"] == {"plan_ref": ref}
    assert store.read_latest(plan["id"])["revision"] == 1


def test_arrangement_saves_one_new_revision_without_rewriting_human_edits(tmp_path, monkeypatch):
    from novelvideo.task_backend.runners import remix
    store, plan, ref, ctx = saved_outline(tmp_path)
    calls = []

    async def arrange(**kwargs):
        calls.append(kwargs)
        result = deepcopy(kwargs["plan"])
        result["authoring_stage"] = "arranged"
        return result

    monkeypatch.setattr(remix, "generate_production_plan_arrangement", arrange)
    result = remix.run_remix_generate_production_plan_arrangement({"payload": {"plan_ref": ref}}, ctx)
    assert result["artifact_ref"] == {"artifact_id": plan["id"], "revision": 2}
    assert len(calls) == 1
    assert calls[0]["plan"]["beats"][0]["turns"][0]["text"] == "修改后的台词"
    assert store.read_latest(plan["id"])["payload"]["beats"] == plan["payload"]["beats"]


def test_arrangement_failure_keeps_outline_and_does_not_retry(tmp_path, monkeypatch):
    from novelvideo.task_backend.runners import remix
    store, plan, ref, ctx = saved_outline(tmp_path)
    calls = []

    async def arrange(**kwargs):
        calls.append(kwargs)
        raise TimeoutError("production_plan_deadline_exceeded")

    monkeypatch.setattr(remix, "generate_production_plan_arrangement", arrange)
    with pytest.raises(TimeoutError):
        remix.run_remix_generate_production_plan_arrangement({"payload": {"plan_ref": ref}}, ctx)
    assert len(calls) == 1
    assert store.read_latest(plan["id"]) == plan


def test_arrangement_cannot_overwrite_a_concurrent_saved_edit(tmp_path, monkeypatch):
    from novelvideo.task_backend.runners import remix
    store, plan, ref, ctx = saved_outline(tmp_path)

    async def arrange(**kwargs):
        edited = deepcopy(plan["payload"])
        edited["beats"][0]["turns"][0]["text"] = "生成期间又改过"
        store.revise(plan["id"], 1, edited, [])
        return deepcopy(kwargs["plan"])

    monkeypatch.setattr(remix, "generate_production_plan_arrangement", arrange)
    with pytest.raises(RemixArtifactConflict):
        remix.run_remix_generate_production_plan_arrangement({"payload": {"plan_ref": ref}}, ctx)
    latest = store.read_latest(plan["id"])
    assert latest["revision"] == 2
    assert latest["payload"]["beats"][0]["turns"][0]["text"] == "生成期间又改过"


def test_stale_arrangement_input_is_rejected_before_generation(tmp_path):
    store, plan, ref, _ = saved_outline(tmp_path)
    store.revise(plan["id"], 1, plan["payload"], [])
    with pytest.raises(ValueError, match="changed|latest"):
        prepare_registered_action(store, "generate_production_plan_arrangement",
            {"plan_ref": ref}, idempotency_key="stale", node_revision=1)


def test_arrangement_input_role_uses_own_node_without_self_edge():
    from novelvideo.freezone.workflow_plan import compile_process_descriptors
    descriptors = compile_process_descriptors({"nodes": [{"id": "plan", "node_type": "script",
        "data": {"processingAction": "generate_production_plan_arrangement", "remixRole": "production_plan"}}],
        "edges": []}, node_revision=1)
    assert descriptors[0]["typed_inputs"] == {"production_plan": {"node_id": "plan"}}

