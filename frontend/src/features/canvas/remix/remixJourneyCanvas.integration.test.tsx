import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ReactFlow, ReactFlowProvider, type NodeTypes } from '@xyflow/react';
import { useMemo } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const backend = vi.hoisted(() => ({
  artifacts: new Map<string, Record<string, unknown>>(),
  operations: new Map<string, Record<string, unknown>>(),
  nextArtifact: 1,
  submit: vi.fn(),
  render: vi.fn(),
  upload: vi.fn(),
}));

vi.mock('@/lib/url-params', () => ({ readUrl: () => ({ project: 'demo', canvas: 'default' }) }));
vi.mock('@/features/canvas/application/imageData', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/features/canvas/application/imageData')>()),
  prepareNodeImageFromFile: vi.fn(async () => ({ aspectRatio: '1:1', previewImageUrl: 'blob:preview' })),
}));
vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>();
  return {
    ...actual,
    apiCall: vi.fn(async (path: string, options?: { json?: Record<string, unknown> }) => {
      const json = options?.json ?? {};
      const expected = Number(json.expected_revision);
      const artifactId = path.split('/artifacts/')[1];
      const current = backend.artifacts.get(`${artifactId}:${expected}`);
      if (!current) throw new Error('missing script revision');
      const revised = { ...current, revision: expected + 1, payload: json.payload, upstream_refs: json.upstream_refs };
      backend.artifacts.set(`${current.id}:${expected + 1}`, revised);
      return revised;
    }),
  };
});
vi.mock('@/api/ops', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/ops')>();
  return {
    ...actual,
    fetchFreezoneAudioReferences: vi.fn(async () => ({ references: [] })),
    uploadFreezoneImage: vi.fn(async (_project: string, file: File) => {
      backend.upload(file);
      return { url: file.type.startsWith('audio/') ? '/api/v1/projects/demo/media/voice.wav' : '/api/v1/projects/demo/media/broll.png' };
    }),
    submitFreezoneAnalyzeVideoStory: vi.fn(async () => ({
      reference_events: [{
        event_id: 'product', start_us: 0, end_us: 2_000_000,
        observed_frame: { pts: 0, time_base: '1/1000', time_us: 0, url: '/frame.png' },
        spoken_range: null, visual_observation: '产品特写', narrative_inference: '产品登场',
        user_narrative_role: '产品登场', review_state: 'reviewed',
      }],
    })),
  };
});
vi.mock('@/api/remix', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/remix')>();
  const putArtifact = (schema: string, payload: Record<string, unknown>, upstream: Array<{ artifactId: string; revision: number }> = []) => {
    const id = `${schema.split('.')[0]}-${backend.nextArtifact++}`;
    const artifact = { id, revision: 1, schema, payload, upstream_refs: upstream.map((ref) => ({ artifact_id: ref.artifactId, revision: ref.revision })) };
    backend.artifacts.set(`${id}:1`, artifact);
    return artifact;
  };
  return {
    ...actual,
    createRemixArtifact: vi.fn(async (_project: string, schema: string, payload: Record<string, unknown>, upstream = []) => putArtifact(schema, payload, upstream)),
    reviseRemixArtifact: vi.fn(async (_project: string, ref: { artifactId: string; revision: number }, payload: Record<string, unknown>, upstream = []) => {
      const previous = backend.artifacts.get(`${ref.artifactId}:${ref.revision}`)!;
      const artifact = { ...previous, revision: ref.revision + 1, payload, upstream_refs: upstream.map((item: { artifactId: string; revision: number }) => ({ artifact_id: item.artifactId, revision: item.revision })) };
      backend.artifacts.set(`${ref.artifactId}:${ref.revision + 1}`, artifact);
      return artifact;
    }),
    fetchRemixArtifact: vi.fn(async (_project: string, ref: { artifactId: string; revision: number }) => backend.artifacts.get(`${ref.artifactId}:${ref.revision}`)),
    generateRemixScriptDraft: vi.fn(async () => ({ dialogue: '新品登场', selections: [{ id: 'product', label: '产品登场', start: 0, end: 4 }] })),
    generateRemixBrollPlan: vi.fn(async (_project: string, ref: { artifactId: string; revision: number }) => {
      const script = backend.artifacts.get(`${ref.artifactId}:${ref.revision}`)!;
      return putArtifact('remix_script.v1', { ...(script.payload as object), media_requirements: [{ event_id: 'product', required: true, purpose: '展示产品', suggested_query: '产品特写' }] }, [ref]);
    }),
    attachRemixMedia: vi.fn(async (_project: string, path: string) => path.endsWith('.wav')
      ? { media_kind: 'audio', media_path: path, media_hash: 'b'.repeat(64), url: `/api/v1/projects/demo/media/${path.split('/').pop()}`, duration_us: 2_000_000, has_audio: true, size: 10 }
      : { media_kind: 'image', media_path: path, media_hash: 'c'.repeat(64), url: `/api/v1/projects/demo/media/${path.split('/').pop()}`, duration_us: 0, has_audio: false, size: 10 }),
    submitRemixProcessAction: vi.fn(async (_project: string, request: Record<string, unknown>) => {
      const action = String(request.action);
      const operationId = `${action}-operation`;
      backend.submit(request);
      if (action === 'analyze_reference_video') {
        backend.operations.set(operationId, { operation_id: operationId, status: 'completed', output_refs: [{ artifact_id: 'evidence', revision: 1 }] });
      } else if (action === 'align_script') {
        const scriptRef = request.scriptRef as { artifactId: string; revision: number };
        const script = backend.artifacts.get(`${scriptRef.artifactId}:${scriptRef.revision}`)!;
        const words = putArtifact('word_timeline.v1', {
          status: 'completed', language: 'zh', media_hash: `sha256:${'b'.repeat(64)}`, script_revision: scriptRef.revision,
          transcript: (script.payload as Record<string, unknown>).dialogue,
          words: [{ text: '新品登场', start_char: 0, end_char: 4, start_us: 0, end_us: 2_000_000, confidence: 1 }],
        }, [scriptRef]);
        backend.operations.set(operationId, { operation_id: operationId, status: 'completed', output_refs: [{ artifact_id: words.id, revision: words.revision }] });
      } else {
        const scriptRef = request.scriptRef as { artifactId: string; revision: number };
        const wordRef = request.wordTimelineRef as { artifactId: string; revision: number };
        const blueprintRef = request.blueprintRef as { artifactId: string; revision: number };
        const timing = putArtifact('performance_timing.v1', {
          media_hash: `sha256:${'b'.repeat(64)}`, fps: 30,
          events: [{ event_id: 'product', start_frame: 0, end_frame: 60 }], unresolved_required_events: [], override_conflicts: [],
        }, [scriptRef, wordRef, blueprintRef]);
        backend.operations.set(operationId, {
          operation_id: operationId, status: 'completed', node_revision: request.nodeRevision,
          input_refs: [scriptRef, wordRef, blueprintRef].map((item) => ({ artifact_id: item.artifactId, revision: item.revision })),
          output_refs: [{ artifact_id: timing.id, revision: timing.revision }],
        });
      }
      return { operation_id: operationId, task_key: operationId, task_type: `remix_${action}`, task_id: operationId };
    }),
    fetchRemixOperation: vi.fn(async (_project: string, id: string) => backend.operations.get(id) ?? { operation_id: id, status: 'pending', output_refs: [] }),
    fetchRemixTaskStatus: vi.fn(async () => ({ status: 'completed' })),
    confirmRemixOperation: vi.fn(async (_project: string, id: string) => backend.operations.get(id)),
    listRemixOperations: vi.fn(async () => ({ items: [] })),
    renderRemixManifest: vi.fn(async (_project: string, request: Record<string, unknown>) => {
      backend.render(request);
      backend.operations.set('render-operation', { operation_id: 'render-operation', status: 'pending', output_refs: [] });
      return { operation_id: 'render-operation', task_key: 'render', task_type: 'remix_render', task_id: 'render' };
    }),
  };
});
vi.mock('@/features/freezone/canvasSyncRuntime', async (importOriginal) => ({ ...(await importOriginal<typeof import('@/features/freezone/canvasSyncRuntime')>()), flushFreezoneCanvasRuntime: vi.fn(async () => true) }));

import { AudioNode } from '@/features/canvas/nodes/AudioNode';
import { ScriptNode } from '@/features/canvas/nodes/ScriptNode';
import { SemanticTimelineNode } from '@/features/canvas/nodes/SemanticTimelineNode';
import { TranscriptNode } from '@/features/canvas/nodes/TranscriptNode';
import { UploadNode } from '@/features/canvas/nodes/UploadNode';
import { VideoComposeNode } from '@/features/canvas/nodes/VideoComposeNode';
import { VideoStoryNode } from '@/features/canvas/nodes/VideoStoryNode';
import { canvasEventBus } from '@/features/canvas/application/canvasServices';
import { stashExternalFile } from '@/features/canvas/application/pendingExternalFiles';
import { NodeActionToolbar } from '@/features/canvas/ui/NodeActionToolbar';
import { useCanvasStore } from '@/stores/canvasStore';

const nodeTypes: NodeTypes = {
  videoNode: () => <div />,
  videoStoryNode: VideoStoryNode,
  scriptNode: ScriptNode,
  uploadNode: UploadNode,
  audioNode: AudioNode,
  transcriptNode: TranscriptNode,
  semanticTimelineNode: SemanticTimelineNode,
  videoComposeNode: VideoComposeNode,
};
const noop = () => {};

function JourneyCanvas() {
  const nodes = useCanvasStore((state) => state.nodes);
  const edges = useCanvasStore((state) => state.edges);
  const selectedId = useCanvasStore((state) => state.selectedNodeId);
  const selected = nodes.find((node) => node.id === selectedId) ?? nodes[0];
  const query = useMemo(() => new QueryClient({ defaultOptions: { queries: { retry: false } } }), []);
  return <QueryClientProvider client={query}><ReactFlowProvider><div style={{ width: 1200, height: 800 }}>
    <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView>
      {selected && <NodeActionToolbar node={selected} onOpenMultiAngleEditor={noop} onOpenLightEditor={noop} onOpenDerivedMedia={noop} onOpenScene360={noop} onOpenUpscale={noop} onOpenOutpaint={noop} onSpawnGridActionNode={noop} onOpenRedraw={noop} onOpenErase={noop} onOpenRotate={noop} />}
    </ReactFlow>
  </div></ReactFlowProvider></QueryClientProvider>;
}

describe('native canvas remix journey integration', () => {
  beforeEach(() => {
    backend.artifacts.clear(); backend.operations.clear(); backend.nextArtifact = 1;
    backend.submit.mockClear(); backend.render.mockClear(); backend.upload.mockClear();
    backend.artifacts.set('evidence:1', { id: 'evidence', revision: 1, schema: 'reference_evidence.v1', upstream_refs: [], payload: { media_hash: `sha256:${'a'.repeat(64)}`, duration_us: 2_000_000, frames: [] } });
    useCanvasStore.setState({
      nodes: [{ id: 'reference', type: 'videoNode', position: { x: 0, y: 0 }, data: { videoUrl: '/api/v1/projects/demo/media/reference.mp4', remixRole: 'reference', remixMedia: { media_kind: 'video', media_path: 'reference.mp4', media_hash: 'a'.repeat(64), url: '/api/v1/projects/demo/media/reference.mp4', duration_us: 2_000_000, has_audio: false, size: 10 } } }],
      edges: [], selectedNodeId: 'reference', history: { past: [], future: [] },
    });
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:preview') });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
    Object.defineProperty(globalThis, 'ResizeObserver', { configurable: true, value: class { observe() {} unobserve() {} disconnect() {} } });
    Object.defineProperty(window, 'DOMMatrixReadOnly', { configurable: true, value: class { m22 = 1 } });
  });

  it('clicks the real toolbar and nodes from uploaded reference through manual audio to render submission', async () => {
    render(<JourneyCanvas />);

    fireEvent.click(await screen.findByRole('button', { name: '继续改编 · 分析参考视频' }));
    await waitFor(() => expect(useCanvasStore.getState().nodes.find((node) => node.type === 'videoStoryNode')?.data.remixAnalysisStage).toBe('completed'));

    fireEvent.click(await screen.findByRole('button', { name: '继续改编 · 确认分析并生成脚本' }));
    await waitFor(() => expect(useCanvasStore.getState().nodes.some((node) => node.type === 'scriptNode')).toBe(true));
    fireEvent.change(await screen.findByLabelText('新主题'), { target: { value: '新品咖啡' } });
    fireEvent.click(screen.getByRole('button', { name: '按蓝图生成' }));
    await screen.findByDisplayValue('新品登场');
    fireEvent.click(screen.getByRole('button', { name: '保存脚本版本' }));

    fireEvent.click(await screen.findByRole('button', { name: '继续改编 · 规划 B-roll' }));
    const storyNode = useCanvasStore.getState().nodes.find((node) => node.type === 'videoStoryNode')!;
    const scriptNode = useCanvasStore.getState().nodes.find((node) => node.type === 'scriptNode')!;
    act(() => useCanvasStore.getState().setSelectedNode(storyNode.id));
    await waitFor(() => expect(screen.queryByRole('button', { name: '继续改编 · 补齐必需 B-roll' })).toBeNull());
    act(() => useCanvasStore.getState().setSelectedNode(scriptNode.id));
    fireEvent.click(await screen.findByRole('button', { name: '继续改编 · 补齐必需 B-roll' }));
    fireEvent.click(await screen.findByRole('button', { name: '上传 产品登场 素材' }));
    const visual = useCanvasStore.getState().nodes.find((node) => node.type === 'uploadNode')!;
    const visualInput = document.querySelector(`input[type="file"]`) as HTMLInputElement;
    fireEvent.change(visualInput, { target: { files: [new File(['png'], 'product.png', { type: 'image/png' })] } });
    await waitFor(() => expect(useCanvasStore.getState().nodes.find((node) => node.id === visual.id)?.data.imageUrl).toContain('broll.png'));

    act(() => useCanvasStore.getState().setSelectedNode(scriptNode.id));
    fireEvent.click(await screen.findByRole('button', { name: /继续改编 · 填写新主题并生成脚本/ }));
    fireEvent.click(await screen.findByRole('button', { name: '替换 产品登场 素材' }));
    await waitFor(() => expect((useCanvasStore.getState().nodes.find((node) => node.type === 'scriptNode')?.data.remixScriptDraft as { selections: Array<{ visualMediaRevision?: string }> }).selections[0].visualMediaRevision).toBe('c'.repeat(64)));
    fireEvent.click(screen.getByRole('button', { name: '保存脚本版本' }));
    fireEvent.click(await screen.findByRole('button', { name: '继续改编 · 规划 B-roll' }));
    fireEvent.click(await screen.findByRole('button', { name: '继续改编 · 创建旁白节点' }));

    const audio = useCanvasStore.getState().nodes.find((node) => node.type === 'audioNode')!;
    const voice = new File(['wav'], 'voice.wav', { type: 'audio/wav' });
    stashExternalFile('audio-node/external-file', audio.id, voice);
    canvasEventBus.publish('audio-node/external-file', { nodeId: audio.id });
    await waitFor(() => expect(useCanvasStore.getState().nodes.find((node) => node.id === audio.id)?.data.audioUrl).toContain('voice.wav'));
    expect(backend.upload).toHaveBeenCalledWith(voice);

    fireEvent.click(await screen.findByRole('button', { name: '继续改编 · 按脚本对齐旁白' }));
    await waitFor(() => expect(useCanvasStore.getState().nodes.find((node) => node.type === 'transcriptNode')?.data.status).toBe('completed'));
    act(() => useCanvasStore.getState().setSelectedNode(scriptNode.id));
    fireEvent.click(await screen.findByRole('button', { name: '继续改编 · 生成语义时间轴' }));
    await waitFor(() => expect(useCanvasStore.getState().nodes.find((node) => node.type === 'semanticTimelineNode')?.data.artifactRef).toBeTruthy());
    act(() => useCanvasStore.getState().setSelectedNode(scriptNode.id));
    fireEvent.click(await screen.findByRole('button', { name: '继续改编 · 添加成片背景 / A-roll' }));
    const background = useCanvasStore.getState().nodes.find((node) => node.data.remixRole === 'background')!;
    const backgroundInput = document.querySelector(`[data-id="${background.id}"] input[type="file"]`) as HTMLInputElement;
    fireEvent.change(backgroundInput, { target: { files: [new File(['png'], 'background.png', { type: 'image/png' })] } });
    await waitFor(() => expect(useCanvasStore.getState().nodes.find((node) => node.id === background.id)?.data.imageUrl).toContain('broll.png'));
    fireEvent.click(await screen.findByRole('button', { name: '继续改编 · 绑定成片背景 / A-roll' }));
    await waitFor(() => expect(useCanvasStore.getState().nodes.find((node) => node.id === background.id)?.data.remixMedia).toBeTruthy());
    const timelineNode = useCanvasStore.getState().nodes.find((node) => node.type === 'semanticTimelineNode')!;
    act(() => useCanvasStore.getState().setSelectedNode(timelineNode.id));
    fireEvent.click(await screen.findByRole('button', { name: '继续改编 · 创建成片清单' }));
    await waitFor(() => expect(useCanvasStore.getState().nodes.find((node) => node.type === 'videoComposeNode')?.data.remixManifestLoadState).toBe('ready'));
    fireEvent.click(await screen.findByRole('button', { name: '继续改编 · 导出 MP4' }));
    await waitFor(() => expect(backend.render).toHaveBeenCalledWith(expect.objectContaining({ manifestRef: expect.any(Object), nodeRevision: expect.any(Number) })));
  });
});
