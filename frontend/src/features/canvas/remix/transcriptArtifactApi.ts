import { apiCall } from '@/api/client';
import type { RemixArtifact, RemixArtifactRef } from '@/api/remix';
import type { TranscriptTimeline } from './transcriptModel';

export function reviseTranscriptArtifact(project: string, ref: RemixArtifactRef, payload: TranscriptTimeline, upstreamRefs: RemixArtifactRef[]): Promise<RemixArtifact> {
  return apiCall<RemixArtifact>(`projects/${project}/freezone/remix/artifacts/${ref.artifactId}`, {
    method: 'PUT',
    json: { expected_revision: ref.revision, payload, upstream_refs: upstreamRefs.map((upstream) => ({ artifact_id: upstream.artifactId, revision: upstream.revision })) },
  });
}
