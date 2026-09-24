import { describe, expect, it, vi } from 'vitest';
import { createPerformanceReferenceNode, legacyPerformanceReferenceLabelPatches, createPerformanceTakeNode, boundPerformanceTakeMedia, selectedPerformanceTakes, takeReferenceSnapshot } from './takeCanvasBinding';
import type { CanvasNode, CanvasNodeData } from '@/features/canvas/domain/canvasNodes';
import type { RemixPerformanceSegment } from './remixScriptModel';

const segment: RemixPerformanceSegment = { id: 'segment-1', start: 2, end: 5, speaker: '女', source: 'speaking_video', personNodeId: 'person', productNodeId: 'product', settingNodeId: 'setting' };
const node = (id: string, type: CanvasNode['type'], data: Record<string, unknown>) => ({ id, type, position: { x: 0, y: 0 }, data }) as CanvasNode;

describe('performance take canvas binding', () => {
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

  it('keeps voiceover as an independent audio take', () => {
    const addNode = vi.fn(() => 'voice');
    createPerformanceTakeNode({ nodes: [node('script', 'scriptNode', {})], addNode, addEdge: vi.fn(), setSelectedNode: vi.fn(), findNodePosition: vi.fn(() => ({ x: 0, y: 0 })) },
      'script', { ...segment, start: 0, end: 4, source: 'voiceover' }, '独立旁白', '');
    expect(addNode).toHaveBeenCalledWith('audioNode', expect.anything(), expect.objectContaining({ remixRole: 'take_audio', text: '独立旁白' }));
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
