import { createRemixArtifact, type RemixArtifactRef } from '@/api/remix';
import { apiCall } from '@/api/client';
import { buildRemixScriptPayload, type RemixScriptDraft } from './remixScriptModel';

export async function saveRemixScriptArtifact(project: string, draft: RemixScriptDraft, existing: RemixArtifactRef | null, blueprint: RemixArtifactRef | null): Promise<RemixArtifactRef> {
  const nextRevision = (existing?.revision ?? 0) + 1;
  const payload = buildRemixScriptPayload(draft, nextRevision);
  const refs = blueprint ? [blueprint] : [];
  if (!existing) {
    const created = await createRemixArtifact(project, 'remix_script.v1', payload, refs);
    return { artifactId: created.id, revision: created.revision };
  }
  const revised = await apiCall<{ id: string; revision: number }>(`projects/${project}/freezone/remix/artifacts/${existing.artifactId}`, {
    method: 'PUT', json: { expected_revision: existing.revision, payload, upstream_refs: refs.map((ref) => ({ artifact_id: ref.artifactId, revision: ref.revision })) },
  });
  return { artifactId: revised.id, revision: revised.revision };
}
