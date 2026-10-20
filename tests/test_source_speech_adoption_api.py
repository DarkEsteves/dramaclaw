from types import SimpleNamespace

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_native_adoption_routes_host_context_and_original_plan_revision(monkeypatch):
    from novelvideo.api.routes import remix as routes
    import novelvideo.api.routes.reference_director as director
    import novelvideo.freezone.remix.source_speech_adoption as adoption
    import novelvideo.freezone.remix.production_advancement as advancement

    store = SimpleNamespace(project_id="p")
    ctx = SimpleNamespace(project_id="p")
    canvas = {"project_id": "p"}
    async def resolve_store(*args, **kwargs): return store
    async def context(*args, **kwargs): return ctx, canvas, store
    monkeypatch.setattr(routes, "_store", resolve_store)
    monkeypatch.setattr(director, "_context", context)
    payload = {"canvas_id": "c", "plan_ref": {"artifact_id": "plan", "revision": 3},
               "work_ref": {"artifact_id": "work", "revision": 6}, "take_id": "take",
               "observation_ref": {"artifact_id": "source", "revision": 1}}
    prepared = {"task_payload": payload}
    def prepare(actual_store, actual_ctx, actual_canvas, **kwargs):
        assert actual_store is store and actual_ctx is ctx and actual_canvas is canvas
        assert kwargs == payload
        return prepared
    async def enqueue(actual_ctx, actual_store, packet, *, command_id, node_revision):
        assert actual_ctx is ctx and actual_store is store and packet is prepared
        assert command_id == "adopt" and node_revision == 3
        return {"task_type": "remix_adopt_source_speech", "operation_id": "native",
                "task_id": "real-task", "operation_status": "pending", "output_refs": []}
    monkeypatch.setattr(adoption, "prepare_source_observation_adoption", prepare)
    monkeypatch.setattr(advancement, "enqueue_source_observation_adoption", enqueue)
    body = routes.ProcessActionBody(action="adopt_source_observation", payload=payload,
                                   idempotency_key="adopt", node_revision=99)
    result = await routes.enqueue_process_action("p", body, {})
    assert result["data"]["task_id"] == "real-task"
    assert result["data"]["action"] == "adopt_source_observation"


@pytest.mark.asyncio
async def test_native_adoption_never_accepts_model_scope_or_paths(monkeypatch):
    from novelvideo.api.routes import remix as routes
    async def resolve_store(*args, **kwargs): return SimpleNamespace(project_id="p")
    monkeypatch.setattr(routes, "_store", resolve_store)
    body = routes.ProcessActionBody(action="adopt_source_observation",
        payload={"media_path": "/tmp/foreign", "scope": {"tenant": "other"}},
        idempotency_key="bad", node_revision=1)
    with pytest.raises(HTTPException) as caught:
        await routes.enqueue_process_action("p", body, {})
    assert caught.value.status_code == 422
