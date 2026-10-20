import type { CanvasEdge, CanvasNode } from '@/features/canvas/domain/canvasNodes';
import { CANVAS_NODE_TYPES } from '@/features/canvas/domain/canvasNodes';
import { compileProductionPlan, createRemixArtifact, fetchRemixArtifact, type RemixArtifact, type RemixArtifactRef, type RemixOperation } from '@/api/remix';
import { buildNodeRemixManifest, type CompiledTiming } from './nodeRemixManifest';
import type { UploadedRemixMedia } from './workbenchModel';
import { boundBrollVisualMedia } from './brollCanvasBinding';
import { selectedPerformanceTakes } from './takeCanvasBinding';
import { buildTakeRemixManifest, type TakeTiming } from './takeRemixManifest';
import { reapplyManifestOverrides, type ManifestOverride, type ManifestOverrideConflict, type ManifestPayload } from './manifestEditor';
import { validateProductionPlanBindings, type ProductionPlan } from './productionPlanModel';

function ancestors(nodes: CanvasNode[], edges: CanvasEdge[], targetId: string): CanvasNode[] {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const result: CanvasNode[] = [];
  const seen = new Set<string>([targetId]);
  const queue = [targetId];
  while (queue.length) {
    const target = queue.shift()!;
    for (const edge of edges) {
      if (edge.target !== target || seen.has(edge.source)) continue;
      seen.add(edge.source);
      const node = byId.get(edge.source);
      if (node) { result.push(node); queue.push(node.id); }
    }
  }
  return result;
}

function brollDecisionsFingerprint(data: Record<string, unknown>): Array<[string, boolean]> {
  const decisions = data.remixBrollDecisions;
  if (!decisions || typeof decisions !== 'object' || Array.isArray(decisions)) return [];
  return Object.entries(decisions).filter((entry): entry is [string, boolean] => typeof entry[1] === 'boolean')
    .sort(([left], [right]) => left.localeCompare(right));
}

/** The exact upstream material versions a compose manifest was built from. */
export function remixGraphFingerprint(nodes: CanvasNode[], edges: CanvasEdge[], composeId: string): string {
  const upstream = ancestors(nodes, edges, composeId);
  const included = new Set([composeId, ...upstream.map((node) => node.id)]);
  const artifactRefs = (data: Record<string, unknown>) => {
    const normalized: Record<string, Array<{ artifactId: string; revision: number }>> = {};
    for (const [key, value] of Object.entries(data).sort(([left], [right]) => left.localeCompare(right))) {
      if (!/Refs?$/u.test(key)) continue;
      const values = Array.isArray(value) ? value : [value];
      const refs = values.flatMap((candidate) => {
        if (!candidate || typeof candidate !== 'object') return [];
        const raw = candidate as Record<string, unknown>;
        const artifactId = raw.artifactId ?? raw.artifact_id;
        const revision = raw.revision;
        return typeof artifactId === 'string' && typeof revision === 'number' && Number.isInteger(revision) ? [{ artifactId, revision }] : [];
      });
      if (refs.length) normalized[key] = refs;
    }
    return normalized;
  };
  return JSON.stringify({ nodes: upstream.map((node) => ({
    id: node.id,
    type: node.type,
    mediaHash: 'remixMedia' in node.data ? (node.data.remixMedia as UploadedRemixMedia | undefined)?.media_hash : undefined,
    videoUrl: 'videoUrl' in node.data ? node.data.videoUrl ?? null : undefined,
    imageUrl: 'imageUrl' in node.data ? node.data.imageUrl ?? null : undefined,
    previewImageUrl: 'previewImageUrl' in node.data ? node.data.previewImageUrl ?? null : undefined,
    audioUrl: node.type === CANVAS_NODE_TYPES.audio ? node.data.audioUrl ?? null : undefined,
    role: 'remixRole' in node.data ? node.data.remixRole : undefined,
    brollDecisions: node.type === CANVAS_NODE_TYPES.script ? brollDecisionsFingerprint(node.data as Record<string, unknown>) : undefined,
    generationRequest: node.type === CANVAS_NODE_TYPES.imageGen ? {
      prompt: node.data.prompt ?? '', model: node.data.model ?? null,
      aspectRatio: node.data.aspectRatio ?? null, size: node.data.size ?? null,
      quality: node.data.quality ?? null, modelParams: node.data.modelParams ?? null,
      styleTemplateId: node.data.styleTemplateId ?? null,
      cameraSelection: node.data.cameraSelection ?? null,
      referenceImageUrl: node.data.referenceImageUrl ?? null,
      requestRevision: node.data.remixVisualRequestRevision ?? null,
      outputCurrent: node.data.remixVisualOutputCurrent ?? null,
    } : undefined,
    artifactRefs: artifactRefs(node.data as Record<string, unknown>),
  })).sort((left, right) => left.id.localeCompare(right.id)), edges: edges.filter((edge) => included.has(edge.source) && included.has(edge.target))
    .map((edge) => ({ source: edge.source, target: edge.target, sourceHandle: edge.sourceHandle, targetHandle: edge.targetHandle }))
    .sort((left, right) => `${left.source}:${left.target}:${left.sourceHandle}`.localeCompare(`${right.source}:${right.target}:${right.sourceHandle}`)) });
}

function one<T>(values: T[], label: string): T {
  if (values.length !== 1) throw new Error(`${label}必须且只能有一个`);
  return values[0];
}

export interface PreparedNodeRemix {
  manifestRef: RemixArtifactRef;
  events: Array<{ selectionId: string; startFrame: number; endFrame: number }>;
  selections: Array<{ id: string; label: string; start: number; end: number }>;
  unresolvedRequiredEvents: string[];
  conflicts?: ManifestOverrideConflict[];
}

export function operationReusableForNode(operation: RemixOperation, manifest: RemixArtifact, nodeRevision: number, inputFingerprint = ''): boolean {
  if (!['pending', 'pending_reconciliation', 'needs_confirmation', 'confirming', 'cancelling'].includes(operation.status)
    || operation.producer_version !== 'remix-render.v1'
    || operation.node_revision !== nodeRevision
    || operation.normalized_parameters?.input_fingerprint !== inputFingerprint
    || operation.input_refs?.length !== 1
    || operation.input_refs[0].artifact_id !== manifest.id
    || operation.input_refs[0].revision !== manifest.revision) return false;
  const bindings = Array.isArray(manifest.payload.media_bindings) ? manifest.payload.media_bindings : [];
  const expected = [...new Set(bindings.flatMap((binding) => {
    if (!binding || typeof binding !== 'object') return [];
    const hash = (binding as { media_hash?: unknown }).media_hash;
    return typeof hash === 'string' ? [hash.toLowerCase()] : [];
  }))].sort();
  const recorded = [...new Set(operation.input_media_hashes ?? [])].map((hash) => hash.toLowerCase()).sort();
  return JSON.stringify(recorded) === JSON.stringify(expected);
}

async function prepareTakeRemix(project: string, upstream: CanvasNode[], edges: CanvasEdge[],
  timingNode: CanvasNode, timingArtifact: RemixArtifact, previousManifestRef?: RemixArtifactRef | null): Promise<PreparedNodeRemix> {
  const scriptNode = one(upstream.filter((node) => node.type === CANVAS_NODE_TYPES.script
    && edges.some((edge) => edge.source === node.id && edge.target === timingNode.id)), '改编脚本');
  const story = one(upstream.filter((node) => node.type === CANVAS_NODE_TYPES.videoStory
    && edges.some((edge) => edge.source === node.id && edge.target === scriptNode.id)), '原片拆解');
  const referenceNode = one(upstream.filter((node) => node.type === CANVAS_NODE_TYPES.video && node.data.remixRole === 'reference' && node.data.remixMedia), '参考视频');
  if (!ancestors(upstream, edges, story.id).some((node) => node.id === referenceNode.id)) throw new Error('参考视频未连接到原片拆解');
  const scriptRef = scriptNode.data.remixScriptRef as RemixArtifactRef;
  const blueprintRef = (scriptNode.data.remixBlueprintRef ?? story.data.referenceBlueprintRef) as RemixArtifactRef;
  const timingRef = timingNode.data.artifactRef as RemixArtifactRef;
  if (!scriptRef?.artifactId || !blueprintRef?.artifactId) throw new Error('缺少当前脚本或蓝图版本');
  const [scriptArtifact, blueprintArtifact] = await Promise.all([fetchRemixArtifact(project, scriptRef), fetchRemixArtifact(project, blueprintRef)]);
  if (scriptArtifact.schema !== 'remix_script.v1' || blueprintArtifact.schema !== 'reference_blueprint.v1') throw new Error('脚本或蓝图产物类型不匹配');
  if (blueprintArtifact.payload.media_hash !== (referenceNode.data.remixMedia as UploadedRemixMedia).media_hash) throw new Error('参考蓝图不是由当前原片生成');
  if (!scriptArtifact.upstream_refs?.some((ref) => ref.artifact_id === blueprintRef.artifactId && ref.revision === blueprintRef.revision)) throw new Error('脚本不是由当前参考蓝图生成');
  if (!timingArtifact.upstream_refs?.some((ref) => ref.artifact_id === scriptRef.artifactId && ref.revision === scriptRef.revision)
    || timingArtifact.payload.script_revision !== scriptRef.revision) throw new Error('时间轴不是由当前脚本生成');
  const takes = selectedPerformanceTakes(project, upstream, edges, scriptNode);
  const savedSegments = scriptArtifact.payload.segments as Array<{ segment_id: string; source: string; take_node_id?: string; take_media_revision?: string; primary_node_id?: string; primary_media_revision?: string }>;
  if (!Array.isArray(savedSegments) || takes.length !== savedSegments.length || !takes.every((take, index) =>
    savedSegments[index].segment_id === take.segment.id && savedSegments[index].source === take.segment.source
    && savedSegments[index].take_node_id === take.node.id && savedSegments[index].take_media_revision === take.media.media_hash)) throw new Error('脚本表演片段与当前画布素材不一致');
  const primaryVisuals: Record<string, UploadedRemixMedia> = {};
  for (const segment of savedSegments.filter((row) => row.source === 'voiceover')) {
    const primary = upstream.find((node) => node.id === segment.primary_node_id);
    if (!primary || !edges.some((edge) => edge.source === primary.id && edge.target === scriptNode.id)) throw new Error('独立旁白缺少已连接的主画面');
    const media = boundBrollVisualMedia(project, primary);
    if (media.media_hash !== segment.primary_media_revision) throw new Error('独立旁白主画面已变化，请重新绑定');
    primaryVisuals[segment.segment_id] = media;
  }
  const rules = (scriptArtifact.payload.events as Array<{ event_id: string; visual_node_id?: string }> | undefined) ?? [];
  const visuals: Record<string, UploadedRemixMedia> = {};
  for (const rule of rules.filter((row) => row.visual_node_id)) {
    const visual = upstream.find((node) => node.id === rule.visual_node_id && node.data.remixRole === 'visual');
    if (!visual) throw new Error(`事件 ${rule.event_id} 缺少补充画面`);
    visuals[visual.id] = boundBrollVisualMedia(project, visual);
  }
  const audioTracks = upstream.filter((node) => node.type === CANVAS_NODE_TYPES.audio && (node.data.remixRole === 'bgm' || node.data.remixRole === 'sfx') && node.data.remixMedia)
    .map((node) => ({ id: node.id, role: node.data.remixRole as 'bgm' | 'sfx', media: node.data.remixMedia as UploadedRemixMedia }));
  const timing = timingArtifact.payload as unknown as TakeTiming;
  const payload = buildTakeRemixManifest({ timing, script: scriptArtifact.payload as unknown as Parameters<typeof buildTakeRemixManifest>[0]['script'],
    takes: Object.fromEntries(takes.map((take) => [take.segment.id, take.media])), primaryVisuals, visuals, audioTracks });
  let effectivePayload = payload as ManifestPayload;
  let conflicts: ManifestOverrideConflict[] = [];
  if (previousManifestRef) {
    const previous = await fetchRemixArtifact(project, previousManifestRef);
    if (previous.schema === 'composition_manifest.v1') {
      const reapplied = reapplyManifestOverrides(effectivePayload, (previous.payload.manual_overrides as ManifestOverride[] | undefined) ?? []);
      effectivePayload = reapplied.payload; conflicts = reapplied.conflicts;
    }
  }
  const sourceRefs = [timingRef, scriptRef, blueprintRef];
  const manifest = await createRemixArtifact(project, 'composition_manifest.v1', { ...effectivePayload,
    source_versions: sourceRefs.map((ref) => ({ artifact_id: ref.artifactId, revision: ref.revision })) }, sourceRefs);
  const scriptSelections = (scriptArtifact.payload.selections as Array<{ selection_id: string; start_char: number; end_char: number }>) ?? [];
  return { manifestRef: { artifactId: manifest.id, revision: manifest.revision },
    events: timing.events.map((event) => ({ selectionId: event.event_id, startFrame: event.start_frame, endFrame: event.end_frame })),
    selections: scriptSelections.map((selection) => ({ id: selection.selection_id, label: selection.selection_id, start: selection.start_char, end: selection.end_char })),
    unresolvedRequiredEvents: timing.unresolved_required_events.map((event) => event.event_id),
    ...(conflicts.length ? { conflicts } : {}) };
}

export async function prepareNodeRemix(project: string, nodes: CanvasNode[], edges: CanvasEdge[], composeId: string, previousManifestRef?: RemixArtifactRef | null): Promise<PreparedNodeRemix> {
  const upstream = ancestors(nodes, edges, composeId);
  const arranged = upstream.find((node) => node.type === CANVAS_NODE_TYPES.semanticTimeline
    && node.data.productionPlanNodeId && edges.some((edge) => edge.source === node.id && edge.target === composeId));
  if (arranged) {
    const planNode = upstream.find((node) => node.id === arranged.data.productionPlanNodeId
      && node.type === CANVAS_NODE_TYPES.script && node.data.remixRole === 'production_plan');
    const planRef = planNode?.data.productionPlanRef as RemixArtifactRef | undefined;
    const compiledRef = arranged.data.compiledProductionPlanRef as RemixArtifactRef | undefined;
    const manifestRef = arranged.data.artifactRef as RemixArtifactRef | undefined;
    if (!planNode || !planRef || !compiledRef || !manifestRef || planNode.data.productionPlanDirty
      || arranged.data.productionTimelineDirty || planRef.artifactId !== compiledRef.artifactId
      || planRef.revision !== compiledRef.revision) throw new Error('作品方案已变化，请重新生成成片编排清单');
    const plan = planNode.data.productionPlanDraft as ProductionPlan | undefined;
    if (!plan) throw new Error('作品方案未加载，请稍后再试');
    validateProductionPlanBindings(project, plan, upstream, edges, planNode.id);
    const arrangedManifest = await fetchRemixArtifact(project, manifestRef);
    if (arrangedManifest.schema !== 'composition_manifest.v1' || !arrangedManifest.upstream_refs?.some((ref) =>
      ref.artifact_id === planRef.artifactId && ref.revision === planRef.revision)) {
      throw new Error('成片编排清单与当前作品方案不匹配，请重新生成');
    }
    if (previousManifestRef && (previousManifestRef.artifactId !== manifestRef.artifactId
      || previousManifestRef.revision !== manifestRef.revision)) {
      const previous = await fetchRemixArtifact(project, previousManifestRef);
      const overrides = previous.schema === 'composition_manifest.v1'
        ? (previous.payload.manual_overrides as ManifestOverride[] | undefined) ?? [] : [];
      if (overrides.length) {
        const reapplied = reapplyManifestOverrides(arrangedManifest.payload as ManifestPayload, overrides);
        const saved = await createRemixArtifact(project, 'composition_manifest.v1', reapplied.payload, [planRef]);
        return { manifestRef: { artifactId: saved.id, revision: saved.revision }, events: [], selections: [],
          unresolvedRequiredEvents: [], conflicts: reapplied.conflicts };
      }
    }
    return { manifestRef, events: [], selections: [], unresolvedRequiredEvents: [] };
  }
  const planNodes = upstream.filter((node) => node.type === CANVAS_NODE_TYPES.script && node.data.remixRole === 'production_plan'
    && edges.some((edge) => edge.source === node.id && edge.target === composeId));
  if (planNodes.length) {
    const planNode = one(planNodes, '作品方案');
    const planRef = planNode.data.productionPlanRef as RemixArtifactRef | undefined;
    if (!planRef?.artifactId) throw new Error('请先保存作品方案');
    if (planNode.data.productionPlanDirty) throw new Error('作品方案有未保存的修改');
    const planArtifact = await fetchRemixArtifact(project, planRef);
    if (planArtifact.schema !== 'production_plan.v1') throw new Error('作品方案产物类型不匹配');
    validateProductionPlanBindings(project, planArtifact.payload as unknown as ProductionPlan, upstream, edges, planNode.id);
    let payload = await compileProductionPlan(project, planRef) as ManifestPayload;
    let conflicts: ManifestOverrideConflict[] = [];
    if (previousManifestRef) {
      const previous = await fetchRemixArtifact(project, previousManifestRef);
      if (previous.schema === 'composition_manifest.v1') {
        const reapplied = reapplyManifestOverrides(payload, (previous.payload.manual_overrides as ManifestOverride[] | undefined) ?? []);
        payload = reapplied.payload; conflicts = reapplied.conflicts;
      }
    }
    const manifest = await createRemixArtifact(project, 'composition_manifest.v1', payload, [planRef]);
    return { manifestRef: { artifactId: manifest.id, revision: manifest.revision }, events: [], selections: [],
      unresolvedRequiredEvents: [], ...(conflicts.length ? { conflicts } : {}) };
  }
  const takeTimingNode = upstream.find((node) => node.type === CANVAS_NODE_TYPES.semanticTimeline && node.data.remixTimingSchema === 'performance_timing.v2'
    && node.data.artifactRef && edges.some((edge) => edge.source === node.id && edge.target === composeId));
  if (takeTimingNode) {
    const artifact = await fetchRemixArtifact(project, takeTimingNode.data.artifactRef as RemixArtifactRef);
    if (artifact.schema !== 'performance_timing.v2') throw new Error('表演时间轴产物类型不匹配');
    return prepareTakeRemix(project, upstream, edges, takeTimingNode, artifact, previousManifestRef);
  }
  const reference = one(upstream.filter((node) => node.type === CANVAS_NODE_TYPES.video && node.data.remixRole === 'reference' && node.data.remixMedia), '参考视频').data.remixMedia as UploadedRemixMedia;
  const narrationNodes = upstream.filter((node) => node.type === CANVAS_NODE_TYPES.audio && node.data.remixRole === 'narration');
  if (narrationNodes.length > 1) throw new Error('旁白必须且只能有一个');
  if (narrationNodes.length === 1 && (typeof narrationNodes[0].data.audioUrl !== 'string' || !narrationNodes[0].data.audioUrl)) {
    throw new Error('当前配音没有音频，请先生成或上传配音');
  }
  const narrationMedia = narrationNodes[0]?.data.remixMedia as UploadedRemixMedia | undefined;
  if (narrationNodes.length === 1 && !narrationMedia) throw new Error('当前配音尚未附着媒体，请重新按脚本对齐');
  if (narrationMedia?.url && narrationNodes[0].data.audioUrl !== narrationMedia.url) throw new Error('当前配音音频已变化，请重新按脚本对齐');
  const narration = narrationMedia ?? (reference.has_audio ? reference : null);
  if (!narration) throw new Error('缺少实际旁白音轨');
  const timingNode = one(upstream.filter((node) => node.type === CANVAS_NODE_TYPES.semanticTimeline && node.data.artifactRef && edges.some((edge) => edge.source === node.id && edge.target === composeId)), '语义时间轴');
  const scriptNode = one(upstream.filter((node) => node.type === CANVAS_NODE_TYPES.script && node.data.remixScriptRef && edges.some((edge) => edge.source === node.id && edge.target === timingNode.id)), '改编脚本');
  const transcriptNode = one(upstream.filter((node) => node.type === CANVAS_NODE_TYPES.transcript && node.data.artifactRef && edges.some((edge) => edge.source === node.id && edge.target === timingNode.id)), '对齐转写');
  const blueprintNode = one(upstream.filter((node) => node.type === CANVAS_NODE_TYPES.videoStory && edges.some((edge) => edge.source === node.id && edge.target === scriptNode.id)), '原片拆解');
  const timingRef = timingNode.data.artifactRef as RemixArtifactRef;
  const scriptRef = scriptNode.data.remixScriptRef as RemixArtifactRef;
  const transcriptRef = transcriptNode.data.artifactRef as RemixArtifactRef;
  const blueprintRef = (scriptNode.data.remixBlueprintRef ?? blueprintNode.data.referenceBlueprintRef) as RemixArtifactRef;
  if (!blueprintRef?.artifactId || !blueprintRef.revision) throw new Error('当前脚本缺少改编规则版本');
  const [timingArtifact, scriptArtifact, blueprintArtifact] = await Promise.all([fetchRemixArtifact(project, timingRef), fetchRemixArtifact(project, scriptRef), fetchRemixArtifact(project, blueprintRef)]);
  if (timingArtifact.schema !== 'performance_timing.v1' || scriptArtifact.schema !== 'remix_script.v1' || blueprintArtifact.schema !== 'reference_blueprint.v1') throw new Error('上游产物类型不匹配');
  if (blueprintArtifact.payload.media_hash !== reference.media_hash) throw new Error('参考蓝图不是由当前原片生成');
  if (!scriptArtifact.upstream_refs?.some((ref) => ref.artifact_id === blueprintRef.artifactId && ref.revision === blueprintRef.revision)) throw new Error('脚本不是由当前参考蓝图生成');
  const compiledMediaHash = timingArtifact.payload.media_hash;
  if (compiledMediaHash !== narration.media_hash && compiledMediaHash !== `sha256:${narration.media_hash}`) throw new Error('语义时间轴对应的语音已变化，请重新对齐并编译');
  const timingSources = timingArtifact.upstream_refs;
  if (!timingSources?.some((ref) => ref.artifact_id === scriptRef.artifactId && ref.revision === scriptRef.revision)) throw new Error('时间轴不是由当前脚本版本编译');
  if (!timingSources.some((ref) => ref.artifact_id === transcriptRef.artifactId && ref.revision === transcriptRef.revision)) throw new Error('时间轴不是由当前对齐版本编译');
  const transcriptArtifact = await fetchRemixArtifact(project, transcriptRef);
  if (transcriptArtifact.schema !== 'word_timeline.v1' || transcriptArtifact.payload.status !== 'completed') throw new Error('当前对齐转写未完成');
  if (!transcriptArtifact.upstream_refs?.some((ref) => ref.artifact_id === scriptRef.artifactId && ref.revision === scriptRef.revision)
    || transcriptArtifact.payload.script_revision !== scriptRef.revision
    || transcriptArtifact.payload.transcript !== scriptArtifact.payload.dialogue) throw new Error('对齐转写不是由当前脚本生成');
  const transcriptMediaHash = transcriptArtifact.payload.media_hash;
  if (transcriptMediaHash !== narration.media_hash && transcriptMediaHash !== `sha256:${narration.media_hash}`) throw new Error('对齐转写对应的语音已变化');
  const visualTypes = new Set<string>([CANVAS_NODE_TYPES.video, CANVAS_NODE_TYPES.upload, CANVAS_NODE_TYPES.imageEdit, CANVAS_NODE_TYPES.imageGen, CANVAS_NODE_TYPES.exportImage]);
  const backgroundNodes = upstream.filter((node) => visualTypes.has(node.type)
    && (node.data.remixRole === 'background' || node.data.remixRole === 'a-roll')
    && edges.some((edge) => edge.source === node.id && edge.target === scriptNode.id)
    && (node.data.videoUrl || node.data.imageUrl || node.data.previewImageUrl));
  if (backgroundNodes.length > 1) throw new Error('成片底画面只能选择一个');
  const background = backgroundNodes.length ? boundBrollVisualMedia(project, backgroundNodes[0]) : undefined;
  const eventRules = (scriptArtifact.payload.events as Array<{ event_id: string; visual_node_id?: string }> | undefined) ?? [];
  const referencedVisualIds = new Set(eventRules.map((event) => event.visual_node_id).filter(Boolean));
  const visuals = Object.fromEntries(upstream.filter((node) => visualTypes.has(node.type) && node.data.remixRole === 'visual' && referencedVisualIds.has(node.id))
    .map((node) => [node.id, boundBrollVisualMedia(project, node)]));
  const primaryNodes = upstream.filter((node) => node.type === CANVAS_NODE_TYPES.video && node.data.remixRole === 'primary'
    && edges.some((edge) => edge.source === node.id && edge.target === scriptNode.id)
    && (node.data.remixScriptRef as RemixArtifactRef | undefined)?.artifactId === scriptRef.artifactId
    && (node.data.remixScriptRef as RemixArtifactRef | undefined)?.revision === scriptRef.revision
    && eventRules.some((event) => event.event_id === node.data.remixEventId && !event.visual_node_id));
  if (new Set(primaryNodes.map((node) => node.data.remixEventId)).size !== primaryNodes.length) throw new Error('同一台词事件存在多个主画面节点');
  const primaryVisuals = Object.fromEntries(primaryNodes.map((node) => [String(node.data.remixEventId), boundBrollVisualMedia(project, node)]));
  const audioTracks = upstream.filter((node) => node.type === CANVAS_NODE_TYPES.audio && (node.data.remixRole === 'bgm' || node.data.remixRole === 'sfx') && node.data.remixMedia)
    .map((node) => ({ id: node.id, role: node.data.remixRole as 'bgm' | 'sfx', media: node.data.remixMedia as UploadedRemixMedia }));
  const payload = buildNodeRemixManifest({
    reference, background, narration, visuals, primaryVisuals, audioTracks,
    timing: timingArtifact.payload as unknown as CompiledTiming,
    script: scriptArtifact.payload as unknown as Parameters<typeof buildNodeRemixManifest>[0]['script'],
    wordTimeline: transcriptArtifact.payload as unknown as Parameters<typeof buildNodeRemixManifest>[0]['wordTimeline'],
  });
  let effectivePayload = payload as ManifestPayload;
  let conflicts: ManifestOverrideConflict[] = [];
  if (previousManifestRef) {
    const previous = await fetchRemixArtifact(project, previousManifestRef);
    if (previous.schema === 'composition_manifest.v1') {
      const reapplied = reapplyManifestOverrides(effectivePayload, (previous.payload.manual_overrides as ManifestOverride[] | undefined) ?? []);
      effectivePayload = reapplied.payload;
      conflicts = reapplied.conflicts;
    }
  }
  const manifest = await createRemixArtifact(project, 'composition_manifest.v1', {
    ...effectivePayload,
    source_versions: [{ artifact_id: timingRef.artifactId, revision: timingRef.revision }, { artifact_id: scriptRef.artifactId, revision: scriptRef.revision }, { artifact_id: blueprintRef.artifactId, revision: blueprintRef.revision }, { artifact_id: transcriptRef.artifactId, revision: transcriptRef.revision }],
  }, [timingRef, scriptRef, blueprintRef, transcriptRef]);
  const timingEvents = (timingArtifact.payload.events as CompiledTiming['events']) ?? [];
  const scriptEvents = (scriptArtifact.payload.events as Array<{ event_id: string; caption?: string }>) ?? [];
  const scriptSelections = (scriptArtifact.payload.selections as Array<{ selection_id: string; start_char: number; end_char: number }>) ?? [];
  return {
    manifestRef: { artifactId: manifest.id, revision: manifest.revision },
    events: timingEvents.map((event) => ({ selectionId: event.event_id, startFrame: event.start_frame, endFrame: event.end_frame })),
    selections: scriptSelections.map((selection) => ({ id: selection.selection_id, label: scriptEvents.find((event) => event.event_id === selection.selection_id)?.caption ?? selection.selection_id, start: selection.start_char, end: selection.end_char })),
    unresolvedRequiredEvents: (timingArtifact.payload.unresolved_required_events as Array<{ event_id: string }> | undefined)?.map((event) => event.event_id) ?? [],
    ...(conflicts.length ? { conflicts } : {}),
  };
}
