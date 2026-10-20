import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { getReferenceMaterialSpeechStatus, saveReferenceMaterialSpeechCorrection, type ReferenceMaterialSpeechStatus, type DirectorMaterialReview } from '@/api/referenceDirector';
import type { MaterialSpeechReviewSummary } from './MaterialSpeechReview';
import { useAuthStore } from '@/stores/auth-store';
import { MaterialSpeechReviewCard } from './MaterialSpeechReviewCard';
vi.mock('@/api/referenceDirector', () => ({ getReferenceMaterialSpeechStatus: vi.fn(), saveReferenceMaterialSpeechCorrection: vi.fn() }));
const review: DirectorMaterialReview = { review_ref: { artifact_id: 'review', revision: 2 }, work_ref: { artifact_id: 'work', revision: 3 }, step_id: 'voice', resource: { media_path: 'media/source.wav', media_kind: 'audio', node_id: 'node' }, technical: { status: 'passed', checks: [], findings: [] }, content: { status: 'unavailable', checks: [], findings: [] } };
const ready: ReferenceMaterialSpeechStatus = { status: 'ready', work_ref: review.work_ref, plan_ref: { artifact_id: 'plan', revision: 1 }, step_id: 'voice', node_id: 'node', summary: { observation_ref: { artifact_id: 'obs', revision: 1 }, target_signature: 'target', target_text: '品牌', recognized_text: '品排', recognized_words: [{ id: 'w', text: '品排', start_us: 0, end_us: 100000 }], recognized_segments: [], timing_granularity: 'word', transcript_match: 'review_required', audio_review: 'unavailable', lip_review: 'unavailable' } };
const props = { projectId: 'p', canvasId: 'c', agentId: 'main', review, active: true, refreshToken: 1, onSeek: vi.fn(), onSaved: vi.fn(async () => {}) };
const correct = () => {
  fireEvent.change(screen.getByRole('textbox', { name: '识别词 1' }), { target: { value: '品牌' } });
  fireEvent.click(screen.getByRole('checkbox', { name: '已试听并核对本次修改' }));
  fireEvent.click(screen.getByRole('button', { name: '核对并保存' }));
};
describe('material speech evidence lifecycle', () => {
  beforeEach(() => { vi.clearAllMocks(); vi.mocked(getReferenceMaterialSpeechStatus).mockResolvedValue(ready); });
  it('rejects evidence for another current work before displaying a correction editor', async () => {
    vi.mocked(getReferenceMaterialSpeechStatus).mockResolvedValue({ ...ready, work_ref: { artifact_id: 'old-work', revision: 1 } });
    render(<MaterialSpeechReviewCard {...props} />);
    await screen.findByText(/素材已变化/);
    expect(screen.queryByRole('textbox')).toBeNull();
  });
  it('ignores old evidence after switching canvases and returning to the same URL', async () => {
    let release!: (value: ReferenceMaterialSpeechStatus) => void;
    vi.mocked(getReferenceMaterialSpeechStatus).mockReturnValueOnce(new Promise((resolve) => { release = resolve; })).mockResolvedValue({ status: 'pending', message: '当前检查未完成' });
    const view = render(<MaterialSpeechReviewCard {...props} />);
    view.rerender(<MaterialSpeechReviewCard {...props} canvasId="other" />);
    await screen.findByText('当前检查未完成');
    view.rerender(<MaterialSpeechReviewCard {...props} />);
    await waitFor(() => expect(getReferenceMaterialSpeechStatus).toHaveBeenCalledTimes(3));
    await act(async () => release(ready));
    expect(screen.queryByText('品排')).toBeNull();
    expect(screen.queryByRole('textbox')).toBeNull();
  });
  it('retains edits and reuses the exact command after an uncertain save failure', async () => {
    vi.mocked(saveReferenceMaterialSpeechCorrection).mockRejectedValue(new Error('暂时无法保存'));
    render(<MaterialSpeechReviewCard {...props} />);
    await screen.findByRole('textbox');
    correct();
    await screen.findByText('暂时无法保存');
    expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('品牌');
    fireEvent.click(screen.getByRole('button', { name: '核对并保存' }));
    await waitFor(() => expect(saveReferenceMaterialSpeechCorrection).toHaveBeenCalledTimes(2));
    expect(vi.mocked(saveReferenceMaterialSpeechCorrection).mock.calls[1][2]).toEqual(vi.mocked(saveReferenceMaterialSpeechCorrection).mock.calls[0][2]);
    expect(props.onSaved).not.toHaveBeenCalled();
  });
  it('ignores an older readonly response after saving and restores the corrected words', async () => {
    let release!: (value: ReferenceMaterialSpeechStatus) => void;
    vi.mocked(getReferenceMaterialSpeechStatus).mockResolvedValueOnce(ready).mockReturnValueOnce(new Promise((resolve) => { release = resolve; }));
    vi.mocked(saveReferenceMaterialSpeechCorrection).mockResolvedValue({ summary: { ...ready.summary, corrected_words: [{ ...ready.summary.recognized_words[0], text: '品牌' }] } });
    const view = render(<MaterialSpeechReviewCard {...props} />);
    await screen.findByRole('textbox');
    view.rerender(<MaterialSpeechReviewCard {...props} refreshToken={2} />);
    correct();
    await waitFor(() => expect(props.onSaved).toHaveBeenCalledTimes(1));
    await act(async () => release(ready));
    expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('品牌');
    expect(screen.getByText('品排')).toBeTruthy();
  });
});

it.each(['pending', 'unavailable'] as const)('retires ready speech actions when a new read reports %s evidence', async (status) => {
  vi.clearAllMocks();
  vi.mocked(getReferenceMaterialSpeechStatus).mockResolvedValueOnce(ready).mockResolvedValue({ status, message: '当前素材尚无可用识别' });
  const view = render(<MaterialSpeechReviewCard {...props} />);
  await screen.findByRole('textbox');
  view.rerender(<MaterialSpeechReviewCard {...props} refreshToken={2} />);
  await screen.findByText('当前素材尚无可用识别');
  expect(screen.queryByRole('textbox')).toBeNull();
  expect(screen.queryByRole('button', { name: '核对并保存' })).toBeNull();
});
it('retires prior ready speech actions after a readonly status error', async () => {
  vi.clearAllMocks();
  vi.mocked(getReferenceMaterialSpeechStatus).mockResolvedValueOnce(ready).mockRejectedValue(new Error('当前来源已变化'));
  const view = render(<MaterialSpeechReviewCard {...props} />);
  await screen.findByRole('textbox');
  view.rerender(<MaterialSpeechReviewCard {...props} refreshToken={2} />);
  await screen.findByText('当前来源已变化');
  expect(screen.queryByRole('textbox')).toBeNull();
});
it('does not restore retired evidence when an old correction save finishes late', async () => {
  vi.clearAllMocks();
  let release!: (result: { summary: MaterialSpeechReviewSummary }) => void;
  vi.mocked(getReferenceMaterialSpeechStatus).mockResolvedValueOnce(ready).mockResolvedValue({ status: 'pending', message: '当前识别未完成' });
  vi.mocked(saveReferenceMaterialSpeechCorrection).mockReturnValue(new Promise((resolve) => { release = resolve; }));
  const view = render(<MaterialSpeechReviewCard {...props} />);
  await screen.findByRole('textbox'); correct();
  view.rerender(<MaterialSpeechReviewCard {...props} refreshToken={2} />);
  await screen.findByText('当前识别未完成');
  await act(async () => release({ summary: ready.summary }));
  expect(screen.queryByRole('textbox')).toBeNull();
  expect(props.onSaved).not.toHaveBeenCalled();
});

it('does not retain evidence after an account changes in the same project and candidate', async () => {
  vi.clearAllMocks(); useAuthStore.setState({ username: 'alice' });
  vi.mocked(getReferenceMaterialSpeechStatus).mockResolvedValueOnce(ready).mockResolvedValue({ status: 'unavailable', message: '当前用户暂无识别证据' });
  render(<MaterialSpeechReviewCard {...props} />);
  await screen.findByRole('textbox');
  act(() => useAuthStore.setState({ username: 'bob' }));
  await screen.findByText('当前用户暂无识别证据');
  expect(screen.queryByRole('textbox')).toBeNull();
  expect(getReferenceMaterialSpeechStatus).toHaveBeenCalledTimes(2);
});
it('does not apply a late old-source read after the candidate hash changes', async () => {
  vi.clearAllMocks();
  let release!: (value: ReferenceMaterialSpeechStatus) => void;
  vi.mocked(getReferenceMaterialSpeechStatus).mockReturnValueOnce(new Promise((resolve) => { release = resolve; })).mockResolvedValue({ status: 'pending', message: '新素材识别未完成' });
  const view = render(<MaterialSpeechReviewCard {...props} review={{ ...review, resource: { ...review.resource, media_hash: 'old-hash' } }} />);
  view.rerender(<MaterialSpeechReviewCard {...props} review={{ ...review, resource: { ...review.resource, media_hash: 'new-hash' } }} />);
  await screen.findByText('新素材识别未完成');
  await act(async () => release(ready));
  expect(screen.queryByRole('textbox')).toBeNull();
});

it('preserves edits and listening acknowledgement when an independent accepted item advances the same work', async () => {
 vi.clearAllMocks(); useAuthStore.setState({ username: 'alice' });
 vi.mocked(getReferenceMaterialSpeechStatus).mockResolvedValue(ready);
 const view = render(<MaterialSpeechReviewCard {...props} />);
 await screen.findByRole('textbox', { name: '识别词 1' });
 await waitFor(() => expect(screen.getByRole('textbox', { name: '识别词 1' })).not.toBeDisabled());
 fireEvent.change(screen.getByRole('textbox', { name: '识别词 1' }), { target: { value: '尚未保存的品牌' } });
 fireEvent.click(screen.getByRole('checkbox', { name: '已试听并核对本次修改' }));
 const nextWork = { artifact_id: 'work', revision: 4 };
 vi.mocked(getReferenceMaterialSpeechStatus).mockResolvedValue({ ...ready, work_ref: nextWork });
 view.rerender(<MaterialSpeechReviewCard {...props} review={{ ...review, current_work_ref: nextWork }} refreshToken={2} />);
 await waitFor(() => expect(getReferenceMaterialSpeechStatus).toHaveBeenCalledTimes(2));
 expect((screen.getByRole('textbox', { name: '识别词 1' }) as HTMLInputElement).value).toBe('尚未保存的品牌');
 expect(screen.getByRole('checkbox', { name: '已试听并核对本次修改' })).toBeChecked();
 expect(vi.mocked(getReferenceMaterialSpeechStatus).mock.calls[1][2].work_ref).toEqual(nextWork);
});

it('retires a pending correction response when the current work advances without losing edits', async () => {
 vi.clearAllMocks(); useAuthStore.setState({ username: 'alice' });
 let release!: (value: { summary: MaterialSpeechReviewSummary }) => void;
 vi.mocked(getReferenceMaterialSpeechStatus).mockResolvedValue(ready);
 vi.mocked(saveReferenceMaterialSpeechCorrection).mockReturnValue(new Promise((resolve) => { release = resolve; }));
 const view = render(<MaterialSpeechReviewCard {...props} />);
 await screen.findByRole('textbox'); await waitFor(() => expect(screen.getByRole('textbox')).not.toBeDisabled()); correct();
 expect(screen.getByRole('button', { name: '保存中…' })).toBeDisabled();
 const nextWork = { artifact_id: 'work', revision: 4 };
 vi.mocked(getReferenceMaterialSpeechStatus).mockResolvedValue({ ...ready, work_ref: nextWork });
 view.rerender(<MaterialSpeechReviewCard {...props} review={{ ...review, current_work_ref: nextWork }} refreshToken={2} />);
 await waitFor(() => expect(screen.getByRole('button', { name: '核对并保存' })).not.toBeDisabled());
 await act(async () => release({ summary: { ...ready.summary, corrected_words: [{ ...ready.summary.recognized_words[0], text: '旧请求结果' }] } }));
 expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('品牌');
 expect(props.onSaved).not.toHaveBeenCalled();
});
