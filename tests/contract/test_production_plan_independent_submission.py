"""Full drafts and legacy-outline continuation each own one signed task."""
from copy import deepcopy
import json

from test_production_plan import native_plan

import pytest

from novelvideo.api.routes.remix import prepare_registered_action
from test_production_plan_independent_tasks import saved_outline


@pytest.mark.asyncio
async def test_full_draft_and_legacy_continuation_preserve_saved_edits(tmp_path, monkeypatch, signed_inline_backend):
    import asyncio
    import time
    from novelvideo.task_backend.runners import remix as runner
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.ports import registry
    from novelvideo.ports.local.tasks import InMemoryCancellationStore
    from novelvideo.project_context import ProjectContext
    from novelvideo.task_state import TaskStateManager

    manager = TaskStateManager()
    monkeypatch.setattr(registry, '_PORTS', dict(registry._PORTS))
    registry.register_port('cancellation_store', InMemoryCancellationStore())
    monkeypatch.setattr('novelvideo.task_state._task_manager', manager)
    monkeypatch.setattr('novelvideo.ports.local.tasks.get_task_manager', lambda: manager)
    ctx = ProjectContext('p', 'p', 'user', 'u', 'alice', 'u', 'alice', (('user', 'u'),),
        'owner', 'local', tmp_path, tmp_path, tmp_path, True)
    store, seed, _, _ = saved_outline(tmp_path)
    operations = RemixOperationStore(tmp_path, 'p')
    calls = []
    async def full(**kwargs):
        calls.append('full')
        assert 'production_plan_stage' not in kwargs
        return json.dumps(native_plan())
    async def arrangement(**kwargs):
        calls.append('arrangement')
        assert kwargs['plan']['beats'][0]['turns'][0]['text'] == '阶段间用户更新'
        return {**deepcopy(kwargs['plan']), 'authoring_stage': 'arranged'}
    monkeypatch.setattr('novelvideo.freezone.remix.production_plan.generate_recipe_text', full)
    monkeypatch.setattr(runner, 'generate_production_plan_arrangement', arrangement)
    backend = signed_inline_backend()
    async def submit(action, payload):
        prepared = prepare_registered_action(store, action, payload, idempotency_key=action, node_revision=1)
        op = operations.start(idempotency_key=action, input_refs=prepared['input_refs'],
            producer_version=prepared['producer_version'], normalized_parameters=prepared['normalized_parameters'], node_revision=1)
        queued = await backend.enqueue_project_task(ctx, product_surface='freezone',
            task_type=prepared['task_type'], queue_kind='default', episode=0,
            scope=op['operation_id'], payload={**prepared['task_payload'], 'operation_id': op['operation_id']})
        until = time.monotonic() + 5
        while time.monotonic() < until:
            state = manager.get_task_for_project(ctx, prepared['task_type'], 0, scope=op['operation_id'])
            if state and state.status in {'completed', 'failed'}:
                break
            await asyncio.sleep(0.02)
        assert state.status == 'completed', state.error
        assert state.task_id == queued.task_state.task_id
        return state, operations.get(op['operation_id'])['output_refs'][0]
    first, first_ref = await submit('generate_production_plan_draft', {'topic': '中文'})
    assert calls == ['full']
    assert first.metadata['authoring_stage'] == 'full'
    assert store.read(first_ref['artifact_id'], first_ref['revision'])['payload']['authoring_stage'] == 'arranged'
    # Continue a pre-existing outline, never submit a second task for the new full draft.
    saved = deepcopy(seed)
    saved['payload']['beats'][0]['turns'][0]['text'] = '阶段间用户更新'
    edited = store.revise(saved['id'], saved['revision'], saved['payload'], saved['upstream_refs'])
    second, second_ref = await submit('generate_production_plan_arrangement',
        {'plan_ref': {'artifact_id': edited['id'], 'revision': edited['revision']}})
    assert second.task_id != first.task_id
    assert second.metadata['authoring_stage'] == 'arrangement'
    assert calls == ['full', 'arrangement']
    assert second_ref == {'artifact_id': saved['id'], 'revision': 3}
