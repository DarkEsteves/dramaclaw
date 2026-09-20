import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
vi.mock('./remixScriptArtifact', () => ({ saveRemixScriptArtifact: vi.fn(async () => ({ artifactId: 'script-1', revision: 1 })) }));
vi.mock('@/api/remix', () => ({
  fetchRemixArtifact: vi.fn(async () => ({ schema: 'reference_blueprint.v1', payload: { narrative_role: '用早晨疲惫引出咖啡', events: [], preserve_replace_rules: [], observations: [], inferences: [], user_choices: [] } })),
  generateRemixScriptDraft: vi.fn(async () => ({ dialogue: '新主题来了，喝一口咖啡。', selections: [{ id: 'generated-1', label: '产品登场', start: 9, end: 11 }] })),
}));
import { saveRemixScriptArtifact } from './remixScriptArtifact';
import { generateRemixScriptDraft } from '@/api/remix';
import { RemixScriptPanel } from './RemixScriptPanel';

describe('RemixScriptPanel', () => {
  beforeEach(() => vi.clearAllMocks());
  it('marks a selected phrase and saves a versioned script artifact', async () => {
    const onSave = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '喝下咖啡', selections: [] }} existingRef={null} blueprintRef={{ artifactId: 'blueprint-1', revision: 1 }} visualNodes={[{ id: 'video-1', label: '咖啡画面' }]} sfxNodes={[]} onDraftChange={() => {}} onSave={onSave} onClose={() => {}} />);
    const dialogue = screen.getByLabelText('改编台词') as HTMLTextAreaElement;
    expect(await screen.findByText('用早晨疲惫引出咖啡')).toBeTruthy();
    dialogue.focus();
    dialogue.setSelectionRange(2, 4);
    fireEvent.click(screen.getByRole('button', { name: '标记选中文本' }));
    fireEvent.change(screen.getByLabelText('产品登场画面'), { target: { value: 'video-1' } });
    fireEvent.click(screen.getByRole('button', { name: '保存脚本版本' }));
    await waitFor(() => expect(saveRemixScriptArtifact).toHaveBeenCalledWith('demo', expect.objectContaining({ selections: [expect.objectContaining({ start: 2, end: 4, visualNodeId: 'video-1' })] }), null, { artifactId: 'blueprint-1', revision: 1 }));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith({ artifactId: 'script-1', revision: 1 }));
  });

  it('generates an editable script from the current blueprint and topic before saving', async () => {
    const onSave = vi.fn();
    const onDraftChange = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '旧台词', selections: [] }} existingRef={null} blueprintRef={{ artifactId: 'blueprint-1', revision: 1 }} visualNodes={[]} sfxNodes={[]} onDraftChange={onDraftChange} onSave={onSave} onClose={() => {}} />);
    fireEvent.change(screen.getByLabelText('新主题'), { target: { value: '新咖啡' } });
    fireEvent.click(screen.getByRole('button', { name: '按蓝图生成' }));

    await waitFor(() => expect(generateRemixScriptDraft).toHaveBeenCalledWith('demo', { artifactId: 'blueprint-1', revision: 1 }, '新咖啡'));
    expect((await screen.findByLabelText('改编台词') as HTMLTextAreaElement).value).toBe('新主题来了，喝一口咖啡。');
    expect(onDraftChange).toHaveBeenCalledWith(expect.objectContaining({ selections: [expect.objectContaining({ label: '产品登场' })] }));
    expect(saveRemixScriptArtifact).not.toHaveBeenCalled();
    expect(onSave).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '保存脚本版本' }));
    await waitFor(() => expect(saveRemixScriptArtifact).toHaveBeenCalledWith('demo', {
      dialogue: '新主题来了，喝一口咖啡。', selections: [{ id: 'generated-1', label: '产品登场', start: 9, end: 11 }],
    }, null, { artifactId: 'blueprint-1', revision: 1 }));
    expect(onSave).toHaveBeenCalledWith({ artifactId: 'script-1', revision: 1 });
  });

  it('keeps the current draft when generation fails', async () => {
    vi.mocked(generateRemixScriptDraft).mockRejectedValueOnce(new Error('模型输出无效'));
    const onDraftChange = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '原稿', selections: [] }} existingRef={null} blueprintRef={{ artifactId: 'blueprint-1', revision: 1 }} visualNodes={[]} sfxNodes={[]} onDraftChange={onDraftChange} onSave={() => {}} onClose={() => {}} />);
    fireEvent.change(screen.getByLabelText('新主题'), { target: { value: '茶' } });
    fireEvent.click(screen.getByRole('button', { name: '按蓝图生成' }));

    expect(await screen.findByRole('alert')).toHaveProperty('textContent', '模型输出无效');
    expect((screen.getByLabelText('改编台词') as HTMLTextAreaElement).value).toBe('原稿');
    expect(onDraftChange).not.toHaveBeenCalled();
  });
});
