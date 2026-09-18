import { apiCall } from './client';

export interface RemixArtifactRef { artifactId: string; revision: number }
export interface RemixArtifact { id: string; schema: string; revision: number; payload: Record<string, unknown> }
export interface RemixRenderRequest { manifestRef: RemixArtifactRef; idempotencyKey: string; nodeRevision: number; canvasId?: string; nodeId?: string }
export interface RemixTaskRef { task_key: string; task_type: string; task_id: string; job_id: string }
export interface RemixRenderTaskRef extends RemixTaskRef { operation_id: string }
export interface RemixOperation { operation_id: string; status: string; task_id: string | null; task_type?: string | null; task_scope?: string | null; canvas_id?: string; node_id?: string; node_revision?: number; input_refs?: Array<{ artifact_id: string; revision: number }>; output_refs: Array<{ project_path?: string; artifact_id?: string; revision?: number }>; output_urls?: Array<{ media_url: string; download_url: string }> }
export interface RemixUploadedMedia { media_path: string; media_hash: string; url: string; size: number; media_kind: 'video' | 'audio'; duration_us: number; has_audio?: boolean }
export type RemixProcessRequest =
  | { action: 'transcribe_media'; mediaPath: string; language: string; idempotencyKey: string; nodeRevision: number }
  | { action: 'align_script'; mediaPath: string; scriptRef: RemixArtifactRef; idempotencyKey: string; nodeRevision: number }
  | { action: 'compile_semantic_timeline'; scriptRef: RemixArtifactRef; wordTimelineRef: RemixArtifactRef; editMap: Record<string, unknown>[]; overrides: Record<string, unknown>[]; fps: number; idempotencyKey: string; nodeRevision: number };

const exactRef = (ref: RemixArtifactRef) => ({ artifact_id: ref.artifactId, revision: ref.revision });

export function createRemixArtifact(project: string, schema: string, payload: Record<string, unknown>, upstreamRefs: RemixArtifactRef[]) {
  return apiCall<RemixArtifact>(`projects/${project}/freezone/remix/artifacts`, { method: 'POST', json: { schema, payload, upstream_refs: upstreamRefs.map(exactRef) } });
}
export function fetchRemixArtifact(project: string, ref: RemixArtifactRef) { return apiCall<RemixArtifact>(`projects/${project}/freezone/remix/artifacts/${ref.artifactId}?revision=${ref.revision}`); }
export function renderRemixManifest(project: string, request: RemixRenderRequest) { return apiCall<RemixRenderTaskRef>(`projects/${project}/freezone/remix/artifacts/${request.manifestRef.artifactId}/render`, { method: 'POST', json: { revision: request.manifestRef.revision, idempotency_key: request.idempotencyKey, node_revision: request.nodeRevision, ...(request.canvasId ? { canvas_id: request.canvasId } : {}), ...(request.nodeId ? { node_id: request.nodeId } : {}) } }); }
export function uploadRemixMedia(project: string, file: File) {
  const body = new FormData();
  body.append('file', file);
  return apiCall<RemixUploadedMedia>(`projects/${project}/freezone/remix/media`, { method: 'POST', body, timeout: false });
}
export function fetchRemixOperation(project: string, operationId: string) {
  return apiCall<RemixOperation>(`projects/${project}/freezone/remix/operations/${operationId}`);
}
export function listRemixOperations(project: string) {
  return apiCall<{ items: RemixOperation[] }>(`projects/${project}/freezone/remix/operations?limit=50`);
}
export function fetchRemixTaskStatus(project: string, taskType: string, scope: string) {
  return apiCall<{ status: string; error?: string | null } | null>(`projects/${project}/tasks/${taskType}/0?scope=${encodeURIComponent(scope)}`);
}
export function confirmRemixOperation(project: string, operationId: string, canvasId: string, nodeId: string) {
  return apiCall<RemixOperation>(`projects/${project}/freezone/remix/operations/${operationId}/confirm`, { method: 'POST', json: { canvas_id: canvasId, node_id: nodeId } });
}

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
