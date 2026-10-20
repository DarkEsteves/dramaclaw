import pytest
from concurrent.futures import ThreadPoolExecutor
from test_reference_remix_continuation import context
from novelvideo.chat.reference_remix_continuation import ReferenceRemixStore


def seeded(tmp_path):
    ctx = context(tmp_path)
    store = ReferenceRemixStore(tmp_path / 'runs.db')
    store.create(dict(run_id='r', project_id=ctx.project_id, requester_user_id=ctx.requester_user_id,
        canvas_id='canvas', agent_id='main', idempotency_key='r', status='awaiting_direction',
        commission_ref={'artifact_id':'c','revision':1}, commission_phase='awaiting_direction'))
    return ctx, store


def claim(ctx, store, turn):
    return store.claim_ordinary_turn(ctx, canvas_id='canvas', agent_id='main', commission_id='c',
        direction_revision=1, turn_id=turn)


def test_direction_transition_cannot_wake_second_submitter(tmp_path):
    ctx, store = seeded(tmp_path)
    owner = claim(ctx, store, 'human')
    assert owner
    assert store.wake_waiting(ctx, 'r', {'artifact_id':'c','revision':2}, 'drafting_plan') is None
    assert store.get('r')['status'] == 'awaiting_direction'


def test_claim_is_atomic_and_receipt_token_fenced(tmp_path):
    ctx, store = seeded(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        owners = list(pool.map(lambda t: claim(ctx, store, t), ['a','b']))
    assert sum(bool(o) for o in owners) == 1
    owner = next(o for o in owners if o)
    assert not store.finish_ordinary_turn('r', 'wrong', state='blocked', receipt={'error':'invalid'})
    assert store.finish_ordinary_turn('r', owner['token'], state='blocked', receipt={'error':'invalid'})
    assert store.wake_waiting(ctx, 'r', {'artifact_id':'c','revision':2}, 'drafting_plan') is None


def test_active_human_submission_lease_can_be_renewed_during_native_preflight(tmp_path):
    ctx, store = seeded(tmp_path)
    owner = claim(ctx, store, 'human')
    before = store.get('r')['ordinary_turn_owner']['lease_until']
    assert store.renew_ordinary_turn('r', owner['token'])
    assert store.get('r')['ordinary_turn_owner']['lease_until'] >= before
    assert not store.renew_ordinary_turn('r', 'wrong-token')
    assert store.finish_ordinary_turn('r', owner['token'], state='waiting_approval')
    assert not store.renew_ordinary_turn('r', owner['token'])


def test_real_completion_bypasses_owner_and_is_consumed_once(tmp_path):
    ctx, store = seeded(tmp_path)
    claim(ctx, store, 'human')
    ref = {'artifact_id':'c','revision':3}
    assert store.wake_waiting(ctx, 'r', ref, 'awaiting_plan_review', event_key='plan:task:saved')
    assert store.wake_waiting(ctx, 'r', ref, 'awaiting_plan_review', event_key='plan:task:saved') is None


def test_expired_owner_reconciles_existing_draft_before_recovery(tmp_path, monkeypatch):
    from novelvideo.chat import reference_remix_continuation as module
    ctx, store = seeded(tmp_path)
    owner = claim(ctx, store, 'human')
    run = store.get('r')
    run['ordinary_turn_owner']['lease_until'] = 0
    commission = {'commission_ref':{'artifact_id':'c','revision':2}, 'direction_revision':1}
    monkeypatch.setattr('novelvideo.freezone.remix.native_plan_stage.read_native_plan_stage',
                        lambda **kwargs: {'state':'ready_for_canvas_approval','draft_id':'d'})
    assert module.native_submission_blocks(ctx, run, {}, commission)
    monkeypatch.setattr('novelvideo.freezone.remix.native_plan_stage.read_native_plan_stage',
                        lambda **kwargs: {'state':'unprepared'})
    assert not module.native_submission_blocks(ctx, run, {}, commission)


def test_turn_finish_retains_missing_submission_as_blocker(tmp_path, monkeypatch):
    from novelvideo.chat import reference_remix_continuation as module
    ctx, store = seeded(tmp_path)
    owner = claim(ctx, store, 'human')
    monkeypatch.setattr(module, 'coordinator', type('C', (), {'store':store})())
    monkeypatch.setattr('novelvideo.freezone.remix.reference_commission.read_reference_commission',
        lambda *args, **kwargs: {'commission_ref':{'artifact_id':'c','revision':2},'direction_revision':1})
    monkeypatch.setattr(module.canvas_store, 'read_canvas', lambda *args: {})
    monkeypatch.setattr('novelvideo.freezone.remix.native_plan_stage.read_native_plan_stage',
                        lambda **kwargs: {'state':'unprepared'})
    module.finish_ordinary_production_turn({'owner':owner,'ctx':ctx})
    assert store.get('r')['ordinary_turn_owner']['state'] == 'blocked'


@pytest.mark.parametrize('interruption', [None, 'before_publish', 'after_publish'])
async def test_native_submission_claim_is_visible_before_direction_publication(tmp_path, monkeypatch, interruption):
    from types import SimpleNamespace
    from novelvideo.chat import reference_direction_commit as boundary
    from novelvideo.chat import reference_remix_continuation as module
    from novelvideo.chat.store import ChatScope, ChatStore
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.canvas_context_reader import saved_context_execution
    from novelvideo.freezone.remix.reference_commission import create_reference_commission
    from novelvideo.freezone.remix.store import RemixArtifactStore

    ctx = SimpleNamespace(project_id='p', requester_user_id='u', requester_username='u',
                          state_dir=tmp_path / 'state', output_dir=tmp_path / 'output')
    artifacts = RemixArtifactStore(ctx.output_dir, 'p', created_by='u')
    commission = create_reference_commission(artifacts, canvas_id='c', requester_user_id='u',
        analysis_node_id='analysis', goal='original',
        analysis_evidence={'task_id': 't', 'result_fingerprint': 'h'})
    scope = ChatScope(kind='project', id='project-name', surface='freezone',
                      canvas_id='c', agent_id='main', state_dir=str(ctx.state_dir))
    msg = ChatStore().append_message('u', scope, 'user', '按讨论好的方向创建方案节点。',
                                      turn_id='caller-turn')
    monkeypatch.setattr(canvas_store, 'read_canvas', lambda *_: {
        'project_id': 'p', 'canvas_id': 'c', 'revision': 1, 'nodes': [], 'edges': []})
    store = ReferenceRemixStore(tmp_path / 'route.db')
    store.create(dict(run_id='r', project_id='p', requester_user_id='u', canvas_id='c',
        agent_id='main', idempotency_key='r', status='awaiting_direction',
        commission_ref=commission['commission_ref'], commission_phase='awaiting_direction'))
    monkeypatch.setattr(module, 'coordinator', SimpleNamespace(store=store))
    async def approved(**kwargs):
        return {'verdict': 'approved', 'reason': 'current user requested creation'}
    monkeypatch.setattr(boundary, 'verify_direction_submission', approved, raising=False)
    original_prepare = boundary.prepare_reference_decision
    original_confirm = boundary.confirm_reference_decision
    def prepare(*args, **kwargs):
        assert store.get('r')['ordinary_turn_owner']['turn_id'] == 'caller-turn'
        if interruption == 'before_publish':
            raise RuntimeError('publication interrupted')
        return original_prepare(*args, **kwargs)
    def confirm(*args, **kwargs):
        result = original_confirm(*args, **kwargs)
        if interruption == 'after_publish':
            raise RuntimeError('receipt interrupted')
        assert store.wake_waiting(ctx, 'r', result['commission']['commission_ref'],
                                  'drafting_plan') is None
        return result
    monkeypatch.setattr(boundary, 'prepare_reference_decision', prepare)
    monkeypatch.setattr(boundary, 'confirm_reference_decision', confirm)
    identity = SimpleNamespace(surface='freezone', requester_user_id='u',
                               project_id='p', canvas_id='c', agent_id='main')
    with saved_context_execution(execution_context=identity, turn_id='caller-turn',
            state_dir=ctx.state_dir, human_message_id=msg['id'], chat_scope_id=scope.id):
        if interruption:
            with pytest.raises(RuntimeError):
                await boundary.ensure_confirmed_direction_for_entry(
                    ctx=ctx, canvas_id='c', agent_id='main', turn_id='caller-turn',
                    commission_ref=commission['commission_ref'], goal='保留节奏，换产品',
                    requested_operation={'kind':'prepare_production_nodes',
                                         'run_after_create':False, 'plan_count':1})
        else:
            result = await boundary.ensure_confirmed_direction_for_entry(
                ctx=ctx, canvas_id='c', agent_id='main', turn_id='caller-turn',
                commission_ref=commission['commission_ref'], goal='保留节奏，换产品',
                requested_operation={'kind':'prepare_production_nodes',
                                     'run_after_create':False, 'plan_count':1})
            assert result['direction_revision'] == 1
    assert store.get('r')['ordinary_turn_owner']['state'] == (
        'released' if interruption == 'before_publish' else
        'blocked' if interruption == 'after_publish' else 'active')


def test_background_save_cannot_erase_new_ordinary_owner(tmp_path):
    ctx, store = seeded(tmp_path)
    store.wake_waiting(ctx, 'r', {'artifact_id':'c','revision':2}, 'drafting_plan')
    stale = store.get('r')
    owner = claim(ctx, store, 'human')
    assert store.claim('r', 'background')
    assert store.update_owned(stale, 'background')
    assert store.get('r')['ordinary_turn_owner']['token'] == owner['token']


def test_all_continuations_of_same_commission_are_fenced(tmp_path):
    ctx, store = seeded(tmp_path)
    other = dict(store.get('r'), run_id='r2', idempotency_key='r2')
    store.create(other)
    owner = claim(ctx, store, 'human')
    assert owner
    for run_id in ('r', 'r2'):
        assert store.wake_waiting(ctx, run_id, {'artifact_id':'c','revision':2}, 'drafting_plan') is None
        assert store.get(run_id)['ordinary_turn_owner']['token'] == owner['token']
    assert store.finish_ordinary_turn(owner['run_id'], owner['token'], state='blocked')
    assert store.get('r2')['ordinary_turn_owner']['state'] == 'blocked'


async def test_receipt_settlement_error_still_releases_chat_lock(tmp_path, monkeypatch):
    from novelvideo.chat import service, reference_remix_continuation as module
    from novelvideo.chat.execution_context import AgentExecutionContext
    from novelvideo.chat.store import ChatScope
    ctx, store = seeded(tmp_path)
    owner = claim(ctx, store, 'human')
    execution = AgentExecutionContext.from_project_scope(
        scope=ChatScope(kind='project', id=ctx.project_id, surface='freezone', canvas_id='canvas'), project=ctx)
    released = []
    monkeypatch.setattr(service, '_acquire_chat_run_lock', lambda *args: 'lock')
    monkeypatch.setattr(service, '_release_chat_run_lock', lambda *args: released.append(args))
    monkeypatch.setattr(service, '_frontend_context_reply', lambda *args: 'reply')
    async def reply(*args, **kwargs): return {'ok':True}
    monkeypatch.setattr(service, '_stream_deterministic_assistant_reply', reply)
    def broken(*args): raise RuntimeError('storage unavailable')
    monkeypatch.setattr(module, 'finish_ordinary_production_turn', broken)
    await service.stream_assistant_reply('actor', ctx.project_id, 'prompt', None,
        execution_context=execution, turn_id='human', trusted_reference_turn_owner={'owner':owner,'ctx':ctx})
    assert len(released) == 1


@pytest.mark.parametrize('state', ['active', 'submitted', 'blocked'])
def test_expired_ordinary_lease_cannot_renew_or_settle(tmp_path, monkeypatch, state):
    from novelvideo.chat import reference_remix_continuation as module
    ctx, store = seeded(tmp_path)
    owner = claim(ctx, store, 'human')
    monkeypatch.setattr(module.time, 'time', lambda: owner['lease_until'] + 1)
    assert not store.finish_ordinary_turn('r', owner['token'], state=state)
    assert store.get('r')['ordinary_turn_owner']['state'] == 'active'


async def test_failed_heartbeat_still_settles_and_releases_chat_lock(tmp_path, monkeypatch):
    import asyncio
    from novelvideo.chat import service, reference_remix_continuation as module
    from novelvideo.chat.execution_context import AgentExecutionContext
    from novelvideo.chat.store import ChatScope
    ctx, store = seeded(tmp_path)
    owner = claim(ctx, store, 'human')
    execution = AgentExecutionContext.from_project_scope(
        scope=ChatScope(kind='project', id=ctx.project_id, surface='freezone', canvas_id='canvas'), project=ctx)
    released, settled = [], []
    monkeypatch.setattr(service, '_acquire_chat_run_lock', lambda *args: 'lock')
    monkeypatch.setattr(service, '_release_chat_run_lock', lambda *args: released.append(args))
    monkeypatch.setattr(service, '_frontend_context_reply', lambda *args: 'reply')
    async def reply(*args, **kwargs):
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return {'ok':True}
    async def broken(*args, **kwargs): raise RuntimeError('heartbeat storage unavailable')
    monkeypatch.setattr(service, '_stream_deterministic_assistant_reply', reply)
    monkeypatch.setattr(service, '_chat_run_lock_heartbeat_loop', broken)
    monkeypatch.setattr(module, 'finish_ordinary_production_turn', lambda *args: settled.append(args))
    await service.stream_assistant_reply('actor', ctx.project_id, 'prompt', None,
        execution_context=execution, turn_id='human', trusted_reference_turn_owner={'owner':owner,'ctx':ctx})
    assert len(released) == len(settled) == 1


async def test_lost_ownership_heartbeat_cancels_current_turn(monkeypatch):
    from types import SimpleNamespace
    from novelvideo.chat import service, reference_remix_continuation as module
    cancelled = []
    async def immediate(*args): pass
    monkeypatch.setattr(service.asyncio, 'sleep', immediate)
    monkeypatch.setattr(service, '_heartbeat_chat_run_lock', lambda *args: True)
    monkeypatch.setattr(module, 'coordinator', SimpleNamespace(
        store=SimpleNamespace(finish_ordinary_turn=lambda *args, **kwargs: False)))
    with pytest.raises(RuntimeError, match='lease was lost'):
        await service._chat_run_lock_heartbeat_loop('u', 'p', 'lock',
            {'owner':{'run_id':'r','token':'token','turn_id':'human'}},
            SimpleNamespace(cancel=lambda: cancelled.append(True)))
    assert cancelled == [True]
