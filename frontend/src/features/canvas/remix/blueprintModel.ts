import type { RemixArtifactRef } from '@/api/remix';
import type { VideoStoryRow } from '@/features/canvas/domain/canvasNodes';
import type { ReferenceEvent } from './ReferenceEventList';

type ConnectedNode = { id: string; data: Record<string, unknown> };
type ConnectedEdge = { source: string; target: string };

export interface BlueprintSources {
  mediaPath: string;
  mediaHash: string;
  durationUs: number;
  videoUrl: string;
  transcriptRef: RemixArtifactRef | null;
}

function artifactRef(value: unknown): RemixArtifactRef | null {
  if (!value || typeof value !== 'object') return null;
  const ref = value as Record<string, unknown>;
  return typeof ref.artifactId === 'string' && Number.isInteger(ref.revision) && (ref.revision as number) > 0
    ? { artifactId: ref.artifactId, revision: ref.revision as number } : null;
}

export function connectedBlueprintSources(nodeId: string, nodes: ConnectedNode[] = [], edges: ConnectedEdge[] = []): BlueprintSources | null {
  const sources = new Set(edges.filter((edge) => edge.target === nodeId).map((edge) => edge.source));
  const connected = nodes.filter((node) => sources.has(node.id));
  const video = connected.find((node) => {
    const media = node.data.remixMedia as Record<string, unknown> | undefined;
    return media?.media_kind === 'video' && typeof media.media_path === 'string';
  });
  if (!video) return null;
  const media = video.data.remixMedia as Record<string, unknown>;
  const transcriptRef = connected
    .filter((node) => node.data.sourceMediaHash === media.media_hash)
    .map((node) => artifactRef(node.data.artifactRef)).find(Boolean) ?? null;
  return {
    mediaPath: media.media_path as string,
    mediaHash: typeof media.media_hash === 'string' ? media.media_hash : '',
    durationUs: typeof media.duration_us === 'number' ? media.duration_us : 0,
    videoUrl: typeof media.url === 'string' ? media.url : typeof video.data.videoUrl === 'string' ? video.data.videoUrl : '',
    transcriptRef,
  };
}

export interface ReferenceBlueprintInput {
  source: BlueprintSources;
  rows: VideoStoryRow[];
  referenceEvents?: ReferenceEvent[];
  narrativeRole: string;
  preserveRule: string;
  replaceRule: string;
  evidenceRef?: RemixArtifactRef | null;
}

const exactPayloadRef = (ref: RemixArtifactRef) => ({ artifact_id: ref.artifactId, revision: ref.revision });

function secondsToUs(value: string | null | undefined): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? Math.round(parsed * 1_000_000) : 0;
}

function structuredReferenceEvent(event: ReferenceEvent, index: number, evidenceRef: RemixArtifactRef, transcriptRef: RemixArtifactRef | null) {
  const frame = event.observed_frame ?? (event.analysis_frame_time_us != null ? {
    time_us: event.analysis_frame_time_us,
    url: event.frame_url,
  } : null);
  const spoken = event.spoken_range ?? ((event.spoken_text?.trim() && event.word_spans?.length) ? {
    text: event.spoken_text,
    start_us: event.word_spans[0].start_us,
    end_us: event.word_spans[event.word_spans.length - 1].end_us,
  } : null);
  return {
    event_id: event.event_id ?? `event-${index + 1}`,
    start_us: event.start_us,
    end_us: event.end_us,
    observed_frame: frame ? { evidence_ref: exactPayloadRef(evidenceRef), ...frame } : null,
    spoken_range: spoken
      ? transcriptRef
        ? { transcript_ref: exactPayloadRef(transcriptRef), ...spoken }
        : { ...spoken, unresolved_reason: 'missing_transcript_reference' }
      : null,
    model: {
      visual_observation: event.visual_observation ?? '',
      narrative_inference: event.narrative_inference ?? '',
    },
    user_override: {
      narrative_role: event.user_narrative_role ?? event.narrative_inference ?? '',
      review_state: event.review_state ?? 'needs_review',
    },
  };
}

/** Analysis rows are model observations and interpretations; user edits stay distinct. */
export function buildReferenceBlueprint(input: ReferenceBlueprintInput): Record<string, unknown> {
  const analyzedEvents = input.referenceEvents ?? [];
  const useReferenceEvents = analyzedEvents.length > 0;
  const eventFallback = useReferenceEvents ? analyzedEvents : [];
  const observations = !useReferenceEvents && input.rows.length
    ? input.rows.filter((row) => typeof row.visualDescription === 'string' && row.visualDescription.trim())
      .map((row, index) => ({ source: 'video_story_analysis', verified: false, shot: index + 1, start_time: row.startTime, end_time: row.endTime, description: row.visualDescription }))
    : eventFallback.filter((event) => event.visual_observation?.trim())
      .map((event, index) => ({ source: 'reference_event', verified: false, shot: index + 1, start_time: (event.start_us / 1_000_000).toFixed(3), end_time: (event.end_us / 1_000_000).toFixed(3), description: event.visual_observation }));
  const inferences = !useReferenceEvents && input.rows.length
    ? input.rows.filter((row) => typeof row.narrative === 'string' && row.narrative.trim())
      .map((row, index) => ({ source: 'video_story_analysis', shot: index + 1, interpretation: row.narrative }))
    : eventFallback.filter((event) => event.narrative_inference?.trim())
      .map((event, index) => ({ source: 'reference_event', shot: index + 1, interpretation: event.narrative_inference }));
  const choices = [
    { type: 'preserve', text: input.preserveRule.trim() },
    { type: 'replace', text: input.replaceRule.trim() },
  ].filter((choice) => choice.text);
  return {
    narrative_role: input.narrativeRole.trim(),
    events: input.evidenceRef && useReferenceEvents
      ? eventFallback.map((event, index) => structuredReferenceEvent(event, index, input.evidenceRef!, input.source.transcriptRef))
      : input.evidenceRef && input.rows.length
        ? input.rows.map((row, index) => ({
          event_id: `event-${index + 1}`,
          start_us: secondsToUs(row.startTime), end_us: secondsToUs(row.endTime),
          observed_frame: { evidence_ref: exactPayloadRef(input.evidenceRef!), time_us: secondsToUs(row.startTime), ...(row.keyframeUrl ? { url: row.keyframeUrl } : {}) },
          spoken_range: null,
          model: { visual_observation: row.visualDescription ?? '', narrative_inference: row.narrative ?? '' },
          user_override: { narrative_role: row.narrative ?? '', review_state: 'reviewed' },
        }))
        : input.rows.length
          ? input.rows.map((row, index) => ({ shot: index + 1, start_time: row.startTime, end_time: row.endTime, visual_description: row.visualDescription ?? '', narrative: row.narrative ?? '' }))
          : eventFallback.map((event, index) => ({ shot: index + 1, start_time: (event.start_us / 1_000_000).toFixed(3), end_time: (event.end_us / 1_000_000).toFixed(3), visual_description: event.visual_observation ?? '', spoken_text: event.spoken_text ?? '', narrative: event.narrative_inference ?? '', note_status: event.note_status ?? '' })),
    preserve_replace_rules: choices,
    observations,
    inferences,
    user_choices: choices,
    media_path: input.source.mediaPath,
    media_hash: input.source.mediaHash,
    ...(input.evidenceRef ? { evidence_ref: { artifact_id: input.evidenceRef.artifactId, revision: input.evidenceRef.revision } } : {}),
    ...(input.source.transcriptRef ? { transcript_ref: { artifact_id: input.source.transcriptRef.artifactId, revision: input.source.transcriptRef.revision } } : {}),
  };
}

function isExactRef(value: unknown): boolean {
  if (!value || typeof value !== 'object') return false;
  const ref = value as Record<string, unknown>;
  return typeof ref.artifact_id === 'string' && ref.artifact_id.length > 0 && Number.isInteger(ref.revision) && Number(ref.revision) > 0;
}

function sameExactRef(left: unknown, right: unknown): boolean {
  if (!isExactRef(left) || !isExactRef(right)) return false;
  const leftRef = left as Record<string, unknown>;
  const rightRef = right as Record<string, unknown>;
  return leftRef.artifact_id === rightRef.artifact_id && leftRef.revision === rightRef.revision;
}

interface BlueprintReadinessContext {
  evidenceRef?: RemixArtifactRef | null;
  transcriptRef?: RemixArtifactRef | null;
  upstreamRefs?: Array<{ artifact_id: string; revision: number }>;
}

/** Return user-facing blockers before an exact blueprint revision can feed script generation. */
export function blueprintReadinessErrors(payload: Record<string, unknown>, context: BlueprintReadinessContext = {}): string[] {
  const errors: string[] = [];
  if (typeof payload.narrative_role !== 'string' || !payload.narrative_role.trim()) errors.push('缺少整体叙事作用');
  const events = Array.isArray(payload.events) ? payload.events : [];
  if (!events.length) return [...errors, '缺少故事事件'];
  const evidenceRef = payload.evidence_ref;
  const transcriptRef = payload.transcript_ref;
  const expectedEvidence = context.evidenceRef ? exactPayloadRef(context.evidenceRef) : undefined;
  const expectedTranscript = context.transcriptRef ? exactPayloadRef(context.transcriptRef) : context.transcriptRef;
  if (expectedEvidence && !sameExactRef(evidenceRef, expectedEvidence)) errors.push('蓝图画面证据不是当前精确版本');
  if (expectedTranscript !== undefined && ((expectedTranscript === null && transcriptRef != null) || (expectedTranscript !== null && !sameExactRef(transcriptRef, expectedTranscript)))) errors.push('蓝图台词证据不是当前精确版本');
  if (context.upstreamRefs && !context.upstreamRefs.some((ref) => sameExactRef(evidenceRef, ref))) errors.push('蓝图画面证据引用已损坏');
  if (transcriptRef != null && context.upstreamRefs && !context.upstreamRefs.some((ref) => sameExactRef(transcriptRef, ref))) errors.push('蓝图台词证据引用已损坏');
  const seenIds = new Set<string>();
  events.forEach((raw, index) => {
    if (!raw || typeof raw !== 'object') { errors.push(`事件 ${index + 1} 结构无效`); return; }
    const event = raw as Record<string, unknown>;
    if (typeof event.event_id !== 'string' || !event.event_id || !Number.isInteger(event.start_us) || !Number.isInteger(event.end_us) || Number(event.end_us) <= Number(event.start_us)) errors.push(`事件 ${index + 1} 缺少必要结构`);
    else if (seenIds.has(event.event_id)) errors.push(`事件 ${index + 1} 标识重复`);
    else seenIds.add(event.event_id);
    const frame = event.observed_frame as Record<string, unknown> | null;
    if (!frame || !isExactRef(frame.evidence_ref) || !Number.isInteger(frame.time_us) || !sameExactRef(frame.evidence_ref, evidenceRef ?? frame.evidence_ref) || Number(frame.time_us) < Number(event.start_us) || Number(frame.time_us) >= Number(event.end_us)) errors.push(`事件 ${index + 1} 的画面证据引用无效`);
    const spoken = event.spoken_range as Record<string, unknown> | null;
    if (spoken && (!isExactRef(spoken.transcript_ref) || !sameExactRef(spoken.transcript_ref, transcriptRef ?? spoken.transcript_ref) || !Number.isInteger(spoken.start_us) || !Number.isInteger(spoken.end_us) || Number(spoken.end_us) <= Number(spoken.start_us) || Number(spoken.end_us) <= Number(event.start_us) || Number(spoken.start_us) >= Number(event.end_us))) errors.push(`事件 ${index + 1} 的台词证据引用无效`);
    const model = event.model as Record<string, unknown> | null;
    if (!model || typeof model.visual_observation !== 'string' || typeof model.narrative_inference !== 'string') errors.push(`事件 ${index + 1} 缺少模型分析结构`);
    const override = event.user_override as Record<string, unknown> | null;
    if (!override || typeof override.narrative_role !== 'string' || !override.narrative_role.trim()) errors.push(`事件 ${index + 1} 缺少叙事作用`);
    if (!override || !['reviewed', 'reviewed_uncertain'].includes(String(override.review_state))) errors.push(`事件 ${index + 1} 尚未核对`);
  });
  return errors;
}

/** Restore review state from an immutable blueprint revision after canvas reload. */
export function referenceEventsFromBlueprint(payload: Record<string, unknown>): ReferenceEvent[] {
  if (!Array.isArray(payload.events)) return [];
  return payload.events.flatMap((raw) => {
    if (!raw || typeof raw !== 'object') return [];
    const event = raw as Record<string, unknown>;
    const frame = event.observed_frame && typeof event.observed_frame === 'object' ? event.observed_frame as Record<string, unknown> : null;
    const spoken = event.spoken_range && typeof event.spoken_range === 'object' ? event.spoken_range as Record<string, unknown> : null;
    const model = event.model && typeof event.model === 'object' ? event.model as Record<string, unknown> : {};
    const override = event.user_override && typeof event.user_override === 'object' ? event.user_override as Record<string, unknown> : {};
    if (typeof event.event_id !== 'string' || !Number.isInteger(event.start_us) || !Number.isInteger(event.end_us)) return [];
    return [{
      event_id: event.event_id,
      start_us: Number(event.start_us), end_us: Number(event.end_us),
      observed_frame: frame && Number.isInteger(frame.time_us) ? {
        time_us: Number(frame.time_us),
        pts: Number.isInteger(frame.pts) ? Number(frame.pts) : null,
        time_base: typeof frame.time_base === 'string' ? frame.time_base : null,
        url: typeof frame.url === 'string' ? frame.url : null,
      } : null,
      frame_url: typeof frame?.url === 'string' ? frame.url : null,
      analysis_frame_time_us: Number.isInteger(frame?.time_us) ? Number(frame?.time_us) : null,
      spoken_range: spoken && typeof spoken.text === 'string' && Number.isInteger(spoken.start_us) && Number.isInteger(spoken.end_us) ? { text: spoken.text, start_us: Number(spoken.start_us), end_us: Number(spoken.end_us) } : null,
      spoken_text: typeof spoken?.text === 'string' ? spoken.text : '',
      visual_observation: typeof model.visual_observation === 'string' ? model.visual_observation : '',
      narrative_inference: typeof model.narrative_inference === 'string' ? model.narrative_inference : '',
      user_narrative_role: typeof override.narrative_role === 'string' ? override.narrative_role : '',
      review_state: ['needs_review', 'reviewed', 'reviewed_uncertain'].includes(String(override.review_state)) ? override.review_state as ReferenceEvent['review_state'] : 'needs_review',
      note_status: frame ? 'needs_review' : 'missing_frame',
    }];
  });
}
