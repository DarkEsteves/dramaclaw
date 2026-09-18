import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  nodes: [
    { id: 'audio', data: { remixMedia: { media_path: 'narration.wav', media_kind: 'audio', url: '/media/narration.wav' } } },
    { id: 'transcript', data: { words: [], status: 'needs_review', artifactRef: null } },
  ] as Array<{ id: string; data: Record<string, unknown> }>,
  edges: [{ source: 'audio', target: 'transcript' }],
  updateNodeData: vi.fn((id: string, patch: Record<string, unknown>) => {
    const node = state.nodes.find((item) => item.id === id);
    if (node) node.data = { ...node.data, ...patch };
  }),
  setSelectedNode: vi.fn(),
}));

vi.mock('@xyflow/react', () => ({ Handle: () => null, Position: { Left: 'left', Right: 'right' }, useUpdateNodeInternals: () => () => {} }));
vi.mock('@/features/canvas/ui/NodeHeader', () => ({ NodeHeader: () => null, NODE_HEADER_FLOATING_POSITION_CLASS: '' }));
vi.mock('@/features/canvas/ui/NodeResizeHandle', () => ({ NodeResizeHandle: () => null }));
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: Object.assign((selector: (value: typeof state) => unknown) => selector(state), { getState: () => state }) }));
vi.mock('@/lib/url-params', () => ({ readUrl: () => ({ project: 'demo', canvas: 'default' }) }));
vi.mock('@/api/remix', () => ({
  submitRemixProcessAction: vi.fn(async () => ({ operation_id: 'operation-1' })),
  fetchRemixOperation: vi.fn(async () => ({ operation_id: 'operation-1', status: 'needs_confirmation', output_refs: [{ artifact_id: 'words-1', revision: 1 }] })),
  fetchRemixTaskStatus: vi.fn(async () => null),
  fetchRemixArtifact: vi.fn(async () => ({ id: 'words-1', revision: 1, schema: 'word_timeline.v1', upstream_refs: [{ artifact_id: 'script-1', revision: 3 }], payload: {
    schema: 'word_timeline.v1', media_hash: 'hash', language: 'zh', status: 'completed', transcript: '你好',
    words: [{ text: '你', start_char: 0, end_char: 1, start_us: 0, end_us: 500_000, confidence: 0.4 }, { text: '好', start_char: 1, end_char: 2, start_us: 500_000, end_us: 1_000_000, confidence: 0.9 }],
  } })),
}));
vi.mock('./transcriptArtifactApi', () => ({ reviseTranscriptArtifact: vi.fn(async () => ({ id: 'words-1', revision: 2 })) }));

import { TranscriptNode } from '../nodes/TranscriptNode';
import { fetchRemixOperation, fetchRemixTaskStatus, submitRemixProcessAction } from '@/api/remix';
import { reviseTranscriptArtifact } from './transcriptArtifactApi';

const flowProps = { type: 'transcriptNode', zIndex: 0, draggable: true, dragging: false, selectable: true, deletable: true, isConnectable: true, positionAbsoluteX: 0, positionAbsoluteY: 0 };

describe('TranscriptNode', () => {
  beforeEach(() => {
    state.nodes.splice(2);
    state.edges.splice(1);
    state.nodes[1].data = { words: [], status: 'needs_review', artifactRef: null };
    state.updateNodeData.mockClear();
    vi.clearAllMocks();
  });

  it('starts transcription automatically when created by its source node', async () => {
    state.nodes[1].data = { words: [], status: 'needs_review', artifactRef: null, autoStartMode: 'transcribe' };
    render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);
    await waitFor(() => expect(submitRemixProcessAction).toHaveBeenCalledWith('demo', expect.objectContaining({ action: 'transcribe_media', mediaPath: 'narration.wav' })));
    expect(state.nodes[1].data.autoStartMode).toBeNull();
  });

  it('releases a failed worker task so transcription can be retried', async () => {
    vi.mocked(fetchRemixOperation).mockResolvedValueOnce({ operation_id: 'operation-1', status: 'pending', output_refs: [], task_type: 'remix_transcribe_media', task_scope: 'scope-1' } as never);
    vi.mocked(fetchRemixTaskStatus).mockResolvedValueOnce({ status: 'failed', error: 'WhisperX worker failed' });
    state.nodes[1].data = { words: [], status: 'needs_review', artifactRef: null, operationId: 'operation-1' };
    render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);
    await waitFor(() => expect(state.nodes[1].data).toMatchObject({ operationId: null, status: 'failed' }));
    expect(screen.getByText('WhisperX worker failed')).toBeTruthy();
  });

  it('transcribes the connected audio and persists a word timeline reference', async () => {
    const { rerender } = render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);
    fireEvent.click(screen.getByRole('button', { name: '开始转写' }));
    await waitFor(() => expect(submitRemixProcessAction).toHaveBeenCalledWith('demo', expect.objectContaining({ action: 'transcribe_media', mediaPath: 'narration.wav' })));
    await waitFor(() => expect(state.nodes[1].data.artifactRef).toEqual({ artifactId: 'words-1', revision: 1 }));
    rerender(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);
    expect(screen.getByDisplayValue('你')).toBeTruthy();
    expect(screen.getByText('低置信度')).toBeTruthy();
  });

  it('saves corrected words as a new artifact revision', async () => {
    state.nodes[1].data = { words: [{ text: '你', startUs: 0, endUs: 500_000, confidence: 0.4 }], status: 'completed', artifactRef: { artifactId: 'words-1', revision: 1 } };
    render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);
    fireEvent.change(screen.getByDisplayValue('你'), { target: { value: '您' } });
    fireEvent.click(screen.getByRole('checkbox', { name: '仅校正同一发音和词边界，保留原时间' }));
    fireEvent.click(screen.getByRole('button', { name: '保存校字' }));
    await waitFor(() => expect(reviseTranscriptArtifact).toHaveBeenCalledWith('demo', { artifactId: 'words-1', revision: 1 }, expect.objectContaining({ transcript: '您好' }), [{ artifactId: 'script-1', revision: 3 }]));
    expect(state.nodes[1].data.artifactRef).toEqual({ artifactId: 'words-1', revision: 2 });
  });

  it('invalidates timing for an unconfirmed same-length replacement', async () => {
    state.nodes[1].data = { words: [{ text: '你', startUs: 0, endUs: 500_000 }], status: 'completed', artifactRef: { artifactId: 'words-1', revision: 1 } };
    render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);
    fireEvent.change(screen.getByDisplayValue('你'), { target: { value: '您' } });
    fireEvent.click(screen.getByRole('button', { name: '保存校字' }));
    await waitFor(() => expect(state.nodes[1].data.artifactRef).toBeNull());
    expect(state.nodes[1].data.pendingCorrectionText).toBe('您');
    expect(reviseTranscriptArtifact).not.toHaveBeenCalled();
  });

  it('invalidates timing for a changed phrase even with confirmation', async () => {
    state.nodes[1].data = { words: [{ text: '你', startUs: 0, endUs: 500_000 }], status: 'completed', artifactRef: { artifactId: 'words-1', revision: 1 } };
    render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);
    fireEvent.change(screen.getByDisplayValue('你'), { target: { value: '您好' } });
    fireEvent.click(screen.getByRole('checkbox', { name: '仅校正同一发音和词边界，保留原时间' }));
    fireEvent.click(screen.getByRole('button', { name: '保存校字' }));
    await waitFor(() => expect(state.nodes[1].data.artifactRef).toBeNull());
    expect(reviseTranscriptArtifact).not.toHaveBeenCalled();
  });

  it('offers forced alignment when an upstream script artifact is connected', async () => {
    state.nodes.push({ id: 'script', data: { remixScriptRef: { artifactId: 'script-1', revision: 2 } } });
    state.edges.push({ source: 'script', target: 'transcript' });
    render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);
    fireEvent.click(screen.getByRole('button', { name: '按脚本对齐' }));
    await waitFor(() => expect(submitRemixProcessAction).toHaveBeenCalledWith('demo', expect.objectContaining({ action: 'align_script', mediaPath: 'narration.wav', scriptRef: { artifactId: 'script-1', revision: 2 } })));
  });

  it('invalidates old alignment when the connected script revision changes', async () => {
    state.nodes.push({ id: 'script', data: { remixScriptRef: { artifactId: 'script-1', revision: 3 } } });
    state.edges.push({ source: 'script', target: 'transcript' });
    state.nodes[1].data = { words: [{ text: '你', startUs: 0, endUs: 500_000 }], artifactRef: { artifactId: 'words-1', revision: 1 }, status: 'completed', alignedScriptRef: { artifactId: 'script-1', revision: 2 } };
    render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);
    await waitFor(() => expect(state.nodes[1].data.artifactRef).toBeNull());
    expect(state.nodes[1].data.status).toBe('needs_review');
  });
});
