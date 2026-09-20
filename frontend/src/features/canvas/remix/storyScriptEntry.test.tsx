import { forwardRef, useImperativeHandle } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { ensureSaved, createScriptFromBlueprint } = vi.hoisted(() => ({
  ensureSaved: vi.fn(async () => ({ artifactId: 'blueprint-1', revision: 1 })),
  createScriptFromBlueprint: vi.fn(() => 'script-1'),
}));
vi.mock('@xyflow/react', () => ({ Handle: () => null, Position: { Left: 'left', Right: 'right' }, useUpdateNodeInternals: () => vi.fn() }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: (selector: (state: unknown) => unknown) => selector({ nodes: [], edges: [], setSelectedNode: vi.fn(), updateNodeData: vi.fn() }) }));
vi.mock('@/features/canvas/ui/NodeHeader', () => ({ NodeHeader: () => null, NODE_HEADER_FLOATING_POSITION_CLASS: '' }));
vi.mock('@/features/canvas/ui/NodeResizeHandle', () => ({ NodeResizeHandle: () => null }));
vi.mock('@/features/canvas/ui/nodeFrameStyles', () => ({ CANVAS_NODE_PANEL_SURFACE_CLASS: '', canvasNodeFrameClass: () => '' }));
vi.mock('@/features/canvas/remix/blueprintPanel', () => ({ ReferenceBlueprintPanel: forwardRef(function MockPanel(_props, ref) { useImperativeHandle(ref, () => ({ ensureSaved })); return null; }) }));
vi.mock('@/features/canvas/remix/createScriptFromBlueprint', () => ({ createScriptFromBlueprint }));

import { VideoStoryNode } from '@/features/canvas/nodes/VideoStoryNode';

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
    fireEvent.click(screen.getByRole('button', { name: '生成改编脚本' }));
    await waitFor(() => expect(createScriptFromBlueprint).toHaveBeenCalledWith('story-1'));
    expect(ensureSaved).toHaveBeenCalledTimes(1);
  });

  it('keeps the user on the story node when blueprint save fails', async () => {
    ensureSaved.mockRejectedValueOnce(new Error('原片证据尚未完成'));
    render(<VideoStoryNode id="story-1" type="videoStoryNode" zIndex={0} draggable dragging={false} selected={false} selectable deletable isConnectable positionAbsoluteX={0} positionAbsoluteY={0} data={{ displayName: '原片拆解', rows: [], referenceEvents: [{ start_us: 0, end_us: 1_000_000, narrative_inference: '引出问题' }] }} />);
    fireEvent.click(screen.getByRole('button', { name: '生成改编脚本' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('原片证据尚未完成');
    expect(createScriptFromBlueprint).not.toHaveBeenCalled();
  });
});
