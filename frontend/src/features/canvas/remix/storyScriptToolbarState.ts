import { CANVAS_NODE_TYPES, type CanvasEdge, type CanvasNode } from '@/features/canvas/domain/canvasNodes';

export interface StoryScriptToolbarState {
  label: '新建作品方案';
  disabled: boolean;
  title?: string;
}

export function storyScriptToolbarState(nodes: CanvasNode[], edges: CanvasEdge[], storyId: string): StoryScriptToolbarState {
  const story = nodes.find((node) => node.id === storyId && node.type === CANVAS_NODE_TYPES.videoStory);
  const label = '新建作品方案';

  if (!story) return { label, disabled: true, title: '找不到原片拆解节点' };
  if (story.data.isAnalyzing || ['preparing', 'extracting', 'transcribing', 'analyzing'].includes(String(story.data.remixAnalysisStage))) {
    return { label, disabled: true, title: '原片分析尚未完成' };
  }
  if (story.data.analysisError || ['failed', 'error'].includes(String(story.data.remixAnalysisStage))) return { label, disabled: true, title: '原片分析失败，请重新分析' };
  const events = Array.isArray(story.data.referenceEvents) ? story.data.referenceEvents : [];
  const hasEvidence = events.length > 0 || (Array.isArray(story.data.rows) && story.data.rows.length > 0);
  if (!hasEvidence) return { label, disabled: true, title: '请先完成原片分析' };
  const hasText = (value: unknown) => typeof value === 'string' && Boolean(value.trim());
  const upstreamIds = new Set(edges.filter((edge) => edge.target === storyId).map((edge) => edge.source));
  const hasSource = hasText(story.data.sourceVideoUrl) || hasText(story.data.sourceMediaPath) || nodes.some((node) => upstreamIds.has(node.id) && node.type === CANVAS_NODE_TYPES.video && hasText(node.data.videoUrl));
  if (!hasSource) return { label, disabled: true, title: '原片素材未绑定，请重新选择原片' };
  return { label, disabled: false, title: undefined };
}
