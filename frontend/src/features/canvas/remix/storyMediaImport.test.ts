import { beforeEach, describe, expect, it, vi } from 'vitest';

import { CANVAS_NODE_TYPES, type CanvasNode } from '@/features/canvas/domain/canvasNodes';
import { useCanvasStore } from '@/stores/canvasStore';

const submitVideoClip = vi.hoisted(() => vi.fn());
vi.mock('@/features/canvas/application/videoClipSubmit', () => ({ submitVideoClip }));

import { importStoryClips, importStoryKeyframes, isStoryClipImportRunning, storyMediaCounts } from './storyMediaImport';

function story(data: Record<string, unknown>): CanvasNode {
  return { id: 'story-1', type: CANVAS_NODE_TYPES.videoStory, position: { x: 0, y: 0 }, data: { rows: [], ...data } } as CanvasNode;
}

beforeEach(() => {
  vi.clearAllMocks();
  useCanvasStore.getState().setCanvasData([], []);
});

describe('story media import', () => {
  it('imports valid distinct keyframes as reusable image nodes and skips already imported frames', () => {
    const source = story({
      rows: [{ shotNumber: 1, keyframeUrl: '/static/one.png' }, { shotNumber: 2, keyframeUrl: '无' }],
      referenceEvents: [
        { event_id: 'a', frame_url: '/static/one.png', start_us: 0, end_us: 1_000_000 },
        { event_id: 'b', observed_frame: { url: '/static/two.png' }, start_us: 1_000_000, end_us: 2_000_000 },
      ],
    });
    useCanvasStore.getState().setCanvasData([
      source,
      { id: 'transcript-1', type: CANVAS_NODE_TYPES.transcript, position: { x: -600, y: 0 }, data: { words: [], status: 'completed' } } as CanvasNode,
      { id: 'video-1', type: CANVAS_NODE_TYPES.video, position: { x: -600, y: 400 }, data: { videoUrl: '/static/original.mp4', aspectRatio: '9:16' } } as CanvasNode,
    ], [
      { id: 'e-transcript', source: 'transcript-1', target: 'story-1' },
      { id: 'e-video', source: 'video-1', target: 'story-1' },
    ]);

    expect(storyMediaCounts(source)).toEqual({ keyframes: 2, clips: 0 });
    const selected = importStoryKeyframes('story-1', { shotNumbers: ['2'] });
    expect(selected.imported).toBe(1);
    expect(selected.skipped).toBe(0);
    expect(selected.createdNodeIds).toHaveLength(1);
    expect(selected.skippedNodeIds).toEqual([]);
    expect(selected.frames).toEqual([
      expect.objectContaining({ shot: '2', url: '/static/two.png', status: 'created', nodeId: selected.createdNodeIds[0] }),
    ]);

    const imported = importStoryKeyframes('story-1');
    expect(imported.imported).toBe(1);
    expect(imported.skipped).toBe(1);
    expect(imported.createdNodeIds).toHaveLength(1);
    expect(imported.skippedNodeIds).toEqual([selected.createdNodeIds[0]]);
    expect(imported.frames).toEqual([
      expect.objectContaining({ shot: '1', url: '/static/one.png', status: 'created', nodeId: imported.createdNodeIds[0] }),
      expect.objectContaining({ shot: '2', url: '/static/two.png', status: 'skipped', nodeId: selected.createdNodeIds[0] }),
    ]);
    const images = useCanvasStore.getState().nodes.filter((node) => node.type === CANVAS_NODE_TYPES.upload);
    expect(images).toHaveLength(2);
    expect(images.map((node) => node.data).sort((left, right) => String(left.imageUrl).localeCompare(String(right.imageUrl)))).toEqual([
      expect.objectContaining({ imageUrl: '/static/one.png', aspectRatio: '9:16', imageOnly: true, sourceStoryNodeId: 'story-1' }),
      expect.objectContaining({ imageUrl: '/static/two.png', aspectRatio: '9:16', imageOnly: true, sourceStoryNodeId: 'story-1' }),
    ]);
    const repeated = importStoryKeyframes('story-1', { shotNumbers: ['2', 'missing'] });
    expect(repeated).toMatchObject({ imported: 0, skipped: 1, createdNodeIds: [], skippedNodeIds: [selected.createdNodeIds[0]] });
    expect(repeated.frames).toEqual([
      expect.objectContaining({ shot: '2', url: '/static/two.png', status: 'skipped', nodeId: selected.createdNodeIds[0] }),
    ]);
  });

  it('cuts each valid reference event with its own source interval and skips imported clips on retry', async () => {
    const source = story({ sourceVideoUrl: '/static/reference.mp4', referenceEvents: [
      { event_id: 'hook', start_us: 500_000, end_us: 2_000_000 },
      { event_id: 'answer', start_us: 2_000_000, end_us: 4_500_000 },
      { event_id: 'invalid', start_us: 5_000_000, end_us: 5_000_000 },
    ] });
    useCanvasStore.getState().setCanvasData([source], []);
    submitVideoClip.mockImplementation(async (_id: string, opts: { displayName: string }) => {
      const id = useCanvasStore.getState().addNode(CANVAS_NODE_TYPES.video, { x: 800, y: 0 }, { videoUrl: `/static/${opts.displayName}.mp4` });
      return { nodeId: id, error: null };
    });

    expect(storyMediaCounts(source)).toEqual({ keyframes: 0, clips: 2 });
    const progress = vi.fn();
    expect(await importStoryClips('story-1', progress)).toEqual({ imported: 2, skipped: 0, failed: 0 });
    expect(submitVideoClip).toHaveBeenNthCalledWith(1, 'story-1', expect.objectContaining({ sourceUrl: '/static/reference.mp4', startMs: 500, endMs: 2000 }));
    expect(submitVideoClip).toHaveBeenNthCalledWith(2, 'story-1', expect.objectContaining({ sourceUrl: '/static/reference.mp4', startMs: 2000, endMs: 4500 }));
    expect(progress).toHaveBeenLastCalledWith(2, 2);
    const clips = useCanvasStore.getState().nodes.filter((node) => node.type === CANVAS_NODE_TYPES.video);
    expect(clips.map((node) => node.data)).toEqual([
      expect.objectContaining({ referenceOnly: true, sourceStoryEventId: 'hook', sourceStoryNodeId: 'story-1' }),
      expect.objectContaining({ referenceOnly: true, sourceStoryEventId: 'answer', sourceStoryNodeId: 'story-1' }),
    ]);
    expect(await importStoryClips('story-1')).toEqual({ imported: 0, skipped: 2, failed: 0 });
    expect(submitVideoClip).toHaveBeenCalledTimes(2);
  });

  it('reports clip failures and allows only failed intervals to be retried', async () => {
    const source = story({ sourceVideoUrl: '/static/reference.mp4', referenceEvents: [
      { event_id: 'one', start_us: 0, end_us: 1_000_000 },
      { event_id: 'two', start_us: 1_000_000, end_us: 2_000_000 },
    ] });
    useCanvasStore.getState().setCanvasData([source], []);
    submitVideoClip.mockResolvedValueOnce({ nodeId: null, error: 'compose failed' });
    submitVideoClip.mockImplementation(async () => ({ nodeId: useCanvasStore.getState().addNode(CANVAS_NODE_TYPES.video, { x: 800, y: 0 }, { videoUrl: '/static/clip.mp4' }), error: null }));

    expect(await importStoryClips('story-1')).toEqual({ imported: 1, skipped: 0, failed: 1 });
    expect(await importStoryClips('story-1')).toEqual({ imported: 1, skipped: 1, failed: 0 });
    expect(submitVideoClip).toHaveBeenCalledTimes(3);
  });

  it('uses timed story rows when reference events are unavailable and rejects a concurrent import', async () => {
    const source = story({ sourceVideoUrl: '/static/reference.mp4', rows: [
      { startTime: '00:01.5', endTime: '00:03.0' },
      { startTime: 'unknown', endTime: '00:04.0' },
    ] });
    useCanvasStore.getState().setCanvasData([source], []);
    let finish!: (value: { nodeId: string | null; error: string | null }) => void;
    submitVideoClip.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));

    expect(storyMediaCounts(source)).toEqual({ keyframes: 0, clips: 1 });
    const pending = importStoryClips('story-1');
    expect(isStoryClipImportRunning('story-1')).toBe(true);
    await expect(importStoryClips('story-1')).rejects.toThrow('片段视频正在导入');
    expect(submitVideoClip).toHaveBeenCalledWith('story-1', expect.objectContaining({ startMs: 1500, endMs: 3000 }));
    finish({ nodeId: null, error: 'failed' });
    expect(await pending).toEqual({ imported: 0, skipped: 0, failed: 1 });
    expect(isStoryClipImportRunning('story-1')).toBe(false);
  });
});
