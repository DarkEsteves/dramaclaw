from novelvideo.freezone.remix.store import RemixArtifactStore

import pytest

pytest_plugins = ["test_reference_material_selection_api"]


def indexed_api(selection_api, monkeypatch):
    from novelvideo.api.routes import freezone
    from novelvideo.chat.reference_remix_continuation import coordinator
    from novelvideo.ports.local.production_index import LocalProductionIndex
    from novelvideo.project_context import ProjectContext
    from types import SimpleNamespace

    resolve = freezone._resolve_freezone_project

    async def host_scope(*args, **kwargs):
        result = await resolve(*args, **kwargs)
        original = result[0]
        ctx = ProjectContext(original.project_id, "test", "user", "owner", "owner",
            original.requester_user_id, original.requester_username,
            (("user", original.requester_user_id),), "owner", "local",
            original.output_dir, original.state_dir, original.output_dir / "runtime", True)
        return (ctx, *result[1:])

    monkeypatch.setattr(freezone, "_resolve_freezone_project", host_scope)
    monkeypatch.setattr(coordinator.store, "list_for_actor", lambda *a: [])
    index = LocalProductionIndex()
    monkeypatch.setattr("novelvideo.ports.get_production_index", lambda: index)
    pending = []

    class Backend:
        async def enqueue_project_task(self, ctx, **kwargs):
            assert kwargs['queue_kind'] == 'default'
            assert kwargs['task_type'] == 'remix_rebuild_production_index'
            pending.append((ctx, kwargs['payload']))
            return SimpleNamespace(task_state=SimpleNamespace(task_id='isolated-index-rebuild'))

    monkeypatch.setattr("novelvideo.ports.get_task_backend", lambda: Backend())
    selection_api[0]._d1_index_tasks = pending
    return selection_api


def select_and_read(selection_api):
    from test_reference_material_selection_api import BASE

    client, store, commission, _, work, _ = selection_api
    selected = client.post(BASE + "/select-material-work", json={
        "commission_ref": commission["commission_ref"], "work_ref": work,
        "command_id": "performance-select"})
    assert selected.status_code == 200, selected.text
    params = {"commission_id": commission["commission_ref"]["artifact_id"]}
    response = client.get(BASE + "/production-state", params=params)
    assert response.status_code == 200, response.text
    if response.json()['data'].get('index_status') == 'refreshing':
        from novelvideo.task_backend.runners.production_index import run_production_index_rebuild
        pending = client._d1_index_tasks
        assert pending, 'cold GET must admit the native scoped rebuild after its leases close'
        while pending:
            ctx, payload = pending.pop(0)
            assert run_production_index_rebuild({'payload': payload}, ctx)['status'] == 'completed'
        response = client.get(BASE + "/production-state", params=params)
        assert response.status_code == 200, response.text
        assert response.json()['data']['index_status'] == 'ready'
    return response.json()["data"], params


def forbid_artifact_enumeration(monkeypatch, root):
    from pathlib import Path

    for name in ("iterdir", "glob", "rglob"):
        original = getattr(Path, name)

        def guarded(path, *args, _original=original, **kwargs):
            if path == root or root in path.parents:
                raise AssertionError("warm production API enumerated artifact history")
            return _original(path, *args, **kwargs)

        monkeypatch.setattr(Path, name, guarded)


def test_selected_work_warm_api_never_enumerates_history(selection_api, monkeypatch):
    from test_reference_material_selection_api import BASE

    indexed_api(selection_api, monkeypatch)
    expected, params = select_and_read(selection_api)
    client, store, _, _, work, _ = selection_api
    forbid_artifact_enumeration(monkeypatch, store.root)
    response = client.get(BASE + "/production-state", params=params)
    assert response.status_code == 200, response.text
    assert response.json()["data"] == expected
    assert expected["work_ref"]["artifact_id"] == work["artifact_id"]


@pytest.mark.parametrize("change,status", [("private", 403), ("target", 409)])
def test_warm_api_rechecks_current_canvas_authority(selection_api, monkeypatch, change, status):
    from novelvideo.freezone import canvas_store
    from test_reference_material_selection_api import BASE

    indexed_api(selection_api, monkeypatch)
    _, params = select_and_read(selection_api)
    client, store, *_ = selection_api
    canvas = canvas_store.read_canvas(store.project_dir, "c")
    if change == "private":
        canvas.update(access_model="private", owner_principal_id="other")
    else:
        canvas["nodes"] = [n for n in canvas["nodes"] if n["type"] != "scriptNode"]
    monkeypatch.setattr(canvas_store, "read_canvas", lambda *a: canvas)
    assert client.get(BASE + "/production-state", params=params).status_code == status


def test_warm_api_rechecks_current_media_without_epoch_write(selection_api, monkeypatch):
    from test_reference_material_selection_api import BASE
    from test_reference_production_state_api import attach_picture

    indexed_api(selection_api, monkeypatch)
    client, store, commission, plan, work, app = selection_api
    work, _ = attach_picture(store, work)
    expected, params = select_and_read((client, store, commission, plan, work, app))
    epoch = store.production_cache_epoch()
    (store.project_dir / "picture.png").unlink()
    forbid_artifact_enumeration(monkeypatch, store.root)
    response = client.get(BASE + "/production-state", params=params)
    assert response.status_code == 200, response.text
    current = response.json()["data"]
    assert expected["progress"]["materials_available"] == 1
    assert current["progress"]["materials_available"] == 0
    assert current["projection_revision"] != expected["projection_revision"]
    assert store.production_cache_epoch() == epoch


def test_revoked_review_reopens_current_action_despite_warm_hint(selection_api, monkeypatch):
    from copy import deepcopy
    from test_reference_material_selection_api import BASE
    from test_reference_production_state_api import attach_picture
    from novelvideo.freezone.remix.material_execution import review_material_candidate, confirm_material_review

    indexed_api(selection_api, monkeypatch)
    client, store, commission, plan, work, app = selection_api
    work, canvas = attach_picture(store, work)
    candidate = review_material_candidate(store, work, "image", canvas, "picture")
    approved = confirm_material_review(store, candidate["review_ref"], canvas,
                                       canvas_id="c", requester_user_id="u")
    review_material_candidate(store, work, "image", canvas, "picture")
    expected, params = select_and_read((client, store, commission, plan, work, app))
    assert expected["progress"]["user_action_count"] == 0
    saved = store.read_latest(approved["review_ref"]["artifact_id"])
    payload = deepcopy(saved["payload"])
    payload["content"]["status"] = "rejected"
    store.revise(saved["id"], saved["revision"], payload, saved["upstream_refs"])
    updated = client.get(BASE + "/production-state", params=params)
    assert updated.status_code == 200, updated.text
    current = updated.json()["data"]
    assert current["progress"]["user_action_count"] == 1
    assert current["next_action"]["kind"] == "review_materials"
    from novelvideo.task_backend.runners.production_index import run_production_index_rebuild
    while client._d1_index_tasks:
        ctx, payload = client._d1_index_tasks.pop(0)
        assert run_production_index_rebuild({'payload': payload}, ctx)['status'] == 'completed'
    current = client.get(BASE + "/production-state", params=params).json()["data"]
    assert current['index_status'] == 'ready'
    forbid_artifact_enumeration(monkeypatch, store.root)
    assert client.get(BASE + "/production-state", params=params).json()["data"] == current


def test_200_heads_1000_history_warm_api_is_bounded_and_scoped(selection_api, monkeypatch):
    from copy import deepcopy
    import json
    import platform
    import statistics
    import time
    from novelvideo.freezone import canvas_store
    from test_reference_commission import confirmed_plan, selectable_work, selection_canvas
    from test_reference_material_selection_api import BASE

    indexed_api(selection_api, monkeypatch)
    client, store, _, plan, *_ = selection_api
    canvas = canvas_store.read_canvas(store.project_dir, "c")
    other_commissions = []
    for number in range(9):
        commission, current_plan = confirmed_plan(store)
        selectable_work(store, current_plan, f"other-task-{number}")
        node = selection_canvas(current_plan)["nodes"][1]
        node["id"] = f"other-plan-{number}"
        canvas["nodes"].append(node)
        canvas["edges"].append({"source": "a", "target": node["id"]})
        other_commissions.append(commission["commission_ref"]["artifact_id"])
    canvas_store.save_canvas(store.project_dir, "c", base_revision=canvas["revision"],
                            build_payload=lambda _: {**canvas, "revision": canvas["revision"] + 1})
    heads = [p for p in store.root.iterdir() if p.is_dir()]
    fillers = [store.create("production_plan.v2", deepcopy(plan["payload"]), [])
               for _ in range(200 - len(heads))]
    # Populate immutable, valid historical envelopes in this isolated fixture.
    # Ten unrelated plan heads each carry 100 old revisions; none are selected.
    for filler in fillers[:10]:
        payload = store._canonical(filler["payload"])
        header = {key: value for key, value in filler.items() if key != "payload"}
        for revision in range(2, 102):
            store._path(filler["id"], revision, ".payload.json").write_bytes(payload)
            store._path(filler["id"], revision, ".json").write_bytes(store._canonical({
                **header, "revision": revision, "payload_path": f"{revision}.payload.json"}))
    assert sum(p.is_dir() for p in store.root.iterdir()) == 200
    expected, params = select_and_read(selection_api)
    assert not any(ref in json.dumps(expected) for ref in other_commissions)
    forbid_artifact_enumeration(monkeypatch, store.root)
    reads = []
    original = RemixArtifactStore._read

    def counted(actual, artifact_id, revision):
        reads.append((artifact_id, revision))
        return original(actual, artifact_id, revision)

    monkeypatch.setattr(RemixArtifactStore, "_read", counted)
    elapsed, counts = [], []
    for _ in range(30):
        reads.clear()
        start = time.perf_counter()
        response = client.get(BASE + "/production-state", params=params)
        elapsed.append((time.perf_counter() - start) * 1000)
        assert response.status_code == 200, response.text
        assert response.json()["data"] == expected
        assert not any(ref in response.text for ref in other_commissions)
        counts.append(len(reads))
    p95 = sorted(elapsed)[int(len(elapsed) * .95)]
    print("D1_PERF " + json.dumps({"hardware": platform.machine(), "os": platform.platform(),
          "heads": 200, "historical_revisions": 1000, "current_tasks": 10,
          "samples": len(elapsed), "median_ms": round(statistics.median(elapsed), 3),
          "p95_ms": round(p95, 3), "max_artifact_reads": max(counts),
          "history_directory_enumerations": 0}))
    assert p95 < 200, f"isolated warm API p95 {p95:.1f}ms exceeds 200ms target"


def test_artifact_publication_changes_cache_epoch(tmp_path):
    store = RemixArtifactStore(tmp_path, "p", created_by="u")
    first = store.production_cache_epoch()
    plan = {"schema_version": 2, "goal": "test",
            "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
            "beats": [], "resources": [], "placements": [], "provenance": {}}
    artifact = store.create("production_plan.v2", plan, [])
    second = store.production_cache_epoch()
    assert second != first
    store.revise(artifact["id"], 1, {**plan, "goal": "changed"}, [])
    assert store.production_cache_epoch() != second


def test_failed_publication_also_invalidates_previous_projection(tmp_path, monkeypatch):
    store = RemixArtifactStore(tmp_path, "p", created_by="u")
    first = store.production_cache_epoch()
    original = store._atomic_json
    def fail_payload(path, value, guard):
        if path.name.endswith(".payload.json"):
            raise OSError("fixture publication interrupted")
        return original(path, value, guard)
    monkeypatch.setattr(store, "_atomic_json", fail_payload)
    import pytest
    with pytest.raises(OSError):
        store.create("production_plan.v2",
                     {"schema_version": 2, "goal": "test", "output": {
                         "width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
                      "beats": [], "resources": [], "placements": [], "provenance": {}}, [])
    assert store.production_cache_epoch() != first
