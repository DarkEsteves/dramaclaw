"""Bounded production discovery rebuilt through the native default lane."""
from novelvideo.task_backend.registry import register_project_task_runner


def run_production_index_rebuild(envelope, ctx):
    from novelvideo.ports import get_production_index
    from novelvideo.project_context import ProjectContext, require_project_home_node
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.remix.source_speech import candidate_scope
    from novelvideo.freezone.remix.production_discovery_index import _DiscoveryIndexedStore, index_scope
    from novelvideo.freezone.remix.production_inventory import discover_artifact_heads
    from novelvideo.freezone.remix.store import RemixArtifactStore

    if not isinstance(ctx, ProjectContext):
        raise PermissionError("host production index context required")
    require_project_home_node(ctx, operation="production index rebuild")
    payload = envelope.get("payload") or {}
    if set(payload) != {"canvas_id", "agent_id", "artifact_epoch"}:
        raise ValueError("scoped production index task payload required")
    canvas_id, agent_id = payload["canvas_id"], payload["agent_id"]
    if not isinstance(agent_id, str) or not agent_id or len(agent_id) > 128:
        raise ValueError("invalid production index agent")
    port = get_production_index()
    if port is None:
        return {"status": "unavailable"}
    scope = index_scope(ctx, canvas_id, agent_id)
    store = RemixArtifactStore(ctx.output_dir, ctx.project_id, created_by=ctx.requester_user_id)
    with canvas_store.get_canvas_write_mutex().write_mutex(ctx.state_dir, canvas_id) as canvas_guard:
        canvas = canvas_store.read_canvas(ctx.state_dir, canvas_id)
        if canvas is None:
            raise PermissionError("production canvas unavailable")
        candidate_scope(ctx, canvas, canvas_id, require_write=False)
        indexed = _DiscoveryIndexedStore(store, None)
        with indexed._locked() as artifact_guard:
            if payload["artifact_epoch"] != indexed.captured_epoch:
                return {"status": "superseded"}
            if indexed.captured_epoch == "legacy":
                import uuid
                epoch = uuid.uuid4().hex
                store._atomic_json(store.root / "_production_epoch.json", {"epoch": epoch}, artifact_guard)
                indexed.captured_epoch = epoch
            discover_artifact_heads(indexed)
        record = indexed.captured
        if not record or record["artifact_epoch"] == "legacy":
            return {"status": "unavailable"}
        # Heads remain a hint: a concurrent authoring publication makes this
        # record cold immediately; never overwrite the author's epoch.
        if store.production_cache_epoch() != record["artifact_epoch"]:
            return {"status": "superseded"}
        try:
            old = port.read_current(scope, "inventory")
            if old is not None and (not isinstance(old, dict) or not isinstance(old.get("index_revision"), str)):
                raise ValueError("invalid production index revision")
        except OSError:
            return {"status": "unavailable"}
        except ValueError:
            recover = getattr(port, "recover_current", None)
            if not callable(recover):
                # Shared deployments may supply this capability. Its absence
                # must never cause installation/reset of a worker-local index.
                return {"status": "unavailable"}
            old = recover(scope, "inventory")
            if old is not None:
                # A valid publisher won before the scoped repair mutex. Do not
                # overwrite it with this worker's older recovery snapshot.
                return {"status": "superseded"}
        canvas_guard.reassert()
        try:
            port.publish(scope, record, expected_revision=old.get("index_revision") if old else None)
        except ValueError:
            return {"status": "superseded"}
        except OSError:
            return {"status": "unavailable"}
    return {"status": "completed"}


register_project_task_runner("remix_rebuild_production_index", run_production_index_rebuild,
                             requires_home_node=True, lane="default")
