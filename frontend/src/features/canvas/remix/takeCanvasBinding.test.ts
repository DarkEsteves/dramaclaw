import { describe, expect, it, vi } from 'vitest';
import { createPerformanceTakeNode, boundPerformanceTakeMedia, selectedPerformanceTakes, takeReferenceSnapshot } from './takeCanvasBinding';
import type { CanvasNode } from '@/features/canvas/domain/canvasNodes';
import type { RemixPerformanceSegment } from './remixScriptModel';

const segment: RemixPerformanceSegment = { id: 'segment-1', start: 2, end: 5, speaker: '女', source: 'speaking_video', personNodeId: 'person', productNodeId: 'product', settingNodeId: 'setting' };
const node = (id: string, type: CanvasNode['type'], data: Record<string, unknown>) => ({ id, type, position: { x: 0, y: 0 }, data }) as CanvasNode;

describe('performance take canvas binding', () => {
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
