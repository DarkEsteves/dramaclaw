import type { RemixArtifactRef } from '@/api/remix';
import type { VideoStoryRow } from '@/features/canvas/domain/canvasNodes';

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
  const transcriptRef = connected.map((node) => artifactRef(node.data.artifactRef)).find(Boolean) ?? null;
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
  narrativeRole: string;
  preserveRule: string;
  replaceRule: string;
  evidenceRef?: RemixArtifactRef | null;
}

/** Analysis rows are model observations and interpretations; user edits stay distinct. */
export function buildReferenceBlueprint(input: ReferenceBlueprintInput): Record<string, unknown> {
  const observations = input.rows.filter((row) => typeof row.visualDescription === 'string' && row.visualDescription.trim())
    .map((row, index) => ({ source: 'video_story_analysis', verified: false, shot: index + 1, start_time: row.startTime, end_time: row.endTime, description: row.visualDescription }));
  const inferences = input.rows.filter((row) => typeof row.narrative === 'string' && row.narrative.trim())
    .map((row, index) => ({ source: 'video_story_analysis', shot: index + 1, interpretation: row.narrative }));
  const choices = [
    { type: 'preserve', text: input.preserveRule.trim() },
    { type: 'replace', text: input.replaceRule.trim() },
  ].filter((choice) => choice.text);
  return {
    narrative_role: input.narrativeRole.trim(),
    events: input.rows.map((row, index) => ({ shot: index + 1, start_time: row.startTime, end_time: row.endTime, visual_description: row.visualDescription ?? '', narrative: row.narrative ?? '' })),
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
