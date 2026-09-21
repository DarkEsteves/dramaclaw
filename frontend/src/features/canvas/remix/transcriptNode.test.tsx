import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
import { fetchRemixArtifact, fetchRemixOperation, fetchRemixTaskStatus, submitRemixProcessAction } from '@/api/remix';
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

  it('starts script alignment automatically only with both saved script and narration audio connected', async () => {
    state.nodes.push({ id: 'script', data: { remixScriptRef: { artifactId: 'script-1', revision: 3 } } });
    state.edges.push({ source: 'script', target: 'transcript' });
    state.nodes[1].data = { words: [], status: 'needs_review', artifactRef: null, autoStartMode: 'align' };
    render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);
    await waitFor(() => expect(submitRemixProcessAction).toHaveBeenCalledWith('demo', expect.objectContaining({
      action: 'align_script', mediaPath: 'narration.wav', scriptRef: { artifactId: 'script-1', revision: 3 },
    })));
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
    const { container, rerender } = render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);
    fireEvent.click(screen.getByRole('button', { name: '开始转写' }));
    await waitFor(() => expect(submitRemixProcessAction).toHaveBeenCalledWith('demo', expect.objectContaining({ action: 'transcribe_media', mediaPath: 'narration.wav' })));
    await waitFor(() => expect(state.nodes[1].data.artifactRef).toEqual({ artifactId: 'words-1', revision: 1 }));
    rerender(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);
    expect(container.querySelector('p')?.textContent).toBe('你好');
    expect(screen.queryByDisplayValue('你')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '展开逐词校对（2）' }));
    expect(screen.getByDisplayValue('你')).toBeTruthy();
    expect(screen.getByText('低置信度')).toBeTruthy();
  });

  it('loads the full text for an existing transcript node', async () => {
    state.nodes[1].data = { words: [{ text: '你', startUs: 0, endUs: 500_000 }], status: 'completed', artifactRef: { artifactId: 'words-1', revision: 1 } };
    render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);
    await waitFor(() => expect(state.nodes[1].data.transcriptText).toBe('你好'));
  });

  it('seeks from a word in the visible transcript while preserving punctuation', () => {
    state.nodes[1].data = {
      words: [
        { text: 'Hello', startUs: 1_200_000, endUs: 1_600_000 },
        { text: 'world', startUs: 2_100_000, endUs: 2_500_000 },
      ],
      transcriptText: 'Hello, world!', status: 'completed', artifactRef: null,
    };
    const { container } = render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);
    const audio = container.querySelector('audio')!;

    expect(container.querySelector('p')?.textContent).toBe('Hello, world!');
    fireEvent.click(screen.getByRole('button', { name: '跳转到 world，00:02.100' }));

    expect(audio.currentTime).toBe(2.1);
  });

  it('highlights the word at the current playback time', () => {
    state.nodes[1].data = {
      words: [
        { text: 'Hello', startUs: 1_200_000, endUs: 1_600_000 },
        { text: 'world', startUs: 2_100_000, endUs: 2_500_000 },
      ],
      transcriptText: 'Hello world', status: 'completed', artifactRef: null,
    };
    const { container } = render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);
    const audio = container.querySelector('audio')!;
    audio.currentTime = 2.2;
    fireEvent.timeUpdate(audio);

    expect(screen.getByRole('button', { name: '跳转到 world，00:02.100' }).getAttribute('aria-current')).toBe('true');
    expect(screen.getByRole('button', { name: '跳转到 Hello，00:01.200' }).hasAttribute('aria-current')).toBe(false);
  });

  it('follows short words between browser timeupdate events while playing', () => {
    state.nodes[1].data = {
      words: [
        { text: 'need', startUs: 13_005_000, endUs: 13_185_000 },
        { text: 'this', startUs: 13_185_000, endUs: 13_446_000 },
        { text: 'way', startUs: 13_446_000, endUs: 13_686_000 },
      ],
      transcriptText: 'need this way', status: 'completed', artifactRef: null,
    };
    const frames: FrameRequestCallback[] = [];
    const requestFrame = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      frames.push(callback);
      return frames.length;
    });
    const cancelFrame = vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {});
    try {
      const { container, unmount } = render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);
      const audio = container.querySelector('audio')!;
      audio.currentTime = 13.05;
      fireEvent.play(audio);
      expect(screen.getByRole('button', { name: '跳转到 need，00:13.005' }).getAttribute('aria-current')).toBe('true');

      audio.currentTime = 13.23;
      act(() => { frames.shift()?.(0); });
      expect(screen.getByRole('button', { name: '跳转到 this，00:13.185' }).getAttribute('aria-current')).toBe('true');

      audio.currentTime = 13.5;
      act(() => { frames.shift()?.(0); });
      expect(screen.getByRole('button', { name: '跳转到 way，00:13.446' }).getAttribute('aria-current')).toBe('true');
      unmount();
      expect(cancelFrame).toHaveBeenCalled();
    } finally {
      requestFrame.mockRestore();
      cancelFrame.mockRestore();
    }
  });

  it('saves corrected words as a new artifact revision', async () => {
    state.nodes[1].data = { words: [{ text: '你', startUs: 0, endUs: 500_000, confidence: 0.4 }], status: 'completed', artifactRef: { artifactId: 'words-1', revision: 1 } };
    render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);
    fireEvent.click(screen.getByRole('button', { name: '展开逐词校对（1）' }));
    fireEvent.change(screen.getByDisplayValue('你'), { target: { value: '您' } });
    fireEvent.click(screen.getByRole('checkbox', { name: '仅校正同一发音和词边界，保留原时间' }));
    fireEvent.click(screen.getByRole('button', { name: '保存校字' }));
    await waitFor(() => expect(reviseTranscriptArtifact).toHaveBeenCalledWith('demo', { artifactId: 'words-1', revision: 1 }, expect.objectContaining({ transcript: '您好' }), [{ artifactId: 'script-1', revision: 3 }]));
    expect(state.nodes[1].data.artifactRef).toEqual({ artifactId: 'words-1', revision: 2 });
  });

  it('invalidates timing for an unconfirmed same-length replacement', async () => {
    state.nodes[1].data = { words: [{ text: '你', startUs: 0, endUs: 500_000 }], status: 'completed', artifactRef: { artifactId: 'words-1', revision: 1 } };
    render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);
    fireEvent.click(screen.getByRole('button', { name: '展开逐词校对（1）' }));
    fireEvent.change(screen.getByDisplayValue('你'), { target: { value: '您' } });
    fireEvent.click(screen.getByRole('button', { name: '保存校字' }));
    await waitFor(() => expect(state.nodes[1].data.artifactRef).toBeNull());
    expect(state.nodes[1].data.pendingCorrectionText).toBe('您');
    expect(reviseTranscriptArtifact).not.toHaveBeenCalled();
  });

  it('invalidates timing for a changed phrase even with confirmation', async () => {
    state.nodes[1].data = { words: [{ text: '你', startUs: 0, endUs: 500_000 }], status: 'completed', artifactRef: { artifactId: 'words-1', revision: 1 } };
    render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);
    fireEvent.click(screen.getByRole('button', { name: '展开逐词校对（1）' }));
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

  it('explains a script mismatch instead of reporting silence', async () => {
    vi.mocked(fetchRemixArtifact).mockResolvedValueOnce({
      id: 'words-1', revision: 1, schema: 'word_timeline.v1', payload: {
        status: 'script_mismatch', words: [], mismatch: { reason: 'ASR transcript does not match the script' },
      },
    } as never);
    state.nodes[1].data = { words: [], status: 'needs_review', artifactRef: null, operationId: 'operation-1', sourceMediaPath: 'narration.wav' };
    render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);

    await waitFor(() => expect(state.nodes[1].data).toMatchObject({ operationId: null, status: 'needs_review' }));
    expect(screen.getByText(/脚本与音频不一致/)).toBeTruthy();
    expect(screen.queryByText(/没有识别到人声/)).toBeNull();
  });

  it('shows exact speech differences and keeps the audio and script revision for recovery', async () => {
    vi.mocked(fetchRemixArtifact).mockResolvedValueOnce({
      id: 'words-review', revision: 2, schema: 'word_timeline.v1', upstream_refs: [{ artifact_id: 'script-1', revision: 3 }], payload: {
        status: 'script_mismatch', script_revision: 3, words: [], mismatch: {
          reason: 'ASR omitted required script text',
          report: {
            character_coverage: 0.72,
            substitutions: [{ script_range: { text: '馋', start_char: 4, end_char: 5 }, asr_range: { text: '茶', start_char: 4, end_char: 5 }, confidence: 'low', provenance: 'asr_comparison' }],
            insertions: [],
            omissions: [{ script_range: { text: '马上出发', start_char: 12, end_char: 16 }, asr_range: { text: '', start_char: 8, end_char: 8 }, confidence: 'low', provenance: 'asr_comparison' }],
          },
        },
      },
    } as never);
    state.nodes.push({ id: 'script', data: { remixScriptRef: { artifactId: 'script-1', revision: 3 } } });
    state.edges.push({ source: 'script', target: 'transcript' });
    state.nodes[1].data = {
      words: [], status: 'needs_review', artifactRef: null, operationId: 'operation-1',
      sourceMediaPath: 'narration.wav', pendingScriptRef: { artifactId: 'script-1', revision: 3 },
    };
    const view = render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);

    await waitFor(() => expect(state.nodes[1].data.operationId).toBeNull());
    expect(state.nodes[1].data).toMatchObject({
      sourceMediaPath: 'narration.wav',
      pendingScriptRef: { artifactId: 'script-1', revision: 3 },
      reviewArtifactRef: { artifactId: 'words-review', revision: 2 },
    });
    view.rerender(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);
    expect(screen.getByText(/馋.*→.*茶/)).toBeTruthy();
    expect(screen.getByText(/缺少.*马上出发/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '校对台词' }));
    expect(state.setSelectedNode).toHaveBeenCalledWith('script');
    fireEvent.click(screen.getByRole('button', { name: '重新对齐' }));
    await waitFor(() => expect(submitRemixProcessAction).toHaveBeenCalledWith('demo', expect.objectContaining({
      action: 'align_script', mediaPath: 'narration.wav', scriptRef: { artifactId: 'script-1', revision: 3 },
    })));
  });

  it('keeps the recovery route when forced alignment fails after an exact ASR match', () => {
    state.nodes.push({ id: 'script', data: { remixScriptRef: { artifactId: 'script-1', revision: 3 } } });
    state.edges.push({ source: 'script', target: 'transcript' });
    state.nodes[1].data = {
      words: [], status: 'needs_review', artifactRef: null, alignmentStale: true,
      pendingScriptRef: { artifactId: 'script-1', revision: 3 },
      speechMismatch: { reason: 'forced alignment left required script characters untimed', report: {
        substitutions: [], insertions: [], omissions: [], character_coverage: 1,
      } },
    };

    render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);

    expect(screen.getByRole('button', { name: '校对台词' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '重新对齐' })).toBeTruthy();
  });

  it('does not accept an alignment completed for a previous script revision', async () => {
    state.nodes.push({ id: 'script', data: { remixScriptRef: { artifactId: 'script-1', revision: 3 } } });
    state.edges.push({ source: 'script', target: 'transcript' });
    state.nodes[1].data = {
      words: [], status: 'needs_review', artifactRef: null, operationId: 'operation-1',
      sourceMediaPath: 'narration.wav', pendingScriptRef: { artifactId: 'script-1', revision: 2 },
    };
    render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);

    await waitFor(() => expect(state.nodes[1].data.operationId).toBeNull());
    expect(state.nodes[1].data.artifactRef).toBeNull();
    expect(screen.getByText(/脚本已修改/)).toBeTruthy();
  });

  it('rejects an alignment artifact linked to a different script revision', async () => {
    state.nodes.push({ id: 'script', data: { remixScriptRef: { artifactId: 'script-1', revision: 2 } } });
    state.edges.push({ source: 'script', target: 'transcript' });
    state.nodes[1].data = {
      words: [], status: 'needs_review', artifactRef: null, operationId: 'operation-1',
      sourceMediaPath: 'narration.wav', pendingScriptRef: { artifactId: 'script-1', revision: 2 },
    };
    render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);

    await waitFor(() => expect(state.nodes[1].data.operationId).toBeNull());
    expect(state.nodes[1].data.artifactRef).toBeNull();
    expect(screen.getByText(/对齐结果对应的脚本版本不一致/)).toBeTruthy();
  });

  it('accepts a completed alignment linked to the current script revision', async () => {
    vi.mocked(fetchRemixArtifact).mockResolvedValueOnce({
      id: 'words-1', revision: 1, schema: 'word_timeline.v1',
      upstream_refs: [{ artifact_id: 'script-1', revision: 2 }],
      payload: { status: 'completed', script_revision: 2, transcript: '你好', words: [
        { text: '你', start_char: 0, end_char: 1, start_us: 0, end_us: 500_000 },
        { text: '好', start_char: 1, end_char: 2, start_us: 500_000, end_us: 1_000_000 },
      ] },
    } as never);
    state.nodes.push({ id: 'script', data: { remixScriptRef: { artifactId: 'script-1', revision: 2 } } });
    state.edges.push({ source: 'script', target: 'transcript' });
    state.nodes[1].data = {
      words: [], status: 'needs_review', artifactRef: null, operationId: 'operation-1',
      sourceMediaPath: 'narration.wav', pendingScriptRef: { artifactId: 'script-1', revision: 2 },
    };
    render(<TranscriptNode {...flowProps} id="transcript" data={state.nodes[1].data as never} selected={false} />);

    await waitFor(() => expect(state.nodes[1].data.artifactRef).toEqual({ artifactId: 'words-1', revision: 1 }));
    expect(state.nodes[1].data.alignedScriptRef).toEqual({ artifactId: 'script-1', revision: 2 });
  });
});
