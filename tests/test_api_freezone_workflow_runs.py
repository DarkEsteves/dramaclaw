from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


def _valid_draft_compiled() -> dict:
    return {
        "ok": True,
        "skill_id": "video-ad",
        "plan": {
            "schema_version": "freezone_workflow_plan.v1",
            "skill": {"id": "video-ad"},
            "inputs": {},
            "nodes": [
                {
                    "id": "brief",
                    "node_type": "textAnnotationNode",
                    "stage": "input",
                    "data": {"title": "广告", "content": "商品广告"},
                }
            ],
            "edges": [],
        },
    }


@pytest.fixture()
def workflow_run_client(monkeypatch, tmp_path):
    from novelvideo.api.auth import get_api_user
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.agent_workflows import catalog

    real_list = catalog.list_user_agent_config_items

    def catalog_items(username, kind):
        items = real_list(username, kind)
        if kind == "skills":
            items = [item for item in items if item.get("id") != "video-ad"]
            items.append(
                {
                    "id": "video-ad",
                    "version": "1.0.0",
                    "enabled": True,
                    "triggers": {"node_scopes": ["textGeneration"]},
                }
            )
        return items

    monkeypatch.setattr(catalog, "list_user_agent_config_items", catalog_items)

    ctx = SimpleNamespace(
        project_id="proj_demo",
        owner_username="alice",
        project_name="demo",
        output_dir=str(tmp_path),
        state_dir=str(tmp_path),
        runtime_dir=str(tmp_path / "_runtime"),
        is_home_node=True,
        requester_user_id="u-alice",
        effective_role="editor",
        enqueued_tasks=[],
    )

    class FakeTaskBackend:
        async def enqueue_project_task(self, _ctx, **kwargs):
            ctx.enqueued_tasks.append(kwargs)
            task_id = f"workflow-task-{len(ctx.enqueued_tasks)}"
            return SimpleNamespace(task_state=SimpleNamespace(task_id=task_id))

    task_backend = FakeTaskBackend()

    async def fake_resolve(
        project: str,
        user: dict,
        *,
        required_role: str = "editor",
        require_home_node: bool = True,
    ):
        del require_home_node
        return ctx, "alice", "demo", tmp_path, str(tmp_path)

    monkeypatch.setattr(freezone, "_resolve_freezone_project", fake_resolve)
    monkeypatch.setattr(freezone, "get_task_backend", lambda: task_backend)
    app = FastAPI()
    app.include_router(freezone.router, prefix="/api/v1")
    app.dependency_overrides[get_api_user] = lambda: {
        "id": "u-alice",
        "username": "alice",
    }
    client = TestClient(app)
    client.state_dir = tmp_path
    client.enqueued_tasks = ctx.enqueued_tasks
    return client


def _recipe_generation_session_payload(
    client: TestClient,
    *,
    session_id: str,
    reused_recipe_id: str = "outdoor-stage-duel-storyboard",
    operation_session_id: str | None = None,
    operation_kind: str = "recipe_generate",
    operation_artifact_id: str | None = None,
) -> tuple[dict, dict]:
    generated_recipe_id = f"generated-{session_id}"
    manifest = {
        "generation_session_id": session_id,
        "generation_attempt_id": f"attempt-{session_id}",
        "artifact_mode": "recipe_only",
        "skill": {"generate": False, "id": ""},
        "recipes": [
            {
                "generate": True,
                "id": generated_recipe_id,
                "generation_attempt_id": f"attempt-{session_id}",
                "output_index": 0,
            },
            {"reuse": True, "id": reused_recipe_id},
        ],
    }
    operation_response = client.post(
        "/api/v1/projects/proj_demo/freezone/agent-product-operations",
        json={
            "product_kind": operation_kind,
            "generation_session_id": operation_session_id or session_id,
            "canvas_id": "default",
            "artifact_id": operation_artifact_id or generated_recipe_id,
            "normalized_inputs_hash": f"inputs-{session_id}",
            "metadata": {"manifest": manifest, "recipe_index": 0},
        },
    )
    assert operation_response.status_code == 200
    operation = operation_response.json()["data"]
    draft = {
        "project_id": "proj_demo",
        "canvas_id": "default",
        "expected_recipe_count": 1,
        "outline": {
            "expected_recipe_count": 1,
            "stages": [
                {
                    "id": generated_recipe_id,
                    "recipe_id": generated_recipe_id,
                    "reuse": "new",
                },
                {
                    "id": reused_recipe_id,
                    "recipe_id": reused_recipe_id,
                    "reuse": "existing",
                },
            ],
        },
        "manifest": manifest,
        "operations": {"recipes": {"0": operation}},
        "recipes": {},
    }
    return manifest, draft


def test_workflow_run_api_lifecycle(workflow_run_client: TestClient) -> None:
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    created_response = workflow_run_client.post(
        base,
        json={"actions": [{"node_id": "image-1", "action": "save"}]},
    )
    assert created_response.status_code == 200
    created = created_response.json()["data"]

    patched_response = workflow_run_client.patch(
        f"{base}/{created['run_id']}",
        json={
            "status": "completed",
            "action_updates": [
                {
                    "node_id": "image-1",
                    "action": "save",
                    "status": "completed",
                    "phase": "syncing_result",
                }
            ],
        },
    )
    assert patched_response.status_code == 200
    assert patched_response.json()["data"]["status"] == "completed"
    assert patched_response.json()["data"]["actions"][0]["phase"] == "syncing_result"

    assert (
        workflow_run_client.get(f"{base}/{created['run_id']}").json()["data"]["run_id"]
        == created["run_id"]
    )
    assert (
        workflow_run_client.get(base).json()["data"]["runs"][0]["run_id"]
        == created["run_id"]
    )


def test_approved_graph_alias_admits_one_host_owned_native_run(
    workflow_run_client: TestClient, monkeypatch,
) -> None:
    from novelvideo.api.routes import chat, remix
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.canvas_command_bridge import canonical_bridge_key
    from novelvideo.freezone.remix import production_processing_settlement, reference_director

    bridge_key = "bridge-native.executor.owner"
    assert canonical_bridge_key(bridge_key) == "bridge-native"
    binding = {"username": "alice", "project_id": "proj_demo", "canvas_id": "default",
               "agent_id": "main", "turn_id": "turn-1"}
    approved = {"kind": "canvas_command", "transport_status": "delivered",
                "producer_binding": binding, "delivery_binding": binding,
                "project_id": "proj_demo", "canvas_id": "default", "execution_token": "owner",
                "commands": [
                    {"type": "create_node", "client_id": "planned", "data": {
                        "workflowInstanceId": "workflow_draft_1", "workflowPlanNodeId": "production_plan_1"}},
                    {"type": "create_node", "client_id": "planned-b", "data": {
                        "workflowInstanceId": "workflow_draft_1", "workflowPlanNodeId": "production_plan_2"}},
                    {"type": "run_workflow", "node_ids": ["planned", "planned-b"]},
                ]}
    monkeypatch.setattr(chat, "_canvas_bridge_dir", lambda *_args, **_kwargs: workflow_run_client.state_dir)
    from novelvideo.freezone import canvas_command_bridge
    monkeypatch.setattr(canvas_command_bridge, "read_bridge_message", lambda *_args, **_kwargs: approved)
    monkeypatch.setattr(canvas_store, "read_canvas", lambda *_args, **_kwargs: {
        "project_id": "proj_demo", "canvas_id": "default", "revision": 1,
        "nodes": [{"id": "actual-plan", "data": {"workflowInstanceId": "workflow_draft_1",
                                                    "workflowPlanNodeId": "production_plan_1"}},
                  {"id": "actual-plan-b", "data": {"workflowInstanceId": "workflow_draft_1",
                                                      "workflowPlanNodeId": "production_plan_2"}}],
    })
    monkeypatch.setattr(reference_director, "list_director_artifacts", lambda *_args, **_kwargs: {"commissions": []})
    monkeypatch.setattr(production_processing_settlement, "native_action_automation_control",
                        lambda *_args, **_kwargs: None)
    def prepared(_store, action, payload, **_kwargs):
        return {"input_refs": [], "producer_version": "test", "normalized_parameters": {
            "action": action, "payload": payload}, "input_media_hashes": [], "scope": "",
            "task_type": "freezone_production_plan", "queue_kind": "default", "task_payload": {}}
    monkeypatch.setattr(remix, "prepare_registered_action", prepared)
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    request = {"bridge_key": bridge_key, "idempotency_key": "canvas-run:approved-1",
               "runner_id": "canvas-runner:browser", "metadata": {"native_completion_owner": "browser"},
               "actions": [{"node_id": node_id, "action": "generate_production_plan_draft",
                            "node_revision": 1, "payload": {"topic": "coffee"}}
                           for node_id in ("actual-plan", "actual-plan-b")]}
    first = workflow_run_client.post(base, json=request)
    assert first.status_code == 200, first.text
    run = first.json()["data"]
    assert run["acceptance"] == {"workflow_run_id": run["run_id"], "completion_owner": "host"}
    assert run["runner_id"] is None
    assert run["actions"][0]["task_id"] == "workflow-task-1"
    assert run["actions"][1]["task_id"] == "workflow-task-2"
    assert run["metadata"]["native_approved_actions"] == [
        {"node_id": node_id, "action": "generate_production_plan_draft"}
        for node_id in ("actual-plan", "actual-plan-b")]
    assert run["metadata"]["canvas_bridge_key"] == "bridge-native"
    approved["execution_token"] = "reclaimed-owner"
    second = workflow_run_client.post(base, json={
        **request, "bridge_key": "bridge-native.executor.reclaimed-owner",
    })
    assert second.status_code == 200, second.text
    assert second.json()["data"]["run_id"] == run["run_id"]
    assert len(workflow_run_client.enqueued_tasks) == 2


def test_approved_bridge_owner_rotation_recovers_ordinary_run(
    workflow_run_client: TestClient, monkeypatch,
) -> None:
    from novelvideo.api.routes import chat
    from novelvideo.freezone import canvas_command_bridge
    from novelvideo.freezone.workflow_runs import workflow_runs_db_path

    binding = {"username": "alice", "project_id": "proj_demo", "canvas_id": "default",
               "agent_id": "main", "turn_id": "turn-ordinary"}
    approved = {"kind": "canvas_command", "transport_status": "delivered",
                "producer_binding": binding, "delivery_binding": binding,
                "project_id": "proj_demo", "canvas_id": "default", "execution_token": "first",
                "commands": [{"type": "run_node_action", "node_id": "note", "action": "save"}]}
    monkeypatch.setattr(chat, "_canvas_bridge_dir", lambda *_args, **_kwargs: workflow_run_client.state_dir)
    monkeypatch.setattr(canvas_command_bridge, "read_bridge_message", lambda *_args, **_kwargs: approved)
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    request = {"bridge_key": "ordinary-bridge.executor.first",
               "idempotency_key": "canvas-run:ordinary-bridge", "runner_id": "canvas-runner:first",
               "actions": [{"node_id": "note", "action": "save"}]}
    first = workflow_run_client.post(base, json=request)
    assert first.status_code == 200, first.text
    approved["execution_token"] = "second"
    second = workflow_run_client.post(base, json={
        **request, "bridge_key": "ordinary-bridge.executor.second",
        "runner_id": "canvas-runner:second",
    })
    assert second.status_code == 409, second.text
    with sqlite3.connect(workflow_runs_db_path(workflow_run_client.state_dir)) as conn:
        conn.execute("UPDATE workflow_runs SET lease_expires_at=? WHERE run_id=?",
                     ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                      first.json()["data"]["run_id"]))
    second = workflow_run_client.post(base, json={
        **request, "bridge_key": "ordinary-bridge.executor.second",
        "runner_id": "canvas-runner:second",
    })
    assert second.status_code == 200, second.text
    assert second.json()["data"]["run_id"] == first.json()["data"]["run_id"]
    assert second.json()["data"]["runner_id"] == "canvas-runner:second"
    assert workflow_run_client.patch(f'{base}/{first.json()["data"]["run_id"]}', json={
        "status": "running", "runner_id": "canvas-runner:first",
    }).status_code == 409


def test_recipe_media_claim_reports_interrupted_run_before_enqueue(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.agent_product_operations import (
        finish_agent_product_operation,
        read_agent_product_operation,
    )
    from novelvideo.freezone.workflow_runs import (
        claim_workflow_media_action,
        interrupt_stale_workflow_runs,
        read_workflow_run,
    )

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    created = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
        json={
            "actions": [
                {
                    "node_id": "video-1",
                    "action": "generate_video",
                    "recipe_id": "product-video",
                    "recipe_version": "1.0.0",
                    "generation_attempt_id": "attempt-video",
                }
            ],
            "runner_id": "runner-a",
        },
    ).json()["data"]
    operation_id = created["actions"][0]["product_operation_id"]
    operation = read_agent_product_operation(
        project_dir=workflow_run_client.state_dir,
        operation_id=operation_id,
    )
    finish_agent_product_operation(
        project_dir=workflow_run_client.state_dir,
        operation_id=operation_id,
        outcome="delivered",
        expected_task_id=operation["task_id"],
        result_ref={
            "kind": "recipe_compile_result",
            "id": operation_id,
            "reason": "timeout_fallback",
            "content": "compiled prompt",
        },
        server_recipe_compile=True,
    )
    assert interrupt_stale_workflow_runs(
        project_dir=workflow_run_client.state_dir,
        canvas_id="default",
        stale_after_seconds=60,
        now=datetime.now(timezone.utc) + timedelta(seconds=181),
    ) == [created["run_id"]]

    with pytest.raises(ValueError, match="workflow run is interrupted"):
        claim_workflow_media_action(
            project_dir=workflow_run_client.state_dir,
            project_id="proj_demo",
            canvas_id="default",
            node_id="video-1",
            operation_id=operation_id,
            attempt_id="attempt-video",
            task_type="freezone_video_gen",
            fingerprint="a" * 64,
        )
    run = read_workflow_run(
        project_dir=workflow_run_client.state_dir,
        canvas_id="default",
        run_id=created["run_id"],
    )
    assert run is not None
    assert run["actions"][0].get("job_id") in (None, "")


def test_recipe_media_claim_replay_after_interrupt_reuses_admitted_job(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.agent_product_operations import (
        finish_agent_product_operation,
        read_agent_product_operation,
    )
    from novelvideo.freezone.workflow_runs import (
        claim_workflow_media_action,
        interrupt_stale_workflow_runs,
    )

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    created = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
        json={
            "actions": [
                {
                    "node_id": "video-1",
                    "action": "generate_video",
                    "recipe_id": "product-video",
                    "recipe_version": "1.0.0",
                    "generation_attempt_id": "attempt-video",
                }
            ],
            "runner_id": "runner-a",
        },
    ).json()["data"]
    operation_id = created["actions"][0]["product_operation_id"]
    operation = read_agent_product_operation(
        project_dir=workflow_run_client.state_dir,
        operation_id=operation_id,
    )
    finish_agent_product_operation(
        project_dir=workflow_run_client.state_dir,
        operation_id=operation_id,
        outcome="delivered",
        expected_task_id=operation["task_id"],
        result_ref={
            "kind": "recipe_compile_result",
            "id": operation_id,
            "reason": "timeout_fallback",
            "content": "compiled prompt",
        },
        server_recipe_compile=True,
    )
    request = dict(
        project_dir=workflow_run_client.state_dir,
        project_id="proj_demo",
        canvas_id="default",
        node_id="video-1",
        operation_id=operation_id,
        attempt_id="attempt-video",
        task_type="freezone_video_gen",
        fingerprint="a" * 64,
    )
    admitted = claim_workflow_media_action(**request)
    assert admitted["created"] is True
    assert interrupt_stale_workflow_runs(
        project_dir=workflow_run_client.state_dir,
        canvas_id="default",
        stale_after_seconds=60,
        now=datetime.now(timezone.utc) + timedelta(seconds=181),
    ) == [created["run_id"]]

    # A request admitted before the lease expired replays to the same job
    # instead of enqueueing (and charging for) a second media task (issue #730).
    replayed = claim_workflow_media_action(**request)
    assert replayed["created"] is False
    assert replayed["job_id"] == admitted["job_id"]
    with pytest.raises(ValueError, match="already claimed"):
        claim_workflow_media_action(**{**request, "fingerprint": "b" * 64})


def test_direct_voice_recipe_claim_requires_bound_audio_action_and_settles_from_media(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.agent_product_operations import (
        finish_agent_product_operation,
        read_agent_product_operation,
    )
    from novelvideo.freezone.workflow_runs import claim_workflow_media_action

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    created = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
        json={
            "actions": [
                {
                    "node_id": "voice-1",
                    "action": "generate_audio",
                    "recipe_id": "drama-shot-voice",
                    "recipe_version": "1.0.0",
                    "generation_attempt_id": "attempt-voice",
                }
            ],
            "runner_id": "runner-voice",
        },
    ).json()["data"]
    operation_id = created["actions"][0]["product_operation_id"]
    request = {
        "project_dir": workflow_run_client.state_dir,
        "project_id": "proj_demo",
        "canvas_id": "default",
        "node_id": "voice-1",
        "operation_id": operation_id,
        "attempt_id": "attempt-voice",
        "task_type": "freezone_audio_speech",
        "fingerprint": "a" * 64,
    }
    with pytest.raises(ValueError, match="does not match"):
        claim_workflow_media_action(**{**request, "node_id": "other-node"})
    with pytest.raises(ValueError, match="does not match"):
        claim_workflow_media_action(**{**request, "attempt_id": "other-attempt"})
    with pytest.raises(ValueError, match="Recipe compilation is not ready"):
        claim_workflow_media_action(**{**request, "task_type": "freezone_audio_eleven_music"})

    admitted = claim_workflow_media_action(**request)
    assert admitted["created"] is True
    replayed = claim_workflow_media_action(**request)
    assert replayed["created"] is False
    assert replayed["job_id"] == admitted["job_id"]
    with pytest.raises(ValueError, match="already claimed"):
        claim_workflow_media_action(**{**request, "fingerprint": "b" * 64})

    operation = read_agent_product_operation(
        project_dir=workflow_run_client.state_dir, operation_id=operation_id
    )
    receipt = {
        "kind": "recipe_result",
        "id": admitted["job_id"],
        "workflow_run_id": created["run_id"],
        "node_id": "voice-1",
        "recipe_id": "drama-shot-voice",
    }
    forged = workflow_run_client.post(
        f"/api/v1/projects/proj_demo/freezone/agent-product-operations/{operation_id}/finish",
        json={
            "task_id": operation["task_id"],
            "outcome": "delivered",
            "result_ref": receipt,
            "server_recipe_direct_audio": True,
        },
    )
    assert forged.status_code == 400
    with pytest.raises(ValueError, match="trusted model execution evidence"):
        finish_agent_product_operation(
            project_dir=workflow_run_client.state_dir,
            operation_id=operation_id,
            outcome="delivered",
            expected_task_id=operation["task_id"],
            result_ref={**receipt, "id": "unclaimed-job"},
            server_recipe_direct_audio=True,
        )
    with pytest.raises(ValueError, match="trusted model execution evidence"):
        finish_agent_product_operation(
            project_dir=workflow_run_client.state_dir,
            operation_id=operation_id,
            outcome="delivered",
            expected_task_id=operation["task_id"],
            result_ref=receipt,
        )
    finished = finish_agent_product_operation(
        project_dir=workflow_run_client.state_dir,
        operation_id=operation_id,
        outcome="delivered",
        expected_task_id=operation["task_id"],
        result_ref=receipt,
        server_recipe_direct_audio=True,
    )
    assert finished["status"] == "delivered"
    assert finished["model_evidence"] == {}
    assert finished["result_ref"] == receipt


def test_other_audio_recipe_cannot_use_direct_voice_admission(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.workflow_runs import claim_workflow_media_action

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    created = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
        json={
            "actions": [
                {
                    "node_id": "voice-1",
                    "action": "generate_audio",
                    "recipe_id": "general-audio",
                    "recipe_version": "1.0.0",
                    "generation_attempt_id": "attempt-other-voice",
                }
            ]
        },
    ).json()["data"]
    with pytest.raises(ValueError, match="Recipe compilation is not ready"):
        claim_workflow_media_action(
            project_dir=workflow_run_client.state_dir,
            project_id="proj_demo",
            canvas_id="default",
            node_id="voice-1",
            operation_id=created["actions"][0]["product_operation_id"],
            attempt_id="attempt-other-voice",
            task_type="freezone_audio_speech",
            fingerprint="a" * 64,
        )


@pytest.mark.parametrize(
    "compile_mode",
    ["memory_cache", "persistent_cache", "deterministic", "timeout_fallback", "model"],
)
def test_recipe_media_claim_accepts_trusted_compile_and_rejects_replay_change(
    workflow_run_client: TestClient, monkeypatch, compile_mode: str
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.agent_product_operations import (
        bind_agent_product_model_execution,
        finish_agent_product_operation,
        read_agent_product_operation,
    )
    from novelvideo.freezone.workflow_runs import (
        claim_workflow_media_action,
        update_workflow_run,
    )

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    created = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
        json={
            "actions": [
                {
                    "node_id": "image-1",
                    "action": "generate_image",
                    "recipe_id": "product-image",
                    "recipe_version": "1.0.0",
                    "generation_attempt_id": "attempt-image",
                }
            ]
        },
    ).json()["data"]
    operation_id = created["actions"][0]["product_operation_id"]
    operation = read_agent_product_operation(
        project_dir=workflow_run_client.state_dir,
        operation_id=operation_id,
    )
    if compile_mode == "model":
        bind_agent_product_model_execution(
            project_dir=workflow_run_client.state_dir,
            operation_id=operation_id,
            model_call_id="recipe-compiler:image",
            executed_at=1.0,
            source="server_recipe_compiler",
            compile_mode="model",
        )
    else:
        finish_agent_product_operation(
            project_dir=workflow_run_client.state_dir,
            operation_id=operation_id,
            outcome="delivered",
            expected_task_id=operation["task_id"],
            result_ref={
                "kind": "recipe_compile_result",
                "id": operation_id,
                "reason": compile_mode,
                "content": "compiled prompt",
            },
            server_recipe_compile=True,
        )
    request = dict(
        project_dir=workflow_run_client.state_dir,
        project_id="proj_demo",
        canvas_id="default",
        node_id="image-1",
        operation_id=operation_id,
        attempt_id="attempt-image",
        task_type="freezone_gen",
        fingerprint="a" * 64,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, again = list(
            pool.map(lambda _index: claim_workflow_media_action(**request), range(2))
        )
    first, again = sorted((first, again), key=lambda claim: not claim["created"])
    assert first["created"] is True
    assert again["created"] is False
    assert first["job_id"] == again["job_id"]
    if compile_mode == "model":
        finish_agent_product_operation(
            project_dir=workflow_run_client.state_dir,
            operation_id=operation_id,
            outcome="delivered",
            expected_task_id=operation["task_id"],
            result_ref={
                "kind": "recipe_result",
                "id": first["job_id"],
                "workflow_run_id": created["run_id"],
                "node_id": "image-1",
            },
        )
        assert claim_workflow_media_action(**request)["job_id"] == first["job_id"]
    with pytest.raises(ValueError, match="already claimed"):
        claim_workflow_media_action(**{**request, "fingerprint": "b" * 64})
    with pytest.raises(ValueError, match="does not match"):
        claim_workflow_media_action(**{**request, "attempt_id": "another"})
    with pytest.raises(ValueError, match="cannot change"):
        update_workflow_run(
            project_dir=workflow_run_client.state_dir,
            canvas_id="default",
            run_id=created["run_id"],
            action_updates=[
                {
                    "node_id": "image-1",
                    "action": "generate_image",
                    "status": "running",
                    "job_id": "attacker-job",
                }
            ],
        )


@pytest.mark.parametrize(
    "cancel_during_enqueue,fail_cancel_once",
    [(False, False), (True, False), (True, True)],
)
@pytest.mark.asyncio
async def test_recipe_media_enqueue_replays_terminal_task_without_rebilling(
    workflow_run_client: TestClient,
    monkeypatch,
    cancel_during_enqueue: bool,
    fail_cancel_once: bool,
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.agent_product_operations import (
        finish_agent_product_operation,
        read_agent_product_operation,
    )

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    created = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
        json={
            "actions": [
                {
                    "node_id": "image-1",
                    "action": "generate_image",
                    "recipe_id": "product-image",
                    "recipe_version": "1.0.0",
                    "generation_attempt_id": "attempt-image",
                }
            ]
        },
    ).json()["data"]
    operation_id = created["actions"][0]["product_operation_id"]
    operation = read_agent_product_operation(
        project_dir=workflow_run_client.state_dir,
        operation_id=operation_id,
    )
    finish_agent_product_operation(
        project_dir=workflow_run_client.state_dir,
        operation_id=operation_id,
        outcome="delivered",
        expected_task_id=operation["task_id"],
        result_ref={
            "kind": "recipe_compile_result",
            "id": operation_id,
            "reason": "memory_cache",
            "content": "compiled prompt",
        },
        server_recipe_compile=True,
    )
    tasks = {}
    enqueue_count = 0
    cancelled_tasks = []
    cancel_attempts = 0

    class TaskManager:
        def get_task_for_project(self, _ctx, _task_type, _episode, *, scope):
            return tasks.get(scope)

    class TaskBackend:
        async def enqueue_project_task(self, _ctx, *, scope, **_kwargs):
            nonlocal enqueue_count
            enqueue_count += 1
            state = SimpleNamespace(
                task_id="media-task-1",
                status="running" if cancel_during_enqueue else "completed",
                metadata={"backend": "celery", "queue_kind": "default", "queue": "default"},
            )
            tasks[scope] = state
            if cancel_during_enqueue:
                from novelvideo.freezone.workflow_runs import update_workflow_run

                update_workflow_run(
                    project_dir=workflow_run_client.state_dir,
                    canvas_id="default",
                    run_id=created["run_id"],
                    status="cancelled",
                )
            return SimpleNamespace(task_state=state, backend="celery", queue="default")

        async def cancel_project_task(self, _ctx, task):
            nonlocal cancel_attempts
            cancel_attempts += 1
            if fail_cancel_once and cancel_attempts == 1:
                raise RuntimeError("temporary cancellation failure")
            cancelled_tasks.append(task.task_id)
            task.status = "cancelled"

    monkeypatch.setattr(freezone, "get_task_manager", lambda: TaskManager())
    monkeypatch.setattr(freezone, "get_task_backend", lambda: TaskBackend())
    ctx = SimpleNamespace(
        project_id="proj_demo", state_dir=str(workflow_run_client.state_dir)
    )
    payload = {
        "canvas_id": "default",
        "node_id": "image-1",
        "product_operation_id": operation_id,
        "generation_attempt_id": "attempt-image",
        "prompt": "a castle",
    }

    async def enqueue(data):
        return await freezone._enqueue_claimed_workflow_media(
            ctx=ctx,
            project_dir=workflow_run_client.state_dir,
            task_type="freezone_gen",
            queue_kind="default",
            payload=data.copy(),
            job_id="discarded-client-job",
        )

    if fail_cancel_once:
        with pytest.raises(HTTPException) as cancellation_error:
            await enqueue(payload)
        assert cancellation_error.value.status_code == 503
        first = await enqueue(payload)
    else:
        first = await enqueue(payload)
    again = await enqueue(payload)
    for field in ("job_id", "task_id", "queue", "backend"):
        assert first["data"][field] == again["data"][field]
    assert enqueue_count == 1
    assert cancelled_tasks == (["media-task-1"] if cancel_during_enqueue else [])
    assert cancel_attempts == (2 if fail_cancel_once else int(cancel_during_enqueue))
    with pytest.raises(HTTPException) as exc:
        await enqueue({**payload, "prompt": "another castle"})
    assert exc.value.status_code == 409


def _cached_recipe_media_operation(
    client: TestClient, monkeypatch, *, runner_id: str = ""
) -> tuple[dict, str]:
    """Create a workflow image action whose Recipe was settled by a cache hit."""
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.agent_product_operations import (
        finish_agent_product_operation,
        read_agent_product_operation,
    )

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    created = client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
        json={"actions": [{
            "node_id": "image-1", "action": "generate_image",
            "recipe_id": "product-image", "recipe_version": "1.0.0",
            "generation_attempt_id": "attempt-image",
        }], **({"runner_id": runner_id} if runner_id else {})},
    ).json()["data"]
    operation_id = created["actions"][0]["product_operation_id"]
    operation = read_agent_product_operation(
        project_dir=client.state_dir, operation_id=operation_id
    )
    finish_agent_product_operation(
        project_dir=client.state_dir,
        operation_id=operation_id,
        outcome="delivered",
        expected_task_id=operation["task_id"],
        result_ref={
            "kind": "recipe_compile_result",
            "id": operation_id,
            "reason": "persistent_cache",
            "content": "cached castle prompt",
        },
        server_recipe_compile=True,
    )
    return created, operation_id


@pytest.mark.parametrize("endpoint", ["compile", "compile-batch"])
def test_recipe_compile_replays_cached_receipt_for_media_retry(
    workflow_run_client: TestClient, monkeypatch, endpoint: str
) -> None:
    """Issue #681: a retry after a transient media failure recompiles the same operation."""
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.agent_product_operations import (
        read_agent_product_operation,
    )

    _created, operation_id = _cached_recipe_media_operation(
        workflow_run_client, monkeypatch
    )
    stored = read_agent_product_operation(
        project_dir=workflow_run_client.state_dir, operation_id=operation_id
    )

    async def forbidden_compile(*_args, **_kwargs):
        raise AssertionError("a settled compile receipt must be replayed, not recompiled")

    monkeypatch.setattr(freezone, "compile_recipe_prompt_result", forbidden_compile)
    monkeypatch.setattr(freezone, "compile_recipe_prompt_batch", forbidden_compile)

    def post(recipe_id: str):
        item = {
            "project_id": "proj_demo",
            "product_operation_id": operation_id,
            "recipe_id": recipe_id,
            "recipe_pipeline": [{"id": "style-pack"}],
            "node_kind": "image",
        }
        if endpoint == "compile":
            return workflow_run_client.post("/api/v1/freezone/recipes/compile", json=item)
        return workflow_run_client.post(
            "/api/v1/freezone/recipes/compile-batch",
            json={"items": [{"request_id": "retry-1", **item}]},
        )

    response = post("product-image")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    if endpoint == "compile-batch":
        assert data["items"][0]["request_id"] == "retry-1"
        assert data["items"][0]["ok"] is True
        data = data["items"][0]["data"]
    assert data == {
        "prompt": "cached castle prompt",
        "compile_mode": "persistent_cache",
        "recipe_ids": ["product-image", "style-pack"],
    }
    assert (
        read_agent_product_operation(
            project_dir=workflow_run_client.state_dir, operation_id=operation_id
        )
        == stored
    )
    mismatch = post("another-recipe")
    assert mismatch.status_code == 409
    assert "does not match admitted operation" in mismatch.json()["detail"]


def test_recipe_text_generation_does_not_replay_compile_receipt(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    _created, operation_id = _cached_recipe_media_operation(
        workflow_run_client, monkeypatch
    )
    response = workflow_run_client.post(
        "/api/v1/freezone/recipes/generate-text",
        json={
            "project_id": "proj_demo",
            "product_operation_id": operation_id,
            "recipe_id": "product-image",
            "node_kind": "text",
        },
    )
    assert response.status_code == 409
    assert "not admitted" in response.json()["detail"]


@pytest.mark.asyncio
async def test_recipe_media_retry_reclaims_failed_task_once_per_retry(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    """Issue #681: a retryable provider failure must lead to a real second submission."""
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.workflow_runs import read_workflow_run, update_workflow_run

    created, operation_id = _cached_recipe_media_operation(
        workflow_run_client, monkeypatch
    )
    tasks: dict[str, SimpleNamespace] = {}
    enqueued: list[str] = []

    class TaskManager:
        def get_task_for_project(self, _ctx, _task_type, _episode, *, scope):
            return tasks.get(scope)

    class TaskBackend:
        async def enqueue_project_task(self, _ctx, *, scope, **_kwargs):
            enqueued.append(scope)
            state = SimpleNamespace(
                task_id=f"media-task-{len(enqueued)}", status="running",
                metadata={"backend": "celery", "queue": "default"},
            )
            tasks[scope] = state
            return SimpleNamespace(task_state=state, backend="celery", queue="default")

    monkeypatch.setattr(freezone, "get_task_manager", lambda: TaskManager())
    monkeypatch.setattr(freezone, "get_task_backend", lambda: TaskBackend())
    ctx = SimpleNamespace(project_id="proj_demo", state_dir=str(workflow_run_client.state_dir))
    payload = {
        "canvas_id": "default", "node_id": "image-1",
        "product_operation_id": operation_id,
        "generation_attempt_id": "attempt-image", "prompt": "cached castle prompt",
    }

    async def enqueue(data=payload):
        return await freezone._enqueue_claimed_workflow_media(
            ctx=ctx, project_dir=workflow_run_client.state_dir,
            task_type="freezone_gen", queue_kind="default",
            payload=data.copy(), job_id="discarded-client-job",
        )

    def record_retry(retry_count: int) -> None:
        update_workflow_run(
            project_dir=workflow_run_client.state_dir,
            canvas_id="default",
            run_id=created["run_id"],
            action_updates=[{
                "node_id": "image-1", "action": "generate_image",
                "status": "running", "phase": "retrying", "retry_count": retry_count,
            }],
        )

    first = await enqueue()
    tasks[first["data"]["job_id"]].status = "failed"
    # Without a recorded retry the failed task is replayed, never resubmitted.
    assert (await enqueue())["data"]["job_id"] == first["data"]["job_id"]
    assert len(enqueued) == 1

    record_retry(1)
    with pytest.raises(HTTPException) as changed:
        await enqueue({**payload, "prompt": "another castle"})
    assert changed.value.status_code == 409
    second = await enqueue()
    assert second["data"]["job_id"] != first["data"]["job_id"]
    assert second["data"]["task_id"] == "media-task-2"
    assert enqueued == [first["data"]["job_id"], second["data"]["job_id"]]
    # Duplicate submissions of the same retry still collapse onto its task.
    tasks[second["data"]["job_id"]].status = "failed"
    assert (await enqueue())["data"]["job_id"] == second["data"]["job_id"]
    assert len(enqueued) == 2
    run = read_workflow_run(
        project_dir=workflow_run_client.state_dir,
        canvas_id="default",
        run_id=created["run_id"],
    )
    assert run["actions"][0]["job_id"] == second["data"]["job_id"]
    assert second["data"]["job_id"] in run["actions"][0]["task_key"]

    record_retry(2)
    third = await enqueue()
    assert third["data"]["task_id"] == "media-task-3"

    # A completed task is never resubmitted, even when a later retry is recorded.
    tasks[third["data"]["job_id"]].status = "completed"
    record_retry(3)
    assert (await enqueue())["data"]["job_id"] == third["data"]["job_id"]
    assert len(enqueued) == 3


class _MediaTasks:
    """In-memory task manager/backend pair for workflow media enqueue tests."""

    def __init__(self, monkeypatch, client: TestClient, operation_id: str) -> None:
        from novelvideo.api.routes import freezone

        self.freezone = freezone
        self.client = client
        self.tasks: dict[str, SimpleNamespace] = {}
        self.enqueued: list[str] = []
        outer = self

        class TaskManager:
            def get_task_for_project(self, _ctx, _task_type, _episode, *, scope):
                return outer.tasks.get(scope)

            def list_tasks_for_project(self, _ctx):
                return list(outer.tasks.values())

        class TaskBackend:
            async def enqueue_project_task(self, _ctx, *, scope, **_kwargs):
                outer.enqueued.append(scope)
                state = SimpleNamespace(
                    task_id=f"media-task-{len(outer.enqueued)}", status="running",
                    metadata={"backend": "celery", "queue": "default"},
                    task_type="freezone_gen", episode=0, beat_num=None, scope=scope,
                    progress=0.0, current_task="", result=None, error=None,
                )
                outer.tasks[scope] = state
                return SimpleNamespace(task_state=state, backend="celery", queue="default")

        monkeypatch.setattr(freezone, "get_task_manager", lambda: TaskManager())
        monkeypatch.setattr(freezone, "get_task_backend", lambda: TaskBackend())
        self.ctx = SimpleNamespace(project_id="proj_demo", state_dir=str(client.state_dir))
        self.payload = {
            "canvas_id": "default", "node_id": "image-1",
            "product_operation_id": operation_id,
            "generation_attempt_id": "attempt-image", "prompt": "cached castle prompt",
        }

    async def enqueue(self) -> dict:
        return await self.freezone._enqueue_claimed_workflow_media(
            ctx=self.ctx, project_dir=self.client.state_dir,
            task_type="freezone_gen", queue_kind="default",
            payload=self.payload.copy(), job_id="discarded-client-job",
        )


def _record_workflow_retry(
    client: TestClient, run_id: str, retry_count: int, *, runner_id: str = ""
) -> None:
    from novelvideo.freezone.workflow_runs import update_workflow_run

    update_workflow_run(
        project_dir=client.state_dir,
        canvas_id="default",
        run_id=run_id,
        runner_id=runner_id,
        action_updates=[{
            "node_id": "image-1", "action": "generate_image",
            "status": "running", "phase": "retrying", "retry_count": retry_count,
        }],
    )


@pytest.mark.parametrize(
    "scenario", ["live_retry", "non_retryable", "lease_expired", "retries_exhausted"]
)
@pytest.mark.asyncio
async def test_workflow_run_poll_keeps_live_runner_media_retry(
    workflow_run_client: TestClient, monkeypatch, scenario: str
) -> None:
    """Issue #681 review: a status poll between failure and retry must not end the run."""
    from novelvideo.freezone.workflow_runs import (
        read_workflow_run,
        reconcile_workflow_runs_with_tasks,
    )

    created, operation_id = _cached_recipe_media_operation(
        workflow_run_client, monkeypatch, runner_id="runner-one"
    )
    media = _MediaTasks(monkeypatch, workflow_run_client, operation_id)
    first = await media.enqueue()
    if scenario == "retries_exhausted":
        _record_workflow_retry(
            workflow_run_client, created["run_id"], 2, runner_id="runner-one"
        )
    if scenario == "lease_expired":
        with sqlite3.connect(workflow_run_client.state_dir / "data.db") as conn:
            conn.execute(
                "UPDATE workflow_runs SET lease_expires_at = ? WHERE run_id = ?",
                ("2000-01-01T00:00:00Z", created["run_id"]),
            )
    error = (
        "HTTP 401: invalid api key"
        if scenario == "non_retryable"
        else "HTTP 503: upstream service unavailable"
    )
    media.tasks[first["data"]["job_id"]].status = "failed"
    run = read_workflow_run(
        project_dir=workflow_run_client.state_dir,
        canvas_id="default",
        run_id=created["run_id"],
    )
    reconcile_workflow_runs_with_tasks(
        project_dir=workflow_run_client.state_dir,
        canvas_id="default",
        tasks_by_key={
            run["actions"][0]["task_key"]: {
                "status": "failed", "result": None, "error": error,
            }
        },
    )
    run = read_workflow_run(
        project_dir=workflow_run_client.state_dir,
        canvas_id="default",
        run_id=created["run_id"],
    )
    if scenario != "live_retry":
        assert run["actions"][0]["status"] == "failed"
        assert run["status"] == "failed"
        return
    assert run["actions"][0]["status"] in {"pending", "running"}
    assert run["status"] == "running"
    _record_workflow_retry(
        workflow_run_client, created["run_id"], 1, runner_id="runner-one"
    )
    second = await media.enqueue()
    assert second["data"]["job_id"] != first["data"]["job_id"]
    assert second["data"]["task_id"] == "media-task-2"


@pytest.mark.parametrize("scenario", ["live_retry", "retries_exhausted"])
@pytest.mark.asyncio
async def test_model_recipe_media_retry_survives_status_poll(
    workflow_run_client: TestClient, monkeypatch, scenario: str
) -> None:
    """Issue #681 review: model-compiled Recipes stay settleable across a retry poll."""
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.agent_product_operations import (
        read_agent_product_operation,
    )
    from novelvideo.freezone.recipe_runtime import RecipeCompileResult

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    created = workflow_run_client.post(
        base,
        json={"actions": [{
            "node_id": "image-1", "action": "generate_image",
            "recipe_id": "product-image", "recipe_version": "1.0.0",
            "generation_attempt_id": "attempt-image",
        }], "runner_id": "runner-one"},
    ).json()["data"]
    operation_id = created["actions"][0]["product_operation_id"]
    compile_calls = []

    async def model_compile(**_kwargs):
        compile_calls.append(1)
        return RecipeCompileResult(
            "model castle prompt", "model", ("product-image",),
            model_call_id=f"recipe-compiler:{len(compile_calls)}", executed_at=1.0,
        )

    monkeypatch.setattr(freezone, "compile_recipe_prompt_result", model_compile)
    request = {
        "project_id": "proj_demo",
        "product_operation_id": operation_id,
        "recipe_id": "product-image",
        "node_kind": "image",
    }

    def compile_prompt():
        return workflow_run_client.post("/api/v1/freezone/recipes/compile", json=request)

    first_compile = compile_prompt()
    assert first_compile.status_code == 200, first_compile.text
    media = _MediaTasks(monkeypatch, workflow_run_client, operation_id)
    media.payload["prompt"] = first_compile.json()["data"]["prompt"]
    first = await media.enqueue()
    failed_task = media.tasks[first["data"]["job_id"]]
    failed_task.status = "failed"
    failed_task.error = "HTTP 503: upstream service unavailable"
    if scenario == "retries_exhausted":
        _record_workflow_retry(
            workflow_run_client, created["run_id"], 2, runner_id="runner-one"
        )

    assert workflow_run_client.get(base).status_code == 200
    operation = read_agent_product_operation(
        project_dir=workflow_run_client.state_dir, operation_id=operation_id
    )
    if scenario == "retries_exhausted":
        assert operation["status"] == "failed"
        assert compile_prompt().status_code == 409
        return
    assert operation["status"] not in {"failed", "cancelled", "delivered"}

    # The retry replays the bound model compilation instead of rebinding a new one.
    retry_compile = compile_prompt()
    assert retry_compile.status_code == 200, retry_compile.text
    assert retry_compile.json()["data"]["prompt"] == "model castle prompt"
    assert retry_compile.json()["data"]["compile_mode"] == "model"
    assert len(compile_calls) == 1
    _record_workflow_retry(
        workflow_run_client, created["run_id"], 1, runner_id="runner-one"
    )
    second = await media.enqueue()
    assert second["data"]["job_id"] != first["data"]["job_id"]
    assert second["data"]["task_id"] == "media-task-2"
    assert read_agent_product_operation(
        project_dir=workflow_run_client.state_dir, operation_id=operation_id
    )["model_evidence"]["model_call_id"] == "recipe-compiler:1"


@pytest.mark.asyncio
async def test_model_recipe_third_media_retry_waits_for_its_claim(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    """A scheduled third try must not be settled as an exhausted second try (#757)."""
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.agent_product_operations import read_agent_product_operation
    from novelvideo.freezone.recipe_runtime import RecipeCompileResult
    from novelvideo.freezone.workflow_runs import read_workflow_run

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    created = workflow_run_client.post(
        base,
        json={"actions": [{
            "node_id": "image-1", "action": "generate_image",
            "recipe_id": "product-image", "recipe_version": "1.0.0",
            "generation_attempt_id": "attempt-image",
        }], "runner_id": "runner-one"},
    ).json()["data"]
    operation_id = created["actions"][0]["product_operation_id"]
    compile_calls = []

    async def model_compile(**_kwargs):
        compile_calls.append(1)
        return RecipeCompileResult(
            "model castle prompt", "model", ("product-image",),
            model_call_id=f"recipe-compiler:{len(compile_calls)}", executed_at=1.0,
        )

    monkeypatch.setattr(freezone, "compile_recipe_prompt_result", model_compile)
    compile_request = {
        "project_id": "proj_demo", "product_operation_id": operation_id,
        "recipe_id": "product-image", "node_kind": "image",
    }

    def compile_prompt():
        return workflow_run_client.post("/api/v1/freezone/recipes/compile", json=compile_request)

    def claimed_retry_count():
        with sqlite3.connect(workflow_run_client.state_dir / "data.db") as conn:
            row = conn.execute(
                "SELECT media_claim_retry_count FROM workflow_run_actions "
                "WHERE run_id = ? AND node_id = ?",
                (created["run_id"], "image-1"),
            ).fetchone()
        assert row is not None
        return row[0]

    assert compile_prompt().status_code == 200
    media = _MediaTasks(monkeypatch, workflow_run_client, operation_id)
    media.payload["prompt"] = "model castle prompt"
    first = await media.enqueue()
    first_task = media.tasks[first["data"]["job_id"]]
    first_task.status = "failed"
    first_task.error = "HTTP 503: upstream service unavailable"
    assert workflow_run_client.get(base).status_code == 200

    _record_workflow_retry(
        workflow_run_client, created["run_id"], 1, runner_id="runner-one"
    )
    assert compile_prompt().status_code == 200
    second = await media.enqueue()
    assert second["data"]["job_id"] != first["data"]["job_id"]
    second_task = media.tasks[second["data"]["job_id"]]
    second_task.status = "failed"
    second_task.error = "HTTP 503: upstream service unavailable"
    _record_workflow_retry(
        workflow_run_client, created["run_id"], 2, runner_id="runner-one"
    )
    before_third = read_workflow_run(
        project_dir=workflow_run_client.state_dir,
        canvas_id="default", run_id=created["run_id"],
    )
    assert before_third["actions"][0]["retry_count"] == 2
    assert claimed_retry_count() == 1

    assert workflow_run_client.get(base).status_code == 200
    waiting = read_workflow_run(
        project_dir=workflow_run_client.state_dir,
        canvas_id="default", run_id=created["run_id"],
    )
    public_action = workflow_run_client.get(base).json()["data"]["runs"][0]["actions"][0]
    assert "media_claim_retry_count" not in public_action
    detail_action = workflow_run_client.get(
        f"{base}/{created['run_id']}"
    ).json()["data"]["actions"][0]
    assert "media_claim_retry_count" not in detail_action
    assert waiting["status"] == "running"
    assert waiting["actions"][0]["status"] == "running"
    assert read_agent_product_operation(
        project_dir=workflow_run_client.state_dir, operation_id=operation_id
    )["status"] not in {"failed", "cancelled", "delivered"}
    assert compile_prompt().status_code == 200
    assert len(compile_calls) == 1

    third = await media.enqueue()
    assert third["data"]["job_id"] not in {
        first["data"]["job_id"], second["data"]["job_id"],
    }
    assert (await media.enqueue())["data"]["job_id"] == third["data"]["job_id"]
    assert len(media.enqueued) == 3
    claimed = read_workflow_run(
        project_dir=workflow_run_client.state_dir,
        canvas_id="default", run_id=created["run_id"],
    )
    assert claimed["actions"][0]["retry_count"] == 2
    assert claimed_retry_count() == 2

    third_task = media.tasks[third["data"]["job_id"]]
    third_task.status = "failed"
    third_task.error = "HTTP 503: upstream service unavailable"
    assert workflow_run_client.get(base).status_code == 200
    exhausted = read_workflow_run(
        project_dir=workflow_run_client.state_dir,
        canvas_id="default", run_id=created["run_id"],
    )
    assert exhausted["status"] == "failed"
    assert exhausted["actions"][0]["status"] == "failed"
    assert read_agent_product_operation(
        project_dir=workflow_run_client.state_dir, operation_id=operation_id
    )["status"] == "failed"
    assert compile_prompt().status_code == 409


@pytest.mark.asyncio
async def test_recipe_media_retry_follows_concurrent_reclaim(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    """Issue #681 review: a losing duplicate must not return the replaced failed task."""
    from novelvideo.api.routes import freezone

    created, operation_id = _cached_recipe_media_operation(
        workflow_run_client, monkeypatch
    )
    media = _MediaTasks(monkeypatch, workflow_run_client, operation_id)
    first = await media.enqueue()
    media.tasks[first["data"]["job_id"]].status = "failed"
    _record_workflow_retry(workflow_run_client, created["run_id"], 1)

    real_reclaim = freezone.reclaim_failed_workflow_media_action
    winners: list[str] = []

    def racing_reclaim(**kwargs):
        if not winners:
            # Another request reclaims and enqueues between our reads.
            winner = real_reclaim(**kwargs)
            winners.append(winner["job_id"])
            media.tasks[winner["job_id"]] = SimpleNamespace(
                task_id="winner-task", status="running",
                metadata={"backend": "celery", "queue": "default"},
            )
        return real_reclaim(**kwargs)

    monkeypatch.setattr(freezone, "reclaim_failed_workflow_media_action", racing_reclaim)
    loser = await media.enqueue()
    assert loser["data"]["job_id"] == winners[0]
    assert loser["data"]["task_id"] == "winner-task"
    assert media.enqueued == [first["data"]["job_id"]]


@pytest.mark.asyncio
async def test_recipe_media_claim_recovers_after_enqueue_interruption(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.agent_product_operations import (
        bind_agent_product_model_execution,
    )

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    created = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
        json={"actions": [{
            "node_id": "image-1", "action": "generate_image",
            "recipe_id": "product-image", "recipe_version": "1.0.0",
            "generation_attempt_id": "attempt-image",
        }]},
    ).json()["data"]
    operation_id = created["actions"][0]["product_operation_id"]
    bind_agent_product_model_execution(
        project_dir=workflow_run_client.state_dir,
        operation_id=operation_id,
        model_call_id="recipe-compiler:image", executed_at=1.0,
        source="server_recipe_compiler", compile_mode="model",
    )
    attempts = []

    class TaskManager:
        def get_task_for_project(self, *_args, **_kwargs):
            return None

    class TaskBackend:
        async def enqueue_project_task(self, _ctx, *, scope, **_kwargs):
            attempts.append(scope)
            if len(attempts) == 1:
                raise RuntimeError("interrupted before task reservation")
            return SimpleNamespace(
                task_state=SimpleNamespace(task_id="recovered-task"),
                backend="celery", queue="default",
            )

    monkeypatch.setattr(freezone, "get_task_manager", lambda: TaskManager())
    monkeypatch.setattr(freezone, "get_task_backend", lambda: TaskBackend())
    ctx = SimpleNamespace(project_id="proj_demo", state_dir=str(workflow_run_client.state_dir))
    payload = {
        "canvas_id": "default", "node_id": "image-1",
        "product_operation_id": operation_id,
        "generation_attempt_id": "attempt-image", "prompt": "a castle",
    }

    async def enqueue():
        return await freezone._enqueue_claimed_workflow_media(
            ctx=ctx, project_dir=workflow_run_client.state_dir,
            task_type="freezone_gen", queue_kind="default",
            payload=payload.copy(), job_id="discarded-client-job",
        )

    with pytest.raises(RuntimeError, match="interrupted"):
        await enqueue()
    with pytest.raises(HTTPException) as early:
        await enqueue()
    assert early.value.status_code == 503
    with sqlite3.connect(workflow_run_client.state_dir / "data.db") as conn:
        conn.execute(
            "UPDATE workflow_run_actions SET media_claimed_at = ? WHERE run_id = ?",
            (time.time() - 20, created["run_id"]),
        )
    recovered = await enqueue()
    assert recovered["data"]["job_id"] == attempts[0] == attempts[1]


def test_metered_workflow_run_admits_each_model_recipe_before_run_creation(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    from novelvideo.api.routes import freezone

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"

    rejected = workflow_run_client.post(
        base,
        json={
            "actions": [
                {
                    "node_id": "image-1",
                    "action": "generate_image",
                    "recipe_id": "product-image",
                }
            ]
        },
    )
    assert rejected.status_code == 400
    assert workflow_run_client.get(base).json()["data"]["runs"] == []

    request = {
        "idempotency_key": "run-attempt-a",
        "actions": [
            {
                "node_id": "image-1",
                "action": "generate_image",
                "recipe_id": "product-image",
                "recipe_version": "1.0.0",
                "generation_attempt_id": "attempt-a",
            },
            {"node_id": "save-1", "action": "save"},
        ],
    }
    first = workflow_run_client.post(base, json=request)
    duplicate = workflow_run_client.post(base, json=request)

    assert first.status_code == 200
    assert duplicate.status_code == 200
    first_data = first.json()["data"]
    assert duplicate.json()["data"]["run_id"] == first_data["run_id"]
    model_action, deterministic_action = first_data["actions"]
    assert model_action["product_operation_id"].startswith("agent_product_")
    assert not deterministic_action["product_operation_id"]


def test_metered_html_recipe_workflow_admits_and_binds_operation(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    from novelvideo.api.routes import freezone

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    action = {
        "node_id": "html-1",
        "action": "generate_html",
        "recipe_id": "html-recipe",
    }
    assert workflow_run_client.post(base, json={"actions": [action]}).status_code == 400
    assert workflow_run_client.get(base).json()["data"]["runs"] == []
    action["generation_attempt_id"] = "html-attempt"
    request = {"actions": [action], "idempotency_key": "html-run"}
    first = workflow_run_client.post(base, json=request)
    duplicate = workflow_run_client.post(base, json=request)
    assert first.status_code == duplicate.status_code == 200
    operation_id = first.json()["data"]["actions"][0]["product_operation_id"]
    assert operation_id.startswith("agent_product_")
    assert (
        duplicate.json()["data"]["actions"][0]["product_operation_id"] == operation_id
    )
    operation = workflow_run_client.get(
        f"/api/v1/projects/proj_demo/freezone/agent-product-operations/{operation_id}"
    ).json()["data"]
    assert operation["metadata"]["recipe_id"] == "html-recipe"
    assert operation["task_id"]
    assert len(workflow_run_client.enqueued_tasks) == 1


def test_metered_ordinary_html_keeps_existing_non_recipe_path(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    from novelvideo.api.routes import freezone

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    response = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
        json={"actions": [{"node_id": "html-1", "action": "generate_html"}]},
    )
    assert response.status_code == 200
    assert not response.json()["data"]["actions"][0]["product_operation_id"]
    assert not workflow_run_client.enqueued_tasks


@pytest.mark.parametrize(
    "action_name",
    [
        "generate_image",
        "generate_video",
        "generate_text_video",
        "generate_audio",
        "generate_story_script",
        "generate_3gs_world",
    ],
)
def test_metered_ordinary_generation_does_not_require_recipe_admission(
    workflow_run_client: TestClient, monkeypatch, action_name: str
) -> None:
    from novelvideo.api.routes import freezone

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    request = {
        "idempotency_key": f"ordinary-{action_name}",
        "actions": [
            {
                "node_id": "ordinary-1",
                "action": action_name,
                "generation_attempt_id": "ordinary-attempt",
            }
        ],
    }
    first = workflow_run_client.post(base, json=request)
    duplicate = workflow_run_client.post(base, json=request)
    assert first.status_code == duplicate.status_code == 200
    assert first.json()["data"]["run_id"] == duplicate.json()["data"]["run_id"]
    action = first.json()["data"]["actions"][0]
    assert not action["product_operation_id"]
    assert action["generation_attempt_id"] == "ordinary-attempt"
    assert not workflow_run_client.enqueued_tasks


@pytest.mark.parametrize(
    "action_name",
    [
        "generate_text",
        "generate_image",
        "generate_video",
        "generate_text_video",
        "generate_audio",
        "generate_story_script",
        "generate_3gs_world",
        "generate_html",
    ],
)
def test_metered_recipe_generation_still_requires_attempt_identity(
    workflow_run_client: TestClient, monkeypatch, action_name: str
) -> None:
    from novelvideo.api.routes import freezone

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    response = workflow_run_client.post(
        base,
        json={
            "actions": [
                {"node_id": "recipe-1", "action": action_name, "recipe_id": "recipe-a"},
            ]
        },
    )
    assert response.status_code == 400
    assert workflow_run_client.get(base).json()["data"]["runs"] == []
    assert not workflow_run_client.enqueued_tasks


def test_metered_recipe_text_still_requires_recipe_identity(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    from novelvideo.api.routes import freezone

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    response = workflow_run_client.post(
        base,
        json={
            "actions": [
                {
                    "node_id": "text-1",
                    "action": "generate_text",
                    "generation_attempt_id": "text-attempt",
                },
            ]
        },
    )
    assert response.status_code == 400
    assert workflow_run_client.get(base).json()["data"]["runs"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["memory_cache", "persistent_cache", "deterministic", "timeout_fallback", "model"],
)
@pytest.mark.parametrize("settlement_fails_once", [False, True])
async def test_late_recipe_compile_receipt_reconciles_timed_out_task(
    workflow_run_client: TestClient, monkeypatch, mode: str, settlement_fails_once: bool
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.api.schemas import FreezoneRecipeCompileRequest
    from novelvideo.freezone.recipe_runtime import RecipeCompileResult
    from novelvideo.freezone.agent_product_operations import (
        read_agent_product_operation,
    )

    operation = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/agent-product-operations",
        json={
            "product_kind": "recipe_result",
            "generation_session_id": "late-html",
            "canvas_id": "default",
            "artifact_id": "html-1",
            "normalized_inputs_hash": "late-html",
            "metadata": {"recipe_id": "html-recipe"},
        },
    ).json()["data"]
    task = SimpleNamespace(
        task_id=operation["task_id"],
        status="failed",
        metadata={
            "feature_credit_reservation_id": "original-reservation",
            "error_code": "AGENT_PRODUCT_SETTLEMENT_PENDING",
        },
    )
    settlements = set()
    attempts = []
    completions = []

    class Meter:
        async def settle_feature_credit_reservation(
            self, reservation_id, *, action, metadata=None
        ):
            attempts.append((reservation_id, action))
            if settlement_fails_once and len(attempts) == 1:
                raise RuntimeError("temporary settlement failure")
            settlements.add((reservation_id, action))
            return {"status": "completed", "action": action}

    class Manager:
        def get_task_for_project(self, *_args, **_kwargs):
            return task

        def complete_task_for_project(self, *_args, **kwargs):
            assert kwargs["expected_task_id"] == task.task_id
            completions.append(kwargs)
            task.status = "completed"
            return True

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: Meter())
    monkeypatch.setattr(freezone, "get_task_manager", lambda: Manager())
    body = FreezoneRecipeCompileRequest(
        project_id="proj_demo",
        product_operation_id=operation["operation_id"],
        recipe_id="html-recipe",
        node_kind="text",
    )
    compiled = RecipeCompileResult(
        "<!doctype html><html></html>",
        mode,
        ("html-recipe",),
        model_call_id="real-call" if mode == "model" else None,
        executed_at=1.0 if mode == "model" else None,
    )
    if settlement_fails_once:
        monkeypatch.setattr(freezone, "_schedule_recipe_settlement_retry", lambda **_kwargs: None)
        await freezone._record_recipe_compile_product_evidence(
            body=body,
            compiled=compiled,
            user={"id": "u-alice", "username": "alice"},
            deliver_text=True,
        )
        assert task.status == "failed"
        recovered = await freezone.get_agent_product_operation(
            project="proj_demo",
            operation_id=operation["operation_id"],
            user={"id": "u-alice", "username": "alice"},
        )
        assert recovered["data"]["status"] == "delivered"
        assert task.status == "completed"
    for _ in range(2):
        await freezone._record_recipe_compile_product_evidence(
            body=body,
            compiled=compiled,
            user={"id": "u-alice", "username": "alice"},
            deliver_text=True,
        )
    stored = read_agent_product_operation(
        project_dir=workflow_run_client.state_dir,
        operation_id=operation["operation_id"],
    )
    assert stored["status"] == "delivered"
    assert task.status == "completed"
    assert len(completions) == 1
    assert completions[0]["metadata"]["settlement_status"] == "reconciled"
    assert attempts == [("original-reservation", "confirm")] * (
        4 if settlement_fails_once else 2
    )
    assert settlements == {("original-reservation", "confirm")}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["model", "memory_cache", "persistent_cache", "deterministic", "timeout_fallback"],
)
@pytest.mark.parametrize("persistent_outage", [False, True])
async def test_settlement_failure_preserves_successful_recipe_response(
    workflow_run_client: TestClient, monkeypatch, mode: str, persistent_outage: bool
) -> None:
    import asyncio
    import httpx
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.agent_product_operations import (
        read_agent_product_operation,
    )
    from novelvideo.freezone.recipe_runtime import RecipeCompileResult

    operation = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/agent-product-operations",
        json={
            "product_kind": "recipe_result",
            "generation_session_id": "billing-outage",
            "canvas_id": "default",
            "artifact_id": "html-1",
            "normalized_inputs_hash": "attempt",
            "metadata": {"recipe_id": "html-recipe"},
        },
    ).json()["data"]
    task = SimpleNamespace(
        task_id=operation["task_id"],
        status="failed",
        metadata={
            "feature_credit_reservation_id": "original-reservation",
            "error_code": "AGENT_PRODUCT_SETTLEMENT_PENDING",
        },
    )
    attempts = []
    reviews = []
    generated = []
    completed = asyncio.Event()
    content = "<!doctype html><html><body>saved</body></html>"
    failure_limit = 99 if persistent_outage else 1

    class Meter:
        async def settle_feature_credit_reservation(
            self, reservation_id, *, action, metadata=None
        ):
            attempts.append((reservation_id, action))
            if len(attempts) <= failure_limit:
                raise RuntimeError("billing service unavailable")
            return {"status": "completed", "action": action}

        async def mark_feature_credit_settlement_for_review(
            self, reservation_id, *, metadata=None
        ):
            reviews.append((reservation_id, metadata))

    class Manager:
        def get_task_for_project(self, *_args, **_kwargs):
            return task

        def complete_task_for_project(self, *_args, **kwargs):
            assert kwargs["result"]["result_ref"]["content"] == content
            task.status = "completed"
            completed.set()
            return True

    async def writer(**_kwargs):
        generated.append(mode)
        return content

    async def compiler(**_kwargs):
        generated.append(mode)
        return RecipeCompileResult(content, mode, ("html-recipe",))

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: Meter())
    monkeypatch.setattr(freezone, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(freezone, "generate_recipe_text", writer)
    monkeypatch.setattr(freezone, "compile_recipe_prompt_result", compiler)
    monkeypatch.setattr(freezone, "_RECIPE_SETTLEMENT_RETRY_DELAYS", (0, 0, 0))
    endpoint = (
        "/api/v1/freezone/recipes/generate-text"
        if mode == "model"
        else "/api/v1/freezone/recipes/compile"
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=workflow_run_client.app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                endpoint,
                json={
                    "project_id": "proj_demo",
                    "product_operation_id": operation["operation_id"],
                    "recipe_id": "html-recipe",
                    "node_kind": "text",
                },
            )
            assert response.status_code == 200, response.text
            assert (
                response.json()["data"]["content" if mode == "model" else "prompt"]
                == content
            )
            if persistent_outage:
                pending = freezone._recipe_settlement_retries.get(
                    ("proj_demo", operation["operation_id"])
                )
                if pending:
                    await asyncio.wait_for(pending, timeout=5)
                assert not completed.is_set()
                assert len(attempts) == 4
                monkeypatch.setattr(
                    freezone,
                    "_schedule_recipe_settlement_retry",
                    lambda **_kwargs: None,
                )
                pending_view = await client.get(
                    f"/api/v1/projects/proj_demo/freezone/agent-product-operations/{operation['operation_id']}"
                )
                assert pending_view.status_code == 200
                assert pending_view.json()["data"]["status"] == "delivered"
                assert not completed.is_set()
                assert len(attempts) == 5
                failure_limit = 0
                recovered = await client.get(
                    f"/api/v1/projects/proj_demo/freezone/agent-product-operations/{operation['operation_id']}"
                )
                assert recovered.status_code == 200
            else:
                # No GET or second generation: independent retry completes.
                await asyncio.wait_for(completed.wait(), timeout=5)
        stored = read_agent_product_operation(
            project_dir=workflow_run_client.state_dir,
            operation_id=operation["operation_id"],
        )
        assert stored["status"] == "delivered"
        assert stored["result_ref"]["content"] == content
        assert generated == [mode]
        assert attempts == [("original-reservation", "confirm")] * (
            6 if persistent_outage else 2
        )
        assert reviews[0][0] == "original-reservation"
        assert reviews[0][1]["settlement_status"] == "awaiting_reconciliation"
        assert len(workflow_run_client.enqueued_tasks) == 1
    finally:
        pending = freezone._recipe_settlement_retries.get(
            ("proj_demo", operation["operation_id"])
        )
        if pending:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["workflow", "standalone"])
async def test_metered_html_recipe_text_generation_accepts_bound_admission(
    workflow_run_client: TestClient, monkeypatch, entry: str
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.api.schemas import FreezoneRecipeCompileRequest
    from novelvideo.freezone.agent_product_operations import (
        read_agent_product_operation,
    )

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    monkeypatch.setattr(
        freezone,
        "get_task_manager",
        lambda: SimpleNamespace(get_task_for_project=lambda *_a, **_k: None),
    )

    async def writer(**kwargs):
        assert kwargs["recipe_id"] == "html-recipe"
        return "<!doctype html><html></html>"

    monkeypatch.setattr(freezone, "generate_recipe_text", writer)
    if entry == "workflow":
        response = workflow_run_client.post(
            "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
            json={
                "actions": [
                    {
                        "node_id": "html-1",
                        "action": "generate_html",
                        "recipe_id": "html-recipe",
                        "generation_attempt_id": "attempt",
                    }
                ]
            },
        )
        assert response.status_code == 200
        operation_id = response.json()["data"]["actions"][0]["product_operation_id"]
    else:
        response = workflow_run_client.post(
            "/api/v1/projects/proj_demo/freezone/agent-product-operations",
            json={
                "product_kind": "recipe_result",
                "generation_session_id": "attempt",
                "canvas_id": "default",
                "artifact_id": "html-1",
                "normalized_inputs_hash": "attempt",
                "metadata": {"recipe_id": "html-recipe"},
            },
        )
        assert response.status_code == 200
        operation_id = response.json()["data"]["operation_id"]
    await freezone.generate_freezone_recipe_text(
        body=FreezoneRecipeCompileRequest(
            project_id="proj_demo",
            product_operation_id=operation_id,
            recipe_id="html-recipe",
            node_kind="text",
        ),
        user={"id": "u-alice", "username": "alice"},
    )
    operation = read_agent_product_operation(
        project_dir=workflow_run_client.state_dir, operation_id=operation_id
    )
    assert operation["status"] == "delivered"
    assert operation["result_ref"]["content"] == "<!doctype html><html></html>"
    assert len(workflow_run_client.enqueued_tasks) == 1


def test_workflow_result_operation_requires_and_enforces_canvas_scope(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    from novelvideo.api.routes import freezone

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    missing_canvas = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/agent-product-operations",
        json={
            "product_kind": "workflow_result",
            "generation_session_id": "generation-no-canvas",
            "artifact_id": "video-ad@1.0.0",
            "normalized_inputs_hash": "inputs-no-canvas",
            "metadata": {"skill_id": "video-ad", "skill_version": "1.0.0"},
        },
    )
    assert missing_canvas.status_code == 400
    assert "canvas_id is required" in missing_canvas.text

    admitted = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/agent-product-operations",
        json={
            "product_kind": "workflow_result",
            "generation_session_id": "generation-canvas-a",
            "canvas_id": "canvas-a",
            "artifact_id": "video-ad@1.0.0",
            "normalized_inputs_hash": "inputs-canvas-a",
            "metadata": {"skill_id": "video-ad", "skill_version": "1.0.0"},
        },
    )
    assert admitted.status_code == 200
    operation_id = admitted.json()["data"]["operation_id"]
    response = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/canvas-b/workflow-drafts",
        json={
            "operation_id": operation_id,
            "intent": {"skill_id": "video-ad", "user_goal": "广告"},
            "compiled": _valid_draft_compiled(),
        },
    )

    assert response.status_code == 400
    assert "does not match target canvas" in response.text


def test_metered_workflow_result_is_delivered_once_before_canvas_confirmation(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    from novelvideo.api.routes import freezone

    observed_metrics: list[str] = []
    monkeypatch.setattr(freezone.evidence_metrics, "observe", observed_metrics.append)
    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    operation_response = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/agent-product-operations",
        json={
            "product_kind": "workflow_result",
            "generation_session_id": "generation-a",
            "canvas_id": "default",
            "artifact_id": "video-ad@1.0.0",
            "normalized_inputs_hash": "inputs-a",
            "metadata": {"skill_id": "video-ad", "skill_version": "1.0.0"},
        },
    )
    assert operation_response.status_code == 200
    operation = operation_response.json()["data"]
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-drafts"
    draft_request = {
        "operation_id": operation["operation_id"],
        "intent": {"skill_id": "video-ad", "user_goal": "广告"},
        "compiled": _valid_draft_compiled(),
    }

    rejected = workflow_run_client.post(base, json=draft_request)
    assert rejected.status_code == 409
    assert observed_metrics == ["agent_product_evidence_rejected"]

    from novelvideo.freezone.agent_product_operations import (
        bind_agent_product_model_execution,
    )

    bind_agent_product_model_execution(
        project_dir=workflow_run_client.state_dir,
        operation_id=operation["operation_id"],
        model_call_id="agent-turn:turn-a:tool:call-a",
        executed_at=1.0,
        source="server_observed_agent_turn",
        turn_id="turn-a",
        tool_call_id="call-a",
    )
    first = workflow_run_client.post(base, json=draft_request)
    duplicate = workflow_run_client.post(base, json=draft_request)

    assert first.status_code == 200
    assert duplicate.status_code == 200
    draft = first.json()["data"]
    assert duplicate.json()["data"]["draft_id"] == draft["draft_id"]
    stored_operation = workflow_run_client.get(
        "/api/v1/projects/proj_demo/freezone/agent-product-operations/"
        + operation["operation_id"]
    ).json()["data"]
    assert stored_operation["status"] == "delivered"
    assert stored_operation["result_ref"]["id"] == draft["draft_id"]

    confirmed = workflow_run_client.post(
        f"{base}/{draft['draft_id']}/claim", json={"revision": draft["revision"]}
    )
    assert confirmed.status_code == 200
    after_confirm = workflow_run_client.get(
        "/api/v1/projects/proj_demo/freezone/agent-product-operations/"
        + operation["operation_id"]
    ).json()["data"]
    assert after_confirm["status"] == "delivered"


def test_workflow_stage_rejection_settles_the_admitted_result_operation(
    workflow_run_client: TestClient, monkeypatch, caplog,
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.agent_product_operations import (
        bind_agent_product_model_execution,
        read_agent_product_operation,
    )

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    admitted = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/agent-product-operations",
        json={
            "product_kind": "workflow_result",
            "generation_session_id": "rejected-stage-session",
            "canvas_id": "default",
            "artifact_id": "video-ad@1.0.0",
            "normalized_inputs_hash": "rejected-stage-inputs",
            "metadata": {"skill_id": "video-ad", "skill_version": "1.0.0"},
        },
    )
    assert admitted.status_code == 200
    operation = admitted.json()["data"]
    bind_agent_product_model_execution(
        project_dir=workflow_run_client.state_dir,
        operation_id=operation["operation_id"],
        model_call_id="agent-turn:rejected-stage:tool:call",
        executed_at=1.0,
        source="server_observed_agent_turn",
        turn_id="rejected-stage",
        tool_call_id="call",
    )

    async def reject_stage(*_args, **_kwargs):
        raise HTTPException(409, {
            "code": "reference_plan_must_use_native_stage",
            "message": "private user plan text",
        })

    monkeypatch.setattr(freezone, "_enforce_reference_workflow_stage", reject_stage)
    rejected = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-drafts",
        json={
            "operation_id": operation["operation_id"],
            "intent": {"skill_id": "video-ad", "user_goal": "广告"},
            "compiled": _valid_draft_compiled(),
        },
    )
    assert rejected.status_code == 409
    assert (
        "workflow_draft.create.rejected project_id=proj_demo canvas_id=default "
        f"operation_id={operation['operation_id']} phase=reference_stage "
        "status_code=409 code=reference_plan_must_use_native_stage"
    ) in caplog.text
    assert "private user plan text" not in caplog.text
    settled = read_agent_product_operation(
        project_dir=workflow_run_client.state_dir,
        operation_id=operation["operation_id"],
    )
    assert settled["status"] == "failed"
    assert settled["result_ref"] == {}


def test_late_stage_rejection_returns_concurrently_delivered_draft(
    workflow_run_client: TestClient, monkeypatch,
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.agent_product_operations import (
        bind_agent_product_model_execution, finish_agent_product_operation,
    )
    from novelvideo.freezone.workflow_drafts import create_workflow_draft

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    admitted = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/agent-product-operations",
        json={"product_kind": "workflow_result", "generation_session_id": "racing-stage",
              "canvas_id": "default", "artifact_id": "video-ad@1.0.0",
              "normalized_inputs_hash": "racing-inputs",
              "metadata": {"skill_id": "video-ad", "skill_version": "1.0.0"}},
    ).json()["data"]
    bind_agent_product_model_execution(
        project_dir=workflow_run_client.state_dir,
        operation_id=admitted["operation_id"],
        model_call_id="agent-turn:racing:tool:call", executed_at=1.0,
        source="server_observed_agent_turn", turn_id="racing", tool_call_id="call",
    )
    winner = {}

    async def concurrent_winner(*_args, **_kwargs):
        draft = create_workflow_draft(
            project_dir=workflow_run_client.state_dir, project_id="proj_demo",
            canvas_id="default", intent={"skill_id": "video-ad", "user_goal": "广告"},
            compiled=_valid_draft_compiled(), run_after_create=False,
            operation_id=admitted["operation_id"],
        )
        winner["draft_id"] = draft["draft_id"]
        finish_agent_product_operation(
            project_dir=workflow_run_client.state_dir,
            operation_id=admitted["operation_id"], outcome="delivered",
            expected_task_id=admitted["task_id"],
            result_ref={"kind": "workflow_draft", "id": draft["draft_id"],
                        "revision": draft["revision"], "digest": draft["plan_digest"]},
        )
        raise HTTPException(409, {"code": "reference_plan_must_use_native_stage"})

    monkeypatch.setattr(freezone, "_enforce_reference_workflow_stage", concurrent_winner)
    response = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-drafts",
        json={"operation_id": admitted["operation_id"],
              "intent": {"skill_id": "video-ad", "user_goal": "广告"},
              "compiled": _valid_draft_compiled()},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["draft_id"] == winner["draft_id"]


@pytest.mark.parametrize("change_direction,draft_failure", [(False, False), (False, True), (True, False)])
def test_confirmed_reference_direction_prepares_one_bound_native_draft(
    workflow_run_client: TestClient, monkeypatch, change_direction: bool, draft_failure: bool,
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.agent_workflows.catalog import compile_workflow_intent
    from novelvideo.freezone.agent_product_operations import bind_agent_product_model_execution
    from novelvideo.freezone.remix.reference_commission import (
        AuthenticatedDecisionEvidence, advance_reference_commission,
        create_reference_commission,
    )
    from novelvideo.freezone.remix.store import RemixArtifactStore

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    state_dir = workflow_run_client.state_dir
    canvas_store.save_canvas(
        state_dir, "default", base_revision=None, enforce_revision=False,
        build_payload=lambda _old: {
            "project_id": "proj_demo", "canvas_id": "default", "revision": 1,
            "nodes": [{"id": "analysis", "type": "videoStoryNode", "data": {
                "sourceVideoUrl": "/static/projects/proj_demo/reference.mp4",
                "rawResult": {"video_story": {"summary": "question then reversal"}},
            }}], "edges": [],
        },
    )
    store = RemixArtifactStore(state_dir, "proj_demo", created_by="u-alice")
    commission = create_reference_commission(
        store, canvas_id="default", requester_user_id="u-alice",
        analysis_node_id="analysis", goal="改成黑咖啡",
        analysis_evidence={"task_id": "analysis-task", "result_fingerprint": "analysis-hash"},
    )
    commission = advance_reference_commission(
        store, commission["commission_ref"],
        {"type": "direction_confirmed", "direction": {"target_goal": "改成黑咖啡"}},
        1, host_evidence=AuthenticatedDecisionEvidence("proj_demo", "default", "u-alice", "message-1"),
    )
    intent = {
        "skill_id": "reference-video-remix", "user_goal": "改成黑咖啡，双人对话，保留英文字幕",
        "reference_commission_id": commission["commission_ref"]["artifact_id"],
        "inputs": {"plan_count": 1},
        "external_inputs": [{"id": "analysis", "node_id": "analysis", "media_kind": "reference_analysis"}],
    }
    compiled = compile_workflow_intent(intent)
    assert compiled["ok"], compiled
    admitted = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/agent-product-operations",
        json={
            "product_kind": "workflow_result", "generation_session_id": "native-plan-session",
            "canvas_id": "default", "artifact_id": "reference-video-remix@2.1.0",
            "normalized_inputs_hash": "native-plan-inputs",
            "metadata": {"skill_id": "reference-video-remix", "skill_version": "2.1.0"},
        },
    )
    assert admitted.status_code == 200
    operation = admitted.json()["data"]
    bind_agent_product_model_execution(
        project_dir=state_dir, operation_id=operation["operation_id"],
        model_call_id="agent-turn:native-plan:tool:call", executed_at=1.0,
        source="server_observed_agent_turn", turn_id="native-plan", tool_call_id="call",
    )
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-drafts"
    created = workflow_run_client.post(base, json={
        "operation_id": operation["operation_id"], "intent": intent,
        "compiled": compiled, "run_after_create": True,
    })
    assert created.status_code == 200, created.text
    draft = created.json()["data"]
    assert len(canvas_store.read_canvas(state_dir, "default")["nodes"]) == 1
    binding = draft["compiled"]["reference_binding"]
    assert binding["commission_id"] == commission["commission_ref"]["artifact_id"]
    assert binding["direction_fingerprint"] == commission["direction_fingerprint"]
    assert draft["run_after_create"] is True
    if change_direction:
        commission = advance_reference_commission(
            store, commission["commission_ref"],
            {"type": "direction_confirmed", "direction": {"target_goal": "改成无糖茶"}},
            commission["commission_ref"]["revision"],
            host_evidence=AuthenticatedDecisionEvidence("proj_demo", "default", "u-alice", "message-2"),
        )
        stale_claim = workflow_run_client.post(f"{base}/{draft['draft_id']}/claim", json={"revision": 1})
        assert stale_claim.status_code == 409
        assert stale_claim.json()["detail"]["code"] == "reference_direction_changed"
        assert len(workflow_run_client.enqueued_tasks) == 1
        return
    claim = workflow_run_client.post(f"{base}/{draft['draft_id']}/claim", json={"revision": 1})
    assert claim.status_code == 200, claim.text
    assert len(workflow_run_client.enqueued_tasks) == 2
    assert len(canvas_store.read_canvas(state_dir, "default")["nodes"]) == 1
    assert workflow_run_client.enqueued_tasks[-1]["task_type"] == "freezone_workflow_confirm"

    # The browser reports the approved canvas result; only this draft's native
    # plan action may subsequently count as completion of this commission.
    from novelvideo.freezone.workflow_drafts import finish_workflow_draft_confirmation
    from novelvideo.chat.reference_production_observer import production_event
    from novelvideo.freezone.remix import production_plan, production_processing_settlement
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.workflow_runs import read_workflow_run
    from novelvideo.task_backend.runners import remix as runner

    confirmed = finish_workflow_draft_confirmation(
        project_dir=state_dir, canvas_id="default", draft_id=draft["draft_id"],
        outcome="confirmed", expected_task_id=claim.json()["data"]["task_id"],
    )
    assert confirmed["status"] == "confirmed"
    plan_node = next(node for node in draft["compiled"]["plan"]["nodes"]
                     if node["node_type"] == "scriptNode")
    request_payload = {"topic": "改成黑咖啡，双人对话，保留英文字幕"}
    plan_data = {
        "remixRole": "production_plan", "workflowInstanceId": draft["draft_id"],
        "workflowPlanNodeId": plan_node["id"], "remixNodeRevision": 1,
        "processingAction": "generate_production_plan_draft", "processingPayload": request_payload,
    }
    canvas_store.save_canvas(
        state_dir, "default", base_revision=None, enforce_revision=False,
        build_payload=lambda old: {**old, "nodes": [*old["nodes"], {
            "id": "approved-plan", "type": "scriptNode", "data": plan_data,
        }], "edges": [{"source": "analysis", "target": "approved-plan"}]},
    )
    assert production_event(store, canvas_store.read_canvas(state_dir, "default"), commission,
                            workflow_state_dir=state_dir) is None
    from novelvideo.project_context import ProjectContext
    ctx = ProjectContext(
        "proj_demo", "demo", "user", "u-alice", "alice", "u-alice", "alice",
        (("user", "u-alice"),), "editor", "local", state_dir, state_dir,
        state_dir / "_runtime", True,
    )
    async def resolve(_project, _user, **_kwargs):
        return ctx, "alice", "demo", state_dir, str(state_dir)
    monkeypatch.setattr(freezone, "_resolve_freezone_project", resolve)
    response = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
        json={"actions": [{"node_id": "approved-plan", "action": "generate_production_plan_draft",
                           "node_revision": 1, "payload": request_payload}]},
    )
    assert response.status_code == 200, response.text
    native_run = response.json()["data"]
    action = native_run["actions"][0]
    assert action["status"] == "running", action.get("error")
    assert workflow_run_client.enqueued_tasks[-1]["task_type"] == "remix_generate_production_plan_draft"
    queued_payload = workflow_run_client.enqueued_tasks[-1]["payload"]
    operations = RemixOperationStore(state_dir, "proj_demo")
    operation = operations.get(action["processing_operation_id"])
    assert queued_payload["workflow_run_id"] == native_run["run_id"]
    assert queued_payload["operation_id"] == operation["operation_id"]
    assert action["task_id"] == operation["task_id"]
    from novelvideo.freezone.remix.production_processing_settlement import _analysis_input, _node_input
    submission = {
        "action": "generate_production_plan_draft", "payload": action["payload"],
        "requestPayload": request_payload, "nodeData": _node_input({"data": plan_data}),
        "planRef": None, "analysisNodeId": "analysis",
        "analysisData": _analysis_input(canvas_store.read_canvas(state_dir, "default")["nodes"][0]),
    }
    canvas_store.save_canvas(
        state_dir, "default", base_revision=None, enforce_revision=False,
        build_payload=lambda old: {**old, "nodes": [
            {**node, "data": {**node["data"], "productionWorkflowSubmission": submission}}
            if node["id"] == "approved-plan" else node for node in old["nodes"]]},
    )
    model_calls = []
    async def model_response(**kwargs):
        model_calls.append(kwargs)
        if draft_failure:
            return "{bad json"
        fixture = Path(__file__).parent / "fixtures" / "production-audio-source-plan.json"
        return fixture.read_text(encoding="utf-8")
    monkeypatch.setattr(production_plan, "generate_recipe_text", model_response)
    task_id = operation["task_id"]
    if draft_failure:
        with pytest.raises(ValueError):
            runner.run_remix_generate_production_plan_draft({"payload": queued_payload}, ctx)
        task = SimpleNamespace(task_id=task_id, status="failed", result=None,
                               error="json_parse_failed")
    else:
        result = runner.run_remix_generate_production_plan_draft({"payload": queued_payload}, ctx)
        task = SimpleNamespace(task_id=task_id, status="completed", result=result, error=None)
    assert len(model_calls) == 1
    assert len(operations.get(operation["operation_id"])["output_refs"]) == (0 if draft_failure else 1)
    manager = SimpleNamespace(get_task_for_project=lambda *_args, **_kwargs: task,
                              _task_key_for_state=lambda _task: action["task_key"])
    production_processing_settlement.settle_native_task_delivery(
        ctx, manager, payload=queued_payload, task_type="remix_generate_production_plan_draft",
        expected_task_id=task_id, episode=0, scope=operation["operation_id"],
    )
    settled = read_workflow_run(project_dir=state_dir, canvas_id="default", run_id=native_run["run_id"])
    canvas = canvas_store.read_canvas(state_dir, "default")
    from novelvideo.chat.reference_production_observer import ReferenceProductionObserver
    from novelvideo.freezone.remix.reference_commission import read_reference_commission
    deliveries = []
    director_run = {
        "run_id": "director-run", "commission_ref": commission["commission_ref"],
        "project_id": "proj_demo", "canvas_id": "default", "requester_user_id": "u-alice",
        "username": "alice", "commission_phase": "drafting_plan",
    }
    class Coordinator:
        async def _resolve_context(self, record):
            assert record is director_run
            return ctx
        def _assert_current(self, resolved, record, **kwargs):
            assert resolved is ctx and record is director_run and kwargs["completed"] is True
            return canvas_store.read_canvas(state_dir, "default")
        async def _notify(self, record, notice, event_key):
            deliveries.append(("notice", notice, event_key))
        async def continue_commission(self, **kwargs):
            deliveries.append(("continue", kwargs["event_key"]))
    observer = ReferenceProductionObserver(Coordinator())
    async def no_cards(_ctx, _run):
        return None
    async def settled_tasks(_ctx, _run):
        return {}
    monkeypatch.setattr(observer, "_publish_cards", no_cards)
    monkeypatch.setattr(observer, "_material_snapshot", settled_tasks)
    assert asyncio.run(observer.observe_run(director_run)) is True
    latest_commission = read_reference_commission(store, commission["commission_ref"], latest=True)
    if draft_failure:
        assert settled["status"] == "failed"
        assert not canvas["nodes"][-1]["data"].get("productionPlanV2Ref")
        failed_event = production_event(store, canvas, commission, workflow_state_dir=state_dir)
        assert failed_event["event"] is None
        assert failed_event["native_failure"] is True
        assert latest_commission.get("plan_ref") is None
        assert latest_commission["phase"] == "drafting_plan"
        assert all("作品方案已保存" not in item[1] for item in deliveries if item[0] == "notice")
        return
    assert settled["status"] == "completed"
    plan_ref = operations.get(operation["operation_id"])["output_refs"][0]
    assert canvas["nodes"][-1]["data"]["productionPlanV2Ref"] == {
        "artifactId": plan_ref["artifact_id"], "revision": plan_ref["revision"],
    }
    observed = production_event(
        store, canvas, commission,
        workflow_state_dir=state_dir,
    )
    assert observed["event"]["type"] == "plan_ready"
    assert all(observed["event"]["plan_ref"][key] == value for key, value in plan_ref.items())
    assert latest_commission["plan_ref"] == observed["event"]["plan_ref"]
    assert latest_commission["phase"] == "awaiting_plan_review"
    assert [item for item in deliveries if item[0] == "continue"] == [
        ("continue", observed["event_key"]),
    ]
    assert any(item[0] == "notice" and item[2] == observed["event_key"] for item in deliveries)


def test_workflow_result_rejects_operation_for_another_compiled_skill(
    workflow_run_client: TestClient,
) -> None:
    from novelvideo.freezone.agent_product_operations import (
        bind_agent_product_model_execution,
    )

    operation = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/agent-product-operations",
        json={
            "product_kind": "workflow_result",
            "generation_session_id": "wrong-skill-session",
            "canvas_id": "default",
            "artifact_id": "other-skill@1.0.0",
            "normalized_inputs_hash": "wrong-skill-inputs",
            "metadata": {"skill_id": "other-skill", "skill_version": "1.0.0"},
        },
    ).json()["data"]
    bind_agent_product_model_execution(
        project_dir=workflow_run_client.state_dir,
        operation_id=operation["operation_id"],
        model_call_id="agent-turn:wrong-skill",
        executed_at=1.0,
        source="server_observed_agent_turn",
    )

    response = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-drafts",
        json={
            "operation_id": operation["operation_id"],
            "intent": {"skill_id": "video-ad", "user_goal": "广告"},
            "compiled": {
                "ok": True,
                "skill_id": "video-ad",
                "plan": {"nodes": [], "edges": [], "phases": []},
            },
        },
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "does not match compiled Skill" in detail
    assert "operation.skill_id='other-skill'" in detail
    assert "operation.artifact_id='other-skill@1.0.0'" in detail
    assert "expected skill_id='video-ad'" in detail


def test_workflow_draft_rejects_skill_definition_operation_with_recovery_hint(
    workflow_run_client: TestClient,
) -> None:
    operation = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/agent-product-operations",
        json={
            "product_kind": "workflow_generate",
            "generation_session_id": "wrong-product-kind-session",
            "canvas_id": "default",
            "artifact_id": "private-html-smoke-test",
            "normalized_inputs_hash": "wrong-product-kind-inputs",
            "metadata": {
                "skill_id": "private-html-smoke-test",
                "skill_version": "3",
            },
        },
    ).json()["data"]

    response = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-drafts",
        json={
            "operation_id": operation["operation_id"],
            "intent": {"skill_id": "private-html-smoke-test", "user_goal": "咖啡网页"},
            "compiled": {
                "ok": True,
                "skill_id": "private-html-smoke-test",
                "plan": {"nodes": [], "edges": [], "phases": []},
            },
        },
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "product_kind='workflow_generate'" in detail
    assert "use product_kind='workflow_result'" in detail


def test_generation_session_validates_manifest_against_durable_operations(
    workflow_run_client: TestClient,
) -> None:
    session_id = "validated-session"
    manifest, draft = _recipe_generation_session_payload(
        workflow_run_client,
        session_id=session_id,
    )

    response = workflow_run_client.put(
        f"/api/v1/projects/proj_demo/freezone/agent-generation-sessions/{session_id}",
        json={"canvas_id": "default", "manifest": manifest, "draft": draft},
    )

    assert response.status_code == 200
    saved = response.json()["data"]
    assert saved["manifest"] == manifest
    assert saved["draft"]["operations"]["recipes"]["0"]["product_kind"] == (
        "recipe_generate"
    )


def test_generation_session_rejects_unavailable_reused_recipe(
    workflow_run_client: TestClient,
) -> None:
    session_id = "missing-reuse-session"
    manifest, draft = _recipe_generation_session_payload(
        workflow_run_client,
        session_id=session_id,
        reused_recipe_id="recipe-that-does-not-exist",
    )

    response = workflow_run_client.put(
        f"/api/v1/projects/proj_demo/freezone/agent-generation-sessions/{session_id}",
        json={"canvas_id": "default", "manifest": manifest, "draft": draft},
    )

    assert response.status_code == 400
    assert "reused Recipe is unavailable" in response.json()["detail"]


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("missing_operation", "operation count does not match"),
        ("wrong_recipe_result", "submitted Recipe does not match"),
    ],
)
def test_generation_session_rejects_manifest_draft_mismatches(
    workflow_run_client: TestClient,
    mutation: str,
    expected_error: str,
) -> None:
    session_id = f"mismatch-{mutation}"
    manifest, draft = _recipe_generation_session_payload(
        workflow_run_client,
        session_id=session_id,
    )
    draft = deepcopy(draft)
    if mutation == "missing_operation":
        draft["operations"]["recipes"] = {}
    else:
        draft["recipes"] = {"0": {"id": "another-recipe"}}

    response = workflow_run_client.put(
        f"/api/v1/projects/proj_demo/freezone/agent-generation-sessions/{session_id}",
        json={"canvas_id": "default", "manifest": manifest, "draft": draft},
    )

    assert response.status_code == 400
    assert expected_error in response.json()["detail"]


@pytest.mark.parametrize(
    ("operation_overrides", "expected_error"),
    [
        ({"operation_session_id": "another-session"}, "operation is unavailable"),
        ({"operation_kind": "workflow_generate"}, "operation kind does not match"),
        ({"operation_artifact_id": "another-artifact"}, "artifact does not match"),
    ],
)
def test_generation_session_rejects_wrong_operation_identity(
    workflow_run_client: TestClient,
    operation_overrides: dict,
    expected_error: str,
) -> None:
    session_id = f"operation-identity-{expected_error.split()[0]}"
    manifest, draft = _recipe_generation_session_payload(
        workflow_run_client,
        session_id=session_id,
        **operation_overrides,
    )

    response = workflow_run_client.put(
        f"/api/v1/projects/proj_demo/freezone/agent-generation-sessions/{session_id}",
        json={"canvas_id": "default", "manifest": manifest, "draft": draft},
    )

    assert response.status_code == 400
    assert expected_error in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("settlement_status", ["pending", "completed"])
@pytest.mark.parametrize(
    ("task_type", "product_kind", "result_ref"),
    [
        (
            "freezone_agent_recipe_result",
            "recipe_result",
            {"kind": "recipe_result", "id": "asset-a"},
        ),
        (
            "freezone_agent_workflow_result",
            "workflow_result",
            {"kind": "workflow_draft", "id": "draft-a"},
        ),
    ],
)
async def test_late_agent_product_delivery_confirms_reserved_credit(
    monkeypatch, settlement_status, task_type, product_kind, result_ref
) -> None:
    from novelvideo.api.routes import freezone

    settlements: list[tuple[str, str]] = []
    completions: list[dict] = []
    observed_metrics: list[str] = []
    task = SimpleNamespace(
        task_id="product-task-a",
        status="running" if product_kind == "workflow_result" else "failed",
        metadata={
            "feature_credit_reservation_id": "reservation-a",
            "error_code": "AGENT_PRODUCT_SETTLEMENT_PENDING",
        },
    )

    class UsageMeter:
        async def settle_feature_credit_reservation(
            self, reservation_id, *, action, metadata=None
        ):
            settlements.append((reservation_id, action))
            assert metadata["source"] == "agent_product_late_delivery"
            return {"status": settlement_status}

    class Manager:
        def get_task_for_project(self, *_args, **_kwargs):
            return task

        def complete_task_for_project(self, *_args, **kwargs):
            completions.append(kwargs)
            return True

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: UsageMeter())
    monkeypatch.setattr(freezone, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(freezone.evidence_metrics, "observe", observed_metrics.append)

    await freezone._settle_delivered_agent_product_task(
        ctx=SimpleNamespace(project_id="proj_demo"),
        operation={
            "operation_id": "agent_product_a",
            "project_id": "proj_demo",
            "task_id": "product-task-a",
            "task_type": task_type,
            "product_kind": product_kind,
            "status": "delivered",
            "model_evidence": {"model_call_id": "provider-job-a"},
            "result_ref": result_ref,
        },
    )

    assert settlements == [("reservation-a", "confirm")]
    assert completions[0]["metadata"]["settlement_status"] == "reconciled"
    assert observed_metrics == ["agent_product_reconciled"]


@pytest.mark.asyncio
async def test_failed_workflow_delivery_terminalizes_waiting_task(monkeypatch) -> None:
    from novelvideo.api.routes import freezone

    task = SimpleNamespace(
        task_id="product-task-a",
        status="running",
        metadata={"error_code": "AGENT_PRODUCT_SETTLEMENT_PENDING"},
    )
    failures: list[dict] = []

    class Manager:
        def get_task_for_project(self, *_args, **_kwargs):
            return task

        def fail_task_for_project(self, *_args, **kwargs):
            failures.append(kwargs)

    monkeypatch.setattr(freezone, "get_task_manager", lambda: Manager())

    await freezone._settle_failed_agent_product_task(
        ctx=SimpleNamespace(project_id="proj_demo"),
        operation={
            "operation_id": "agent_product_a",
            "project_id": "proj_demo",
            "task_id": "product-task-a",
            "task_type": "freezone_agent_workflow_result",
            "product_kind": "workflow_result",
            "status": "failed",
        },
        error="draft persistence failed",
    )

    assert failures[0]["expected_task_id"] == "product-task-a"
    assert failures[0]["error"] == "draft persistence failed"
    assert failures[0]["metadata"]["settlement_status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "settlement_result",
    [
        {
            "status": "awaiting",
            "action": "confirm",
            "error_code": "durable_settlement_update_failed",
        },
        {"status": "completed", "action": "refund"},
    ],
)
async def test_late_agent_product_delivery_waits_for_durable_confirmation(
    monkeypatch, caplog, settlement_result
) -> None:
    from novelvideo.api.routes import freezone

    completions: list[dict] = []
    observed_metrics: list[str] = []
    task = SimpleNamespace(
        task_id="product-task-a",
        status="failed",
        metadata={
            "feature_credit_reservation_id": "reservation-a",
            "error_code": "AGENT_PRODUCT_SETTLEMENT_PENDING",
        },
    )

    class UsageMeter:
        async def settle_feature_credit_reservation(self, *_args, **_kwargs):
            return settlement_result

    class Manager:
        def get_task_for_project(self, *_args, **_kwargs):
            return task

        def complete_task_for_project(self, *_args, **kwargs):
            completions.append(kwargs)
            return True

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: UsageMeter())
    monkeypatch.setattr(freezone, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(freezone.evidence_metrics, "observe", observed_metrics.append)

    with pytest.raises(RuntimeError, match="credit confirmation unavailable"):
        await freezone._settle_delivered_agent_product_task(
            ctx=SimpleNamespace(project_id="proj_demo"),
            operation={
                "operation_id": "agent_product_a",
                "project_id": "proj_demo",
                "task_id": "product-task-a",
                "task_type": "freezone_agent_recipe_result",
                "product_kind": "recipe_result",
                "status": "delivered",
                "model_evidence": {"model_call_id": "provider-job-a"},
                "result_ref": {"kind": "recipe_result", "id": "asset-a"},
            },
        )

    assert completions == []
    assert observed_metrics == ["agent_product_awaiting_reconciliation"]
    assert "Agent product late delivery credit confirmation unavailable" in caplog.text


@pytest.mark.asyncio
async def test_late_agent_product_delivery_does_not_claim_failed_task_reconciled(
    monkeypatch,
) -> None:
    from novelvideo.api.routes import freezone

    settlements: list[str] = []
    observed_metrics: list[str] = []
    task = SimpleNamespace(
        task_id="product-task-a",
        status="failed",
        metadata={
            "feature_credit_reservation_id": "reservation-a",
            "error_code": "AGENT_PRODUCT_SETTLEMENT_PENDING",
        },
    )

    class UsageMeter:
        async def settle_feature_credit_reservation(
            self, reservation_id, *, action, metadata
        ):
            settlements.append(reservation_id)
            return {"status": "completed"}

    class Manager:
        def get_task_for_project(self, *_args, **_kwargs):
            return task

        def complete_task_for_project(self, *_args, **_kwargs):
            return False

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: UsageMeter())
    monkeypatch.setattr(freezone, "get_task_manager", lambda: Manager())
    monkeypatch.setattr(freezone.evidence_metrics, "observe", observed_metrics.append)

    with pytest.raises(RuntimeError, match="did not reconcile"):
        await freezone._settle_delivered_agent_product_task(
            ctx=SimpleNamespace(project_id="proj_demo"),
            operation={
                "operation_id": "agent_product_a",
                "project_id": "proj_demo",
                "task_id": "product-task-a",
                "task_type": "freezone_agent_recipe_result",
                "product_kind": "recipe_result",
                "status": "delivered",
                "model_evidence": {"model_call_id": "provider-job-a"},
                "result_ref": {"kind": "recipe_result", "id": "asset-a"},
            },
        )
    assert settlements == ["reservation-a"]
    assert observed_metrics == []


@pytest.mark.asyncio
async def test_late_agent_product_delivery_rejects_cross_project_receipt(monkeypatch) -> None:
    from novelvideo.api.routes import freezone

    class UsageMeter:
        async def settle_feature_credit_reservation(self, *_args, **_kwargs):
            pytest.fail("cross-project operation must not settle a reservation")

    class Manager:
        def get_task_for_project(self, *_args, **_kwargs):
            pytest.fail("cross-project operation must not read another task")

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: UsageMeter())
    monkeypatch.setattr(freezone, "get_task_manager", lambda: Manager())

    await freezone._settle_delivered_agent_product_task(
        ctx=SimpleNamespace(project_id="proj_demo"),
        operation={
            "operation_id": "agent_product_a",
            "project_id": "another-project",
            "task_id": "product-task-a",
            "task_type": "freezone_agent_recipe_result",
            "product_kind": "recipe_result",
            "status": "delivered",
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("node_kind", ["image", "text"])
@pytest.mark.parametrize("usable_prompt", [True, False])
@pytest.mark.parametrize(
    "reuse_mode",
    ["memory_cache", "persistent_cache", "deterministic", "timeout_fallback"],
)
async def test_recipe_compile_binds_only_fresh_model_evidence(
    workflow_run_client: TestClient,
    node_kind: str,
    reuse_mode: str,
    usable_prompt: bool,
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.api.schemas import FreezoneRecipeCompileRequest
    from novelvideo.freezone.agent_product_operations import (
        read_agent_product_operation,
    )
    from novelvideo.freezone.recipe_runtime import RecipeCompileResult

    def admit(session_id: str) -> dict:
        response = workflow_run_client.post(
            "/api/v1/projects/proj_demo/freezone/agent-product-operations",
            json={
                "product_kind": "recipe_result",
                "generation_session_id": session_id,
                "canvas_id": "default",
                "artifact_id": "image-1",
                "normalized_inputs_hash": session_id,
                "metadata": {"recipe_id": "product-image"},
            },
        )
        assert response.status_code == 200
        return response.json()["data"]

    def request(operation_id: str) -> FreezoneRecipeCompileRequest:
        return FreezoneRecipeCompileRequest(
            project_id="proj_demo",
            product_operation_id=operation_id,
            recipe_id="product-image",
            node_kind=node_kind,
        )

    model_operation = admit("model-compile")
    await freezone._record_recipe_compile_product_evidence(
        body=request(model_operation["operation_id"]),
        compiled=RecipeCompileResult(
            "compiled",
            "model",
            ("product-image",),
            model_call_id="recipe-compiler:call-a",
            executed_at=1.0,
        ),
        user={"id": "u-alice", "username": "alice"},
        deliver_text=node_kind == "text",
    )
    stored_model = read_agent_product_operation(
        project_dir=workflow_run_client.state_dir,
        operation_id=model_operation["operation_id"],
    )
    assert stored_model["model_evidence"]["compile_mode"] == "model"
    if node_kind == "text":
        assert stored_model["status"] == "delivered"
        assert stored_model["result_ref"] == {
            "kind": "recipe_text_result",
            "id": model_operation["operation_id"],
            "content": "compiled",
        }
    else:
        assert stored_model["status"] == "reserved"

    cached_operation = admit("cached-compile")
    await freezone._record_recipe_compile_product_evidence(
        body=request(cached_operation["operation_id"]),
        compiled=RecipeCompileResult(
            "cached" if usable_prompt else "   ",
            reuse_mode,
            ("product-image",),
        ),
        user={"id": "u-alice", "username": "alice"},
        deliver_text=node_kind == "text",
    )
    stored_cached = read_agent_product_operation(
        project_dir=workflow_run_client.state_dir,
        operation_id=cached_operation["operation_id"],
    )
    if not usable_prompt:
        assert stored_cached["status"] == "failed"
        assert stored_cached["model_evidence"] == {}
        return
    assert stored_cached["status"] == "delivered"
    assert stored_cached["result_ref"] == {
        "kind": "recipe_compile_result",
        "id": cached_operation["operation_id"],
        "reason": reuse_mode,
        "content": "cached",
    }
    from novelvideo.task_backend.runners.freezone import (
        _run_freezone_agent_product_async,
    )

    result = await _run_freezone_agent_product_async(
        {
            "__run_task_id": stored_cached["task_id"],
            "payload": {
                "operation_id": cached_operation["operation_id"],
                "product_kind": "recipe_result",
            },
        },
        SimpleNamespace(state_dir=workflow_run_client.state_dir),
    )
    assert result["compile_mode"] == reuse_mode
    assert result["delivery_status"] == "delivered"
    assert "正常计费" in result["message"]
    assert stored_cached["model_evidence"] == {}
    # A repeated compiler result cannot create another operation or overwrite delivery.
    repeated_admission = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/agent-product-operations",
        json={
            "product_kind": "recipe_result",
            "generation_session_id": "cached-compile",
            "canvas_id": "default",
            "artifact_id": "image-1",
            "normalized_inputs_hash": "cached-compile",
        },
    )
    assert repeated_admission.status_code == 400
    assert "already terminal" in repeated_admission.json()["detail"]
    await freezone._record_recipe_compile_product_evidence(
        body=request(cached_operation["operation_id"]),
        compiled=RecipeCompileResult("changed", reuse_mode, ("product-image",)),
        user={"id": "u-alice", "username": "alice"},
        deliver_text=node_kind == "text",
    )
    assert (
        read_agent_product_operation(
            project_dir=workflow_run_client.state_dir,
            operation_id=cached_operation["operation_id"],
        )
        == stored_cached
    )


@pytest.mark.parametrize("strategy", ["llm_refine", "template", "user_message", "previous_output"])
def test_metered_recipe_compile_requires_operation_before_compiler(
    workflow_run_client: TestClient,
    monkeypatch,
    strategy,
) -> None:
    from novelvideo.api.routes import freezone

    compiler_called = False

    async def fake_compile(**_kwargs):
        nonlocal compiler_called
        compiler_called = True
        raise AssertionError("compiler must not run without product admission")

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    monkeypatch.setattr(freezone, "compile_recipe_prompt_result", fake_compile)

    response = workflow_run_client.post(
        "/api/v1/freezone/recipes/compile",
        json={
            "recipe_id": "product-image",
            "node_kind": "image",
            "prompt_strategy": strategy,
        },
    )

    assert response.status_code == 400
    assert "product_operation_id is required" in response.json()["detail"]
    assert compiler_called is False


@pytest.mark.parametrize(
    "strategy", ["llm_refine", "template", "user_message", "previous_output"]
)
def test_metered_recipe_compile_batch_fails_before_any_compiler_call(
    workflow_run_client: TestClient,
    monkeypatch,
    strategy,
) -> None:
    from novelvideo.api.routes import freezone

    compiler_called = False

    async def fake_compile_batch(_items):
        nonlocal compiler_called
        compiler_called = True
        raise AssertionError("batch compiler must not run with an unadmitted item")

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    monkeypatch.setattr(freezone, "compile_recipe_prompt_batch", fake_compile_batch)

    response = workflow_run_client.post(
        "/api/v1/freezone/recipes/compile-batch",
        json={
            "items": [
                {
                    "request_id": "request-a",
                    "recipe_id": "product-image",
                    "node_kind": "image",
                    "prompt_strategy": strategy,
                }
            ]
        },
    )

    assert response.status_code == 400
    assert "product_operation_id is required" in response.json()["detail"]
    assert compiler_called is False


def test_metered_recipe_text_requires_operation_before_generation(
    workflow_run_client: TestClient,
    monkeypatch,
) -> None:
    from novelvideo.api.routes import freezone

    generated = False

    async def fake_generate(**_kwargs):
        nonlocal generated
        generated = True
        raise AssertionError("text generation must not run without admission")

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    monkeypatch.setattr(freezone, "generate_recipe_text", fake_generate)

    response = workflow_run_client.post(
        "/api/v1/freezone/recipes/generate-text",
        json={
            "recipe_id": "product-image",
            "node_kind": "text",
            "prompt_strategy": "template",
        },
    )

    assert response.status_code == 400
    assert "product_operation_id is required" in response.json()["detail"]
    assert generated is False


def test_canvas_revision_endpoint_returns_only_revision(
    workflow_run_client: TestClient,
) -> None:
    response = workflow_run_client.get(
        "/api/v1/projects/proj_demo/freezone/canvases/default/revision"
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"canvas_id": "default", "revision": 1}


def test_workflow_draft_does_not_require_planning_credit_confirmation_in_ce(
    workflow_run_client: TestClient,
) -> None:
    response = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-drafts",
        json={
            "intent": {"skill_id": "video-ad", "user_goal": "广告"},
            "compiled": _valid_draft_compiled(),
        },
    )

    assert response.status_code == 200
    assert "agent_credit_estimate" not in response.json()["data"]
    assert "agent_planning_charge" not in response.json()["data"]


def test_workflow_confirmation_enqueues_non_monetary_durable_task(
    workflow_run_client: TestClient,
) -> None:
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-drafts"
    created = workflow_run_client.post(
        base,
        json={
            "intent": {"skill_id": "video-ad", "user_goal": "广告"},
            "compiled": _valid_draft_compiled(),
        },
    ).json()["data"]

    response = workflow_run_client.post(
        f"{base}/{created['draft_id']}/claim",
        json={"revision": 1},
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["task_id"] == "workflow-task-1"
    assert payload["root_task_id"] == "workflow-task-1"
    assert "billing" not in payload
    assert "quote_id" not in payload


def test_workflow_confirmation_retry_uses_a_new_task_scope(
    workflow_run_client: TestClient,
) -> None:
    client = workflow_run_client
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-drafts"
    draft = client.post(
        base,
        json={
            "intent": {"skill_id": "video-ad", "user_goal": "广告"},
            "compiled": _valid_draft_compiled(),
        },
    ).json()["data"]
    target = f"{base}/{draft['draft_id']}"

    first = client.post(target + "/claim", json={"revision": 1}).json()["data"]
    finished = client.post(
        target + "/finish",
        json={"outcome": "ready", "task_id": first["task_id"], "revision": 1},
    )
    assert finished.status_code == 200, finished.text

    second_response = client.post(target + "/claim", json={"revision": 1})
    assert second_response.status_code == 200, second_response.text
    second = second_response.json()["data"]
    assert second["task_id"] != first["task_id"]
    assert len(client.enqueued_tasks) == 2
    assert client.enqueued_tasks[0]["scope"] != client.enqueued_tasks[1]["scope"]
    assert (
        client.enqueued_tasks[0]["payload"]["confirmation_started_at"]
        != client.enqueued_tasks[1]["payload"]["confirmation_started_at"]
    )


def test_workflow_run_api_rejects_invalid_action_phase(
    workflow_run_client: TestClient,
) -> None:
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    created = workflow_run_client.post(
        base,
        json={"actions": [{"node_id": "image-1", "action": "generate_image"}]},
    ).json()["data"]

    response = workflow_run_client.patch(
        f"{base}/{created['run_id']}",
        json={
            "action_updates": [
                {
                    "node_id": "image-1",
                    "action": "generate_image",
                    "status": "running",
                    "phase": "unknown_phase",
                }
            ]
        },
    )

    assert response.status_code == 400


def test_workflow_run_api_rejects_empty_actions(
    workflow_run_client: TestClient,
) -> None:
    response = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
        json={"actions": []},
    )

    assert response.status_code == 400


def test_workflow_run_dispatches_registered_remix_action_without_recipe(
    workflow_run_client: TestClient, monkeypatch
) -> None:

    media = workflow_run_client.state_dir / "media" / "reference.mp4"
    media.parent.mkdir()
    media.write_bytes(b"reference")
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"

    response = workflow_run_client.post(base, json={
        "idempotency_key": "remix-analysis-run",
        "actions": [{
            "node_id": "analysis",
            "action": "analyze_reference_video",
            "node_revision": 4,
            "payload": {"media_path": "media/reference.mp4"},
        }],
    })

    assert response.status_code == 200, response.text
    action = response.json()["data"]["actions"][0]
    assert action["status"] == "running"
    assert action["task_type"] == "remix_analyze_reference_video"
    assert action["task_key"]
    assert action["processing_operation_id"]
    assert action["product_operation_id"] is None
    assert action["recipe_id"] is None
    assert workflow_run_client.enqueued_tasks[0]["payload"]["media_sha256"]


def test_workflow_production_draft_requests_v2_output(workflow_run_client: TestClient) -> None:
    from novelvideo.freezone import canvas_store

    canvas = canvas_store.default_canvas_payload(project_id="proj_demo", actor_id="u-alice")
    canvas["nodes"] = [{"id": "plan", "type": "scriptNode", "data": {"remixRole": "production_plan"}}]
    path = canvas_store.canvas_path(workflow_run_client.state_dir, "default")
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas_store.atomic_write_json(path, canvas)
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    response = workflow_run_client.post(base, json={
        "idempotency_key": "production-draft-v2-run",
        "actions": [{"node_id": "plan", "action": "generate_production_plan_draft", "node_revision": 1,
                     "payload": {"topic": "横屏短片"}}],
    })
    assert response.status_code == 200, response.text
    queued = workflow_run_client.enqueued_tasks[-1]
    assert queued["task_type"] == "remix_generate_production_plan_draft"
    assert "output_version" not in queued["payload"]


def test_workflow_native_plan_binds_active_automation_before_enqueue(
    workflow_run_client: TestClient, monkeypatch,
) -> None:
    from novelvideo.api.auth import get_api_user
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.remix import production_automation, production_processing_settlement

    canvas = canvas_store.default_canvas_payload(project_id="proj_demo", actor_id="u-alice")
    canvas["nodes"] = [{"id": "plan", "type": "scriptNode", "data": {"remixRole": "production_plan"}}]
    path = canvas_store.canvas_path(workflow_run_client.state_dir, "default")
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas_store.atomic_write_json(path, canvas)
    workflow_run_client.app.dependency_overrides[get_api_user] = lambda: {
        "id": "u-alice", "username": "alice", "credential_kind": "agent_session",
    }
    monkeypatch.setattr(production_processing_settlement, "native_action_automation_control",
                        lambda *_args, **_kwargs: {"active": True, "generation": 3,
                            "scope": {"commission_id": "commission", "agent_id": "main"}})
    bindings = []

    def bind(_ctx, **kwargs):
        assert not workflow_run_client.enqueued_tasks
        bindings.append(kwargs)

    monkeypatch.setattr(production_automation, "bind_automation_operation", bind)
    response = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
        json={"actions": [{"node_id": "plan", "action": "generate_production_plan_draft",
                           "node_revision": 1, "payload": {"topic": "coffee"}}]},
    )

    assert response.status_code == 200, response.text
    assert len(bindings) == len(workflow_run_client.enqueued_tasks) == 1
    assert bindings[0]["commission_id"] == "commission"
    assert bindings[0]["generation"] == 3


def test_workflow_native_plan_does_not_enqueue_after_automation_stops(
    workflow_run_client: TestClient, monkeypatch,
) -> None:
    from novelvideo.api.auth import get_api_user
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.remix import production_automation, production_processing_settlement

    canvas = canvas_store.default_canvas_payload(project_id="proj_demo", actor_id="u-alice")
    canvas["nodes"] = [{"id": "plan", "type": "scriptNode", "data": {"remixRole": "production_plan"}}]
    path = canvas_store.canvas_path(workflow_run_client.state_dir, "default")
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas_store.atomic_write_json(path, canvas)
    workflow_run_client.app.dependency_overrides[get_api_user] = lambda: {
        "id": "u-alice", "username": "alice", "credential_kind": "agent_session",
    }
    monkeypatch.setattr(production_processing_settlement, "native_action_automation_control",
                        lambda *_args, **_kwargs: {"active": False, "generation": 3,
                            "scope": {"commission_id": "commission", "agent_id": "main"}})
    monkeypatch.setattr(production_automation, "bind_automation_operation",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("production automation stopped")))
    response = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
        json={"actions": [{"node_id": "plan", "action": "generate_production_plan_draft",
                           "node_revision": 1, "payload": {"topic": "coffee"}}]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["actions"][0]["status"] == "failed"
    assert workflow_run_client.enqueued_tasks == []


def test_manual_plan_draft_enqueues_after_automation_stops(
    workflow_run_client: TestClient, monkeypatch,
) -> None:
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.remix import production_automation, production_processing_settlement

    canvas = canvas_store.default_canvas_payload(project_id="proj_demo", actor_id="u-alice")
    canvas["nodes"] = [{"id": "plan", "type": "scriptNode", "data": {"remixRole": "production_plan"}}]
    path = canvas_store.canvas_path(workflow_run_client.state_dir, "default")
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas_store.atomic_write_json(path, canvas)
    monkeypatch.setattr(production_processing_settlement, "native_action_automation_control",
                        lambda *_args, **_kwargs: {"active": False, "generation": 3,
                            "scope": {"commission_id": "commission", "agent_id": "main"}})
    monkeypatch.setattr(production_automation, "bind_automation_operation",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not bind")))
    response = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
        json={"idempotency_key": "manual-draft-once",
              "runner_id": "canvas-runner:browser",
              "metadata": {"native_execution_source": "agent_command"},
              "actions": [{"node_id": "plan", "action": "generate_production_plan_draft",
                           "node_revision": 1, "payload": {"topic": "coffee"}}]},
    )

    assert response.status_code == 200, response.text
    run = response.json()["data"]
    assert run["metadata"]["native_execution_source"] == "canvas_user"
    assert run["metadata"]["native_completion_owner"] == "host"
    assert run["runner_id"] is None
    assert run["metadata"]["native_execution_context"] == {
        "source": "canvas_user", "requester_user_id": "u-alice",
        "project_id": "proj_demo", "canvas_id": "default", "run_id": run["run_id"],
    }
    assert len(workflow_run_client.enqueued_tasks) == 1
    assert run["actions"][0]["task_key"]
    from novelvideo.freezone.workflow_runs import interrupt_stale_workflow_runs
    assert interrupt_stale_workflow_runs(
        project_dir=workflow_run_client.state_dir, canvas_id="default",
        stale_after_seconds=60,
        now=datetime.now(timezone.utc) + timedelta(seconds=181),
    ) == []
    duplicate = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
        json={"idempotency_key": "manual-draft-twice",
              "runner_id": "canvas-runner:browser-two",
              "actions": [{"node_id": "plan", "action": "generate_production_plan_draft",
                           "node_revision": 1, "payload": {"topic": "coffee"}}]},
    )
    assert duplicate.status_code == 409
    assert len(workflow_run_client.enqueued_tasks) == 1
    from novelvideo.api.auth import get_api_user
    workflow_run_client.app.dependency_overrides[get_api_user] = lambda: {
        "id": "u-alice", "username": "alice", "credential_kind": "agent_session",
    }
    replay = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
        json={"idempotency_key": "manual-draft-once",
              "actions": [{"node_id": "plan", "action": "generate_production_plan_draft",
                           "node_revision": 1, "payload": {"topic": "coffee"}}]},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["run_id"] == run["run_id"]
    assert replay.json()["data"]["metadata"]["native_execution_source"] == "canvas_user"
    assert len(workflow_run_client.enqueued_tasks) == 1


def test_workflow_script_alignment_uses_newapi_readiness(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    from novelvideo.api.routes import remix
    from novelvideo.freezone.remix.store import RemixArtifactStore

    media = workflow_run_client.state_dir / "media" / "voice.wav"
    media.parent.mkdir()
    media.write_bytes(b"voice")
    script = RemixArtifactStore(workflow_run_client.state_dir, "proj_demo").create(
        "remix_script.v1",
        {"dialogue": "你好", "segments": [], "selections": [],
         "blueprint_mappings": [], "media_requirements": []},
        [],
    )
    monkeypatch.setattr(remix, "_transcription_engine_ready", lambda: True)

    response = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
        json={"idempotency_key": "newapi-align-run", "actions": [{
            "node_id": "align", "action": "align_script", "node_revision": 1,
            "payload": {"media_path": "media/voice.wav", "script_ref": {
                "artifact_id": script["id"], "revision": script["revision"],
            }},
        }]},
    )

    assert response.status_code == 200, response.text
    assert workflow_run_client.enqueued_tasks[0]["task_type"] == "remix_align_script"


def test_workflow_production_alignment_preflights_transcription_engine(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    from novelvideo.api.routes import remix

    monkeypatch.setattr(remix, "_transcription_engine_ready", lambda: False)
    response = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
        json={"idempotency_key": "production-align-not-configured", "actions": [{
            "node_id": "align", "action": "align_production_speech", "node_revision": 1,
            "payload": {"media_path": "media/voice.wav", "plan_ref": {"artifact_id": "pending", "revision": 1},
                        "turn_id": "line", "placement_id": "picture"},
        }]},
    )
    assert response.status_code == 400
    assert "NewAPI audio-transcribe is not configured" in response.text


def test_concurrent_workflow_processing_dispatch_enqueues_once(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    from novelvideo.api.routes import freezone

    media = workflow_run_client.state_dir / "media" / "reference.mp4"
    media.parent.mkdir()
    media.write_bytes(b"reference")
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    class SlowBackend:
        async def enqueue_project_task(self, _ctx, **kwargs):
            nonlocal calls
            calls += 1
            entered.set()
            await asyncio.to_thread(release.wait, 2)
            return SimpleNamespace(task_state=SimpleNamespace(task_id="workflow-task-one"))

    monkeypatch.setattr(freezone, "get_task_backend", lambda: SlowBackend())
    body = {
        "idempotency_key": "concurrent-processing-run",
        "actions": [{
            "node_id": "analysis",
            "action": "analyze_reference_video",
            "node_revision": 4,
            "payload": {"media_path": "media/reference.mp4"},
        }],
    }
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(workflow_run_client.post, base, json=body)
        assert entered.wait(2)
        second = executor.submit(workflow_run_client.post, base, json=body)
        second_response = second.result(timeout=2)
        release.set()
        first_response = first.result(timeout=2)

    assert first_response.status_code == second_response.status_code == 200
    assert calls == 1


def test_workflow_definite_task_envelope_rejection_finishes_action(
    workflow_run_client: TestClient, monkeypatch,
) -> None:
    from novelvideo.freezone import canvas_store
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.task_backend.envelope import InvalidTaskEnvelope

    canvas = canvas_store.default_canvas_payload(project_id="proj_demo", actor_id="u-alice")
    canvas["nodes"] = [{"id": "plan", "type": "scriptNode",
                        "data": {"remixRole": "production_plan"}}]
    path = canvas_store.canvas_path(workflow_run_client.state_dir, "default")
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas_store.atomic_write_json(path, canvas)
    attempts = []

    class RejectingBackend:
        async def enqueue_project_task(self, _ctx, **kwargs):
            attempts.append(kwargs)
            raise InvalidTaskEnvelope()

    monkeypatch.setattr(freezone, "get_task_backend", lambda: RejectingBackend())
    url = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    body = {"idempotency_key": "rejected-plan", "actions": [{
        "node_id": "plan", "action": "generate_production_plan_draft",
        "node_revision": 1, "payload": {"topic": "黑咖啡"},
    }]}

    first = workflow_run_client.post(url, json=body)
    assert first.status_code == 200, first.text
    action = first.json()["data"]["actions"][0]
    assert action["status"] == "failed"
    operation = RemixOperationStore(
        workflow_run_client.state_dir, "proj_demo"
    ).get(action["processing_operation_id"])
    assert operation["status"] == "failed"
    assert operation["failure_reason"] == "TASK_ENVELOPE_INVALID"

    repeated = workflow_run_client.post(url, json=body)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["data"]["actions"][0]["status"] == "failed"
    assert len(attempts) == 1


def test_workflow_envelope_rejection_preserves_concurrent_cancellation(
    workflow_run_client: TestClient, monkeypatch,
) -> None:
    from novelvideo.freezone import canvas_store
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.task_backend.envelope import InvalidTaskEnvelope

    canvas = canvas_store.default_canvas_payload(project_id="proj_demo", actor_id="u-alice")
    canvas["nodes"] = [{"id": "plan", "type": "scriptNode",
                        "data": {"remixRole": "production_plan"}}]
    path = canvas_store.canvas_path(workflow_run_client.state_dir, "default")
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas_store.atomic_write_json(path, canvas)
    operations = RemixOperationStore(workflow_run_client.state_dir, "proj_demo")

    class CancellingBackend:
        async def enqueue_project_task(self, _ctx, **kwargs):
            import novelvideo.freezone.remix.operations as operations_module

            submitted = operations.get(kwargs["payload"]["operation_id"])
            monkeypatch.setattr(
                operations_module, "time",
                SimpleNamespace(time=lambda: submitted["submission_claimed_at"] + 120),
            )
            operations.claim_cancellation(
                kwargs["payload"]["operation_id"],
                reason="user_cancelled", owner="user-request",
            )
            raise InvalidTaskEnvelope()

    monkeypatch.setattr(freezone, "get_task_backend", lambda: CancellingBackend())
    response = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
        json={"actions": [{"node_id": "plan", "action": "generate_production_plan_draft",
                           "node_revision": 1, "payload": {"topic": "黑咖啡"}}]},
    )

    assert response.status_code == 200, response.text
    action = response.json()["data"]["actions"][0]
    operation = operations.get(action["processing_operation_id"])
    assert operation["cancel_reason"] == "user_cancelled"
    assert operation["status"] == "pending_reconciliation"
    assert action["status"] != "failed"


def test_workflow_plan_draft_crosses_real_signed_inline_backend(
    workflow_run_client: TestClient, monkeypatch,
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.ports import registry
    from novelvideo.ports.local.tasks import InlineTaskBackend, InMemoryCancellationStore
    from novelvideo.project_context import ProjectContext
    from novelvideo.task_backend.consumer import TaskEnvelopeConsumer
    from novelvideo.task_backend.runners import remix as runner
    from novelvideo.task_state import TaskStateManager
    from test_task_envelope_producer import FakeAuthz, KEYRING, NOW, _producer

    project_dir = workflow_run_client.state_dir
    ctx = ProjectContext(
        "proj_demo", "demo", "user", "u-alice", "alice", "u-alice", "alice",
        (("user", "u-alice"),), "editor", "local", project_dir, project_dir,
        project_dir / "_runtime", True,
    )

    async def resolve(_project, _user, **_kwargs):
        return ctx, "alice", "demo", project_dir, str(project_dir)

    monkeypatch.setattr(freezone, "_resolve_freezone_project", resolve)
    manager = TaskStateManager()
    monkeypatch.setattr(registry, "_PORTS", dict(registry._PORTS))
    registry.register_port("cancellation_store", InMemoryCancellationStore())
    monkeypatch.setattr("novelvideo.task_state._task_manager", manager)
    monkeypatch.setattr("novelvideo.ports.local.tasks.get_task_manager", lambda: manager)
    authz = FakeAuthz()
    backend = InlineTaskBackend(
        producer=_producer(authz),
        consumer=TaskEnvelopeConsumer(keyring=KEYRING, authz=authz, clock=lambda: NOW),
    )
    monkeypatch.setattr(freezone, "get_task_backend", lambda: backend)
    canvas = canvas_store.default_canvas_payload(project_id="proj_demo", actor_id="u-alice")
    canvas["nodes"] = [{"id": "plan", "type": "scriptNode",
                        "data": {"remixRole": "production_plan"}}]
    path = canvas_store.canvas_path(project_dir, "default")
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas_store.atomic_write_json(path, canvas)
    calls = []

    async def generate(**kwargs):
        calls.append(kwargs)
        return {
            "schema_version": 2, "goal": kwargs["topic"],
            "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
            "beats": [{"id": "hook", "purpose": "开场", "duration_frames": 90,
                       "turns": []}],
            "resources": [], "placements": [], "provenance": {},
        }

    monkeypatch.setattr(runner, "generate_production_plan_draft", generate)
    url = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    body = {"idempotency_key": "signed-workflow-plan", "actions": [{
        "node_id": "plan", "action": "generate_production_plan_draft",
        "node_revision": 1, "payload": {"topic": "作品目标"},
    }]}
    with workflow_run_client as client:
        accepted = client.post(url, json=body)
        assert accepted.status_code == 200, accepted.text
        action = accepted.json()["data"]["actions"][0]
        operation_id = action["processing_operation_id"]
        deadline = time.monotonic() + 5
        state = None
        while time.monotonic() < deadline:
            state = manager.get_task_for_project(
                ctx, "remix_generate_production_plan_draft", 0, scope=operation_id,
            )
            if state is not None and state.status in {"completed", "failed"}:
                break
            time.sleep(0.02)
        assert state is not None and state.status == "completed", state
        replay = client.post(url, json=body)
        assert replay.status_code == 200, replay.text

    assert len(calls) == 1
    operation = RemixOperationStore(project_dir, "proj_demo").get(operation_id)
    assert len(operation["output_refs"]) == 1
    ref = operation["output_refs"][0]
    artifact = RemixArtifactStore(project_dir, "proj_demo").read(
        ref["artifact_id"], ref["revision"],
    )
    assert artifact["schema"] == "production_plan.v2"


def test_workflow_processing_persists_operation_before_ambiguous_enqueue_and_reconciles_task(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.workflow_runs import list_workflow_runs

    media = workflow_run_client.state_dir / "media" / "reference.mp4"
    media.parent.mkdir()
    media.write_bytes(b"reference")
    accepted_task = SimpleNamespace(
        task_id="accepted-before-timeout", task_type="remix_analyze_reference_video",
        status="pending", scope=None,
    )

    class AmbiguousBackend:
        async def enqueue_project_task(self, _ctx, **kwargs):
            accepted_task.scope = kwargs["scope"]
            raise TimeoutError("queue accepted but response was lost")

    monkeypatch.setattr(freezone, "get_task_backend", lambda: AmbiguousBackend())
    body = {
        "idempotency_key": "workflow-ambiguous-enqueue",
        "actions": [{
            "node_id": "analysis", "action": "analyze_reference_video",
            "node_revision": 4, "payload": {"media_path": "media/reference.mp4"},
        }],
    }
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    with pytest.raises(TimeoutError, match="response was lost"):
        workflow_run_client.post(base, json=body)

    run = list_workflow_runs(
        project_dir=workflow_run_client.state_dir, canvas_id="default"
    )[0]
    action = run["actions"][0]
    assert action["processing_operation_id"]
    operation = RemixOperationStore(workflow_run_client.state_dir, "proj_demo").get(
        action["processing_operation_id"]
    )
    assert operation["status"] == "pending_reconciliation"
    assert operation["task_id"] == ""

    monkeypatch.setattr(
        freezone, "get_task_manager",
        lambda: SimpleNamespace(get_task_for_project=lambda *args, **kwargs: accepted_task),
    )
    recovered = workflow_run_client.post(base, json=body)

    assert recovered.status_code == 200
    recovered_action = recovered.json()["data"]["actions"][0]
    assert recovered_action["processing_operation_id"] == operation["operation_id"]
    assert recovered_action["status"] == "running"
    assert RemixOperationStore(workflow_run_client.state_dir, "proj_demo").get(
        operation["operation_id"]
    )["task_id"] == accepted_task.task_id


def test_workflow_processing_recovers_task_after_crash_and_submission_lease_expiry(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    from novelvideo.api.routes import freezone
    import novelvideo.freezone.remix.operations as operations_module
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.workflow_runs import list_workflow_runs

    class SimulatedProcessCrash(BaseException):
        pass

    now = [1_000.0]
    monkeypatch.setattr(operations_module.time, "time", lambda: now[0])
    media = workflow_run_client.state_dir / "media" / "reference.mp4"
    media.parent.mkdir()
    media.write_bytes(b"reference")
    accepted_task = SimpleNamespace(
        task_id="accepted-before-crash", task_type="remix_analyze_reference_video",
        status="pending", scope=None,
    )

    class CrashAfterAcceptBackend:
        async def enqueue_project_task(self, _ctx, **kwargs):
            accepted_task.scope = kwargs["scope"]
            raise SimulatedProcessCrash()

    monkeypatch.setattr(freezone, "get_task_backend", lambda: CrashAfterAcceptBackend())
    body = {
        "idempotency_key": "workflow-crash-window",
        "actions": [{
            "node_id": "analysis", "action": "analyze_reference_video",
            "node_revision": 4, "payload": {"media_path": "media/reference.mp4"},
        }],
    }
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    with pytest.raises(SimulatedProcessCrash):
        workflow_run_client.post(base, json=body)

    action = list_workflow_runs(
        project_dir=workflow_run_client.state_dir, canvas_id="default"
    )[0]["actions"][0]
    operation = RemixOperationStore(workflow_run_client.state_dir, "proj_demo").get(
        action["processing_operation_id"]
    )
    assert operation["status"] == "pending"
    assert operation["submission_owner"]

    now[0] += 61
    monkeypatch.setattr(
        freezone, "get_task_manager",
        lambda: SimpleNamespace(get_task_for_project=lambda *args, **kwargs: accepted_task),
    )
    recovered = workflow_run_client.post(base, json=body)

    assert recovered.status_code == 200
    assert recovered.json()["data"]["actions"][0]["status"] == "running"
    durable = RemixOperationStore(workflow_run_client.state_dir, "proj_demo").get(
        operation["operation_id"]
    )
    assert durable["task_id"] == accepted_task.task_id
    assert durable["submission_bound_fence"] == operation["submission_fence"]


def test_workflow_render_manifest_same_key_does_not_reuse_after_media_replacement(
    workflow_run_client: TestClient,
) -> None:
    import hashlib

    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.freezone.workflow_runs import update_workflow_run

    for path in (
        workflow_run_client.state_dir / "media/background.png",
        workflow_run_client.state_dir / "media/narration.wav",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"media")
    media_hash = "sha256:" + hashlib.sha256(b"media").hexdigest()
    manifest = RemixArtifactStore(workflow_run_client.state_dir, "proj_demo").create(
        "composition_manifest.v1",
        {
            "output_spec": {"width": 1080, "height": 1920, "fps": 30, "duration_frames": 30},
            "media_bindings": [
                {"media_id": "background", "kind": "image", "project_path": "media/background.png", "media_hash": media_hash},
                {"media_id": "narration", "kind": "audio", "project_path": "media/narration.wav", "media_hash": media_hash},
            ],
            "layers": [{"media_id": "background", "start_frame": 0, "end_frame": 30, "z_order": 0}],
            "subtitle_events": [],
            "audio_events": [{"media_id": "narration", "role": "narration", "start_frame": 0, "end_frame": 30}],
            "manual_overrides": [],
            "source_versions": [],
        },
        [],
    )
    body = {
        "idempotency_key": "workflow-render-reuse",
        "actions": [{
            "node_id": "render", "action": "render_manifest", "node_revision": 3,
            "payload": {"manifest_ref": {"artifact_id": manifest["id"], "revision": 1}},
        }],
    }
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    first = workflow_run_client.post(base, json=body).json()["data"]
    action = first["actions"][0]
    operations = RemixOperationStore(workflow_run_client.state_dir, "proj_demo")
    operation = operations.get(action["processing_operation_id"])
    output = workflow_run_client.state_dir / "freezone/remix_renders/workflow-reused.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"render")
    operations.complete(
        operation["operation_id"],
        output_refs=[{"project_path": output.relative_to(workflow_run_client.state_dir).as_posix()}],
        current_input_refs=operation["input_refs"],
        current_node_revision=3,
    )
    update_workflow_run(
        project_dir=workflow_run_client.state_dir,
        canvas_id="default",
        run_id=first["run_id"],
        action_updates=[{"node_id": "render", "action": "render_manifest", "status": "pending"}],
    )

    (workflow_run_client.state_dir / "media/narration.wav").write_bytes(b"replacement")
    repeated = workflow_run_client.post(base, json=body)

    assert repeated.status_code == 200
    repeated_run = repeated.json()["data"]
    assert repeated_run["status"] == "running"
    assert repeated_run["actions"][0]["status"] == "blocked"
    assert repeated_run["actions"][0]["artifact_status"] == "stale"
    assert repeated_run["actions"][0]["error"] == "input_media_changed"
    assert repeated_run["actions"][0]["result_ref"] == {
        "project_path": output.relative_to(workflow_run_client.state_dir).as_posix()
    }
    durable = operations.get(operation["operation_id"])
    assert durable["status"] == "stale"
    assert durable["stale_reason"] == "input_media_changed"
    assert durable["output_reusable"] is False
    assert durable["input_media_hashes"] == [media_hash]
    assert len(workflow_run_client.enqueued_tasks) == 1

    for _ in range(2):
        polled = workflow_run_client.get(base).json()["data"]["runs"][0]
        assert polled["status"] == "running"
        assert polled["actions"][0]["status"] == "blocked"
        assert polled["actions"][0]["artifact_status"] == "stale"
        assert polled["actions"][0]["error"] == "input_media_changed"
        assert len(workflow_run_client.enqueued_tasks) == 1


def test_workflow_render_does_not_release_result_when_media_changes_after_render(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    import hashlib

    from novelvideo.api.routes import freezone
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.remix.store import RemixArtifactStore

    for path in (
        workflow_run_client.state_dir / "media/background.png",
        workflow_run_client.state_dir / "media/narration.wav",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"media")
    media_hash = "sha256:" + hashlib.sha256(b"media").hexdigest()
    manifest = RemixArtifactStore(workflow_run_client.state_dir, "proj_demo").create(
        "composition_manifest.v1",
        {
            "output_spec": {"width": 1080, "height": 1920, "fps": 30, "duration_frames": 30},
            "media_bindings": [
                {"media_id": "background", "kind": "image", "project_path": "media/background.png", "media_hash": media_hash},
                {"media_id": "narration", "kind": "audio", "project_path": "media/narration.wav", "media_hash": media_hash},
            ],
            "layers": [{"media_id": "background", "start_frame": 0, "end_frame": 30, "z_order": 0}],
            "subtitle_events": [],
            "audio_events": [{"media_id": "narration", "role": "narration", "start_frame": 0, "end_frame": 30}],
            "manual_overrides": [], "source_versions": [],
        },
        [],
    )
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    created = workflow_run_client.post(base, json={"actions": [{
        "node_id": "render", "action": "render_manifest", "node_revision": 3,
        "payload": {"manifest_ref": {"artifact_id": manifest["id"], "revision": 1}},
    }]}).json()["data"]
    action = created["actions"][0]
    operations = RemixOperationStore(workflow_run_client.state_dir, "proj_demo")
    operation = operations.get(action["processing_operation_id"])
    relative = "freezone/remix_renders/post-render-mutation.mp4"
    output = workflow_run_client.state_dir / relative
    output.parent.mkdir(parents=True)
    output.write_bytes(b"render")
    operations.publish_result_unverified(
        operation["operation_id"], output_refs=[{"project_path": relative}]
    )
    task = SimpleNamespace(
        task_id=operation["task_id"], task_type="remix_render", status="completed",
        progress=1.0, current_task="completed", episode=0, beat_num=None,
        scope=action["job_id"], result={"output_path": relative}, error=None,
    )
    monkeypatch.setattr(
        freezone, "get_task_manager",
        lambda: SimpleNamespace(list_tasks_for_project=lambda _ctx: [task]),
    )
    (workflow_run_client.state_dir / "media/background.png").write_bytes(
        b"changed-after-render"
    )

    polled = workflow_run_client.get(base).json()["data"]["runs"][0]

    assert polled["actions"][0]["status"] == "blocked"
    assert polled["actions"][0]["artifact_status"] == "stale"
    assert polled["actions"][0]["error"] == "input_media_changed"
    assert polled["actions"][0]["result_ref"] == {"project_path": relative}
    durable = operations.get(operation["operation_id"])
    assert durable["status"] == "stale"
    assert durable["output_refs"] == [{"project_path": relative}]
    assert len(workflow_run_client.enqueued_tasks) == 1


def test_workflow_run_processing_action_requires_exact_registered_payload(
    workflow_run_client: TestClient,
) -> None:
    response = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
        json={"actions": [{
            "node_id": "analysis", "action": "analyze_reference_video", "node_revision": 1,
            "payload": {"media_path": "media/reference.mp4", "command": "echo unsafe"},
        }]},
    )

    assert response.status_code == 400
    assert "requires exactly" in response.text


def test_workflow_run_advances_registered_actions_in_dependency_order(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.remix.store import RemixArtifactStore

    media = workflow_run_client.state_dir / "media" / "reference.mp4"
    media.parent.mkdir(exist_ok=True)
    media.write_bytes(b"reference")
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    created = workflow_run_client.post(base, json={"actions": [
        {"node_id": "first", "action": "analyze_reference_video", "node_revision": 1,
         "payload": {"media_path": "media/reference.mp4"}},
        {"node_id": "second", "action": "analyze_reference_video", "node_revision": 1,
         "depends_on": ["first"], "payload": {"media_path": "media/reference.mp4"}},
    ]}).json()["data"]
    assert len(workflow_run_client.enqueued_tasks) == 1
    first = created["actions"][0]
    artifact = RemixArtifactStore(workflow_run_client.state_dir, "proj_demo").create(
        "reference_evidence.v1",
        {"media_hash": "sha256:" + "0" * 64, "time_base": "1/1000", "duration_us": 1,
         "cuts": [], "representative_frames": []}, [],
    )
    RemixOperationStore(workflow_run_client.state_dir, "proj_demo").publish_result_unverified(
        first["processing_operation_id"],
        output_refs=[{"artifact_id": artifact["id"], "revision": 1}],
    )
    task = SimpleNamespace(
        task_id=RemixOperationStore(workflow_run_client.state_dir, "proj_demo").get(
            first["processing_operation_id"]
        )["task_id"],
        task_type=first["task_type"], status="completed", progress=1.0,
        current_task="completed", episode=0, beat_num=None, scope=first["job_id"],
        result={"artifact_ref": {"artifact_id": artifact["id"], "revision": 1}}, error=None,
    )
    monkeypatch.setattr(
        freezone, "get_task_manager",
        lambda: SimpleNamespace(list_tasks_for_project=lambda _ctx: [task]),
    )

    advanced = workflow_run_client.get(base).json()["data"]["runs"][0]

    assert len(workflow_run_client.enqueued_tasks) == 2
    assert advanced["actions"][0]["status"] == "completed"
    assert advanced["actions"][0]["result_ref"]["artifact_id"] == artifact["id"]
    assert advanced["actions"][1]["status"] == "running"


def test_processing_review_cannot_be_bypassed_by_workflow_patch(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.remix.store import RemixArtifactStore

    media = workflow_run_client.state_dir / "media" / "reference.mp4"
    media.parent.mkdir(exist_ok=True)
    media.write_bytes(b"reference")
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    created = workflow_run_client.post(base, json={"actions": [
        {"node_id": "first", "action": "analyze_reference_video", "node_revision": 1,
         "payload": {"media_path": "media/reference.mp4"}},
        {"node_id": "second", "action": "analyze_reference_video", "node_revision": 1,
         "depends_on": ["first"], "payload": {"media_path": "media/reference.mp4"}},
    ]}).json()["data"]
    first = created["actions"][0]
    operations = RemixOperationStore(workflow_run_client.state_dir, "proj_demo")
    operation = operations.get(first["processing_operation_id"])
    artifact = RemixArtifactStore(workflow_run_client.state_dir, "proj_demo").create(
        "reference_evidence.v1",
        {"media_hash": "sha256:" + "0" * 64, "time_base": "1/1000", "duration_us": 1,
         "cuts": [], "representative_frames": []}, [],
    )
    ref = {"artifact_id": artifact["id"], "revision": 1}
    operations.publish_result_unverified(operation["operation_id"], output_refs=[ref])
    task = SimpleNamespace(
        task_id=operation["task_id"], task_type=first["task_type"], status="completed",
        progress=1.0, current_task="completed", episode=0, beat_num=None,
        scope=first["job_id"], result={"status": "needs_review", "artifact_ref": ref}, error=None,
    )
    monkeypatch.setattr(
        freezone, "get_task_manager",
        lambda: SimpleNamespace(list_tasks_for_project=lambda _ctx: [task]),
    )

    reviewed = workflow_run_client.get(base).json()["data"]["runs"][0]
    assert reviewed["actions"][0]["status"] == "blocked"
    assert reviewed["actions"][0]["result_ref"] == ref
    assert len(workflow_run_client.enqueued_tasks) == 1

    bypass = workflow_run_client.patch(
        f"{base}/{created['run_id']}",
        json={"status": "completed", "action_updates": [{
            "node_id": "first", "action": "analyze_reference_video", "status": "completed",
        }]},
    )

    assert bypass.status_code == 400
    current = workflow_run_client.get(base).json()["data"]["runs"][0]
    assert current["actions"][0]["status"] == "blocked"
    assert current["actions"][1]["status"] == "pending"
    assert len(workflow_run_client.enqueued_tasks) == 1


def test_metered_recipe_admission_failure_submits_no_processing_task(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    from novelvideo.api.routes import freezone

    media = workflow_run_client.state_dir / "media" / "reference.mp4"
    media.parent.mkdir(exist_ok=True)
    media.write_bytes(b"reference")
    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    monkeypatch.setattr(
        freezone, "create_agent_product_operation",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("admission failed")),
    )

    with pytest.raises(RuntimeError, match="admission failed"):
        workflow_run_client.post(
            "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
            json={"actions": [
                {"node_id": "analysis", "action": "analyze_reference_video", "node_revision": 1,
                 "payload": {"media_path": "media/reference.mp4"}},
                {"node_id": "image", "action": "generate_image", "recipe_id": "product-image",
                 "recipe_version": "1", "generation_attempt_id": "attempt-1"},
            ]},
        )

    assert workflow_run_client.enqueued_tasks == []


def test_workflow_run_api_reuses_idempotent_creation(
    workflow_run_client: TestClient,
) -> None:
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    request_body = {
        "actions": [{"node_id": "image-1", "action": "generate_image"}],
        "idempotency_key": "canvas-run:request-1",
    }

    first = workflow_run_client.post(base, json=request_body)
    duplicate = workflow_run_client.post(base, json=request_body)

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert duplicate.json()["data"]["run_id"] == first.json()["data"]["run_id"]
    assert len(workflow_run_client.get(base).json()["data"]["runs"]) == 1


def test_workflow_run_api_rejects_idempotency_key_rebinding(
    workflow_run_client: TestClient,
) -> None:
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    first = workflow_run_client.post(
        base,
        json={
            "actions": [{"node_id": "image-1", "action": "generate_image"}],
            "idempotency_key": "canvas-run:request-1",
        },
    )
    conflict = workflow_run_client.post(
        base,
        json={
            "actions": [{"node_id": "video-1", "action": "generate_video"}],
            "idempotency_key": "canvas-run:request-1",
        },
    )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert "different request" in conflict.json()["detail"]


def test_workflow_run_api_rejects_competing_runner(
    workflow_run_client: TestClient,
) -> None:
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    first = workflow_run_client.post(
        base,
        json={
            "actions": [{"node_id": "image-1", "action": "generate_image"}],
            "runner_id": "runner-one",
        },
    )
    competing = workflow_run_client.post(
        base,
        json={
            "actions": [{"node_id": "image-2", "action": "generate_image"}],
            "runner_id": "runner-two",
        },
    )

    assert first.status_code == 200
    assert competing.status_code == 409


def test_workflow_draft_api_lifecycle(workflow_run_client: TestClient) -> None:
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-drafts"
    compiled = _valid_draft_compiled()
    created_response = workflow_run_client.post(
        base,
        json={
            "intent": {"skill_id": "video-ad", "user_goal": "广告"},
            "compiled": compiled,
        },
    )
    assert created_response.status_code == 200
    created = created_response.json()["data"]
    assert "agent_credit_estimate" not in created
    assert "agent_planning_charge" not in created
    assert "billing" not in created

    patched_response = workflow_run_client.patch(
        f"{base}/{created['draft_id']}",
        json={
            "expected_revision": 1,
            "intent": {
                "skill_id": "video-ad",
                "user_goal": "广告",
                "items": ["开场"],
            },
            "compiled": compiled,
            "last_changes": {"items": ["开场"]},
        },
    )
    assert patched_response.status_code == 200
    patched = patched_response.json()["data"]
    assert patched["revision"] == 2

    claimed_response = workflow_run_client.post(
        f"{base}/{created['draft_id']}/claim",
        json={"revision": 2},
    )
    assert claimed_response.json()["data"]["status"] == "confirming"
    assert claimed_response.json()["data"]["task_id"] == "workflow-task-1"
    assert claimed_response.json()["data"]["root_task_id"] == "workflow-task-1"

    finished_response = workflow_run_client.post(
        f"{base}/{created['draft_id']}/finish",
        json={"outcome": "confirmed"},
    )
    assert finished_response.status_code == 403
    assert (
        workflow_run_client.get(f"{base}/{created['draft_id']}").json()["data"][
            "status"
        ]
        == "confirming"
    )


def test_workflow_draft_cancel_prevents_later_canvas_creation(
    workflow_run_client: TestClient,
) -> None:
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-drafts"
    created = workflow_run_client.post(
        base,
        json={
            "intent": {"skill_id": "video-ad", "user_goal": "广告"},
            "compiled": _valid_draft_compiled(),
        },
    ).json()["data"]
    path = f"{base}/{created['draft_id']}"

    stale = workflow_run_client.post(
        f"{path}/cancel", json={"expected_revision": 2}
    )
    assert stale.json()["status"] == "workflow_draft_revision_conflict"
    cancelled = workflow_run_client.post(
        f"{path}/cancel", json={"expected_revision": 1}
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["status"] == "cancelled"
    assert workflow_run_client.get(path).json()["data"]["status"] == "cancelled"
    claim = workflow_run_client.post(f"{path}/claim", json={"revision": 1})
    assert claim.json()["status"] == "workflow_draft_not_confirmable"
    assert workflow_run_client.enqueued_tasks == []


@pytest.mark.parametrize("method", ["post", "patch"])
def test_workflow_draft_revalidates_untrusted_compiled_result(
    workflow_run_client, method
):
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-drafts"
    body = {"intent": {}, "compiled": _valid_draft_compiled()}
    target = base
    if method == "patch":
        created = workflow_run_client.post(base, json=body).json()["data"]
        target = f"{base}/{created['draft_id']}"
        body["expected_revision"] = 1
    body["compiled"]["plan"]["nodes"] = []
    response = getattr(workflow_run_client, method)(target, json=body)
    assert response.status_code == 400
    if method == "patch":
        assert workflow_run_client.get(target).json()["data"]["revision"] == 1


def test_workflow_draft_discards_agent_validation_metadata(workflow_run_client):
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-drafts"
    compiled = _valid_draft_compiled()
    compiled.update({"node_count": 999, "preflight": {"trusted": True}})
    response = workflow_run_client.post(base, json={"intent": {}, "compiled": compiled})
    assert response.status_code == 200, response.json()
    from novelvideo.freezone.workflow_drafts import read_workflow_draft

    draft, _ = read_workflow_draft(
        project_dir=workflow_run_client.state_dir,
        canvas_id="default",
        draft_id=response.json()["data"]["draft_id"],
    )
    assert draft["compiled"]["node_count"] == 1
    assert "trusted" not in draft["compiled"]["preflight"]


@pytest.mark.parametrize(
    "body_patch",
    [
        {"run_after_create": "false"},
        {"intent": {"plan": {"nodes": []}}},
    ],
)
def test_workflow_draft_rejects_ambiguous_execution_and_mismatched_intent(
    workflow_run_client, body_patch
):
    response = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-drafts",
        json={"intent": {}, "compiled": _valid_draft_compiled(), **body_patch},
    )
    assert response.status_code == 400


def test_workflow_claim_rechecks_authenticated_catalog(
    workflow_run_client, monkeypatch
):
    from novelvideo.freezone.agent_workflows import catalog

    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-drafts"
    body = {"intent": {}, "compiled": _valid_draft_compiled()}
    created = workflow_run_client.post(base, json=body).json()["data"]
    observed = []

    def revoked_catalog(username, kind):
        observed.append((username, kind))
        return []

    monkeypatch.setenv("DRAMACLAW_USERNAME", "another-user")
    monkeypatch.setattr(catalog, "list_user_agent_config_items", revoked_catalog)
    response = workflow_run_client.post(
        f"{base}/{created['draft_id']}/claim", json={"revision": 1}
    )
    assert response.status_code == 400
    assert observed == [("alice", "skills"), ("alice", "recipes")]
    assert (
        workflow_run_client.get(f"{base}/{created['draft_id']}").json()["data"][
            "status"
        ]
        == "ready"
    )


@pytest.mark.parametrize(
    "finish_body,status",
    [
        ({"outcome": "confirmed", "task_id": "workflow-task-1", "revision": 1}, 403),
        ({"outcome": "submitted", "revision": 1}, 400),
        ({"outcome": "submitted", "task_id": "workflow-task-1"}, 400),
        ({"outcome": "submitted", "task_id": "old-task", "revision": 1}, 400),
        ({"outcome": "submitted", "task_id": "workflow-task-1", "revision": 2}, 400),
    ],
)
def test_workflow_finish_rejects_unbound_or_untrusted_updates(
    workflow_run_client, finish_body, status
):
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-drafts"
    draft = workflow_run_client.post(
        base, json={"intent": {}, "compiled": _valid_draft_compiled()}
    ).json()["data"]
    target = f"{base}/{draft['draft_id']}"
    claimed = workflow_run_client.post(f"{target}/claim", json={"revision": 1})
    assert claimed.status_code == 200
    response = workflow_run_client.post(f"{target}/finish", json=finish_body)
    assert response.status_code == status
    assert workflow_run_client.get(target).json()["data"]["status"] == "confirming"


def test_workflow_run_list_reconciles_completed_project_task(
    workflow_run_client: TestClient,
    monkeypatch,
) -> None:
    from novelvideo.api.routes import freezone

    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    created = workflow_run_client.post(
        base,
        json={"actions": [{"node_id": "image-1", "action": "generate_image"}]},
    ).json()["data"]
    workflow_run_client.patch(
        f"{base}/{created['run_id']}",
        json={
            "action_updates": [
                {
                    "node_id": "image-1",
                    "action": "generate_image",
                    "status": "running",
                    "task_key": "task:freezone_image:project:proj_demo:0:job-one",
                }
            ]
        },
    )
    task = SimpleNamespace(
        task_type="freezone_image",
        status="completed",
        progress=1.0,
        current_task="completed",
        episode=0,
        beat_num=None,
        scope="job-one",
        result={"image_url": "https://cdn.example.test/image.png"},
        error=None,
    )
    monkeypatch.setattr(
        freezone,
        "get_task_manager",
        lambda: SimpleNamespace(list_tasks_for_project=lambda _ctx: [task]),
    )

    runs = workflow_run_client.get(base).json()["data"]["runs"]

    reconciled = next(item for item in runs if item["run_id"] == created["run_id"])
    assert reconciled["status"] == "completed"
    assert reconciled["actions"][0]["artifact_status"] == "valid"


def test_workflow_run_list_reconciles_taskless_text_from_saved_canvas(
    workflow_run_client: TestClient,
    monkeypatch,
) -> None:
    from novelvideo.api.routes import freezone

    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    created = workflow_run_client.post(
        base,
        json={
            "actions": [
                {"node_id": "text-1", "action": "generate_text"},
            ]
        },
    ).json()["data"]
    patched = workflow_run_client.patch(
        f"{base}/{created['run_id']}",
        json={
            "action_updates": [
                {
                    "node_id": "text-1",
                    "action": "generate_text",
                    "status": "completed",
                }
            ],
            "status": "completed",
        },
    ).json()["data"]
    assert patched["status"] == "running"
    assert patched["actions"][0]["status"] == "running"

    monkeypatch.setattr(
        freezone.canvas_store,
        "read_canvas",
        lambda _project_dir, _canvas_id: {
            "nodes": [
                {
                    "id": "text-1",
                    "type": "textAnnotationNode",
                    "data": {
                        "content": "A durable generated result",
                        "workflowTextGenerated": True,
                    },
                }
            ],
            "edges": [],
        },
    )

    runs = workflow_run_client.get(base).json()["data"]["runs"]

    reconciled = next(item for item in runs if item["run_id"] == created["run_id"])
    assert reconciled["status"] == "completed"
    assert reconciled["resumable"] is False
    assert reconciled["actions"][0]["status"] == "completed"
    assert reconciled["actions"][0]["artifact_status"] == "valid"


def test_recipe_result_does_not_use_media_task_id_as_model_evidence(
    workflow_run_client: TestClient,
    monkeypatch,
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.agent_product_operations import (
        read_agent_product_operation,
    )

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    created = workflow_run_client.post(
        base,
        json={
            "idempotency_key": "media-proof-is-not-model-proof",
            "actions": [
                {
                    "node_id": "image-1",
                    "action": "generate_image",
                    "recipe_id": "product-image",
                    "recipe_version": "1.0.0",
                    "generation_attempt_id": "attempt-a",
                }
            ],
        },
    ).json()["data"]
    operation_id = created["actions"][0]["product_operation_id"]
    task_key = "task:freezone_image:project:proj_demo:0:media-provider-job"
    workflow_run_client.patch(
        f"{base}/{created['run_id']}",
        json={
            "action_updates": [
                {
                    "node_id": "image-1",
                    "action": "generate_image",
                    "status": "running",
                    "task_key": task_key,
                    "job_id": "media-provider-job",
                }
            ]
        },
    )
    media_task = SimpleNamespace(
        task_type="freezone_image",
        status="completed",
        progress=1.0,
        current_task="completed",
        episode=0,
        beat_num=None,
        scope="media-provider-job",
        result={"image_url": "https://cdn.example.test/image.png"},
        error=None,
    )
    monkeypatch.setattr(
        freezone,
        "get_task_manager",
        lambda: SimpleNamespace(
            list_tasks_for_project=lambda _ctx: [media_task],
            get_task_for_project=lambda *_args, **_kwargs: None,
        ),
    )

    response = workflow_run_client.get(base)

    assert response.status_code == 200
    operation = read_agent_product_operation(
        project_dir=workflow_run_client.state_dir,
        operation_id=operation_id,
    )
    assert operation["status"] == "failed"
    assert operation["model_evidence"] == {}


@pytest.mark.parametrize("waiter_before_delivery", [False, True])
@pytest.mark.parametrize(
    "tampered_field",
    [
        None,
        "kind",
        "id",
        "workflow_run_id",
        "node_id",
        "recipe_id",
        "action.job_id",
        "action.task_type",
        "action.recipe_id",
        "action.recipe_version",
        "action.product_operation_id",
    ],
)
def test_direct_voice_recipe_result_is_delivered_from_completed_audio_task(
    workflow_run_client: TestClient, monkeypatch, waiter_before_delivery, tampered_field
) -> None:
    import asyncio

    from novelvideo.api.routes import freezone
    from novelvideo.freezone.agent_product_operations import (
        AgentProductSettlementPending,
        read_agent_product_operation,
    )
    from novelvideo.task_backend.runners.freezone import (
        _run_freezone_agent_product_async,
    )
    from novelvideo.freezone.workflow_runs import claim_workflow_media_action

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    created = workflow_run_client.post(
        base,
        json={
            "actions": [
                {
                    "node_id": "voice-1",
                    "action": "generate_audio",
                    "recipe_id": "drama-shot-voice",
                    "recipe_version": "1.0.0",
                    "generation_attempt_id": "attempt-voice",
                }
            ]
        },
    ).json()["data"]
    operation_id = created["actions"][0]["product_operation_id"]
    claimed = claim_workflow_media_action(
        project_dir=workflow_run_client.state_dir,
        project_id="proj_demo",
        canvas_id="default",
        node_id="voice-1",
        operation_id=operation_id,
        attempt_id="attempt-voice",
        task_type="freezone_audio_speech",
        fingerprint="a" * 64,
    )
    job_id = claimed["job_id"]
    task_key = f"task:freezone_audio_speech:project:proj_demo:0:{job_id}"
    workflow_run_client.patch(
        f"{base}/{created['run_id']}",
        json={
            "action_updates": [
                {
                    "node_id": "voice-1",
                    "action": "generate_audio",
                    "status": "running",
                    "task_key": task_key,
                    "job_id": job_id,
                }
            ]
        },
    )
    media_task = SimpleNamespace(
        task_type="freezone_audio_speech",
        status="completed",
        progress=1.0,
        current_task="completed",
        episode=0,
        beat_num=None,
        scope=job_id,
        result={"audio_url": "https://cdn.example.test/voice.wav"},
        error=None,
    )
    monkeypatch.setattr(
        freezone,
        "get_task_manager",
        lambda: SimpleNamespace(
            list_tasks_for_project=lambda _ctx: [media_task],
            get_task_for_project=lambda *_args, **_kwargs: None,
        ),
    )

    operation = read_agent_product_operation(
        project_dir=workflow_run_client.state_dir, operation_id=operation_id
    )
    envelope = {
        "task_type": "freezone_agent_recipe_result",
        "__run_task_id": operation["task_id"],
        "payload": {"operation_id": operation_id, "product_kind": "recipe_result"},
    }
    ctx = SimpleNamespace(state_dir=workflow_run_client.state_dir)
    if waiter_before_delivery:
        with pytest.raises(AgentProductSettlementPending) as exc_info:
            asyncio.run(_run_freezone_agent_product_async(envelope, ctx))
        assert exc_info.value.status == "awaiting_delivery"

    response = workflow_run_client.get(base)

    assert response.status_code == 200
    operation = read_agent_product_operation(
        project_dir=workflow_run_client.state_dir, operation_id=operation_id
    )
    assert operation["status"] == "delivered"
    assert operation["result_ref"]["id"] == job_id
    assert operation["model_evidence"] == {}

    if tampered_field:
        with sqlite3.connect(workflow_run_client.state_dir / "data.db") as conn:
            if tampered_field.startswith("action."):
                # Columns come exclusively from the test parameter list above.
                column = tampered_field.removeprefix("action.")
                conn.execute(
                    f"UPDATE workflow_run_actions SET {column} = ? WHERE run_id = ?",
                    ("untrusted", created["run_id"]),
                )
            else:
                receipt = {**operation["result_ref"], tampered_field: "untrusted"}
                conn.execute(
                    "UPDATE freezone_agent_product_operations SET result_ref_json = ? "
                    "WHERE operation_id = ?",
                    (json.dumps(receipt), operation_id),
                )
        with pytest.raises(RuntimeError, match="lacks trusted delivery evidence"):
            asyncio.run(_run_freezone_agent_product_async(envelope, ctx))
        return

    # Starting after delivery and replaying must consume the same trusted receipt.
    result = asyncio.run(_run_freezone_agent_product_async(envelope, ctx))
    assert result["delivery_status"] == "delivered"
    assert result["result_ref"] == operation["result_ref"]
    assert result["model_evidence"] == {}
    assert asyncio.run(_run_freezone_agent_product_async(envelope, ctx)) == result


def test_cancelled_workflow_reconciles_late_recipe_media_result(
    workflow_run_client: TestClient,
    monkeypatch,
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.agent_product_operations import (
        bind_agent_product_model_execution,
        read_agent_product_operation,
    )

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    created = workflow_run_client.post(
        base,
        json={
            "idempotency_key": "late-image-after-cancel",
            "actions": [{
                "node_id": "image-1",
                "action": "generate_image",
                "recipe_id": "product-image",
                "recipe_version": "1.0.0",
                "generation_attempt_id": "attempt-a",
            }],
        },
    ).json()["data"]
    operation_id = created["actions"][0]["product_operation_id"]
    task_key = "task:freezone_image:project:proj_demo:0:late-image-job"
    workflow_run_client.patch(
        f"{base}/{created['run_id']}",
        json={"action_updates": [{
            "node_id": "image-1",
            "action": "generate_image",
            "status": "running",
            "task_key": task_key,
            "job_id": "late-image-job",
        }]},
    )
    bind_agent_product_model_execution(
        project_dir=workflow_run_client.state_dir,
        operation_id=operation_id,
        model_call_id="recipe-compiler:late-image",
        executed_at=1.0,
        source="server_recipe_compiler",
        compile_mode="model",
    )
    monkeypatch.setattr(
        freezone,
        "get_task_manager",
        lambda: SimpleNamespace(
            list_tasks_for_project=lambda _ctx: [],
            get_task_for_project=lambda *_args, **_kwargs: None,
        ),
    )
    cancelled = workflow_run_client.patch(
        f"{base}/{created['run_id']}", json={"status": "cancelled"}
    )
    assert cancelled.status_code == 200

    media_task = SimpleNamespace(
        task_type="freezone_image",
        status="completed",
        progress=1.0,
        current_task="completed",
        episode=0,
        beat_num=None,
        scope="late-image-job",
        result={"image_url": "https://cdn.example.test/late-image.png"},
        error=None,
    )
    monkeypatch.setattr(
        freezone,
        "get_task_manager",
        lambda: SimpleNamespace(
            list_tasks_for_project=lambda _ctx: [media_task],
            get_task_for_project=lambda *_args, **_kwargs: None,
        ),
    )
    listed = workflow_run_client.get(base)

    assert listed.status_code == 200
    operation = read_agent_product_operation(
        project_dir=workflow_run_client.state_dir,
        operation_id=operation_id,
    )
    assert operation["status"] == "delivered"
    assert operation["result_ref"]["id"] == "late-image-job"


@pytest.mark.parametrize(
    ("linked", "unrelated"),
    [(True, False), (True, True), (False, True)],
)
def test_cancelled_workflow_recovers_completed_video_without_persisted_task_key(
    workflow_run_client: TestClient,
    monkeypatch,
    linked: bool,
    unrelated: bool,
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.agent_product_operations import (
        bind_agent_product_model_execution,
        read_agent_product_operation,
    )
    from novelvideo.freezone.history import append_generation_history, build_node_history_record

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    created = workflow_run_client.post(
        base,
        json={
            "idempotency_key": "video-with-missing-task-key",
            "actions": [{
                "node_id": "video-1",
                "action": "generate_video",
                "recipe_id": "product-video",
                "recipe_version": "1.0.0",
                "generation_attempt_id": "attempt-video",
            }],
        },
    ).json()["data"]
    operation_id = created["actions"][0]["product_operation_id"]
    bind_agent_product_model_execution(
        project_dir=workflow_run_client.state_dir,
        operation_id=operation_id,
        model_call_id="recipe-compiler:video",
        executed_at=1.0,
        source="server_recipe_compiler",
        compile_mode="model",
    )
    from novelvideo.api.routes import freezone as freezone_routes

    link_context = SimpleNamespace(
        project_id="proj_demo", state_dir=str(workflow_run_client.state_dir)
    )
    assert freezone_routes._verified_workflow_media_link(
        ctx=link_context,
        project_dir=workflow_run_client.state_dir,
        canvas_id="default",
        node_id="video-1",
        operation_id=operation_id,
        attempt_id="attempt-video",
    ) == {
        "product_operation_id": operation_id,
        "generation_attempt_id": "attempt-video",
    }
    with pytest.raises(HTTPException, match="does not match admitted Recipe attempt"):
        freezone_routes._verified_workflow_media_link(
            ctx=link_context,
            project_dir=workflow_run_client.state_dir,
            canvas_id="default",
            node_id="video-1",
            operation_id=operation_id,
            attempt_id="unrelated-attempt",
        )
    task_key = "task:freezone_video_gen:project:proj_demo:0:video-job"
    result = {"output_url": "/static/projects/proj_demo/video.mp4"}
    monkeypatch.setattr(
        freezone,
        "get_task_manager",
        lambda: SimpleNamespace(
            list_tasks_for_project=lambda _ctx: [],
            get_task_for_project=lambda *_args, **_kwargs: None,
        ),
    )
    assert workflow_run_client.patch(
        f"{base}/{created['run_id']}", json={"status": "cancelled"}
    ).status_code == 200
    if linked:
        append_generation_history(
            project_dir=workflow_run_client.state_dir,
            canvas_id="default",
            node_id="video-1",
            record=build_node_history_record(
                task_type="freezone_video_gen",
                job_id="video-job",
                task_key=task_key,
                status="completed",
                media_type="video",
                result=result,
                extra={
                    "generation_attempt_id": "attempt-video",
                    "product_operation_id": operation_id,
                },
            ),
        )
    if unrelated:
        append_generation_history(
            project_dir=workflow_run_client.state_dir,
            canvas_id="default",
            node_id="video-1",
            record=build_node_history_record(
                task_type="freezone_video_gen",
                job_id="other-video-job",
                task_key="task:freezone_video_gen:project:proj_demo:0:other-video-job",
                status="completed",
                media_type="video",
                result=result,
            ),
        )
    media_task = SimpleNamespace(
        task_type="freezone_video_gen",
        status="completed",
        progress=1.0,
        current_task="completed",
        episode=0,
        beat_num=None,
        scope="video-job",
        result=result,
        error=None,
    )
    tasks = [media_task]
    if unrelated:
        tasks.append(SimpleNamespace(**{**vars(media_task), "scope": "other-video-job"}))
    monkeypatch.setattr(
        freezone,
        "get_task_manager",
        lambda: SimpleNamespace(
            list_tasks_for_project=lambda _ctx: tasks,
            get_task_for_project=lambda *_args, **_kwargs: None,
        ),
    )

    assert workflow_run_client.get(base).status_code == 200
    operation = read_agent_product_operation(
        project_dir=workflow_run_client.state_dir,
        operation_id=operation_id,
    )
    assert operation["status"] == ("delivered" if linked else "reserved")
    if linked:
        assert operation["result_ref"]["id"] == "video-job"


def test_workflow_run_cancel_stops_linked_active_project_task(
    workflow_run_client: TestClient,
    monkeypatch,
) -> None:
    from novelvideo.api.routes import freezone

    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    task_key = "task:freezone_image:project:proj_demo:0:job-one"
    created = workflow_run_client.post(
        base,
        json={"actions": [{"node_id": "image-1", "action": "generate_image"}]},
    ).json()["data"]
    workflow_run_client.patch(
        f"{base}/{created['run_id']}",
        json={
            "action_updates": [
                {
                    "node_id": "image-1",
                    "action": "generate_image",
                    "status": "running",
                    "task_key": task_key,
                }
            ]
        },
    )
    task = SimpleNamespace(
        task_type="freezone_image",
        task_id="task-one",
        status="queued",
        progress=0.0,
        episode=0,
        beat_num=None,
        scope="job-one",
    )
    cancelled_tasks = []

    class FakeTaskBackend:
        async def cancel_project_task(self, _ctx, task_state):
            cancelled_tasks.append(task_state)
            return True

    monkeypatch.setattr(
        freezone,
        "get_task_manager",
        lambda: SimpleNamespace(list_tasks_for_project=lambda _ctx: [task]),
    )
    monkeypatch.setattr(freezone, "get_task_backend", FakeTaskBackend)

    response = workflow_run_client.patch(
        f"{base}/{created['run_id']}",
        json={"status": "cancelled"},
    )

    assert response.status_code == 200
    assert cancelled_tasks == [task]
    assert response.json()["data"]["actions"][0]["status"] == "skipped"


def test_recover_completed_generation_binds_task_after_run_cancelled(
    workflow_run_client: TestClient,
):
    """A completed media task must be bound without reopening the cancelled run."""
    from novelvideo.freezone.workflow_runs import (
        recover_completed_generation_task,
        read_workflow_run,
    )

    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    created = workflow_run_client.post(
        base,
        json={
            "actions": [{
                "node_id": "bgm-1",
                "action": "generate_audio",
                "generation_attempt_id": "attempt-bgm-1",
            }],
        },
    ).json()["data"]
    cancelled = workflow_run_client.patch(
        f"{base}/{created['run_id']}", json={"status": "cancelled"}
    )
    assert cancelled.status_code == 200

    recovered = recover_completed_generation_task(
        project_dir=workflow_run_client.state_dir,
        canvas_id="default",
        run_id=created["run_id"],
        node_id="bgm-1",
        task_key="task:freezone_audio_eleven_music:project:proj_demo:0:bgm-1",
        task_type="freezone_audio_eleven_music",
        job_id="bgm-1",
        result_ref={"value": "/static/projects/proj_demo/bgm-1.mp3"},
    )

    assert recovered["status"] == "cancelled"
    assert recovered["actions"][0]["status"] == "completed"
    assert recovered["actions"][0]["task_key"].endswith(":bgm-1")
    assert read_workflow_run(
        project_dir=workflow_run_client.state_dir,
        canvas_id="default",
        run_id=created["run_id"],
    )["status"] == "cancelled"


def test_bind_generation_task_does_not_depend_on_recipe_operation(
    workflow_run_client: TestClient,
):
    from novelvideo.freezone.workflow_runs import bind_workflow_generation_task

    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    created = workflow_run_client.post(
        base,
        json={
            "actions": [{
                "node_id": "bgm-1",
                "action": "generate_audio",
                "generation_attempt_id": "attempt-bgm-1",
            }],
        },
    ).json()["data"]
    bound = bind_workflow_generation_task(
        project_dir=workflow_run_client.state_dir,
        canvas_id="default",
        run_id=created["run_id"],
        node_id="bgm-1",
        generation_attempt_id="attempt-bgm-1",
        task_key="task:freezone_audio_eleven_music:project:proj_demo:0:bgm-1",
        task_type="freezone_audio_eleven_music",
        job_id="bgm-1",
    )
    action = bound["actions"][0]
    assert action["task_key"].endswith(":bgm-1")
    assert action["task_type"] == "freezone_audio_eleven_music"
    assert action["status"] == "running"


def test_verified_media_link_accepts_saved_canvas_workflow_context(
    workflow_run_client: TestClient,
):
    from novelvideo.api.routes import freezone as freezone_routes

    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    created = workflow_run_client.post(
        base,
        json={
            "actions": [{
                "node_id": "bgm-1",
                "action": "generate_audio",
                "generation_attempt_id": "attempt-bgm-1",
            }],
        },
    ).json()["data"]
    link_context = SimpleNamespace(
        project_id="proj_demo", state_dir=str(workflow_run_client.state_dir)
    )
    assert freezone_routes._verified_workflow_media_link(
        ctx=link_context,
        project_dir=workflow_run_client.state_dir,
        canvas_id="default",
        node_id="bgm-1",
        operation_id="",
        attempt_id="attempt-bgm-1",
        workflow_run_id=created["run_id"],
    ) == {
        "workflow_run_id": created["run_id"],
        "generation_attempt_id": "attempt-bgm-1",
    }


def test_workflow_list_recovers_unlinked_completed_audio_task(
    workflow_run_client: TestClient,
    monkeypatch,
):
    """A node task completed after the browser lost its PATCH is recovered once."""
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.history import append_generation_history, build_node_history_record

    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    created = workflow_run_client.post(
        base,
        json={
            "actions": [{
                "node_id": "bgm-1",
                "action": "generate_audio",
                "generation_attempt_id": "attempt-bgm-1",
            }],
        },
    ).json()["data"]
    workflow_run_client.patch(
        f"{base}/{created['run_id']}", json={"status": "cancelled"}
    )
    task_key = "task:freezone_audio_eleven_music:project:proj_demo:0:bgm-1"
    append_generation_history(
        project_dir=workflow_run_client.state_dir,
        canvas_id="default",
        node_id="bgm-1",
        record=build_node_history_record(
            task_type="freezone_audio_eleven_music",
            job_id="bgm-1",
            task_key=task_key,
            status="completed",
            media_type="audio",
            result={"audio_url": "/static/projects/proj_demo/bgm-1.mp3"},
        ),
    )
    monkeypatch.setattr(
        freezone,
        "get_task_manager",
        lambda: SimpleNamespace(
            # The task state may expire after a successful generation; the
            # node history remains the durable evidence used for recovery.
            list_tasks_for_project=lambda _ctx: [],
            get_task_for_project=lambda *_args, **_kwargs: None,
        ),
    )

    listed = workflow_run_client.get(base).json()["data"]["runs"]
    recovered = next(item for item in listed if item["run_id"] == created["run_id"])
    action = recovered["actions"][0]
    assert action["task_key"] == task_key
    assert action["task_type"] == "freezone_audio_eleven_music"
    assert action["status"] == "completed"
    assert recovered["status"] == "cancelled"


def test_workflow_run_list_cancels_orphaned_failed_record(
    workflow_run_client: TestClient,
    monkeypatch,
) -> None:
    from novelvideo.api.routes import freezone

    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    created = workflow_run_client.post(
        base,
        json={"actions": [{"node_id": "deleted-node", "action": "generate_image"}]},
    ).json()["data"]
    workflow_run_client.patch(
        f"{base}/{created['run_id']}",
        json={
            "status": "failed",
            "action_updates": [
                {
                    "node_id": "deleted-node",
                    "action": "generate_image",
                    "status": "failed",
                }
            ],
        },
    )
    monkeypatch.setattr(
        freezone.canvas_store, "read_canvas", lambda *_args, **_kwargs: None
    )

    runs = workflow_run_client.get(base).json()["data"]["runs"]

    listed = next(item for item in runs if item["run_id"] == created["run_id"])
    assert listed["status"] == "cancelled"
    assert listed["resumable"] is False
    assert listed["metadata"]["cancel_reason"] == "workflow_nodes_deleted"


@pytest.mark.parametrize(
    "receipt_kind", ["recipe_compile_result", "recipe_nonbillable"]
)
@pytest.mark.parametrize("outcome", ["delivered", "cancelled"])
def test_client_cannot_forge_server_recipe_receipt(
    workflow_run_client, receipt_kind, outcome
):
    from novelvideo.freezone.agent_product_operations import (
        read_agent_product_operation,
    )

    client = workflow_run_client
    response = client.post(
        "/api/v1/projects/proj_demo/freezone/agent-product-operations",
        json={
            "product_kind": "recipe_result",
            "generation_session_id": "forged-reuse",
            "canvas_id": "default",
            "artifact_id": "image-1",
            "normalized_inputs_hash": "forged-reuse",
        },
    )
    assert response.status_code == 200
    operation = response.json()["data"]
    operation_id = operation["operation_id"]
    response = client.post(
        f"/api/v1/projects/proj_demo/freezone/agent-product-operations/{operation_id}/finish",
        json={
            "task_id": operation["task_id"],
            "outcome": outcome,
            "result_ref": {
                "kind": receipt_kind,
                "id": operation_id,
                "reason": "timeout_fallback",
                "content": "forged prompt",
            },
            "server_recipe_compile": True,
        },
    )
    assert response.status_code == 400
    stored = read_agent_product_operation(
        project_dir=client.state_dir, operation_id=operation_id
    )
    assert stored["status"] == operation["status"]
    assert stored["result_ref"] == {}


def test_server_workflow_prepare_revise_and_compact_query(workflow_run_client):
    base = "/api/v1/projects/proj_demo/freezone/canvases/canvas_demo/workflow-drafts"
    response = workflow_run_client.post(
        base,
        json={
            "plan": _valid_draft_compiled()["plan"],
            "response_view": "summary",
        },
    )
    assert response.status_code == 200, response.text
    draft = response.json()["data"]
    assert draft["revision"] == 1
    assert not {"plan", "intent", "compiled"} & draft.keys()
    assert draft["preview"]["nodes"] == [
        {
            "id": "brief",
            "name": "广告",
            "stage": "input",
            "node_type": "textAnnotationNode",
        }
    ]
    assert draft["preview"]["recipe_pipelines"] == []
    assert draft["preview"]["node_count"] == 1
    target = f"{base}/{draft['draft_id']}"
    changes = {"step_updates": [{"node_id": "brief", "prompt": "新广告要求"}]}
    revised = workflow_run_client.patch(
        target,
        json={
            "expected_revision": 1,
            "changes": changes,
            "response_view": "summary",
        },
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["data"]["revision"] == 2
    full = workflow_run_client.get(target).json()["data"]
    assert full["compiled"]["plan"]["nodes"][0]["data"]["content"] == "新广告要求"
    assert len(full["preview"]["nodes"]) == 1
    stale = workflow_run_client.patch(
        target, json={"expected_revision": 1, "changes": changes}
    )
    assert stale.json()["status"] == "workflow_draft_revision_conflict"
    summary = workflow_run_client.get(target + "?view=summary").json()["data"]
    assert summary["revision"] == 2
    assert summary["next_action"] == "review_and_confirm"
    assert not {"plan", "intent", "compiled"} & summary.keys()


def test_story_frame_draft_rejects_invalid_target_before_confirmation(
    workflow_run_client, monkeypatch
):
    from novelvideo.api.routes import freezone

    monkeypatch.setattr(freezone.canvas_store, "read_canvas", lambda *_args: {
        "nodes": [
            {"id": "story-group", "type": "groupNode", "data": {
                "storyGroup": True, "interactiveStoryId": "story-1"}},
            {"id": "video-opening", "type": "videoNode", "parentId": "story-group",
             "data": {"storySegmentId": "opening"}},
        ],
    })
    plan = deepcopy(_valid_draft_compiled()["plan"])
    plan["source_context"] = {
        "story_id": "story-1",
        "targets": [{"plan_node_id": "missing-frame", "story_segment_id": "opening",
                     "video_node_id": "missing-video"}],
    }
    response = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/canvas_demo/workflow-drafts",
        json={"plan": plan},
    )
    assert response.status_code == 409
    assert "unknown or duplicate image" in response.text


def test_workflow_claim_rechecks_story_targets_before_task_admission(
    workflow_run_client, monkeypatch
):
    from novelvideo.api.routes import freezone

    async def image_models(*_args, **_kwargs):
        return {"ok": True, "data": [{"id": "story-image-model"}]}

    monkeypatch.setattr(freezone, "freezone_image_models", image_models)
    canvas = {"nodes": [
        {"id": "story-group", "type": "groupNode", "data": {
            "storyGroup": True, "interactiveStoryId": "story-1"}},
        {"id": "video-opening", "type": "videoNode", "parentId": "story-group",
         "data": {"storySegmentId": "opening"}},
    ]}
    monkeypatch.setattr(freezone.canvas_store, "read_canvas", lambda *_args: canvas)
    plan = {
        "schema_version": "freezone_workflow_plan.v1",
        "skill": {"id": "text-to-image-video"},
        "source_context": {"story_id": "story-1", "targets": [
            {"plan_node_id": "frame-opening", "story_segment_id": "opening",
             "video_node_id": "video-opening"},
        ]},
        "nodes": [{"id": "frame-opening", "node_type": "imageGenNode",
                   "stage": "image", "data": {"prompt": "开场分镜", "model": "story-image-model",
                   "workflowCatalog": {"skillId": "text-to-image-video",
                                       "recipeId": "general-image"}}}],
        "edges": [],
    }
    base = "/api/v1/projects/proj_demo/freezone/canvases/canvas_demo/workflow-drafts"
    created = workflow_run_client.post(
        base, json={"plan": plan, "run_after_create": False}
    )
    assert created.status_code == 200, created.text
    draft_id = created.json()["data"]["draft_id"]
    canvas["nodes"][1]["data"]["storySegmentId"] = "rewritten"
    patched = workflow_run_client.patch(
        f"{base}/{draft_id}", json={"expected_revision": 1, "plan": plan}
    )
    assert patched.status_code == 409
    claimed = workflow_run_client.post(
        f"{base}/{draft_id}/claim", json={"revision": 1}
    )
    assert claimed.status_code == 409
    assert "story frame target" in claimed.text
    assert workflow_run_client.get(f"{base}/{draft_id}").json()["data"]["status"] == "ready"


def test_workflow_capabilities_do_not_advertise_headless_execution(workflow_run_client):
    response = workflow_run_client.get(
        "/api/v1/projects/proj_demo/freezone/workflow-capabilities"
    )
    assert response.status_code == 200
    assert response.json()["data"]["capabilities"]["headless_execution"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"step_updates": [{"node_id": "brief", "semanticOutputRole": "input_text"}]},
        {"step_updates": [{"node_id": "absent", "prompt": "changed"}]},
        {"bindings": [{"source": "brief", "target": "brief", "usage": "prompt"}]},
    ],
)
def test_server_invalid_patch_preserves_revision(workflow_run_client, changes):
    base = "/api/v1/projects/proj_demo/freezone/canvases/canvas_demo/workflow-drafts"
    draft = workflow_run_client.post(
        base, json={"plan": _valid_draft_compiled()["plan"]}
    ).json()["data"]
    target = f"{base}/{draft['draft_id']}"
    response = workflow_run_client.patch(
        target, json={"expected_revision": 1, "changes": changes}
    )
    assert response.status_code == 400
    assert response.json()["detail"]["retryable"] is False
    assert workflow_run_client.get(target).json()["data"]["revision"] == 1


@pytest.fixture()
def runtime_workflow_source(monkeypatch):
    from novelvideo.api.routes import freezone, tasks
    from novelvideo.freezone.agent_workflows import catalog

    def items(user, kind):
        assert user == "alice"
        if kind == "skills":
            return [
                {
                    "id": "sample",
                    "name": "Sample",
                    "version": 1,
                    "enabled": True,
                    "triggers": {"node_scopes": ["imageGeneration"]},
                    "allowed_recipe_ids": ["sample-image"],
                }
            ]
        return [
            {
                "id": "sample-image",
                "name": "Image",
                "version": 1,
                "enabled": True,
                "output_kind": "image",
                "requires_source_media": False,
            }
        ]

    async def models(project, user):
        assert user["username"] == "alice"
        return {"ok": True, "data": [{"id": "test-image", "ratioOptions": ["1:1"]}]}

    async def limits(project, user):
        return {"ok": True, "data": {"default": {"limit": 2, "remaining": 2}}}

    monkeypatch.setattr(catalog, "list_user_agent_config_items", items)
    monkeypatch.setattr(freezone, "freezone_image_models", models)
    monkeypatch.setattr(tasks, "get_project_task_limits", limits)
    return {
        "intent": {
            "skill_id": "sample",
            "user_goal": "商品图",
            "include_compose": False,
            "inputs": {"image_model": "test-image", "image_aspect_ratio": "1:1"},
            "items": [{"id": "hero", "title": "商品", "recipe_id": "sample-image"}],
        }
    }


def test_backend_rejects_model_parameters_without_plugin_preflight(
    workflow_run_client, runtime_workflow_source
):
    body = deepcopy(runtime_workflow_source)
    body["intent"]["inputs"]["image_aspect_ratio"] = "7:3"
    response = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-drafts",
        json=body,
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["status"] == "workflow_preflight_failed"


def test_backend_returns_standard_clarification_for_missing_generation_choices(monkeypatch):
    """Issue #677: the drafts API is the server-owned entry the Skill recommends;
    a video node without durationSec must come back as the standard
    clarification structure, not a generic preflight failure."""
    import asyncio

    from novelvideo.api.routes import freezone, tasks

    async def video_models(project, user):
        return {"ok": True, "data": [{
            "id": "seedance-2.0", "ratioOptions": ["16:9"], "resolutionOptions": ["720P"],
            "minDuration": 2, "maxDuration": 10, "supportsGenerateAudio": True,
        }]}

    async def limits(project, user):
        return {"ok": True, "data": {"video": {"limit": 2, "remaining": 2}}}

    monkeypatch.setattr(freezone, "freezone_video_models", video_models)
    monkeypatch.setattr(tasks, "get_project_task_limits", limits)
    compiled = {
        "ok": True,
        "skill_id": "video-ad",
        "plan": {"nodes": [
            {"id": "shot-1", "node_type": "videoNode", "data": {
                "model": "seedance-2.0", "quality": "720P",
                "workflowCatalog": {"recipeId": "dialogue-continuity-shot-video"},
            }},
        ], "edges": []},
        "preflight": {"status": "ready", "blockers": [], "warnings": []},
    }

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(freezone._check_workflow_runtime(
            compiled, project="proj_demo", user={"username": "alice"}
        ))

    detail = excinfo.value.detail
    assert excinfo.value.status_code == 400
    assert detail["status"] == "clarification_required"
    assert detail["code"] == "generation_parameters_required"
    assert detail["required_choices"] == {"video": ["duration_seconds", "generate_audio"]}
    assert detail["missing_parameters"] == [
        {"node_id": "shot-1", "node_type": "videoNode", "fields": ["durationSec", "generateAudio"]},
    ]
    assert detail["retryable"] is True
    assert detail["next_action"] == "request_user_clarification"
    assert detail["preflight"]["status"] == "blocked"


def test_backend_reports_missing_skill_stage_before_missing_generation_choices(monkeypatch):
    """Issue #677: a required stage the plan skipped is a structural blocker the
    server-owned entry keeps from the compiled preflight and reports first,
    ahead of any clarification about generation choices."""
    import asyncio

    from novelvideo.api.routes import freezone, tasks

    async def video_models(project, user):
        return {"ok": True, "data": [{
            "id": "seedance-2.0", "ratioOptions": ["16:9"], "resolutionOptions": ["720P"],
            "minDuration": 2, "maxDuration": 10, "supportsGenerateAudio": True,
        }]}

    async def limits(project, user):
        return {"ok": True, "data": {"video": {"limit": 2, "remaining": 2}}}

    monkeypatch.setattr(freezone, "freezone_video_models", video_models)
    monkeypatch.setattr(tasks, "get_project_task_limits", limits)
    stage_blocker = {
        "path": "plan.stages.shots",
        "code": "skill_stage_missing",
        "message": "Skill short-drama-quick requires a shots stage; no textAnnotationNode "
                   "node carries stage=\"shots\" or one of its recipes (drama-shot-group-detail)",
        "stage": "shots", "node_type": "textAnnotationNode",
        "recipes": ["drama-shot-group-detail"],
    }
    compiled = {
        "ok": True,
        "skill_id": "short-drama-quick",
        "plan": {"nodes": [
            {"id": "shot-1", "node_type": "videoNode", "data": {
                "model": "seedance-2.0", "quality": "720P",
                "workflowCatalog": {"recipeId": "general-video"},
            }},
        ], "edges": []},
        "preflight": {"status": "blocked", "blockers": [stage_blocker], "warnings": []},
    }

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(freezone._check_workflow_runtime(
            compiled, project="proj_demo", user={"username": "alice"}
        ))

    detail = excinfo.value.detail
    assert excinfo.value.status_code == 400
    assert detail["status"] == "workflow_preflight_failed"
    assert detail["error"] == stage_blocker["message"]
    assert detail["retryable"] is False
    assert detail["next_action"] == "resolve_preflight_blockers"
    codes = [blocker["code"] for blocker in detail["preflight"]["blockers"]]
    assert codes[0] == "skill_stage_missing"
    assert "generation_parameters_required" in codes


def test_backend_reports_non_retryable_blocker_before_missing_generation_choices(monkeypatch):
    """A disabled queue cannot be fixed by answering a card: beside missing
    generation choices the entry must fail with that blocker instead of
    sending the agent through a clarification that ends in the same error."""
    import asyncio

    from novelvideo.api.routes import freezone, tasks

    async def video_models(project, user):
        return {"ok": True, "data": [{
            "id": "seedance-2.0", "ratioOptions": ["16:9"], "resolutionOptions": ["720P"],
            "minDuration": 2, "maxDuration": 10, "supportsGenerateAudio": True,
        }]}

    async def limits(project, user):
        return {"ok": True, "data": {"video": {"limit": 0, "remaining": 0}}}

    monkeypatch.setattr(freezone, "freezone_video_models", video_models)
    monkeypatch.setattr(tasks, "get_project_task_limits", limits)
    compiled = {
        "ok": True,
        "skill_id": "video-ad",
        "plan": {"nodes": [
            {"id": "shot-1", "node_type": "videoNode", "data": {
                "model": "seedance-2.0", "quality": "720P",
                "workflowCatalog": {"recipeId": "dialogue-continuity-shot-video"},
            }},
        ], "edges": []},
        "preflight": {"status": "ready", "blockers": [], "warnings": []},
    }

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(freezone._check_workflow_runtime(
            compiled, project="proj_demo", user={"username": "alice"}
        ))

    detail = excinfo.value.detail
    assert excinfo.value.status_code == 400
    assert detail["status"] == "workflow_preflight_failed"
    assert detail["error"] == "video generation queue is disabled"
    assert detail["retryable"] is False
    assert detail["next_action"] == "resolve_preflight_blockers"
    assert "missing_parameters" not in detail
    codes = [blocker["code"] for blocker in detail["preflight"]["blockers"]]
    assert "queue_disabled" in codes
    assert "generation_parameters_required" in codes


@pytest.mark.parametrize("via_patch", [False, True])
def test_exact_plan_recommendation_can_be_confirmed(
    workflow_run_client, runtime_workflow_source, monkeypatch, via_patch
):
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.workflow_transactions import prepare_workflow_source

    async def recommended_models(project, user):
        return {"ok": True, "data": [{
            "id": "LingShan-G2", "aliases": ["newapi_gpt_image2"],
            "ratioOptions": ["1:1", "9:16"],
            "resolutionOptions": ["1K"], "qualityOptions": ["medium"],
        }]}

    monkeypatch.setattr(freezone, "freezone_image_models", recommended_models)
    plan = prepare_workflow_source(runtime_workflow_source, username="alice")["compiled"]["plan"]
    plan["inputs"] = {}
    image = next(node for node in plan["nodes"] if node["node_type"] == "imageGenNode")
    image["data"]["model"] = "recommended"
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-drafts"
    if via_patch:
        original = deepcopy(plan)
        next(node for node in original["nodes"] if node["node_type"] == "imageGenNode")[
            "data"
        ]["model"] = "LingShan-G2"
        created = workflow_run_client.post(base, json={"plan": original})
        assert created.status_code == 200, created.text
        target = f"{base}/{created.json()['data']['draft_id']}"
        response = workflow_run_client.patch(
            target, json={"expected_revision": 1, "plan": plan}
        )
    else:
        response = workflow_run_client.post(base, json={"plan": plan})
    assert response.status_code == 200, response.text
    draft = response.json()["data"]
    assert draft["intent"]["plan"] == draft["compiled"]["plan"]
    resolved = next(
        node for node in draft["compiled"]["plan"]["nodes"]
        if node["node_type"] == "imageGenNode"
    )["data"]
    assert resolved["model"] == "LingShan-G2"
    assert resolved["size"] == "1K"
    claim = workflow_run_client.post(
        f"{base}/{draft['draft_id']}/claim", json={"revision": draft["revision"]}
    )
    assert claim.status_code == 200, claim.text
    assert claim.json()["data"]["status"] == "confirming"


def test_confirmation_rechecks_live_models(
    workflow_run_client, runtime_workflow_source, monkeypatch
):
    from novelvideo.api.routes import freezone

    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-drafts"
    response = workflow_run_client.post(base, json=runtime_workflow_source)
    assert response.status_code == 200, response.text
    draft = response.json()["data"]

    async def disabled_models(project, user):
        return {"ok": True, "data": []}

    monkeypatch.setattr(freezone, "freezone_image_models", disabled_models)
    target = f"{base}/{draft['draft_id']}"
    claimed = workflow_run_client.post(
        target + "/claim", json={"revision": draft["revision"]}
    )
    assert claimed.status_code == 400, claimed.text
    assert claimed.json()["detail"]["status"] == "workflow_preflight_failed"
    assert workflow_run_client.get(target).json()["data"]["status"] == "ready"


def test_run_observation_reconciles_and_returns_compact_status(
    workflow_run_client, monkeypatch
):
    from novelvideo.api.routes import freezone

    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    run = workflow_run_client.post(
        base, json={"actions": [{"node_id": "image", "action": "generate_image"}]}
    ).json()["data"]
    calls = []

    async def reconcile(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "data": {"runs": []}}

    monkeypatch.setattr(freezone, "get_canvas_workflow_runs", reconcile)
    target = f"{base}/{run['run_id']}"
    response = workflow_run_client.get(
        target,
        params={"view": "summary", "wait_seconds": 20, "after": "previous-state"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["changed"] is True
    assert data["run_status"] == "running"
    assert "actions" not in data
    assert len(calls) == 1
    assert (
        workflow_run_client.get(target, params={"wait_seconds": 21}).status_code == 422
    )


def test_run_wait_stops_at_deadline_without_creating_a_retry(
    workflow_run_client, monkeypatch
):
    from novelvideo.api.routes import freezone

    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs"
    run = workflow_run_client.post(
        base, json={"actions": [{"node_id": "image", "action": "generate_image"}]}
    ).json()["data"]
    calls = []

    async def reconcile(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "data": {"runs": []}}

    monkeypatch.setattr(freezone, "get_canvas_workflow_runs", reconcile)
    response = workflow_run_client.get(
        f"{base}/{run['run_id']}", params={"view": "summary", "wait_seconds": 1}
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["changed"] is False
    assert data["run_status"] == "running"
    assert data["automatic_retry"] is False
    assert len(calls) >= 2
    assert data["progress"][0]["retry_count"] == 0
@pytest.mark.parametrize(
    ('reused_recipe_id', 'expected_status'),
    [('bob-private-recipe', 200), ('alice-private-recipe', 400)],
)
def test_shared_project_generation_session_uses_requester_private_catalog(
    workflow_run_client, monkeypatch, reused_recipe_id, expected_status
):
    from novelvideo.api.auth import get_api_user
    from novelvideo.api.routes import freezone

    # The project and durable storage remain Alice's; Bob is its authenticated editor.
    workflow_run_client.app.dependency_overrides[get_api_user] = lambda: {
        'id': 'u-bob', 'username': 'bob',
    }
    catalog_users = []

    def private_catalog(username, kind):
        catalog_users.append(username)
        assert kind == 'recipes'
        return [{'id': f'{username}-private-recipe', 'enabled': True}]

    monkeypatch.setattr(freezone, 'list_user_agent_config_items', private_catalog)
    session_id = f'shared-{reused_recipe_id}'
    manifest, draft = _recipe_generation_session_payload(
        workflow_run_client, session_id=session_id, reused_recipe_id=reused_recipe_id,
    )
    response = workflow_run_client.put(
        f'/api/v1/projects/proj_demo/freezone/agent-generation-sessions/{session_id}',
        json={'canvas_id': 'default', 'manifest': manifest, 'draft': draft},
    )
    assert response.status_code == expected_status, response.text
    assert catalog_users == ['bob']
    if expected_status == 200:
        saved = workflow_run_client.get(
            f'/api/v1/projects/proj_demo/freezone/agent-generation-sessions/{session_id}',
        )
        assert saved.status_code == 200
        assert saved.json()['data']['manifest'] == manifest
    else:
        assert 'reused Recipe is unavailable' in response.json()['detail']
def test_workflow_policy_change_persists_new_revision(workflow_run_client):
    base = "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-drafts"
    created = workflow_run_client.post(base, json={
        "intent": {"skill_id": "video-ad", "user_goal": "广告"},
        "compiled": _valid_draft_compiled(), "run_after_create": False,
    }).json()["data"]
    response = workflow_run_client.patch(f"{base}/{created['draft_id']}", json={
        "expected_revision": created["revision"], "changes": {"run_after_create": True},
    })
    assert response.status_code == 200
    patched = response.json()["data"]
    assert patched["revision"] == created["revision"] + 1
    assert patched["run_after_create"] is True
    assert patched["draft_id"] == created["draft_id"]
    stale = workflow_run_client.patch(f"{base}/{created['draft_id']}", json={
        "expected_revision": created["revision"], "changes": {"run_after_create": False},
    })
    assert stale.json()["status"] == "workflow_draft_revision_conflict"


def _mode_revision_plan() -> dict:
    from novelvideo.freezone.agent_workflows import catalog

    compiled = catalog.compile_workflow_intent(
        {
            "skill_id": "text-to-image-video",
            "user_goal": "一段图生视频",
            "inputs": {
                "image_model": "LingShan-G2",
                "image_aspect_ratio": "16:9",
                "image_resolution": "2K",
                "image_quality": "medium",
                "video_model": "seedance-2.0",
                "video_aspect_ratio": "16:9",
                "video_resolution": "720P",
                "video_duration_seconds": 5,
                "video_generate_audio": False,
                "video_generation_mode": "imageToVideo",
            },
            "planner": {"mode": "standard", "item_count": 1},
        }
    )
    assert compiled["ok"] is True, compiled
    # A JSON round trip, like a request body: the planner shares one inputs
    # object between plan.inputs and every node's confirmedInputs.
    plan = json.loads(json.dumps(compiled["plan"]))
    plan.pop("planner", None)
    plan.pop("layout", None)
    return plan


def _video_node(plan: dict) -> dict:
    return next(node for node in plan["nodes"] if node["node_type"] == "videoNode")


def _genmode_blockers(preflight: dict) -> list[str]:
    return [
        blocker["code"]
        for blocker in (preflight or {}).get("blockers") or []
        if str(blocker.get("path", "")).endswith(".genMode")
    ]


def test_revised_per_node_video_mode_draft_can_be_claimed(workflow_run_client):
    """Review of #714: a per-node mode revision recorded by the server keeps the
    stored draft claimable; claim-time revalidation must not drop it."""
    base = "/api/v1/projects/proj_demo/freezone/canvases/canvas_demo/workflow-drafts"
    created = workflow_run_client.post(base, json={"plan": _mode_revision_plan()})
    assert created.status_code == 200, created.text
    draft = created.json()["data"]
    target = f"{base}/{draft['draft_id']}"
    stored = workflow_run_client.get(target).json()["data"]
    assert _genmode_blockers(stored["compiled"]["preflight"]) == []
    revised = workflow_run_client.patch(
        target,
        json={
            "expected_revision": draft["revision"],
            "changes": {"step_updates": [
                {
                    "node_id": _video_node(stored["compiled"]["plan"])["id"],
                    "settings": {"generation_mode": "firstFrame"},
                }
            ]},
        },
    )
    assert revised.status_code == 200, revised.text
    stored = workflow_run_client.get(target).json()["data"]
    assert _video_node(stored["compiled"]["plan"])["data"]["genMode"] == "firstFrame"
    assert _genmode_blockers(stored["compiled"]["preflight"]) == []

    claimed = workflow_run_client.post(
        f"{target}/claim", json={"revision": stored["revision"]}
    )

    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["data"]["task_id"]


def test_caller_written_video_mode_confirmation_is_rejected_at_the_route(
    workflow_run_client,
):
    """A submitted plan that confirms its own swapped mode is refused by the
    route, so no caller-written confirmation is ever stored as trusted."""
    plan = _mode_revision_plan()
    video = _video_node(plan)["data"]
    video["genMode"] = "firstFrame"
    video["workflowCatalog"].setdefault("confirmedInputs", {})[
        "video_generation_mode"
    ] = "firstFrame"
    base = "/api/v1/projects/proj_demo/freezone/canvases/canvas_demo/workflow-drafts"

    created = workflow_run_client.post(base, json={"plan": plan})

    assert created.status_code == 400, created.text
    assert _genmode_blockers(created.json()["detail"]["preflight"]) == [
        "video_generation_mode_conflict"
    ]
    assert workflow_run_client.enqueued_tasks == []


def _store_legacy_plan(client, draft_id: str, plan: dict) -> None:
    """Rewrite a stored draft's plan the way code before #711 could store it."""
    from novelvideo.freezone.workflow_drafts import workflow_drafts_db_path

    conn = sqlite3.connect(workflow_drafts_db_path(client.state_dir))
    try:
        intent_json, compiled_json = conn.execute(
            "SELECT intent_json, compiled_json FROM workflow_drafts WHERE draft_id = ?",
            (draft_id,),
        ).fetchone()
        intent, compiled = json.loads(intent_json), json.loads(compiled_json)
        intent["plan"] = plan
        compiled["plan"] = plan
        compiled.pop("mode_confirmations", None)
        conn.execute(
            "UPDATE workflow_drafts SET intent_json = ?, compiled_json = ? WHERE draft_id = ?",
            (json.dumps(intent), json.dumps(compiled), draft_id),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("legacy_swap", [True, False], ids=["self_confirmed_swap", "control"])
def test_legacy_draft_self_confirmed_video_mode_cannot_be_claimed(
    workflow_run_client, legacy_swap
):
    """Review of #714: a draft stored before #711 may carry a caller-written
    confirmedInputs.video_generation_mode; claiming must revalidate it as
    untrusted instead of admitting the swapped mode."""
    base = "/api/v1/projects/proj_demo/freezone/canvases/canvas_demo/workflow-drafts"
    created = workflow_run_client.post(base, json={"plan": _mode_revision_plan()})
    assert created.status_code == 200, created.text
    draft = created.json()["data"]
    target = f"{base}/{draft['draft_id']}"
    stored = workflow_run_client.get(target).json()["data"]
    plan = stored["compiled"]["plan"]
    if legacy_swap:
        video = _video_node(plan)["data"]
        video["genMode"] = "firstFrame"
        video["workflowCatalog"]["confirmedInputs"] = {
            **video["workflowCatalog"].get("confirmedInputs", {}),
            "video_generation_mode": "firstFrame",
        }
    _store_legacy_plan(workflow_run_client, draft["draft_id"], plan)

    claimed = workflow_run_client.post(
        f"{target}/claim", json={"revision": stored["revision"]}
    )

    if not legacy_swap:
        assert claimed.status_code == 200, claimed.text
        return
    assert claimed.status_code == 400, claimed.text
    detail = claimed.json()["detail"]
    assert _genmode_blockers(detail.get("preflight") or {}) == [
        "video_generation_mode_conflict"
    ], detail
    assert workflow_run_client.enqueued_tasks == []


def test_caller_supplied_compiled_mode_confirmations_are_ignored(workflow_run_client):
    """mode_confirmations is server-owned: one sent in a create request's
    compiled payload does not confirm a swapped per-node mode."""
    plan = _mode_revision_plan()
    video = _video_node(plan)
    video["data"]["genMode"] = "firstFrame"
    base = "/api/v1/projects/proj_demo/freezone/canvases/canvas_demo/workflow-drafts"

    created = workflow_run_client.post(
        base,
        json={
            "intent": {"schema_version": "freezone_workflow_plan_draft.v1", "plan": plan},
            "compiled": {
                "ok": True,
                "skill_id": "text-to-image-video",
                "plan": plan,
                "mode_confirmations": {video["id"]: "firstFrame"},
            },
        },
    )

    assert created.status_code == 400, created.text
    assert _genmode_blockers(created.json()["detail"]["preflight"]) == [
        "video_generation_mode_conflict"
    ]


def _two_video_recipe_operations(client: TestClient, monkeypatch) -> list[str]:
    """Admit two model-compiled video Recipe actions in one workflow run."""
    from novelvideo.api.routes import freezone

    monkeypatch.setattr(freezone, "get_usage_meter", lambda: object())
    created = client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs",
        json={"actions": [
            {
                "node_id": node_id, "action": "generate_video",
                "recipe_id": "product-video", "recipe_version": "1.0.0",
                "generation_attempt_id": f"attempt-{node_id}",
            }
            for node_id in ("video-1", "video-2")
        ]},
    ).json()["data"]
    return [action["product_operation_id"] for action in created["actions"]]


def _fail_recipe_operation(client: TestClient, operation_id: str) -> None:
    from novelvideo.freezone.agent_product_operations import (
        finish_agent_product_operation,
        read_agent_product_operation,
    )

    operation = read_agent_product_operation(
        project_dir=client.state_dir, operation_id=operation_id
    )
    finish_agent_product_operation(
        project_dir=client.state_dir,
        operation_id=operation_id,
        outcome="failed",
        expected_task_id=operation["task_id"],
    )


def _video_compile_item(operation_id: str, request_id: str = "") -> dict:
    return {
        **({"request_id": request_id} if request_id else {}),
        "project_id": "proj_demo",
        "product_operation_id": operation_id,
        "recipe_id": "product-video",
        "node_kind": "video",
    }


def test_recipe_compile_rejects_attempt_failed_during_compilation(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    """A retry must not get a prompt for an attempt a concurrent compile failed."""
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.recipe_runtime import RecipeCompileResult

    operation_id, _other = _two_video_recipe_operations(workflow_run_client, monkeypatch)

    async def compile_while_earlier_request_fails(**_kwargs):
        # The timed-out earlier compile of this attempt fails it meanwhile.
        _fail_recipe_operation(workflow_run_client, operation_id)
        return RecipeCompileResult(
            "video prompt", "model", ("product-video",),
            model_call_id="recipe-compiler:late", executed_at=1.0,
        )

    monkeypatch.setattr(
        freezone, "compile_recipe_prompt_result", compile_while_earlier_request_fails
    )
    response = workflow_run_client.post(
        "/api/v1/freezone/recipes/compile", json=_video_compile_item(operation_id)
    )
    assert response.status_code == 409
    assert "recipe attempt ended (failed)" in response.json()["detail"]
    assert "rerun the node" in response.json()["detail"]


def test_recipe_compile_batch_isolates_ended_attempts(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.recipe_runtime import RecipeCompileResult

    admitted_failed, live = _two_video_recipe_operations(workflow_run_client, monkeypatch)
    _fail_recipe_operation(workflow_run_client, admitted_failed)
    compiled_items: list[int] = []

    async def compile_batch(items):
        compiled_items.append(len(items))
        return [
            RecipeCompileResult(
                "video prompt", "model", ("product-video",),
                model_call_id="recipe-compiler:live", executed_at=1.0,
            )
        ]

    monkeypatch.setattr(freezone, "compile_recipe_prompt_batch", compile_batch)
    response = workflow_run_client.post(
        "/api/v1/freezone/recipes/compile-batch",
        json={"items": [
            _video_compile_item(admitted_failed, "ended"),
            _video_compile_item(live, "live"),
        ]},
    )
    assert response.status_code == 200, response.text
    items = {item["request_id"]: item for item in response.json()["data"]["items"]}
    assert compiled_items == [1]
    assert items["ended"]["ok"] is False
    assert "recipe attempt ended (failed)" in items["ended"]["error"]
    assert not items["ended"].get("retryable")
    assert items["live"]["ok"] is True
    assert items["live"]["data"]["prompt"] == "video prompt"


def test_recipe_compile_batch_reports_attempt_failed_during_compilation(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.recipe_runtime import RecipeCompileResult

    racing, live = _two_video_recipe_operations(workflow_run_client, monkeypatch)

    async def compile_batch(items):
        _fail_recipe_operation(workflow_run_client, racing)
        return [
            RecipeCompileResult(
                "video prompt", "model", ("product-video",),
                model_call_id=f"recipe-compiler:{index}", executed_at=1.0,
            )
            for index, _item in enumerate(items)
        ]

    monkeypatch.setattr(freezone, "compile_recipe_prompt_batch", compile_batch)
    response = workflow_run_client.post(
        "/api/v1/freezone/recipes/compile-batch",
        json={"items": [
            _video_compile_item(racing, "racing"),
            _video_compile_item(live, "live"),
        ]},
    )
    assert response.status_code == 200, response.text
    items = {item["request_id"]: item for item in response.json()["data"]["items"]}
    assert items["racing"]["ok"] is False
    assert "recipe attempt ended (failed)" in items["racing"]["error"]
    assert items["live"]["ok"] is True


def test_workflow_media_link_reports_ended_recipe_attempt(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.workflow_runs import (
        claim_workflow_media_action,
        classify_workflow_error,
        workflow_error_diagnostics,
    )

    operation_id, _other = _two_video_recipe_operations(workflow_run_client, monkeypatch)
    _fail_recipe_operation(workflow_run_client, operation_id)
    link_context = SimpleNamespace(
        project_id="proj_demo", state_dir=str(workflow_run_client.state_dir)
    )
    with pytest.raises(HTTPException, match=r"recipe attempt ended \(failed\)") as raised:
        freezone._verified_workflow_media_link(
            ctx=link_context,
            project_dir=workflow_run_client.state_dir,
            canvas_id="default",
            node_id="video-1",
            operation_id=operation_id,
            attempt_id="attempt-video-1",
        )
    assert raised.value.status_code == 409
    with pytest.raises(ValueError, match=r"recipe attempt ended \(failed\)"):
        claim_workflow_media_action(
            project_dir=workflow_run_client.state_dir,
            project_id="proj_demo",
            canvas_id="default",
            node_id="video-1",
            operation_id=operation_id,
            attempt_id="attempt-video-1",
            task_type="freezone_video_gen",
            fingerprint="f" * 64,
        )
    # Identity mismatches keep their own error; only the settled status is "ended".
    with pytest.raises(HTTPException, match="does not match admitted Recipe attempt"):
        freezone._verified_workflow_media_link(
            ctx=link_context,
            project_dir=workflow_run_client.state_dir,
            canvas_id="default",
            node_id="video-1",
            operation_id=operation_id,
            attempt_id="unrelated-attempt",
        )
    detail = str(raised.value.detail)
    assert classify_workflow_error(detail) == ("attempt_ended", False)
    # The runner's localized wrapper keeps the raw reason, so it stays final too.
    wrapped = f"本次节点执行已结束，请重新运行该节点以开始新的执行（{detail}）"
    diagnostics = workflow_error_diagnostics(wrapped)
    assert diagnostics["retryable"] is False
    assert "重新运行该节点" in diagnostics["user_error"]


@pytest.mark.parametrize("endpoint", ["compile", "compile-batch"])
def test_recipe_compile_reports_attempt_settled_during_evidence_write(
    workflow_run_client: TestClient, monkeypatch, endpoint: str
) -> None:
    """The attempt can end between the terminal check and the evidence write."""
    from novelvideo.api.routes import freezone
    from novelvideo.freezone.recipe_runtime import RecipeCompileResult

    racing, live = _two_video_recipe_operations(workflow_run_client, monkeypatch)
    real_bind = freezone.bind_agent_product_model_execution

    def bind_after_concurrent_settle(**kwargs):
        if kwargs["operation_id"] == racing:
            _fail_recipe_operation(workflow_run_client, racing)
        return real_bind(**kwargs)

    def model_result(index: int) -> RecipeCompileResult:
        return RecipeCompileResult(
            "video prompt", "model", ("product-video",),
            model_call_id=f"recipe-compiler:{index}", executed_at=1.0,
        )

    async def compile_one(**_kwargs):
        return model_result(0)

    async def compile_batch(items):
        return [model_result(index) for index, _item in enumerate(items)]

    monkeypatch.setattr(
        freezone, "bind_agent_product_model_execution", bind_after_concurrent_settle
    )
    monkeypatch.setattr(freezone, "compile_recipe_prompt_result", compile_one)
    monkeypatch.setattr(freezone, "compile_recipe_prompt_batch", compile_batch)
    if endpoint == "compile":
        response = workflow_run_client.post(
            "/api/v1/freezone/recipes/compile", json=_video_compile_item(racing)
        )
        assert response.status_code == 409
        assert "recipe attempt ended (failed)" in response.json()["detail"]
        return
    response = workflow_run_client.post(
        "/api/v1/freezone/recipes/compile-batch",
        json={"items": [
            _video_compile_item(racing, "racing"),
            _video_compile_item(live, "live"),
        ]},
    )
    assert response.status_code == 200, response.text
    items = {item["request_id"]: item for item in response.json()["data"]["items"]}
    assert items["racing"]["ok"] is False
    assert "recipe attempt ended (failed)" in items["racing"]["error"]
    assert items["live"]["ok"] is True


def test_recipe_compile_does_not_replay_attempt_settled_during_replay(
    workflow_run_client: TestClient, monkeypatch
) -> None:
    """The saved-prompt read follows the status read; an ended attempt is not replayed."""
    from novelvideo.api.routes import freezone

    operation_id, _other = _two_video_recipe_operations(workflow_run_client, monkeypatch)

    def saved_prompt_after_concurrent_settle(**_kwargs):
        _fail_recipe_operation(workflow_run_client, operation_id)
        return "saved video prompt"

    async def forbidden_compile(**_kwargs):
        raise AssertionError("a saved model prompt must be replayed, not recompiled")

    monkeypatch.setattr(
        freezone, "read_recipe_model_prompt", saved_prompt_after_concurrent_settle
    )
    monkeypatch.setattr(freezone, "compile_recipe_prompt_result", forbidden_compile)
    response = workflow_run_client.post(
        "/api/v1/freezone/recipes/compile", json=_video_compile_item(operation_id)
    )
    assert response.status_code == 409
    assert "recipe attempt ended (failed)" in response.json()["detail"]


@pytest.fixture(params=["frame", "subject", "scene"])
def story_execution_plan(request, monkeypatch):
    from novelvideo.api.routes import freezone

    canvas = {"nodes": [
        {"id": "story-group", "type": "groupNode", "data": {
            "storyGroup": True, "interactiveStoryId": "story-1",
            "storyCharacters": [{"id": "hero"}], "storyScenes": [{"id": "room"}],
        }},
        {"id": "video-opening", "type": "videoNode", "parentId": "story-group", "data": {
            "storySegmentId": "opening", "storyCharacterIds": ["hero"],
            "storySceneRefs": [{"scene_id": "room", "usage": "setting"}],
        }},
    ]}
    monkeypatch.setattr(freezone.canvas_store, "read_canvas", lambda *_args: canvas)

    async def image_models(*_args, **_kwargs):
        return {"ok": True, "data": [{"id": "story-image-model"}]}

    monkeypatch.setattr(freezone, "freezone_image_models", image_models)
    context = {"story_id": "story-1"}
    if request.param == "frame":
        context["targets"] = [{"plan_node_id": "image", "story_segment_id": "opening",
                               "video_node_id": "video-opening"}]
    else:
        context["asset_targets"] = [{"plan_node_id": "image", "kind": request.param,
            "entity_id": "hero" if request.param == "subject" else "room",
            "segment_ids": ["opening"]}]
    return {
        "schema_version": "freezone_workflow_plan.v1",
        "skill": {"id": "text-to-image-video"}, "source_context": context,
        "nodes": [{"id": "image", "node_type": "imageGenNode", "stage": "image",
                   "data": {"prompt": "Story image", "model": "story-image-model",
                            "workflowCatalog": {"skillId": "text-to-image-video", "recipeId": "general-image"}}}],
        "edges": [],
    }


def test_story_bound_prepare_requires_execution_policy(workflow_run_client, story_execution_plan):
    from novelvideo.freezone.workflow_drafts import workflow_drafts_db_path

    client = workflow_run_client
    base = "/api/v1/projects/proj_demo/freezone/canvases/canvas_demo/workflow-drafts"
    response = client.post(base, json={"plan": story_execution_plan})
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "story_execution_policy_required"
    assert detail["retryable"] is True
    assert "run_after_create" in detail["error"]
    assert not workflow_drafts_db_path(client.state_dir).exists()
    assert client.enqueued_tasks == []
    # The rejected request can be corrected without creating duplicate drafts.
    corrected = client.post(base, json={"plan": story_execution_plan, "run_after_create": False})
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["data"]["run_after_create"] is False


@pytest.mark.parametrize("policy", [True, False])
def test_story_bound_prepare_preserves_explicit_policy(workflow_run_client, story_execution_plan, policy):
    client = workflow_run_client
    base = "/api/v1/projects/proj_demo/freezone/canvases/canvas_demo/workflow-drafts"
    response = client.post(base, json={"plan": story_execution_plan, "run_after_create": policy})
    assert response.status_code == 200, response.text
    draft = response.json()["data"]
    assert draft["run_after_create"] is policy
    # Ordinary edits to an existing draft inherit the saved decision.
    patched = client.patch(f"{base}/{draft['draft_id']}", json={"expected_revision": 1, "plan": story_execution_plan})
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["run_after_create"] is policy
    assert client.enqueued_tasks == []


def test_ordinary_prepare_defaults_false_on_story_canvas(workflow_run_client, monkeypatch):
    from novelvideo.api.routes import freezone

    monkeypatch.setattr(freezone.canvas_store, "read_canvas", lambda *_args: {"nodes": [{
        "id": "story-group", "type": "groupNode",
        "data": {"storyGroup": True, "interactiveStoryId": "story-1"},
    }]})
    # Use the full ordinary image/video template of the same story-production Skill.
    plan = _mode_revision_plan()
    response = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/canvas_demo/workflow-drafts",
        json={"plan": plan},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["run_after_create"] is False
    assert workflow_run_client.enqueued_tasks == []


@pytest.mark.parametrize("policy", [None, True, False])
def test_execution_policy_never_bypasses_invalid_story_binding(workflow_run_client, story_execution_plan, policy):
    plan = deepcopy(story_execution_plan)
    plan["source_context"]["story_id"] = "missing-story"
    body = {"plan": plan}
    if policy is not None:
        body["run_after_create"] = policy
    response = workflow_run_client.post(
        "/api/v1/projects/proj_demo/freezone/canvases/canvas_demo/workflow-drafts", json=body,
    )
    assert response.status_code == 409, response.text
    assert workflow_run_client.enqueued_tasks == []
