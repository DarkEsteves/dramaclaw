import { describe, expect, it, vi } from 'vitest';
import type { CanvasNode } from '@/features/canvas/domain/canvasNodes';
import { attachBrollVisualMedia, boundBrollVisualMedia, createBrollVisualNode, createPrimaryVisualNodes } from './brollCanvasBinding';

const node = (id: string, type: string, data: Record<string, unknown>) => ({ id, type, data, position: { x: 0, y: 0 } }) as CanvasNode;

describe('B-roll canvas binding', () => {
  it('prefills generated primary shots from the adapted dialogue and reference shot observations', () => {
    const addNode = vi.fn().mockReturnValueOnce('first').mockReturnValueOnce('second');
    const store = {
      nodes: [node('script', 'scriptNode', { remixTopic: '把咖啡改成茶', remixScriptRef: { artifactId: 'script', revision: 2 }, remixScriptDraft: { dialogue: '男：喝茶吗？女：每天都喝。', selections: [
        { id: 'question', label: '开场提问', start: 2, end: 6 }, { id: 'answer', label: '回答', start: 8, end: 13 },
      ] } }), node('story', 'videoStoryNode', { referenceEvents: [{ visual_observation: '双人中景' }, { visual_observation: '回答者特写' }] })],
      addNode, addEdge: vi.fn(() => 'edge'), setSelectedNode: vi.fn(), findNodePosition: vi.fn(() => ({ x: 100, y: 200 })),
    };
    const created = createPrimaryVisualNodes(store, 'script', 'story', ['question', 'answer']);
    expect(created).toEqual(['first', 'second']);
    expect(addNode).toHaveBeenNthCalledWith(1, 'imageGenNode', expect.any(Object), expect.objectContaining({
      remixRole: 'primary', remixEventId: 'question', aspectRatio: '9:16', prompt: expect.stringContaining('双人中景'),
    }));
    expect(addNode.mock.calls[0][2].prompt).toContain('把咖啡改成茶');
    expect(addNode.mock.calls[0][2].prompt).toContain('喝茶吗？');
    expect(addNode.mock.calls[0][1]).toEqual({ x: 100, y: 200 });
    expect(addNode.mock.calls[1][1]).toEqual({ x: 100, y: 560 });
  });

  it('reattaches a replacement when the current node URL no longer matches saved remix media', async () => {
    const current = node('visual', 'imageGenNode', {
      imageUrl: '/static/projects/demo/freezone/image/new.png',
      remixRole: 'visual',
      remixMedia: { media_path: 'freezone/image/old.png', media_hash: 'a'.repeat(64), media_kind: 'image' },
    });
    const updateNodeData = vi.fn();
    const attach = vi.fn(async () => ({ media_path: 'freezone/image/new.png', media_hash: 'b'.repeat(64), media_kind: 'image' as const, url: '/static/projects/demo/freezone/image/new.png', size: 1, duration_us: 0 }));

    const attachment = await attachBrollVisualMedia('demo', current, attach, updateNodeData);

    expect(attach).toHaveBeenCalledWith('demo', 'freezone/image/new.png');
    expect(updateNodeData).not.toHaveBeenCalled();
    attachment.commit();
    expect(updateNodeData).toHaveBeenCalledWith('visual', expect.objectContaining({ remixMedia: expect.objectContaining({ media_hash: 'b'.repeat(64) }), remixRole: 'visual' }));
    expect(attachment.mediaRevision).toBe('b'.repeat(64));
  });

  it('creates a normal upload node that accepts image or video and preserves its edge', () => {
    const addNode = vi.fn(() => 'upload-1');
    const addEdge = vi.fn(() => 'edge-1');
    const setSelectedNode = vi.fn();
    const created = createBrollVisualNode({
      nodes: [node('script', 'scriptNode', {})], addNode, addEdge, setSelectedNode,
      findNodePosition: vi.fn(() => ({ x: 100, y: 200 })),
    }, 'script', 'upload', 'product', '产品登场');

    expect(addNode).toHaveBeenCalledWith('uploadNode', { x: 100, y: 200 }, expect.not.objectContaining({ imageOnly: true }));
    expect(addNode).toHaveBeenCalledWith('uploadNode', { x: 100, y: 200 }, expect.objectContaining({
      displayName: '上传画面 · 产品登场',
      remixRole: 'visual',
    }));
    expect(addEdge).toHaveBeenCalledWith('upload-1', 'script');
    expect(created).toEqual({ id: 'upload-1', label: '上传画面 · 产品登场' });
  });

  it('prefills a generated image node with the planned B-roll prompt', () => {
    const addNode = vi.fn(() => 'image-1');
    createBrollVisualNode({
      nodes: [node('script', 'scriptNode', {})], addNode, addEdge: vi.fn(() => 'edge-1'), setSelectedNode: vi.fn(),
      findNodePosition: vi.fn(() => ({ x: 100, y: 200 })),
    }, 'script', 'generate', 'product', '产品登场', '咖啡杯产品特写，柔和侧光');

    expect(addNode).toHaveBeenCalledWith('imageGenNode', { x: 100, y: 200 }, expect.objectContaining({
      displayName: '生成画面 · 产品登场',
      prompt: '咖啡杯产品特写，柔和侧光',
      remixRole: 'visual',
    }));
  });

  it('rejects manifest preparation when the displayed visual replaced the saved media binding', () => {
    const current = node('visual', 'videoNode', {
      videoUrl: '/static/projects/demo/freezone/video/new.mp4',
      remixRole: 'visual',
      remixMedia: { media_path: 'freezone/video/old.mp4', media_hash: 'a'.repeat(64), media_kind: 'video' },
    });
    expect(() => boundBrollVisualMedia('demo', current)).toThrow(/已变化/);
  });

  it('explains that a new background needs an upload before manifest preparation', () => {
    const current = node('background', 'uploadNode', { displayName: '成片背景 / A-roll', remixRole: 'background' });
    expect(() => boundBrollVisualMedia('demo', current)).toThrow('成片背景 / A-roll 尚未上传，请先上传图片或视频');
  });

  it('rejects the old generated image after its prompt changes until regeneration finishes', () => {
    const current = node('visual', 'imageGenNode', {
      prompt: 'new prompt', imageUrl: '/static/projects/demo/freezone/image/old.png', remixRole: 'visual',
      remixVisualOutputCurrent: false,
      remixMedia: { url: '/static/projects/demo/freezone/image/old.png', media_path: 'freezone/image/old.png', media_hash: 'a'.repeat(64), media_kind: 'image' },
    });
    expect(() => boundBrollVisualMedia('demo', current)).toThrow(/重新生成/);
  });
});
