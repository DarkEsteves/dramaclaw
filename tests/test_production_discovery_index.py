from pathlib import Path
from types import SimpleNamespace

import pytest

from novelvideo.freezone.remix.store import RemixArtifactStore


def setup(tmp_path):
    ctx = SimpleNamespace(output_dir=tmp_path, project_id="p", requester_user_id="u",
                          owner_type="user", owner_id="owner")
    store = RemixArtifactStore(tmp_path, "p", created_by="u")
    payload = {"schema_version": 2, "goal": "test", "output": {
        "width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
        "beats": [], "resources": [], "placements": [], "provenance": {}}
    plan = store.create("production_plan.v2", payload, [])
    return ctx, store, plan


def test_warm_discovery_uses_scoped_heads_without_history_enumeration(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.production_discovery_index import read_with_production_index
    from novelvideo.freezone.remix.production_inventory import discover_artifact_heads
    ctx, store, plan = setup(tmp_path)
    def read(actual, **kwargs):
        with actual._locked():
            return discover_artifact_heads(actual)
    first = read_with_production_index(ctx, store, canvas_id="c", agent_id="main", reader=read)
    assert first[0][0]["id"] == plan["id"]
    def forbidden(*args, **kwargs):
        raise AssertionError("warm discovery must not enumerate artifact history")
    monkeypatch.setattr(Path, "iterdir", forbidden)
    monkeypatch.setattr(Path, "glob", forbidden)
    second = read_with_production_index(ctx, store, canvas_id="c", agent_id="main", reader=read)
    assert second == first


def test_new_publication_invalidates_cached_heads(tmp_path):
    from novelvideo.freezone.remix.production_discovery_index import read_with_production_index
    from novelvideo.freezone.remix.production_inventory import discover_artifact_heads
    ctx, store, plan = setup(tmp_path)
    def read(actual, **kwargs):
        with actual._locked(): return discover_artifact_heads(actual)
    read_with_production_index(ctx, store, canvas_id="c", agent_id="main", reader=read)
    store.revise(plan["id"], 1, {**plan["payload"], "goal": "new"}, [])
    result = read_with_production_index(ctx, store, canvas_id="c", agent_id="main", reader=read)
    assert result[0][0]["revision"] == 2


def test_missing_deployment_index_uses_bounded_authoritative_read(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.production_discovery_index import read_with_production_index
    from novelvideo.freezone.remix.production_inventory import discover_artifact_heads
    ctx, store, plan = setup(tmp_path)
    monkeypatch.setattr("novelvideo.ports.get_production_index", lambda: None)
    def read(actual, **kwargs):
        with actual._locked(): return discover_artifact_heads(actual)
    result = read_with_production_index(ctx, store, canvas_id="c", agent_id="main", reader=read)
    assert result[0][0]["id"] == plan["id"]


def discovery(ctx, store):
    from novelvideo.freezone.remix.production_discovery_index import read_with_production_index
    from novelvideo.freezone.remix.production_inventory import discover_artifact_heads

    def read(actual, **kwargs):
        with actual._locked():
            return discover_artifact_heads(actual)
    return read_with_production_index(ctx, store, canvas_id="c", agent_id="main", reader=read)


def test_deleted_cached_head_is_omitted_and_hint_rebuilt(tmp_path):
    ctx, store, plan = setup(tmp_path)
    assert discovery(ctx, store)[0][0]["id"] == plan["id"]
    store._path(plan["id"], 1, ".json").unlink()
    assert discovery(ctx, store) == ([], False)
    assert discovery(ctx, store) == ([], False)


def test_corrupt_epoch_disables_hint_without_hiding_authoritative_facts(tmp_path):
    ctx, store, plan = setup(tmp_path)
    discovery(ctx, store)
    (store.root / "_production_epoch.json").write_text('{"epoch":"broken"}')
    assert discovery(ctx, store)[0][0]["id"] == plan["id"]


@pytest.mark.parametrize("method", ["read_current", "publish"])
def test_cache_io_failure_does_not_change_authoritative_result(tmp_path, monkeypatch, method):
    from novelvideo.ports.local.production_index import LocalProductionIndex

    ctx, store, plan = setup(tmp_path)
    expected = discovery(ctx, store)
    index = LocalProductionIndex()
    monkeypatch.setattr("novelvideo.ports.get_production_index", lambda: index)

    def unavailable(*args, **kwargs):
        raise OSError("fixture cache unavailable")
    monkeypatch.setattr(index, method, unavailable)
    updated = store.revise(plan["id"], 1, {**plan["payload"], "goal": "updated"}, [])
    actual = discovery(ctx, store)
    assert actual[0][0]["revision"] == updated["revision"]
    assert actual != expected
    monkeypatch.undo()
    assert discovery(ctx, store) == actual


@pytest.mark.parametrize("field", ["project_id", "created_by"])
def test_revoked_cached_header_never_returns_foreign_scope(tmp_path, field):
    import json

    ctx, store, plan = setup(tmp_path)
    discovery(ctx, store)
    path = store._path(plan["id"], 1, ".json")
    header = json.loads(path.read_text())
    header[field] = "other"
    path.write_text(json.dumps(header))
    assert discovery(ctx, store) == ([], False)


def test_hint_contains_only_refs_and_scope_not_speech_or_signed_urls(tmp_path):
    import json

    ctx, store, plan = setup(tmp_path)
    payload = {**plan["payload"], "goal": "private full speech https://media.invalid/?signature=secret"}
    store.revise(plan["id"], 1, payload, [])
    discovery(ctx, store)
    records = list((store.project_dir / "freezone" / "_production_index").rglob("*.json"))
    assert records
    for path in records:
        text = path.read_text()
        assert "private full speech" not in text
        assert "signature=secret" not in text
        record = json.loads(text)["record"]
        assert set(record) == {"commission_ref", "artifact_epoch", "heads", "truncated",
                               "projection_revision", "index_revision"}


@pytest.mark.parametrize('malformed', [['malformed'], 'malformed', 1])
def test_malformed_local_index_reference_keeps_authorized_fallback(tmp_path, malformed):
    import json
    from novelvideo.ports.local.production_index import LocalProductionIndex
    from novelvideo.freezone.remix.production_discovery_index import index_scope

    ctx, store, plan = setup(tmp_path)
    discovery(ctx, store)
    folder, _, _ = LocalProductionIndex._location(index_scope(ctx, 'c', 'main'), 'inventory')
    path = folder / 'head.json'
    envelope = json.loads(path.read_text())
    envelope['record']['commission_ref'] = malformed
    path.write_text(json.dumps(envelope))
    assert discovery(ctx, store)[0][0]['id'] == plan['id']


def test_malformed_deployment_port_return_keeps_authorized_fallback(tmp_path, monkeypatch):
    ctx, store, plan = setup(tmp_path)

    class Index:
        def read_current(self, *args):
            return ['malformed']

        def publish(self, *args, **kwargs):
            raise OSError('optional unsupported cache')

    monkeypatch.setattr('novelvideo.ports.get_production_index', lambda: Index())
    assert discovery(ctx, store)[0][0]['id'] == plan['id']
