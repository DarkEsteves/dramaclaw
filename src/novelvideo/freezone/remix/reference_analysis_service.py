"""Shared project-bound reference analysis admission and evidence reads.

The content identity, queue lease and completed task receipt belong to the server;
manual canvas actions and agent tools use the same operation.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from functools import lru_cache
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from novelvideo.ports import get_task_backend
from novelvideo.task_state import get_task_manager
from novelvideo.task_identity import project_task_state_key
from novelvideo.project_context import require_project_home_node

TASK_TYPE = 'remix_joint_reference_analysis'


class ReferenceAnalysisV1(BaseModel):
    model_config = ConfigDict(extra='allow', strict=True)
    media_path: str = Field(min_length=1)
    media_hash: str = Field(pattern=r'^sha256:[a-f0-9]{64}$')
    evidence_ref: dict[str, Any]
    transcript_ref: dict[str, Any] | None = None
    blueprint_ref: dict[str, Any] | None = None
    transcript_status: Literal['completed', 'no_speech', 'no_audio', 'failed']
    story: dict[str, Any]
    coverage: dict[str, Any]
    limitations: list[str] = Field(default_factory=list)
    visual_system: dict[str, Any] = Field(default_factory=dict)
    sound_roles: list[dict[str, Any]] = Field(default_factory=list)
    semantic_triggers: list[dict[str, Any]] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)



@lru_cache(maxsize=128)
def _source_digest(path: str, identity: tuple[int, ...]) -> str:
    with Path(path).open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    current = Path(path).stat()
    if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns) != identity:
        raise ValueError('reference media changed while reading')
    return digest

def source_snapshot(ctx, source_ref: dict[str, Any]) -> dict[str, str]:
    require_project_home_node(ctx, operation='reference analysis')
    if source_ref.get('project_id', ctx.project_id) != ctx.project_id:
        raise ValueError('reference media belongs to another project')
    relative = source_ref.get('media_path')
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError('reference media must be project relative')
    root = Path(ctx.output_dir).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError('reference media must belong to this project')
    if not path.is_file():
        raise FileNotFoundError('reference media is unavailable')
    stat = path.stat()
    digest = _source_digest(str(path), (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    expected = source_ref.get('media_hash')
    if expected is not None and str(expected).removeprefix('sha256:') != digest:
        raise ValueError('reference media changed since submission')
    return {'media_path': path.relative_to(root).as_posix(), 'media_sha256': digest}


def validated_transcript_ref(ctx, reference: dict[str, Any], media_sha256: str) -> dict[str, Any]:
    """Resolve the exact project artifact against the actual video content."""
    from .store import RemixArtifactStore
    if not isinstance(reference, dict):
        raise ValueError('transcript_ref must name an immutable artifact revision')
    if reference.get('project_id', ctx.project_id) != ctx.project_id:
        raise ValueError('reference transcript belongs to another project')
    artifact = RemixArtifactStore(Path(ctx.output_dir), ctx.project_id).read(
        reference.get('artifact_id'), reference.get('revision'))
    if artifact['schema'] != 'word_timeline.v1':
        raise ValueError('reference transcript must be word_timeline.v1')
    timeline = artifact['payload']
    if timeline.get('media_hash') != 'sha256:' + media_sha256:
        raise ValueError('reference transcript source does not match')
    if timeline.get('status') != 'completed' or not timeline.get('words'):
        raise ValueError('reference transcript requires completed actual words')
    return {'artifact_id': artifact['id'], 'revision': artifact['revision']}


def validated_no_speech_ref(ctx, reference: dict[str, Any], media_sha256: str) -> dict[str, Any]:
    """Pin an independent full-source ASR result that found no spoken words."""
    from .store import RemixArtifactStore
    if not isinstance(reference, dict) or reference.get('project_id', ctx.project_id) != ctx.project_id:
        raise ValueError('no-speech observation belongs to another project')
    artifact = RemixArtifactStore(Path(ctx.output_dir), ctx.project_id).read(
        reference.get('artifact_id'), reference.get('revision'))
    timeline = artifact['payload']
    if (artifact['schema'] != 'word_timeline.v1' or timeline.get('media_hash') != 'sha256:' + media_sha256
            or timeline.get('status') != 'no_speech' or timeline.get('words')):
        raise ValueError('no-speech observation must match the complete current source')
    return {'artifact_id': artifact['id'], 'revision': artifact['revision']}


async def analyze_story(envelope, ctx):
    # Reuse the real ffmpeg extraction + vision runner, including its egress and
    # cancellation wrappers; no second narrative engine is introduced.
    from novelvideo.task_backend.runners.freezone import run_freezone_video_story
    return await asyncio.to_thread(run_freezone_video_story, envelope, ctx)


async def get_reference_analysis(ctx, operation_id: str) -> dict[str, Any]:
    from .operations import RemixOperationStore
    from .store import RemixArtifactStore
    operations = RemixOperationStore(Path(ctx.output_dir), ctx.project_id)
    record = await asyncio.to_thread(operations.get, operation_id)
    if record.get('normalized_parameters', {}).get('action') != 'joint_reference_analysis':
        raise ValueError('operation is not reference analysis')
    parameters = record['normalized_parameters']
    await asyncio.to_thread(source_snapshot, ctx, {**parameters['payload'], 'media_hash': parameters['media_sha256']})
    result = {'operation_id': operation_id, 'task_id': record.get('task_id'),
              'task_type': TASK_TYPE, 'task_scope': operation_id,
              'task_key': project_task_state_key(TASK_TYPE, ctx.project_id, 0, scope=operation_id),
              'status': record['status'], 'analysis_ref': None, 'evidence_ref': None,
              'transcript_ref': None, 'blueprint_ref': None}
    task = await asyncio.to_thread(get_task_manager().get_task_for_project, ctx, TASK_TYPE, 0, beat_num=None, scope=operation_id)
    if task and not record.get('task_id') and record.get('submission_owner') and record.get('submission_fence'):
        record = await asyncio.to_thread(operations.bind_task, operation_id, task_id=task.task_id,
                                         owner=record['submission_owner'], fencing_token=record['submission_fence'])
        result['task_id'] = record['task_id']
    # Runner publication alone is not a completed task receipt.
    if not task or task.task_id != record.get('task_id'):
        return result
    if task.status in {'failed', 'cancelled'}:
        result.update(status=task.status, error=task.error)
        return result
    if task.status != 'completed' or record['status'] not in {'needs_confirmation', 'completed'}:
        return result
    ref = next((item for item in record.get('output_refs', []) if item.get('artifact_id')), None)
    if ref is None or (getattr(task, "result", None) or {}).get("analysis_ref") != ref:
        return result
    artifact = await asyncio.to_thread(RemixArtifactStore(Path(ctx.output_dir), ctx.project_id).read, ref['artifact_id'], ref['revision'])
    if artifact['schema'] != 'reference_analysis.v1' or artifact['payload']['media_hash'] != 'sha256:' + parameters['media_sha256']:
        raise ValueError('reference analysis source does not match')
    if parameters['payload'].get('transcript_ref') and artifact['payload'].get('transcript_ref') != parameters['payload']['transcript_ref']:
        raise ValueError('reference analysis transcript revision does not match')
    if parameters['payload'].get('no_speech_ref') and (artifact['payload'].get('transcript_ref') != parameters['payload']['no_speech_ref']
            or artifact['payload'].get('transcript_status') != 'no_speech'):
        raise ValueError('reference analysis no-speech observation does not match')
    result.update(status='completed', analysis_ref=ref, **{key: artifact['payload'].get(key) for key in
                  ('evidence_ref', 'transcript_ref', 'blueprint_ref', 'transcript_status', 'limitations', 'coverage')})
    return result


async def ensure_reference_analysis(ctx, source_ref: dict[str, Any], requested_evidence: dict[str, Any] | None = None,
                                    idempotency_key: str | None = None, *, on_reserved=None) -> dict[str, Any]:
    from .operations import RemixOperationStore
    from novelvideo.api.routes.remix import record_enqueue_exception
    if getattr(ctx, 'effective_role', 'editor') not in {'editor', 'owner', 'admin'}:
        raise PermissionError('reference analysis requires project editor access')
    requested_evidence = requested_evidence or {}
    if not isinstance(requested_evidence, dict) or set(requested_evidence) - {'range_us', 'cursor', 'limit'}:
        raise ValueError('requested_evidence supports range_us, cursor and limit')
    if 'range_us' in requested_evidence:
        bounds = requested_evidence['range_us']
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 2 or any(type(value) is not int for value in bounds) or not 0 <= bounds[0] < bounds[1]:
            raise ValueError('requested evidence range must contain increasing microseconds')
    snapshot = await asyncio.to_thread(source_snapshot, ctx, source_ref)
    parameters = {'action': 'joint_reference_analysis', 'payload': {'media_path': snapshot['media_path']},
                  'media_sha256': snapshot['media_sha256']}
    input_refs = []
    if source_ref.get('transcript_ref') is not None:
        ref = await asyncio.to_thread(validated_transcript_ref, ctx, source_ref['transcript_ref'], snapshot['media_sha256'])
        parameters['payload']['transcript_ref'] = ref
        input_refs.append(ref)
    if source_ref.get('no_speech_ref') is not None:
        if input_refs:
            raise ValueError('reference source cannot carry speech and no-speech evidence together')
        ref = await asyncio.to_thread(validated_no_speech_ref, ctx, source_ref['no_speech_ref'], snapshot['media_sha256'])
        parameters['payload']['no_speech_ref'] = ref
        input_refs.append(ref)
    # Caller keys are intentionally not the work identity: two authorized
    # entrances for the same source share one durable lease and task.
    key = 'joint-reference:' + hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()
    operations = RemixOperationStore(Path(ctx.output_dir), ctx.project_id)
    operation = await asyncio.to_thread(operations.start, idempotency_key=key, input_refs=input_refs,
                                      producer_version='remix-process.v1', normalized_parameters=parameters,
                                      node_revision=1, input_media_hashes=['sha256:' + snapshot['media_sha256']])
    if on_reserved is not None:
        await on_reserved(operation['operation_id'])
    if not operation.get('task_id') and operation['status'] == 'pending':
        owner = uuid.uuid4().hex
        fence = await asyncio.to_thread(operations.claim_submission, operation['operation_id'], owner=owner)
        if fence is not None:
            try:
                queued = await get_task_backend().enqueue_project_task(ctx, product_surface='freezone',
                    task_type=TASK_TYPE, queue_kind='default', episode=0, scope=operation['operation_id'],
                    payload={**snapshot, **parameters['payload'], 'operation_id': operation['operation_id'], 'job_id': operation['operation_id']})
            except Exception as exc:
                await record_enqueue_exception(operations, operation['operation_id'], owner=owner, fencing_token=fence, error=exc)
                raise
            await asyncio.to_thread(operations.bind_task, operation['operation_id'], task_id=queued.task_state.task_id,
                                    owner=owner, fencing_token=fence)
    receipt = await get_reference_analysis(ctx, operation['operation_id'])
    if requested_evidence and receipt.get('analysis_ref'):
        receipt['requested_evidence_page'] = await asyncio.to_thread(read_reference_evidence, ctx, receipt['analysis_ref'],
            requested_evidence.get('range_us'), requested_evidence.get('cursor'), limit=requested_evidence.get('limit', 50))
    return receipt


def read_reference_evidence(ctx, analysis_ref: dict[str, Any], range_us: tuple[int, int] | list[int] | None = None,
                            cursor: str | None = None, *, limit: int = 50) -> dict[str, Any]:
    """Read bounded time-local evidence with a cursor tied to source and range."""
    import base64
    from .store import RemixArtifactStore
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('evidence limit must be between 1 and 100')
    if analysis_ref.get('project_id', ctx.project_id) != ctx.project_id:
        raise ValueError('analysis belongs to another project')
    store = RemixArtifactStore(Path(ctx.output_dir), ctx.project_id)
    artifact = store.read(analysis_ref['artifact_id'], analysis_ref['revision'])
    if artifact['schema'] != 'reference_analysis.v1':
        raise ValueError('analysis_ref must name reference analysis')
    receipt = artifact['payload']
    source_snapshot(ctx, {'media_path': receipt['media_path'], 'media_hash': receipt['media_hash']})
    ref = receipt['evidence_ref']
    evidence = store.read(ref['artifact_id'], ref['revision'])['payload']
    if evidence.get('media_hash') != receipt['media_hash']:
        raise ValueError('reference evidence does not match source')
    duration = evidence['duration_us']
    decoded_cursor = None
    if cursor:
        if len(cursor) > 2048:
            raise ValueError('evidence cursor exceeds capacity')
        try:
            decoded_cursor = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if not isinstance(decoded_cursor, dict):
                raise ValueError('invalid cursor')
        except Exception as exc:
            raise ValueError('evidence cursor does not match this source and range') from exc
    if range_us is None:
        range_us = (decoded_cursor.get('range_us') if decoded_cursor else None) or (0, duration)
    if not isinstance(range_us, (tuple, list)) or len(range_us) != 2 or any(type(value) is not int for value in range_us):
        raise ValueError('evidence range must contain integer microseconds')
    start, end = range_us
    if not 0 <= start < end <= duration:
        raise ValueError('evidence range is outside the source duration')
    identity = hashlib.sha256(json.dumps([ctx.project_id, analysis_ref['artifact_id'], analysis_ref['revision'], start, end, receipt['media_hash']]).encode()).hexdigest()
    offset = 0
    if decoded_cursor is not None:
        try:
            if (decoded_cursor['identity'] != identity or type(decoded_cursor['offset']) is not int
                    or decoded_cursor['offset'] < 0):
                raise ValueError('invalid cursor')
            offset = decoded_cursor['offset']
        except Exception as exc:
            raise ValueError('evidence cursor does not match this source and range') from exc
    items = []
    read_limitations = list(receipt.get('limitations', []))
    range_frames = []
    if (start, end) != (0, duration):
        try:
            range_frames = extract_range_frames(ctx, receipt['media_path'], (start, end))
            items.extend(range_frames)
        except (OSError, ValueError, RuntimeError):
            read_limitations.append('Targeted frame extraction unavailable; existing sampled evidence remains available.')
    for key, kind in [('representative_frames', 'frame'), ('cuts', 'cut')]:
        for row in evidence.get(key, []):
            if start <= row.get('time_us', -1) < end:
                items.append({'kind': kind, **row})
    story = receipt.get('story', {})
    for time_us, url in zip(story.get('frame_times_us', []), story.get('frame_urls', [])):
        if start <= time_us < end:
            items.append({'kind': 'observed_frame', 'time_us': time_us, 'url': url})
    for row in story.get('reference_events', []):
        if row.get('start_us', end) < end and row.get('end_us', start) > start:
            items.append({'kind': 'event', **row})
    transcript_ref = receipt.get('transcript_ref')
    if transcript_ref:
        transcript = store.read(transcript_ref['artifact_id'], transcript_ref['revision'])['payload']
        if transcript.get('media_hash') != receipt['media_hash']:
            raise ValueError('reference transcript does not match source')
        for word in transcript.get('words', []):
            if word['start_us'] < end and word['end_us'] > start:
                items.append({'kind': 'word', **word})
    items.sort(key=lambda row: (row.get('time_us', row.get('start_us', 0)), row['kind']))
    if offset > len(items):
        raise ValueError('evidence cursor offset is invalid')
    page = []
    size = 0
    for row in items[offset:offset + limit]:
        row_size = len(json.dumps(row, ensure_ascii=False).encode())
        if row_size > 65536:
            raise ValueError('evidence item exceeds response capacity')
        if size + row_size > 65536:
            break
        page.append(row)
        size += row_size
    next_offset = offset + len(page)
    next_cursor = (base64.urlsafe_b64encode(json.dumps({'identity': identity, 'offset': next_offset,
                                                        'range_us': [start, end]}).encode()).decode()
                   if next_offset < len(items) else None)
    source_snapshot(ctx, {'media_path': receipt['media_path'], 'media_hash': receipt['media_hash']})
    return {'analysis_ref': analysis_ref, 'evidence_ref': ref, 'transcript_ref': transcript_ref,
            'range_us': [start, end], 'items': page, 'next_cursor': next_cursor,
            'coverage': receipt['coverage'], 'transcript_status': receipt['transcript_status'],
            'limitations': read_limitations, 'unknowns': receipt.get('unknowns', []),
            'requested_frame_coverage': [row['time_us'] for row in range_frames]}


def probe_audio_track(path: Path) -> bool | None:
    """An audio stream proves only audio presence, never speech presence."""
    from .evidence import _run_ffprobe
    try:
        probe = _run_ffprobe(path, '-select_streams', 'a', '-show_entries', 'stream=index', '-show_streams')
    except (ValueError, OSError):
        return None
    return bool(probe.get('streams'))


def extract_range_frames(ctx, media_path: str, range_us: tuple[int, int]) -> list[dict[str, Any]]:
    """Supplement a requested interval with bounded actual source-clock frames.

    This is evidence retrieval, not another ASR or story task. The cache remains
    inside the authorized project and is bounded by count and total image bytes.
    """
    import subprocess
    import os
    from urllib.parse import quote
    from .evidence import _probe_all_video_frames, _probe_stream, _parse_time_base, _pts_to_us
    from novelvideo.ports import get_canvas_write_mutex
    snapshot = source_snapshot(ctx, {'media_path': media_path})
    root = Path(ctx.output_dir).resolve()
    path = root / snapshot['media_path']
    stream = _probe_stream(path)
    clock = _parse_time_base(stream['time_base'])
    frames = [(frame['pts'], _pts_to_us(frame['pts'], clock)) for frame in _probe_all_video_frames(path)]
    start, end = range_us
    frames = [(pts, time) for pts, time in frames if start <= time < end]
    if not frames:
        return []
    targets = [start, (start + end) // 2, end - 1]
    selected = sorted(set(min(frames, key=lambda row: (abs(row[1] - target), row[1])) for target in targets), key=lambda row: row[1])
    cache = root / 'freezone' / '_reference_evidence_frames'
    if cache.is_symlink() or not cache.resolve().is_relative_to(root):
        raise ValueError('evidence cache is unavailable')
    result = []
    with get_canvas_write_mutex().write_mutex(root, 'reference_evidence_frames') as guard:
        cache.mkdir(parents=True, exist_ok=True)
        for pts, time_us in selected:
            target = cache / f"{snapshot['media_sha256']}-{pts}.png"
            if target.is_symlink():
                raise ValueError('evidence frame cache is unavailable')
            if not target.is_file():
                temporary = cache / f'.{uuid.uuid4().hex}.png'
                try:
                    completed = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(path), '-ss', f'{time_us / 1_000_000:.6f}',
                        '-frames:v', '1', '-vf', 'scale=640:640:force_original_aspect_ratio=decrease', str(temporary)], capture_output=True, text=True, timeout=30)
                    if completed.returncode or not temporary.is_file():
                        raise ValueError('reference range frame extraction failed')
                    guard.reassert()
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            os.utime(target, None)
            relative = target.relative_to(root).as_posix()
            result.append({'kind': 'requested_frame', 'pts': pts, 'time_base': stream['time_base'], 'time_us': time_us,
                           'media_path': relative, 'url': f'/api/v1/projects/{quote(ctx.project_id, safe="")}/media/{quote(relative, safe="/")}'})
        cached = sorted((item for item in cache.glob('*.png') if item.is_file() and not item.is_symlink()), key=lambda item: item.stat().st_mtime, reverse=True)
        total = 0
        for index, item in enumerate(cached):
            total += item.stat().st_size
            if index >= 200 or total > 64 * 1024 * 1024:
                guard.reassert()
                item.unlink(missing_ok=True)
    return result
