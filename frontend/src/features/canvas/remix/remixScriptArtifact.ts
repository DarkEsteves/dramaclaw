import { createRemixArtifact, type RemixArtifactRef } from '@/api/remix';
import { apiCall } from '@/api/client';
import { buildRemixScriptPayload, type RemixScriptDraft } from './remixScriptModel';

export function latestWritableRemixScriptRef(active: RemixArtifactRef | null, lastSaved: RemixArtifactRef | null): RemixArtifactRef | null {
  if (!active) return lastSaved;
  if (!lastSaved || active.artifactId !== lastSaved.artifactId) return active;
  return active.revision >= lastSaved.revision ? active : lastSaved;
}

export function canRecordRemixScriptSave(base: RemixArtifactRef | null, active: RemixArtifactRef | null,
  lastSaved: RemixArtifactRef | null, saved: RemixArtifactRef): boolean {
  const latest = latestWritableRemixScriptRef(active, lastSaved);
  return base ? latest?.artifactId === base.artifactId && latest.revision <= saved.revision : !latest;
}

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
