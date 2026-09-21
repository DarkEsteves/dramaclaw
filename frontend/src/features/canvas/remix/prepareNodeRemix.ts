import type { CanvasEdge, CanvasNode } from '@/features/canvas/domain/canvasNodes';
import { CANVAS_NODE_TYPES } from '@/features/canvas/domain/canvasNodes';
import { createRemixArtifact, fetchRemixArtifact, type RemixArtifactRef } from '@/api/remix';
import { buildNodeRemixManifest, type CompiledTiming } from './nodeRemixManifest';
import type { UploadedRemixMedia } from './workbenchModel';
import { boundBrollVisualMedia } from './brollCanvasBinding';
import { reapplyManifestOverrides, type ManifestOverride, type ManifestPayload } from './manifestEditor';

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

/** The exact upstream material versions a compose manifest was built from. */
export function remixGraphFingerprint(nodes: CanvasNode[], edges: CanvasEdge[], composeId: string): string {
  const upstream = ancestors(nodes, edges, composeId);
  const included = new Set([composeId, ...upstream.map((node) => node.id)]);
  return JSON.stringify({ nodes: upstream.map((node) => ({
    id: node.id,
    type: node.type,
    mediaHash: 'remixMedia' in node.data ? (node.data.remixMedia as UploadedRemixMedia | undefined)?.media_hash : undefined,
    videoUrl: 'videoUrl' in node.data ? node.data.videoUrl ?? null : undefined,
    imageUrl: 'imageUrl' in node.data ? node.data.imageUrl ?? null : undefined,
    previewImageUrl: 'previewImageUrl' in node.data ? node.data.previewImageUrl ?? null : undefined,
    audioUrl: node.type === CANVAS_NODE_TYPES.audio ? node.data.audioUrl ?? null : undefined,
    role: 'remixRole' in node.data ? node.data.remixRole : undefined,
    scriptRef: 'remixScriptRef' in node.data ? node.data.remixScriptRef : undefined,
    artifactRef: 'artifactRef' in node.data ? node.data.artifactRef : undefined,
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
  conflicts?: string[];
}

export async function prepareNodeRemix(project: string, nodes: CanvasNode[], edges: CanvasEdge[], composeId: string, previousManifestRef?: RemixArtifactRef | null): Promise<PreparedNodeRemix> {
  const upstream = ancestors(nodes, edges, composeId);
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
  const blueprintNode = one(upstream.filter((node) => node.type === CANVAS_NODE_TYPES.videoStory && node.data.referenceBlueprintRef && edges.some((edge) => edge.source === node.id && edge.target === scriptNode.id)), '参考蓝图');
  const timingRef = timingNode.data.artifactRef as RemixArtifactRef;
  const scriptRef = scriptNode.data.remixScriptRef as RemixArtifactRef;
  const transcriptRef = transcriptNode.data.artifactRef as RemixArtifactRef;
  const blueprintRef = blueprintNode.data.referenceBlueprintRef as RemixArtifactRef;
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
  const backgroundNode = one(upstream.filter((node) => visualTypes.has(node.type) && (node.data.remixRole === 'background' || node.data.remixRole === 'a-roll') && node.data.remixMedia), '成片背景');
  const background = backgroundNode.data.remixMedia as UploadedRemixMedia;
  const visuals = Object.fromEntries(upstream.filter((node) => visualTypes.has(node.type) && node.data.remixRole === 'visual').map((node) => [node.id, boundBrollVisualMedia(project, node)]));
  const audioTracks = upstream.filter((node) => node.type === CANVAS_NODE_TYPES.audio && (node.data.remixRole === 'bgm' || node.data.remixRole === 'sfx') && node.data.remixMedia)
    .map((node) => ({ id: node.id, role: node.data.remixRole as 'bgm' | 'sfx', media: node.data.remixMedia as UploadedRemixMedia }));
  const payload = buildNodeRemixManifest({
    reference, background, narration, visuals, audioTracks,
    timing: timingArtifact.payload as unknown as CompiledTiming,
    script: scriptArtifact.payload as unknown as Parameters<typeof buildNodeRemixManifest>[0]['script'],
    wordTimeline: transcriptArtifact.payload as unknown as Parameters<typeof buildNodeRemixManifest>[0]['wordTimeline'],
  });
  let effectivePayload = payload as ManifestPayload;
  let conflicts: string[] = [];
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
