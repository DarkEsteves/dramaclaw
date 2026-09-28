import { describe, expect, it, vi } from 'vitest';
import { createPerformanceAutoBindRetryGate, createPerformanceReferenceNode, legacyPerformanceReferenceLabelPatches, legacyTakeReferencePatch, createPerformanceTakeNode, boundPerformanceTakeMedia, selectedPerformanceTakes, takeBindingsCurrent, takeReferenceSnapshot, performanceTakeAutoBindKey, syncPerformanceReferenceSegment, syncPerformanceReferenceDraft } from './takeCanvasBinding';
import type { CanvasNode, CanvasNodeData } from '@/features/canvas/domain/canvasNodes';
import type { RemixPerformanceSegment } from './remixScriptModel';

const segment: RemixPerformanceSegment = { id: 'segment-1', start: 2, end: 5, speaker: '女', source: 'speaking_video', personNodeId: 'person', productNodeId: 'product', settingNodeId: 'setting' };
const node = (id: string, type: CanvasNode['type'], data: Record<string, unknown>) => ({ id, type, position: { x: 0, y: 0 }, data }) as CanvasNode;

describe('performance take canvas binding', () => {
  it('retries failed automatic binding only when its take or reference inputs change', () => {
    const take = node('take', 'videoNode', { videoUrl: '/take-v1.mp4', remixReferenceNodeIds: ['person'],
      remixReferenceBindings: [{ nodeId: 'person', purpose: '保持角色形象' }] });
    const person = node('person', 'imageGenNode', { imageUrl: '/person-v1.png' });
    const edges = [{ id: 'person-take', source: 'person', target: 'take' }];
    const selected = { ...segment, referenceBindings: [{ nodeId: 'person', purpose: '参考镜头移动' }] };
    const failedKey = performanceTakeAutoBindKey(take, selected, [take, person], edges);
    const gate = createPerformanceAutoBindRetryGate();

    expect(gate.canAttempt(failedKey)).toBe(true);
    gate.recordFailure(failedKey);
    expect(gate.canAttempt(failedKey)).toBe(false);

    expect(performanceTakeAutoBindKey(take, { ...selected }, [take, person], [...edges])).toBe(failedKey);
    const regeneratedKey = performanceTakeAutoBindKey({ ...take, data: { ...take.data, videoUrl: '/take-v2.mp4' } }, selected, [take, person], edges);
    expect(gate.canAttempt(regeneratedKey)).toBe(true);
    expect(performanceTakeAutoBindKey(take, { ...selected, referenceBindings: [{ nodeId: 'person', purpose: '保持角色形象' }] }, [take, person], edges)).not.toBe(failedKey);
    expect(performanceTakeAutoBindKey(take, selected, [take, { ...person, data: { ...person.data, imageUrl: '/person-v2.png' } }], edges)).not.toBe(failedKey);
  });
  it('reads a selected take’s actual canvas links and keeps optional purposes', () => {
    const take = node('take', 'videoNode', { referenceOrder: ['frame', 'voice'], remixReferenceBindings: [{ nodeId: 'frame', purpose: '旧备注' }] });
    const frame = node('frame', 'imageGenNode', { imageUrl: '/frame.png' });
    const voice = node('voice', 'audioNode', { audioUrl: '/voice.wav' });
    const edges = [{ id: 'voice-take', source: 'voice', target: 'take' }, { id: 'frame-take', source: 'frame', target: 'take' }];
    const selected = { ...segment, takeNodeId: 'take', takeMediaRevision: 'a'.repeat(64), referenceBindings: [{ nodeId: 'frame', purpose: '保留画面构图' }] };
    expect(syncPerformanceReferenceSegment(selected, [take, frame, voice], edges)).toEqual({
      ...selected, takeMediaRevision: undefined,
      referenceBindings: [{ nodeId: 'frame', purpose: '保留画面构图' }, { nodeId: 'voice', purpose: '' }],
    });
    const draft = { dialogue: '你好', selections: [], segments: [selected] };
    expect(syncPerformanceReferenceDraft(draft, [take, frame, voice], edges).segments?.[0].referenceBindings).toEqual([
      { nodeId: 'frame', purpose: '保留画面构图' }, { nodeId: 'voice', purpose: '' },
    ]);
    expect(syncPerformanceReferenceDraft({ ...draft, segments: [syncPerformanceReferenceSegment(selected, [take, frame, voice], edges)] }, [take, frame, voice], edges).segments?.[0].takeNodeId).toBe('take');
  });
  it('adopts external links once for an already generated legacy take', () => {
    const take = node('take', 'videoNode', { videoUrl: '/take.mp4', remixReferenceNodeIds: ['old'] });
    const frame = node('frame', 'imageGenNode', { imageUrl: '/frame.png' });
    const voice = node('voice', 'audioNode', { audioUrl: '/voice.wav' });
    const edges = [{ id: 'frame-take', source: 'frame', target: 'take' }, { id: 'voice-take', source: 'voice', target: 'take' }];
    const selected = { ...segment, takeNodeId: 'take', referenceBindings: [{ nodeId: 'frame', purpose: '' }, { nodeId: 'voice', purpose: '' }] };
    const patch = legacyTakeReferencePatch(take, selected, [take, frame, voice], edges);
    expect(patch).toEqual(expect.objectContaining({
      remixReferenceNodeIds: ['frame', 'voice'], remixReferenceBindings: selected.referenceBindings,
      remixReferenceSnapshot: [{ id: 'frame', url: '/frame.png', hash: null }, { id: 'voice', url: '/voice.wav', hash: null }],
    }));
    expect(legacyTakeReferencePatch({ ...take, data: { ...take.data, ...patch } }, selected, [take, frame, voice], edges)).toBeNull();
  });
  it('names generated person references by speaker and segment, including a repeated creation', () => {
    const nodes = [node('script', 'scriptNode', {})];
    let nextId = 0;
    const store = {
      nodes,
      addNode: vi.fn((type: CanvasNode['type'], _position: { x: number; y: number }, data?: Partial<CanvasNodeData>) => {
        const id = `reference-${++nextId}`;
        nodes.push(node(id, type, data ?? {}));
        return id;
      }),
      addEdge: vi.fn(() => 'edge'),
      setSelectedNode: vi.fn(),
      findNodePosition: vi.fn(() => ({ x: 420, y: 0 })),
    };
    const male = { id: 'segment-1', start: 0, end: 4, speaker: '男', source: 'speaking_video' as const };
    const female = { id: 'segment-2', start: 5, end: 9, speaker: '女', source: 'speaking_video' as const };

    createPerformanceReferenceNode(store, 'script', 'person', 'generate', male, 0);
    createPerformanceReferenceNode(store, 'script', 'person', 'generate', female, 1);
    createPerformanceReferenceNode(store, 'script', 'person', 'generate', male, 0);

    expect(nodes.slice(1).map((item) => item.data.displayName)).toEqual([
      '生成人物参考 · 01 男', '生成人物参考 · 02 女', '生成人物参考 · 01 男 · 2',
    ]);
    expect(nodes.slice(1).map((item) => item.data.remixSegmentId)).toEqual(['segment-1', 'segment-2', 'segment-1']);
  });

  it('names a shared person reference for the speaker instead of one segment', () => {
    const addNode = vi.fn(() => 'shared-person');
    createPerformanceReferenceNode({ nodes: [node('script', 'scriptNode', {})], addNode,
      addEdge: vi.fn(() => 'edge'), setSelectedNode: vi.fn(), findNodePosition: vi.fn(() => ({ x: 0, y: 0 })) },
    'script', 'person', 'generate', { id: 'segment-1', start: 0, end: 4, speaker: '男', source: 'speaking_video' }, 0, 'speaker');
    expect(addNode).toHaveBeenCalledWith('imageGenNode', expect.anything(), expect.objectContaining({
      displayName: '生成人物参考 · 男（共用）', remixRole: 'identity', remixIdentityKind: 'person',
    }));
  });

  it('labels bound legacy reference nodes while preserving user-edited titles', () => {
    const nodes = [
      node('script', 'scriptNode', {}),
      node('male', 'imageGenNode', { displayName: '生成人物参考', remixRole: 'identity', remixIdentityKind: 'person' }),
      node('female', 'imageGenNode', { displayName: '生成人物参考', remixRole: 'identity', remixIdentityKind: 'person' }),
      node('custom', 'imageGenNode', { displayName: '女主形象', remixRole: 'identity', remixIdentityKind: 'person' }),
    ];
    const edges = ['male', 'female', 'custom'].map((source) => ({ id: `${source}-script`, source, target: 'script' }));
    const segments = [
      { id: 'segment-1', start: 0, end: 4, speaker: '男', source: 'speaking_video' as const, personNodeId: 'male' },
      { id: 'segment-2', start: 5, end: 9, speaker: '女', source: 'speaking_video' as const, personNodeId: 'female' },
      { id: 'segment-3', start: 10, end: 14, speaker: '女', source: 'speaking_video' as const, personNodeId: 'custom' },
    ];

    expect(legacyPerformanceReferenceLabelPatches(nodes, edges, 'script', segments)).toEqual([
      { id: 'male', displayName: '生成人物参考 · 01 男', remixSegmentId: 'segment-1' },
      { id: 'female', displayName: '生成人物参考 · 02 女', remixSegmentId: 'segment-2' },
    ]);
  });
  it('creates a speaking video with explicit identity references and native speech', () => {
    const addNode = vi.fn(() => 'take');
    const addEdge = vi.fn(() => 'edge');
    const store = { nodes: [node('script', 'scriptNode', {}), node('person', 'imageGenNode', { displayName: '新人物' }), node('product', 'uploadNode', { displayName: '新产品' }), node('setting', 'imageGenNode', { displayName: '新场景' })],
      addNode, addEdge, setSelectedNode: vi.fn(), findNodePosition: vi.fn(() => ({ x: 400, y: 0 })) };
    createPerformanceTakeNode(store, 'script', segment, '女：新品', '新品咖啡', '双人中景，桌面产品特写');
    expect(addNode).toHaveBeenCalledWith('videoNode', expect.anything(), expect.objectContaining({ remixRole: 'performance', remixSegmentId: 'segment-1', generateAudio: true, genMode: 'allReference', prompt: expect.stringContaining('双人中景') }));
    expect(addEdge.mock.calls).toEqual(expect.arrayContaining([['person', 'take'], ['product', 'take'], ['setting', 'take'], ['take', 'script']]));
  });

  it('connects arbitrary media references in order and uses only their stated purposes', () => {
    const addNode = vi.fn((_type: CanvasNode['type'], _position: { x: number; y: number }, _data?: Partial<CanvasNodeData>) => 'take');
    const addEdge = vi.fn(() => 'edge');
    const store = { nodes: [node('script', 'scriptNode', {}), node('camera', 'videoNode', { displayName: '原片镜头' }), node('voice', 'audioNode', { displayName: '声音样本' })],
      addNode, addEdge, setSelectedNode: vi.fn(), findNodePosition: vi.fn(() => ({ x: 400, y: 0 })) };
    createPerformanceTakeNode(store, 'script', { ...segment, referenceBindings: [
      { nodeId: 'camera', purpose: '参考镜头移动' }, { nodeId: 'voice', purpose: '参考语速与音色' },
    ] }, '女：新品', '新品咖啡');
    const take = addNode.mock.calls[0][2] as Record<string, unknown>;
    expect(take.referenceOrder).toEqual(['camera', 'voice']);
    expect(take.remixReferenceNodeIds).toEqual(['camera', 'voice']);
    expect(take.prompt).toContain('参考镜头移动');
    expect(take.prompt).toContain('参考语速与音色');
    expect(take.prompt).not.toContain('替换人物、产品和场景');
    expect(addEdge.mock.calls).toEqual([['camera', 'take'], ['voice', 'take'], ['take', 'script']]);
  });

  it('keeps speakers attached to their lines when a single take covers multiple turns', () => {
    const addNode = vi.fn((_type: CanvasNode['type'], _position: { x: number; y: number }, _data?: Partial<CanvasNodeData>) => 'take');
    createPerformanceTakeNode({ nodes: [node('script', 'scriptNode', {})], addNode, addEdge: vi.fn(() => 'edge'),
      setSelectedNode: vi.fn(), findNodePosition: vi.fn(() => ({ x: 0, y: 0 })) }, 'script',
    { id: 'both', start: 2, end: 11, speaker: '多人', source: 'speaking_video', speechRanges: [{ start: 2, end: 5 }, { start: 8, end: 11 }] },
    '男：你好。\n女：再见。', '问候');
    const prompt = String(addNode.mock.calls[0][2]?.prompt);
    expect(prompt).toContain('男：你好。');
    expect(prompt).toContain('女：再见。');
    expect(prompt).not.toContain('男：你好。\n女：再见。\n男：你好。');
  });

  it('keeps voiceover as an independent audio take', () => {
    const addNode = vi.fn(() => 'voice');
    createPerformanceTakeNode({ nodes: [node('script', 'scriptNode', {})], addNode, addEdge: vi.fn(), setSelectedNode: vi.fn(), findNodePosition: vi.fn(() => ({ x: 0, y: 0 })) },
      'script', { ...segment, start: 0, end: 4, source: 'voiceover' }, '独立旁白', '');
    expect(addNode).toHaveBeenCalledWith('audioNode', expect.anything(), expect.objectContaining({ remixRole: 'take_audio', text: '独立旁白' }));
  });

  it('records ordered references for an audio take without adding them to spoken text', () => {
    const addNode = vi.fn(() => 'voice');
    const addEdge = vi.fn(() => 'edge');
    createPerformanceTakeNode({ nodes: [node('script', 'scriptNode', {}), node('sample', 'audioNode', {})], addNode,
      addEdge, setSelectedNode: vi.fn(), findNodePosition: vi.fn(() => ({ x: 0, y: 0 })) }, 'script',
    { id: 'voice', start: 0, end: 4, speaker: '旁白', source: 'voiceover', referenceBindings: [{ nodeId: 'sample', purpose: '参考音色' }] },
    '独立旁白', '');
    expect(addNode).toHaveBeenCalledWith('audioNode', expect.anything(), expect.objectContaining({ text: '独立旁白',
      remixReferenceBindings: [{ nodeId: 'sample', purpose: '参考音色' }] }));
    expect(addEdge.mock.calls).toContainEqual(['sample', 'voice']);
  });

  it('checks reference identities while allowing an optional purpose annotation', () => {
    const take = node('take', 'videoNode', { remixReferenceNodeIds: ['person'], remixReferenceBindings: [{ nodeId: 'person', purpose: '保持角色形象' }] });
    expect(takeBindingsCurrent(take, { ...segment, referenceBindings: [{ nodeId: 'person', purpose: '保持角色形象' }] })).toBe(true);
    expect(takeBindingsCurrent(take, { ...segment, referenceBindings: [{ nodeId: 'person', purpose: '' }] })).toBe(true);
    expect(takeBindingsCurrent(take, { ...segment, referenceBindings: [] })).toBe(false);
  });

  it('lets the timeline use a take after editing only its reference note', () => {
    const reference = node('person', 'imageGenNode', { imageUrl: '/person.png' });
    const take = node('take', 'videoNode', { videoUrl: '/take.mp4', remixReferenceNodeIds: ['person'],
      remixReferenceBindings: [{ nodeId: 'person', purpose: '保持角色形象' }],
      remixReferenceSnapshot: [{ id: 'person', url: '/person.png', hash: null }],
      remixMedia: { url: '/take.mp4', media_path: 'take.mp4', media_hash: 'a'.repeat(64), media_kind: 'video', has_audio: true } });
    const script = node('script', 'scriptNode', { remixScriptDraft: { segments: [{ ...segment,
      referenceBindings: [{ nodeId: 'person', purpose: '参考镜头移动' }], takeNodeId: 'take', takeMediaRevision: 'a'.repeat(64),
    }] } });
    const edges = [{ id: 'reference-take', source: 'person', target: 'take' }, { id: 'take-script', source: 'take', target: 'script' }];
    expect(selectedPerformanceTakes('demo', [script, take, reference], edges, script)).toHaveLength(1);
  });

  it('does not treat a newly connected reference as part of an older generated video', () => {
    const frame = node('frame', 'imageGenNode', { imageUrl: '/frame.png' });
    const voice = node('voice', 'audioNode', { audioUrl: '/voice.wav' });
    const take = node('take', 'videoNode', { videoUrl: '/take.mp4', remixGenerationReferenceUrl: '/take.mp4',
      remixReferenceNodeIds: ['frame'], remixReferenceSnapshot: [{ id: 'frame', url: '/frame.png', hash: null }],
      remixMedia: { url: '/take.mp4', media_path: 'take.mp4', media_hash: 'a'.repeat(64), media_kind: 'video', has_audio: true } });
    const script = node('script', 'scriptNode', { remixScriptDraft: { segments: [{ ...segment, takeNodeId: 'take',
      takeMediaRevision: 'a'.repeat(64), referenceBindings: [{ nodeId: 'frame', purpose: '' }, { nodeId: 'voice', purpose: '' }] }] } });
    const edges = [{ id: 'frame-take', source: 'frame', target: 'take' }, { id: 'voice-take', source: 'voice', target: 'take' },
      { id: 'take-script', source: 'take', target: 'script' }];
    expect(() => selectedPerformanceTakes('demo', [script, take, frame, voice], edges, script)).toThrow(/参考素材已变化/);
  });

  it('rejects stale and silent speaking video media', () => {
    const video = node('take', 'videoNode', { videoUrl: '/static/projects/demo/freezone/take.mp4', remixMedia: { url: '/static/projects/demo/freezone/take.mp4', media_path: 'freezone/take.mp4', media_hash: 'a'.repeat(64), media_kind: 'video', has_audio: false } });
    expect(() => boundPerformanceTakeMedia('demo', video, 'speaking_video')).toThrow(/声音/);
    video.data.remixMedia = { ...(video.data.remixMedia as object), has_audio: true, url: '/old.mp4' };
    expect(() => boundPerformanceTakeMedia('demo', video, 'speaking_video')).toThrow(/变化/);
  });

  it('invalidates a generated take when its character reference changes', () => {
    const reference = node('person', 'imageGenNode', { imageUrl: '/person-v2.png' });
    const take = node('take', 'videoNode', { videoUrl: '/take.mp4', remixReferenceNodeIds: ['person'],
      remixReferenceSnapshot: [{ id: 'person', url: '/person-v1.png', hash: null }],
      remixMedia: { url: '/take.mp4', media_path: 'take.mp4', media_hash: 'a'.repeat(64), media_kind: 'video', has_audio: true } });
    const script = node('script', 'scriptNode', { remixScriptDraft: { segments: [{ ...segment, takeNodeId: 'take', takeMediaRevision: 'a'.repeat(64) }] } });
    expect(takeReferenceSnapshot([reference], ['person'])).toEqual([{ id: 'person', url: '/person-v2.png', hash: null }]);
    expect(() => selectedPerformanceTakes('demo', [script, take, reference], [{ id: 'take-script', source: 'take', target: 'script' }], script)).toThrow(/参考素材已变化/);
  });

  it('tracks the image input shown by an image generation reference node', () => {
    const reference = node('person', 'imageGenNode', { referenceImageUrl: '/uploaded-person.png' });
    expect(takeReferenceSnapshot([reference], ['person'])).toEqual([{ id: 'person', url: '/uploaded-person.png', hash: null }]);
  });
});
