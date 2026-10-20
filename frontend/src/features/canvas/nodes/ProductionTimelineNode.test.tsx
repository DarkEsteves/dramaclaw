import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useSyncExternalStore } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ compileProductionPlan: vi.fn(), createRemixArtifact: vi.fn() }));
const saves = vi.hoisted(() => ({ flushProductionPlanAutoSave: vi.fn(), scheduleProductionPlanAutoSave: vi.fn() }));
const prepare = vi.hoisted(() => ({ prepareNodeRemix: vi.fn() }));
const graph = vi.hoisted(() => ({ nodes: [] as Array<{ id: string; type: string; data: Record<string, unknown> }>,
  edges: [] as Array<{ id?: string; source: string; target: string }>, listeners: new Set<() => void>(), addNode: vi.fn(() => 'compose-1') }));
const notify = () => graph.listeners.forEach((listener) => listener());
const state = {
  get nodes() { return graph.nodes; }, get edges() { return graph.edges; },
  updateNodeData: (id: string, patch: Record<string, unknown>) => {
    const node = graph.nodes.find((item) => item.id === id);
    if (node) { node.data = { ...node.data, ...patch }; notify(); }
  },
  addNode: graph.addNode, addEdge: (source: string, target: string) => { graph.edges.push({ source, target }); notify(); return 'edge'; },
  deleteNodes: vi.fn(), deleteEdge: (edgeId: string) => { graph.edges = graph.edges.filter((edge) => edge.id !== edgeId); notify(); },
  findNodePosition: () => ({ x: 500, y: 0 }),
  setSelectedNode: vi.fn(), requestFocusNode: vi.fn(),
};
vi.mock('@/api/remix', () => api);
vi.mock('@/features/canvas/remix/productionPlanAutoSave', () => saves);
vi.mock('@/features/canvas/remix/prepareNodeRemix', () => ({ remixGraphFingerprint: () => 'current', prepareNodeRemix: prepare.prepareNodeRemix }));
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: Object.assign((selector: (value: typeof state) => unknown) =>
  useSyncExternalStore((listener) => { graph.listeners.add(listener); return () => graph.listeners.delete(listener); }, () => selector(state)),
{ getState: () => state }) }));
vi.mock('@xyflow/react', () => ({ Handle: () => null, Position: { Left: 'left', Right: 'right' } }));
vi.mock('@/features/canvas/ui/NodeHeader', () => ({ NodeHeader: () => null, NODE_HEADER_FLOATING_POSITION_CLASS: '' }));
vi.mock('@/lib/url-params', () => ({ readUrl: () => ({ project: 'demo' }) }));

import { ProductionTimelineNode } from './ProductionTimelineNode';
import { emptyProductionPlan } from '@/features/canvas/remix/productionPlanModel';
import type { SemanticTimelineNodeData } from '@/features/canvas/domain/canvasNodes';

const props = { id: 'timeline-1', type: 'semanticTimelineNode', selected: false, draggable: true,
  dragging: false, selectable: true, deletable: true, zIndex: 0, isConnectable: true,
  positionAbsoluteX: 0, positionAbsoluteY: 0 } as const;
const timelineData = () => graph.nodes[1].data as SemanticTimelineNodeData & { productionPlanNodeId: string };

describe('production timeline', () => {
  beforeEach(() => {
    vi.clearAllMocks(); graph.listeners.clear(); graph.edges = [
      { source: 'image-1', target: 'plan-1' }, { source: 'audio-1', target: 'plan-1' },
      { source: 'plan-1', target: 'timeline-1' },
    ];
    const plan = emptyProductionPlan(); plan.goal = '咖啡短片'; plan.beats[0].purpose = '提出问题';
    plan.beats[0].visuals[0].media = { node_id: 'image-1', media_hash: 'a'.repeat(64), media_path: 'still.png',
      media_kind: 'image', duration_us: 0, has_audio: false };
    plan.beats[0].audio = [{ id: 'voice', role: 'narration', description: '旁白', media: {
      node_id: 'audio-1', media_hash: 'b'.repeat(64), media_path: 'voice.wav', media_kind: 'audio',
      duration_us: 2_000_000, has_audio: true,
    } }];
    graph.nodes = [
      { id: 'plan-1', type: 'scriptNode', data: { remixRole: 'production_plan', productionPlanDraft: plan,
        productionPlanRef: { artifactId: 'plan', revision: 1 }, productionPlanDirty: false } },
      { id: 'timeline-1', type: 'semanticTimelineNode', data: { productionPlanNodeId: 'plan-1',
        productionTimelineOpenOnCreate: true, events: [] } },
      { id: 'image-1', type: 'imageGenNode', data: { imageUrl: '/static/projects/demo/still.png',
        remixMedia: { ...plan.beats[0].visuals[0].media, url: '/static/projects/demo/still.png' } } },
      { id: 'audio-1', type: 'audioNode', data: { audioUrl: '/static/projects/demo/voice.wav',
        remixMedia: { ...plan.beats[0].audio[0].media, url: '/static/projects/demo/voice.wav' } } },
    ];
    graph.addNode.mockImplementation(() => { graph.nodes.push({ id: 'compose-1', type: 'videoComposeNode', data: {} }); notify(); return 'compose-1'; });
    saves.flushProductionPlanAutoSave.mockImplementation(async () => {
      state.updateNodeData('plan-1', { productionPlanRef: { artifactId: 'plan', revision: 2 }, productionPlanDirty: false });
      return { artifactId: 'plan', revision: 2 };
    });
    api.compileProductionPlan.mockResolvedValue({ output_spec: { width: 1080, height: 1920, fps: 30, duration_frames: 60 },
      media_bindings: [], layers: [], audio_events: [], subtitle_events: [], manual_overrides: [], source_versions: [] });
    api.createRemixArtifact.mockResolvedValue({ id: 'manifest', revision: 1 });
    prepare.prepareNodeRemix.mockResolvedValue({ manifestRef: { artifactId: 'manifest', revision: 1 },
      conflicts: [], unresolvedRequiredEvents: [] });
  });

  it('shows the real voice length and lets the user fit the planned beat before compiling', async () => {
    render(<ProductionTimelineNode {...props} data={timelineData()} />);
    const dialog = screen.getByRole('dialog', { name: '成片编排' });
    expect(dialog).toHaveTextContent('提出问题');
    expect(dialog).toHaveTextContent('人声 2.0 秒');
    expect(dialog).toHaveTextContent('计划 3.0 秒');
    fireEvent.click(screen.getByRole('button', { name: '按人声时长调整' }));
    expect((graph.nodes[0].data.productionPlanDraft as ReturnType<typeof emptyProductionPlan>).beats[0].duration_frames).toBe(60);
    expect(saves.scheduleProductionPlanAutoSave).toHaveBeenCalledWith('demo', 'plan-1');
  });

  it('compiles in the arrangement node and only then creates the render node', async () => {
    const view = render(<ProductionTimelineNode {...props} data={timelineData()} />);
    expect(graph.addNode).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '生成合成清单' }));
    await waitFor(() => expect(api.compileProductionPlan).toHaveBeenCalledWith('demo', { artifactId: 'plan', revision: 2 }));
    expect(api.createRemixArtifact).toHaveBeenCalledWith('demo', 'composition_manifest.v1', expect.anything(),
      [{ artifactId: 'plan', revision: 2 }]);
    expect(graph.addNode).not.toHaveBeenCalled();
    view.rerender(<ProductionTimelineNode {...props} data={timelineData()} />);
    fireEvent.click(within(screen.getByRole('dialog', { name: '成片编排' })).getByRole('button', { name: '进入视频合成' }));
    await waitFor(() => expect(graph.addNode).toHaveBeenCalledWith('videoComposeNode', expect.anything(),
      expect.objectContaining({ displayName: '视频合成', remixRole: 'production_compose' })));
    expect(graph.edges).toContainEqual({ source: 'timeline-1', target: 'compose-1' });
  });

  it('reuses an existing direct-compose node and keeps its prior preview while moving the connection', async () => {
    const view = render(<ProductionTimelineNode {...props} data={timelineData()} />);
    fireEvent.click(screen.getByRole('button', { name: '生成合成清单' }));
    await waitFor(() => expect(api.createRemixArtifact).toHaveBeenCalled());
    graph.nodes.push({ id: 'old-compose', type: 'videoComposeNode', data: { remixRole: 'production_compose',
      remixManifestRef: { artifactId: 'old-manifest', revision: 1 }, resultVideoUrl: '/old.mp4' } });
    graph.edges.push({ id: 'plan-1-old-compose', source: 'plan-1', target: 'old-compose' }); notify();
    view.rerender(<ProductionTimelineNode {...props} data={timelineData()} />);
    fireEvent.click(within(screen.getByRole('dialog', { name: '成片编排' })).getByRole('button', { name: '进入视频合成' }));
    await waitFor(() => expect(prepare.prepareNodeRemix).toHaveBeenCalled());
    expect(graph.addNode).not.toHaveBeenCalled();
    expect(graph.edges).toContainEqual({ source: 'timeline-1', target: 'old-compose' });
    expect(graph.edges.find((edge) => edge.source === 'plan-1' && edge.target === 'old-compose')).toBeUndefined();
    expect(graph.nodes.find((node) => node.id === 'old-compose')?.data.resultVideoUrl).toBe('/old.mp4');
  });
});
