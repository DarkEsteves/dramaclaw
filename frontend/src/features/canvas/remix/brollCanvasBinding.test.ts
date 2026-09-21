import { describe, expect, it, vi } from 'vitest';
import type { CanvasNode } from '@/features/canvas/domain/canvasNodes';
import { attachBrollVisualMedia, boundBrollVisualMedia, createBrollVisualNode } from './brollCanvasBinding';

const node = (id: string, type: string, data: Record<string, unknown>) => ({ id, type, data, position: { x: 0, y: 0 } }) as CanvasNode;

describe('B-roll canvas binding', () => {
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
    }, 'script', 'upload', 'product');

    expect(addNode).toHaveBeenCalledWith('uploadNode', { x: 100, y: 200 }, expect.not.objectContaining({ imageOnly: true }));
    expect(addNode).toHaveBeenCalledWith('uploadNode', { x: 100, y: 200 }, expect.objectContaining({ remixRole: 'visual' }));
    expect(addEdge).toHaveBeenCalledWith('upload-1', 'script');
    expect(created).toEqual({ id: 'upload-1', label: '上传画面' });
  });

  it('rejects manifest preparation when the displayed visual replaced the saved media binding', () => {
    const current = node('visual', 'videoNode', {
      videoUrl: '/static/projects/demo/freezone/video/new.mp4',
      remixRole: 'visual',
      remixMedia: { media_path: 'freezone/video/old.mp4', media_hash: 'a'.repeat(64), media_kind: 'video' },
    });
    expect(() => boundBrollVisualMedia('demo', current)).toThrow(/已变化/);
  });
});
