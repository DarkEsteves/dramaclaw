import { CANVAS_NODE_TYPES, type CanvasEdge, type CanvasNode } from '@/features/canvas/domain/canvasNodes';
import { boundBrollVisualMedia } from './brollCanvasBinding';
import { boundPerformanceTakeMedia, selectedPerformanceTakes } from './takeCanvasBinding';

export type RemixJourneyAction =
  | 'analyze_reference' | 'continue_story' | 'edit_script' | 'create_narration'
  | 'plan_broll' | 'align_narration' | 'create_timeline' | 'compile_timeline'
  | 'create_background' | 'bind_background' | 'create_primary_visuals' | 'generate_primary' | 'bind_primary'
  | 'create_compose' | 'prepare_manifest'
  | 'save_manifest' | 'resolve_manifest_conflicts' | 'render_export';

export type RemixJourneyStep = {
  kind: 'action' | 'input' | 'wait' | 'complete';
  targetNodeId: string;
  label: string;
  inputHint?: string;
  action?: RemixJourneyAction;
};

type Ref = { artifactId: string; revision: number };

function ref(value: unknown): Ref | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as Record<string, unknown>;
  return typeof item.artifactId === 'string' && Number.isInteger(item.revision) && Number(item.revision) > 0
    ? { artifactId: item.artifactId, revision: Number(item.revision) } : null;
}

function sameRef(left: unknown, right: unknown): boolean {
  const a = ref(left); const b = ref(right);
  return Boolean(a && b && a.artifactId === b.artifactId && a.revision === b.revision);
}

function connectedIds(edges: CanvasEdge[], startId: string): Set<string> {
  const found = new Set([startId]);
  const queue = [startId];
  while (queue.length) {
    const current = queue.shift()!;
    for (const edge of edges) {
      const next = edge.source === current ? edge.target : edge.target === current ? edge.source : null;
      if (next && !found.has(next)) { found.add(next); queue.push(next); }
    }
  }
  return found;
}

function directChild(nodes: CanvasNode[], edges: CanvasEdge[], sourceId: string, type: CanvasNode['type']): CanvasNode | undefined {
  const ids = new Set(edges.filter((edge) => edge.source === sourceId).map((edge) => edge.target));
  return nodes.find((node) => ids.has(node.id) && node.type === type);
}

function scriptBranchForSelection(
  nodes: CanvasNode[],
  edges: CanvasEdge[],
  storyId: string,
  selectedNodeId: string,
): CanvasNode | undefined {
  const scriptIds = new Set(edges
    .filter((edge) => edge.source === storyId)
    .map((edge) => edge.target));
  const scripts = nodes.filter((node) => scriptIds.has(node.id) && node.type === CANVAS_NODE_TYPES.script);
  const selectedScript = scripts.find((script) => script.id === selectedNodeId);
  if (selectedScript) return selectedScript;

  for (const script of scripts) {
    const branch = new Set([script.id]);
    const queue = [script.id];
    while (queue.length) {
      const current = queue.shift()!;
      for (const edge of edges) {
        const next = edge.source === current ? edge.target : edge.target === current ? edge.source : null;
        if (!next || next === storyId || (scriptIds.has(next) && next !== script.id) || branch.has(next)) continue;
        branch.add(next);
        queue.push(next);
      }
    }
    if (branch.has(selectedNodeId)) return script;
  }

  return scripts[0];
}

const VISUAL_NODE_TYPES = new Set<CanvasNode['type']>([
  CANVAS_NODE_TYPES.video, CANVAS_NODE_TYPES.upload, CANVAS_NODE_TYPES.imageEdit,
  CANVAS_NODE_TYPES.imageGen, CANVAS_NODE_TYPES.exportImage,
]);

function validBoundVisual(node: CanvasNode | undefined, expectedHash?: unknown): boolean {
  if (!node || !VISUAL_NODE_TYPES.has(node.type)) return false;
  try {
    const media = boundBrollVisualMedia('', node);
    return expectedHash === undefined || media.media_hash === expectedHash;
  } catch {
    return false;
  }
}

function missingRequiredBroll(node: CanvasNode, nodes: CanvasNode[], edges: CanvasEdge[]): boolean {
  const draft = node.data.remixScriptDraft as { selections?: Array<{ id?: unknown; visualNodeId?: unknown; visualMediaRevision?: unknown }> } | undefined;
  const requirements = Array.isArray(node.data.remixBrollRequirements)
    ? node.data.remixBrollRequirements as Array<{ event_id?: unknown; required?: unknown }> : [];
  return requirements.some((requirement) => {
    if (requirement.required !== true || typeof requirement.event_id !== 'string') return false;
    const selection = draft?.selections?.find((item) => item.id === requirement.event_id);
    if (typeof selection?.visualNodeId !== 'string' || typeof selection.visualMediaRevision !== 'string'
      || !/^[a-f0-9]{64}$/u.test(selection.visualMediaRevision)) return true;
    const visual = nodes.find((item) => item.id === selection.visualNodeId);
    return !edges.some((edge) => edge.source === selection.visualNodeId && edge.target === node.id)
      || visual?.data.remixRole !== 'visual'
      || !validBoundVisual(visual, selection.visualMediaRevision);
  });
}

function composeJourneyStep(nodes: CanvasNode[], edges: CanvasEdge[], timeline: CanvasNode): RemixJourneyStep {
  const compose = directChild(nodes, edges, timeline.id, CANVAS_NODE_TYPES.videoCompose);
  if (!compose) return { kind: 'action', action: 'create_compose', targetNodeId: timeline.id, label: '创建成片清单' };
  if (!ref(compose.data.remixManifestRef)) return { kind: 'action', action: 'prepare_manifest', targetNodeId: compose.id, label: '生成合成清单' };
  if (compose.data.remixManifestLoadState === 'failed') return { kind: 'action', action: 'prepare_manifest', targetNodeId: compose.id, label: '重新生成合成清单', inputHint: '旧清单加载失败；将使用当前画布上游重新生成。' };
  if (compose.data.remixManifestLoadState !== 'ready' || !sameRef(compose.data.remixManifestLoadedRef, compose.data.remixManifestRef)) return { kind: 'wait', targetNodeId: compose.id, label: '加载合成清单', inputHint: '清单加载完成后才能检查并导出。' };
  const conflicts = Array.isArray(compose.data.remixManifestConflicts) ? compose.data.remixManifestConflicts : [];
  if (conflicts.length) return { kind: 'action', action: 'resolve_manifest_conflicts', targetNodeId: compose.id, label: '处理清单冲突', inputHint: '逐项选择保留手工值或采用上游值。' };
  if (compose.data.remixManifestDirty === true) return { kind: 'action', action: 'save_manifest', targetNodeId: compose.id, label: '保存合成清单修改' };
  if (compose.data.remixManifestDirty !== false) return { kind: 'input', targetNodeId: compose.id, label: '确认合成清单状态', inputHint: '打开合成节点，等待清单状态同步后再导出。' };
  const unresolved = Array.isArray(compose.data.unresolvedRequiredEvents) ? compose.data.unresolvedRequiredEvents : [];
  if (unresolved.length) return { kind: 'input', targetNodeId: timeline.id, label: '修正未解析事件', inputHint: `仍需修正：${unresolved.join('、')}` };
  if (typeof compose.data.remixInputFingerprint !== 'string' || !compose.data.remixInputFingerprint || compose.data.remixInputsCurrent !== true) return { kind: 'action', action: 'prepare_manifest', targetNodeId: compose.id, label: '上游已变化，更新清单' };
  if (!Number.isInteger(compose.data.remixNodeRevision) || Number(compose.data.remixNodeRevision) < 1) return { kind: 'action', action: 'prepare_manifest', targetNodeId: compose.id, label: '更新合成清单版本' };
  if (compose.data.remixOperationId && !compose.data.resultVideoUrl) return { kind: 'wait', targetNodeId: compose.id, label: '等待成片导出' };
  if (!compose.data.resultVideoUrl) return { kind: 'action', action: 'render_export', targetNodeId: compose.id, label: '导出 MP4' };
  return { kind: 'complete', targetNodeId: compose.id, label: '成片可编辑并可下载 MP4' };
}

export function primaryVisualEventsForScript(script: CanvasNode, nodes: CanvasNode[], edges: CanvasEdge[]): Array<{ id: string; label: string }> {
  const draft = script.data.remixScriptDraft as { selections?: Array<{ id: string; label: string; visualNodeId?: string; visualMediaRevision?: string }> } | undefined;
  return (draft?.selections ?? []).filter((selection) => {
    const selected = nodes.find((candidate) => candidate.id === selection.visualNodeId && candidate.data.remixRole === 'visual');
    return typeof selection.visualMediaRevision !== 'string'
      || !edges.some((edge) => edge.source === selected?.id && edge.target === script.id)
      || !validBoundVisual(selected, selection.visualMediaRevision);
  });
}

/** Resolve exactly one visible next step from the selected remix graph. */
export function nextRemixJourneyStep(nodes: CanvasNode[], edges: CanvasEdge[], selectedNodeId: string): RemixJourneyStep | null {
  const component = connectedIds(edges, selectedNodeId);
  const graph = nodes.filter((node) => component.has(node.id));
  const reference = graph.find((node) => node.type === CANVAS_NODE_TYPES.video && node.data.remixRole === 'reference');
  if (!reference) return null;
  if (!reference.data.videoUrl) return { kind: 'input', targetNodeId: reference.id, label: '上传参考视频', inputHint: '在视频节点上传一个参考视频。' };

  const story = directChild(nodes, edges, reference.id, CANVAS_NODE_TYPES.videoStory);
  if (!story) return { kind: 'action', action: 'analyze_reference', targetNodeId: reference.id, label: '分析参考视频' };
  if (story.data.isAnalyzing || ['preparing', 'extracting', 'transcribing', 'analyzing'].includes(String(story.data.remixAnalysisStage))) {
    return { kind: 'wait', targetNodeId: story.id, label: '等待原片分析', inputHint: '分析完成后可继续检查蓝图。' };
  }
  const hasEvidence = (Array.isArray(story.data.referenceEvents) && story.data.referenceEvents.length > 0)
    || (Array.isArray(story.data.rows) && story.data.rows.length > 0);
  if (!hasEvidence || story.data.analysisError) return { kind: 'action', action: 'analyze_reference', targetNodeId: reference.id, label: '重新分析参考视频' };
  const script = scriptBranchForSelection(nodes, edges, story.id, selectedNodeId);
  if (!script) return { kind: 'action', action: 'continue_story', targetNodeId: story.id, label: '确认分析并生成脚本', inputHint: '新脚本节点会继承分析结果，并在自己的编辑弹窗中设置改编规则。' };
  if (!ref(script.data.remixScriptRef)) return { kind: 'action', action: 'edit_script', targetNodeId: script.id, label: '填写新主题并生成脚本', inputHint: '确认台词、语义区间和素材要求后保存。' };
  if (script.data.remixBrollPlanStatus === 'running') return { kind: 'wait', targetNodeId: script.id, label: '等待 B-roll 规划' };
  if (!ref(script.data.remixBrollPlanRef) || !sameRef(script.data.remixBrollScriptRef, script.data.remixScriptRef)) {
    return { kind: 'action', action: 'plan_broll', targetNodeId: script.id, label: script.data.remixBrollPlanStatus === 'failed' ? '重新规划 B-roll' : '规划 B-roll' };
  }
  if (missingRequiredBroll(script, nodes, edges)) return { kind: 'input', action: 'edit_script', targetNodeId: script.id, label: '补齐必需 B-roll', inputHint: '打开脚本，重新选择当前已连接的替换画面，并确认素材版本。' };

  const performance = (script.data.remixScriptDraft as { segments?: Array<{ id: string; speaker: string; source: 'speaking_video' | 'voiceover' | 'original_sound'; takeNodeId?: string; takeMediaRevision?: string; primaryNodeId?: string; primaryMediaRevision?: string }> } | undefined)?.segments;
  if (performance?.length) {
    const takeBindings: Array<{ segment_id: string; media_path: string; media_hash: string }> = [];
    for (const segment of performance) {
      const take = nodes.find((node) => node.id === segment.takeNodeId && edges.some((edge) => edge.source === node.id && edge.target === script.id));
      if (!take) return { kind: 'action', action: 'edit_script', targetNodeId: script.id, label: `选择 ${segment.speaker} 的表演片段` };
      if (take.data.isGenerating || take.data.workflowActionRunning) return { kind: 'wait', targetNodeId: take.id, label: `等待 ${segment.speaker} 的片段` };
      if (!take.data.videoUrl && !take.data.audioUrl) return { kind: 'input', targetNodeId: take.id, label: `制作 ${segment.speaker} 的${segment.source === 'voiceover' ? '旁白' : '口播视频'}` };
      try {
        const media = boundPerformanceTakeMedia('', take, segment.source);
        if (media.media_hash !== segment.takeMediaRevision) throw new Error('素材未绑定');
        takeBindings.push({ segment_id: segment.id, media_path: media.media_path, media_hash: media.media_hash });
      } catch { return { kind: 'action', action: 'edit_script', targetNodeId: script.id, label: `绑定 ${segment.speaker} 的片段` }; }
      if (segment.source === 'voiceover') {
        const primary = nodes.find((node) => node.id === segment.primaryNodeId && edges.some((edge) => edge.source === node.id && edge.target === script.id));
        if (!primary) return { kind: 'action', action: 'edit_script', targetNodeId: script.id, label: `选择 ${segment.speaker} 的旁白主画面` };
        try { if (boundBrollVisualMedia('', primary).media_hash !== segment.primaryMediaRevision) throw new Error('主画面未绑定'); }
        catch { return { kind: 'action', action: 'edit_script', targetNodeId: script.id, label: `绑定 ${segment.speaker} 的旁白主画面` }; }
      }
    }
    try { selectedPerformanceTakes('', nodes, edges, script); }
    catch { return { kind: 'action', action: 'edit_script', targetNodeId: script.id, label: '检查表演片段和参考素材版本' }; }
    const timeline = nodes.find((node) => node.type === CANVAS_NODE_TYPES.semanticTimeline
      && edges.some((edge) => edge.source === script.id && edge.target === node.id)
      && performance.every((segment) => edges.some((edge) => edge.source === segment.takeNodeId && edge.target === node.id)));
    if (!timeline) return { kind: 'action', action: 'create_timeline', targetNodeId: script.id, label: '按表演片段生成时间轴' };
    if (timeline.data.compileOperationId) return { kind: 'wait', targetNodeId: timeline.id, label: '等待表演时间轴编译' };
    if (!ref(timeline.data.artifactRef) || timeline.data.remixTimingSchema !== 'performance_timing.v2'
      || !sameRef(timeline.data.compiledScriptRef, script.data.remixScriptRef)
      || timeline.data.compiledTakeSignature !== JSON.stringify(takeBindings)) return { kind: 'action', action: 'compile_timeline', targetNodeId: timeline.id, label: '按片段对齐时间轴' };
    if (Array.isArray(timeline.data.events) && timeline.data.events.some((event) => event.invalidReason)) return { kind: 'input', targetNodeId: timeline.id, label: '修正未解析事件' };
    return composeJourneyStep(nodes, edges, timeline);
  }

  const scriptRef = script.data.remixScriptRef;
  const audio = directChild(nodes, edges, script.id, CANVAS_NODE_TYPES.audio);
  if (!audio || audio.data.remixRole !== 'narration' || !sameRef(audio.data.remixScriptRef, scriptRef)) {
    return { kind: 'action', action: 'create_narration', targetNodeId: script.id, label: '创建旁白节点' };
  }
  if (!audio.data.audioUrl) return { kind: 'input', targetNodeId: audio.id, label: '生成或上传旁白音频', inputHint: '可选择音色生成，也可直接上传已经录好的音频。' };

  const transcript = nodes.find((node) => node.type === CANVAS_NODE_TYPES.transcript
    && edges.some((edge) => edge.source === script.id && edge.target === node.id)
    && edges.some((edge) => edge.source === audio.id && edge.target === node.id));
  const currentAlignment = transcript?.data.status === 'completed' && !transcript.data.alignmentStale
    && ref(transcript.data.artifactRef) && sameRef(transcript.data.alignedScriptRef, scriptRef);
  if (!currentAlignment) {
    if (transcript && (transcript.data.operationId || transcript.data.autoStartMode === 'align')) {
      return { kind: 'wait', targetNodeId: transcript.id, label: '等待旁白对齐', inputHint: '对齐完成后会生成逐词时间。' };
    }
    return { kind: 'action', action: 'align_narration', targetNodeId: audio.id, label: '按脚本对齐旁白' };
  }

  const timeline = nodes.find((node) => node.type === CANVAS_NODE_TYPES.semanticTimeline
    && edges.some((edge) => edge.source === script.id && edge.target === node.id)
    && edges.some((edge) => edge.source === transcript.id && edge.target === node.id));
  if (!timeline) return { kind: 'action', action: 'create_timeline', targetNodeId: script.id, label: '生成语义时间轴' };
  if (timeline.data.compileOperationId) return { kind: 'wait', targetNodeId: timeline.id, label: '等待时间轴编译' };
  if (!ref(timeline.data.artifactRef)) return { kind: 'action', action: 'compile_timeline', targetNodeId: timeline.id, label: '编译语义时间轴' };

  const backgrounds = graph.filter((node) => VISUAL_NODE_TYPES.has(node.type) && node.data.remixRole === 'background'
    && node.id !== reference.id && edges.some((edge) => edge.source === node.id && edge.target === script.id));
  const background = backgrounds.find((candidate) => candidate.data.videoUrl || candidate.data.imageUrl || candidate.data.previewImageUrl);
  if (background) {
    if (!validBoundVisual(background)) return { kind: 'action', action: 'bind_background', targetNodeId: background.id, label: '绑定成片底画面' };
  } else {
    const needsPrimary = primaryVisualEventsForScript(script, graph, edges);
    const primaryNodes = graph.filter((candidate) => candidate.type === CANVAS_NODE_TYPES.video && candidate.data.remixRole === 'primary'
      && sameRef(candidate.data.remixScriptRef, scriptRef)
      && edges.some((edge) => edge.source === candidate.id && edge.target === script.id));
    const missing = needsPrimary.filter((selection) => !primaryNodes.some((candidate) => candidate.data.remixEventId === selection.id));
    if (missing.length) return { kind: 'action', action: 'create_primary_visuals', targetNodeId: script.id, label: '创建主画面', inputHint: `为 ${missing.length} 个台词事件准备生成节点。` };
    for (const selection of needsPrimary) {
      const primary = primaryNodes.find((candidate) => candidate.data.remixEventId === selection.id)!;
      if (primary.data.isGenerating || primary.data.workflowActionRunning) return { kind: 'wait', targetNodeId: primary.id, label: `等待主画面 · ${selection.label}` };
      if (!primary.data.imageUrl && !primary.data.videoUrl) return { kind: 'action', action: 'generate_primary', targetNodeId: primary.id, label: `生成主画面视频 · ${selection.label}` };
      if (!validBoundVisual(primary)) return { kind: 'action', action: 'bind_primary', targetNodeId: primary.id, label: `关联主画面 · ${selection.label}` };
    }
  }

  return composeJourneyStep(nodes, edges, timeline);
}
