"""Cache only scoped discovery hints, then read authoritative artifact facts."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path


class _DiscoveryIndexedStore:
    def __init__(self, store, cached):
        self._store = store
        self._cached = cached
        self._active_cache = False
        self.captured = None
        self.captured_epoch = None

    def __getattr__(self, name):
        return getattr(self._store, name)

    @contextmanager
    def _locked(self):
        try:
            with self._store._locked() as guard:
                try:
                    epoch = self._store.production_cache_epoch()
                except (OSError, ValueError):
                    epoch = "legacy"
                self._active_cache = bool(self._cached and epoch != "legacy"
                                          and self._cached.get("artifact_epoch") == epoch)
                self.captured_epoch = epoch
                yield guard
                guard.reassert()
        finally:
            self._active_cache = False

    def _latest_revision(self, artifact_id):
        if self._active_cache:
            for row in self._cached["heads"]:
                if row[0] == artifact_id:
                    return row[1]
        return self._store._latest_revision(artifact_id)

    def production_discovery_heads(self):
        if not self._active_cache:
            return None
        heads = []
        for row in self._cached["heads"]:
            try:
                header = self._store._read_json(self._store._path(row[0], row[1], ".json"))
            except (OSError, ValueError):
                self._active_cache = False
                return None
            if (header.get("id") != row[0] or header.get("revision") != row[1]
                    or header.get("schema") != row[2] or header.get("project_id") != self.project_id
                    or header.get("created_by") != self.created_by):
                self._active_cache = False
                return None
            heads.append(header)
        return heads, self._cached["truncated"]

    def capture_production_heads(self, rows, truncated):
        self.captured = {
            "commission_ref": {"artifact_id": "inventory", "revision": 1},
            "artifact_epoch": self.captured_epoch,
            "heads": [[row["id"], row["revision"], row["schema"]] for row in rows],
            "truncated": truncated,
        }


def index_scope(ctx, canvas_id, agent_id):
    return {"project_dir": Path(ctx.output_dir), "project_id": ctx.project_id,
            "canvas_id": canvas_id, "agent_id": agent_id,
            "requester_user_id": ctx.requester_user_id,
            "tenant_id": f"{ctx.owner_type}:{ctx.owner_id}"}


def read_with_production_index(ctx, store, *, canvas_id: str, agent_id: str, reader,
                               _defer_rebuild=False, **arguments):
    from novelvideo.ports import get_production_index

    arguments = {**arguments, "canvas_id": canvas_id, "agent_id": agent_id}
    port = get_production_index()
    if port is None or not getattr(ctx, "owner_type", None) or not getattr(ctx, "owner_id", None):
        result = reader(store, **arguments)
        return {**result, "index_status": "unavailable"} if _defer_rebuild else result
    scope = index_scope(ctx, canvas_id, agent_id)
    cache_unavailable = False
    try:
        cached = port.read_current(scope, "inventory")
        if cached is not None and (
            not isinstance(cached, dict)
            or not isinstance(cached.get("heads"), list) or len(cached["heads"]) > 512
            or type(cached.get("truncated")) is not bool
            or any(not isinstance(row, list) or len(row) != 3
                   or not isinstance(row[0], str) or type(row[1]) is not int or row[1] < 1
                   or not isinstance(row[2], str) for row in cached["heads"])):
            cached = None
            cache_unavailable = not callable(getattr(port, "recover_current", None))
    except OSError:
        cached = None
        cache_unavailable = True
    except ValueError:
        cached = None
        cache_unavailable = not callable(getattr(port, "recover_current", None))
    indexed = _DiscoveryIndexedStore(store, cached)
    result = reader(indexed, **arguments)
    record = indexed.captured
    if _defer_rebuild:
        # Request readers retain bounded authorized evidence. Only the native
        # background lane publishes a cold rebuild, outside the request leases.
        status = "unavailable" if cache_unavailable else "refreshing" if record else "ready"
        return {**result, "index_status": status}
    if record and record["artifact_epoch"] != "legacy":
        try:
            # A later authoring write invalidates this hint instead of being
            # hidden by publication of an older discovery snapshot.
            if store.production_cache_epoch() == record["artifact_epoch"]:
                port.publish(scope, record,
                             expected_revision=cached.get("index_revision") if cached else None)
        except (OSError, ValueError):
            # Indexes are optional; successful authorized reads remain useful.
            pass
    return result


async def schedule_production_index_rebuild(ctx, *, canvas_id, agent_id="main"):
    """Native scoped admission; call only after the request write leases close."""
    import hashlib
    import json
    from novelvideo.ports import get_production_index, get_task_backend
    from novelvideo.project_context import ProjectContext, require_project_home_node
    from novelvideo.freezone import canvas_store
    from .source_speech import candidate_scope
    from .store import RemixArtifactStore

    if get_production_index() is None:
        return {"status": "unavailable"}
    if not isinstance(ctx, ProjectContext):
        return {"status": "unavailable"}
    try:
        require_project_home_node(ctx, operation="production index rebuild")
        canvas = canvas_store.read_canvas(ctx.state_dir, canvas_id)
        if canvas is None:
            return {"status": "unavailable"}
        candidate_scope(ctx, canvas, canvas_id, require_write=False)
        epoch = RemixArtifactStore(ctx.output_dir, ctx.project_id,
                                   created_by=ctx.requester_user_id).production_cache_epoch()
        scope = index_scope(ctx, canvas_id, agent_id)
        identity = {key: str(value) for key, value in scope.items() if key != "project_dir"}
        scope_key = hashlib.sha256(json.dumps({**identity, "artifact_epoch": epoch},
                                              sort_keys=True).encode()).hexdigest()
        queued = await get_task_backend().enqueue_project_task(ctx,
            task_type="remix_rebuild_production_index", product_surface="freezone",
            queue_kind="default", scope=scope_key,
            payload={"canvas_id": canvas_id, "agent_id": agent_id, "artifact_epoch": epoch})
        return {"status": "queued", "task_id": queued.task_state.task_id}
    except Exception:
        # Native lane saturation/authz/cache outages leave the authorized bounded
        # GET usable. A subsequent user read retries admission; no new poller.
        return {"status": "unavailable"}
