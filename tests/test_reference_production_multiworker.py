"""Durable automation authority survives worker races and explicit stops."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import importlib
import pytest
from test_reference_remix_continuation import context, write_canvas
from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.freezone.remix.reference_commission import create_reference_commission
from novelvideo.freezone.remix.operations import RemixOperationStore


def api():
    name = 'novelvideo.freezone.remix.production_automation'
    assert importlib.util.find_spec(name), 'production automation has no durable host control'
    return importlib.import_module(name)


def setup(tmp_path, actor='actor'):
    ctx = context(tmp_path, actor=actor)
    write_canvas(ctx)
    store = RemixArtifactStore(ctx.output_dir, ctx.project_id, created_by=actor)
    commission = create_reference_commission(store, canvas_id='canvas', requester_user_id=actor,
        analysis_node_id='analysis', goal='produce', analysis_evidence={'task_id':'native', 'result_fingerprint':'hash'})
    return ctx, commission['commission_ref']


def test_two_workers_claim_one_event_and_old_owner_cannot_commit(tmp_path):
    module = api()
    ctx, ref = setup(tmp_path)
    control = module.enroll_production_automation(ctx, canvas_id='canvas', commission_ref=ref, command_id='human')
    def claim(owner):
        return module.claim_automation_event(ctx, canvas_id='canvas', commission_id=ref['artifact_id'],
            agent_id='main', generation=control['generation'], event_key='projection-one', owner=owner)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ('worker-a','worker-b')))
    assert sum(bool(c) for c in claims) == 1
    winner = next(c for c in claims if c)
    assert not module.finish_automation_event(ctx, winner, owner='not-owner')
    assert module.finish_automation_event(ctx, winner, owner=winner['lease_owner'])
    assert claim('worker-c') is None
    assert module.read_production_control(ctx, canvas_id='canvas', commission_id=ref['artifact_id'])['event_replays'] == 1


def test_stop_fences_late_native_authoring_and_explicit_resume_owns_new_generation(tmp_path):
    module = api()
    ctx, ref = setup(tmp_path)
    first = module.enroll_production_automation(ctx, canvas_id='canvas', commission_ref=ref, command_id='start')
    operations = RemixOperationStore(ctx.output_dir, ctx.project_id)
    operation = operations.start(idempotency_key='native', input_refs=[ref], producer_version='remix-adopt-source-speech.v1',
        normalized_parameters={'action':'adopt_source_observation','payload':{'canvas_id':'canvas','requester_user_id':'actor'}},
        node_revision=1, canvas_id='canvas', node_id='plan')
    module.bind_automation_operation(ctx, canvas_id='canvas', commission_id=ref['artifact_id'],
        generation=first['generation'], operation_id=operation['operation_id'])
    module.assert_operation_automation_current(ctx, operation['operation_id'])
    module.stop_production_automation(ctx, canvas_id='canvas', commission_ref=ref)
    with pytest.raises(ValueError, match='stopped|generation'):
        module.assert_operation_automation_current(ctx, operation['operation_id'])
    # Durable operation remains available: stopping automation does not cancel paid generation.
    assert operations.get(operation['operation_id'])['status'] == 'pending'
    # Reload/idempotent replay cannot enable a stopped generation.
    replay = module.enroll_production_automation(ctx, canvas_id='canvas', commission_ref=ref, command_id='start')
    assert replay['active'] is False
    resumed = module.enroll_production_automation(ctx, canvas_id='canvas', commission_ref=ref, command_id='resume')
    assert resumed['generation'] == first['generation'] + 1
    with pytest.raises(ValueError, match='generation'):
        module.assert_operation_automation_current(ctx, operation['operation_id'])


def test_controls_isolate_actor_project_and_private_agent(tmp_path):
    module = api()
    ctx, ref = setup(tmp_path)
    control = module.enroll_production_automation(ctx, canvas_id='canvas', commission_ref=ref, command_id='start', agent_id='private-a')
    assert module.read_production_control(ctx, canvas_id='canvas', commission_id=ref['artifact_id'], agent_id='private-b') is None
    assert module.read_production_control(replace(ctx, requester_user_id='other'), canvas_id='canvas', commission_id=ref['artifact_id'], agent_id='private-a') is None
    assert module.read_production_control(replace(ctx, project_id='other'), canvas_id='canvas', commission_id=ref['artifact_id'], agent_id='private-a') is None
    with pytest.raises(PermissionError):
        module.enroll_production_automation(replace(ctx, requester_user_id='other'), canvas_id='canvas', commission_ref=ref, command_id='other')
    assert control['scope']['agent_id'] == 'private-a'


@pytest.mark.asyncio
async def test_observer_factory_receives_genuine_host_context_and_canvas(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from novelvideo.chat import reference_production_observer as observer_module
    from novelvideo.freezone.remix import source_speech
    ctx, ref = setup(tmp_path)
    api().enroll_production_automation(ctx, canvas_id='canvas', commission_ref=ref, command_id='human')
    run = dict(run_id='r', canvas_id='canvas', agent_id='main', commission_ref=ref, commission_phase='drafting_plan')
    commission = {'phase':'drafting_plan','requester_user_id':'actor','canvas_id':'canvas','commission_ref':ref}
    calls = []
    def factory(host, *, canvas_id):
        calls.append((host, canvas_id))
        return lambda **kwargs: None
    monkeypatch.setattr(source_speech, 'make_material_speech_scheduler', factory)
    monkeypatch.setattr(observer_module, 'read_reference_commission', lambda *a, **kw: commission)
    monkeypatch.setattr(observer_module, 'production_event', lambda *a, **kw: None)
    async def resolve(run): return ctx
    coordinator = SimpleNamespace(_resolve_context=resolve, _assert_current=lambda *a, **kw: {'nodes':[]},
        store=SimpleNamespace(list_for_actor=lambda *a: []))
    async def idle(*a, **kw): return False
    monkeypatch.setattr(api(), 'advance_enrolled_production', idle)
    observer = observer_module.ReferenceProductionObserver(coordinator)
    assert not await observer.observe_run(run)
    assert calls == [(ctx, 'canvas')]


@pytest.mark.asyncio
async def test_two_observers_dispatch_one_native_transition_and_legacy_is_idle(tmp_path, monkeypatch):
    import asyncio
    from novelvideo.freezone.remix import production_advancement as native
    module = api()
    ctx, ref = setup(tmp_path)
    run = {'run_id':'run', 'canvas_id':'canvas','agent_id':'main','commission_ref':ref}
    state = {'commission_ref':ref, 'projection_revision':'projection', 'phase':'preparing_materials',
        'execution_status':'waiting_user', 'ready_system_actions':[{'owner':'system','kind':'check_materials','action_id':'check'}],
        'blockers':[], 'plan_ref':None, 'work_ref':None}
    calls, entered, release = [], asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(module, '_read_automation_state', lambda *a, **kw: state)
    async def advance(host, **kwargs):
        calls.append((host, kwargs))
        entered.set()
        await release.wait()
        return {**state, 'advancement':{'action': state['ready_system_actions'][0]}}
    monkeypatch.setattr(native, 'advance_production', advance)
    assert not await module.advance_enrolled_production(ctx, run, runs=[run])
    assert calls == []
    module.enroll_production_automation(ctx, canvas_id='canvas', commission_ref=ref, command_id='human')
    first = asyncio.create_task(module.advance_enrolled_production(ctx, run, runs=[run]))
    await asyncio.wait_for(entered.wait(), 1)
    assert not await module.advance_enrolled_production(ctx, run, runs=[run])
    release.set()
    assert await first
    assert not await module.advance_enrolled_production(ctx, run, runs=[run])
    assert len(calls) == 1 and calls[0][0] is ctx
    assert calls[0][1]['automation_generation'] == 1


@pytest.mark.asyncio
async def test_coordinator_stop_only_enrolled_actor_scope_and_resume_keeps_analysis(tmp_path):
    from novelvideo.chat.reference_remix_continuation import ReferenceRemixStore, ReferenceRemixCoordinator
    module = api()
    ctx, ref = setup(tmp_path)
    store = ReferenceRemixStore(tmp_path / 'runs.db')
    coordinator = ReferenceRemixCoordinator(store)
    for run_id, agent in [('owned','private-a'), ('other-agent','private-b'), ('legacy','private-a')]:
        store.create(dict(run_id=run_id, project_id=ctx.project_id,requester_user_id=ctx.requester_user_id,
            canvas_id='canvas', agent_id=agent, idempotency_key=run_id,request_fingerprint=run_id,
            commission_ref=ref if run_id!='legacy' else {'artifact_id':'legacy','revision':1},
            status='aligning_speech', commission_phase='aligning_speech', task_id='original-analysis'))
    module.enroll_production_automation(ctx, canvas_id='canvas', commission_ref=ref, command_id='human',agent_id='private-a')
    assert await coordinator.stop_automation_scope(ctx=ctx, canvas_id='canvas', agent_id='private-a') == 1
    assert store.get('owned')['status'] == 'stopped'
    assert store.get('other-agent')['status'] == 'aligning_speech'
    assert store.get('legacy')['status'] == 'aligning_speech'
    assert await coordinator.stop_automation_scope(ctx=replace(ctx, requester_user_id='other'), canvas_id='canvas',agent_id='private-a') == 0
    await coordinator.resume_production(ctx=ctx, run_id='owned', command_id='human-resume')
    assert store.get('owned')['status'] == 'awaiting_direction'
    assert store.get('owned')['task_id'] == 'original-analysis'


def _claim_process(ctx, ref, queue):
    queue.put(api().claim_automation_event(ctx, canvas_id='canvas', commission_id=ref['artifact_id'],
        agent_id='main', generation=1, event_key='shared', owner=str(__import__('os').getpid())) is not None)


def test_independent_processes_share_one_event_lease(tmp_path):
    import multiprocessing
    module = api()
    ctx, ref = setup(tmp_path)
    module.enroll_production_automation(ctx, canvas_id='canvas', commission_ref=ref, command_id='human')
    mp = multiprocessing.get_context('spawn')
    queue = mp.Queue()
    workers = [mp.Process(target=_claim_process,args=(ctx,ref,queue)) for _ in range(2)]
    try:
        for worker in workers: worker.start()
        assert sum(queue.get(timeout=15) for _ in workers) == 1
        for worker in workers:
            worker.join(timeout=15)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive(): worker.terminate(); worker.join()
        queue.close()


def test_expired_event_holder_cannot_consume_replacement_lease(tmp_path, monkeypatch):
    module = api()
    ctx, ref = setup(tmp_path)
    module.enroll_production_automation(ctx,canvas_id='canvas',commission_ref=ref,command_id='human')
    clock = [1000.0]
    monkeypatch.setattr(module.time,'time',lambda:clock[0])
    first = module.claim_automation_event(ctx,canvas_id='canvas',commission_id=ref['artifact_id'],generation=1,event_key='event',owner='old')
    clock[0] += module.LEASE_SECONDS + 1
    second = module.claim_automation_event(ctx,canvas_id='canvas',commission_id=ref['artifact_id'],generation=1,event_key='event',owner='new')
    assert second
    assert not module.finish_automation_event(ctx,first,owner='old')
    assert module.finish_automation_event(ctx,second,owner='new')


@pytest.mark.asyncio
async def test_enrolled_existing_dialogue_schedules_native_check_before_review_wait(tmp_path, monkeypatch):
    from novelvideo.freezone.remix import source_speech
    module = api()
    ctx, ref = setup(tmp_path)
    module.enroll_production_automation(ctx,canvas_id='canvas',commission_ref=ref,command_id='human')
    run = {'run_id':'run','canvas_id':'canvas','agent_id':'main','commission_ref':ref}
    state = {'commission_ref':ref,'projection_revision':'p','phase':'preparing_materials','execution_status':'waiting_user',
        'blockers':[{'owner':'user','kind':'review_materials','action_id':'review'}], 'ready_system_actions':[],
        'plan_ref':{'artifact_id':'plan','revision':1},'work_ref':{'artifact_id':'work','revision':1}}
    prepared = {'task_payload':{'work_ref':state['work_ref'],'resource':{'media_hash':'a'*64}},
        'normalized_parameters':{'action':'analyze_material_speech'}}
    monkeypatch.setattr(module,'_read_automation_state',lambda *a:state)
    monkeypatch.setattr(module,'_prepare_next_candidate_check',lambda *a:prepared)
    calls=[]
    async def enqueue(host, store, packet, **kwargs):
        calls.append((host,packet,kwargs))
        return {'operation_id':'a'*32,'task_id':'actual-native','status':'pending'}
    monkeypatch.setattr(source_speech,'enqueue_material_speech',enqueue)
    assert await module.advance_enrolled_production(ctx,run,runs=[run])
    assert not await module.advance_enrolled_production(ctx,run,runs=[run])
    assert len(calls)==1 and calls[0][0] is ctx and calls[0][1] is prepared


@pytest.mark.asyncio
@pytest.mark.parametrize('live', [False, True])
async def test_http_stop_persists_control_before_interrupt_only_for_actual_live_turn(tmp_path, monkeypatch, live):
    from novelvideo.api.routes import chat
    from novelvideo.chat.reference_remix_continuation import ReferenceRemixCoordinator, ReferenceRemixStore
    module = api()
    ctx, ref = setup(tmp_path)
    module.enroll_production_automation(ctx,canvas_id='canvas',commission_ref=ref,command_id='human')
    db = ReferenceRemixStore(tmp_path / 'run.db')
    db.create(dict(run_id='run',project_id=ctx.project_id,requester_user_id=ctx.requester_user_id,canvas_id='canvas',agent_id='main',
        idempotency_key='start',request_fingerprint='start',commission_ref=ref,status='aligning_speech'))
    coordinator = ReferenceRemixCoordinator(db)
    monkeypatch.setattr('novelvideo.chat.reference_remix_continuation.coordinator',coordinator)
    monkeypatch.setattr(chat.chat_service,'get_chat_backend_name',lambda:'codex')
    monkeypatch.setattr(chat,'_enforce_agent_chat_scope',lambda *a, **kw:None)
    async def host(*a): return ctx
    monkeypatch.setattr(chat,'_project_context_for_scope',host)
    async def interrupt(username, scope, turn_id, *, before_interrupt=None):
        if not live: return False
        assert callable(before_interrupt), 'HTTP stop has no pre-interrupt durable production fence'
        await before_interrupt()
        assert not module.read_production_control(ctx,canvas_id='canvas',commission_id=ref['artifact_id'])['active']
        return True
    monkeypatch.setattr(chat.chat_service,'interrupt_active_codex_turn',interrupt)
    payload=chat.CancelChatTurnRequest(scope=chat.ChatScopePayload(kind='project',id=ctx.project_id,surface='freezone',canvasId='canvas',agentId='main'),turn_id='live' if live else 'obsolete')
    result=await chat.cancel_chat_turn({'username':'actor'},payload)
    assert result['data']['cancelled'] is live
    assert module.read_production_control(ctx,canvas_id='canvas',commission_id=ref['artifact_id'])['active'] is (not live)


@pytest.mark.asyncio
async def test_ee_disabled_automation_does_not_block_ordinary_canvas_cancel(tmp_path, monkeypatch):
    from novelvideo.api.routes import chat
    from novelvideo.chat.reference_remix_continuation import ReferenceRemixCoordinator, ReferenceRemixStore

    ctx = context(tmp_path, actor='actor')
    monkeypatch.setenv('ST_CONTROL_PLANE_DSN', 'postgresql://local/ee')
    monkeypatch.delenv('ST_REFERENCE_REMIX_AUTO_CONTINUATION', raising=False)
    coordinator = ReferenceRemixCoordinator(ReferenceRemixStore(tmp_path / 'missing' / 'runs.db'))
    monkeypatch.setattr('novelvideo.chat.reference_remix_continuation.coordinator', coordinator)
    monkeypatch.setattr(chat.chat_service, 'get_chat_backend_name', lambda: 'codex')
    monkeypatch.setattr(chat, '_enforce_agent_chat_scope', lambda *a, **kw: None)

    async def host(*args):
        return ctx

    async def interrupt(username, scope, turn_id, *, before_interrupt=None):
        if before_interrupt is not None:
            await before_interrupt()
        return True

    monkeypatch.setattr(chat, '_project_context_for_scope', host)
    monkeypatch.setattr(chat.chat_service, 'interrupt_active_codex_turn', interrupt)
    payload = chat.CancelChatTurnRequest(
        scope=chat.ChatScopePayload(kind='project', id=ctx.project_id, surface='freezone',
                                    canvasId='canvas', agentId='main'),
        turn_id='live',
    )
    result = await chat.cancel_chat_turn({'username': 'actor'}, payload)
    assert result['data']['cancelled'] is True
    assert not (tmp_path / 'missing' / 'runs.db').exists()


def test_old_enrollment_replay_cannot_resume_after_command_history_rotation(tmp_path):
    module=api()
    ctx,ref=setup(tmp_path)
    for i in range(130):
        module.enroll_production_automation(ctx,canvas_id='canvas',commission_ref=ref,command_id='human-'+str(i))
    module.stop_production_automation(ctx,canvas_id='canvas',commission_ref=ref)
    result=module.enroll_production_automation(ctx,canvas_id='canvas',commission_ref=ref,command_id='human-0')
    assert result['active'] is False
    assert result['generation']==130


@pytest.mark.asyncio
async def test_observer_stop_race_cannot_publish_stage_after_saved_evidence(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from novelvideo.chat import reference_production_observer as observation
    module=api()
    ctx,ref=setup(tmp_path)
    module.enroll_production_automation(ctx,canvas_id='canvas',commission_ref=ref,command_id='human')
    run={'run_id':'r','canvas_id':'canvas','agent_id':'main','commission_ref':ref,'commission_phase':'drafting_plan'}
    commission={'phase':'drafting_plan','canvas_id':'canvas','requester_user_id':'actor','commission_ref':ref}
    writes=[]
    monkeypatch.setattr(observation,'read_reference_commission',lambda *a, **kw:commission)
    def evidence(*a, **kw):
        module.stop_production_automation(ctx,canvas_id='canvas',commission_ref=ref)
        return {'event':{'type':'plan_ready'},'event_key':'saved-evidence'}
    monkeypatch.setattr(observation,'production_event',evidence)
    monkeypatch.setattr(observation,'advance_reference_commission',lambda *a: writes.append('published'))
    async def resolve(run):return ctx
    async def continued(**kwargs):pass
    async def idle(*a, **kw):return False
    monkeypatch.setattr(module,'advance_enrolled_production',idle)
    observer=observation.ReferenceProductionObserver(SimpleNamespace(_resolve_context=resolve,
        _assert_current=lambda *a, **kw:{'nodes':[]},continue_commission=continued,
        store=SimpleNamespace(list_for_actor=lambda *a:[])))
    with pytest.raises(ValueError,match='stopped'):
        await observer.observe_run(run)
    assert writes==[]


def test_late_delivery_notification_cannot_reopen_stopped_recovery_index(tmp_path):
    from novelvideo.chat.reference_remix_continuation import ReferenceRemixStore
    ctx,ref=setup(tmp_path)
    store=ReferenceRemixStore(tmp_path/'runs.db')
    store.create(dict(run_id='r',project_id=ctx.project_id,requester_user_id=ctx.requester_user_id,canvas_id='canvas',agent_id='main',
        idempotency_key='k',request_fingerprint='f',commission_ref=ref,status='stopped'))
    assert store.finish_waiting(ctx,'r',ref) is None
    assert store.get('r')['status']=='stopped'
