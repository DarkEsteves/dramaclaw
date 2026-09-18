import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/api/remix', () => ({
  uploadRemixMedia: vi.fn(),
  createRemixArtifact: vi.fn(async () => ({ id: 'manifest-1', revision: 1 })),
  renderRemixManifest: vi.fn(async () => ({ operation_id: 'operation-1', task_key: 'task-1' })),
  fetchRemixOperation: vi.fn(async () => ({ operation_id: 'operation-1', status: 'pending', output_refs: [] })),
  confirmRemixOperation: vi.fn(async () => ({ operation_id: 'operation-1', status: 'completed', output_refs: [{ project_path: 'freezone/remix_renders/output.mp4' }] })),
  fetchRemixTaskStatus: vi.fn(async () => ({ status: 'running' })),
  listRemixOperations: vi.fn(async () => ({ items: [] })),
  submitRemixProcessAction: vi.fn(async () => ({ operation_id: 'transcribe-1', task_type: 'remix_transcribe_media', task_id: 'task-transcribe', task_key: 'key', job_id: 'job-transcribe' })),
  fetchRemixArtifact: vi.fn(async () => ({ id: 'timeline-1', schema: 'word_timeline.v1', revision: 1, payload: { status: 'completed', transcript: '你好', words: [{ text: '你', start_us: 0, end_us: 500_000 }, { text: '好', start_us: 500_000, end_us: 1_000_000 }] } })),
}));
vi.mock('@/features/freezone/canvasSyncRuntime', () => ({ flushFreezoneCanvasRuntime: vi.fn(async () => true), applyRemoteFreezoneCanvas: vi.fn(() => true) }));
vi.mock('@/api/canvas', () => ({ getFreezoneCanvas: vi.fn(async () => ({ revision: 4, nodes: [], edges: [] })) }));
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: { getState: () => ({ nodes: [], edges: [] }) } }));

import { confirmRemixOperation, createRemixArtifact, fetchRemixArtifact, fetchRemixOperation, listRemixOperations, renderRemixManifest, submitRemixProcessAction, uploadRemixMedia } from '@/api/remix';
import { applyRemoteFreezoneCanvas, flushFreezoneCanvasRuntime } from '@/features/freezone/canvasSyncRuntime';
import { getFreezoneCanvas } from '@/api/canvas';
import type { RemixOperation, RemixUploadedMedia } from '@/api/remix';
import { RemixWorkbench } from './RemixWorkbench';

describe('RemixWorkbench', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(fetchRemixOperation).mockReset().mockResolvedValue({ operation_id: 'operation-1', status: 'pending', task_id: 'task-1', output_refs: [] });
    vi.mocked(listRemixOperations).mockReset().mockResolvedValue({ items: [] });
    vi.mocked(uploadRemixMedia).mockReset();
    vi.mocked(submitRemixProcessAction).mockReset().mockResolvedValue({ operation_id: 'transcribe-1', task_type: 'remix_transcribe_media', task_id: 'task-transcribe', task_key: 'key' });
    vi.mocked(fetchRemixArtifact).mockReset().mockResolvedValue({ id: 'timeline-1', schema: 'word_timeline.v1', revision: 1, payload: { status: 'completed', transcript: '你好', words: [{ text: '你', start_us: 0, end_us: 500_000 }, { text: '好', start_us: 500_000, end_us: 1_000_000 }] } });
  });
  it('transcribes the selected audio and fills editable cues', async () => {
    vi.mocked(fetchRemixOperation).mockResolvedValue({ operation_id: 'transcribe-1', status: 'needs_confirmation', task_id: 'task-transcribe', output_refs: [{ artifact_id: 'timeline-1', revision: 1 }] });
    const onPersist = vi.fn();
    render(<RemixWorkbench project="demo" canvasId="default" nodeId="compose-1" onClose={() => {}} onPersist={onPersist} initialDraft={{
      video: { media_path: 'media/video.mp4', media_hash: 'a'.repeat(64), url: '/video.mp4', size: 100, media_kind: 'video', duration_us: 2_000_000, has_audio: true },
      cues: [],
    }} />);
    fireEvent.click(screen.getByRole('button', { name: '自动识别字幕' }));
    await waitFor(() => expect(submitRemixProcessAction).toHaveBeenCalledWith('demo', expect.objectContaining({ action: 'transcribe_media', mediaPath: 'media/video.mp4', language: 'zh' })));
    await waitFor(() => expect(flushFreezoneCanvasRuntime).toHaveBeenCalledWith('demo', 'default'));
    await waitFor(() => expect(onPersist).toHaveBeenCalledWith(expect.objectContaining({ cues: [expect.objectContaining({ text: '你好', startSeconds: 0, endSeconds: 1 })] })));
    expect((screen.getByLabelText('字幕内容') as HTMLInputElement).value).toBe('你好');
  });

  it('asks before replacing manually edited cues or visual replacements', async () => {
    vi.mocked(window.confirm).mockReturnValue(false);
    render(<RemixWorkbench project="demo" canvasId="default" nodeId="compose-1" onClose={() => {}} onPersist={() => {}} initialDraft={{
      video: { media_path: 'media/video.mp4', media_hash: 'a'.repeat(64), url: '/video.mp4', size: 100, media_kind: 'video', duration_us: 2_000_000, has_audio: true },
      cues: [{ id: 'existing', label: '产品登场', text: '手工台词', startSeconds: 0, endSeconds: 1, visual: { media_path: 'media/visual.mp4', media_hash: 'b'.repeat(64), url: '/visual.mp4', size: 100, media_kind: 'video', duration_us: 2_000_000 } }],
    }} />);
    fireEvent.click(screen.getByRole('button', { name: '自动识别字幕' }));
    expect(window.confirm).toHaveBeenCalled();
    expect(submitRemixProcessAction).not.toHaveBeenCalled();
    expect((screen.getByLabelText('字幕内容') as HTMLInputElement).value).toBe('手工台词');
  });

  it('keeps manual cues when transcription reports no speech', async () => {
    vi.mocked(fetchRemixOperation).mockResolvedValue({ operation_id: 'transcribe-1', status: 'needs_confirmation', task_id: 'task-transcribe', output_refs: [{ artifact_id: 'timeline-1', revision: 1 }] });
    vi.mocked(fetchRemixArtifact).mockResolvedValue({ id: 'timeline-1', schema: 'word_timeline.v1', revision: 1, payload: { status: 'no_speech', words: [] } });
    const onPersist = vi.fn();
    render(<RemixWorkbench project="demo" canvasId="default" nodeId="compose-1" onClose={() => {}} onPersist={onPersist} initialDraft={{
      video: { media_path: 'media/video.mp4', media_hash: 'a'.repeat(64), url: '/video.mp4', size: 100, media_kind: 'video', duration_us: 2_000_000, has_audio: true },
      cues: [{ id: 'existing', label: '保留', text: '已有台词', startSeconds: 0, endSeconds: 1 }],
    }} />);
    fireEvent.click(screen.getByRole('button', { name: '自动识别字幕' }));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('没有识别到人声'));
    expect((screen.getByLabelText('字幕内容') as HTMLInputElement).value).toBe('已有台词');
  });

  it('resumes an in-flight transcription after reopening the canvas', async () => {
    vi.mocked(fetchRemixOperation).mockResolvedValue({ operation_id: 'transcribe-1', status: 'needs_confirmation', task_id: 'task-transcribe', output_refs: [{ artifact_id: 'timeline-1', revision: 1 }] });
    const onPersist = vi.fn();
    render(<RemixWorkbench project="demo" canvasId="default" nodeId="compose-1" onClose={() => {}} onPersist={onPersist} initialDraft={{
      video: { media_path: 'media/video.mp4', media_hash: 'a'.repeat(64), url: '/video.mp4', size: 100, media_kind: 'video', duration_us: 2_000_000, has_audio: true },
      cues: [], transcription: { operationId: 'transcribe-1', mediaPath: 'media/video.mp4', nodeRevision: 1 },
    }} />);
    await waitFor(() => expect(onPersist).toHaveBeenCalledWith(expect.objectContaining({ cues: [expect.objectContaining({ text: '你好' })], transcription: undefined })));
    expect(submitRemixProcessAction).not.toHaveBeenCalled();
  });

  it('keeps the transcription handle after a temporary polling error', async () => {
    vi.mocked(fetchRemixOperation).mockRejectedValueOnce(new Error('连接中断'));
    const onPersist = vi.fn();
    render(<RemixWorkbench project="demo" canvasId="default" nodeId="compose-1" onClose={() => {}} onPersist={onPersist} initialDraft={{
      video: { media_path: 'media/video.mp4', media_hash: 'a'.repeat(64), url: '/video.mp4', size: 100, media_kind: 'video', duration_us: 2_000_000, has_audio: true },
      cues: [], transcription: { operationId: 'transcribe-1', mediaPath: 'media/video.mp4', nodeRevision: 1 },
    }} />);
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('连接中断'));
    expect(onPersist).not.toHaveBeenCalled();
    expect((screen.getByRole('button', { name: '正在识别…' }) as HTMLButtonElement).disabled).toBe(true);
  });
  it('recovers an orphaned matching operation and exposes older render history', async () => {
    vi.mocked(listRemixOperations).mockResolvedValueOnce({ items: [
      { operation_id: 'current', status: 'completed', task_id: 'task-1', task_type: 'remix_render', canvas_id: 'default', node_id: 'compose-1', node_revision: 3, input_refs: [{ artifact_id: 'manifest-1', revision: 2 }], output_refs: [{ project_path: 'freezone/remix_renders/current.mp4' }] },
      { operation_id: 'older', status: 'completed', task_id: 'task-0', task_type: 'remix_render', canvas_id: 'default', node_id: 'compose-1', node_revision: 1, input_refs: [{ artifact_id: 'old', revision: 1 }], output_refs: [{ project_path: 'freezone/remix_renders/older.mp4' }] },
    ] });
    vi.mocked(fetchRemixOperation).mockImplementation(async (_project, id) => id === 'current'
      ? { operation_id: 'current', status: 'completed', task_id: 'task-1', output_refs: [{ project_path: 'freezone/remix_renders/current.mp4' }] }
      : { operation_id: 'older', status: 'needs_confirmation', task_id: 'task-0', node_revision: 1, input_refs: [{ artifact_id: 'old', revision: 1 }], output_refs: [{ project_path: 'freezone/remix_renders/older.mp4' }] });
    const onPersist = vi.fn();
    render(<RemixWorkbench project="demo" canvasId="default" nodeId="compose-1" onClose={() => {}} onPersist={onPersist} initialDraft={{ cues: [], manifestRef: { artifactId: 'manifest-1', revision: 2 }, nodeRevision: 3 }} />);
    await waitFor(() => expect(screen.getByText('下载成片')).toBeTruthy());
    expect(screen.getByRole('button', { name: /older/ })).toBeTruthy();
    expect(onPersist).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /older/ }));
    await waitFor(() => expect(screen.getByRole('link', { name: '下载成片' }).getAttribute('href')).toContain('older.mp4'));
    expect(confirmRemixOperation).not.toHaveBeenCalled();
  });
  it('recovers a finished operation without saving result polling state to the canvas', async () => {
    vi.mocked(fetchRemixOperation).mockResolvedValue({ operation_id: 'operation-1', status: 'needs_confirmation', task_id: 'task-1', node_revision: 1, input_refs: [{ artifact_id: 'manifest', revision: 1 }], output_refs: [{ project_path: 'freezone/remix_renders/output.mp4' }] });
    const onPersist = vi.fn();
    render(<RemixWorkbench project="demo" canvasId="default" nodeId="compose-1" onClose={() => {}} onPersist={onPersist} initialDraft={{ cues: [], operationId: 'operation-1', status: 'pending', manifestRef: { artifactId: 'manifest', revision: 1 }, nodeRevision: 1 }} />);
    await waitFor(() => expect(screen.getByText('下载成片')).toBeTruthy());
    expect(confirmRemixOperation).toHaveBeenCalledWith('demo', 'operation-1', 'default', 'compose-1');
    expect(getFreezoneCanvas).toHaveBeenCalledWith('demo', 'default');
    expect(applyRemoteFreezoneCanvas).toHaveBeenCalledWith('demo', 'default', expect.objectContaining({ revision: 4 }), expect.any(Function));
    expect(onPersist).not.toHaveBeenCalled();
  });

  it('reopens an already confirmed render without rehydrating the canvas again', async () => {
    vi.mocked(fetchRemixOperation).mockResolvedValue({ operation_id: 'operation-1', status: 'completed', task_id: 'task-1', output_refs: [{ project_path: 'freezone/remix_renders/output.mp4' }] });
    render(<RemixWorkbench project="demo" canvasId="default" nodeId="compose-1" onClose={() => {}} onPersist={() => {}} initialDraft={{ cues: [], operationId: 'operation-1', status: 'pending' }} />);
    await waitFor(() => expect(screen.getByText('下载成片')).toBeTruthy());
    expect(confirmRemixOperation).not.toHaveBeenCalled();
    expect(getFreezoneCanvas).not.toHaveBeenCalled();
    expect(applyRemoteFreezoneCanvas).not.toHaveBeenCalled();
  });

  it('blocks cue edits while confirmation flush and remote canvas apply are in progress', async () => {
    let releaseFlush!: (value: boolean) => void;
    vi.mocked(flushFreezoneCanvasRuntime).mockImplementationOnce(() => new Promise<boolean>((resolve) => { releaseFlush = resolve; }));
    vi.mocked(fetchRemixOperation).mockResolvedValue({ operation_id: 'operation-1', status: 'needs_confirmation', task_id: 'task-1', node_revision: 2, input_refs: [{ artifact_id: 'manifest', revision: 1 }], output_refs: [{ project_path: 'freezone/remix_renders/output.mp4' }] });
    const onPersist = vi.fn();
    render(<RemixWorkbench project="demo" canvasId="default" nodeId="compose-1" onClose={() => {}} onPersist={onPersist} initialDraft={{ cues: [{ id: 'cue', label: 'hook', text: 'before', startSeconds: 0, endSeconds: 1 }], operationId: 'operation-1', manifestRef: { artifactId: 'manifest', revision: 1 }, nodeRevision: 2 }} />);
    await waitFor(() => expect(flushFreezoneCanvasRuntime).toHaveBeenCalled());
    const caption = screen.getByLabelText('字幕内容') as HTMLInputElement;
    expect(caption.disabled).toBe(true);
    fireEvent.change(caption, { target: { value: 'after' } });
    expect(caption.value).toBe('before');
    expect(onPersist).not.toHaveBeenCalled();
    releaseFlush(true);
    await waitFor(() => expect(confirmRemixOperation).toHaveBeenCalled());
  });

  it('waits for an in-flight upload before deciding whether to confirm the prior render', async () => {
    let releaseOperation!: (value: RemixOperation) => void;
    let releaseUpload!: (value: RemixUploadedMedia) => void;
    vi.mocked(fetchRemixOperation).mockImplementationOnce(() => new Promise((resolve) => { releaseOperation = resolve; }));
    vi.mocked(uploadRemixMedia).mockImplementationOnce(() => new Promise((resolve) => { releaseUpload = resolve; }));
    const onPersist = vi.fn();
    render(<RemixWorkbench project="demo" canvasId="default" nodeId="compose-1" onClose={() => {}} onPersist={onPersist} initialDraft={{ cues: [], operationId: 'operation-1', manifestRef: { artifactId: 'manifest', revision: 1 }, nodeRevision: 2 }} />);
    await waitFor(() => expect(fetchRemixOperation).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText(/参考视频/), { target: { files: [new File(['video'], 'new.mp4', { type: 'video/mp4' })] } });
    await waitFor(() => expect(uploadRemixMedia).toHaveBeenCalled());
    releaseOperation({ operation_id: 'operation-1', status: 'needs_confirmation', task_id: 'task-1', node_revision: 2, input_refs: [{ artifact_id: 'manifest', revision: 1 }], output_refs: [{ project_path: 'freezone/remix_renders/output.mp4' }] });
    await Promise.resolve();
    expect(confirmRemixOperation).not.toHaveBeenCalled();
    releaseUpload({ media_path: 'media/new.mp4', media_hash: 'a'.repeat(64), url: '/new.mp4', size: 100, media_kind: 'video', duration_us: 2_000_000 });
    await waitFor(() => expect(onPersist).toHaveBeenCalledWith(expect.objectContaining({ operationId: undefined })));
    expect(confirmRemixOperation).not.toHaveBeenCalled();
  });

  it('does not lose cue edits attempted while manifest submission is in flight', async () => {
    let releaseArtifact!: (value: { id: string; schema: string; revision: number; payload: Record<string, unknown> }) => void;
    vi.mocked(createRemixArtifact).mockImplementationOnce(() => new Promise((resolve) => { releaseArtifact = resolve; }));
    const onPersist = vi.fn();
    render(<RemixWorkbench project="demo" canvasId="default" nodeId="compose-1" onClose={() => {}} onPersist={onPersist} initialDraft={{
      video: { media_path: 'media/video.mp4', media_hash: 'a'.repeat(64), url: '/video.mp4', size: 100, media_kind: 'video', duration_us: 3_000_000 },
      audio: { media_path: 'media/audio.wav', media_hash: 'b'.repeat(64), url: '/audio.wav', size: 100, media_kind: 'audio', duration_us: 3_000_000 },
      cues: [{ id: 'cue', label: 'hook', text: 'before', startSeconds: 0, endSeconds: 1 }],
    }} />);
    fireEvent.click(screen.getByRole('button', { name: '生成视频' }));
    await waitFor(() => expect(createRemixArtifact).toHaveBeenCalled());
    const caption = screen.getByLabelText('字幕内容') as HTMLInputElement;
    expect(caption.disabled).toBe(true);
    fireEvent.change(caption, { target: { value: 'after' } });
    expect(caption.value).toBe('before');
    releaseArtifact({ id: 'manifest-1', schema: 'composition_manifest.v1', revision: 1, payload: {} });
    await waitFor(() => expect(renderRemixManifest).toHaveBeenCalled());
    expect(onPersist).not.toHaveBeenCalledWith(expect.objectContaining({ cues: [expect.objectContaining({ text: 'after' })] }));
  });

  it('uploads a reference video into the persisted draft', async () => {
    const uploaded = { media_path: 'remix/uploads/new.mp4', media_hash: 'a'.repeat(64), url: '/new.mp4', size: 100, media_kind: 'video' as const, duration_us: 2_000_000, has_audio: true };
    vi.mocked(uploadRemixMedia).mockResolvedValueOnce(uploaded);
    const onPersist = vi.fn();
    render(<RemixWorkbench project="demo" canvasId="default" nodeId="compose-1" onClose={() => {}} onPersist={onPersist} initialDraft={{ cues: [] }} />);
    fireEvent.change(screen.getByLabelText(/参考视频/), { target: { files: [new File(['video'], 'new.mp4', { type: 'video/mp4' })] } });
    await waitFor(() => expect(onPersist).toHaveBeenCalledWith(expect.objectContaining({ video: uploaded })));
  });

  it('publishes the edited caption manifest and submits a render', async () => {
    const onPersist = vi.fn();
    render(<RemixWorkbench project="demo" canvasId="default" nodeId="compose-1" onClose={() => {}} onPersist={onPersist} initialDraft={{
      video: { media_path: 'media/video.mp4', media_hash: 'a'.repeat(64), url: '/video.mp4', size: 100, media_kind: 'video', duration_us: 3_000_000 },
      audio: { media_path: 'media/audio.wav', media_hash: 'b'.repeat(64), url: '/audio.wav', size: 100, media_kind: 'audio', duration_us: 3_000_000 },
      cues: [{ id: 'hook', label: '开头', text: '你好', startSeconds: 0, endSeconds: 1 }],
    }} />);
    fireEvent.change(screen.getByLabelText('字幕内容'), { target: { value: '早上好' } });
    expect(screen.getByLabelText('替换画面')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '生成视频' }));
    await waitFor(() => expect(renderRemixManifest).toHaveBeenCalled());
    expect(flushFreezoneCanvasRuntime).toHaveBeenCalledWith('demo', 'default');
    expect(createRemixArtifact).toHaveBeenCalledWith('demo', 'composition_manifest.v1', expect.objectContaining({
      subtitle_events: [{ text: '早上好', start_frame: 0, end_frame: 30 }],
    }), []);
    await waitFor(() => expect(flushFreezoneCanvasRuntime).toHaveBeenCalledTimes(2));
    expect(onPersist).toHaveBeenCalledWith(expect.objectContaining({ operationId: 'operation-1', status: 'pending' }));
  });
});
