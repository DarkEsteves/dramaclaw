import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { http, HttpResponse } from 'msw';
import { server } from '@/__mocks__/msw/server';
import { useAuthStore } from '@/stores/auth-store';
import { ReferenceProductionPanel } from './ReferenceProductionPanel';

vi.mock('@/api/client', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/api/client')>();
  return { ...original, apiCall: <T,>(path: string, options?: import('ky').Options) => original.apiCall<T>(path, { ...options, prefix: 'http://localhost/api/v1' }) };
});

// Wire shape emitted by _saved_material_reviews: the historical evidence ref is
// preserved while its independent candidate remains compatible with current r2.
const current = { artifact_id: 'work', revision: 2 };
const original = { artifact_id: 'work', revision: 1 };
const reviewRef = { artifact_id: 'voice-review', revision: 1 };
const compactReview = {
  review_ref: reviewRef, work_ref: original, current_work_ref: current,
  matches_current_work_revision: false, review_compatible_with_current_work: true,
  step_id: 'voice', resource: { id: 'material_voice', node_id: 'voice-node', duration_us: 300000, has_audio: true, media_path: 'media/voice.wav', media_kind: 'audio', media_hash: 'a'.repeat(64) },
  technical: { status: 'passed' }, content: { status: 'unavailable' }, actor_kind: 'system', upstream_refs: [],
};
const summary = {
  observation_ref: { artifact_id: 'observation', revision: 1 }, target_signature: 'target', target_text: '品牌', recognized_text: '品排',
  recognized_words: [{ id: 'word', text: '品排', start_us: 0, end_us: 300000 }], recognized_segments: [], timing_granularity: 'word',
  transcript_match: 'review_required', audio_review: 'unavailable', lip_review: 'unavailable',
};
const path = '*/api/v1/projects/p/freezone/canvases/c/director';
const production = (review = compactReview) => JSON.parse(JSON.stringify({
  schema_version: 'reference_production_state.v1', scope: { project_id: 'p', canvas_id: 'c', agent_id: 'main' },
  commission_ref: { artifact_id: 'commission', revision: 2 }, plan_ref: { artifact_id: 'plan', revision: 1 }, work_ref: current,
  projection_revision: 'current-facts', phase: 'preparing_materials', execution_status: 'waiting_user',
  progress: { materials_total: 2, materials_available: 2, materials_accepted: 1, final_materials_total: 2, final_materials_accepted: 1, user_action_count: 1 },
  blockers: [], next_action: { owner: 'user', kind: 'review_materials', action_id: 'voice-review' },
  director_state: { commissions: [{ commission_ref: { artifact_id: 'commission', revision: 2 }, project_id: 'p', canvas_id: 'c', phase: 'preparing_materials', goal: '兼容素材继续审看' }], decisions: [], material_work: [], material_reviews: [review], speech_corrections: [] },
}));
describe('current production HTTP contracts', () => {
  beforeEach(() => { localStorage.clear(); useAuthStore.setState({ username: 'alice' }); });
  it('renders the compact saved-review wire shape while folded without requiring legacy arrays', async () => {
    server.use(http.get(`${path}/production-state`, () => HttpResponse.json({ ok: true, data: production() })));
    render(<ReferenceProductionPanel projectId="p" canvasId="c" agentId="main" connected />);
    await screen.findByText(/素材就绪 2\/2/);
    expect(screen.getByRole('button', { name: '展开虾导制作进度' })).toBeTruthy();
  });
  it('reads and corrects compatible historical evidence against current r2 and batches the immutable review ref', async () => {
    const received: Record<string, unknown[]> = { status: [], correction: [], batch: [] };
    let savedSummary: typeof summary & { corrected_words?: typeof summary.recognized_words } = summary;
    const requireCurrent = (input: Record<string, unknown>) => JSON.stringify(input.work_ref) === JSON.stringify(current);
    const review = { ...compactReview, technical: { status: 'passed', checks: [], findings: [] }, content: { status: 'unavailable', checks: [], findings: [] } };
    server.use(
      http.get(`${path}/production-state`, () => HttpResponse.json({ ok: true, data: production(review) })),
      http.post(`${path}/material-speech-status`, async ({ request }) => {
        const input = await request.json() as Record<string, unknown>; received.status.push(input);
        if (!requireCurrent(input)) return HttpResponse.json({ error: 'current material work required' }, { status: 409 });
        return HttpResponse.json({ ok: true, data: { status: 'ready', work_ref: current, plan_ref: { artifact_id: 'plan', revision: 1 }, step_id: 'voice', node_id: 'voice-node', summary: savedSummary } });
      }),
      http.post(`${path}/material-speech-correction`, async ({ request }) => {
        const input = await request.json() as Record<string, unknown>; received.correction.push(input);
        if (!requireCurrent(input)) return HttpResponse.json({ error: 'current material work required' }, { status: 409 });
        savedSummary = { ...summary, corrected_words: [{ ...summary.recognized_words[0], text: '品牌' }] };
        return HttpResponse.json({ ok: true, data: { summary: savedSummary } });
      }),
      http.post(`${path}/material-reviews/confirm-batch`, async ({ request }) => {
        const input = await request.json() as { items: Record<string, unknown>[] }; received.batch.push(input);
        if (!input.items.every(requireCurrent)) return HttpResponse.json({ error: 'material review or work target changed' }, { status: 409 });
        return HttpResponse.json({ ok: true, data: { items: [], replayed: false } });
      }),
    );
    render(<ReferenceProductionPanel projectId="p" canvasId="c" agentId="main" connected />);
    fireEvent.click(await screen.findByRole('button', { name: '展开虾导制作进度' }));
    await screen.findByRole('textbox', { name: '识别词 1' });
    await waitFor(() => expect(screen.getByRole('textbox', { name: '识别词 1' })).not.toBeDisabled());
    expect(received.status[0]).toEqual({ work_ref: current, step_id: 'voice', node_id: 'voice-node', language: 'auto' });
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '品牌' } });
    fireEvent.click(screen.getByRole('checkbox', { name: '已试听并核对本次修改' }));
    fireEvent.click(screen.getByRole('button', { name: '核对并保存' }));
    await waitFor(() => expect(received.correction).toHaveLength(1));
    expect(received.correction[0]).toMatchObject({ work_ref: current, observation_ref: summary.observation_ref });
    await waitFor(() => expect(received.status.length).toBeGreaterThanOrEqual(2));
    await waitFor(() => expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('品牌'));
    await waitFor(() => expect(screen.getByRole('button', { name: '核对并保存' }).hasAttribute('disabled')).toBe(true));
    fireEvent.click(screen.getByRole('checkbox', { name: '已完整试听当前素材' }));
    fireEvent.click(screen.getByRole('checkbox', { name: '已审看此素材，加入本次确认' }));
    fireEvent.click(screen.getByRole('button', { name: '确认已勾选的素材' }));
    await waitFor(() => expect(received.batch).toHaveLength(1));
    expect(received.batch[0]).toMatchObject({ items: [{ review_ref: reviewRef, work_ref: current, step_id: 'voice', node_id: 'voice-node' }] });
    expect(compactReview.work_ref).toEqual(original);
  });
});


describe('explicit production recovery HTTP contracts', () => {
  const scope = { project_id: 'p', canvas_id: 'c', agent_id: 'main' };
  const commission = { artifact_id: 'saved-commission', revision: 4 };
  const recoverable = { commission_ref: commission, goal: '已保存的品牌短片', phase: 'preparing_materials' };
  beforeEach(() => { localStorage.clear(); useAuthStore.setState({ username: 'alice' }); });
  it('recovers a cold 409 only after explicit selection and replays a failed command unchanged', async () => {
    const resumes: Record<string, unknown>[] = [];
    let resumed = false;
    server.use(
      http.get(`${path}/production-state`, () => resumed
        ? HttpResponse.json({ ok: true, data: production() })
        : HttpResponse.json({ error: 'production projection is stale' }, { status: 409 })),
      http.get(`${path}/production-recovery`, ({ request }) => {
        expect(new URL(request.url).searchParams.get('agent_id')).toBe('main');
        return HttpResponse.json({ ok: true, data: { scope, items: [recoverable, { ...recoverable, commission_ref: { artifact_id: 'other', revision: 1 }, goal: '另一份方案' }] } });
      }),
      http.post(`${path}/resume-production`, async ({ request }) => {
        resumes.push(await request.json() as Record<string, unknown>);
        if (resumes.length === 1) return HttpResponse.json({ error: '继续请求未送达，请重试' }, { status: 409 });
        resumed = true;
        return HttpResponse.json({ ok: true, data: { accepted: true, commission_ref: commission, scope } });
      }),
    );
    render(<ReferenceProductionPanel projectId="p" canvasId="c" agentId="main" connected />);
    const chooser = await screen.findByRole('combobox', { name: '选择要恢复的作品方案' });
    expect((chooser as HTMLSelectElement).value).toBe('');
    const resume = screen.getByRole('button', { name: '恢复这份方案' });
    expect(resume.hasAttribute('disabled')).toBe(true);
    expect(resumes).toHaveLength(0);
    fireEvent.change(chooser, { target: { value: commission.artifact_id } });
    fireEvent.click(resume);
    await screen.findByText('继续请求未送达，请重试');
    fireEvent.click(screen.getByRole('button', { name: '恢复这份方案' }));
    await screen.findByText(/素材就绪 2\/2/);
    expect(resumes).toHaveLength(2);
    expect(resumes[0]).toEqual(resumes[1]);
    expect(resumes[0]).toEqual({ commission_ref: commission, agent_id: 'main', command_id: expect.any(String) });
  });
  it('continues a stopped current branch through the public resume endpoint', async () => {
    const stopped = production(); stopped.execution_status = 'stopped';
    let payload: unknown;
    server.use(
      http.get(`${path}/production-state`, () => payload
        ? HttpResponse.json({ error: 'production projection is stale' }, { status: 409 })
        : HttpResponse.json({ ok: true, data: stopped })),
      http.post(`${path}/resume-production`, async ({ request }) => {
        payload = await request.json();
        return HttpResponse.json({ ok: true, data: { accepted: true, commission_ref: { ...stopped.commission_ref, revision: stopped.commission_ref.revision + 1 }, scope } });
      }),
    );
    render(<ReferenceProductionPanel projectId="p" canvasId="c" agentId="main" connected />);
    fireEvent.click(await screen.findByRole('button', { name: '继续当前方案' }));
    await screen.findByText('已提交继续请求，正在更新制作状态…');
    expect(payload).toEqual({ commission_ref: stopped.commission_ref, agent_id: 'main', command_id: expect.any(String) });
    await screen.findByText('制作状态正在更新，请稍后再试。');
    expect(screen.getByRole('button', { name: '继续当前方案' }).hasAttribute('disabled')).toBe(true);
    expect(screen.queryByText(/production projection/)).toBeNull();
  });
  it('keeps accepted cold recovery honest and manually refreshable while state is still refreshing', async () => {
    let resumeCount = 0;
    let settled = false;
    server.use(
      http.get(`${path}/production-state`, () => settled ? HttpResponse.json({ ok: true, data: production() }) : HttpResponse.json({ error: 'production projection is stale' }, { status: 409 })),
      http.get(`${path}/production-recovery`, () => HttpResponse.json({ ok: true, data: { scope, items: [recoverable] } })),
      http.post(`${path}/resume-production`, () => { resumeCount += 1; return HttpResponse.json({ ok: true, data: { accepted: true, commission_ref: commission, scope } }); }),
    );
    render(<ReferenceProductionPanel projectId="p" canvasId="c" agentId="main" connected />);
    fireEvent.change(await screen.findByRole('combobox', { name: '选择要恢复的作品方案' }), { target: { value: commission.artifact_id } });
    fireEvent.click(screen.getByRole('button', { name: '恢复这份方案' }));
    await screen.findByText('已提交继续请求，正在更新制作状态…');
    expect(resumeCount).toBe(1);
    expect(screen.queryByText(/production projection/)).toBeNull();
    const refresh = screen.getByRole('button', { name: '刷新制作状态' });
    expect(refresh.hasAttribute('disabled')).toBe(false);
    expect(screen.getByRole('button', { name: '恢复这份方案' }).hasAttribute('disabled')).toBe(true);
    settled = true;
    fireEvent.click(refresh);
    await screen.findByText(/素材就绪 2\/2/);
    expect(resumeCount).toBe(1);
    expect(screen.queryByText('已提交继续请求，正在更新制作状态…')).toBeNull();
  });
  it('discards delayed recovery when the scope changes', async () => {
    let release!: () => void;
    let requested = false;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    server.use(
      http.get(`${path}/production-state`, () => HttpResponse.json({ error: 'stale' }, { status: 409 })),
      http.get(`${path}/production-recovery`, async () => { requested = true; await gate; return HttpResponse.json({ ok: true, data: { scope, items: [recoverable] } }); }),
      http.get('*/api/v1/projects/p/freezone/canvases/new/director/production-state', () => {
        const next = production(); next.scope.canvas_id = 'new';
        return HttpResponse.json({ ok: true, data: next });
      }),
    );
    const view = render(<ReferenceProductionPanel projectId="p" canvasId="c" agentId="main" connected />);
    await waitFor(() => expect(requested).toBe(true));
    view.rerender(<ReferenceProductionPanel projectId="p" canvasId="new" agentId="main" connected />);
    await screen.findByText(/素材就绪 2\/2/);
    release();
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(screen.queryByRole('combobox', { name: '选择要恢复的作品方案' })).toBeNull();
    expect(screen.queryByText('已保存的品牌短片')).toBeNull();
  });
  it('discards the first recovery response after leaving and returning to the same scope', async () => {
    let release!: () => void;
    let reads = 0;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    server.use(
      http.get(`${path}/production-state`, () => HttpResponse.json({ error: 'stale' }, { status: 409 })),
      http.get(`${path}/production-recovery`, async () => {
        reads += 1;
        if (reads === 1) { await gate; return HttpResponse.json({ ok: true, data: { scope, items: [recoverable] } }); }
        return HttpResponse.json({ ok: true, data: { scope, items: [{ ...recoverable, goal: '当前保存的方案' }] } });
      }),
      http.get('*/api/v1/projects/p/freezone/canvases/new/director/production-state', () => HttpResponse.json({ ok: true, data: { branches: [], selection_required: false } })),
    );
    const view = render(<ReferenceProductionPanel projectId="p" canvasId="c" agentId="main" connected />);
    await waitFor(() => expect(reads).toBe(1));
    view.rerender(<ReferenceProductionPanel projectId="p" canvasId="new" agentId="main" connected />);
    view.rerender(<ReferenceProductionPanel projectId="p" canvasId="c" agentId="main" connected />);
    await screen.findByText('当前保存的方案');
    await act(async () => { release(); await new Promise((resolve) => setTimeout(resolve, 30)); });
    expect(screen.getByText('当前保存的方案')).toBeTruthy();
    expect(screen.queryByText('已保存的品牌短片')).toBeNull();
  });
  it('ignores a delayed resume response after the selected commission revision changes', async () => {
    let release!: () => void;
    let posted = false;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const stopped = production(); stopped.execution_status = 'stopped';
    let currentState = stopped;
    server.use(
      http.get(`${path}/production-state`, () => HttpResponse.json({ ok: true, data: currentState })),
      http.post(`${path}/resume-production`, async () => {
        posted = true; await gate;
        return HttpResponse.json({ ok: true, data: { accepted: true, commission_ref: stopped.commission_ref, scope } });
      }),
    );
    render(<ReferenceProductionPanel projectId="p" canvasId="c" agentId="main" connected />);
    fireEvent.click(await screen.findByRole('button', { name: '继续当前方案' }));
    await waitFor(() => expect(posted).toBe(true));
    currentState = production(); currentState.commission_ref.revision = 3;
    currentState.director_state.commissions[0].commission_ref.revision = 3;
    act(() => window.dispatchEvent(new CustomEvent('reference-director-status', { detail: scope })));
    await waitFor(() => expect(screen.queryByRole('button', { name: '继续当前方案' })).toBeNull());
    await act(async () => { release(); await new Promise((resolve) => setTimeout(resolve, 30)); });
    expect(screen.queryByText('已提交继续请求，正在更新制作状态…')).toBeNull();
  });
  it('retains the legacy fallback for a real HTTP 404', async () => {
    let fallbackReads = 0;
    let recoveryReads = 0;
    server.use(
      http.get(`${path}/production-state`, () => HttpResponse.json({ error: 'not found' }, { status: 404 })),
      http.get(`${path}/state`, () => {
        fallbackReads += 1;
        return HttpResponse.json({ ok: true, data: { commissions: [], decisions: [], material_work: [], material_reviews: [], speech_corrections: [] } });
      }),
      http.get(`${path}/production-recovery`, () => { recoveryReads += 1; return HttpResponse.json({ ok: true, data: { scope, items: [] } }); }),
    );
    render(<ReferenceProductionPanel projectId="p" canvasId="c" agentId="main" connected />);
    await waitFor(() => expect(fallbackReads).toBe(1));
    expect(recoveryReads).toBe(0);
    expect(screen.queryByText('not found')).toBeNull();
  });
  it('preserves authorization errors without requesting recovery', async () => {
    let reads = 0;
    server.use(
      http.get(`${path}/production-state`, () => HttpResponse.json({ error: '无权访问此作品方案' }, { status: 403 })),
      http.get(`${path}/production-recovery`, () => { reads += 1; return HttpResponse.json({ ok: true, data: { scope, items: [recoverable] } }); }),
    );
    render(<ReferenceProductionPanel projectId="p" canvasId="c" agentId="main" connected />);
    await screen.findByRole('alert');
    expect(screen.getByText('无权访问此作品方案')).toBeTruthy();
    expect(reads).toBe(0);
  });
});
