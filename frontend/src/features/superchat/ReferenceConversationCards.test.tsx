import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ReferenceDirectorPanel } from './ReferenceDirectorPanel';
import { ReferenceAnalysisStatus } from './ReferenceAnalysisStatus';
import { useAuthStore } from '@/stores/auth-store';
import { getReferenceDirectorState } from '@/api/referenceDirector';
import { listReferenceAnalysisRuns, retryReferenceAnalysisRun } from '@/api/referenceAnalysisRuns';
vi.mock('@/api/referenceDirector', async (original) => ({ ...await original<typeof import('@/api/referenceDirector')>(), getReferenceDirectorState: vi.fn(), confirmReferenceDecision: vi.fn() }));
vi.mock('@/api/referenceAnalysisRuns', () => ({ listReferenceAnalysisRuns: vi.fn(), retryReferenceAnalysisRun: vi.fn() }));
vi.mock('./productionCanvasRefresh', () => ({ refreshProductionCanvas: vi.fn(async () => {}) }));
vi.mock('@/api/canvas', () => ({ getFreezoneCanvas: vi.fn(async () => ({ nodes: [], edges: [], revision: 1 })) }));
const props = { projectId: 'p', canvasId: 'c', agentId: 'main', connected: true };
const direction = () => ({ commissions: [{ commission_ref: { artifact_id: 'commission', revision: 1 }, project_id: 'p', canvas_id: 'c', goal: '咖啡问答', phase: 'awaiting_direction' as const }], decisions: [{ decision_ref: { artifact_id: 'direction', revision: 1 }, commission_ref: { artifact_id: 'commission', revision: 1 }, kind: 'direction' as const, status: 'pending' as const, summary: '保留问答，替换产品', proposal: { keep: '双人问答', plan_ref: { artifact_id: 'private-plan-id', revision: 7 }, node_id: 'private-node-id' } }], material_work: [] });
describe('conversation production surface', () => {
 beforeEach(() => { vi.clearAllMocks(); useAuthStore.setState({ username: 'alice' }); vi.mocked(listReferenceAnalysisRuns).mockResolvedValue([]); });
 it('keeps the compact summary free of direction confirmation controls', async () => {
   const summary = document.createElement('div'); document.body.append(summary);
   vi.mocked(getReferenceDirectorState).mockResolvedValue(direction());
   render(<ReferenceDirectorPanel {...props} summaryTarget={summary} defaultExpanded={false} />);
   await screen.findByRole('region', { name: '虾导制作进度' });
   expect(screen.queryByRole('button', { name: '确认复刻方向' })).toBeNull();
   expect(within(summary).queryByRole('button', { name: '确认复刻方向' })).toBeNull();
   expect(screen.queryByText(/private-plan-id|private-node-id/)).toBeNull();
   summary.remove();
 });
 it('does not add a direction card in conversation mode', async () => {
   const summary = document.createElement('div'); document.body.append(summary);
   vi.mocked(getReferenceDirectorState).mockResolvedValue(direction());
   render(<ReferenceDirectorPanel {...props} summaryTarget={summary} defaultExpanded={false} conversationMode />);
   await screen.findByRole('region', { name: '作品制作操作' });
   expect(screen.queryByRole('button', { name: '确认复刻方向' })).toBeNull();
   expect(screen.queryByText('等待确认复刻方向')).toBeNull();
   expect(screen.queryByRole('button', { name: /虾导制作进度/ })).toBeNull();
   expect(summary.textContent).toBe('');
   summary.remove();
 });
 it('does not add a reviewed plan confirmation card in conversation mode', async () => {
   const summary = document.createElement('div'); document.body.append(summary);
   const current = direction();
   vi.mocked(getReferenceDirectorState).mockResolvedValue({ ...current,
     commissions: [{ ...current.commissions[0], phase: 'awaiting_plan_confirmation' as const,
       commission_ref: { artifact_id: 'commission', revision: 2 } }],
     decisions: [{ decision_ref: { artifact_id: 'plan-decision', revision: 1 },
       commission_ref: { artifact_id: 'commission', revision: 2 }, kind: 'plan' as const,
       status: 'pending' as const, summary: '已审核的产品方案',
       proposal: { plan_ref: { artifact_id: 'plan', revision: 1 } } }],
   });
   render(<ReferenceDirectorPanel {...props} summaryTarget={summary} defaultExpanded={false} conversationMode />);
   await screen.findByRole('region', { name: '作品制作操作' });
   expect(screen.queryByRole('button', { name: '确认作品方案' })).toBeNull();
   expect(screen.queryByText('已审核的产品方案')).toBeNull();
   summary.remove();
 });
 it('retains drafting failure recovery for the exact current run without starting another analysis', async () => {
   const saved = { run_id: 'original-run', project_id: 'p', canvas_id: 'c', agent_id: 'main', skill_id: 'reference-video-remix', source_node_id: 'video', analysis_node_id: 'analysis', source_video_url: '/source.mp4', status: 'resume_failed' as const, task_id: 'original-analysis-task', commission_ref: { artifact_id: 'commission', revision: 2 }, phase: 'drafting_plan' as const, error: '方案生成中断', execution_blocker: { code: 'provider_connection_error', next_action: 'retry', message: '方案生成中断' }, created_at: '2026-10-11', updated_at: '2026-10-11' };
   vi.mocked(listReferenceAnalysisRuns).mockResolvedValue([saved]); vi.mocked(retryReferenceAnalysisRun).mockResolvedValue({ ...saved, status: 'resuming', error: null });
   const production = { scope: { project_id: 'p', canvas_id: 'c', agent_id: 'main' }, analysis_node_id: 'analysis', commission_ref: saved.commission_ref, phase: 'drafting_plan' } as never;
   render(<ReferenceAnalysisStatus {...props} currentProduction={production} />);
   fireEvent.click(await screen.findByRole('button', { name: '继续准备方案' }));
   await waitFor(() => expect(retryReferenceAnalysisRun).toHaveBeenCalledExactlyOnceWith('p', 'c', 'original-run'));
 });
});

import { ReferenceProductionPanel } from './ReferenceProductionPanel';
import { getReferenceProductionState, getReferenceConversationCards } from '@/api/referenceDirector';
vi.mock('@/api/referenceDirector', async (original) => ({ ...await original<typeof import('@/api/referenceDirector')>(), getReferenceDirectorState: vi.fn(), confirmReferenceDecision: vi.fn(), getReferenceProductionState: vi.fn(), getReferenceConversationCards: vi.fn(), getReferenceMaterialSpeechStatus: vi.fn(async () => ({ status: 'pending', message: '等待原生语音证据' })) }));
it('does not render a legacy saved direction card after loading authorized facts', async () => {
 const summary = document.createElement('div'); document.body.append(summary);
 useAuthStore.setState({ username: 'alice' });
 const commissionRef = { artifact_id: 'commission', revision: 2 };
 const production = { scope: { project_id: 'p', canvas_id: 'c', agent_id: 'main' }, commission_ref: commissionRef, phase: 'drafting_plan', execution_status: 'running', progress: { materials_available: 0, materials_accepted: 0, materials_total: 0, user_action_count: 0 }, director_state: { ...direction(), decisions: [], commissions: [{ ...direction().commissions[0], commission_ref: commissionRef, phase: 'drafting_plan' }] } } as never;
 vi.mocked(getReferenceProductionState).mockResolvedValue(production);
 vi.mocked(getReferenceConversationCards).mockResolvedValue({ scope: { project_id: 'p', canvas_id: 'c', agent_id: 'main' }, commission_ref: commissionRef, items: [{ card_id: 'direction', ref: { artifact_id: 'direction', revision: 2 }, commission_ref: commissionRef, schema: 'reference_decision.v1', status: 'confirmed', title: '复刻方向', summary: '原生确认的问答方向', reason: '已由用户确认' }] });
 const messages = [{ id: 'native-card', role: 'assistant', text: '', timestamp: 1, parts: [{ id: 'part', type: 'reference_production', event: { type: 'reference.production.card', project_id: 'p', canvas_id: 'c', agent_id: 'main', commission_ref: { artifact_id: 'commission', revision: 1 }, ref: { artifact_id: 'direction', revision: 1 }, status: 'pending' } }] }] as never;
 const view = render(<ReferenceProductionPanel {...props} messages={messages} summaryTarget={summary} />);
 await waitFor(() => expect(getReferenceConversationCards).toHaveBeenCalled());
 expect(screen.queryByRole('article', { name: '复刻方向 · 历史记录' })).toBeNull();
 expect(getReferenceConversationCards).toHaveBeenCalledExactlyOnceWith('p', 'c', 'commission', [{ artifact_id: 'direction', revision: 1 }], 'main');
 view.unmount(); summary.remove();
});

it('filters saved plan confirmation and material review history from the conversation', async () => {
 const commissionRef = { artifact_id: 'commission', revision: 2 };
 vi.mocked(getReferenceProductionState).mockResolvedValue({
   scope: { project_id: 'p', canvas_id: 'c', agent_id: 'main' }, commission_ref: commissionRef,
   phase: 'awaiting_plan_confirmation', execution_status: 'waiting_user',
   progress: { materials_available: 0, materials_accepted: 0, materials_total: 0, user_action_count: 1 },
   director_state: { ...direction(), decisions: [], commissions: [{ ...direction().commissions[0],
     commission_ref: commissionRef, phase: 'awaiting_plan_confirmation' }] },
 } as never);
 vi.mocked(getReferenceConversationCards).mockResolvedValue({
   scope: { project_id: 'p', canvas_id: 'c', agent_id: 'main' }, commission_ref: commissionRef,
   items: [
     { card_id: 'plan', ref: { artifact_id: 'plan', revision: 2 }, commission_ref: commissionRef,
       schema: 'reference_decision.v1', decision_kind: 'plan', status: 'confirmed',
       title: '作品方案', summary: '旧方案确认', reason: '已确认' },
     { card_id: 'material', ref: { artifact_id: 'material', revision: 1 }, commission_ref: commissionRef,
       schema: 'material_review.v1', status: 'confirmed',
       title: '素材审看', summary: '已审看素材', reason: '已由用户确认' },
   ],
 } as never);
 const messages = ['plan', 'material'].map((id) => ({ id, role: 'assistant', text: '', timestamp: 1,
   parts: [{ id: `${id}-part`, type: 'reference_production', event: { type: 'reference.production.card',
     project_id: 'p', canvas_id: 'c', agent_id: 'main', commission_ref: commissionRef,
     ref: { artifact_id: id, revision: 1 } } }] })) as never;
 render(<ReferenceProductionPanel {...props} messages={messages} conversationMode />);
 await waitFor(() => expect(getReferenceConversationCards).toHaveBeenCalled());
 expect(screen.queryByRole('article', { name: '素材审看 · 历史记录' })).toBeNull();
 expect(screen.queryByRole('article', { name: '作品方案 · 历史记录' })).toBeNull();
});

it('keeps a confirmed direction out of the conversation after an ordinary reply', async () => {
 useAuthStore.setState({ username: 'alice' });
 const commissionRef = { artifact_id: 'commission', revision: 2 };
 vi.mocked(getReferenceProductionState).mockResolvedValue({ scope: { project_id: 'p', canvas_id: 'c', agent_id: 'main' }, commission_ref: commissionRef,
   phase: 'drafting_plan', execution_status: 'running', projection_revision: 'current',
   progress: { materials_available: 0, materials_accepted: 0, materials_total: 0, user_action_count: 0 },
   director_state: { ...direction(), decisions: [], commissions: [{ ...direction().commissions[0], commission_ref: commissionRef, phase: 'drafting_plan' }] } } as never);
 vi.mocked(getReferenceConversationCards).mockResolvedValue({ scope: { project_id: 'p', canvas_id: 'c', agent_id: 'main' }, commission_ref: commissionRef,
   items: [{ card_id: 'direction', ref: { artifact_id: 'direction', revision: 2 }, commission_ref: commissionRef,
     schema: 'reference_decision.v1', status: 'confirmed', title: '复刻方向', summary: '保留问答，产品换成黑咖啡', reason: '已由用户确认' }] });
 const messages = [{ id: 'native-card', role: 'assistant', text: '', timestamp: 1, parts: [{ id: 'part', type: 'reference_production', event: {
   type: 'reference.production.card', project_id: 'p', canvas_id: 'c', agent_id: 'main', commission_ref: { artifact_id: 'commission', revision: 1 },
   ref: { artifact_id: 'direction', revision: 1 }, status: 'pending' } }] }] as never;
 render(<ReferenceProductionPanel {...props} messages={messages} conversationMode />);
 await screen.findByRole('article', { name: '作品制作操作' });
 expect(screen.queryByRole('article', { name: '复刻方向 · 历史记录' })).toBeNull();
 expect(screen.queryByRole('button', { name: '确认复刻方向' })).toBeNull();
});

import { confirmReferenceDecision } from '@/api/referenceDirector';
it('leaves a saved direction proposal for an authenticated chat reply', async () => {
 useAuthStore.setState({ username: 'alice' });
 const native = direction();
 const summary = document.createElement('div'); document.body.append(summary);
 const progress = { commission_ref: native.commissions[0].commission_ref, projection_revision: 'f'.repeat(64), work_ref: null, execution_status: 'waiting_user', progress: { user_action_count: 1, materials_available: 0, materials_accepted: 0, materials_total: 0 } } as never;
 render(<ReferenceDirectorPanel {...props} controlledState={native} progress={progress} summaryTarget={summary} onRefresh={async () => {}} />);
 await screen.findByRole('region', { name: '虾导制作进度' });
 expect(screen.queryByRole('button', { name: '确认复刻方向' })).toBeNull();
 expect(confirmReferenceDecision).not.toHaveBeenCalled();
 summary.remove();
});

it('preserves the raw source watching acknowledgement when another material is accepted', async () => {
 useAuthStore.setState({ username: 'alice' });
 const current = { ...direction(), decisions: [], material_reviews: [{ review_ref: { artifact_id: 'voice-review', revision: 1 }, work_ref: { artifact_id: 'work', revision: 1 }, current_work_ref: { artifact_id: 'work', revision: 6 }, step_id: 'voice', resource: { media_kind: 'audio' as const, media_path: 'voice.wav', node_id: 'voice' }, technical: { status: 'passed' as const }, content: { status: 'unavailable' as const } }] };
 const summary = document.createElement('div'); document.body.append(summary);
 const progress = { commission_ref: current.commissions[0].commission_ref, work_ref: { artifact_id: 'work', revision: 6 }, projection_revision: 'a', execution_status: 'waiting_user', progress: { user_action_count: 1, materials_available: 1, materials_accepted: 0, materials_total: 1 } } as never;
 const view = render(<ReferenceDirectorPanel {...props} controlledState={current} progress={progress} summaryTarget={summary} />);
 const checkbox = await screen.findByRole('checkbox', { name: '已完整试听当前素材' }); fireEvent.click(checkbox);
 view.rerender(<ReferenceDirectorPanel {...props} controlledState={{ ...current, material_reviews: [{ ...current.material_reviews[0], current_work_ref: { artifact_id: 'work', revision: 7 } }] }} progress={{ ...(progress as object), work_ref: { artifact_id: 'work', revision: 7 }, projection_revision: 'b' } as never} summaryTarget={summary} />);
 await waitFor(() => expect(screen.getByRole('checkbox', { name: '已完整试听当前素材' })).toBeChecked());
 view.unmount(); summary.remove();
});

it('names a failed drafting phase truthfully', async () => {
 const summary = document.createElement('div'); document.body.append(summary);
 const current = { ...direction(), decisions: [], commissions: [{ ...direction().commissions[0], phase: 'drafting_plan' as const }] };
 const progress = { commission_ref: current.commissions[0].commission_ref, work_ref: null, execution_status: 'failed', progress: { user_action_count: 0, materials_available: 0, materials_accepted: 0, materials_total: 0 } } as never;
 render(<ReferenceDirectorPanel {...props} controlledState={current} progress={progress} summaryTarget={summary} />);
 await screen.findAllByText('作品方案准备失败');
 expect(screen.queryByText('素材制作遇到问题')).toBeNull(); summary.remove();
});

it('rejects a native retry response for another run and never replaces the original analysis identity', async () => {
 const saved = { run_id: 'original-run', project_id: 'p', canvas_id: 'c', agent_id: 'main', skill_id: 'reference-video-remix', source_node_id: 'video', analysis_node_id: 'analysis', source_video_url: '/source.mp4', status: 'resume_failed' as const, task_id: 'original-task', phase: 'drafting_plan' as const, created_at: '2026-10-11', updated_at: '2026-10-11' };
 vi.mocked(listReferenceAnalysisRuns).mockResolvedValue([saved]);
 vi.mocked(retryReferenceAnalysisRun).mockResolvedValue({ ...saved, run_id: 'other-run', task_id: 'other-task', status: 'resuming' });
 render(<ReferenceAnalysisStatus {...props} />);
 fireEvent.click(await screen.findByRole('button', { name: '继续准备方案' }));
 await screen.findByText('继续结果与原分析任务不匹配，请刷新状态。');
 expect(screen.queryByText('虾导正在整理复刻建议')).toBeNull();
});

it('hydrates all twenty-six persisted card references in bounded pages with an explicit older-history action', async () => {
 useAuthStore.setState({ username: 'alice' });
 const commissionRef = { artifact_id: 'commission', revision: 2 };
 const production = { scope: { project_id: 'p', canvas_id: 'c', agent_id: 'main' }, commission_ref: commissionRef, projection_revision: 'stable', phase: 'drafting_plan', execution_status: 'running', progress: { materials_available: 0, materials_accepted: 0, materials_total: 0, user_action_count: 0 }, director_state: { ...direction(), decisions: [], commissions: [{ ...direction().commissions[0], commission_ref: commissionRef, phase: 'drafting_plan' }] } } as never;
 vi.mocked(getReferenceProductionState).mockResolvedValue(production);
 vi.mocked(getReferenceConversationCards).mockImplementation(async (_p,_c,_commission,refs) => ({ scope: { project_id: 'p', canvas_id: 'c', agent_id: 'main' }, commission_ref: commissionRef, items: refs.map((ref) => ({ card_id: ref.artifact_id, ref, commission_ref: commissionRef, schema: 'reference_decision.v1', status: 'pending' as const, title: ref.artifact_id, summary: '真实记录', reason: '当前事实' })) }));
 const messages = Array.from({ length: 26 }, (_,i) => ({ id: `m${i}`, role: 'assistant', text: '', timestamp: i, parts: [{ id: `part${i}`, type: 'reference_production', event: { type: 'reference.production.card', project_id: 'p', canvas_id: 'c', agent_id: 'main', commission_ref: commissionRef, ref: { artifact_id: `card${i}`, revision: 1 } } }] })) as never;
 render(<ReferenceProductionPanel {...props} messages={messages} />);
 await screen.findByRole('article', { name: 'card25 · 历史记录' });
 expect(screen.queryByRole('article', { name: 'card0 · 历史记录' })).toBeNull();
 fireEvent.click(screen.getByRole('button', { name: '显示更早的制作卡片' }));
 await screen.findByRole('article', { name: 'card0 · 历史记录' });
 expect(screen.getAllByRole('article').filter((node) => node.getAttribute('aria-label')?.includes('历史记录'))).toHaveLength(26);
 expect(vi.mocked(getReferenceConversationCards).mock.calls.every((call) => call[3].length <= 20)).toBe(true);
});
