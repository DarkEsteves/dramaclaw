from pathlib import Path
from types import SimpleNamespace
import hashlib
import pytest


def context(tmp_path):
    return SimpleNamespace(output_dir=tmp_path, project_id='project-a', is_home_node=True,
                           requester_username='alice', requester_user_id='alice', effective_role='editor')


@pytest.mark.parametrize('organization,trusted', [(False, True), (True, True), (True, False)])
def test_joint_story_preserves_verified_runner_identity(tmp_path, monkeypatch, organization, trusted):
    from novelvideo.egress_context import TRUSTED_EGRESS_CONTEXT_KEY, TrustedEgressContext, TrustedRunnerEnvelope
    from novelvideo.ports.authz import BillingPrincipal
    from novelvideo.ports.model_credentials import CredentialReference
    from novelvideo.freezone.remix import reference_analysis_service as service
    from novelvideo.task_backend.envelope import InvalidTaskEnvelope
    from novelvideo.task_backend.runners import freezone, remix

    path = tmp_path / 'clip.mp4'
    path.write_bytes(b'video')
    digest = hashlib.sha256(b'video').hexdigest()
    identity = TrustedEgressContext(
        envelope_id='envelope', project_id='project-a', task_type=service.TASK_TYPE,
        requester_user_id='alice', root_task_id='task', admission_id='admission',
        admitted_at='2026-10-10T10:00:00Z', membership_id='member' if organization else None,
        authz_version=1,
        billing_principal=BillingPrincipal(kind='organization' if organization else 'platform', id='org' if organization else 'platform'),
        credential=CredentialReference(source='organization' if organization else 'platform', credential_id='credential',
                                       key_version=1, org_id='org' if organization else None),
    )
    envelope_type = TrustedRunnerEnvelope if trusted else dict
    envelope = envelope_type({'task_type': service.TASK_TYPE,
                              'payload': {'media_path': 'clip.mp4', 'media_sha256': digest},
                              TRUSTED_EGRESS_CONTEXT_KEY: identity})
    monkeypatch.setattr(remix, 'build_reference_evidence', lambda _: {
        'media_hash': 'sha256:' + digest, 'time_base': '1/1000', 'duration_us': 1000000,
        'frame_count': 30, 'cuts': [], 'representative_frames': [{'time_us': 0, 'pts': 0, 'time_base': '1/1000'}]})
    monkeypatch.setattr(service, 'probe_audio_track', lambda _: False)
    async def story(story_envelope, ctx):
        assert freezone._extract_trusted_egress_context(story_envelope) is identity
        assert story_envelope['payload']['evidence_ref']
        async def vision(**kwargs):
            assert kwargs.get('egress_context') is (identity if organization else None)
        await freezone._call_freezone_leaf(story_envelope, vision, 'run_freezone_analyze_shots')
        return {'video_story': {'summary': 'Actual source analysis'}, 'reference_events': [], 'frame_times_us': []}
    monkeypatch.setattr(service, 'analyze_story', story)
    if trusted:
        result = remix.run_remix_joint_reference_analysis(envelope, context(tmp_path))
        assert result['video_story']['summary'] == 'Actual source analysis'
        from novelvideo.freezone.remix.store import RemixArtifactStore
        store = RemixArtifactStore(tmp_path, 'project-a', created_by='alice')
        for key in ('evidence_ref', 'blueprint_ref', 'analysis_ref'):
            assert store.read(**result[key])['created_by'] == 'alice'
    else:
        with pytest.raises(InvalidTaskEnvelope):
            remix.run_remix_joint_reference_analysis(envelope, context(tmp_path))
    assert 'evidence_ref' not in envelope['payload']
    assert envelope[TRUSTED_EGRESS_CONTEXT_KEY] is identity


@pytest.mark.asyncio
async def test_same_source_different_entry_keys_admit_one_real_task(tmp_path, monkeypatch):
    from novelvideo.freezone.remix import reference_analysis_service as service
    (tmp_path / 'clip.mp4').write_bytes(b'video')
    calls = []
    class Backend:
        async def enqueue_project_task(self, ctx, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(task_state=SimpleNamespace(task_id='task-a'))
    monkeypatch.setattr(service, 'get_task_backend', lambda: Backend())
    monkeypatch.setattr(service, 'get_task_manager', lambda: SimpleNamespace(get_task_for_project=lambda *a, **k: None))
    first = await service.ensure_reference_analysis(context(tmp_path), {'media_path': 'clip.mp4'}, {}, 'manual')
    second = await service.ensure_reference_analysis(context(tmp_path), {'media_path': 'clip.mp4'}, {}, 'agent')
    assert first['operation_id'] == second['operation_id']
    assert first['task_id'] == second['task_id'] == 'task-a'
    assert len(calls) == 1
    assert calls[0]['task_type'] == 'remix_joint_reference_analysis'
    assert calls[0]['payload']['media_sha256'] == hashlib.sha256(b'video').hexdigest()
    assert first['analysis_ref'] is None


@pytest.mark.asyncio
async def test_source_rejects_escape_and_content_mismatch(tmp_path):
    from novelvideo.freezone.remix import reference_analysis_service as service
    (tmp_path / 'clip.mp4').write_bytes(b'video')
    with pytest.raises(ValueError, match='project'):
        await service.ensure_reference_analysis(context(tmp_path), {'media_path': '../outside.mp4'}, {}, 'key')
    with pytest.raises(ValueError, match='changed'):
        await service.ensure_reference_analysis(context(tmp_path), {'media_path': 'clip.mp4', 'media_hash': '0' * 64}, {}, 'key')


@pytest.mark.parametrize('asr', ['no_speech', 'failed'])
def test_joint_runner_preserves_visual_analysis_without_speech(tmp_path, monkeypatch, asr):
    from novelvideo.freezone.remix import reference_analysis_service as service
    from novelvideo.task_backend.runners import remix
    path = tmp_path / 'clip.mp4'
    path.write_bytes(b'video')
    digest = hashlib.sha256(b'video').hexdigest()
    monkeypatch.setattr(remix, 'build_reference_evidence', lambda path: {
        'media_hash': 'sha256:' + digest, 'time_base': '1/1000', 'duration_us': 1000000,
        'frame_count': 30, 'cuts': [], 'representative_frames': [{'time_us': 0, 'pts': 0, 'time_base': '1/1000'}]})
    def transcribe(path, language):
        if asr == 'failed':
            raise RuntimeError('provider unavailable')
        return {'media_hash': 'sha256:' + digest, 'language': 'auto', 'status': 'no_speech', 'words': [], 'transcript': ''}
    monkeypatch.setattr(remix, 'transcribe_media', transcribe)
    async def story(envelope, ctx):
        assert envelope['payload']['evidence_ref']
        assert envelope['payload'].get('transcript_ref') is None
        return {'video_story': {'summary': 'Visual structure'}, 'analyses': [], 'reference_events': [],
                'frame_urls': ['frame'], 'frame_times_us': [0], 'frame_count': 1}
    monkeypatch.setattr(service, 'analyze_story', story)
    result = remix.run_remix_joint_reference_analysis({'payload': {'media_path': 'clip.mp4', 'media_sha256': digest}}, context(tmp_path))
    assert result['video_story']['summary'] == 'Visual structure'
    assert result['transcript_status'] == asr
    assert result['analysis_ref'] and result['evidence_ref']
    if asr == 'failed':
        assert result['limitations']
    assert result['coverage']['visual_inspection'] == 'sampled_frames'
    assert result['coverage']['frame_times_us'] == [0]


def saved_receipt(tmp_path):
    from novelvideo.freezone.remix.store import RemixArtifactStore
    (tmp_path / 'clip.mp4').write_bytes(b'video')
    digest = 'sha256:' + hashlib.sha256(b'video').hexdigest()
    store = RemixArtifactStore(tmp_path, 'project-a')
    evidence = store.create('reference_evidence.v1', {'media_hash': digest, 'time_base': '1/1000', 'duration_us': 1000000, 'cuts': [], 'representative_frames': [{'time_us': 0, 'pts': 0}, {'time_us': 700000, 'pts': 700}]}, [])
    ref = {'artifact_id': evidence['id'], 'revision': 1}
    receipt = store.create('reference_analysis.v1', {'media_path': 'clip.mp4', 'media_hash': digest, 'evidence_ref': ref,
        'transcript_status': 'no_speech', 'story': {'reference_events': [{'start_us': 0, 'end_us': 1000000}]},
        'coverage': {'visual_inspection': 'sampled_frames'}}, [ref])
    return {'artifact_id': receipt['id'], 'revision': 1}


def test_evidence_reader_ranges_paginates_and_rejects_stale_source(tmp_path):
    from novelvideo.freezone.remix import reference_analysis_service as service
    ref = saved_receipt(tmp_path)
    first = service.read_reference_evidence(context(tmp_path), ref, (0, 1000000), None, limit=1)
    assert len(first['items']) == 1
    assert first['next_cursor']
    second = service.read_reference_evidence(context(tmp_path), ref, (0, 1000000), first['next_cursor'], limit=1)
    assert first['items'] != second['items']
    clipped = service.read_reference_evidence(context(tmp_path), ref, (0, 900000), None, limit=1)
    explicit = service.read_reference_evidence(context(tmp_path), ref, (0, 900000), clipped['next_cursor'], limit=1)
    inferred = service.read_reference_evidence(context(tmp_path), ref, None, clipped['next_cursor'], limit=1)
    assert inferred['range_us'] == [0, 900000]
    assert inferred['items'] == explicit['items']
    only_later = service.read_reference_evidence(context(tmp_path), ref, (600000, 900000), None)
    assert all(item.get('time_us', item.get('end_us', 0)) >= 600000 for item in only_later['items'])
    with pytest.raises(ValueError, match='cursor'):
        service.read_reference_evidence(context(tmp_path), ref, (600000, 900000), first['next_cursor'])
    with pytest.raises(ValueError, match='limit'):
        service.read_reference_evidence(context(tmp_path), ref, (0, 1000000), None, limit=1001)
    (tmp_path / 'clip.mp4').write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed'):
        service.read_reference_evidence(context(tmp_path), ref, (0, 1000000), None)


def test_joint_runner_skips_asr_only_when_probe_proves_no_audio(tmp_path, monkeypatch):
    from novelvideo.freezone.remix import reference_analysis_service as service
    from novelvideo.task_backend.runners import remix
    (tmp_path / 'clip.mp4').write_bytes(b'video')
    digest = hashlib.sha256(b'video').hexdigest()
    monkeypatch.setattr(remix, 'build_reference_evidence', lambda path: {'media_hash': 'sha256:' + digest,
        'time_base': '1/1000', 'duration_us': 1000000, 'cuts': [], 'representative_frames': []})
    monkeypatch.setattr(service, 'probe_audio_track', lambda path: False)
    monkeypatch.setattr(remix, 'transcribe_media', lambda *a: pytest.fail('no audio must skip ASR'))
    async def story(*args):
        return {'video_story': {'summary': 'Silent visuals'}, 'frame_times_us': [], 'frame_urls': []}
    monkeypatch.setattr(service, 'analyze_story', story)
    result = remix.run_remix_joint_reference_analysis({'payload': {'media_path': 'clip.mp4', 'media_sha256': digest}}, context(tmp_path))
    assert result['transcript_status'] == 'no_audio'
    assert result['coverage']['speech_recognition'] == 'skipped_no_audio'
    assert result['blueprint_ref']


def test_joint_runner_blueprint_pins_real_observed_frames_and_speech(tmp_path, monkeypatch):
    from novelvideo.freezone.remix import reference_analysis_service as service
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.task_backend.runners import remix
    (tmp_path / 'clip.mp4').write_bytes(b'video'); digest = hashlib.sha256(b'video').hexdigest()
    monkeypatch.setattr(service, 'probe_audio_track', lambda path: True)
    monkeypatch.setattr(remix, 'build_reference_evidence', lambda path: {'media_hash': 'sha256:' + digest,
        'time_base': '1/1000', 'duration_us': 1000000, 'cuts': [], 'representative_frames': []})
    monkeypatch.setattr(remix, 'transcribe_media', lambda *a: {'media_hash': 'sha256:' + digest, 'language': 'auto', 'status': 'completed', 'transcript': 'Hi', 'words': [{'text': 'Hi', 'start_us': 0, 'end_us': 100000, 'start_char': 0, 'end_char': 2}]})
    async def story(*args):
        return {'video_story': {'summary': 'Introduction'}, 'frame_times_us': [500000], 'frame_urls': ['image'],
                'reference_events': [{'start_us': 0, 'end_us': 1000000, 'observed_frame': {'time_us': 500000, 'url': 'image'},
                    'spoken_range': {'start_us': 0, 'end_us': 100000, 'text': 'Hi', 'start_char': 0, 'end_char': 2},
                    'visual_observation': 'Product card', 'narrative_inference': 'Introduction'}]}
    monkeypatch.setattr(service, 'analyze_story', story)
    result = remix.run_remix_joint_reference_analysis({'payload': {'media_path': 'clip.mp4', 'media_sha256': digest}}, context(tmp_path))
    blueprint = RemixArtifactStore(tmp_path, 'project-a').read(**result['blueprint_ref'])['payload']
    assert blueprint['events'][0]['observed_frame']['evidence_ref'] == result['evidence_ref']
    assert blueprint['events'][0]['spoken_range']['transcript_ref'] == result['transcript_ref']
    assert blueprint['events'][0]['user_override']['review_state'] == 'needs_review'


@pytest.mark.asyncio
async def test_runner_publication_is_not_exposed_until_bound_task_receipt_completes(tmp_path, monkeypatch):
    from novelvideo.freezone.remix import reference_analysis_service as service
    from novelvideo.freezone.remix.operations import RemixOperationStore
    ref = saved_receipt(tmp_path)
    operations = RemixOperationStore(tmp_path, 'project-a')
    operation = operations.start(idempotency_key='proof', input_refs=[], producer_version='remix-process.v1',
       normalized_parameters={'action': 'joint_reference_analysis', 'payload': {'media_path': 'clip.mp4'}, 'media_sha256': hashlib.sha256(b'video').hexdigest()}, node_revision=1)
    fence = operations.claim_submission(operation['operation_id'], owner='test')
    operations.bind_task(operation['operation_id'], task_id='bound-task', owner='test', fencing_token=fence)
    operations.publish_result_unverified(operation['operation_id'], output_refs=[ref])
    task = SimpleNamespace(task_id='bound-task', status='running', error=None, result={'analysis_ref': ref})
    monkeypatch.setattr(service, 'get_task_manager', lambda: SimpleNamespace(get_task_for_project=lambda *a, **k: task))
    result = await service.get_reference_analysis(context(tmp_path), operation['operation_id'])
    assert result['analysis_ref'] is None
    task.status = 'completed'
    task.result = {'analysis_ref': {'artifact_id': 'other', 'revision': 1}}
    result = await service.get_reference_analysis(context(tmp_path), operation['operation_id'])
    assert result['analysis_ref'] is None
    task.result = {'analysis_ref': ref}
    result = await service.get_reference_analysis(context(tmp_path), operation['operation_id'])
    assert result['status'] == 'completed' and result['analysis_ref'] == ref


def test_targeted_range_extracts_real_cached_frames(tmp_path):
    import shutil
    import subprocess
    from novelvideo.freezone.remix import reference_analysis_service as service
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg unavailable')
    path = tmp_path / 'source.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=64x64:r=10', '-t', '1', '-pix_fmt', 'yuv420p', str(path)], check=True)
    first = service.extract_range_frames(context(tmp_path), 'source.mp4', (200000, 600000))
    assert [row['time_us'] for row in first] == [200000, 400000, 500000]
    assert all((tmp_path / row['media_path']).is_file() for row in first)
    assert first == service.extract_range_frames(context(tmp_path), 'source.mp4', (200000, 600000))


def test_source_hash_cache_invalidates_when_media_changes(tmp_path, monkeypatch):
    from novelvideo.freezone.remix import reference_analysis_service as service
    path = tmp_path / 'clip.mp4'; path.write_bytes(b'first')
    calls = []
    original = hashlib.file_digest
    def digest(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(service.hashlib, 'file_digest', digest)
    first = service.source_snapshot(context(tmp_path), {'media_path': 'clip.mp4'})
    assert service.source_snapshot(context(tmp_path), {'media_path': 'clip.mp4'}) == first
    assert len(calls) == 1
    path.write_bytes(b'changed')
    second = service.source_snapshot(context(tmp_path), {'media_path': 'clip.mp4'})
    assert second['media_sha256'] != first['media_sha256']
    assert len(calls) == 2


def saved_transcript(tmp_path, *, media=b'video', status='completed'):
    from novelvideo.freezone.remix.store import RemixArtifactStore
    payload = {'media_hash': 'sha256:' + hashlib.sha256(media).hexdigest(), 'language': 'zh',
               'status': status, 'transcript': '校字' if status == 'completed' else '',
               'words': [{'text': '校字', 'start_us': 123456, 'end_us': 765432,
                          'start_char': 0, 'end_char': 2}] if status == 'completed' else []}
    artifact = RemixArtifactStore(tmp_path, 'project-a').create('word_timeline.v1', payload, [])
    return {'artifact_id': artifact['id'], 'revision': artifact['revision']}


@pytest.mark.asyncio
async def test_exact_transcript_revision_changes_work_identity_and_is_saved_as_input(tmp_path, monkeypatch):
    from novelvideo.freezone.remix import reference_analysis_service as service
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.freezone.remix.operations import RemixOperationStore
    (tmp_path / 'clip.mp4').write_bytes(b'video')
    ref = saved_transcript(tmp_path)
    store = RemixArtifactStore(tmp_path, 'project-a')
    original = store.read(**ref)
    changed = store.revise(ref['artifact_id'], 1, {**original['payload'], 'transcript': '修订',
        'words': [{**original['payload']['words'][0], 'text': '修订'}]}, [])
    revision = {'artifact_id': changed['id'], 'revision': 2}
    calls = []
    class Backend:
        async def enqueue_project_task(self, ctx, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(task_state=SimpleNamespace(task_id=f'task-{len(calls)}'))
    monkeypatch.setattr(service, 'get_task_backend', lambda: Backend())
    monkeypatch.setattr(service, 'get_task_manager', lambda: SimpleNamespace(get_task_for_project=lambda *a, **k: None))
    first = await service.ensure_reference_analysis(context(tmp_path), {'media_path': 'clip.mp4', 'transcript_ref': ref})
    replay = await service.ensure_reference_analysis(context(tmp_path), {'media_path': 'clip.mp4', 'transcript_ref': ref}, idempotency_key='agent')
    corrected = await service.ensure_reference_analysis(context(tmp_path), {'media_path': 'clip.mp4', 'transcript_ref': revision})
    assert first['operation_id'] == replay['operation_id'] != corrected['operation_id']
    assert len(calls) == 2
    operation = RemixOperationStore(tmp_path, 'project-a').get(first['operation_id'])
    assert operation['normalized_parameters']['payload']['transcript_ref'] == ref
    assert operation['input_refs'] == [{**ref, 'project_id': 'project-a'}]
    assert calls[0]['payload']['transcript_ref'] == ref


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['media', 'schema', 'project', 'status', 'missing', 'revision'])
async def test_invalid_connected_transcript_is_rejected_before_enqueue(tmp_path, monkeypatch, invalid):
    from novelvideo.freezone.remix import reference_analysis_service as service
    (tmp_path / 'clip.mp4').write_bytes(b'video')
    ref = saved_transcript(tmp_path, media=b'other' if invalid == 'media' else b'video',
                           status='no_speech' if invalid == 'status' else 'completed')
    if invalid == 'schema':
        ref = saved_receipt(tmp_path)
    if invalid == 'project':
        ref['project_id'] = 'project-b'
    if invalid == 'missing':
        ref['artifact_id'] = 'missing'
    if invalid == 'revision':
        ref['revision'] = True
    monkeypatch.setattr(service, 'get_task_backend', lambda: pytest.fail('invalid transcript must not enqueue'))
    with pytest.raises((ValueError, FileNotFoundError)):
        await service.ensure_reference_analysis(context(tmp_path), {'media_path': 'clip.mp4', 'transcript_ref': ref})


def test_joint_runner_uses_actual_corrected_artifact_and_skips_asr(tmp_path, monkeypatch):
    from novelvideo.freezone.remix import reference_analysis_service as service
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.task_backend.runners import remix
    (tmp_path / 'clip.mp4').write_bytes(b'video')
    digest = hashlib.sha256(b'video').hexdigest()
    ref = saved_transcript(tmp_path)
    monkeypatch.setattr(remix, 'build_reference_evidence', lambda _: {'media_hash': 'sha256:' + digest,
        'time_base': '1/1000', 'duration_us': 1000000, 'cuts': [], 'representative_frames': []})
    monkeypatch.setattr(remix, 'transcribe_media', lambda *a: pytest.fail('connected corrected artifact must skip ASR'))
    monkeypatch.setattr(service, 'probe_audio_track', lambda _: True)
    async def story(envelope, ctx):
        assert envelope['payload']['transcript_ref'] == ref
        actual = RemixArtifactStore(tmp_path, 'project-a').read(**envelope['payload']['transcript_ref'])
        assert actual['payload']['transcript'] == '校字'
        assert actual['payload']['words'][0]['start_us'] == 123456
        return {'video_story': {'summary': actual['payload']['transcript']}, 'frame_times_us': []}
    monkeypatch.setattr(service, 'analyze_story', story)
    result = remix.run_remix_joint_reference_analysis({'payload': {'media_path': 'clip.mp4',
        'media_sha256': digest, 'transcript_ref': ref}}, context(tmp_path))
    assert result['transcript_ref'] == ref and result['video_story']['summary'] == '校字'
    assert result['coverage']['speech_recognition'] == 'reused_transcript'
    for key in ('analysis_ref', 'blueprint_ref'):
        assert {**ref, 'project_id': 'project-a'} in RemixArtifactStore(tmp_path, 'project-a').read(**result[key])['upstream_refs']


def test_joint_runner_reuses_independent_no_speech_evidence_without_second_asr(tmp_path, monkeypatch):
    from novelvideo.freezone.remix import reference_analysis_service as service
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.task_backend.runners import remix
    (tmp_path / 'clip.mp4').write_bytes(b'video')
    digest = hashlib.sha256(b'video').hexdigest()
    saved = RemixArtifactStore(tmp_path, 'project-a').create('word_timeline.v1', {
        'media_hash': 'sha256:' + digest, 'language': 'auto', 'status': 'no_speech',
        'transcript': '', 'words': []}, [])
    ref = {'artifact_id': saved['id'], 'revision': saved['revision']}
    monkeypatch.setattr(remix, 'build_reference_evidence', lambda _: {'media_hash': 'sha256:' + digest,
        'time_base': '1/1000', 'duration_us': 1000000, 'cuts': [], 'representative_frames': []})
    monkeypatch.setattr(remix, 'transcribe_media', lambda *a: pytest.fail('saved no-speech evidence must skip ASR'))
    async def story(envelope, ctx):
        assert envelope['payload']['transcript_ref'] is None
        return {'video_story': {'summary': 'Visual structure'}, 'frame_times_us': []}
    monkeypatch.setattr(service, 'analyze_story', story)
    result = remix.run_remix_joint_reference_analysis({'payload': {'media_path': 'clip.mp4',
        'media_sha256': digest, 'no_speech_ref': ref}}, context(tmp_path))
    assert result['transcript_status'] == 'no_speech'
    assert result['coverage']['speech_recognition'] == 'reused_no_speech'
    assert result['transcript_ref'] == ref


def test_joint_runner_revalidates_transcript_source_before_vision(tmp_path, monkeypatch):
    from novelvideo.task_backend.runners import remix
    (tmp_path / 'clip.mp4').write_bytes(b'video')
    ref = saved_transcript(tmp_path, media=b'other')
    monkeypatch.setattr(remix, 'build_reference_evidence', lambda _: pytest.fail('invalid input must fail before analysis'))
    with pytest.raises(ValueError, match='source'):
        remix.run_remix_joint_reference_analysis({'payload': {'media_path': 'clip.mp4',
            'media_sha256': hashlib.sha256(b'video').hexdigest(), 'transcript_ref': ref}}, context(tmp_path))


@pytest.mark.asyncio
async def test_completed_receipt_cannot_expose_different_transcript_than_admitted(tmp_path, monkeypatch):
    from novelvideo.freezone.remix import reference_analysis_service as service
    from novelvideo.freezone.remix.operations import RemixOperationStore
    receipt = saved_receipt(tmp_path)
    transcript = saved_transcript(tmp_path)
    operations = RemixOperationStore(tmp_path, 'project-a')
    operation = operations.start(idempotency_key='pinned-input', input_refs=[transcript],
        producer_version='remix-process.v1', normalized_parameters={'action': 'joint_reference_analysis',
        'payload': {'media_path': 'clip.mp4', 'transcript_ref': transcript},
        'media_sha256': hashlib.sha256(b'video').hexdigest()}, node_revision=1)
    fence = operations.claim_submission(operation['operation_id'], owner='test')
    operations.bind_task(operation['operation_id'], task_id='bound-task', owner='test', fencing_token=fence)
    operations.publish_result_unverified(operation['operation_id'], output_refs=[receipt])
    task = SimpleNamespace(task_id='bound-task', status='completed', result={'analysis_ref': receipt})
    monkeypatch.setattr(service, 'get_task_manager', lambda: SimpleNamespace(get_task_for_project=lambda *a, **k: task))
    with pytest.raises(ValueError, match='transcript revision'):
        await service.get_reference_analysis(context(tmp_path), operation['operation_id'])


@pytest.mark.asyncio
async def test_replaying_original_standalone_admission_returns_its_one_real_task(tmp_path, monkeypatch):
    from novelvideo.api.routes import remix as routes
    from novelvideo.freezone.remix.store import RemixArtifactStore
    (tmp_path / 'clip.mp4').write_bytes(b'video')
    ctx = context(tmp_path)
    async def store(*args):
        return RemixArtifactStore(tmp_path, ctx.project_id)
    async def resolve(**kwargs):
        return ctx
    calls = []
    class Backend:
        async def enqueue_project_task(self, ctx, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(task_state=SimpleNamespace(task_id='original-asr'))
    monkeypatch.setattr(routes, '_store', store)
    monkeypatch.setattr(routes, '_transcription_engine_ready', lambda: True)
    monkeypatch.setattr(routes, 'resolve_project_context', resolve)
    monkeypatch.setattr(routes, 'get_task_backend', lambda: Backend())
    request = routes.ProcessActionBody(action='transcribe_media', payload={'media_path': 'clip.mp4', 'language': 'auto'},
        idempotency_key='original-standalone-key', node_revision=3)
    first = await routes.enqueue_process_action('project-a', request, {'id': 'alice'})
    replay = await routes.enqueue_process_action('project-a', request, {'id': 'alice'})
    assert first['data']['operation_id'] == replay['data']['operation_id']
    assert first['data']['task_id'] == replay['data']['task_id'] == 'original-asr'
    assert len(calls) == 1
