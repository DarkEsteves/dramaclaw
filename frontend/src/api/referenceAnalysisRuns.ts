import { apiCall } from './client';
import type { DirectorArtifactRef, ReferenceDirectorPhase } from './referenceDirector';

export type ReferenceAnalysisStatus = ReferenceDirectorPhase | 'submitting' | 'transcribing' | 'analyzing' | 'analysis_ready' | 'resuming' | 'completed' | 'analysis_failed' | 'resume_failed' | 'stale';
export interface ReferenceAnalysisRun {
  run_id: string;
  project_id: string;
  canvas_id: string;
  agent_id: string;
  skill_id: string;
  source_node_id: string;
  analysis_node_id: string;
  transcript_node_id?: string | null;
  source_video_url: string;
  task_id?: string | null;
  job_id?: string | null;
  task_key?: string | null;
  task_type?: string | null;
  workflow_draft_id?: string | null;
  workflow_draft_revision?: number | null;
  status: ReferenceAnalysisStatus;
  pending_interaction?: { bridge_key: string; clarification_id?: string; turn_id: string; canvas_revision: number | string } | null;
  execution_blocker?: { code: string; next_action: string; message: string; request_id?: string } | null;
  last_successful_context_receipt?: { turn_id: string; source: string; revision: number | string; scope: Record<string, unknown> } | null;
  commission_ref?: DirectorArtifactRef | null;
  phase?: ReferenceDirectorPhase | null;
  message?: string | null;
  error?: string | null;
  created_at: string;
  updated_at: string;
}

function path(projectId: string, canvasId: string): string {
  return `projects/${encodeURIComponent(projectId)}/freezone/canvases/${encodeURIComponent(canvasId)}/reference-analysis-runs`;
}

export async function listReferenceAnalysisRuns(projectId: string, canvasId: string, agentId = 'main'): Promise<ReferenceAnalysisRun[]> {
  return (await apiCall<{ runs: ReferenceAnalysisRun[] }>(`${path(projectId, canvasId)}?agent_id=${encodeURIComponent(agentId)}`)).runs;
}

export async function retryReferenceAnalysisRun(projectId: string, canvasId: string, runId: string): Promise<ReferenceAnalysisRun> {
  return apiCall<ReferenceAnalysisRun>(`${path(projectId, canvasId)}/${encodeURIComponent(runId)}/retry`, { method: 'POST' });
}
