import { apiCall } from './client';

export interface RemixArtifactRef { artifactId: string; revision: number }
export interface RemixArtifact { id: string; schema: string; revision: number; payload: Record<string, unknown> }
export interface RemixRenderRequest { manifestRef: RemixArtifactRef; idempotencyKey: string; nodeRevision: number }
export interface RemixTaskRef { task_key: string; task_type: string; task_id: string; job_id: string }
export type RemixProcessRequest =
  | { action: 'transcribe_media'; mediaPath: string; language: string; idempotencyKey: string; nodeRevision: number }
  | { action: 'align_script'; mediaPath: string; scriptRef: RemixArtifactRef; idempotencyKey: string; nodeRevision: number }
  | { action: 'compile_semantic_timeline'; scriptRef: RemixArtifactRef; wordTimelineRef: RemixArtifactRef; editMap: Record<string, unknown>[]; overrides: Record<string, unknown>[]; fps: number; idempotencyKey: string; nodeRevision: number };

const exactRef = (ref: RemixArtifactRef) => ({ artifact_id: ref.artifactId, revision: ref.revision });

export function createRemixArtifact(project: string, schema: string, payload: Record<string, unknown>, upstreamRefs: RemixArtifactRef[]) {
  return apiCall<RemixArtifact>(`projects/${project}/freezone/remix/artifacts`, { method: 'POST', json: { schema, payload, upstream_refs: upstreamRefs.map(exactRef) } });
}
export function fetchRemixArtifact(project: string, ref: RemixArtifactRef) { return apiCall<RemixArtifact>(`projects/${project}/freezone/remix/artifacts/${ref.artifactId}?revision=${ref.revision}`); }
export function renderRemixManifest(project: string, request: RemixRenderRequest) { return apiCall<RemixTaskRef>(`projects/${project}/freezone/remix/artifacts/${request.manifestRef.artifactId}/render`, { method: 'POST', json: { revision: request.manifestRef.revision, idempotency_key: request.idempotencyKey, node_revision: request.nodeRevision } }); }

export function submitRemixProcessAction(project: string, request: RemixProcessRequest) {
  const payload = request.action === 'transcribe_media'
    ? { media_path: request.mediaPath, language: request.language }
    : request.action === 'align_script'
      ? { media_path: request.mediaPath, script_ref: exactRef(request.scriptRef) }
      : { script_ref: exactRef(request.scriptRef), word_timeline_ref: exactRef(request.wordTimelineRef), edit_map: request.editMap, overrides: request.overrides, fps: request.fps };
  return apiCall<RemixTaskRef>(`projects/${project}/freezone/remix/actions`, {
    method: 'POST', json: { action: request.action, payload, idempotency_key: request.idempotencyKey, node_revision: request.nodeRevision },
  });
}
