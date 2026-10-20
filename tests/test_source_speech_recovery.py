from pathlib import Path

import httpx
import pytest
import sqlite3

from test_source_speech_api import setup_candidate
from test_source_speech import resource, SCOPE
from novelvideo.freezone.canvas_lock import CanvasLockBusy
from novelvideo.ports.canvas_mutex import CanvasLeaseLost


def bound_candidate(tmp_path, monkeypatch):
    import novelvideo.freezone.remix.source_speech as speech
    from novelvideo.freezone.remix.operations import RemixOperationStore
    ctx, canvas, store, ref, media = setup_candidate(tmp_path, monkeypatch)
    original = tmp_path / media['media_path']
    original.rename(tmp_path / 'candidate.wav')
    media['media_path'] = 'candidate.wav'
    monkeypatch.setattr('novelvideo.freezone.canvas_store.read_canvas', lambda *args: canvas)
    monkeypatch.setattr(speech, 'configured_provider_signature', lambda: 'p')
    prepared = speech.prepare_material_speech(store, ctx, canvas, canvas_id='canvas', work_ref=ref,
        step_id='video', node_id='n', language='en')
    operations = RemixOperationStore(tmp_path, 'project')
    op = operations.start(idempotency_key='recover', input_refs=prepared['input_refs'],
        producer_version=prepared['producer_version'], normalized_parameters=prepared['normalized_parameters'], node_revision=1)
    fence = operations.claim_submission(op['operation_id'], owner='submit')
    operations.bind_task(op['operation_id'], task_id='native-task', owner='submit', fencing_token=fence)
    envelope = {'__run_task_id': 'native-task', 'payload': {**prepared['task_payload'], 'operation_id': op['operation_id']}}
    return ctx, canvas, store, operations, op['operation_id'], envelope


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_point', ['artifact', 'operation', 'cache_publication'])
async def test_registered_runner_publication_failure_recovers_through_api_without_provider_retry(tmp_path, monkeypatch, failure_point):
    from novelvideo.freezone.remix.source_speech import SourceSpeechPending
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.freezone.remix.operations import RemixOperationStore
    import novelvideo.freezone.remix.alignment as alignment
    import novelvideo.api.routes.remix as route
    from novelvideo.task_backend.registry import get_project_task_runner
    import novelvideo.task_backend.runners.remix
    ctx, canvas, store, operations, operation_id, envelope = bound_candidate(tmp_path, monkeypatch)
    calls = []
    class Engine:
        def transcribe(self, *args):
            calls.append(1)
            return {'status': 'no_speech', 'words': []}
    monkeypatch.setattr(alignment, 'configured_newapi_transcriber', lambda **kwargs: Engine())
    if failure_point == 'artifact':
        original = RemixArtifactStore._publish
        def crash(self, **kwargs):
            result = original(self, **kwargs)
            monkeypatch.setattr(RemixArtifactStore, '_publish', original)
            raise OSError('artifact notification failed after durable publication')
        monkeypatch.setattr(RemixArtifactStore, '_publish', crash)
    elif failure_point == 'operation':
        original = RemixOperationStore.publish_result_unverified
        def crash(self, *args, **kwargs):
            monkeypatch.setattr(RemixOperationStore, 'publish_result_unverified', original)
            raise OSError('operation publication unavailable')
        monkeypatch.setattr(RemixOperationStore, 'publish_result_unverified', crash)
    else:
        import novelvideo.freezone.remix.source_speech as speech
        original = speech._connection
        failed = []
        class InterruptedConnection:
            def __init__(self, conn): self.conn = conn
            def __getattr__(self, name): return getattr(self.conn, name)
            def __enter__(self): self.conn.__enter__(); return self
            def __exit__(self, *args): return self.conn.__exit__(*args)
            def execute(self, sql, *args):
                if sql == 'BEGIN IMMEDIATE' and not failed:
                    failed.append(True)
                    raise sqlite3.OperationalError('durable result publication busy')
                return self.conn.execute(sql, *args)
        monkeypatch.setattr(speech, '_connection', lambda *args: InterruptedConnection(original(*args)))
    with pytest.raises((OSError, SourceSpeechPending, sqlite3.OperationalError)):
        get_project_task_runner('remix_analyze_material_speech')(envelope, ctx)
    assert operations.get(operation_id)['status'] == 'pending_reconciliation'
    async def get_store(*args): return store
    async def get_ctx(**kwargs): return ctx
    async def authorize(*args): return None
    monkeypatch.setattr(route, '_store', get_store)
    async def local_admission(*_args, **_kwargs):
        return None
    monkeypatch.setattr(route, '_request_transcription_context', local_admission)
    monkeypatch.setattr(route, 'resolve_project_context', get_ctx)
    monkeypatch.setattr(route, '_authorize_material_speech_operation', authorize)
    recovered = await route.reconcile_operation('project', operation_id, {})
    assert recovered['data']['status'] == 'needs_confirmation'
    assert len(recovered['data']['output_refs']) == 1
    assert calls == [1]


def test_real_httpx_timeout_preserves_native_operation_and_never_retries(tmp_path, monkeypatch):
    import novelvideo.freezone.remix.alignment as alignment
    from novelvideo.freezone.remix.source_speech import SourceSpeechPending, reconcile_material_speech
    from novelvideo.freezone.remix.newapi_transcription import NewApiSpeechTranscriber
    from novelvideo.task_backend.registry import get_project_task_runner
    import novelvideo.task_backend.runners.remix
    ctx, canvas, store, operations, operation_id, envelope = bound_candidate(tmp_path, monkeypatch)
    calls = []
    def timed_out(request):
        calls.append(request.url)
        raise httpx.ReadTimeout('response unknown', request=request)
    engine = NewApiSpeechTranscriber(api_key='fake', base_url='https://gateway.invalid',
        transport=httpx.MockTransport(timed_out), allow_segments=True)
    monkeypatch.setattr(alignment, 'configured_newapi_transcriber', lambda **kwargs: engine)
    runner = get_project_task_runner('remix_analyze_material_speech')
    with pytest.raises(SourceSpeechPending): runner(envelope, ctx)
    op = operations.get(operation_id)
    assert op['status'] == 'pending_reconciliation'
    assert op['provider_task_id'] == 'native-task'
    assert reconcile_material_speech(ctx, op) is None
    with pytest.raises(SourceSpeechPending): runner(envelope, ctx)
    assert len(calls) == 1


@pytest.mark.parametrize('api_key,base_url', [('', 'https://gateway.invalid'), ('fake', 'not-a-gateway')])
def test_invalid_configuration_can_be_repaired_before_dispatch(tmp_path, monkeypatch, api_key, base_url):
    import novelvideo.freezone.remix.alignment as alignment
    from novelvideo.freezone.remix.source_speech import observe_source_speech
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.freezone.remix.newapi_transcription import NewApiSpeechTranscriber
    store = RemixArtifactStore(tmp_path, 'project')
    media = resource(tmp_path)
    calls = []
    monkeypatch.setattr(alignment, 'configured_newapi_transcriber', lambda **kwargs:
        NewApiSpeechTranscriber(api_key=api_key, base_url=base_url))
    kwargs = dict(scope=SCOPE, resource=media, language='en', provider_signature='same-config')
    with pytest.raises(RuntimeError, match='configured'): observe_source_speech(store, **kwargs)
    class Engine:
        def transcribe(self, *args):
            calls.append(1)
            return {'status': 'no_speech', 'words': []}
    monkeypatch.setattr(alignment, 'configured_newapi_transcriber', lambda **kwargs: Engine())
    assert observe_source_speech(store, **kwargs)['revision'] == 1
    assert len(calls) == 1


def test_proven_local_extraction_failure_can_retry_without_dispatch(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from novelvideo.freezone.remix.source_speech import observe_source_speech
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.freezone.remix.newapi_transcription import NewApiSpeechTranscriber
    import novelvideo.freezone.remix.newapi_transcription as adapter
    media = resource(tmp_path)
    (tmp_path / media['media_path']).rename(tmp_path / 'candidate.mp4')
    media['media_path'] = 'candidate.mp4'
    calls = []
    def responded(request):
        calls.append(1)
        return httpx.Response(200, json={'text': '', 'words': []})
    engine = NewApiSpeechTranscriber(api_key='fake', base_url='https://gateway.invalid',
        transport=httpx.MockTransport(responded), allow_segments=True)
    monkeypatch.setattr(adapter.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=1))
    store = RemixArtifactStore(tmp_path, 'project')
    kwargs = dict(scope=SCOPE, resource=media, language='en', provider_signature='p', aligner=engine)
    with pytest.raises(RuntimeError, match='extract'): observe_source_speech(store, **kwargs)
    assert not calls
    def extracted(command, **kwargs):
        Path(command[-1]).write_bytes(b'full audio')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(adapter.subprocess, 'run', extracted)
    assert observe_source_speech(store, **kwargs)['revision'] == 1
    assert calls == [1]


def test_request_oserror_after_video_extraction_is_never_pre_dispatch(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from novelvideo.freezone.remix.source_speech import observe_source_speech, SourceSpeechPending
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.freezone.remix.newapi_transcription import NewApiSpeechTranscriber
    import novelvideo.freezone.remix.newapi_transcription as adapter
    media = resource(tmp_path)
    (tmp_path / media['media_path']).rename(tmp_path / 'candidate.mp4')
    media['media_path'] = 'candidate.mp4'
    calls = []
    def extracted(command, **kwargs):
        Path(command[-1]).write_bytes(b'full audio')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(adapter.subprocess, 'run', extracted)
    engine = NewApiSpeechTranscriber(api_key='fake', base_url='https://gateway.invalid')
    def uncertain(*args):
        calls.append(1)
        raise OSError('request transport died after dispatch')
    monkeypatch.setattr(engine, '_request', uncertain)
    store = RemixArtifactStore(tmp_path, 'project')
    kwargs = dict(scope=SCOPE, resource=media, language='en', provider_signature='p', aligner=engine)
    with pytest.raises(SourceSpeechPending): observe_source_speech(store, **kwargs)
    with pytest.raises(SourceSpeechPending): observe_source_speech(store, **kwargs)
    assert calls == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['artifact', 'operation'])
@pytest.mark.parametrize('failure_type', [CanvasLockBusy, CanvasLeaseLost])
async def test_native_publication_mutex_failure_recovers_same_operation(tmp_path, monkeypatch, boundary, failure_type):
    from contextlib import contextmanager
    import novelvideo.api.routes.remix as route
    import novelvideo.freezone.remix.alignment as alignment
    from novelvideo.freezone.remix.source_speech import SourceSpeechPending
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.task_backend.registry import get_project_task_runner
    import novelvideo.task_backend.runners.remix
    ctx, canvas, store, operations, operation_id, envelope = bound_candidate(tmp_path, monkeypatch)
    calls = []
    class Engine:
        def transcribe(self, *args):
            calls.append(1)
            return {'status': 'no_speech', 'words': []}
    monkeypatch.setattr(alignment, 'configured_newapi_transcriber', lambda **kwargs: Engine())
    publisher = RemixArtifactStore if boundary == 'artifact' else RemixOperationStore
    original = publisher._locked
    @contextmanager
    def interrupted(self):
        if boundary == 'artifact' and self.producer_version != 'remix-source-speech.v1':
            with original(self) as guard:
                yield guard
            return
        # Both failures occur only after recognition is durably saved.
        with sqlite3.connect(tmp_path / 'freezone/_source_speech.sqlite3') as conn:
            assert conn.execute('SELECT result_json FROM observations').fetchone()[0]
        monkeypatch.setattr(publisher, '_locked', original)
        if failure_type is CanvasLockBusy:
            raise CanvasLockBusy('remix_' + boundary)
        with original(self):
            class LostGuard:
                def reassert(self): raise CanvasLeaseLost('remix_' + boundary)
            yield LostGuard()
    monkeypatch.setattr(publisher, '_locked', interrupted)
    with pytest.raises(SourceSpeechPending):
        get_project_task_runner('remix_analyze_material_speech')(envelope, ctx)
    assert operations.get(operation_id)['status'] == 'pending_reconciliation'
    async def get_store(*args): return store
    async def get_ctx(**kwargs): return ctx
    async def authorize(*args): return None
    monkeypatch.setattr(route, '_store', get_store)
    async def local_admission(*_args, **_kwargs):
        return None
    monkeypatch.setattr(route, '_request_transcription_context', local_admission)
    monkeypatch.setattr(route, 'resolve_project_context', get_ctx)
    monkeypatch.setattr(route, '_authorize_material_speech_operation', authorize)
    recovered = await route.reconcile_operation('project', operation_id, {})
    assert recovered['data']['operation_id'] == operation_id
    assert recovered['data']['status'] == 'needs_confirmation'
    assert len(recovered['data']['output_refs']) == 1
    assert calls == [1]


@pytest.mark.parametrize('boundary', ['artifact', 'operation'])
def test_arbitrary_publication_runtime_error_stays_definitive(tmp_path, monkeypatch, boundary):
    import novelvideo.freezone.remix.alignment as alignment
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.task_backend.registry import get_project_task_runner
    import novelvideo.task_backend.runners.remix
    ctx, canvas, store, operations, operation_id, envelope = bound_candidate(tmp_path, monkeypatch)
    calls = []
    class Engine:
        def transcribe(self, *args):
            calls.append(1)
            return {'status': 'no_speech', 'words': []}
    monkeypatch.setattr(alignment, 'configured_newapi_transcriber', lambda **kwargs: Engine())
    def broken(*args, **kwargs): raise RuntimeError('definitive publication invariant violated')
    if boundary == 'artifact': monkeypatch.setattr(RemixArtifactStore, '_publish', broken)
    else: monkeypatch.setattr(RemixOperationStore, 'publish_result_unverified', broken)
    with pytest.raises(RuntimeError, match='definitive publication invariant'):
        get_project_task_runner('remix_analyze_material_speech')(envelope, ctx)
    assert operations.get(operation_id)['status'] == 'failed'
    assert calls == [1]
