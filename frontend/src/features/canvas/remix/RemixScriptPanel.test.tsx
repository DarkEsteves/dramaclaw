import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
vi.mock('./remixScriptArtifact', () => ({ saveRemixScriptArtifact: vi.fn(async () => ({ artifactId: 'script-1', revision: 1 })) }));
vi.mock('@/api/remix', () => ({ fetchRemixArtifact: vi.fn(async () => ({ schema: 'reference_blueprint.v1', payload: { narrative_role: '用早晨疲惫引出咖啡', events: [], preserve_replace_rules: [], observations: [], inferences: [], user_choices: [] } })) }));
import { saveRemixScriptArtifact } from './remixScriptArtifact';
import { RemixScriptPanel } from './RemixScriptPanel';

describe('RemixScriptPanel', () => {
  it('marks a selected phrase and saves a versioned script artifact', async () => {
    const onSave = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '喝下咖啡', selections: [] }} existingRef={null} blueprintRef={{ artifactId: 'blueprint-1', revision: 1 }} visualNodes={[{ id: 'video-1', label: '咖啡画面' }]} onDraftChange={() => {}} onSave={onSave} onClose={() => {}} />);
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
});
