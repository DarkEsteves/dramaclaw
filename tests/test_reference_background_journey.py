"""Controlled external calls; real saved context, bridge, CAS and native media.

The local ASGI host supplies a fixture identity. No live model/provider is called.
macOS speech fixtures use local built-in voices and original one-word dialogue;
other hosts explicitly skip that case when no local speech renderer is installed.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from novelvideo.chat import reference_remix_continuation as continuation
from novelvideo.chat.store import ChatStore
from novelvideo.freezone import canvas_command_bridge as bridge, canvas_store
from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.project_context import ProjectContext


def project(tmp_path):
    return ProjectContext('p', 'fixture', 'user', 'u', 'actor', 'u', 'actor',
                          (('user', 'u'),), 'owner', 'local', tmp_path / 'output',
                          tmp_path / 'state', tmp_path / 'runtime', True)


def saved_canvas(ctx):
    return canvas_store.read_canvas(ctx.state_dir, 'c')


def source_canvas(ctx):
    return canvas_store.save_canvas(ctx.state_dir, 'c', base_revision=None,
        build_payload=lambda _: {'project_id': 'p', 'canvas_id': 'c',
        'access_model': 'private', 'owner_principal_id': 'u', 'nodes': [
            {'id': 'source', 'type': 'videoNode', 'data': {'videoUrl': '/fixture.mp4'}},
            {'id': 'analysis', 'type': 'videoStoryNode', 'data': {
                'sourceVideoUrl': '/fixture.mp4', 'rows': [], 'isAnalyzing': True}}],
        'edges': [{'id': 'source-analysis', 'source': 'source', 'target': 'analysis'}]})


@pytest.mark.asyncio
async def test_completion_to_direction_requires_native_canvas_plan(tmp_path, monkeypatch):
    from novelvideo.api.auth import get_api_user
    from novelvideo.api.routes import canvas_context, chat, freezone, reference_director
    from novelvideo.api import egress_binding
    from novelvideo.chat.reference_decision_ingress import apply_user_confirmation, pending_user_decision
    from novelvideo.freezone.remix.reference_director import list_director_artifacts

    ctx = project(tmp_path)
    source_canvas(ctx)
    coordinator = continuation.ReferenceRemixCoordinator(
        continuation.ReferenceRemixStore(tmp_path / 'runs.db'))
    monkeypatch.setattr(coordinator, '_ensure_worker', lambda _: None)
    async def resolve(*args, **kwargs):
        return ctx
    async def route_resolve(*args, **kwargs):
        return ctx, 'actor', 'p', ctx.output_dir, str(ctx.output_dir)
    monkeypatch.setattr(continuation, 'resolve_project_context', resolve)
    monkeypatch.setattr(freezone, '_resolve_freezone_project', route_resolve)
    monkeypatch.setattr(chat, '_canvas_bridge_dir', lambda *a, **k: tmp_path / 'bridge')
    @asynccontextmanager
    async def egress(**kwargs):
        yield None
    monkeypatch.setattr(egress_binding, 'request_egress_scope', egress)
    run = await coordinator.reserve(ctx=ctx, canvas_id='c', source_node_id='source',
        analysis_node_id='analysis', goal='保留结构并换产品', inputs={'plan_count': 1},
        idempotency_key='journey')
    token = await coordinator.claim_submission(ctx=ctx, run_id=run['run_id'])
    manager = continuation.get_task_manager()
    task = manager.create_task_for_project(ctx, continuation.TASK_TYPE, 0, scope=run['job_id'])
    result = {'job_id': run['job_id'], 'video_story': {
        'title': '无台词产品演示', 'summary': '先展示问题，再展示产品解决问题。',
        'shots': [{'shot': 1, 'narrative': '问题到解决', 'voice_sound': '无台词'}]}}
    assert manager.complete_task_for_project(ctx, continuation.TASK_TYPE, 0,
        scope=run['job_id'], result=result, expected_task_id=task.task_id)
    task = manager.get_task_for_project(ctx, continuation.TASK_TYPE, 0, scope=run['job_id'])
    assert task.status == 'completed' and task.result == result
    await coordinator.attach_task(ctx=ctx, run_id=run['run_id'], task_id=task.task_id,
                                  submission_token=token)
    assert coordinator.store.claim(run['run_id'], coordinator.owner_token)
    app = FastAPI()
    for router in (canvas_context.router, reference_director.router, chat.router):
        app.include_router(router, prefix='/api/v1')
    user = {'username': 'actor', 'user_id': 'u', 'credential_kind': 'agent_session',
            'current_scope_kind': 'project', 'current_project_id': 'p',
            'scopes': ['projects:read', 'projects:write'], 'role': 'owner'}
    app.dependency_overrides[get_api_user] = lambda: user
    base = '/api/v1/projects/p/freezone/canvases/c'
    reads = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
        async def controlled_model(*args, **kwargs):
            # Keep actual hosted turn binding and saved-read receipt; only model reasoning is controlled.
            response = await client.post(base + '/context/read', headers={
                'X-DramaClaw-Turn-ID': kwargs['turn_id'], 'X-DramaClaw-Agent-ID': 'main'},
                json={'requests': [{'type': 'node_detail', 'node_id': 'analysis'}],
                      'read_consistency': 'saved'})
            assert response.status_code == 200, response.text
            read = response.json()['data']
            reads.append(read)
            assert read['source'] == 'saved_canvas'
            assert '先展示问题' in json.dumps(read, ensure_ascii=False)
            assert '动态选中' in args[2]
            store = RemixArtifactStore(ctx.output_dir, 'p', created_by='u')
            commission = list_director_artifacts(store, 'c', 'u')['commissions'][0]
            decision = await client.post(base + '/director/decisions', json={
                'commission_ref': commission['commission_ref'], 'kind': 'direction',
                'command_id': 'direction', 'proposal': {'target_goal': '保留结构并换产品'},
                'summary': '先问题后解决；无台词，换产品画面'})
            assert decision.status_code == 200, decision.text
            await args[3]({'type': 'assistant_message', 'message': {
                'text': '原片先展示问题，再展示产品解决问题。建议保留结构、替换产品，等待你确认方向。'}})
        monkeypatch.setattr(continuation.chat_service, 'stream_assistant_reply', controlled_model)
        await coordinator._process_once(coordinator.store.get(run['run_id']))
        current = coordinator.store.get(run['run_id'])
        assert current['status'] == 'awaiting_direction', current
        assert current['last_successful_context_receipt']['source'] == 'saved_canvas'
        assert current['execution_blocker'] is None
        assert len(reads) == 1
        assert saved_canvas(ctx)['nodes'][1]['data']['rawResult'] == task.result
        messages = ChatStore().list_messages('actor', coordinator.scope(current), limit=20)
        assert any('原片分析已保存' in m['content'] for m in messages)
        # A fresh coordinator sees the same completed task and waiting decision, no resubmission.
        recovered = continuation.ReferenceRemixStore(coordinator.store.db_path).get(run['run_id'])
        assert recovered['task_id'] == task.task_id and recovered['commission_ref'] == current['commission_ref']
        store = RemixArtifactStore(ctx.output_dir, 'p', created_by='u')
        scope = coordinator.scope(current)
        assistant = ChatStore().append_message('actor', scope, 'assistant', '确认方向')
        assert apply_user_confirmation(store, scope=scope, requester_user_id='u',
            credential_kind='browser_session', message=assistant) is None
        msg = ChatStore().append_message('actor', scope, 'user', '确认方向')
        direction = pending_user_decision(store, scope=scope, requester_user_id='u',
            credential_kind='browser_session', message=msg)
        confirmed = apply_user_confirmation(store, scope=scope, requester_user_id='u',
            credential_kind='browser_session', message=msg,
            interpretation={'intent': 'approve'}, expected_decision_ref=direction['decision_ref'])['commission']
        assert confirmed['phase'] == 'drafting_plan'
        assert confirmed['decisions'][-1]['message_id'].endswith(':' + str(msg['id']))
        direct = await client.post(base + '/director/direct-plan', json={
            'commission_ref': confirmed['commission_ref'], 'goal': '保留结构并换产品',
            'output': {'width': 320, 'height': 180, 'fps_num': 24, 'fps_den': 1},
            'command_id': 'native-plan'})
        assert direct.status_code == 409
        assert 'workflow Intent' in direct.text
        assert all('productionPlanV2Ref' not in (node.get('data') or {})
                   for node in saved_canvas(ctx)['nodes'])
        assert len(reads) == 1 and coordinator.store.get(run['run_id'])['task_id'] == task.task_id


@pytest.mark.asyncio
async def test_disconnected_context_reconnects_with_real_http_receipt(tmp_path, monkeypatch):
    from novelvideo.api.auth import get_api_user
    from novelvideo.api.routes import chat
    from novelvideo.chat.canvas_tool_session import dispatch_pending_bridge_messages
    from novelvideo.chat.store import ChatScope
    ctx = project(tmp_path)
    source_canvas(ctx)
    scope = ChatScope('project', 'p', 'freezone', 'c', 'main', str(ctx.state_dir))
    directory = tmp_path / 'bridge'
    for key, value in {'USERNAME': 'actor', 'PROJECT_ID': 'p', 'CANVAS_ID': 'c',
                       'AGENT_ID': 'main', 'TURN_ID': 'resume'}.items():
        monkeypatch.setenv('DRAMACLAW_' + key, value)
    monkeypatch.setattr(chat, '_canvas_bridge_dir', lambda *a, **k: directory)
    async def state(*args):
        return ctx.state_dir
    async def resolve(*args):
        return ctx
    monkeypatch.setattr(chat, '_bridge_project_state_dir', state)
    monkeypatch.setattr(chat, '_project_context_for_scope', resolve)
    app = FastAPI()
    app.include_router(chat.router, prefix='/api/v1')
    app.dependency_overrides[get_api_user] = lambda: {'username': 'actor', 'user_id': 'u', 'credential_kind': 'browser_session'}
    bridge.put_pending_canvas_context(key='selection', project_id='p', canvas_id='c',
        requests=[{'type': 'selection_detail'}], envelope={'requests': [{'type': 'selection_detail'}]}, bridge_dir=directory)
    async def offline(frame):
        return False
    await dispatch_pending_bridge_messages(username='actor', scope=scope, turn_id='resume',
        bridge_dirs=[directory], send=offline, emitted_bridge_keys=set())
    assert bridge.read_bridge_message('selection', bridge_dir=directory)['transport_status'] == 'pending'
    # New host session after disconnect delivers the persisted request, preserving turn identity.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
        async def browser(frame):
            response = await client.post('/api/v1/chat/canvas-context-tool-result', json={
                'bridge_key': frame['bridge_key'], 'turn_id': frame['turn_id'],
                'project_id': 'p', 'canvas_id': 'c', 'agent_id': 'main',
                'responses': [{'type': 'selection_detail', 'data': {'node_ids': ['source']}}]})
            assert response.status_code == 200, response.text
            return True
        emitted = set()
        await dispatch_pending_bridge_messages(username='actor', scope=scope, turn_id='resume',
            bridge_dirs=[directory], send=browser, emitted_bridge_keys=emitted)
    result = await asyncio.to_thread(bridge.wait_canvas_context_result, 'selection', 0.2, bridge_dir=directory)
    assert result['source'] == 'current_browser'
    assert result['responses'][0]['data']['node_ids'] == ['source']
    assert emitted == {'selection'}
    row = bridge.read_bridge_message('selection', bridge_dir=directory)
    assert row['delivery_binding']['turn_id'] == 'resume' and row['transport_status'] == 'applied'


@pytest.fixture
def native_media_tools(monkeypatch):
    # Homebrew tools are real installed binaries, not test shims.
    monkeypatch.setenv('PATH', '/opt/homebrew/bin:' + os.environ.get('PATH', ''))
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('real FFmpeg and FFprobe required')


def ffmpeg(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', *map(str, args)], check=True, capture_output=True)


def probe(path):
    return json.loads(subprocess.run(['ffprobe', '-v', 'error', '-show_format', '-show_streams',
        '-of', 'json', str(path)], check=True, capture_output=True, text=True).stdout)


def media_plan(media, *, dialogue=False):
    duration = float(probe(media)['format']['duration'])
    return {'schema_version': 2, 'goal': 'original local fixture',
        'output': {'width': 320, 'height': 180, 'fps_num': 24, 'fps_den': 1},
        'beats': [{'id': 'opening', 'purpose': 'fixture', 'duration_frames': math.ceil(duration * 24),
            'turns': ([{'id': 'guest', 'speaker': 'Samantha', 'text': 'Hello', 'speech_placement_id': 'picture'},
                       {'id': 'host', 'speaker': 'Daniel', 'text': 'Goodbye', 'speech_placement_id': 'picture'}] if dialogue else [])}],
        'resources': [{'id': 'video', 'node_id': 'uploaded-video', 'media_kind': 'video',
            'media_path': 'media/source.mp4', 'media_hash': hashlib.sha256(media.read_bytes()).hexdigest(),
            'duration_us': round(duration * 1_000_000), 'has_audio': dialogue}],
        'placements': [{'id': 'picture', 'kind': 'visual', 'role': 'primary', 'track_id': 'main',
            'resource_id': 'video', 'start': {'kind': 'beat', 'ref_id': 'opening', 'edge': 'start'},
            'end': {'kind': 'beat', 'ref_id': 'opening', 'edge': 'end'}}], 'provenance': {}}


def compile_export(store, plan, media):
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.task_backend.runners import remix as runner
    ref = {'artifact_id': plan['id'], 'revision': plan['revision']}
    ctx = SimpleNamespace(output_dir=store.project_dir, project_id='p',
                          requester_user_id='u')
    operations = RemixOperationStore(store.project_dir, 'p')
    compile_op = operations.start(idempotency_key='compile', input_refs=[ref],
        producer_version='remix-compile-production-plan.v1', normalized_parameters={},
        node_revision=1, canvas_id='c', node_id='compose')
    compiled = runner.run_remix_compile_production_plan({'payload': {
        'plan_ref': ref, 'operation_id': compile_op['operation_id']}}, ctx)
    manifest_ref = compiled['artifact_ref']
    assert operations.get(compile_op['operation_id'])['status'] == 'needs_confirmation'
    assert operations.confirm(compile_op['operation_id'], current_input_refs=[ref],
        current_node_revision=1, canvas_id='c', node_id='compose')['status'] == 'completed'
    saved = store.read(manifest_ref['artifact_id'], manifest_ref['revision'])
    manifest = saved['payload']
    render_op = operations.start(idempotency_key='render', input_refs=[manifest_ref],
        producer_version='remix-render.v1', normalized_parameters={},
        node_revision=1, canvas_id='c', node_id='compose')
    exported = runner.run_remix_render({'payload': {'manifest_ref': manifest_ref,
        'job_id': '0' * 64, 'operation_id': render_op['operation_id']}}, ctx)
    assert operations.get(render_op['operation_id'])['status'] == 'needs_confirmation'
    assert operations.confirm(render_op['operation_id'], current_input_refs=[manifest_ref],
        current_node_revision=1, canvas_id='c', node_id='compose')['status'] == 'completed'
    result = store.project_dir / exported['output_path']
    video = next(stream for stream in probe(result)['streams'] if stream['codec_type'] == 'video')
    assert (video['width'], video['height']) == (320, 180)
    assert abs(float(probe(result)['format']['duration']) -
               manifest['output_spec']['duration_frames'] / 24) < 0.12
    assert saved['upstream_refs'][0]['artifact_id'] == plan['id']
    assert exported['manifest_ref'] == manifest_ref
    return manifest, result


def audio_rms(path, start):
    import array
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(start), '-i', str(path),
        '-t', '0.2', '-vn', '-ac', '1', '-ar', '48000', '-f', 'f32le', 'pipe:1'],
        check=True, capture_output=True).stdout
    samples = array.array('f')
    samples.frombytes(raw)
    return math.sqrt(sum(x*x for x in samples) / len(samples))


def test_no_dialogue_actual_native_compile_export(tmp_path, native_media_tools):
    from novelvideo.chat.reference_production_observer import production_event
    media = tmp_path / 'media/source.mp4'
    media.parent.mkdir()
    ffmpeg('-f', 'lavfi', '-i', 'color=c=blue:s=320x180:d=2:r=24', '-an', '-c:v', 'libx264', media)
    store = RemixArtifactStore(tmp_path, 'p')
    plan = store.create('production_plan.v2', media_plan(media), [])
    manifest, result = compile_export(store, plan, media)
    assert manifest['subtitle_events'] == [] and manifest['audio_events'] == []
    # Saved no-dialogue plan yields an explicit skip; not a fabricated ASR result.
    ref = {'artifact_id': plan['id'], 'revision': 1, 'project_id': 'p'}
    observation = production_event(store, {'nodes': [{'id': 'analysis', 'data': {}}, {'id': 'plan', 'data': {'productionPlanV2Ref': {'artifactId': plan['id'], 'revision': 1}}}], 'edges': [{'source': 'analysis', 'target': 'plan'}]}, {
        'phase': 'aligning_speech', 'plan_ref': ref, 'analysis_node_id': 'analysis'})
    assert observation['event']['type'] == 'speech_aligned'
    assert observation['event']['plan_ref']['artifact_id'] == plan['id']
    assert not any(stream['codec_type'] == 'audio' for stream in probe(media)['streams'])
    assert audio_rms(result, 0.5) < 0.00001  # Native renderer adds a silent audio stream.
    # Bound media version is checked by the actual compiler, not trusted from
    # the fixture's declared hash. A changed source cannot silently re-export.
    from novelvideo.task_backend.runners import remix as runner
    media.write_bytes(media.read_bytes() + b'changed-source-version')
    with pytest.raises(ValueError, match='not ready'):
        runner.run_remix_compile_production_plan({'payload': {'plan_ref': {
            'artifact_id': plan['id'], 'revision': 1}}},
            SimpleNamespace(output_dir=tmp_path, project_id='p', requester_user_id='u'))


def test_shared_two_speaker_actual_media_evidence_and_export(tmp_path, native_media_tools, monkeypatch):
    from novelvideo.task_backend.runners import remix as runner
    if not Path('/usr/bin/say').exists():
        pytest.skip('offline macOS built-in voices required for two-speaker fixture')
    media = tmp_path / 'media/source.mp4'
    media.parent.mkdir()
    voices = [('Samantha', 'Hello'), ('Daniel', 'Goodbye')]
    sources = []
    for index, (voice, text) in enumerate(voices):
        audio = media.parent / f'{index}.aiff'
        subprocess.run(['/usr/bin/say', '-v', voice, '-r', '150', '-o', str(audio), text],
                       check=True, capture_output=True)
        sources.append(audio)
    first_duration = float(probe(sources[0])['format']['duration'])
    second_duration = float(probe(sources[1])['format']['duration'])
    total = first_duration + second_duration
    ffmpeg('-f', 'lavfi', '-i', f'color=c=blue:s=320x180:d={total}:r=24',
        '-i', sources[0], '-i', sources[1], '-filter_complex', '[1:a][2:a]concat=n=2:v=0:a=1[a]',
        '-map', '0:v', '-map', '[a]', '-c:v', 'libx264', '-c:a', 'aac', media)
    payload = media_plan(media, dialogue=True)
    payload['speech_takes'] = [{'id': 'take', 'beat_id': 'opening', 'resource_id': 'video',
        'turn_ids': ['guest', 'host'], 'origin_placement_id': 'picture',
        'source_in_us': 0, 'source_out_us': None, 'speed_num': 1, 'speed_den': 1, 'lead_in_frames': 0}]
    payload['placements'][0]['speech_take_id'] = 'take'
    store = RemixArtifactStore(tmp_path, 'p')
    plan = store.create('production_plan.v2', payload, [])
    calls = []
    # External recognition is controlled. These source-chunk bounds are fixture
    # timings, not evidence of live acoustic recognition or an actual ASR quality score.
    def recognize(path, script):
        calls.append((path, script['text']))
        return {'language': 'en', 'status': 'completed', 'transcript': script['text'],
            'words': [{'text': 'Hello', 'start_char': 0, 'end_char': 5, 'start_us': 0,
                       'end_us': round(first_duration * 1_000_000)},
                      {'text': 'Goodbye', 'start_char': 6, 'end_char': 13,
                       'start_us': round(first_duration * 1_000_000),
                       'end_us': round(total * 1_000_000)}]}
    monkeypatch.setattr(runner, 'align_script', recognize)
    result = runner.run_remix_align_production_speech({'payload': {
        'media_path': 'media/source.mp4', 'media_sha256': payload['resources'][0]['media_hash'],
        'plan_ref': {'artifact_id': plan['id'], 'revision': 1}, 'turn_id': 'guest', 'take_id': 'take'}},
        SimpleNamespace(output_dir=tmp_path, project_id='p', requester_user_id='u'))
    assert calls == [(media, 'Hello\nGoodbye')] and result['status'] == 'completed'
    primary = store.read(result['artifact_ref']['artifact_id'], 1)
    other_ref = primary['payload']['group_refs'][0]
    other = store.read(other_ref['artifact_id'], other_ref['revision'])
    revised = deepcopy(plan['payload'])
    refs = []
    revised['speech_cues'] = []
    for turn, evidence in zip(revised['beats'][0]['turns'], (primary, other), strict=True):
        ep = evidence['payload']
        ref = {'artifact_id': evidence['id'], 'revision': 1}
        refs.append(ref)
        turn['speech_timing'] = {'placement_id': 'picture', 'media_hash': payload['resources'][0]['media_hash'],
            'text': turn['text'], 'text_hash': ep['text_hash'], 'source_in_us': 0,
            'source_out_us': payload['resources'][0]['duration_us'], 'evidence_ref': ref,
            'words': [{'id': f"{turn['id']}:word:{index}", **word} for index, word in enumerate(ep['words'])]}
        revised['speech_cues'].append({'id': f"cue:{turn['id']}", 'turn_id': turn['id'], 'take_id': 'take',
            'text': turn['text'], 'source_start_us': ep['words'][0]['start_us'],
            'source_end_us': ep['words'][-1]['end_us'], 'timing_source': 'asr', 'timing_status': 'ready',
            'word_ids': [word['id'] for word in turn['speech_timing']['words']], 'evidence_ref': ref})
    saved = store.revise(plan['id'], 1, revised, refs)
    assert [row['text'] for row in saved['payload']['speech_cues']] == ['Hello', 'Goodbye']
    assert all(row['word_ids'] for row in saved['payload']['speech_cues'])
    manifest, exported = compile_export(store, saved, media)
    assert len(manifest['layers']) == 1 and len(manifest['audio_events']) == 1
    assert len(saved['payload']['speech_cues']) == 2
    assert any(stream['codec_type'] == 'audio' for stream in probe(exported)['streams'])
    # Actual PCM in both source segments survives; compare the rendered source
    # rather than certifying listening, pronouncing words or continuous motion.
    for start in (0.1, first_duration + 0.1):
        original, rendered = audio_rms(media, start), audio_rms(exported, start)
        assert original > 0.001 and 0.75 < rendered / original < 1.25


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['failure', 'wrong_turn', 'wrong_actor', 'wrong_canvas',
                                  'wrong_agent', 'successful_read', 'successful_browser', 'empty_browser', 'other_node', 'direction', 'receipt'])
async def test_historical_real_tool_status_snapshot_recovery(tmp_path, monkeypatch, case):
    from novelvideo.freezone.remix.reference_commission import create_reference_commission
    ctx = project(tmp_path)
    source_canvas(ctx)
    coordinator = continuation.ReferenceRemixCoordinator(
        continuation.ReferenceRemixStore(tmp_path / 'runs.db'))
    monkeypatch.setattr(coordinator, '_ensure_worker', lambda _: None)
    run = await coordinator.reserve(ctx=ctx, canvas_id='c', source_node_id='source',
        analysis_node_id='analysis', goal='g', inputs={}, idempotency_key='historical')
    run = coordinator.store.get(run['run_id'])
    store = RemixArtifactStore(ctx.output_dir, 'p', created_by='u')
    commission = create_reference_commission(store, canvas_id='c', requester_user_id='u',
        analysis_node_id='analysis', goal='g', analysis_evidence={'task_id': 'original-paid-task', 'result_fingerprint': 'fixture'})
    assert coordinator.store.claim(run['run_id'], coordinator.owner_token)
    run.update(status='awaiting_direction', commission_ref=commission['commission_ref'],
        commission_phase='awaiting_direction', resume_turn_id='original-resume',
        task_id='original-paid-task', execution_blocker=None,
        last_successful_context_receipt=({'source': 'saved_canvas'} if case == 'receipt' else None))
    assert coordinator.store.update_owned(run, coordinator.owner_token)
    coordinator.store.release(run['run_id'], coordinator.owner_token)
    scope = coordinator.scope(run)
    identity = scope.to_dict()
    if case == 'wrong_canvas':
        identity['canvasId'] = 'other'
    if case == 'wrong_agent':
        identity['agentId'] = 'other'
    raw = {'type': 'agent.tool.updated', 'name': 'dramaclaw.freezone_get_node_detail',
        'turn_id': 'other' if case == 'wrong_turn' else run['resume_turn_id'], 'scope': identity,
        'result_json': {'ok': False, 'status': 'failed',
            'errors': ['Persistent canvas inbox message expired before completion.'],
            'tool_call_status': 'failed', 'bridge_key': 'original-request'}}
    parts = [{'type': 'tool_status', 'event': {'raw': raw}}]
    if case in {'successful_read', 'successful_browser', 'empty_browser', 'other_node'}:
        node = {'id': 'other' if case == 'other_node' else 'analysis',
                'type': 'videoStoryNode', 'data': {'rawResult': {'video_story': {
                    'summary': 'actual saved content'}}, 'sourceVideoUrl': '/fixture.mp4'}}
        response = {'ok': True, 'status': 'canvas_context_frontend_result',
                    'responses': [] if case == 'empty_browser' else [{'data': {'nodes': [node]}}]}
        if case == 'successful_read':
            response = {'ok': True, 'data': {'responses': [{'data': {'nodes': [node]}}]}}
        # Actual later optional context error must not erase a successful target read.
        parts.insert(0, {'type': 'tool_status', 'event': {'raw': {**raw, 'result_json': response}}})
    if case == 'direction':
        from novelvideo.freezone.remix.reference_commission import advance_reference_commission, AuthenticatedDecisionEvidence
        advance_reference_commission(store, commission['commission_ref'],
            {'type': 'direction_confirmed', 'direction': {'target_goal': 'g'}}, 1,
            host_evidence=AuthenticatedDecisionEvidence('p', 'c', 'u', 'real-direction-message'))
    if case == 'wrong_actor':
        from dataclasses import replace
        scope = replace(scope, state_dir=str(tmp_path / 'peer-state'))
    await ChatStore().append_ui_event_async('peer' if case == 'wrong_actor' else 'actor',
        scope, run['resume_turn_id'], {'type': 'assistant.message_parts', 'parts': parts})
    rows = await coordinator.list(ctx=ctx, canvas_id='c')
    recovered = next(row for row in rows if row['run_id'] == run['run_id'])
    if case in {'failure', 'empty_browser', 'other_node'}:
        assert recovered['status'] == 'resume_failed'
        assert recovered['execution_blocker']['historical_recovery'] is True
    else:
        assert recovered['status'] == 'awaiting_direction'
    assert recovered['task_id'] == 'original-paid-task'
    assert (await coordinator.get(ctx=ctx, run_id=run['run_id']))['status'] == recovered['status']
