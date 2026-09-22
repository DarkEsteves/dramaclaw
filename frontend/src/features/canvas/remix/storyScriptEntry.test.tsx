import { forwardRef, useImperativeHandle } from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { ensureSaved, createScriptFromBlueprint, updateNodeData } = vi.hoisted(() => ({
  ensureSaved: vi.fn(async () => ({ artifactId: 'blueprint-1', revision: 1 })),
  createScriptFromBlueprint: vi.fn(() => 'script-1'),
  updateNodeData: vi.fn(),
}));
vi.mock('@xyflow/react', () => ({ Handle: () => null, Position: { Left: 'left', Right: 'right' }, useUpdateNodeInternals: () => vi.fn() }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: (selector: (state: unknown) => unknown) => selector({ nodes: [], edges: [], setSelectedNode: vi.fn(), updateNodeData }) }));
vi.mock('@/features/canvas/ui/NodeHeader', () => ({ NodeHeader: () => null, NODE_HEADER_FLOATING_POSITION_CLASS: '' }));
vi.mock('@/features/canvas/ui/NodeResizeHandle', () => ({ NodeResizeHandle: () => null }));
vi.mock('@/features/canvas/ui/nodeFrameStyles', () => ({ CANVAS_NODE_PANEL_SURFACE_CLASS: '', canvasNodeFrameClass: () => '' }));
vi.mock('@/features/canvas/remix/blueprintPanel', () => ({ ReferenceBlueprintPanel: forwardRef(function MockPanel(_props, ref) { useImperativeHandle(ref, () => ({ ensureSaved })); return null; }) }));
vi.mock('@/features/canvas/remix/createScriptFromBlueprint', () => ({ createScriptFromBlueprint }));

import { VideoStoryNode } from '@/features/canvas/nodes/VideoStoryNode';
import { canvasEventBus } from '@/features/canvas/application/canvasServices';

describe('story script entry', () => {
  beforeEach(() => vi.clearAllMocks());
  it('opens a readable story card by default and keeps an explicitly resized card', () => {
    const props = { id: 'story-1', type: 'videoStoryNode', zIndex: 0, draggable: true, dragging: false, selected: false, selectable: true, deletable: true, isConnectable: true, positionAbsoluteX: 0, positionAbsoluteY: 0, data: { displayName: '原片拆解', rows: [] } };
    const { container, rerender } = render(<VideoStoryNode {...props} />);
    expect(container.firstElementChild).toHaveStyle({ width: '800px', height: '480px' });
    rerender(<VideoStoryNode {...props} width={810} height={450} />);
    expect(container.firstElementChild).toHaveStyle({ width: '810px', height: '450px' });
  });
  it('saves the blueprint before opening a connected script', async () => {
    render(<VideoStoryNode id="story-1" type="videoStoryNode" zIndex={0} draggable dragging={false} selected={false} selectable deletable isConnectable positionAbsoluteX={0} positionAbsoluteY={0} data={{ displayName: '原片拆解', rows: [], referenceEvents: [{ start_us: 0, end_us: 1_000_000, narrative_inference: '引出问题' }] }} />);
    expect(screen.queryByRole('button', { name: '生成改编脚本' })).toBeNull();
    canvasEventBus.publish('remix-node/action', { nodeId: 'story-1', action: 'continue_story' });
    await waitFor(() => expect(createScriptFromBlueprint).toHaveBeenCalledWith('story-1'));
    expect(ensureSaved).toHaveBeenCalledTimes(1);
  });

  it('keeps the user on the story node when blueprint save fails', async () => {
    ensureSaved.mockRejectedValueOnce(new Error('原片证据尚未完成'));
    render(<VideoStoryNode id="story-1" type="videoStoryNode" zIndex={0} draggable dragging={false} selected={false} selectable deletable isConnectable positionAbsoluteX={0} positionAbsoluteY={0} data={{ displayName: '原片拆解', rows: [], referenceEvents: [{ start_us: 0, end_us: 1_000_000, narrative_inference: '引出问题' }] }} />);
    canvasEventBus.publish('remix-node/action', { nodeId: 'story-1', action: 'continue_story' });
    expect(await screen.findByRole('alert')).toHaveTextContent('原片证据尚未完成');
    expect(createScriptFromBlueprint).not.toHaveBeenCalled();
  });

  it('marks every pending reference event reviewed with one canvas update', () => {
    render(<VideoStoryNode id="story-1" type="videoStoryNode" zIndex={0} draggable dragging={false} selected={false} selectable deletable isConnectable positionAbsoluteX={0} positionAbsoluteY={0} data={{ displayName: '原片拆解', rows: [], referenceEvents: [
      { event_id: 'pending-1', start_us: 0, end_us: 1_000_000 },
      { event_id: 'uncertain-1', start_us: 1_000_000, end_us: 2_000_000, review_state: 'reviewed_uncertain' },
      { event_id: 'pending-2', start_us: 2_000_000, end_us: 3_000_000, review_state: 'needs_review' },
    ] }} />);

    fireEvent.click(screen.getByRole('button', { name: '全部标记为已核对' }));
    expect(updateNodeData).toHaveBeenCalledWith('story-1', {
      referenceEvents: [
        expect.objectContaining({ event_id: 'pending-1', review_state: 'reviewed' }),
        expect.objectContaining({ event_id: 'uncertain-1', review_state: 'reviewed_uncertain' }),
        expect.objectContaining({ event_id: 'pending-2', review_state: 'reviewed' }),
      ],
      blueprintDirty: true,
    });
  });

  it('shows the complete reference-event review UI in fullscreen', () => {
    render(<VideoStoryNode id="story-1" type="videoStoryNode" zIndex={0} draggable dragging={false} selected={false} selectable deletable isConnectable positionAbsoluteX={0} positionAbsoluteY={0} data={{ displayName: '原片拆解', rows: [], sourceVideoUrl: '/clip.mp4', referenceEvents: [{
      event_id: 'event-1', start_us: 0, end_us: 1_000_000,
      visual_observation: '人物展示产品', spoken_text: '这是产品台词', narrative_inference: '产品登场', review_state: 'needs_review',
      observed_frame: { time_us: 500_000, pts: 7500, time_base: '1/15000', url: '/frame.png' },
    }] }} />);

    fireEvent.click(screen.getByRole('button', { name: 'node.videoStory.fullscreen' }));
    const dialog = screen.getByRole('dialog', { name: '原片拆解全屏校对' });
    expect(within(dialog).getByText('人物展示产品')).not.toHaveClass('line-clamp-2');
    expect(within(dialog).getByText('这是产品台词')).toBeTruthy();
    expect(within(dialog).getByText('产品登场')).toBeTruthy();
    expect(within(dialog).getByText(/PTS 7500 · 1\/15000/)).toBeTruthy();
    fireEvent.change(within(dialog).getByLabelText('事件 1 核对状态'), { target: { value: 'reviewed' } });
    expect(updateNodeData).toHaveBeenCalledWith('story-1', expect.objectContaining({
      referenceEvents: [expect.objectContaining({ event_id: 'event-1', review_state: 'reviewed' })],
      blueprintDirty: true,
    }));
  });
});
