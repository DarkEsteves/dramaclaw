from copy import deepcopy

import pytest
from PIL import Image

from test_reference_material_selection_api import BASE

from test_reference_commission import confirmed_plan, selectable_work, selection_canvas
from novelvideo.freezone.remix import reference_commission as commissions
from novelvideo.freezone.remix.material_execution import (
    review_material_candidate,
    confirm_material_review,
)


pytest_plugins = [
    "test_reference_material_selection_api",
    "test_production_output_review",
]


@pytest.fixture(autouse=True)
def isolated_coordinator(monkeypatch):
    from novelvideo.chat.reference_remix_continuation import coordinator

    monkeypatch.setattr(coordinator.store, "production_for_actor", lambda *a: [])


def get_state(client, c):
    response = client.get(
        BASE + "/production-state",
        params={"commission_id": c["commission_ref"]["artifact_id"]},
    )
    assert response.status_code in {200, 403, 409}, response.text
    return response


def test_state_reports_measurement_for_current_plan_revision(selection_api):
    client, store, commission, plan, _, _ = selection_api
    first = get_state(client, commission).json()["data"]
    assert first["speech_estimates"] == []
    assert first["plan_diagnostics"] == []
    payload = deepcopy(plan["payload"])
    payload["beats"][0]["turns"] = [{"id": "line", "text": "你好世界"}]
    updated = store.revise(plan["id"], 1, payload, [])
    from novelvideo.freezone import canvas_store
    canvas = canvas_store.read_canvas(store.project_dir, "c")
    for node in canvas["nodes"]:
        if (node.get("data", {}).get("productionPlanV2Ref") or {}).get("artifactId") == plan["id"]:
            node["data"]["productionPlanV2Ref"]["revision"] = 2
    canvas_store.save_canvas(store.project_dir, "c", base_revision=canvas["revision"],
        build_payload=lambda _: {**canvas, "revision": canvas["revision"] + 1})
    from novelvideo.freezone.remix.reference_commission import advance_reference_commission
    commission = advance_reference_commission(store, commission["commission_ref"], {
        "type": "plan_ready", "plan_ref": {"artifact_id": updated["id"], "revision": 2, "project_id": "p"},
    }, commission["commission_ref"]["revision"])
    latest = get_state(client, commission).json()["data"]
    assert latest["plan_ref"]["revision"] == 2
    assert any(row["entity_id"] == "line" for row in latest["speech_estimates"])


def test_state_exposes_scoped_selected_model_duration_intersection(selection_api, monkeypatch):
    from novelvideo.api.routes import freezone, reference_director
    client, _, commission, _, _, _ = selection_api
    async def choices(ctx, canvas_id, saved_commission):
        assert canvas_id == 'c'
        assert saved_commission['commission_ref'] == commission['commission_ref']
        assert saved_commission['direction'] == commission['direction']
        return {'video_model': 'chosen-video', 'video_aspect_ratio': '9:16',
                'video_resolution': '720P'}
    async def catalog(kind, *, requester_user_id):
        assert requester_user_id == 'u' and kind == 'video'
        return [{'id': 'chosen-video', 'minDuration': 2, 'maxDuration': 20,
                 'durationOptions': [2, 5, 10, 20], 'resolutionOptions': ['720P'],
                 'supportedModes': ['text_to_video']}]
    monkeypatch.setattr(reference_director, '_saved_material_generation_choices', choices)
    monkeypatch.setattr(freezone, '_scoped_media_model_catalog', catalog)
    state = get_state(client, commission).json()['data']
    assert state['model_capabilities']['video']['allowed_duration_seconds'] == [5, 10]
    assert state['model_capabilities']['video']['resolution_options'] == ['720P']


def test_state_exposes_effective_preservation_requirements(selection_api):
    from novelvideo.freezone.remix.reference_commission import (
        AuthenticatedDecisionEvidence, advance_reference_commission, create_reference_commission,
    )

    client, store, _, _, _, _ = selection_api
    current = create_reference_commission(
        store, canvas_id='c', requester_user_id='u', analysis_node_id='a',
        goal='remix', analysis_evidence={'task_id': 'analysis', 'result_fingerprint': 'hash'},
    )
    current = advance_reference_commission(store, current['commission_ref'], {
        'type': 'direction_confirmed',
        'direction': {'preserve': ['Keep the opening question']},
    }, 1, host_evidence=AuthenticatedDecisionEvidence('p', 'c', 'u', 'message-1'))

    state = get_state(client, current).json()['data']
    assert 'Keep the opening question' in [
        row['text'] for row in state['review_context']['requirements']['semantic']
    ]
    assert state['review_context']['direction_revision'] == 1


def test_drafting_state_points_to_confirmed_bound_empty_plan(selection_api):
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.workflow_drafts import (
        bind_workflow_draft_task, claim_workflow_draft_confirmation,
        create_workflow_draft, finish_workflow_draft_confirmation,
    )
    from novelvideo.freezone.remix.reference_commission import (
        AuthenticatedDecisionEvidence, advance_reference_commission,
        create_reference_commission,
    )

    client, store, _, _, _, _ = selection_api
    current = create_reference_commission(store, canvas_id="c", requester_user_id="u",
        analysis_node_id="a", goal="new", analysis_evidence={"task_id": "analysis-task", "result_fingerprint": "hash"})
    current = advance_reference_commission(store, current["commission_ref"],
        {"type": "direction_confirmed", "direction": {"target_goal": "new"}},
        current["commission_ref"]["revision"],
        host_evidence=AuthenticatedDecisionEvidence("p", "c", "u", "direction-message"))
    binding = {"project_id": "p", "canvas_id": "c", "requester_user_id": "u",
        "commission_id": current["commission_ref"]["artifact_id"], "analysis_node_id": "a",
        "direction_revision": current["direction_revision"],
        "direction_fingerprint": current["direction_fingerprint"], "stage": "production_plans"}
    draft = create_workflow_draft(project_dir=store.project_dir, project_id="p", canvas_id="c",
        intent={"reference_commission_id": binding["commission_id"]},
        compiled={"ok": True, "skill_id": "reference-video-remix", "reference_binding": binding,
                  "plan": {"nodes": [{"id": "production_plan_1", "stage": "production_plans",
                                      "node_type": "scriptNode", "data": {"remixRole": "production_plan",
                                      "processingAction": "generate_production_plan_draft"}}]}})
    claimed, _ = claim_workflow_draft_confirmation(project_dir=store.project_dir, canvas_id="c",
        draft_id=draft["draft_id"], revision=1)
    assert claimed
    bind_workflow_draft_task(project_dir=store.project_dir, canvas_id="c", draft_id=draft["draft_id"],
        task_id="confirm-task", root_task_id="confirm-task")
    finish_workflow_draft_confirmation(project_dir=store.project_dir, canvas_id="c",
        draft_id=draft["draft_id"], outcome="confirmed", expected_task_id="confirm-task")
    canvas = canvas_store.read_canvas(store.project_dir, "c")
    canvas["nodes"].append({"id": "bound-original", "type": "scriptNode", "data": {
        "remixRole": "production_plan", "workflowInstanceId": draft["draft_id"],
        "workflowPlanNodeId": "production_plan_1"}})
    canvas["nodes"].append({"id": "unrelated-newer", "type": "scriptNode", "data": {"remixRole": "production_plan"}})
    canvas_store.save_canvas(store.project_dir, "c", base_revision=canvas["revision"],
        build_payload=lambda _: {**canvas, "revision": canvas["revision"] + 1})
    state = get_state(client, current).json()["data"]
    assert state["next_action"]["node_id"] == "bound-original"
    assert state["next_action"]["action"] == "generate_production_plan_draft"


def test_progress_exposes_the_scoped_source_for_analysis_reuse(selection_api, monkeypatch):
    from novelvideo.chat.reference_remix_continuation import coordinator
    client, _, commission, _, _, _ = selection_api
    run = {'run_id': 'original-run', 'project_id': 'p', 'canvas_id': 'c',
           'requester_user_id': 'u', 'agent_id': 'main', 'source_node_id': 'source-video',
           'analysis_node_id': 'a', 'commission_ref': commission['commission_ref']}
    monkeypatch.setattr(coordinator.store, 'production_for_actor', lambda *args: [run])
    response = get_state(client, commission)
    assert response.status_code == 200, response.text
    assert response.json()['data']['source_node_id'] == 'source-video'
    assert response.json()['data']['run_id'] == 'original-run'


def select(client, c, work, command="select"):
    result = client.post(
        BASE + "/select-material-work",
        json={
            "commission_ref": c["commission_ref"],
            "work_ref": work,
            "command_id": command,
        },
    )
    assert result.status_code == 200, result.text
    return result.json()["data"]


def attach_picture(store, work):
    from novelvideo.freezone import canvas_store

    Image.new("RGB", (16, 12), "red").save(store.project_dir / "picture.png")
    canvas = canvas_store.read_canvas(store.project_dir, "c")
    canvas["nodes"].append(
        {
            "id": "picture",
            "type": "imageNode",
            "data": {"imageUrl": "/static/projects/p/picture.png"},
        }
    )
    canvas_store.save_canvas(
        store.project_dir,
        "c",
        base_revision=canvas["revision"],
        build_payload=lambda _: {**canvas, "revision": canvas["revision"] + 1},
    )
    saved = store.read_latest(work["artifact_id"])
    payload = saved["payload"]
    payload["steps"][0]["node_id"] = "picture"
    saved = store.revise(
        saved["id"], saved["revision"], payload, saved["upstream_refs"]
    )
    return {"artifact_id": saved["id"], "revision": saved["revision"]}, canvas


def test_current_selected_work_only_and_get_never_adopts(selection_api):
    client, store, c, p, work, _ = selection_api
    older = selectable_work(store, p, "old")
    newest = selectable_work(store, p, "newest")
    response = get_state(client, c)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["work_ref"] is None
    assert data["next_action"]["kind"] == "select_material_work"
    assert {r["artifact_id"] for r in data["candidate_work_refs"]} == {
        r["artifact_id"] for r in (older, newest, work)
    }
    assert data["suggested_work_ref"] is None
    assert (
        commissions.read_reference_commission(store, c["commission_ref"], latest=True)[
            "commission_ref"
        ]
        == c["commission_ref"]
    )
    c = select(client, c, work)
    data = get_state(client, c).json()["data"]
    assert data["work_ref"]["artifact_id"] == work["artifact_id"]
    assert [
        w["work_ref"]["artifact_id"] for w in data["director_state"]["material_work"]
    ] == [work["artifact_id"]]
    assert data["progress"]["materials_total"] == 1
    assert data["analysis_node_id"] == "a"


def test_valid_pass_suppresses_duplicate_unavailable_review_and_changed_media_reopens(
    selection_api,
):
    client, store, c, _, work, _ = selection_api
    work, canvas = attach_picture(store, work)
    first = review_material_candidate(store, work, "image", canvas, "picture")
    confirmed = confirm_material_review(
        store, first["review_ref"], canvas, canvas_id="c", requester_user_id="u"
    )
    review_material_candidate(store, work, "image", canvas, "picture")
    c = select(client, c, work)
    data = get_state(client, c).json()["data"]
    assert data["progress"]["materials_available"] == 1
    assert data["progress"]["user_action_count"] == 0
    assert data["director_state"]["material_reviews"] == []
    assert data["next_action"]["owner"] == "system"
    assert data["next_action"]["kind"] == "accept_materials"
    # Revoking the exact pass makes the unchanged current unavailable review relevant.
    saved = store.read_latest(confirmed["review_ref"]["artifact_id"])
    payload = deepcopy(saved["payload"])
    payload["content"]["status"] = "rejected"
    store.revise(saved["id"], saved["revision"], payload, saved["upstream_refs"])
    data = get_state(client, c).json()["data"]
    assert data["progress"]["user_action_count"] == 1
    Image.new("RGB", (16, 12), "blue").save(store.project_dir / "picture.png")
    data = get_state(client, c).json()["data"]
    assert data["progress"]["materials_available"] == 1
    assert data["progress"]["materials_accepted"] == 0
    assert data["director_state"]["material_reviews"] == []
    assert data["next_action"]["owner"] == "system"
    assert data["next_action"]["kind"] == "check_materials"


def test_missing_media_is_unavailable_and_read_has_no_artifact_writes(selection_api):
    client, store, c, _, work, _ = selection_api
    work, canvas = attach_picture(store, work)
    review_material_candidate(store, work, "image", canvas, "picture")
    c = select(client, c, work)
    (store.project_dir / "picture.png").unlink()
    before = {str(p): p.read_bytes() for p in store.root.rglob("*.json")}
    data = get_state(client, c).json()["data"]
    assert data["progress"]["materials_available"] == 0
    assert data["progress"]["user_action_count"] == 0
    assert data["director_state"]["material_reviews"] == []
    assert {str(p): p.read_bytes() for p in store.root.rglob("*.json")} == before
    assert str(store.project_dir) not in str(data)


def test_branches_are_isolated_and_require_explicit_choice(selection_api):
    from novelvideo.freezone import canvas_store

    client, store, c, _, work, _ = selection_api
    c = select(client, c, work)
    other, plan = confirmed_plan(store)
    second = selectable_work(store, plan, "second")
    canvas = canvas_store.read_canvas(store.project_dir, "c")
    node = selection_canvas(plan)["nodes"][1]
    node["id"] = "plan-second"
    canvas["nodes"].append(node)
    canvas["edges"].append({"source": "a", "target": node["id"]})
    canvas_store.save_canvas(
        store.project_dir,
        "c",
        base_revision=canvas["revision"],
        build_payload=lambda _: {**canvas, "revision": canvas["revision"] + 1},
    )
    other = select(client, other, second, "select-second")
    data = client.get(BASE + "/production-state").json()["data"]
    assert data["selection_required"] is True
    assert len(data["branches"]) == 2
    for current, wanted in ((c, work), (other, second)):
        branch = get_state(client, current).json()["data"]
        assert branch["work_ref"]["artifact_id"] == wanted["artifact_id"]
        assert len(branch["director_state"]["commissions"]) == 1
        assert len(branch["director_state"]["material_work"]) == 1


@pytest.mark.parametrize(
    "status,expected",
    [
        ("stopped", "stopped"),
        ("cancelled", "stopped"),
        ("resume_failed", "waiting_user"),
        ("resuming", "waiting_user"),
    ],
)
def test_execution_uses_durable_status_and_old_failure_cannot_override_review(
    selection_api, monkeypatch, status, expected
):
    from novelvideo.chat.reference_remix_continuation import coordinator

    client, store, c, _, work, _ = selection_api
    work, canvas = attach_picture(store, work)
    review_material_candidate(store, work, "image", canvas, "picture")
    c = select(client, c, work)
    monkeypatch.setattr(
        coordinator.store,
        "production_for_actor",
        lambda *a: [
            {
                "project_id": "p",
                "canvas_id": "c",
                "requester_user_id": "u",
                "agent_id": "main",
                "commission_ref": c["commission_ref"],
                "status": status,
                "error": "old analysis failure",
                "analysis_node_id": "a",
                "run_id": "run",
            }
        ],
    )
    data = get_state(client, c).json()["data"]
    assert data["execution_status"] == expected
    assert not any(b["kind"] == "resume_analysis" for b in data["blockers"])
    if expected in {"stopped", "running"}:
        assert data["next_action"] is None
    else:
        assert data["next_action"]["kind"] == "review_materials"


@pytest.mark.parametrize(
    "change,expected",
    [("private", 403), ("actor", 403), ("missing_plan", 409), ("analysis", 409)],
)
def test_current_state_revalidates_scope_and_current_canvas(
    selection_api, monkeypatch, change, expected
):
    from novelvideo.freezone import canvas_store

    client, store, c, _, work, app = selection_api
    c = select(client, c, work)
    canvas = canvas_store.read_canvas(store.project_dir, "c")
    if change == "private":
        canvas.update(access_model="private", owner_principal_id="other")
    if change == "actor":
        from novelvideo.api.auth import get_api_user

        app.dependency_overrides[get_api_user] = lambda: {
            "username": "bob",
            "user_id": "other",
            "role": "owner",
        }
        # ProjectContext supplied by resolver remains the authenticated host authority.
        from novelvideo.api.routes import freezone

        async def resolve(*a, **k):
            from types import SimpleNamespace

            ctx = SimpleNamespace(
                project_id="p",
                state_dir=store.project_dir,
                output_dir=store.project_dir,
                requester_user_id="other",
                requester_username="bob",
            )
            return ctx, "bob", "demo", store.project_dir, str(store.project_dir)

        monkeypatch.setattr(freezone, "_resolve_freezone_project", resolve)
    if change == "missing_plan":
        canvas["nodes"] = [n for n in canvas["nodes"] if n["type"] != "scriptNode"]
    if change == "analysis":
        canvas["edges"] = []
    monkeypatch.setattr(canvas_store, "read_canvas", lambda *a: canvas)
    assert get_state(client, c).status_code == expected


def test_history_cursor_is_stable_scoped_and_bounded(selection_api):
    client, _, c, _, work, _ = selection_api
    c = select(client, c, work)
    first = client.get(
        BASE + "/history",
        params={"commission_id": c["commission_ref"]["artifact_id"], "limit": 2},
    )
    assert first.status_code == 200, first.text
    page = first.json()["data"]
    assert len(page["items"]) == 2
    assert page["next_cursor"]
    second = client.get(
        BASE + "/history",
        params={
            "commission_id": c["commission_ref"]["artifact_id"],
            "limit": 2,
            "cursor": page["next_cursor"],
        },
    ).json()["data"]
    assert len(second["items"]) == 2
    assert {(i["id"], i["revision"]) for i in page["items"]}.isdisjoint(
        {(i["id"], i["revision"]) for i in second["items"]}
    )
    assert (
        client.get(
            BASE + "/history",
            params={"commission_id": c["commission_ref"]["artifact_id"], "limit": 101},
        ).status_code
        == 422
    )
    assert (
        client.get(
            BASE + "/history",
            params={
                "commission_id": c["commission_ref"]["artifact_id"],
                "cursor": "invalid",
            },
        ).status_code
        == 422
    )


def test_unique_candidate_suggestion_is_explicit_and_no_get_selection(selection_api):
    client, store, c, _, work, _ = selection_api
    data = get_state(client, c).json()["data"]
    assert data["suggested_work_ref"]["artifact_id"] == work["artifact_id"]
    assert data["work_ref"] is None
    assert (
        commissions.read_reference_commission(
            store, c["commission_ref"], latest=True
        ).get("work_ref")
        is None
    )


def test_history_cursor_cannot_be_reused_for_another_agent_scope(selection_api):
    client, _, c, _, work, _ = selection_api
    c = select(client, c, work)
    page = client.get(
        BASE + "/history",
        params={"commission_id": c["commission_ref"]["artifact_id"], "limit": 1},
    ).json()["data"]
    response = client.get(
        BASE + "/history",
        params={
            "commission_id": c["commission_ref"]["artifact_id"],
            "agent_id": "other",
            "cursor": page["next_cursor"],
        },
    )
    assert response.status_code == 422
    assert page["ordering_provenance"] == "artifact_id_revision"


def test_same_counts_but_changed_media_invalidate_projection_revision(selection_api):
    client, store, c, _, work, _ = selection_api
    work, _ = attach_picture(store, work)
    c = select(client, c, work)
    first = get_state(client, c).json()["data"]
    Image.new("RGB", (16, 12), "blue").save(store.project_dir / "picture.png")
    second = get_state(client, c).json()["data"]
    assert first["progress"] == second["progress"]
    assert first["projection_revision"] != second["projection_revision"]


def test_original_compatible_pass_suppresses_new_unavailable_after_independent_accept(
    tmp_path,
):
    from test_independent_material_review_continuity import fixture, accept
    from novelvideo.freezone.remix.production_inventory import _material_evidence

    store, original, canvas, reviews = fixture(tmp_path)
    current = accept(store, original, reviews, 0)["work_ref"]
    review_material_candidate(store, current, "s1", canvas, "n1")
    work = store.read_latest(current["artifact_id"])
    facts, actions, pending, steps, _ = _material_evidence(
        store, work, canvas, "canvas", tmp_path
    )
    assert next(f for f in facts if f["step_id"] == "s0")["accepted"] is True
    assert not any(r["step_id"] == "s1" for r in pending)
    assert (
        next(a for a in actions if a["step_id"] == "s1")["kind"] == "accept_materials"
    )
    assert next(s for s in steps if s["step_id"] == "s1")["status"] == "checked"


def test_current_output_review_uses_actual_current_render_and_needs_no_completed_receipt(
    scene,
):
    from novelvideo.freezone.remix.production_inventory import _details
    from novelvideo.freezone.remix.production_output_review import prepare_output_review

    store, ctx, canvas, cref, *_ = scene
    saved = prepare_output_review(store, ctx, canvas, cref, "compose")
    saved.update(created_by="u", project_id="p")
    store.created_by = "u"
    store._read = lambda *a: saved
    store._latest_revision = lambda *a: 1
    from novelvideo.freezone.remix.reference_commission import read_reference_commission

    c = read_reference_commission(store, cref)
    detail = _details(store, [saved], c, None, canvas)
    assert detail["output_reviews"][0]["review_ref"]["artifact_id"] == saved["id"]
    path = store.project_dir / "freezone/remix_renders/test.mp4"
    path.write_bytes(path.read_bytes() + b"changed")
    assert _details(store, [saved], c, None, canvas)["output_reviews"] == []


def test_current_speech_corrections_resolve_original_words_and_check_current_media(
    tmp_path, monkeypatch
):
    from test_agent_speech_review import setup
    from novelvideo.freezone.remix.agent_speech_review import (
        get_speech_review,
        preview_speech_correction,
        _source,
    )
    from novelvideo.freezone.remix import production_inventory as inventory

    store, ref, rec = setup(tmp_path)
    raw = get_speech_review(store, ref, "take", rec)
    word = raw["recognized_words"][0]
    saved = preview_speech_correction(
        store,
        ref,
        "take",
        rec,
        [{"word_id": word["id"], "text": "为什么", "reason": "ASR typo"}],
        actor_id="agent-1",
    )
    _, _, resource, _, _ = _source(store, ref, "take", rec)
    canvas = {
        "nodes": [
            {
                "id": "plan-node",
                "type": "scriptNode",
                "data": {
                    "productionPlanV2Ref": {
                        "artifactId": ref["artifact_id"],
                        "revision": 1,
                    }
                },
            },
            {"id": resource["node_id"], "data": {}},
        ]
    }
    c = {
        "commission_ref": {"artifact_id": "c", "revision": 1},
        "plan_ref": ref,
        "phase": "aligning_speech",
    }
    monkeypatch.setattr(inventory, "canvas_material_resource", lambda *a: resource)
    with store._locked() as guard:
        proxy = inventory._LockedEvidenceStore(store, guard)
        details = inventory._details(proxy, [saved], c, None, canvas)
        assert (
            details["speech_corrections"][0]["original_words"][0]["text"]
            == word["text"]
        )
        assert details["speech_corrections"][0]["node_id"] == "plan-node"
        monkeypatch.setattr(
            inventory,
            "canvas_material_resource",
            lambda *a: {**resource, "media_hash": "0" * 64},
        )
        assert (
            inventory._details(proxy, [saved], c, None, canvas)["speech_corrections"]
            == []
        )


def test_old_analysis_failure_is_history_when_current_work_can_be_checked(
    selection_api, monkeypatch
):
    from novelvideo.chat.reference_remix_continuation import coordinator

    client, store, c, _, work, _ = selection_api
    work, _ = attach_picture(store, work)
    c = select(client, c, work)
    monkeypatch.setattr(
        coordinator.store,
        "production_for_actor",
        lambda *a: [
            {
                "project_id": "p",
                "canvas_id": "c",
                "requester_user_id": "u",
                "agent_id": "main",
                "commission_ref": c["commission_ref"],
                "status": "resume_failed",
                "run_id": "old",
                "error": "old analysis continuation interrupted",
            }
        ],
    )
    data = get_state(client, c).json()["data"]
    assert data["execution_status"] == "waiting_agent"
    assert data["next_action"]["kind"] == "check_materials"


def test_current_user_execution_blocker_is_not_guessed_from_phase(
    selection_api, monkeypatch
):
    from novelvideo.chat.reference_remix_continuation import coordinator

    client, _, c, _, work, _ = selection_api
    c = select(client, c, work)
    monkeypatch.setattr(
        coordinator.store,
        "production_for_actor",
        lambda *a: [
            {
                "project_id": "p",
                "canvas_id": "c",
                "requester_user_id": "u",
                "agent_id": "main",
                "commission_ref": c["commission_ref"],
                "status": "preparing_materials",
                "run_id": "current",
                "commission_phase": "preparing_materials",
                "execution_blocker": {
                    "code": "user_required",
                    "message": "confirm native workflow",
                },
            }
        ],
    )
    data = get_state(client, c).json()["data"]
    assert data["execution_status"] == "waiting_user"
    assert any(b["kind"] == "resolve_execution_blocker" for b in data["blockers"])
    assert data["next_action"]["owner"] == "user"


def test_browser_details_redact_credentials_and_paths_without_changing_creative_text():
    from novelvideo.freezone.remix.production_inventory import _safe

    value = {
        "summary": "Why? Better: yes#1",
        "api_key": "secret",
        "token": "hidden",
        "source_url": "https://user:pass@example.com/video?token=secret",
        "host_path": "/Users/user/private.mp4",
        "preview_url": "/api/v1/projects/p/media/movie.mp4?token=secret",
    }
    cleaned = _safe(value)
    assert cleaned["summary"] == value["summary"]
    assert "api_key" not in cleaned and "token" not in cleaned
    assert cleaned["source_url"] is None and cleaned["host_path"] is None
    assert cleaned["preview_url"] == "/api/v1/projects/p/media/movie.mp4"


def test_read_rechecks_private_canvas_under_guard(selection_api, monkeypatch):
    from novelvideo.freezone import canvas_store

    client, _, c, _, work, _ = selection_api
    c = select(client, c, work)
    original = canvas_store.read_canvas
    count = 0

    def changed(*args):
        nonlocal count
        count += 1
        value = original(*args)
        return (
            value
            if count == 1
            else {**value, "access_model": "private", "owner_principal_id": "other"}
        )

    monkeypatch.setattr(canvas_store, "read_canvas", changed)
    assert get_state(client, c).status_code == 403
    assert count == 2


def test_current_plan_review_same_identity_revocation_blocks_ready_actions(
    selection_api,
):
    client, store, c, _, work, _ = selection_api
    work, canvas = attach_picture(store, work)
    pending = review_material_candidate(store, work, "image", canvas, "picture")
    confirm_material_review(
        store, pending["review_ref"], canvas, canvas_id="c", requester_user_id="u"
    )
    c = select(client, c, work)
    first = get_state(client, c).json()["data"]
    assert first["next_action"]["kind"] == "accept_materials"
    review = store.read_latest(c["review_ref"]["artifact_id"])
    rejected = deepcopy(review["payload"])
    from novelvideo.freezone.remix.production_review import evaluate_production_review

    findings = deepcopy(rejected["findings"])
    findings[0]["verdict"] = "change"
    plan = store.read(c["plan_ref"]["artifact_id"], c["plan_ref"]["revision"])
    rejected.update(
        evaluate_production_review(plan["payload"], c["direction"], findings)
    )
    # A separate review identity never revokes the commission's pinned approval.
    store.create("production_review.v1", rejected, review["upstream_refs"])
    assert (
        get_state(client, c).json()["data"]["next_action"]["kind"] == "accept_materials"
    )
    store.revise(review["id"], review["revision"], rejected, review["upstream_refs"])
    data = get_state(client, c).json()["data"]
    assert data["next_action"]["kind"] == "review_production_plan"
    assert any(b["kind"] == "review_production_plan" for b in data["blockers"])
    assert data["plan_review_valid"] is False
    assert data["director_state"]["material_reviews"] == []
    assert data["projection_revision"] != first["projection_revision"]


@pytest.mark.parametrize("valid", [False, True])
def test_material_decision_details_require_current_host_review_basis(
    selection_api, valid
):
    from novelvideo.freezone.remix.reference_director import prepare_reference_decision

    client, store, c, _, work, _ = selection_api
    work, canvas = attach_picture(store, work)
    review = review_material_candidate(store, work, "image", canvas, "picture")
    c = select(client, c, work)
    proposal = {"work_ref": work, "plan_ref": c["plan_ref"]}
    if valid:
        proposal["candidate"] = {
            "step_id": "image",
            "review_ref": review["review_ref"],
            "media_hash": review["review"]["resource"]["media_hash"],
        }
    prepare_reference_decision(
        store,
        c["commission_ref"],
        "material",
        proposal,
        "review this picture",
        "material-decision",
    )
    data = get_state(client, c).json()["data"]
    assert len(data["director_state"]["decisions"]) == int(valid)
    assert data["progress"]["user_action_count"] == 1


def test_inventory_retains_exact_native_media_and_review_after_both_cleanups(tmp_path):
    from datetime import datetime, timezone

    from test_material_settlement import native_material, settle
    from novelvideo.freezone.remix.production_inventory import _material_evidence
    from novelvideo.freezone.workflow_drafts import prune_expired_workflow_drafts
    from novelvideo.freezone.workflow_runs import (
        prune_workflow_runs,
        update_workflow_run,
    )

    fixture = native_material(tmp_path)
    settled = settle(fixture)
    store, state_dir, canvas, ref, native = fixture
    work = store.read_latest(ref["artifact_id"])
    before = _material_evidence(store, work, canvas, "canvas", state_dir)
    assert before[3][0]["status"] == "needs_review"
    assert before[3][0]["media_hash"] == settled["resource"]["media_hash"]
    update_workflow_run(
        project_dir=state_dir,
        canvas_id="canvas",
        run_id=native["run_id"],
        status="completed",
    )
    assert prune_workflow_runs(
        project_dir=state_dir,
        canvas_id="canvas",
        now=datetime(2100, 1, 1, tzinfo=timezone.utc),
    ) == [native["run_id"]]
    assert (
        prune_expired_workflow_drafts(
            project_dir=state_dir,
            canvas_id="canvas",
            now=10**12,
        )
        == 1
    )
    after = _material_evidence(store, work, canvas, "canvas", state_dir)
    assert after == before
    assert after[3][0]["preview_url"] == "/api/v1/projects/project/media/picture.png"
    assert after[2][0]["review_ref"] == settled["review_ref"]
    assert not any(
        a["kind"] == "prepare_materials" and a["step_id"] == "image" for a in after[1]
    )
    # Retained proof remains bound to the actual file, even after native cleanup.
    Image.new("RGB", (16, 12), "blue").save(store.project_dir / "picture.png")
    changed = _material_evidence(store, work, canvas, "canvas", state_dir)
    assert changed[3][0]["media_hash"] is None
    assert changed[2] == []


@pytest.mark.parametrize("native_status", ["failed", "interrupted", "cancelled"])
@pytest.mark.parametrize("current_user_blocker", [False, True])
def test_current_native_failure_remains_visible_alongside_another_material_review(
    selection_api,
    monkeypatch,
    native_status,
    current_user_blocker,
):
    from novelvideo.chat.reference_remix_continuation import coordinator
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.remix.material_work import prepare_material_work
    from novelvideo.freezone.workflow_runs import (
        create_workflow_run,
        update_workflow_run,
    )

    client, store, c, plan, _, _ = selection_api
    work = prepare_material_work(
        store,
        {"artifact_id": plan["id"], "revision": plan["revision"]},
        [
            {
                "id": "r",
                "description": "Reference image",
                "media_kind": "image",
                "final_step_id": "image",
            }
        ],
        [
            {
                "id": "image",
                "label": "Image",
                "media_kind": "image",
                "source": "upload",
            },
            {
                "id": "generation",
                "label": "Generation",
                "media_kind": "image",
                "source": "generate",
                "recipe_id": "general-image",
                "prompt": "Generate another image",
            },
        ],
        "mixed-materials",
    )["work_ref"]
    work, canvas = attach_picture(store, work)
    canvas = canvas_store.read_canvas(store.project_dir, "c")
    canvas["nodes"].append({"id": "generation", "type": "imageNode", "data": {}})
    canvas_store.save_canvas(
        store.project_dir,
        "c",
        base_revision=canvas["revision"],
        build_payload=lambda _: {**canvas, "revision": canvas["revision"] + 1},
    )
    native = create_workflow_run(
        project_dir=store.project_dir,
        project_id="p",
        canvas_id="c",
        actor_id="u",
        actions=[
            {
                "node_id": "generation",
                "action": "generate_image",
                "recipe_id": "general-image",
            }
        ],
    )
    update_workflow_run(
        project_dir=store.project_dir,
        canvas_id="c",
        run_id=native["run_id"],
        status=native_status,
        action_updates=[
            {
                "node_id": "generation",
                "action": "generate_image",
                "status": "failed",
                "error": "PRIVATE_PROVIDER_DETAIL",
            }
        ],
    )
    saved = store.read_latest(work["artifact_id"])
    saved["payload"]["steps"][1].update(
        node_id="generation",
        status="submitted",
        execution={
            "run_id": native["run_id"],
            "canvas_id": "c",
            "task_id": "generation-task",
        },
    )
    saved = store.revise(
        saved["id"], saved["revision"], saved["payload"], saved["upstream_refs"]
    )
    work = {"artifact_id": saved["id"], "revision": saved["revision"]}
    canvas = canvas_store.read_canvas(store.project_dir, "c")
    review = review_material_candidate(store, work, "image", canvas, "picture")
    c = select(client, c, work)
    if current_user_blocker:
        monkeypatch.setattr(
            coordinator.store,
            "production_for_actor",
            lambda *a: [
                {
                    "project_id": "p",
                    "canvas_id": "c",
                    "requester_user_id": "u",
                    "agent_id": "main",
                    "commission_ref": c["commission_ref"],
                    "commission_phase": c["phase"],
                    "status": "resume_failed",
                    "run_id": "continuation",
                    "execution_blocker": {"code": "user_required"},
                }
            ],
        )
    before = {str(p): p.read_bytes() for p in store.root.rglob("*.json")}
    response = get_state(client, c)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["progress"]["user_action_count"] == (
        2 if current_user_blocker and native_status != "cancelled" else 1
    )
    assert (
        data["director_state"]["material_reviews"][0]["review_ref"]
        == review["review_ref"]
    )
    assert data["material_steps"][0]["status"] == "needs_review"
    assert data["next_action"] is None
    assert data["ready_system_actions"] == []
    assert "PRIVATE_PROVIDER_DETAIL" not in str(data)
    if native_status == "cancelled":
        assert data["execution_status"] == "stopped"
        assert not any(b["kind"] == "recover_execution" for b in data["blockers"])
    else:
        assert not any(b["kind"] == "prepare_materials" for b in data["blockers"])
        assert data["execution_status"] == "failed"
        assert {
            "owner": "agent",
            "kind": "recover_execution",
            "action_id": native["run_id"],
        } in data["blockers"]
        assert data["execution_problem"] == [
            {
                "run_id": native["run_id"],
                "step_id": "generation",
                "status": native_status,
                "message": "Native material execution failed."
                if native_status == "failed"
                else "Native material execution was interrupted.",
            }
        ]
    assert {str(p): p.read_bytes() for p in store.root.rglob("*.json")} == before


def test_selected_ready_material_draft_exposes_exact_submit_action(selection_api):
    from novelvideo.freezone.workflow_drafts import create_workflow_draft
    from novelvideo.freezone.remix.material_execution import attach_material_execution_draft
    from novelvideo.freezone.remix.material_work import material_workflow_intent
    client, store, c, _, work_ref, _ = selection_api
    saved = store.read(work_ref['artifact_id'], work_ref['revision'])
    saved['payload']['steps'][0].update(source='generate', recipe_id='general-image', prompt='Reference image')
    saved = store.revise(saved['id'], saved['revision'], saved['payload'], saved['upstream_refs'])
    work_ref = {'artifact_id': saved['id'], 'revision': saved['revision']}
    c = select(client, c, work_ref)
    intent = material_workflow_intent(saved['payload'])
    draft = create_workflow_draft(project_dir=store.project_dir, project_id='p', canvas_id='c',
        intent=intent, compiled={'ok':True, 'plan':{'nodes':[]}})
    attached = attach_material_execution_draft(store, work_ref, canvas_id='c',
        draft_id=draft['draft_id'], draft_revision=1)
    from novelvideo.freezone.remix.material_execution import read_material_state
    observed = read_material_state(store, attached['work_ref'], canvas_id='c', include_workflow_intent=False)
    assert observed['material_execution']['drafts'], observed
    state = get_state(client, c).json()['data']
    assert state['work_ref']['revision'] == attached['work_ref']['revision']
    assert state['execution_status'] == 'waiting_agent'
    assert state['next_action'] == {'owner':'agent', 'kind':'submit_canvas_approval',
        'action_id':'submit:' + draft['draft_id'] + ':1', 'tool':'freezone_confirm_workflow_draft',
        'draft_id':draft['draft_id'], 'revision':1}
    assert state['progress']['user_action_count'] == 0
