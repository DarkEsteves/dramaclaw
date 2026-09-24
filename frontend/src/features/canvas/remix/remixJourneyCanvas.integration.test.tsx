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
      video_story: { adaptation_structure: { narrative_role: '通过展示引出核心信息。', reusable_beats: ['展示', '讲解'] } },
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
      : { media_kind: path.endsWith('.mp4') ? 'video' : 'image', media_path: path, media_hash: 'c'.repeat(64), url: `/api/v1/projects/demo/media/${path.split('/').pop()}`, duration_us: 0, has_audio: false, size: 10 }),
    submitRemixProcessAction: vi.fn(async (_project: string, request: Record<string, unknown>) => {
      const action = String(request.action);
      const operationId = `${action}-operation`;
      backend.submit(request);
      if (action === 'analyze_reference_video') {
        backend.operations.set(operationId, { operation_id: operationId, status: 'completed', output_refs: [{ artifact_id: 'evidence', revision: 1 }] });
      } else if (action === 'generate_script_draft') {
        const blueprintRef = request.blueprintRef as { artifactId: string; revision: number };
        const draft = putArtifact('remix_script.v1', {
          dialogue: '新品登场', segments: [], blueprint_mappings: [], media_requirements: [],
          selections: [{ id: 'product', label: '产品登场', start: 0, end: 4 }],
        }, [blueprintRef]);
        backend.operations.set(operationId, {
          operation_id: operationId, status: 'completed',
          input_refs: [{ artifact_id: blueprintRef.artifactId, revision: blueprintRef.revision }],
          output_refs: [{ artifact_id: draft.id, revision: draft.revision }],
        });
      } else if (action === 'plan_broll') {
        const scriptRef = request.scriptRef as { artifactId: string; revision: number };
        const script = backend.artifacts.get(`${scriptRef.artifactId}:${scriptRef.revision}`)!;
        const plan = putArtifact('remix_script.v1', {
          ...(script.payload as object),
          media_requirements: [{ event_id: 'product', required: true, purpose: '展示产品', suggested_query: '产品特写' }],
        }, [scriptRef]);
        backend.operations.set(operationId, {
          operation_id: operationId, status: 'completed',
          input_refs: [{ artifact_id: scriptRef.artifactId, revision: scriptRef.revision }],
          output_refs: [{ artifact_id: plan.id, revision: plan.revision }],
        });
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

  it('shows node actions without a remix journey button', () => {
    render(<JourneyCanvas />);
    expect(screen.getByRole('button', { name: /分析原片/ })).toBeVisible();
    expect(screen.queryByRole('button', { name: /继续改编/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '视频用途' })).not.toBeInTheDocument();
  });

  it('keeps original video analysis available outside the remix reference flow', () => {
    useCanvasStore.getState().updateNodeData('reference', { remixRole: undefined });
    render(<JourneyCanvas />);
    expect(screen.queryByRole('button', { name: /继续改编/ })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /分析原片/ })).toBeVisible();
  });

  it('clicks node actions from uploaded reference through manual audio to a compose manifest', async () => {
    render(<JourneyCanvas />);

    fireEvent.click(await screen.findByRole('button', { name: /分析原片/ }));
    await waitFor(() => expect(useCanvasStore.getState().nodes.find((node) => node.type === 'videoStoryNode')?.data.remixAnalysisStage).toBe('completed'));

    fireEvent.click(await screen.findByRole('button', { name: '生成改编脚本' }));
    await waitFor(() => expect(useCanvasStore.getState().nodes.some((node) => node.type === 'scriptNode')).toBe(true));
    expect(screen.queryByRole('dialog', { name: '改编脚本设置' })).not.toBeInTheDocument();
    await screen.findByRole('dialog', { name: '改编脚本与语义区间' });
    expect(screen.getByText('这些规则只属于当前脚本分支，不会影响原片拆解或其他脚本节点。')).toBeVisible();
    expect(screen.getByLabelText('整体叙事作用')).toHaveValue('通过展示引出核心信息。');
    expect((screen.getByLabelText('保留规则') as HTMLTextAreaElement).value).toContain('展示 → 讲解');
    expect(screen.getByLabelText('替换规则')).toHaveValue('');
    fireEvent.change(await screen.findByLabelText('新主题'), { target: { value: '新品咖啡' } });
    fireEvent.click(screen.getByRole('button', { name: '生成改编台词' }));
    await screen.findByDisplayValue('新品登场');
    expect(screen.getByText(/生成台词不会替你决定口播视频还是独立旁白/)).toBeVisible();
    expect((useCanvasStore.getState().nodes.find((node) => node.type === 'scriptNode')?.data.remixScriptDraft as { segments?: unknown[] })?.segments ?? []).toEqual([]);
    fireEvent.click(await screen.findByRole('button', { name: '上传 产品登场 素材' }));
    const visual = useCanvasStore.getState().nodes.find((node) => node.type === 'uploadNode')!;
    await waitFor(() => expect(useCanvasStore.getState().nodes.find((node) => node.type === 'scriptNode')?.data.remixScriptRef).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: '关闭' }));
    const storyNode = useCanvasStore.getState().nodes.find((node) => node.type === 'videoStoryNode')!;
    const scriptNode = useCanvasStore.getState().nodes.find((node) => node.type === 'scriptNode')!;
    const visualInput = document.querySelector(`input[type="file"]`) as HTMLInputElement;
    fireEvent.change(visualInput, { target: { files: [new File(['png'], 'product.png', { type: 'image/png' })] } });
    await waitFor(() => expect(useCanvasStore.getState().nodes.find((node) => node.id === visual.id)?.data.imageUrl).toContain('broll.png'));
    await waitFor(() => expect((useCanvasStore.getState().nodes.find((node) => node.type === 'scriptNode')?.data.remixScriptDraft as { selections: Array<{ visualMediaRevision?: string }> }).selections[0].visualMediaRevision).toBe('c'.repeat(64)));
    await waitFor(() => expect((useCanvasStore.getState().nodes.find((node) => node.type === 'scriptNode')?.data.remixScriptRef as { revision?: number }).revision).toBe(3));

    act(() => useCanvasStore.getState().setSelectedNode(storyNode.id));
    expect(screen.queryByRole('button', { name: /继续改编/ })).not.toBeInTheDocument();
    act(() => useCanvasStore.getState().setSelectedNode(scriptNode.id));
    fireEvent.click(await screen.findByRole('button', { name: '规划 B-roll' }));
    fireEvent.click(await screen.findByRole('button', { name: '制作配音' }));

    const audio = useCanvasStore.getState().nodes.find((node) => node.type === 'audioNode')!;
    const voice = new File(['wav'], 'voice.wav', { type: 'audio/wav' });
    stashExternalFile('audio-node/external-file', audio.id, voice);
    canvasEventBus.publish('audio-node/external-file', { nodeId: audio.id });
    await waitFor(() => expect(useCanvasStore.getState().nodes.find((node) => node.id === audio.id)?.data.audioUrl).toContain('voice.wav'));
    expect(backend.upload).toHaveBeenCalledWith(voice);

    act(() => useCanvasStore.getState().setSelectedNode(scriptNode.id));
    expect(await screen.findByRole('button', { name: '编辑改编脚本' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '编辑改编脚本' }));
    expect(await screen.findByRole('dialog', { name: '改编脚本与语义区间' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '关闭' }));
    act(() => useCanvasStore.getState().setSelectedNode(audio.id));

    fireEvent.click(await screen.findByRole('button', { name: '按脚本对齐' }));
    await waitFor(() => expect(useCanvasStore.getState().nodes.find((node) => node.type === 'transcriptNode')?.data.status).toBe('completed'));
    act(() => useCanvasStore.getState().setSelectedNode(scriptNode.id));
    fireEvent.click(await screen.findByRole('button', { name: '生成语义时间轴' }));
    await waitFor(() => expect(useCanvasStore.getState().nodes.find((node) => node.type === 'semanticTimelineNode')?.data.artifactRef).toBeTruthy());
    const timelineNode = useCanvasStore.getState().nodes.find((node) => node.type === 'semanticTimelineNode')!;
    act(() => useCanvasStore.getState().setSelectedNode(timelineNode.id));
    fireEvent.click(await screen.findByRole('button', { name: '生成合成清单' }));
    await waitFor(() => expect(useCanvasStore.getState().nodes.find((node) => node.type === 'videoComposeNode')?.data.remixManifestLoadState).toBe('ready'));
    expect(screen.queryByRole('button', { name: /继续改编/ })).not.toBeInTheDocument();
  });

  it('opens the script editor when the next step is to complete required B-roll', async () => {
    backend.artifacts.set('script-artifact:1', {
      id: 'script-artifact', revision: 1, schema: 'remix_script.v1', upstream_refs: [],
      payload: { dialogue: '新品登场', selections: [{ id: 'product', label: '产品登场', start: 0, end: 4 }] },
    });
    backend.artifacts.set('blueprint:1', {
      id: 'blueprint', revision: 1, schema: 'reference_blueprint.v1', upstream_refs: [],
      payload: { narrative_role: '产品问答', preserve_rule: '保留问答结构', replace_rule: '替换产品信息' },
    });
    useCanvasStore.setState({
      nodes: [
        {
          id: 'reference',
          type: 'videoNode',
          position: { x: -800, y: 0 },
          data: { videoUrl: '/reference.mp4', remixRole: 'reference' },
        },
        {
          id: 'story',
          type: 'videoStoryNode',
          position: { x: -400, y: 0 },
          data: { referenceEvents: [{ event_id: 'product' }], referenceBlueprintRef: { artifactId: 'blueprint', revision: 1 } },
        },
        {
          id: 'script',
          type: 'scriptNode',
          position: { x: 0, y: 0 },
          data: {
            remixRole: 'adaptation_script',
            remixTopic: '新品咖啡',
            referenceBlueprintRef: { artifactId: 'blueprint', revision: 1 },
            remixScriptRef: { artifactId: 'script-artifact', revision: 1 },
            remixBrollPlanRef: { artifactId: 'broll-plan', revision: 1 },
            remixBrollScriptRef: { artifactId: 'script-artifact', revision: 1 },
            remixBrollRequirements: [
              { event_id: 'product', required: true },
              { event_id: 'cta', required: true },
            ],
            remixScriptDraft: {
              dialogue: '新品登场\n第二句台词\n第三句台词\n第四句台词',
              selections: [
                { id: 'product', label: '产品登场', start: 0, end: 4, visualNodeId: 'visual', visualMediaRevision: 'a'.repeat(64) },
                { id: 'cta', label: '结尾 CTA', start: 5, end: 9 },
              ],
            },
          },
        },
      ],
      edges: [
        { id: 'reference-story', source: 'reference', target: 'story' },
        { id: 'story-script', source: 'story', target: 'script' },
      ],
      selectedNodeId: 'script',
      history: { past: [], future: [] },
    });

    render(<JourneyCanvas />);
    expect(await screen.findByText('改编主题：新品咖啡')).toBeInTheDocument();
    expect(screen.getByText('2 个事件')).toBeInTheDocument();
    expect(screen.getByText('B-roll 1/2')).toBeInTheDocument();
    expect(screen.getByText('下一步：补齐必需 B-roll')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '编辑改编脚本' })).toBeVisible();
    expect(screen.queryByRole('button', { name: /继续改编/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '编辑改编脚本' }));

    expect(await screen.findByRole('dialog', { name: '改编脚本与语义区间' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '生成 产品登场 图片素材' }));
    expect(useCanvasStore.getState().nodes.find((node) => node.type === 'imageGenNode')?.data.displayName)
      .toBe('生成图片 · 产品登场');
    expect(useCanvasStore.getState().nodes.find((node) => node.id === 'script')?.data.remixBrollRequirements)
      .toEqual([
        { event_id: 'product', required: true },
        { event_id: 'cta', required: true },
      ]);
    await waitFor(() => expect(useCanvasStore.getState().nodes.find((node) => node.id === 'script')?.data.remixScriptRef)
      .toEqual({ artifactId: 'script-artifact', revision: 2 }));
    expect(useCanvasStore.getState().nodes.find((node) => node.id === 'script')?.data.remixBrollPlanRef)
      .toEqual({ artifactId: 'broll-plan', revision: 1 });
    expect(useCanvasStore.getState().nodes.find((node) => node.id === 'script')?.data.remixBrollScriptRef)
      .toEqual({ artifactId: 'script-artifact', revision: 2 });
    expect(screen.getByRole('dialog', { name: '改编脚本与语义区间' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '关闭' }));
    act(() => useCanvasStore.getState().setSelectedNode('script'));
    fireEvent.click(await screen.findByRole('button', { name: '编辑改编脚本' }));
    expect(await screen.findByRole('dialog', { name: '改编脚本与语义区间' })).toBeVisible();
    expect(backend.submit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '关闭' }));
    fireEvent.click(await screen.findByRole('button', { name: '创建主画面节点' }));
    const primary = useCanvasStore.getState().nodes.find((node) => node.data.remixRole === 'primary')!;
    expect(primary.data.displayName).toContain('主画面 · 产品登场');
    act(() => useCanvasStore.getState().setSelectedNode('script'));
    expect(screen.queryByRole('button', { name: '创建主画面节点' })).not.toBeInTheDocument();
    act(() => useCanvasStore.getState().setSelectedNode(primary.id));
    act(() => useCanvasStore.getState().updateNodeData(primary.id, { videoUrl: '/api/v1/projects/demo/media/primary.mp4' }));
    fireEvent.click(await screen.findByRole('button', { name: '关联画面' }));
    await waitFor(() => expect(useCanvasStore.getState().nodes.find((node) => node.id === primary.id)?.data.remixMedia)
      .toMatchObject({ media_kind: 'video', media_hash: 'c'.repeat(64) }));
  });

  it('shows clear empty states in an adaptation script summary', async () => {
    useCanvasStore.setState({
      nodes: [{
        id: 'script',
        type: 'scriptNode',
        position: { x: 0, y: 0 },
        data: {
          remixRole: 'adaptation_script',
          remixScriptDraft: { dialogue: '待编辑台词', selections: [] },
          remixBrollRequirements: [],
        },
      }],
      edges: [],
      selectedNodeId: 'script',
      history: { past: [], future: [] },
    });

    render(<JourneyCanvas />);

    expect(await screen.findByText('改编主题：尚未填写改编主题')).toBeInTheDocument();
    expect(screen.getByText('B-roll 无必需项')).toBeInTheDocument();
    expect(screen.getByText('下一步：编辑改编脚本')).toBeInTheDocument();
  });
});
