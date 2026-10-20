from types import SimpleNamespace
import hashlib

import pytest
from fastapi import HTTPException


def setup_candidate(tmp_path, monkeypatch, source='existing'):
    from test_material_work import setup_work
    import novelvideo.freezone.remix.material_execution as execution
    store, plan_ref, prepared = setup_work(tmp_path)
    saved = store.read(prepared['work_ref']['artifact_id'], 1)
    step = saved['payload']['steps'][1]
    step.update(source=source, node_id='n', status='candidate')
    if source != 'generate':
        step['recipe_id'] = None
    changed = store.revise(saved['id'], 1, saved['payload'], saved['upstream_refs'])
    ref = {'artifact_id': changed['id'], 'revision': changed['revision']}
    media = tmp_path / 'candidate.mp4'; media.write_bytes(b'full source')
    resource = {'id': 'real', 'node_id': 'n', 'media_kind': 'video', 'media_path': media.name,
                'media_hash': hashlib.sha256(media.read_bytes()).hexdigest(), 'duration_us': 4_100_000, 'has_audio': True}
    monkeypatch.setattr(execution, 'canvas_material_resource', lambda *args: dict(resource))
    ctx = SimpleNamespace(output_dir=tmp_path, state_dir=tmp_path, project_id='project',
        owner_id='owner', owner_type='user', requester_user_id='alice', effective_role='editor', is_home_node=True)
    canvas = {'project_id': 'project', 'access_model': 'private', 'owner_principal_id': 'alice',
              'nodes': [{'id': 'plan', 'type': 'scriptNode', 'data': {'productionPlanV2Ref': {
                  'artifactId': plan_ref['artifact_id'], 'revision': plan_ref['revision']}}}, {'id': 'n', 'data': {}}]}
    return ctx, canvas, store, ref, resource


def test_prepare_existing_candidate_without_acceptance_resolves_host_signatures(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.source_speech import prepare_material_speech
    ctx, canvas, store, ref, resource = setup_candidate(tmp_path, monkeypatch)
    prepared = prepare_material_speech(store, ctx, canvas, canvas_id='canvas', work_ref=ref,
        step_id='video', node_id='n', language='en', provider_signature='provider')
    assert prepared['queue_kind'] == 'default'
    assert prepared['task_type'] == 'remix_analyze_material_speech'
    assert prepared['task_payload']['resource'] == resource
    assert prepared['task_payload']['scope']['private_owner_id'] == 'alice'
    assert 'target_text' not in prepared['task_payload']
    assert prepared['input_refs'] == [ref]
    assert store.read(ref['artifact_id'], ref['revision'])['payload']['steps'][1]['status'] == 'candidate'


def test_private_scope_and_stale_work_are_rejected(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.source_speech import prepare_material_speech
    ctx, canvas, store, ref, _ = setup_candidate(tmp_path, monkeypatch)
    kwargs = dict(canvas_id='canvas', work_ref=ref, step_id='video', node_id='n', language='en', provider_signature='p')
    canvas['owner_principal_id'] = 'someone_else'
    with pytest.raises(ValueError, match='private'):
        prepare_material_speech(store, ctx, canvas, **kwargs)
    canvas['owner_principal_id'] = 'alice'
    saved = store.read(ref['artifact_id'], ref['revision'])
    store.revise(saved['id'], saved['revision'], saved['payload'], saved['upstream_refs'])
    with pytest.raises(ValueError, match='work changed'):
        prepare_material_speech(store, ctx, canvas, **kwargs)


def test_generated_candidate_requires_verified_native_completion(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.source_speech import prepare_material_speech
    ctx, canvas, store, ref, _ = setup_candidate(tmp_path, monkeypatch, source='generate')
    with pytest.raises(ValueError, match='native execution'):
        prepare_material_speech(store, ctx, canvas, canvas_id='canvas', work_ref=ref,
            step_id='video', node_id='n', language='en', provider_signature='p')


@pytest.mark.asyncio
async def test_generic_artifact_clients_cannot_create_source_observations(tmp_path, monkeypatch):
    import novelvideo.api.routes.remix as route
    from novelvideo.freezone.remix.store import RemixArtifactStore
    async def store(*args):
        return RemixArtifactStore(tmp_path, 'project')
    monkeypatch.setattr(route, '_store', store)
    with pytest.raises(HTTPException) as caught:
        await route.create_artifact('project', route.CreateArtifactBody(schema='source_speech_observation.v1', payload={}), {})
    assert caught.value.status_code == 422


def test_runner_is_registered_on_speech_lane():
    import novelvideo.task_backend.runners.remix
    from novelvideo.task_backend.registry import get_project_task_runner, project_task_lane
    assert get_project_task_runner('remix_analyze_material_speech') is not None
    assert project_task_lane('remix_analyze_material_speech') == 'default'


def test_candidate_action_accepts_only_native_refs(tmp_path):
    from novelvideo.api.routes.remix import ProcessActionBody
    body = ProcessActionBody(action='analyze_material_speech', payload={}, idempotency_key='one', node_revision=1)
    assert body.action == 'analyze_material_speech'


def test_worker_revalidates_binding_and_publishes_source_only(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.source_speech import prepare_material_speech
    from novelvideo.freezone.remix.operations import RemixOperationStore
    import novelvideo.freezone.remix.source_speech as speech
    import novelvideo.freezone.remix.alignment as alignment
    from novelvideo.task_backend.runners.remix import run_remix_analyze_material_speech
    ctx, canvas, store, ref, resource = setup_candidate(tmp_path, monkeypatch)
    monkeypatch.setattr('novelvideo.freezone.canvas_store.read_canvas', lambda *args: canvas)
    monkeypatch.setattr(speech, 'configured_provider_signature', lambda: 'p')
    class Engine:
        def transcribe(self, path, language):
            assert path.read_bytes() == b'full source'
            return {'status': 'completed', 'transcript': 'tail', 'words': [
                {'text': 'tail', 'start': 3.1, 'end': 4.1}]}
    monkeypatch.setattr(alignment, 'configured_newapi_transcriber', lambda **kwargs: Engine())
    prepared = prepare_material_speech(store, ctx, canvas, canvas_id='canvas', work_ref=ref,
        step_id='video', node_id='n', language='en')
    ops = RemixOperationStore(tmp_path, 'project')
    op = ops.start(idempotency_key='test', input_refs=prepared['input_refs'], producer_version=prepared['producer_version'],
        normalized_parameters=prepared['normalized_parameters'], node_revision=1, input_media_hashes=prepared['input_media_hashes'])
    fence = ops.claim_submission(op['operation_id'], owner='submit')
    ops.bind_task(op['operation_id'], task_id='native-task', owner='submit', fencing_token=fence)
    envelope = {'__run_task_id': 'native-task', 'payload': {**prepared['task_payload'], 'operation_id': op['operation_id']}}
    with pytest.raises(ValueError, match='another task'):
        run_remix_analyze_material_speech({**envelope, '__run_task_id': 'forged'}, ctx)
    result = run_remix_analyze_material_speech(envelope, ctx)
    assert ops.get(op['operation_id'])['output_refs'] == [result['artifact_ref']]
    assert store.read(result['artifact_ref']['artifact_id'], 1)['payload']['recognized_words'][0]['end_us'] == 4_100_000
    assert store.read_latest(ref['artifact_id'])['revision'] == ref['revision']
    resource['media_hash'] = 'f' * 64
    with pytest.raises(ValueError, match='changed'):
        run_remix_analyze_material_speech(envelope, ctx)


@pytest.mark.asyncio
async def test_candidate_api_rejects_model_supplied_paths_and_scope(tmp_path, monkeypatch):
    import novelvideo.api.routes.remix as route
    from novelvideo.freezone.remix.store import RemixArtifactStore
    async def store(*args):
        return RemixArtifactStore(tmp_path, 'project')
    monkeypatch.setattr(route, '_store', store)
    with pytest.raises(HTTPException) as caught:
        await route.enqueue_process_action('project', route.ProcessActionBody(action='analyze_material_speech',
            payload={'media_path': '/tmp/secret', 'tenant_id': 'foreign'}, idempotency_key='one', node_revision=1), {})
    assert caught.value.status_code == 422


def test_settlement_scheduler_skips_no_dialogue_and_enqueues_targeted_once(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.source_speech import make_material_speech_scheduler
    import novelvideo.freezone.remix.source_speech as speech
    from novelvideo.freezone.remix.operations import RemixOperationStore
    ctx, canvas, store, ref, resource = setup_candidate(tmp_path, monkeypatch)
    calls = []
    async def enqueue(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(task_state=SimpleNamespace(task_id='queued-native'))
    monkeypatch.setattr('novelvideo.ports.get_task_backend', lambda: SimpleNamespace(enqueue_project_task=enqueue))
    monkeypatch.setattr(speech, 'configured_provider_signature', lambda: 'p')
    scheduler = make_material_speech_scheduler(ctx, canvas_id='canvas')
    kwargs = dict(store=store, canvas=canvas, work_ref=ref, step_id='video', resource=resource,
                  native_result={}, workflow_state_dir=tmp_path, command_id='native-completion')
    skipped = scheduler(**kwargs)
    assert skipped['status'] == 'needs_review' and skipped['review_ref']
    assert calls == []
    saved = store.read(ref['artifact_id'], ref['revision'])
    saved['payload']['requirements'][0]['turn_ids'] = ['turn']
    # The saved plan must contain the referenced turn for the work contract.
    plan_ref = saved['payload']['plan_ref']
    plan = store.read(plan_ref['artifact_id'], plan_ref['revision'])
    plan['payload']['beats'][0]['turns'] = [{'id': 'turn', 'speaker': 'person', 'text': 'target'}]
    plan2 = store.revise(plan['id'], plan['revision'], plan['payload'], plan['upstream_refs'])
    from novelvideo.freezone.remix.material_work import prepare_material_work
    for step in saved['payload']['steps']:
        step['status'] = 'planned'
    new = prepare_material_work(store, {'artifact_id': plan2['id'], 'revision': plan2['revision']},
        saved['payload']['requirements'], saved['payload']['steps'], 'speech-target')
    canvas['nodes'][0]['data']['productionPlanV2Ref']['revision'] = plan2['revision']
    kwargs.update(work_ref=new['work_ref'], command_id='second-completion')
    first = scheduler(**kwargs)
    second = scheduler(**kwargs)
    assert first['check_ref'] == second['check_ref']
    assert len(calls) == 1
    assert calls[0]['queue_kind'] == 'default'
    assert RemixOperationStore(tmp_path, 'project').get(first['check_ref']['operation_id'])['task_id'] == 'queued-native'


@pytest.mark.asyncio
async def test_candidate_api_keeps_operation_parameters_equal_to_worker_snapshot(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    import novelvideo.api.egress_binding as egress_binding
    import novelvideo.api.routes.remix as route
    import novelvideo.api.routes.reference_director as director
    import novelvideo.freezone.remix.source_speech as speech
    from novelvideo.freezone.remix.operations import RemixOperationStore
    ctx, canvas, store, ref, resource = setup_candidate(tmp_path, monkeypatch)
    async def get_store(*args): return store
    async def get_ctx(**kwargs): return ctx
    async def get_context(*args): return ctx, canvas, store
    calls = []
    async def enqueue(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(task_state=SimpleNamespace(task_id='api-native'))
    monkeypatch.setattr(route, '_store', get_store)
    @asynccontextmanager
    async def local_scope(**_kwargs):
        yield None
    monkeypatch.setattr(egress_binding, 'request_egress_scope', local_scope)
    monkeypatch.setattr(route, 'resolve_project_context', get_ctx)
    monkeypatch.setattr(director, '_context', get_context)
    monkeypatch.setattr(speech, 'configured_provider_signature', lambda: 'p')
    monkeypatch.setattr(route, 'get_task_backend', lambda: SimpleNamespace(enqueue_project_task=enqueue))
    body = route.ProcessActionBody(action='analyze_material_speech', payload={
        'canvas_id': 'canvas', 'work_ref': ref, 'step_id': 'video', 'node_id': 'n', 'language': 'en'},
        idempotency_key='api-one', node_revision=2)
    first = await route.enqueue_process_action('project', body, {})
    again = await route.enqueue_process_action('project', body, {})
    assert first['data']['operation_id'] == again['data']['operation_id']
    assert len(calls) == 1
    assert calls[0]['queue_kind'] == 'default'
    op = RemixOperationStore(tmp_path, 'project').get(first['data']['operation_id'])
    expected = speech.prepare_material_speech(store, ctx, canvas, **body.payload)
    assert op['normalized_parameters'] == expected['normalized_parameters']


@pytest.mark.asyncio
async def test_source_artifact_read_rechecks_private_canvas_access(tmp_path, monkeypatch):
    import novelvideo.api.routes.remix as route
    import novelvideo.api.routes.reference_director as director
    import novelvideo.freezone.remix.source_speech as speech
    ctx, canvas, store, ref, resource = setup_candidate(tmp_path, monkeypatch)
    class Engine:
        def transcribe(self, *args): return {'status': 'no_speech', 'words': []}
    observation = speech.observe_source_speech(store, scope=speech.candidate_scope(ctx, canvas, 'canvas'),
        resource=resource, language='en', provider_signature='p', aligner=Engine())
    async def get_store(*args): return store
    async def denied(*args): raise HTTPException(403, 'private canvas access denied')
    monkeypatch.setattr(route, '_store', get_store)
    monkeypatch.setattr(director, '_context', denied)
    with pytest.raises(HTTPException) as caught:
        await route.read_artifact('project', observation['artifact_id'], 1, {})
    assert caught.value.status_code == 403
    with pytest.raises(HTTPException):
        await route.read_latest_artifact('project', observation['artifact_id'], {})
    async def authorized(*args): return ctx, canvas, store
    monkeypatch.setattr(director, '_context', authorized)
    assert (await route.read_artifact('project', observation['artifact_id'], 1, {}))['data']['id'] == observation['artifact_id']


@pytest.mark.asyncio
async def test_private_source_operation_read_is_reauthorized(tmp_path, monkeypatch):
    import novelvideo.api.routes.remix as route
    import novelvideo.api.routes.reference_director as director
    import novelvideo.freezone.remix.source_speech as speech
    from novelvideo.freezone.remix.operations import RemixOperationStore
    ctx, canvas, store, ref, resource = setup_candidate(tmp_path, monkeypatch)
    prepared = speech.prepare_material_speech(store, ctx, canvas, canvas_id='canvas', work_ref=ref,
        step_id='video', node_id='n', language='en', provider_signature='p')
    op = RemixOperationStore(tmp_path, 'project').start(idempotency_key='one', input_refs=prepared['input_refs'],
        producer_version=prepared['producer_version'], normalized_parameters=prepared['normalized_parameters'], node_revision=1)
    async def get_store(*args): return store
    async def denied(*args): raise HTTPException(403, 'private canvas access denied')
    monkeypatch.setattr(route, '_store', get_store)
    monkeypatch.setattr(director, '_context', denied)
    with pytest.raises(HTTPException):
        await route.read_operation('project', op['operation_id'], {})
    assert (await route.list_operations('project', 50, {}))['data']['items'] == []


def test_cache_only_reconcile_recovers_follower_without_provider_call(tmp_path, monkeypatch):
    import novelvideo.freezone.remix.source_speech as speech
    from novelvideo.freezone.remix.operations import RemixOperationStore
    ctx, canvas, store, ref, resource = setup_candidate(tmp_path, monkeypatch)
    monkeypatch.setattr('novelvideo.freezone.canvas_store.read_canvas', lambda *args: canvas)
    monkeypatch.setattr(speech, 'configured_provider_signature', lambda: 'p')
    prepared = speech.prepare_material_speech(store, ctx, canvas, canvas_id='canvas', work_ref=ref,
        step_id='video', node_id='n', language='en')
    ops = RemixOperationStore(tmp_path, 'project')
    op = ops.start(idempotency_key='follower', input_refs=prepared['input_refs'],
        producer_version=prepared['producer_version'], normalized_parameters=prepared['normalized_parameters'], node_revision=1)
    fence = ops.claim_submission(op['operation_id'], owner='owner')
    ops.bind_task(op['operation_id'], task_id='native', owner='owner', fencing_token=fence)
    op = ops.provider_outcome_unknown(op['operation_id'], provider_task_id='native')
    assert speech.reconcile_material_speech(ctx, op) is None
    class Engine:
        def transcribe(self, *args): return {'status': 'no_speech', 'words': []}
    observation = speech.observe_source_speech(store, scope=prepared['task_payload']['scope'], resource=resource,
        language='en', provider_signature='p', aligner=Engine())
    recovered = speech.reconcile_material_speech(ctx, op)
    assert recovered['output_refs'] == [observation]
    assert recovered['status'] == 'needs_confirmation'


def test_worker_delivery_can_finish_submission_binding_race(tmp_path, monkeypatch):
    import novelvideo.freezone.remix.source_speech as speech
    import novelvideo.freezone.remix.alignment as alignment
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.task_backend.runners.remix import run_remix_analyze_material_speech
    ctx, canvas, store, ref, resource = setup_candidate(tmp_path, monkeypatch)
    monkeypatch.setattr('novelvideo.freezone.canvas_store.read_canvas', lambda *args: canvas)
    monkeypatch.setattr(speech, 'configured_provider_signature', lambda: 'p')
    class Engine:
        def transcribe(self, *args): return {'status': 'no_speech', 'words': []}
    monkeypatch.setattr(alignment, 'configured_newapi_transcriber', lambda **kwargs: Engine())
    prepared = speech.prepare_material_speech(store, ctx, canvas, canvas_id='canvas', work_ref=ref,
        step_id='video', node_id='n', language='en')
    ops = RemixOperationStore(tmp_path, 'project')
    op = ops.start(idempotency_key='racing', input_refs=prepared['input_refs'],
        producer_version=prepared['producer_version'], normalized_parameters=prepared['normalized_parameters'], node_revision=1)
    fence = ops.claim_submission(op['operation_id'], owner='submit')
    envelope = {'__run_task_id': 'native-delivery', 'payload': {**prepared['task_payload'], 'operation_id': op['operation_id']}}
    result = run_remix_analyze_material_speech(envelope, ctx)
    assert result['artifact_ref']
    bound = ops.bind_task(op['operation_id'], task_id='native-delivery', owner='submit', fencing_token=fence)
    assert bound['task_id'] == 'native-delivery'


def test_candidate_requires_exact_current_plan_revision_on_canvas(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.source_speech import prepare_material_speech
    ctx, canvas, store, ref, _ = setup_candidate(tmp_path, monkeypatch)
    canvas['nodes'][0]['data']['productionPlanV2Ref']['revision'] += 1
    with pytest.raises(ValueError, match='plan revision'):
        prepare_material_speech(store, ctx, canvas, canvas_id='canvas', work_ref=ref,
            step_id='video', node_id='n', language='en', provider_signature='p')


@pytest.mark.asyncio
async def test_candidate_correction_generic_writes_and_private_reads_are_guarded(tmp_path, monkeypatch):
    import novelvideo.api.routes.remix as route
    import novelvideo.api.routes.reference_director as director
    import novelvideo.freezone.remix.source_speech as speech
    ctx, canvas, store, ref, resource = setup_candidate(tmp_path, monkeypatch)
    artifact = {'id': 'correction', 'revision': 1, 'schema': 'candidate_speech_correction.v1',
        'producer_version': 'remix-candidate-speech-correction.v1',
        'payload': {'authorization_scope': speech.candidate_scope(ctx, canvas, 'canvas')}}
    async def get_store(*args): return store
    monkeypatch.setattr(route, '_store', get_store)
    monkeypatch.setattr(store, 'read', lambda *args: artifact)
    monkeypatch.setattr(store, 'read_latest', lambda *args: artifact)
    with pytest.raises(HTTPException) as denied:
        await route.create_artifact('project', route.CreateArtifactBody(schema='candidate_speech_correction.v1', payload={}), {})
    assert denied.value.status_code == 422
    with pytest.raises(HTTPException) as denied:
        await route.revise_artifact('project', 'correction', route.ReviseArtifactBody(expected_revision=1, payload={}), {})
    assert denied.value.status_code == 422
    async def unauthorized(*args): raise HTTPException(403, 'private canvas access denied')
    monkeypatch.setattr(director, '_context', unauthorized)
    with pytest.raises(HTTPException): await route.read_artifact('project', 'correction', 1, {})
    with pytest.raises(HTTPException): await route.read_latest_artifact('project', 'correction', {})
    async def authorized(*args): return ctx, canvas, store
    monkeypatch.setattr(director, '_context', authorized)
    assert (await route.read_artifact('project', 'correction', 1, {}))['data'] == artifact


def test_no_speech_scheduler_forwards_exact_settlement_proof(tmp_path, monkeypatch):
    import novelvideo.freezone.remix.source_speech as speech
    import novelvideo.freezone.remix.material_execution as execution
    ctx, canvas, store, ref, resource = setup_candidate(tmp_path, monkeypatch)
    prepared = {'task_payload': {'resource': resource}, 'speech_required': False}
    monkeypatch.setattr(speech, 'prepare_material_speech', lambda *args, **kwargs: prepared)
    seen = []
    def review(*args, **kwargs):
        seen.append(kwargs)
        return {'review_ref': {'artifact_id': 'review', 'revision': 1}}
    monkeypatch.setattr(execution, 'review_material_candidate', review)
    result = speech.make_material_speech_scheduler(ctx, canvas_id='canvas')(store=store, canvas=canvas,
        work_ref=ref, step_id='video', resource=resource, native_result={},
        workflow_state_dir=tmp_path, command_id='completion', settlement_id='exact-settlement')
    assert result['status'] == 'needs_review'
    assert seen[0]['settlement_id'] == 'exact-settlement'


def test_automatic_music_review_skips_asr_even_without_speech_configuration(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.material_work import prepare_material_work
    from novelvideo.freezone.remix.source_speech import make_material_speech_scheduler
    ctx, canvas, store, old_ref, media = setup_candidate(tmp_path, monkeypatch)
    old = store.read(old_ref['artifact_id'], old_ref['revision'])['payload']
    work = prepare_material_work(store, old['plan_ref'],
        [{'id': 'music', 'description': 'background music', 'media_kind': 'audio', 'final_step_id': 'music'}],
        [{'id': 'music', 'label': 'background music', 'media_kind': 'audio', 'source': 'existing', 'node_id': 'n'}], 'music-only')['work_ref']
    media['media_kind'] = 'audio'
    def not_configured():
        raise AssertionError('a music review must not resolve an ASR provider')
    monkeypatch.setattr('novelvideo.freezone.remix.source_speech.configured_provider_signature', not_configured)
    result = make_material_speech_scheduler(ctx, canvas_id='canvas')(store=store, canvas=canvas,
        work_ref=work, step_id='music', resource=media, native_result={}, workflow_state_dir=tmp_path, command_id='music-check')
    assert result['status'] == 'needs_review' and result['review_ref']
    from novelvideo.freezone.remix.operations import RemixOperationStore
    assert RemixOperationStore(tmp_path, 'project').list() == []
