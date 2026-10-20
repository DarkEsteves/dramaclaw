from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path

import pytest

from test_reference_remix_continuation import context, write_canvas
from novelvideo.freezone.remix import production_discovery_index as module
from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.ports.local.production_index import LocalProductionIndex


def setup(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    write_canvas(ctx)
    store = RemixArtifactStore(ctx.output_dir, ctx.project_id, created_by=ctx.requester_user_id)
    store.create('production_plan.v2', {'schema_version': 2, 'goal': 'isolated', 'output': {
        'width': 1080, 'height': 1920, 'fps_num': 30, 'fps_den': 1},
        'beats': [], 'resources': [], 'placements': [], 'provenance': {}}, [])
    index = LocalProductionIndex()
    monkeypatch.setattr('novelvideo.ports.get_production_index', lambda: index)
    return ctx, store, index


def read(ctx, store):
    from novelvideo.freezone.remix.production_inventory import discover_artifact_heads

    def reader(actual, **kwargs):
        with actual._locked():
            heads, truncated = discover_artifact_heads(actual)
            return {'head_ids': [head['id'] for head in heads], 'inventory_truncated': truncated}
    return module.read_with_production_index(ctx, store, canvas_id='canvas', agent_id='main',
                                            reader=reader, _defer_rebuild=True)


def test_cold_read_returns_refreshing_without_request_index_publication(tmp_path, monkeypatch):
    ctx, store, _ = setup(tmp_path, monkeypatch)
    result = read(ctx, store)
    assert result.get('index_status') == 'refreshing', 'cold production read needs refreshing status'
    assert result['head_ids']
    assert not (ctx.output_dir / 'freezone' / '_production_index').exists()


@pytest.mark.asyncio
async def test_rebuild_uses_native_default_lane_and_host_scope(tmp_path, monkeypatch):
    ctx, store, _ = setup(tmp_path, monkeypatch)
    assert hasattr(module, 'schedule_production_index_rebuild'), 'index rebuild needs native task admission'
    calls = []

    class Backend:
        async def enqueue_project_task(self, actual, **kwargs):
            assert actual is ctx
            calls.append(kwargs)
            return SimpleNamespace(task_state=SimpleNamespace(task_id='native'))

    monkeypatch.setattr('novelvideo.ports.get_task_backend', lambda: Backend())
    first = await module.schedule_production_index_rebuild(ctx, canvas_id='canvas', agent_id='main')
    second = await module.schedule_production_index_rebuild(ctx, canvas_id='canvas', agent_id='main')
    assert first['status'] == second['status'] == 'queued'
    assert calls[0]['scope'] == calls[1]['scope']
    assert calls[0]['queue_kind'] == 'default'
    assert calls[0]['task_type'] == 'remix_rebuild_production_index'
    assert calls[0]['payload'] == {'canvas_id': 'canvas', 'agent_id': 'main',
                                  'artifact_epoch': store.production_cache_epoch()}


def test_worker_builds_only_current_host_scope_and_rejects_revoked_private_canvas(tmp_path, monkeypatch):
    from novelvideo.task_backend.registry import get_project_task_runner, project_task_lane
    from novelvideo.task_backend import runners

    ctx, store, index = setup(tmp_path, monkeypatch)
    runner = get_project_task_runner('remix_rebuild_production_index')
    assert callable(runner), 'production rebuild must register a native runner'
    assert project_task_lane('remix_rebuild_production_index') == 'default'
    payload = {'canvas_id': 'canvas', 'agent_id': 'main', 'artifact_epoch': store.production_cache_epoch()}
    result = runner({'payload': payload}, ctx)
    assert result['status'] == 'completed'
    assert read(ctx, store)['index_status'] == 'ready'
    write_canvas(ctx, private=True)
    with pytest.raises((PermissionError, ValueError), match='private'):
        runner({'payload': payload}, replace(ctx, requester_user_id='other'))


def test_missing_ee_port_keeps_authorized_fallback_without_local_cache(tmp_path, monkeypatch):
    ctx, store, _ = setup(tmp_path, monkeypatch)
    monkeypatch.setattr('novelvideo.ports.get_production_index', lambda: None)
    result = read(ctx, store)
    assert result['head_ids']
    assert result.get('index_status') == 'unavailable'
    assert not (ctx.output_dir / 'freezone' / '_production_index').exists()


def test_legacy_worker_establishes_only_rebuild_metadata(tmp_path, monkeypatch):
    from novelvideo.task_backend.runners.production_index import run_production_index_rebuild

    ctx, store, _ = setup(tmp_path, monkeypatch)
    (store.root / '_production_epoch.json').unlink()
    before = {str(path): path.read_bytes() for path in store.root.rglob('*.json')}
    result = run_production_index_rebuild({'payload': {'canvas_id': 'canvas', 'agent_id': 'main',
                                                      'artifact_epoch': 'legacy'}}, ctx)
    assert result['status'] == 'completed'
    assert read(ctx, store)['index_status'] == 'ready'
    assert all(Path(path).read_bytes() == value for path, value in before.items())


def test_stale_native_rebuild_cannot_publish_old_heads(tmp_path, monkeypatch):
    from novelvideo.task_backend.runners.production_index import run_production_index_rebuild

    ctx, store, _ = setup(tmp_path, monkeypatch)
    epoch = store.production_cache_epoch()
    head = next(path for path in store.root.iterdir() if path.is_dir())
    saved = store.read_latest(head.name)
    store.revise(saved['id'], saved['revision'], {**saved['payload'], 'goal': 'new'}, saved['upstream_refs'])
    result = run_production_index_rebuild({'payload': {'canvas_id': 'canvas', 'agent_id': 'main',
                                                      'artifact_epoch': epoch}}, ctx)
    assert result['status'] == 'superseded'
    assert not (ctx.output_dir / 'freezone' / '_production_index').exists()


@pytest.mark.asyncio
async def test_lane_admission_failure_keeps_authoritative_fallback(tmp_path, monkeypatch):
    ctx, store, _ = setup(tmp_path, monkeypatch)

    class Busy:
        async def enqueue_project_task(self, *args, **kwargs):
            raise RuntimeError('native lane capacity exhausted')

    monkeypatch.setattr('novelvideo.ports.get_task_backend', lambda: Busy())
    assert await module.schedule_production_index_rebuild(ctx, canvas_id='canvas') == {'status': 'unavailable'}
    assert read(ctx, store)['head_ids']
    assert not (ctx.output_dir / 'freezone' / '_production_index').exists()


@pytest.mark.parametrize('corruption', ['reference', 'revision', 'json'])
def test_native_rebuild_recovers_corrupt_scope_without_touching_artifacts(tmp_path, monkeypatch, corruption):
    import json
    from novelvideo.task_backend.runners.production_index import run_production_index_rebuild

    ctx, store, index = setup(tmp_path, monkeypatch)
    payload = {'canvas_id': 'canvas', 'agent_id': 'main', 'artifact_epoch': store.production_cache_epoch()}
    assert run_production_index_rebuild({'payload': payload}, ctx)['status'] == 'completed'
    scope = module.index_scope(ctx, 'canvas', 'main')
    first = index.read_current(scope, 'inventory')
    index.publish(scope, {**first, 'projection_revision': 'second-snapshot'}, expected_revision=first['index_revision'])
    cursor = index.history(scope, 'inventory', cursor=None, limit=1)['next_cursor']
    folder, _, _ = index._location(scope, 'inventory')
    path = folder / 'head.json'
    envelope = json.loads(path.read_text())
    if corruption == 'reference':
        envelope['record']['commission_ref'] = ['malformed']
    elif corruption == 'revision':
        envelope['record']['index_revision'] = 'corrupted'
    path.write_text('{broken' if corruption == 'json' else json.dumps(envelope))
    before = {str(path): path.read_bytes() for path in store.root.rglob('*.json')}
    assert read(ctx, store)['index_status'] == 'refreshing'
    assert run_production_index_rebuild({'payload': payload}, ctx)['status'] == 'completed'
    assert read(ctx, store)['index_status'] == 'ready'
    assert list(folder.parent.glob('_corrupt_inventory_*'))
    assert all(Path(path).read_bytes() == value for path, value in before.items())
    with pytest.raises(ValueError, match='cursor'):
        index.history(scope, 'inventory', cursor=cursor, limit=1)


def test_repair_worker_preserves_a_valid_concurrent_publisher(tmp_path, monkeypatch):
    import json
    from novelvideo.task_backend.runners.production_index import run_production_index_rebuild

    ctx, store, index = setup(tmp_path, monkeypatch)
    payload = {'canvas_id': 'canvas', 'agent_id': 'main', 'artifact_epoch': store.production_cache_epoch()}
    assert run_production_index_rebuild({'payload': payload}, ctx)['status'] == 'completed'
    scope = module.index_scope(ctx, 'canvas', 'main')
    first = index.read_current(scope, 'inventory')
    folder, _, _ = index._location(scope, 'inventory')
    path = folder / 'head.json'
    envelope = json.loads(path.read_text())
    envelope['record']['index_revision'] = 'corrupted'
    path.write_text(json.dumps(envelope))
    assert hasattr(index, 'recover_current'), 'optional index needs scoped recovery under injected mutex'
    recover = index.recover_current
    winner = []

    def valid_publisher_before_repair(*args):
        assert recover(*args) is None
        winner.append(index.publish(scope, {**first, 'projection_revision': 'concurrent-valid'}, expected_revision=None))
        return recover(*args)

    monkeypatch.setattr(index, 'recover_current', valid_publisher_before_repair)
    assert run_production_index_rebuild({'payload': payload}, ctx)['status'] == 'superseded'
    assert index.read_current(scope, 'inventory') == winner[0]


def test_deployment_without_recovery_capability_keeps_authorized_fallback(tmp_path, monkeypatch):
    from novelvideo.task_backend.runners.production_index import run_production_index_rebuild

    ctx, store, _ = setup(tmp_path, monkeypatch)

    class ExternalIndex:
        def read_current(self, *args):
            raise ValueError('invalid external cache')

        def publish(self, *args, **kwargs):
            raise AssertionError('unsupported corrupt cache must never be blindly overwritten')

    monkeypatch.setattr('novelvideo.ports.get_production_index', lambda: ExternalIndex())
    payload = {'canvas_id': 'canvas', 'agent_id': 'main', 'artifact_epoch': store.production_cache_epoch()}
    result = read(ctx, store)
    assert result['head_ids']
    assert result['index_status'] == 'unavailable'
    assert run_production_index_rebuild({'payload': payload}, ctx) == {'status': 'unavailable'}
    assert not (ctx.output_dir / 'freezone' / '_production_index').exists()
