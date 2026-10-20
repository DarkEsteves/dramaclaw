import { forwardRef, useImperativeHandle } from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { ensureSaved, createScriptFromBlueprint, updateNodeData, addNode, findNodePosition, setSelectedNode, requestFocusNode } = vi.hoisted(() => ({
  ensureSaved: vi.fn(async () => ({ artifactId: 'blueprint-1', revision: 1 })),
  createScriptFromBlueprint: vi.fn(() => 'script-1'),
  updateNodeData: vi.fn(),
  addNode: vi.fn(() => 'keyframe-image-1'),
  findNodePosition: vi.fn(() => ({ x: 1200, y: 240 })),
  setSelectedNode: vi.fn(),
  requestFocusNode: vi.fn(),
}));
const canvasNodes = vi.hoisted(() => [] as Array<{ id: string; width?: number; height?: number; measured?: { width: number; height: number }; style?: { width?: number; height?: number } }>);
vi.mock('@xyflow/react', () => ({ Handle: () => null, Position: { Left: 'left', Right: 'right' }, useUpdateNodeInternals: () => vi.fn() }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: (selector: (state: unknown) => unknown) => selector({ nodes: canvasNodes, edges: [], setSelectedNode, updateNodeData, addNode, findNodePosition, requestFocusNode }) }));
vi.mock('@/features/canvas/ui/NodeHeader', () => ({ NodeHeader: () => null, NODE_HEADER_FLOATING_POSITION_CLASS: '' }));
vi.mock('@/features/canvas/ui/NodeResizeHandle', () => ({ NodeResizeHandle: () => null }));
vi.mock('@/features/canvas/ui/nodeFrameStyles', () => ({ CANVAS_NODE_PANEL_SURFACE_CLASS: '', canvasNodeFrameClass: () => '' }));
vi.mock('@/features/canvas/remix/blueprintPanel', () => ({ ReferenceBlueprintPanel: forwardRef(function MockPanel(_props, ref) { useImperativeHandle(ref, () => ({ ensureSaved })); return <div>改编规则内容</div>; }) }));
vi.mock('@/features/canvas/remix/createScriptFromBlueprint', () => ({ createScriptFromBlueprint }));

import { VideoStoryNode } from '@/features/canvas/nodes/VideoStoryNode';
import { canvasEventBus } from '@/features/canvas/application/canvasServices';

describe('story script entry', () => {
  beforeEach(() => { vi.clearAllMocks(); canvasNodes.length = 0; });
  it('opens a readable story card by default and keeps an explicitly resized card', () => {
    const props = { id: 'story-1', type: 'videoStoryNode', zIndex: 0, draggable: true, dragging: false, selected: false, selectable: true, deletable: true, isConnectable: true, positionAbsoluteX: 0, positionAbsoluteY: 0, data: { displayName: '原片拆解', rows: [] } };
    canvasNodes.push({ id: 'story-1' });
    const { container, rerender } = render(<VideoStoryNode {...props} />);
    expect(container.firstElementChild).toHaveStyle({ width: '960px', height: '680px' });
    canvasNodes[0] = { id: 'story-1', measured: { width: 480, height: 240 } };
    rerender(<VideoStoryNode {...props} width={480} height={240} />);
    expect(container.firstElementChild).toHaveStyle({ width: '960px', height: '680px' });
    canvasNodes[0] = { id: 'story-1', width: 810, height: 450, measured: { width: 810, height: 450 } };
    rerender(<VideoStoryNode {...props} width={810} height={450} />);
    expect(container.firstElementChild).toHaveStyle({ width: '810px', height: '450px' });
  });
  it('updates the old measured default while respecting a manually resized story node', () => {
    const data = { displayName: '原片拆解', rows: [] };
    const props = { id: 'story-1', type: 'videoStoryNode', zIndex: 0, draggable: true, dragging: false, selected: false, selectable: true, deletable: true, isConnectable: true, positionAbsoluteX: 0, positionAbsoluteY: 0, data };
    canvasNodes.push({ id: 'story-1', measured: { width: 800, height: 480 } });
    const { container, rerender } = render(<VideoStoryNode {...props} width={800} height={480} />);
    expect(container.firstElementChild).toHaveStyle({ width: '960px', height: '680px' });
    canvasNodes[0] = { id: 'story-1', width: 810, height: 450, measured: { width: 810, height: 450 } };
    rerender(<VideoStoryNode {...props} width={810} height={450} />);
    expect(container.firstElementChild).toHaveStyle({ width: '810px', height: '450px' });
  });
  it('creates a connected script immediately without opening story-owned settings', async () => {
    render(<VideoStoryNode id="story-1" type="videoStoryNode" zIndex={0} draggable dragging={false} selected={false} selectable deletable isConnectable positionAbsoluteX={0} positionAbsoluteY={0} data={{ displayName: '原片拆解', rows: [], referenceEvents: [{ start_us: 0, end_us: 1_000_000, narrative_inference: '引出问题' }] }} />);
    expect(screen.queryByRole('button', { name: '生成改编脚本' })).toBeNull();
    expect(screen.queryByText(/参考蓝图与分析设置/)).toBeNull();
    act(() => canvasEventBus.publish('remix-node/action', { nodeId: 'story-1', action: 'continue_story' }));
    await waitFor(() => expect(createScriptFromBlueprint).toHaveBeenCalledWith('story-1'));
    expect(screen.queryByRole('dialog', { name: '改编脚本设置' })).toBeNull();
  });

  it('shows the next step on the story card without adding a second creation button', () => {
    const base = { id: 'story-1', type: 'videoStoryNode', zIndex: 0, draggable: true, dragging: false, selected: false, selectable: true, deletable: true, isConnectable: true, positionAbsoluteX: 0, positionAbsoluteY: 0 };
    const { rerender } = render(<VideoStoryNode {...base} data={{ rows: [], referenceEvents: [{ event_id: 'event-1', review_state: 'needs_review', start_us: 0, end_us: 1_000_000 }] }} />);
    expect(screen.getByText('下一步：新建作品方案')).toBeVisible();
    rerender(<VideoStoryNode {...base} data={{ rows: [], referenceEvents: [{ event_id: 'event-1', review_state: 'reviewed', start_us: 0, end_us: 1_000_000 }] }} />);
    expect(screen.getByText('下一步：新建作品方案')).toBeVisible();
    expect(screen.queryByRole('button', { name: '新建作品方案' })).not.toBeInTheDocument();
  });

  it('keeps the user on the story node when creating the child fails', async () => {
    createScriptFromBlueprint.mockImplementationOnce(() => { throw new Error('改编脚本连线失败'); });
    render(<VideoStoryNode id="story-1" type="videoStoryNode" zIndex={0} draggable dragging={false} selected={false} selectable deletable isConnectable positionAbsoluteX={0} positionAbsoluteY={0} data={{ displayName: '原片拆解', rows: [], referenceEvents: [{ start_us: 0, end_us: 1_000_000, narrative_inference: '引出问题' }] }} />);
    act(() => canvasEventBus.publish('remix-node/action', { nodeId: 'story-1', action: 'continue_story' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('改编脚本连线失败');
    expect(screen.queryByRole('dialog', { name: '改编脚本设置' })).toBeNull();
  });

  it('does not ask for review acknowledgements or rewrite nine historical event states', () => {
    const events = Array.from({ length: 9 }, (_, index) => ({ event_id: `pending-${index}`, start_us: index * 1_000_000, end_us: (index + 1) * 1_000_000, review_state: 'needs_review' as const }));
    render(<VideoStoryNode id="story-1" type="videoStoryNode" zIndex={0} draggable dragging={false} selected={false} selectable deletable isConnectable positionAbsoluteX={0} positionAbsoluteY={0} data={{ displayName: '原片拆解', rows: [], referenceEvents: [
      ...events,
    ] }} />);
    expect(screen.queryByRole('button', { name: '全部标记为已核对' })).toBeNull();
    expect(screen.getByText('下一步：新建作品方案')).toBeVisible();
    act(() => canvasEventBus.publish('remix-node/action', { nodeId: 'story-1', action: 'continue_story' }));
    expect(createScriptFromBlueprint).toHaveBeenCalledWith('story-1');
    expect(updateNodeData).not.toHaveBeenCalled();
  });

  it('shows the complete reference-event review UI in fullscreen', async () => {
    render(<VideoStoryNode id="story-1" type="videoStoryNode" zIndex={0} draggable dragging={false} selected={false} selectable deletable isConnectable positionAbsoluteX={0} positionAbsoluteY={0} data={{ displayName: '原片拆解', rows: [{
      shotNumber: 1, startTime: '0', endTime: '1', duration: '1', visualDescription: '完整画面描述', narrative: '旧版叙事内容',
      shotSize: '中景', cameraAngle: '平视', cameraMovement: '固定', focalAndDof: '中等景深', lighting: '室内柔和光',
      backgroundMusic: '轻快音乐', voiceAndSfx: '环境音', imagePrompt: '完整图像提示词', videoMotionPrompt: '完整视频提示词', keyframeUrl: '/keyframe.png',
    }], sourceVideoUrl: '/clip.mp4', referenceEvents: [{
      event_id: 'event-1', start_us: 0, end_us: 1_000_000,
      visual_observation: '人物展示产品', visible_text: '画面字幕', spoken_text: '这是产品台词', narrative_inference: '产品登场', review_state: 'needs_review',
      observed_frame: { time_us: 500_000, pts: 7500, time_base: '1/15000', url: '/frame.png' },
    }] }} />);

    fireEvent.click(screen.getByRole('button', { name: 'node.videoStory.fullscreen' }));
    const dialog = screen.getByRole('dialog', { name: '原片拆解全屏校对' });
    expect(within(dialog).getByRole('table', { name: '原片拆解完整字段' })).toHaveClass('table-fixed');
    expect(within(dialog).getByLabelText('事件 1 画面描述')).toHaveValue('人物展示产品');
    expect(within(dialog).getByText('这是产品台词')).toBeTruthy();
    expect(within(dialog).getByText('产品登场')).toBeTruthy();
    expect(within(dialog).getByText(/PTS 7500 · 1\/15000/)).toBeTruthy();
    const editableValues = () => Array.from(dialog.querySelectorAll<HTMLElement>('.editable-table-cell')).map((cell) => cell.innerText);
    await waitFor(() => expect(editableValues()).toContain('完整画面描述'));
    for (const value of ['旧版叙事内容', '中景', '平视', '固定', '中等景深', '室内柔和光', '轻快音乐', '环境音', '完整图像提示词', '完整视频提示词']) expect(editableValues()).toContain(value);
    fireEvent.change(within(dialog).getByLabelText('事件 1 画面描述'), { target: { value: '人物举着产品，没有展示' } });
    expect(updateNodeData).toHaveBeenCalledWith('story-1', expect.objectContaining({
      referenceEvents: [expect.objectContaining({ event_id: 'event-1', user_visual_observation: '人物举着产品，没有展示', review_state: 'needs_review' })],
      referenceBlueprintRef: null,
    }));
    expect(within(dialog).queryByLabelText('事件 1 核对状态')).toBeNull();
    expect(within(dialog).queryByRole('button', { name: '全部标记为已核对' })).toBeNull();
  });

  it('previews a keyframe and exports that image to a reusable canvas node', () => {
    render(<VideoStoryNode id="story-1" type="videoStoryNode" zIndex={0} draggable dragging={false} selected={false} selectable deletable isConnectable positionAbsoluteX={0} positionAbsoluteY={0} data={{ displayName: '原片拆解', rows: [
      { shotNumber: 1, keyframeUrl: '/static/projects/demo/freezone/frames/one.png' },
      { shotNumber: 2, keyframeUrl: '/static/projects/demo/freezone/frames/two.png' },
    ], referenceEvents: [{ event_id: 'event-1', start_us: 0, end_us: 1_000_000 }] }} />);

    fireEvent.click(screen.getByRole('button', { name: 'node.videoStory.fullscreen' }));
    const table = screen.getByRole('table', { name: '原片拆解完整字段' });
    fireEvent.click(within(table).getByRole('button', { name: '查看关键帧 2' }));
    const preview = screen.getByRole('dialog', { name: '关键帧预览' });
    expect(within(preview).getByRole('img', { name: '第 2 镜关键帧' })).toHaveAttribute('src', '/static/projects/demo/freezone/frames/two.png');
    expect(preview).toHaveFocus();

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: '关键帧预览' })).not.toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: '原片拆解全屏校对' })).toBeVisible();
    fireEvent.click(within(table).getByRole('button', { name: '查看关键帧 2' }));

    fireEvent.click(within(screen.getByRole('dialog', { name: '关键帧预览' })).getByRole('button', { name: '放到画布' }));
    expect(addNode).toHaveBeenCalledWith('uploadNode', { x: 1200, y: 240 }, expect.objectContaining({
      imageUrl: '/static/projects/demo/freezone/frames/two.png',
      imageOnly: true,
      displayName: '原片关键帧 · 第 2 镜',
      sourceStoryNodeId: 'story-1',
    }));
    expect(setSelectedNode).toHaveBeenCalledWith('keyframe-image-1');
    expect(requestFocusNode).toHaveBeenCalledWith('keyframe-image-1');
    expect(screen.queryByRole('dialog', { name: '关键帧预览' })).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: '原片拆解全屏校对' })).not.toBeInTheDocument();
  });

  it('offers keyframe controls in the compact table and skips missing images', () => {
    render(<VideoStoryNode id="story-1" type="videoStoryNode" zIndex={0} draggable dragging={false} selected={false} selectable deletable isConnectable positionAbsoluteX={0} positionAbsoluteY={0} data={{ displayName: '原片拆解', rows: [
      { shotNumber: 1, keyframeUrl: '/frame.png' }, { shotNumber: 2, keyframeUrl: null },
    ] }} />);
    expect(screen.getByRole('button', { name: '查看关键帧 1' })).toBeVisible();
    expect(screen.getByRole('button', { name: '放到画布 · 关键帧 1' })).toBeVisible();
    expect(screen.queryByRole('button', { name: '查看关键帧 2' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '放到画布 · 关键帧 1' }));
    expect(addNode).toHaveBeenCalledWith('uploadNode', expect.any(Object), expect.objectContaining({ imageUrl: '/frame.png', imageOnly: true }));
  });

  it('uses the observed event frame when the analysis has no shot row keyframe', () => {
    render(<VideoStoryNode id="story-1" type="videoStoryNode" zIndex={0} draggable dragging={false} selected={false} selectable deletable isConnectable positionAbsoluteX={0} positionAbsoluteY={0} data={{ displayName: '原片拆解', rows: [], referenceEvents: [
      { event_id: 'event-1', start_us: 0, end_us: 1_000_000, observed_frame: { time_us: 500_000, url: '/observed.png' } },
    ] }} />);
    fireEvent.click(screen.getByRole('button', { name: 'node.videoStory.fullscreen' }));
    const table = screen.getByRole('table', { name: '原片拆解完整字段' });
    expect(within(table).getByRole('button', { name: '查看关键帧 1' })).toBeVisible();
    fireEvent.click(within(table).getByRole('button', { name: '放到画布 · 关键帧 1' }));
    expect(addNode).toHaveBeenCalledWith('uploadNode', expect.any(Object), expect.objectContaining({ imageUrl: '/observed.png' }));
  });
});
