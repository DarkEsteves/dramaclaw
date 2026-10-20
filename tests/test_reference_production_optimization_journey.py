"""Deterministic native evidence/compilation journeys; no real provider or user media.

Test human receipts are synthetic fixtures. They do not certify the live film.
The native queue recorder uses real project TaskState records, with synchronous
worker execution under test control; production quota adapters have separate tests.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import wave

from PIL import Image
import pytest

from test_production_advancement import production, composing, current  # noqa: F401

CASES = json.loads((Path(__file__).parent / 'fixtures/reference-production-optimization.json').read_text())['cases']


def source_project(tmp_path, case):
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.freezone.remix.material_execution import canvas_material_resource, review_material_candidate, confirm_material_review
    from novelvideo.freezone.remix.material_work import prepare_material_work, accept_material_candidate
    from novelvideo.freezone.remix.source_speech import candidate_scope
    from novelvideo.project_context import ProjectContext
    from test_material_work import plan_payload
    store = RemixArtifactStore(tmp_path, 'synthetic-project', created_by='synthetic-user')
    ctx = ProjectContext('synthetic-project', 'fixture', 'user', 'synthetic-user', 'synthetic-user',
        'synthetic-user', 'synthetic-user', (('user', 'synthetic-user'),), 'owner', 'local', tmp_path, tmp_path, tmp_path, True)
    canvas = {'project_id': ctx.project_id, 'canvas_id': 'fixture-canvas', 'access_model': 'private',
              'owner_principal_id': ctx.requester_user_id, 'nodes': [], 'edges': []}
    plan = plan_payload()
    plan['output'].update(width=320, height=180)
    plan['beats'] = []; plan['placements'] = []; plan['speech_takes'] = []
    Image.new('RGB', (320, 180), '#223344').save(tmp_path / 'background.png')
    canvas['nodes'].append({'id': 'background', 'type': 'imageNode', 'data': {
        'imageUrl': '/static/projects/synthetic-project/background.png'}})
    background = canvas_material_resource(store, canvas, 'background', 'image')
    plan['resources'] = [background]
    requirements = []; steps = []
    sources = []
    for index in range(case['source_count']):
        beat_id = f'beat-{index}'
        turns = [{'id': f'turn-{index}', 'speaker': 'Fixture', 'text': f'word{index}'}]
        if case['name'] == 'shared_source':
            turns = [{'id': 'one', 'speaker': 'A', 'text': 'one'}, {'id': 'two', 'speaker': 'B', 'text': 'two'}]
        if case['name'] == 'silent':
            turns = []
        plan['beats'].append({'id': beat_id, 'purpose': 'synthetic fixture', 'duration_frames': 24,
                              'timing_mode': 'auto' if turns else 'fixed', 'turns': turns})
        def anchor(edge): return {'kind': 'beat', 'ref_id': beat_id, 'edge': edge}
        plan['placements'].append({'id': f'picture-{index}', 'kind': 'visual', 'role': 'primary',
            'resource_id': background['id'], 'track_id': 'main', 'start': anchor('start'), 'end': anchor('end')})
        if not turns:
            continue
        name = f'voice-{index}'
        with wave.open(str(tmp_path / (name + '.wav')), 'wb') as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(8000)
            output.writeframes(bytes([index, 0]) * 32800)
        canvas['nodes'].append({'id': name, 'type': 'audioNode', 'data': {
            'audioUrl': f'/static/projects/synthetic-project/{name}.wav'}})
        media = canvas_material_resource(store, canvas, name, 'audio')
        plan['resources'].append(media)
        placement = f'speech-{index}'; take = f'take-{index}'
        plan['placements'].append({'id': placement, 'kind': 'audio', 'role': 'speech', 'track_id': 'speech',
            'resource_id': media['id'], 'start': anchor('start'), 'end': anchor('end')})
        plan['speech_takes'].append({'id': take, 'beat_id': beat_id, 'origin_placement_id': placement,
                                    'resource_id': media['id'], 'turn_ids': [t['id'] for t in turns]})
        for turn in turns:
            plan['placements'].append({'id': 'caption-' + turn['id'], 'kind': 'subtitle', 'role': 'dialogue',
                'turn_id': turn['id'], 'track_id': 'captions',
                'start': {'kind': 'turn', 'ref_id': turn['id'], 'edge': 'start'},
                'end': {'kind': 'turn', 'ref_id': turn['id'], 'edge': 'end'}})
        requirements.append({'id': name, 'take_ids': [take], 'turn_ids': [t['id'] for t in turns],
                             'description': name, 'media_kind': 'audio', 'final_step_id': name})
        steps.append({'id': name, 'label': name, 'source': 'existing', 'media_kind': 'audio', 'node_id': name})
        sources.append((take, media, turns))
    requirements.append({'id': 'background', 'placement_ids': [p['id'] for p in plan['placements'] if p['kind'] == 'visual'],
                         'description': 'background', 'media_kind': 'image', 'final_step_id': 'background'})
    steps.append({'id': 'background', 'label': 'background', 'source': 'existing', 'media_kind': 'image', 'node_id': 'background'})
    saved = store.create('production_plan.v2', plan, [])
    pref = {'artifact_id': saved['id'], 'revision': 1}
    work = prepare_material_work(store, pref, requirements, steps, 'fixture-work')['work_ref']
    # Actual native material validation and immutable user test receipts, rather
    # than fabricated content.status-only payloads.
    for step in steps:
        checked = review_material_candidate(store, work, step['id'], canvas, step['node_id'])
        checked = confirm_material_review(store, checked['review_ref'], canvas,
            canvas_id='fixture-canvas', requester_user_id=ctx.requester_user_id)
        work = accept_material_candidate(store, work, step['id'], checked['review']['resource'],
                                         checked['review_ref'], 'accept-' + step['id'], canvas_id='fixture-canvas')['work_ref']
    return ctx, canvas, store, pref, work, sources, candidate_scope(ctx, canvas, 'fixture-canvas')


@pytest.mark.parametrize('case', CASES[:3], ids=lambda x: x['name'])
def test_synthetic_sources_reuse_measured_evidence_and_compile_native_manifest(tmp_path, case):
    from novelvideo.freezone.remix.source_speech import observe_source_speech
    from novelvideo.freezone.remix.source_speech_adoption import adopt_source_observation
    from novelvideo.task_backend.runners.remix import run_remix_compile_production_plan, run_remix_render
    ctx, canvas, store, pref, work, sources, scope = source_project(tmp_path, case)
    calls = []
    for take, media, turns in sources:
        class Engine:
            def transcribe(self, path, language):
                calls.append(hashlib.sha256(path.read_bytes()).hexdigest())
                return {'status': 'completed', 'words': [
                    {'text': t['text'], 'start': .1 if i == 0 else 3.2,
                     'end': 4.1 if len(turns) == 1 or i else 1.0} for i, t in enumerate(turns)]}
        obs = observe_source_speech(store, scope=scope, resource=media, language='en', provider_signature='fixture', aligner=Engine())
        replay = observe_source_speech(store, scope=scope, resource=media, language='en', provider_signature='fixture', aligner=Engine())
        assert replay == obs
        adopted = adopt_source_observation(store, plan_ref=pref, take_id=take, observation_ref=obs,
            work_ref=work, scope=scope, requester_user_id=ctx.requester_user_id, command_id='adopt-' + take)
        pref = adopted['plan_ref']
        assert len(adopted['timeline_refs']) == len(turns)
    assert len(calls) == case['expect_asr_calls']
    saved = store.read(pref['artifact_id'], pref['revision'])
    assert len(saved['payload']['speech_takes']) == len(sources)
    compiled = run_remix_compile_production_plan({'payload': {'plan_ref': pref}}, ctx)
    manifest = store.read(compiled['artifact_ref']['artifact_id'], 1)['payload']
    assert manifest['source_versions'] == [pref]
    if case['name'] in {'silent', 'shared_source'}:
        rendered = run_remix_render({'payload': {'manifest_ref': compiled['artifact_ref'], 'job_id': 'f' * 64}}, ctx)
        import subprocess
        probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(ctx.output_dir / rendered['output_path'])]))
        video = next(s for s in probe['streams'] if s['codec_type'] == 'video')
        assert (video['width'], video['height'], video['r_frame_rate']) == (320, 180, '24/1')
        if sources:
            assert any(s['codec_type'] == 'audio' for s in probe['streams'])
        else:
            assert not manifest.get('audio_clips')  # Renderer may encode its silent fallback track.
        assert abs(float(probe['format']['duration']) - (4.1 if sources else 3)) < .2


def test_reconnect_deliveries_retain_one_actual_native_result(tmp_path):
    from test_material_settlement import native_material, settle
    from novelvideo.freezone.remix.store import RemixArtifactStore
    fixture = native_material(tmp_path)
    first = settle(fixture)
    version = fixture[0].read_latest(fixture[3]['artifact_id'])['revision']
    for _ in range(CASES[3]['completion_deliveries']):
        restarted = (RemixArtifactStore(fixture[0].project_dir, 'project', created_by='alice'), *fixture[1:])
        assert settle(restarted) == first
    assert fixture[0].read_latest(fixture[3]['artifact_id'])['revision'] == version
    assert len(list(fixture[0].project_dir.glob('picture.png'))) == 1


def test_replacing_one_source_invalidates_only_its_branch(tmp_path):
    from novelvideo.freezone.remix.source_speech import observe_source_speech, read_source_speech_observation
    from novelvideo.freezone.remix.material_work import invalidate_material_step
    case = CASES[4]
    ctx, canvas, store, pref, work, sources, scope = source_project(tmp_path, case)
    observed = []
    class Engine:
        def transcribe(self, *args):
            return {'status': 'completed', 'words': [{'text': 'fixture', 'start': .1, 'end': 4.1}]}
    for _, media, _ in sources:
        ref = observe_source_speech(store, scope=scope, resource=media, language='en', provider_signature='fixture', aligner=Engine())
        observed.append(ref)
    (tmp_path / sources[0][1]['media_path']).write_bytes(b'replaced')
    with pytest.raises(ValueError, match='changed'):
        read_source_speech_observation(store, observed[0], scope=scope, resource=sources[0][1], language='en', provider_signature='fixture')
    for index in range(1, len(sources)):
        assert read_source_speech_observation(store, observed[index], scope=scope, resource=sources[index][1], language='en', provider_signature='fixture')['id'] == observed[index]['artifact_id']
    updated = invalidate_material_step(store.read(work['artifact_id'], work['revision'])['payload'], 'voice-0', 'source changed')
    assert sum(s['status'] == 'stale' for s in updated['steps']) == case['expect_invalidated_takes']
    assert all(s['status'] == 'accepted' for s in updated['steps'] if s['id'] != 'voice-0')


@pytest.mark.asyncio
async def test_native_advance_queue_compile_render_and_test_delivery_receipt(production, monkeypatch):
    from novelvideo import ports, task_state
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.remix.production_advancement import advance_production
    from novelvideo.freezone.remix.results import confirm_render
    from novelvideo.freezone.remix.production_output_review import prepare_output_review, confirm_output_review
    from novelvideo.task_backend.runners.remix import run_remix_compile_production_plan, run_remix_render
    ctx, store, commission, _, _ = production
    saved_commission = store.read_latest(commission['commission_ref']['artifact_id'])
    payload = deepcopy(saved_commission['payload'])
    payload['production_scope'] = '预览成片'
    store.revise(saved_commission['id'], saved_commission['revision'], payload, saved_commission['upstream_refs'])
    manager = task_state.TaskStateManager()
    queued = []
    class Backend:
        async def enqueue_project_task(self, actual_ctx, **request):
            assert actual_ctx is ctx
            state = manager.create_task_for_project(ctx, request['task_type'], request['episode'],
                scope=request['scope'], queue_kind=request['queue_kind'])
            queued.append((request, state.task_id))
            return SimpleNamespace(task_state=state)
    monkeypatch.setattr(ports, 'get_task_backend', lambda: Backend())
    monkeypatch.setattr(task_state, 'get_task_manager', lambda: manager)
    state = await composing(production)
    async def step(command):
        fresh = current(production)
        return await advance_production(ctx, canvas_id='c', commission_ref=fresh['commission_ref'],
            expected_projection_revision=fresh['projection_revision'], command_id=command)
    compiled = await step('compile-journey')
    request, task_id = queued[-1]
    assert request['task_type'] == 'remix_compile_production_plan'
    native_manifest = run_remix_compile_production_plan({'__run_task_id': task_id, 'payload': request['payload']}, ctx)
    assert manager.complete_task_for_project(ctx, request['task_type'], request['episode'],
        scope=request['scope'], result=native_manifest, expected_task_id=task_id)
    settled = await step('settle-journey')
    assert settled['next_action'] and settled['next_action']['kind'] == 'render_manifest', settled
    rendered_request = await step('render-journey')
    request, task_id = queued[-1]
    assert request['task_type'] == 'remix_render' and request['queue_kind'] == 'ffmpeg'
    rendered = run_remix_render({'__run_task_id': task_id, 'payload': request['payload']}, ctx)
    assert manager.complete_task_for_project(ctx, request['task_type'], request['episode'],
        scope=request['scope'], result=rendered, expected_task_id=task_id)
    operation_id = rendered_request['advancement']['result']['operation_id']
    confirm_render(ctx.output_dir, ctx.state_dir, ctx.project_id, operation_id, canvas_id='c', node_id='compose')
    state = current(production)
    assert state['next_action']['kind'] == 'review_output'
    canvas = canvas_store.read_canvas(ctx.state_dir, 'c')
    review = prepare_output_review(store, ctx, canvas, state['commission_ref'], 'compose')
    finished = confirm_output_review(store, ctx, canvas, {'artifact_id': review['id'], 'revision': 1},
                                     viewed_complete_media=True)
    assert finished['commission']['phase'] == 'completed'
    assert current(production)['execution_status'] == 'completed'
    assert len(queued) == 2
    assert store.read(native_manifest['artifact_ref']['artifact_id'], 1)['payload']['source_versions'] == [
        {'artifact_id': state['plan_ref']['artifact_id'], 'revision': state['plan_ref']['revision']}]
    assert (ctx.output_dir / rendered['output_path']).is_file()


def test_source_evidence_cannot_cross_private_tenant_scope(tmp_path):
    from novelvideo.freezone.remix.source_speech import observe_source_speech, read_source_speech_observation
    case = CASES[2]
    ctx, canvas, store, pref, work, sources, scope = source_project(tmp_path, case)
    class Engine:
        def transcribe(self, *args):
            return {'status': 'completed', 'words': [{'text': 'one two', 'start': .1, 'end': 4.1}]}
    ref = observe_source_speech(store, scope=scope, resource=sources[0][1], language='en', provider_signature='fixture', aligner=Engine())
    with pytest.raises(ValueError, match='provenance'):
        read_source_speech_observation(store, ref, scope={**scope, 'owner_id': 'another-tenant', 'private_owner_id': 'another-user'},
                                      resource=sources[0][1], language='en', provider_signature='fixture')
