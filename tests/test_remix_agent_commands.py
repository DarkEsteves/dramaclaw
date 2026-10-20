"""An agent uses the same project-scoped production command endpoint as the UI."""

from __future__ import annotations

import pytest

from test_api_remix_artifacts import BASE, client  # noqa: F401
from test_production_commands import _accepted_spoken_plan, _plan


def test_production_command_endpoint_requires_editor_and_reports_current_revision(request: pytest.FixtureRequest) -> None:
    api, role = request.getfixturevalue("client")
    saved = api.post(BASE, json={"schema": "production_plan.v2", "payload": _plan()}).json()["data"]
    root = "/api/v1/projects/demo/freezone/remix/production-plans/commands"
    command = {"command_id": "first", "plan_ref": {"artifact_id": saved["id"], "revision": 1},
               "kind": "edit_beat", "target_id": "intro", "payload": {"purpose": "直接开场"}}
    role["value"] = "viewer"
    assert api.post(root, json=command).status_code == 403
    assert api.post(root.replace("/demo/", "/peer/"), json=command).status_code == 403
    role["value"] = "editor"
    response = api.post(root, json=command)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["plan_ref"]["revision"] == 2
    assert api.post(root, json=command).json()["data"] == response.json()["data"]
    changed = {**command, "command_id": "second", "payload": {"purpose": "另一个开场"}}
    conflict = api.post(root, json=changed)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["current_revision"] == 2


def test_production_state_endpoint_reads_exact_revision_with_project_acl(request: pytest.FixtureRequest) -> None:
    api, role = request.getfixturevalue("client")
    saved = api.post(BASE, json={"schema": "production_plan.v2", "payload": _plan()}).json()["data"]
    path = f"/api/v1/projects/demo/freezone/remix/production-plans/{saved['id']}/state"
    result = api.get(path)
    assert result.status_code == 200, result.text
    state = result.json()["data"]
    assert state["plan_ref"] == {"artifact_id": saved["id"], "revision": 1}
    assert state["beats"] == [{"id": "intro", "purpose": "提问", "turn_count": 1, "duration_frames": 72, "timing_mode": "auto"}]
    assert state["unbound_placement_ids"] == ["picture"]
    assert state["turns"] == [{"id": "line", "beat_id": "intro", "speaker": "嘉宾", "text": "原台词",
                              "timing_status": "estimated"}]
    detail = api.get(path, params={"entity_id": "picture"})
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["entity"]["kind"] == "placement"
    assert detail.json()["data"]["entity"]["data"]["start"] == {"kind": "beat", "ref_id": "intro", "edge": "start", "offset_frames": 0}
    assert api.get(path, params={"entity_id": "unknown"}).status_code == 404
    assert api.get(path.replace("/demo/", "/peer/")).status_code == 403
    role["home"] = False
    assert api.get(path).status_code == 409


def test_production_command_api_preserves_accepted_speech_evidence(
    request: pytest.FixtureRequest, tmp_path
) -> None:
    from novelvideo.freezone.remix.store import RemixArtifactStore

    api, _role = request.getfixturevalue("client")
    store = RemixArtifactStore(tmp_path, "demo", created_by="alice")
    accepted, _ = _accepted_spoken_plan(store)
    command = {"command_id": "rewrite-accepted-api",
               "plan_ref": {"artifact_id": accepted["id"], "revision": accepted["revision"]},
               "kind": "edit_turn", "target_id": "line", "payload": {"text": "新台词"}}
    response = api.post("/api/v1/projects/demo/freezone/remix/production-plans/commands", json=command)
    assert response.status_code == 422
    assert "accepted speech" in response.text
    assert store.read_latest(accepted["id"])["revision"] == accepted["revision"]


def test_production_state_shows_actual_content_before_purpose_only_edit(request):
    api, role = request.getfixturevalue("client")
    plan = _plan()
    plan["goal"] = "18-25 seconds of tea"
    plan["beats"][0].update(purpose="Tea reveal", duration_frames=150)
    plan["placements"][0].update(description="Old muscle reveal " + "x" * 600, turn_id="line")
    plan["placements"].append({**plan["placements"][0], "id": "card", "kind": "graphic", "role": "effect", "description": "Old card", "graphic": {"template": "keyword", "text": "Muscles", "emphasis": "Muscles"}})
    saved = api.post(BASE, json={"schema": "production_plan.v2", "payload": plan}).json()["data"]
    path = f"/api/v1/projects/demo/freezone/remix/production-plans/{saved['id']}/state"
    api.post("/api/v1/projects/demo/freezone/remix/production-plans/commands", json={"command_id": "purpose-only", "plan_ref": {"artifact_id": saved["id"], "revision": 1}, "kind": "edit_beat", "target_id": "intro", "payload": {"purpose": "Changed purpose"}})
    role["value"] = "viewer"
    state = api.get(path, params={"revision": 1}).json()["data"]
    assert state["plan_ref"]["revision"] == 1
    assert state["beats"][0]["purpose"] == "Tea reveal"
    assert state["beats"][0]["duration_frames"] == 150
    assert state["beats"][0]["timing_mode"] == "auto"
    visual, card = state["placements"]
    assert visual["description"] == plan["placements"][0]["description"][:512]
    assert visual["description_truncated"] is True
    assert visual["turn_id"] == "line" and visual["track_id"] == "visual"
    assert card["description"] == "Old card" and card["description_truncated"] is False
    assert "graphic" not in visual
    assert card["graphic"] == {"template": "keyword", "text": "Muscles", "text_truncated": False, "emphasis": "Muscles", "emphasis_truncated": False}
    detail = api.get(path, params={"revision": 1, "entity_id": "picture"}).json()["data"]["entity"]
    assert detail["data"] == saved["payload"]["placements"][0]
    graphic_detail = api.get(path, params={"revision": 1, "entity_id": "card"}).json()["data"]["entity"]
    assert graphic_detail["data"] == saved["payload"]["placements"][1]
    assert set(card["graphic"]) == {"template", "text", "text_truncated", "emphasis", "emphasis_truncated"}
    assert api.get(path).json()["data"]["plan_ref"]["revision"] == 2
    assert api.get(path.replace("/demo/", "/peer/"), params={"revision": 1}).status_code == 403
