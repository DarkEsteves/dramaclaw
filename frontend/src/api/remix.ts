import { apiBlob, apiCall } from './client';

export interface RemixArtifactRef { artifactId: string; revision: number }
export interface RemixArtifact { id: string; schema: string; revision: number; payload: Record<string, unknown>; upstream_refs?: Array<{ artifact_id: string; revision: number }> }
export interface RemixRenderRequest { manifestRef: RemixArtifactRef; idempotencyKey: string; nodeRevision: number; canvasId?: string; nodeId?: string; inputFingerprint?: string; regenerate?: boolean }
export interface RemixTaskRef { task_key: string; task_type: string; task_id: string | null; operation_id: string }
export interface RemixRenderTaskRef extends RemixTaskRef { job_id: string }
export interface RemixOperation { operation_id: string; status: string; task_id: string | null; task_type?: string | null; task_scope?: string | null; producer_version?: string; normalized_parameters?: { input_fingerprint?: string; [key: string]: unknown }; canvas_id?: string; node_id?: string; node_revision?: number; input_refs?: Array<{ artifact_id: string; revision: number }>; input_media_hashes?: string[]; output_refs: Array<{ project_path?: string; artifact_id?: string; revision?: number }>; output_urls?: Array<{ media_url: string; download_url: string }> }
export interface RemixUploadedMedia { media_path: string; media_hash: string; url: string; size: number; media_kind: 'image' | 'video' | 'audio'; duration_us: number; has_audio?: boolean; audio_stream_count?: number; width_px?: number; height_px?: number; fps_num?: number; fps_den?: number }
export type RemixProcessRequest =
  | { action: 'analyze_reference_video'; mediaPath: string; idempotencyKey: string; nodeRevision: number }
  | { action: 'transcribe_media'; mediaPath: string; language: string; idempotencyKey: string; nodeRevision: number }
  | { action: 'align_script'; mediaPath: string; scriptRef: RemixArtifactRef; idempotencyKey: string; nodeRevision: number }
  | { action: 'align_production_speech'; mediaPath: string; planRef: RemixArtifactRef; turnId: string; placementId?: string; takeId?: string; idempotencyKey: string; nodeRevision: number }
  | { action: 'plan_broll'; scriptRef: RemixArtifactRef; idempotencyKey: string; nodeRevision: number }
  | { action: 'generate_script_draft'; blueprintRef: RemixArtifactRef; newTopic: string; idempotencyKey: string; nodeRevision: number }
  | { action: 'generate_production_plan_arrangement'; planRef: RemixArtifactRef; idempotencyKey: string; nodeRevision: number }
  | { action: 'generate_production_plan_draft'; topic: string; blueprintRef?: RemixArtifactRef | null; analysisContext?: Record<string, unknown> | null; idempotencyKey: string; nodeRevision: number }
  | { action: 'compile_take_timeline'; scriptRef: RemixArtifactRef; takeBindings: Array<{ segment_id: string; media_path: string }>; fps: number; idempotencyKey: string; nodeRevision: number }
  | { action: 'compile_semantic_timeline'; mediaPath: string; scriptRef: RemixArtifactRef; wordTimelineRef: RemixArtifactRef; blueprintRef: RemixArtifactRef; editMap: Record<string, unknown>[]; overrides: Record<string, unknown>[]; fps: number; idempotencyKey: string; nodeRevision: number };

const exactRef = (ref: RemixArtifactRef) => ({ artifact_id: ref.artifactId, revision: ref.revision });

export function createRemixArtifact(project: string, schema: string, payload: Record<string, unknown>, upstreamRefs: RemixArtifactRef[]) {
  return apiCall<RemixArtifact>(`projects/${project}/freezone/remix/artifacts`, { method: 'POST', json: { schema, payload, upstream_refs: upstreamRefs.map(exactRef) } });
}
export function reviseRemixArtifact(project: string, ref: RemixArtifactRef, payload: Record<string, unknown>, upstreamRefs: RemixArtifactRef[]) {
  return apiCall<RemixArtifact>(`projects/${project}/freezone/remix/artifacts/${ref.artifactId}`, { method: 'PUT', json: { expected_revision: ref.revision, payload, upstream_refs: upstreamRefs.map(exactRef) } });
}
export function fetchRemixArtifact(project: string, ref: RemixArtifactRef) { return apiCall<RemixArtifact>(`projects/${project}/freezone/remix/artifacts/${ref.artifactId}?revision=${ref.revision}`); }
export function fetchLatestRemixArtifact(project: string, artifactId: string) { return apiCall<RemixArtifact>(`projects/${project}/freezone/remix/artifacts/${artifactId}/latest`); }
export function generateRemixScriptDraft(project: string, blueprintRef: RemixArtifactRef, newTopic: string) {
  return apiCall<{ dialogue: string; selections: Array<{ id: string; label: string; start: number; end: number }> }>(`projects/${project}/freezone/remix/script-drafts`, {
    method: 'POST', json: { blueprint_ref: exactRef(blueprintRef), new_topic: newTopic }, timeout: false,
  });
}
export function generateProductionPlanDraft(project: string, topic: string, blueprintRef?: RemixArtifactRef | null, analysisContext?: Record<string, unknown> | null) {
  return apiCall<Record<string, unknown>>(`projects/${project}/freezone/remix/production-plan-drafts`, {
    method: 'POST', json: { topic, ...(blueprintRef ? { blueprint_ref: exactRef(blueprintRef) } : analysisContext ? { analysis_context: analysisContext } : {}) }, timeout: false,
  });
}
export interface ProductionPlanProjection { plan_ref: { artifact_id: string; revision: number }; output_spec: Record<string, unknown>; beats: Record<string, unknown>[]; selections?: Record<string, unknown>[]; resources: Record<string, unknown>[]; placements: Array<Record<string, unknown> & { id?: string; resource_status?: string }>; speech_cues?: Record<string, unknown>[]; issues: Record<string, unknown>[]; ready: boolean }
export function projectProductionPlan(project: string, planRef: RemixArtifactRef) {
 return apiCall<ProductionPlanProjection>(`projects/${project}/freezone/remix/production-plans/project`, { method: 'POST', json: { plan_ref: exactRef(planRef) } });
}
export interface ProductionPlanState {
  plan_ref: { artifact_id: string; revision: number };
  speech_cues_pending: string[];
  speech_turns_pending: string[];
  unbound_placement_ids: string[];
}
export function fetchProductionPlanState(project: string, ref: RemixArtifactRef) {
  return apiCall<ProductionPlanState>(`projects/${project}/freezone/remix/production-plans/${ref.artifactId}/state?revision=${ref.revision}`);
}
export interface ProductionCommandResult { plan_ref: { artifact_id: string; revision: number }; changed_ids: string[]; invalidated: string[]; blockers: string[]; speech_estimates?: Record<string, unknown>[]; plan_diagnostics?: Record<string, unknown>[] }
export interface ProductionCommandPreflightResult {
  ok: boolean;
  plan_ref: { artifact_id: string; revision: number } | null;
  issues: Array<{ code: string; path: string; entity_id: string | null; message: string }>;
  speech_estimates: Record<string, unknown>[];
  plan_diagnostics: Record<string, unknown>[];
}
export function preflightProductionCommand(project: string, command: Record<string, unknown>) {
  return apiCall<ProductionCommandPreflightResult>(`projects/${project}/freezone/remix/production-plans/commands/preflight`, { method: 'POST', json: command });
}
export function applyProductionCommand(project: string, command: Record<string, unknown>) {
 return apiCall<ProductionCommandResult>(`projects/${project}/freezone/remix/production-plans/commands`, { method: 'POST', json: command });
}
export function migrateProductionPlan(project: string, planRef: RemixArtifactRef) {
  return apiCall<RemixArtifact>(`projects/${project}/freezone/remix/production-plans/migrate`, { method: 'POST', json: { plan_ref: exactRef(planRef) } });
}
export function compileProductionPlan(project: string, planRef: RemixArtifactRef) {
  return apiCall<Record<string, unknown>>(`projects/${project}/freezone/remix/production-plans/compile`, {
    method: 'POST', json: { plan_ref: exactRef(planRef) }, timeout: false,
  });
}
export function previewProductionFrame(project: string, planRef: RemixArtifactRef, frame: number) {
  return apiBlob(`projects/${project}/freezone/remix/production-plans/preview-frame`, {
    method: 'POST', json: { plan_ref: exactRef(planRef), frame }, timeout: false,
  });
}
export function generateRemixBrollPlan(project: string, scriptRef: RemixArtifactRef) {
  return apiCall<RemixArtifact>(`projects/${project}/freezone/remix/broll-plans`, {
    method: 'POST', json: { script_ref: exactRef(scriptRef) }, timeout: false,
  });
}
export function renderRemixManifest(project: string, request: RemixRenderRequest) { return apiCall<RemixRenderTaskRef>(`projects/${project}/freezone/remix/artifacts/${request.manifestRef.artifactId}/render`, { method: 'POST', json: { revision: request.manifestRef.revision, idempotency_key: request.idempotencyKey, node_revision: request.nodeRevision, input_fingerprint: request.inputFingerprint ?? '', regenerate: request.regenerate ?? false, ...(request.canvasId ? { canvas_id: request.canvasId } : {}), ...(request.nodeId ? { node_id: request.nodeId } : {}) } }); }
export function uploadRemixMedia(project: string, file: File) {
  const body = new FormData();
  body.append('file', file);
  return apiCall<RemixUploadedMedia>(`projects/${project}/freezone/remix/media`, { method: 'POST', body, timeout: false });
}
export function attachRemixMedia(project: string, mediaPath: string) {
  return apiCall<RemixUploadedMedia>(`projects/${project}/freezone/remix/media/attach`, { method: 'POST', json: { media_path: mediaPath } });
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
export function transitionRemixOperation(project: string, operationId: string, outcome: 'unknown' | 'failed' | 'cancelled', reason = '', providerTaskId = '') {
  return apiCall<RemixOperation>(`projects/${project}/freezone/remix/operations/${operationId}/transition`, { method: 'POST', json: { outcome, reason, provider_task_id: providerTaskId } });
}
export function reconcileRemixOperation(project: string, operationId: string) {
  return apiCall<RemixOperation>(`projects/${project}/freezone/remix/operations/${operationId}/reconcile`, { method: 'POST' });
}

export function submitRemixProcessAction(project: string, request: RemixProcessRequest) {
  const payload = request.action === 'analyze_reference_video'
    ? { media_path: request.mediaPath }
    : request.action === 'transcribe_media'
    ? { media_path: request.mediaPath, language: request.language }
    : request.action === 'align_script'
      ? { media_path: request.mediaPath, script_ref: exactRef(request.scriptRef) }
      : request.action === 'align_production_speech'
        ? { media_path: request.mediaPath, plan_ref: exactRef(request.planRef), turn_id: request.turnId,
            ...(request.placementId ? { placement_id: request.placementId } : {}),
            ...(request.takeId ? { take_id: request.takeId } : {}) }
      : request.action === 'plan_broll'
        ? { script_ref: exactRef(request.scriptRef) }
      : request.action === 'generate_script_draft'
        ? { blueprint_ref: exactRef(request.blueprintRef), new_topic: request.newTopic }
      : request.action === 'generate_production_plan_draft'
        ? { topic: request.topic, ...(request.blueprintRef ? { blueprint_ref: exactRef(request.blueprintRef) } : request.analysisContext ? { analysis_context: request.analysisContext } : {}) }
      : request.action === 'generate_production_plan_arrangement'
        ? { plan_ref: exactRef(request.planRef) }
      : request.action === 'compile_take_timeline'
        ? { script_ref: exactRef(request.scriptRef), take_bindings: request.takeBindings, fps: request.fps }
      : { media_path: request.mediaPath, script_ref: exactRef(request.scriptRef), word_timeline_ref: exactRef(request.wordTimelineRef), blueprint_ref: exactRef(request.blueprintRef), edit_map: request.editMap, overrides: request.overrides, fps: request.fps };
  return apiCall<RemixTaskRef>(`projects/${project}/freezone/remix/actions`, {
    method: 'POST', json: { action: request.action, payload, idempotency_key: request.idempotencyKey, node_revision: request.nodeRevision },
  });
}
