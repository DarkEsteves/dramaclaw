import { CANVAS_NODE_TYPES, type CanvasEdge, type CanvasNode } from '@/features/canvas/domain/canvasNodes';

export interface StoryScriptToolbarState {
  label: '生成改编脚本';
  disabled: boolean;
  title?: string;
}

export function storyScriptToolbarState(nodes: CanvasNode[], _edges: CanvasEdge[], storyId: string): StoryScriptToolbarState {
  const story = nodes.find((node) => node.id === storyId && node.type === CANVAS_NODE_TYPES.videoStory);
  const label = '生成改编脚本';

  if (!story || story.data.isAnalyzing || ['preparing', 'extracting', 'transcribing', 'analyzing'].includes(String(story.data.remixAnalysisStage))) {
    return { label, disabled: true, title: '原片分析尚未完成' };
  }
  const events = Array.isArray(story.data.referenceEvents) ? story.data.referenceEvents as Array<{ review_state?: unknown }> : [];
  const pendingCount = events.filter((event) => event.review_state !== 'reviewed' && event.review_state !== 'reviewed_uncertain').length;
  if (pendingCount > 0) return { label, disabled: true, title: `还有 ${pendingCount} 项待核对` };
  const hasEvidence = events.length > 0 || (Array.isArray(story.data.rows) && story.data.rows.length > 0);
  if (!hasEvidence) return { label, disabled: true, title: '请先完成原片分析' };
  return { label, disabled: false, title: undefined };
}
