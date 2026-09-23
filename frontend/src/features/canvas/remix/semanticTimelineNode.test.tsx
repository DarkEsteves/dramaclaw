import { StrictMode } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const updateNodeData = vi.fn();
const upstreamNodes = [
  { id: 'script-1', type: 'scriptNode', data: {
    remixScriptRef: { artifactId: 'script-art', revision: 1 },
    remixBlueprintRef: { artifactId: 'blueprint-art', revision: 2 },
    remixScriptDraft: {
      dialogue: '男：看看这杯黑咖啡。',
      selections: [{ id: 'product', label: '产品亮相', start: 0, end: 100 }],
    },
  } },
  { id: 'transcript-1', type: 'transcriptNode', data: { artifactRef: { artifactId: 'words-art', revision: 1 }, sourceMediaPath: 'media/voice.wav' } },
];
vi.mock('@xyflow/react', () => ({ Handle: ({ id }: { id: string }) => <div data-handle={id} />, Position: { Left: 'left', Right: 'right' } }));
vi.mock('@/features/canvas/ui/NodeHeader', () => ({ NodeHeader: () => null, NODE_HEADER_FLOATING_POSITION_CLASS: '' }));
vi.mock('@/features/canvas/application/useUpstreamGraph', () => ({ useUpstreamNodes: () => upstreamNodes }));
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: (selector: (state: unknown) => unknown) => selector({ updateNodeData, setSelectedNode: vi.fn() }) }));
vi.mock('@/lib/url-params', () => ({ readUrl: () => ({ project: 'demo' }) }));
vi.mock('@/api/remix', () => ({
  fetchRemixArtifact: vi.fn(async (_project, ref) => ref.artifactId === 'words-art'
    ? { schema: 'word_timeline.v1', payload: { status: 'completed', words: [{ start_us: 0, end_us: 1_000_000 }] } }
    : { schema: 'performance_timing.v1', payload: { events: [{ event_id: 'product', start_frame: 3, end_frame: 15 }], unresolved_required_events: [], override_conflicts: [] } }),
  submitRemixProcessAction: vi.fn(async () => ({ operation_id: 'op-1' })),
  fetchRemixOperation: vi.fn(async () => ({ operation_id: 'op-1', status: 'needs_confirmation', node_revision: 2, input_refs: [{ artifact_id: 'script-art', revision: 1 }, { artifact_id: 'words-art', revision: 1 }, { artifact_id: 'blueprint-art', revision: 2 }], output_refs: [{ artifact_id: 'timing-art', revision: 1 }] })),
  confirmRemixOperation: vi.fn(async () => ({ operation_id: 'op-1', status: 'completed', output_refs: [{ artifact_id: 'timing-art', revision: 1 }] })),
}));

import { fetchRemixOperation, submitRemixProcessAction } from '@/api/remix';
import { SemanticTimelineNode } from '../nodes/SemanticTimelineNode';
import { canvasEventBus } from '@/features/canvas/application/canvasServices';

describe('SemanticTimelineNode', () => {
  beforeEach(() => vi.clearAllMocks());
  it('compiles connected script and speech alignment with editable event offset', async () => {
    render(<SemanticTimelineNode id="timeline-1" type="semanticTimelineNode" data={{ displayName: 'Semantic timeline', events: [{ selectionId: 'product', label: '产品登场' }], eventOverrides: {} }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);
    expect(document.querySelector('[data-handle="target"]')).toBeTruthy();
    expect(document.querySelector('[data-handle="source"]')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '微调 产品亮相' }));
    expect(screen.getByLabelText('产品亮相 最短时长（帧）')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('产品亮相 整体移动（毫秒）'), { target: { value: '250' } });
    act(() => canvasEventBus.publish('remix-node/action', { nodeId: 'timeline-1', action: 'compile' }));
    await waitFor(() => expect(submitRemixProcessAction).toHaveBeenCalledWith('demo', expect.objectContaining({
      action: 'compile_semantic_timeline',
      scriptRef: { artifactId: 'script-art', revision: 1 },
      wordTimelineRef: { artifactId: 'words-art', revision: 1 },
      blueprintRef: { artifactId: 'blueprint-art', revision: 2 },
      mediaPath: 'media/voice.wav',
      editMap: [{ source_start_us: 0, source_end_us: 1_000_000, timeline_start_us: 0 }],
      overrides: [{ event_id: 'product', relative_offset_us: 250_000 }],
    })));
    await waitFor(() => expect(updateNodeData).toHaveBeenCalledWith('timeline-1', expect.objectContaining({ artifactRef: { artifactId: 'timing-art', revision: 1 } })));
  });

  it('keeps an incomplete fixed-frame edit visible and reports it before submitting', async () => {
    render(<SemanticTimelineNode id="timeline-1" type="semanticTimelineNode" data={{ displayName: 'Semantic timeline', events: [{ selectionId: 'product', label: '产品登场' }], eventOverrides: {} }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);
    fireEvent.click(screen.getByRole('button', { name: '微调 产品亮相' }));
    const start = screen.getByLabelText('产品亮相 固定开始（帧）') as HTMLInputElement;
    fireEvent.change(start, { target: { value: '12' } });
    act(() => canvasEventBus.publish('remix-node/action', { nodeId: 'timeline-1', action: 'compile' }));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('同时填写'));
    expect(start.value).toBe('12');
    expect(submitRemixProcessAction).not.toHaveBeenCalled();
  });

  it('presents events as readable script sections and hides advanced controls by default', () => {
    render(<SemanticTimelineNode id="timeline-1" type="semanticTimelineNode" data={{ displayName: 'Semantic timeline', artifactRef: { artifactId: 'timing-art', revision: 1 }, events: [{ selectionId: 'product', label: 'product', startFrame: 60, endFrame: 150 }], eventOverrides: {}, fps: 30 }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);

    expect(screen.getByText('1 个片段')).toBeTruthy();
    expect(screen.getByText('产品亮相')).toBeTruthy();
    expect(screen.getByText('男：看看这杯黑咖啡。')).toBeTruthy();
    expect(screen.getByText('00:02.0 – 00:05.0')).toBeTruthy();
    expect(screen.queryByText('product')).toBeNull();
    expect(screen.queryByLabelText('产品亮相 整体移动（毫秒）')).toBeNull();
  });

  it('resumes a persisted compile operation after mounting without submitting another task', async () => {
    vi.mocked(fetchRemixOperation).mockResolvedValueOnce({ operation_id: 'op-1', task_id: null, status: 'needs_confirmation', node_revision: 1, input_refs: [{ artifact_id: 'script-art', revision: 1 }, { artifact_id: 'words-art', revision: 1 }, { artifact_id: 'blueprint-art', revision: 2 }], output_refs: [{ artifact_id: 'timing-art', revision: 1 }] });
    render(<SemanticTimelineNode id="timeline-1" type="semanticTimelineNode" data={{ displayName: 'Semantic timeline', events: [], compileOperationId: 'op-1', nodeRevision: 1 }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);
    await waitFor(() => expect(updateNodeData).toHaveBeenCalledWith('timeline-1', expect.objectContaining({ artifactRef: { artifactId: 'timing-art', revision: 1 }, compileOperationId: null })));
    expect(submitRemixProcessAction).not.toHaveBeenCalled();
  });

  it('clears the completed operation even after a stale operation render', async () => {
    const props = { id: 'timeline-1', type: 'semanticTimelineNode', selected: false, draggable: true, dragging: false, selectable: true, deletable: true, zIndex: 0, isConnectable: true, positionAbsoluteX: 0, positionAbsoluteY: 0 } as const;
    const stale = { displayName: 'Semantic timeline', events: [], compileOperationId: 'op-1', nodeRevision: 1 };
    const view = render(<SemanticTimelineNode {...props} data={stale} />);

    await waitFor(() => expect(updateNodeData).toHaveBeenCalledWith('timeline-1', expect.objectContaining({ compileOperationId: null })));
    view.rerender(<SemanticTimelineNode {...props} data={stale} />);
    view.rerender(<SemanticTimelineNode {...props} data={{ ...stale, compileOperationId: null, artifactRef: { artifactId: 'timing-art', revision: 1 } }} />);

    expect(screen.getByText('版本 1')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '继续检查' })).toBeNull();
    expect(screen.queryByText('正在计算时间区间…')).toBeNull();
  });

  it('does not publish an operation compiled for an older script revision', async () => {
    vi.mocked(fetchRemixOperation).mockResolvedValueOnce({ operation_id: 'op-1', task_id: null, status: 'needs_confirmation', node_revision: 1, input_refs: [{ artifact_id: 'script-art', revision: 0 }, { artifact_id: 'words-art', revision: 1 }, { artifact_id: 'blueprint-art', revision: 2 }], output_refs: [{ artifact_id: 'timing-art', revision: 1 }] });
    render(<SemanticTimelineNode id="timeline-1" type="semanticTimelineNode" data={{ displayName: 'Semantic timeline', events: [], compileOperationId: 'op-1', nodeRevision: 1 }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);
    await waitFor(() => expect(updateNodeData).toHaveBeenCalledWith('timeline-1', expect.objectContaining({ artifactRef: null, compileOperationId: null })));
    expect(updateNodeData).not.toHaveBeenCalledWith('timeline-1', expect.objectContaining({ artifactRef: { artifactId: 'timing-art', revision: 1 } }));
    expect(submitRemixProcessAction).not.toHaveBeenCalled();
  });

  it('rechecks the saved operation after a temporary request failure instead of submitting a duplicate', async () => {
    vi.mocked(fetchRemixOperation)
      .mockRejectedValueOnce(new Error('网络暂时不可用'))
      .mockResolvedValueOnce({ operation_id: 'op-1', task_id: null, status: 'needs_confirmation', node_revision: 1, input_refs: [{ artifact_id: 'script-art', revision: 1 }, { artifact_id: 'words-art', revision: 1 }, { artifact_id: 'blueprint-art', revision: 2 }], output_refs: [{ artifact_id: 'timing-art', revision: 1 }] });
    render(<SemanticTimelineNode id="timeline-1" type="semanticTimelineNode" data={{ displayName: 'Semantic timeline', events: [], compileOperationId: 'op-1', nodeRevision: 1 }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('网络暂时不可用'));
    act(() => canvasEventBus.publish('remix-node/action', { nodeId: 'timeline-1', action: 'compile' }));
    await waitFor(() => expect(updateNodeData).toHaveBeenCalledWith('timeline-1', expect.objectContaining({ artifactRef: { artifactId: 'timing-art', revision: 1 } })));
    expect(submitRemixProcessAction).not.toHaveBeenCalled();
  });

  it('claims automatic compilation only once under StrictMode', async () => {
    let release!: (value: Awaited<ReturnType<typeof submitRemixProcessAction>>) => void;
    vi.mocked(submitRemixProcessAction).mockImplementation(() => new Promise((resolve) => { release = resolve; }));
    render(<StrictMode><SemanticTimelineNode id="timeline-1" type="semanticTimelineNode" data={{ displayName: 'Semantic timeline', events: [], autoCompileRequested: true }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} /></StrictMode>);
    await waitFor(() => expect(submitRemixProcessAction).toHaveBeenCalled());
    expect(submitRemixProcessAction).toHaveBeenCalledTimes(1);
    release({ operation_id: 'op-1', task_key: 'key', task_type: 'remix_compile_semantic_timeline', task_id: 'task-1' });
  });
});
