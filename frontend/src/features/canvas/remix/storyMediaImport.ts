// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { submitVideoClip } from '@/features/canvas/application/videoClipSubmit';
import { isRenderableImageSrc } from '@/features/canvas/application/imageData';
import { aspectRatioFromImageDimensions } from '@/features/canvas/application/imageNodeSizing';
import { CANVAS_NODE_TYPES, type CanvasNode, type VideoStoryNodeData } from '@/features/canvas/domain/canvasNodes';
import type { ReferenceEvent } from './ReferenceEventList';
import { useCanvasStore } from '@/stores/canvasStore';

interface StoryFrame { url: string; shot: string }
interface StoryClip { key: string; startMs: number; endMs: number; number: number }

function storyData(node: CanvasNode): VideoStoryNodeData {
  return node.data as VideoStoryNodeData;
}

function storyEvents(data: VideoStoryNodeData): ReferenceEvent[] {
  return Array.isArray(data.referenceEvents) ? data.referenceEvents as ReferenceEvent[] : [];
}

function storyFrames(data: VideoStoryNodeData): StoryFrame[] {
  const rows = Array.isArray(data.rows) ? data.rows : [];
  const events = storyEvents(data);
  const frames: StoryFrame[] = [];
  const seen = new Set<string>();
  for (let index = 0; index < Math.max(rows.length, events.length); index += 1) {
    const row = rows[index];
    const event = events[index];
    const url = [row?.keyframeUrl, event?.frame_url, event?.observed_frame?.url]
      .find((candidate): candidate is string => typeof candidate === 'string' && isRenderableImageSrc(candidate));
    if (!url || seen.has(url)) continue;
    seen.add(url);
    frames.push({ url, shot: String(row?.shotNumber ?? index + 1) });
  }
  return frames;
}

function parseTimeMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const parts = value.trim().split(':').map(Number);
  if (!parts.length || parts.some((part) => !Number.isFinite(part))) return null;
  return parts.reduce((seconds, part) => seconds * 60 + part, 0) * 1000;
}

function storyClips(data: VideoStoryNodeData): StoryClip[] {
  if (!data.sourceVideoUrl) return [];
  const events = storyEvents(data);
  const ranges = events.length
    ? events.map((event, index) => ({
      key: event.event_id || `${event.start_us}-${event.end_us}`,
      startMs: Number(event.start_us) / 1000,
      endMs: Number(event.end_us) / 1000,
      number: index + 1,
    }))
    : (Array.isArray(data.rows) ? data.rows : []).map((row, index) => ({
      key: `row-${index + 1}-${row.startTime}-${row.endTime}`,
      startMs: parseTimeMs(row.startTime),
      endMs: parseTimeMs(row.endTime),
      number: index + 1,
    }));
  const seen = new Set<string>();
  return ranges.filter((range): range is StoryClip => {
    if (range.startMs == null || range.endMs == null || !Number.isFinite(range.startMs) || !Number.isFinite(range.endMs) || range.startMs < 0 || range.endMs <= range.startMs || seen.has(range.key)) return false;
    seen.add(range.key);
    return true;
  });
}

function currentStory(storyId: string): CanvasNode {
  const story = useCanvasStore.getState().nodes.find((node) => node.id === storyId);
  if (!story || story.type !== CANVAS_NODE_TYPES.videoStory) throw new Error('原片拆解节点已不存在');
  return story;
}

function sourceAspectRatio(storyId: string): string {
  const store = useCanvasStore.getState();
  const sourceIds = new Set(store.edges.filter((edge) => edge.target === storyId).map((edge) => edge.source));
  const source = store.nodes.find((node) => sourceIds.has(node.id) && node.type === CANVAS_NODE_TYPES.video);
  return source && typeof source.data.aspectRatio === 'string' ? source.data.aspectRatio : '1:1';
}

export function storyMediaCounts(story: CanvasNode): { keyframes: number; clips: number } {
  const data = storyData(story);
  return { keyframes: storyFrames(data).length, clips: storyClips(data).length };
}

export type StoryKeyframeImportOptions = {
  shotNumbers?: readonly string[];
};

export type StoryKeyframeImportResult = {
  imported: number;
  skipped: number;
  createdNodeIds: string[];
  skippedNodeIds: string[];
  frames: Array<{
    shot: string;
    url: string;
    nodeId: string;
    status: 'created' | 'skipped';
  }>;
};

export function importStoryKeyframes(
  storyId: string,
  options: StoryKeyframeImportOptions = {},
): StoryKeyframeImportResult {
  const selectedShots = options.shotNumbers === undefined
    ? null
    : new Set(options.shotNumbers
      .filter((shot): shot is string => typeof shot === 'string')
      .map((shot) => shot.trim())
      .filter(Boolean));
  const frames = storyFrames(storyData(currentStory(storyId)))
    .filter((frame) => selectedShots === null || selectedShots.has(frame.shot));
  let imported = 0;
  let skipped = 0;
  const createdNodeIds: string[] = [];
  const skippedNodeIds: string[] = [];
  const frameResults: StoryKeyframeImportResult['frames'] = [];
  for (const frame of frames) {
    const store = useCanvasStore.getState();
    const existing = store.nodes.find((node) => node.type === CANVAS_NODE_TYPES.upload
      && node.data.sourceStoryNodeId === storyId && node.data.imageUrl === frame.url);
    if (existing) {
      skipped += 1;
      skippedNodeIds.push(existing.id);
      frameResults.push({ shot: frame.shot, url: frame.url, nodeId: existing.id, status: 'skipped' });
      continue;
    }
    const nodeId = store.addNode(CANVAS_NODE_TYPES.upload, store.findNodePosition(storyId, 360, 360), {
      displayName: `原片关键帧 · 第 ${frame.shot} 镜`,
      imageUrl: frame.url,
      previewImageUrl: frame.url,
      aspectRatio: sourceAspectRatio(storyId),
      imageOnly: true,
      sourceStoryNodeId: storyId,
      sourceStoryShotNumber: frame.shot,
    });
    if (typeof Image !== 'undefined') {
      const image = new Image();
      image.onload = () => {
        const ratio = aspectRatioFromImageDimensions(image.naturalWidth, image.naturalHeight);
        if (ratio && useCanvasStore.getState().nodes.some((node) => node.id === nodeId)) {
          useCanvasStore.getState().updateNodeData(nodeId, { aspectRatio: ratio });
        }
      };
      image.src = frame.url;
    }
    imported += 1;
    createdNodeIds.push(nodeId);
    frameResults.push({ shot: frame.shot, url: frame.url, nodeId, status: 'created' });
  }
  return { imported, skipped, createdNodeIds, skippedNodeIds, frames: frameResults };
}

const importingStories = new Set<string>();

export function isStoryClipImportRunning(storyId: string): boolean {
  return importingStories.has(storyId);
}

export async function importStoryClips(storyId: string, onProgress?: (done: number, total: number) => void): Promise<{ imported: number; skipped: number; failed: number }> {
  if (importingStories.has(storyId)) throw new Error('片段视频正在导入');
  const data = storyData(currentStory(storyId));
  const clips = storyClips(data);
  if (!clips.length) return { imported: 0, skipped: 0, failed: 0 };
  importingStories.add(storyId);
  let imported = 0;
  let skipped = 0;
  let failed = 0;
  try {
    for (const clip of clips) {
      const store = useCanvasStore.getState();
      if (store.nodes.some((node) => node.type === CANVAS_NODE_TYPES.video
        && node.data.sourceStoryNodeId === storyId && node.data.sourceStoryEventId === clip.key)) {
        skipped += 1;
      } else {
        const result = await submitVideoClip(storyId, {
          sourceUrl: data.sourceVideoUrl!,
          startMs: clip.startMs,
          endMs: clip.endMs,
          displayName: `原片片段 · ${clip.number}`,
        });
        if (result.nodeId) {
          useCanvasStore.getState().updateNodeData(result.nodeId, {
            referenceOnly: true,
            aspectRatio: sourceAspectRatio(storyId),
            sourceStoryNodeId: storyId,
            sourceStoryEventId: clip.key,
            sourceStartMs: clip.startMs,
            sourceEndMs: clip.endMs,
          });
          imported += 1;
        } else {
          failed += 1;
        }
      }
      onProgress?.(imported + skipped + failed, clips.length);
    }
    return { imported, skipped, failed };
  } finally {
    importingStories.delete(storyId);
  }
}
