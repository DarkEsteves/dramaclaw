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

/** Analysis rows are model observations and interpretations; user edits stay distinct. */
export function buildReferenceBlueprint(input: ReferenceBlueprintInput): Record<string, unknown> {
  const eventFallback = input.rows.length === 0 ? input.referenceEvents ?? [] : [];
  const observations = input.rows.length
    ? input.rows.filter((row) => typeof row.visualDescription === 'string' && row.visualDescription.trim())
      .map((row, index) => ({ source: 'video_story_analysis', verified: false, shot: index + 1, start_time: row.startTime, end_time: row.endTime, description: row.visualDescription }))
    : eventFallback.filter((event) => event.visual_observation?.trim())
      .map((event, index) => ({ source: 'reference_event', verified: false, shot: index + 1, start_time: (event.start_us / 1_000_000).toFixed(3), end_time: (event.end_us / 1_000_000).toFixed(3), description: event.visual_observation }));
  const inferences = input.rows.length
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
    events: input.rows.length
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
