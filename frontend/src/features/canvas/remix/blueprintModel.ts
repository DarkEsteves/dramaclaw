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

export interface BlueprintRuleSuggestions {
  narrativeRole: string;
  preserveRule: string;
  replaceRule: string;
}

function videoStoryAnalysis(storyData: Record<string, unknown> | undefined): Record<string, unknown> | null {
  const rawResult = storyData?.rawResult;
  const videoStory = rawResult && typeof rawResult === 'object'
    ? (rawResult as Record<string, unknown>).video_story : null;
  return videoStory && typeof videoStory === 'object' ? videoStory as Record<string, unknown> : null;
}

function analyzedRoles(storyData: Record<string, unknown> | undefined): string[] {
  const events = Array.isArray(storyData?.referenceEvents) ? storyData.referenceEvents : [];
  const roles = events.flatMap((event) => {
    if (!event || typeof event !== 'object') return [];
    const record = event as Record<string, unknown>;
    const role = record.user_narrative_role || record.narrative_inference;
    return typeof role === 'string' && role.trim() ? [role.trim()] : [];
  });
  if (!roles.length && Array.isArray(storyData?.rows)) {
    for (const row of storyData.rows) {
      if (!row || typeof row !== 'object') continue;
      const role = (row as Record<string, unknown>).narrative;
      if (typeof role === 'string' && role.trim()) roles.push(role.trim());
    }
  }
  return roles;
}

const BEAT_CUES: Array<[RegExp, string]> = [
  [/提问|发问|问题|悬念|设问/, '提问'],
  [/回答|答案|揭晓|亮出|揭示|展示/, '揭晓'],
  [/举例|列举|日常|场景|演示|使用|佐证/, '举例佐证'],
  [/调侃|质疑|吐槽|反问|冲突/, '调侃'],
  [/反转|反击|回击|反称|逆转/, '反转'],
];

/** Use the model's reusable structure, with a conservative fallback for older analyses. */
export function suggestBlueprintRules(storyData: Record<string, unknown> | undefined): BlueprintRuleSuggestions {
  const structure = videoStoryAnalysis(storyData)?.adaptation_structure;
  const structured = structure && typeof structure === 'object' ? structure as Record<string, unknown> : null;
  const modelRole = typeof structured?.narrative_role === 'string' && structured.narrative_role.length <= 100
    ? structured.narrative_role.trim() : '';
  const modelBeats = Array.isArray(structured?.reusable_beats)
    ? structured.reusable_beats.filter((beat): beat is string => typeof beat === 'string' && beat.trim().length > 0 && beat.trim().length <= 16)
      .map((beat) => beat.trim()).slice(0, 6)
    : [];
  const beats = modelBeats.length ? [...new Set(modelBeats)] : analyzedRoles(storyData).flatMap((role) => {
    const matches = BEAT_CUES.filter(([pattern]) => pattern.test(role)).map(([, beat]) => beat);
    return matches;
  }).filter((beat, index, all) => all.indexOf(beat) === index);
  if (!modelRole && beats.length < 2) return { narrativeRole: '', preserveRule: '', replaceRule: '' };
  return {
    narrativeRole: modelRole || `通过${beats.join('、')}推进故事。`,
    preserveRule: beats.length >= 2 ? `保留“${beats.join(' → ')}”的叙事节奏。` : '',
    replaceRule: '',
  };
}

/** Identify untouched defaults written by the previous release before upgrading them. */
export function legacySuggestBlueprintRules(storyData: Record<string, unknown> | undefined): BlueprintRuleSuggestions {
  const videoStory = videoStoryAnalysis(storyData);
  const summary = videoStory && typeof videoStory === 'object'
    ? (videoStory as Record<string, unknown>).summary : null;
  const narrativeSummary = typeof summary === 'string' ? summary.trim() : '';
  const roles = analyzedRoles(storyData);
  const keyRoles = roles.length <= 5
    ? roles
    : [roles[0], roles[1], roles[Math.floor(roles.length / 2)], roles[roles.length - 2], roles[roles.length - 1]];
  const distinctRoles = keyRoles.filter((role, index) => keyRoles.indexOf(role) === index);
  const hasAnalysis = Boolean(narrativeSummary || roles.length);
  return {
    narrativeRole: narrativeSummary || distinctRoles.join(' → '),
    preserveRule: distinctRoles.length
      ? `保留原片的叙事顺序与关键转折：${distinctRoles.join(' → ')}。`
      : narrativeSummary ? `保留原片的核心叙事线与节奏：${narrativeSummary}` : '',
    replaceRule: hasAnalysis ? '将原片特有的人物、产品、场景和具体说法替换为新主题的真实信息；重新制作对应画面与台词。' : '',
  };
}

type RuleDraft = {
  blueprintNarrativeRole?: string;
  blueprintPreserveRule?: string;
  blueprintReplaceRule?: string;
  blueprintRulesTouched?: boolean;
  referenceBlueprintRef?: RemixArtifactRef | null;
};

/** Refresh only empty fields or exact old generated copy; preserve authored choices. */
export function upgradeBlueprintRuleDraft(
  input: object,
  suggestions: BlueprintRuleSuggestions,
  legacy: BlueprintRuleSuggestions,
  hasSeed = false,
): Partial<RuleDraft> {
  const draft = input as RuleDraft;
  if (draft.blueprintRulesTouched) return {};
  const isExactLegacyCopy = Boolean(legacy.narrativeRole && suggestions.narrativeRole
    && draft.blueprintNarrativeRole === legacy.narrativeRole
    && draft.blueprintPreserveRule === legacy.preserveRule
    && draft.blueprintReplaceRule === legacy.replaceRule);
  if ((draft.referenceBlueprintRef || hasSeed) && !isExactLegacyCopy) return {};
  const patch: Partial<RuleDraft> = {};
  const fields: Array<[keyof Pick<RuleDraft, 'blueprintNarrativeRole' | 'blueprintPreserveRule' | 'blueprintReplaceRule'>, keyof BlueprintRuleSuggestions]> = [
    ['blueprintNarrativeRole', 'narrativeRole'],
    ['blueprintPreserveRule', 'preserveRule'],
    ['blueprintReplaceRule', 'replaceRule'],
  ];
  for (const [field, key] of fields) {
    const current = draft[field] ?? '';
    const next = suggestions[key];
    if (current !== next && (!current || (legacy[key] && current === legacy[key]))) patch[field] = next;
  }
  return patch;
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
