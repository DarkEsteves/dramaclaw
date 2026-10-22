import { apiCall } from './client';
import type { RemixArtifact } from './remix';
import type { MaterialSpeechReviewSummary, MaterialSpeechCorrectionInput } from '@/features/superchat/MaterialSpeechReview';

export interface DirectorArtifactRef { artifact_id: string; revision: number; project_id?: string }
export type ReferenceDirectorPhase = 'awaiting_direction' | 'drafting_plan' | 'awaiting_plan_review' | 'awaiting_plan_confirmation' | 'preparing_materials' | 'aligning_speech' | 'composing' | 'reviewing_output' | 'completed';
export const REFERENCE_DIRECTOR_PHASE_LABELS: Record<ReferenceDirectorPhase, string> = {
  awaiting_direction: '等待确认复刻方向', drafting_plan: '正在创作作品方案',
  awaiting_plan_review: '虾导正在审核作品方案', awaiting_plan_confirmation: '等待确认作品方案',
  preparing_materials: '正在准备制作素材', aligning_speech: '正在校对实际语音',
  composing: '正在合成作品', reviewing_output: '正在检查成片', completed: '作品已交付',
};
export interface ReferenceCommission {
  commission_ref: DirectorArtifactRef;
  project_id: string; canvas_id: string; goal: string; phase: ReferenceDirectorPhase;
  analysis_node_id?: string;
  plan_ref?: DirectorArtifactRef | null;
  blocker?: { kind: string; reason: string; next_action?: string; entity_ids?: string[] } | null;
}
export interface ReferenceDecision {
  decision_ref: DirectorArtifactRef; commission_ref: DirectorArtifactRef;
  kind: 'direction' | 'plan' | 'material' | 'composition'; summary: string; proposal: Record<string, unknown>; status: 'pending' | 'confirmed';
}
export interface DirectorMaterialStep {
  media_kind?: 'image' | 'video' | 'audio';
  id: string; label: string; node_id?: string | null;
  source: 'generate' | 'existing' | 'upload';
  status: 'planned' | 'awaiting_upload' | 'submitted' | 'candidate' | 'accepted' | 'rejected' | 'stale';
  depends_on: string[]; references?: Array<{ step_id?: string; node_id?: string; role: string }>;
  reason?: string | null;
}
export interface DirectorMaterialWork {
  work_ref: DirectorArtifactRef; plan_ref: DirectorArtifactRef;
  requirements: Array<{ id: string; description: string; final_step_id: string; acceptance?: string[] }>;
  steps: DirectorMaterialStep[];
}
export interface DirectorSpeechCorrection {
  preview_ref: DirectorArtifactRef; source_plan_ref: DirectorArtifactRef;
  node_id: string; take_id: string; lexical_change: boolean; blockers: string[];
  corrections: Array<{ word_id: string; text: string; reason: string }>;
  corrected_words?: Array<{ id: string; text: string; start_us: number; end_us: number }>;
  cues?: Array<{ turn_id: string; text: string }>;
  original_words?: Array<{ id: string; text: string; start_us: number; end_us: number }>;
  source_node_id?: string | null; source_url?: string | null; target_text?: string;
}
export interface DirectorMaterialReview {
  review_ref: DirectorArtifactRef; work_ref: DirectorArtifactRef; step_id: string;
  current_work_ref?: DirectorArtifactRef;
  resource: { media_path: string; media_kind: 'image' | 'video' | 'audio'; node_id: string;
    id?: string; node_revision?: number; media_hash?: string; duration_us?: number; has_audio?: boolean };
  technical: { status: 'passed' | 'rejected' | 'unavailable'; checks?: string[]; findings?: string[] };
  content: { status: 'passed' | 'rejected' | 'unavailable'; checks?: string[]; findings?: string[] };
}
export interface DirectorOutputReview {
  review_ref: DirectorArtifactRef; commission_ref: DirectorArtifactRef;
  manifest_ref: DirectorArtifactRef; plan_ref: DirectorArtifactRef;
  compose_node_id: string; media_url: string;
  status: 'needs_review' | 'passed' | 'rejected';
  technical: Pick<DirectorMaterialReview['technical'], 'status'> & Partial<Omit<DirectorMaterialReview['technical'], 'status'>>;
  content: Pick<DirectorMaterialReview['content'], 'status'> & Partial<Omit<DirectorMaterialReview['content'], 'status'>>;
  findings: string[];
}
export interface DirectorMaterialStage {
  stage_id: string; stage_number: number; step_ids: string[];
  status: 'preparing' | 'awaiting_confirmation' | 'confirmed';
  total: number; confirmed: number; reviewed: number;
}
export interface ReferenceDirectorState {
  material_stages?: DirectorMaterialStage[];
  commissions: ReferenceCommission[]; decisions: ReferenceDecision[];
  output_reviews?: DirectorOutputReview[]; material_work: DirectorMaterialWork[]; material_reviews?: DirectorMaterialReview[]; speech_corrections?: DirectorSpeechCorrection[];
}
export interface ReferenceProductionState {
  schema_version: 'reference_production_state.v1';
  scope: { project_id: string; canvas_id: string; agent_id: string };
  commission_ref: DirectorArtifactRef;
  plan_ref: DirectorArtifactRef | null;
  work_ref: DirectorArtifactRef | null;
  projection_revision: string;
  analysis_node_id?: string;
  phase: ReferenceDirectorPhase;
  execution_status: 'idle' | 'running' | 'waiting_user' | 'waiting_agent' | 'recovering' | 'stopped' | 'failed' | 'completed';
  progress: { materials_total: number; materials_available: number; materials_accepted: number;
    final_materials_total: number; final_materials_accepted: number; user_action_count: number };
  blockers: Array<{ owner: 'user' | 'agent' | 'system'; kind: string; action_id?: string | null }>;
  next_action: { owner: 'user' | 'agent' | 'system'; kind: string; action_id?: string | null;
    tool?: string; draft_id?: string; revision?: number; request_id?: string } | null;
  director_state: ReferenceDirectorState;
  candidate_work_refs?: DirectorArtifactRef[];
  suggested_work_ref?: DirectorArtifactRef | null;
  execution_problem?: Array<{ run_id: string; step_id: string; status: 'failed' | 'interrupted'; message: string }>;
}
export interface ReferenceProductionBranches {
  branches: ReferenceProductionState[];
  selection_required: boolean;
}
function path(projectId: string, canvasId: string): string {
  return `projects/${encodeURIComponent(projectId)}/freezone/canvases/${encodeURIComponent(canvasId)}/director`;
}
export function getReferenceDirectorState(projectId: string, canvasId: string): Promise<ReferenceDirectorState> {
  return apiCall<ReferenceDirectorState>(`${path(projectId, canvasId)}/state`);
}
export function getReferenceProductionState(projectId: string, canvasId: string, commissionId?: string,
  agentId = 'main'): Promise<ReferenceProductionState | ReferenceProductionBranches> {
  const query = new URLSearchParams({ agent_id: agentId });
  if (commissionId) query.set('commission_id', commissionId);
  return apiCall(`${path(projectId, canvasId)}/production-state?${query}`);
}
export interface ReferenceProductionRecovery {
  scope: ReferenceProductionState['scope'];
  items: Array<{ commission_ref: DirectorArtifactRef; goal: string; phase: ReferenceDirectorPhase; analysis_node_id?: string }>;
}
export function getReferenceProductionRecovery(projectId: string, canvasId: string, agentId = 'main'): Promise<ReferenceProductionRecovery> {
  const query = new URLSearchParams({ agent_id: agentId });
  return apiCall(`${path(projectId, canvasId)}/production-recovery?${query}`);
}
// The acknowledgement carries the current commission after settlement, which
// may be newer than the exact accepted request. Read production-state for facts.
export function resumeReferenceProduction(projectId: string, canvasId: string,
  input: { commission_ref: DirectorArtifactRef; command_id: string; agent_id: string }): Promise<{ accepted: true; commission_ref: DirectorArtifactRef; scope: ReferenceProductionState['scope'] }> {
  return apiCall(`${path(projectId, canvasId)}/resume-production`, { method: 'POST', json: input });
}
export function getReferenceProductionHistory(projectId: string, canvasId: string, commissionId: string,
  cursor?: string, agentId = 'main'): Promise<{ items: Array<{ schema: string; id?: string; artifact_id?: string; revision: number }>; next_cursor: string | null }> {
  const query = new URLSearchParams({ commission_id: commissionId, agent_id: agentId, limit: '20' });
  if (cursor) query.set('cursor', cursor);
  return apiCall(`${path(projectId, canvasId)}/history?${query}`);
}
export function selectReferenceMaterialWork(projectId: string, canvasId: string,
  input: { commission_ref: DirectorArtifactRef; work_ref: DirectorArtifactRef; command_id: string }): Promise<ReferenceCommission> {
  return apiCall(`${path(projectId, canvasId)}/select-material-work`, { method: 'POST', json: input });
}
export interface ReferenceDecisionConfirmation { decision_ref: DirectorArtifactRef; commission_ref: DirectorArtifactRef; agent_id: string; expected_projection_revision: string }
export function confirmReferenceDecision(projectId: string, canvasId: string, artifactId: string, input?: ReferenceDecisionConfirmation): Promise<{ commission: ReferenceCommission }> {
  return apiCall(`${path(projectId, canvasId)}/decisions/${encodeURIComponent(artifactId)}/confirm`, { method: 'POST', ...(input ? { json: input } : {}) });
}
export function confirmReferenceSpeechCorrection(projectId: string, canvasId: string, artifactId: string): Promise<{ plan: RemixArtifact; node_id: string }> {
  return apiCall(`${path(projectId, canvasId)}/speech-corrections/${encodeURIComponent(artifactId)}/confirm`, { method: 'POST' });
}

export function confirmReferenceMaterialReview(projectId: string, canvasId: string, artifactId: string): Promise<unknown> {
  return apiCall(`${path(projectId, canvasId)}/material-reviews/${encodeURIComponent(artifactId)}/confirm`, { method: 'POST' });
}
export function confirmReferenceMaterialReviewBatch(projectId: string, canvasId: string,
  input: { items: Array<{ review_ref: DirectorArtifactRef; work_ref: DirectorArtifactRef; step_id: string; node_id: string }>; command_id: string }): Promise<{ items: Array<{ review_ref: DirectorArtifactRef; review: DirectorMaterialReview }>; replayed: boolean }> {
  return apiCall(`${path(projectId, canvasId)}/material-reviews/confirm-batch`, { method: 'POST', json: input });
}

export function confirmReferenceOutputReview(projectId: string, canvasId: string, reviewRef: DirectorArtifactRef): Promise<{ review_ref: DirectorArtifactRef; review: DirectorOutputReview; commission: ReferenceCommission }> {
  return apiCall(`${path(projectId, canvasId)}/output-review/confirm`, { method: 'POST', json: { review_ref: reviewRef, viewed_complete_media: true } });
}

export function associateReferenceMaterialUpload(projectId: string, canvasId: string, workRef: DirectorArtifactRef, stepId: string, nodeId: string): Promise<{work_ref: DirectorArtifactRef; work: DirectorMaterialWork}> {
  return apiCall(`${path(projectId, canvasId)}/material-upload`, {method:'POST', json:{work_ref:workRef,step_id:stepId,node_id:nodeId}});
}

export type ReferenceMaterialSpeechStatus =
  | { status: 'ready'; work_ref: DirectorArtifactRef; plan_ref: DirectorArtifactRef; step_id: string; node_id: string; summary: MaterialSpeechReviewSummary }
  | { status: 'pending' | 'unavailable'; message: string };
export function getReferenceMaterialSpeechStatus(projectId: string, canvasId: string,
  input: { work_ref: DirectorArtifactRef; step_id: string; node_id: string; language: 'auto' }, agentId = 'main'): Promise<ReferenceMaterialSpeechStatus> {
  return apiCall(`${path(projectId, canvasId)}/material-speech-status?${new URLSearchParams({ agent_id: agentId })}`,
    { method: 'POST', json: input });
}
export function saveReferenceMaterialSpeechCorrection(projectId: string, canvasId: string,
  input: MaterialSpeechCorrectionInput & { work_ref: DirectorArtifactRef; step_id: string; node_id: string; command_id: string }): Promise<{ summary: MaterialSpeechReviewSummary }> {
  return apiCall(`${path(projectId, canvasId)}/material-speech-correction`, { method: 'POST', json: input });
}

export interface ReferenceConversationCard {
  card_id: string; ref: DirectorArtifactRef; commission_ref: DirectorArtifactRef; schema: string;
  title: string; summary: string; status: 'pending' | 'confirmed' | 'stale' | 'recorded'; reason: string;
  decision_kind?: 'direction' | 'plan' | 'material' | 'composition';
}
export function getReferenceConversationCards(projectId: string, canvasId: string, commissionId: string,
  refs: DirectorArtifactRef[], agentId = 'main'): Promise<{ scope: ReferenceProductionState['scope']; commission_ref: DirectorArtifactRef; items: ReferenceConversationCard[] }> {
  const query = new URLSearchParams({ commission_id: commissionId, agent_id: agentId, refs: JSON.stringify(refs) });
  return apiCall(`${path(projectId, canvasId)}/conversation-cards?${query}`);
}
