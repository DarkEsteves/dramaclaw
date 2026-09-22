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
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '喝下咖啡', selections: [] }} existingRef={null} blueprintRef={{ artifactId: 'blueprint-1', revision: 1 }} visualNodes={[{ id: 'video-1', label: '咖啡画面', mediaRevision: 'a'.repeat(64) }]} sfxNodes={[]} onDraftChange={() => {}} onSave={onSave} onClose={() => {}} />);
    const dialogue = screen.getByLabelText('改编台词') as HTMLTextAreaElement;
    expect(await screen.findByText('用早晨疲惫引出咖啡')).toBeTruthy();
    dialogue.focus();
    dialogue.setSelectionRange(2, 4);
    expect(screen.getByText('选中一段台词后添加为语义事件，用来指定这段台词需要搭配的补充画面（B-roll）或音效。')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '将选中台词添加为事件' }));
    expect(screen.getByText('补充画面（B-roll）')).toBeVisible();
    expect(screen.getByText('手动添加')).toBeVisible();
    fireEvent.change(screen.getByLabelText('产品登场画面'), { target: { value: 'video-1' } });
    fireEvent.click(screen.getByRole('button', { name: '保存脚本版本' }));
    await waitFor(() => expect(saveRemixScriptArtifact).toHaveBeenCalledWith('demo', expect.objectContaining({ selections: [expect.objectContaining({ start: 2, end: 4, visualNodeId: 'video-1', visualMediaRevision: 'a'.repeat(64) })] }), null, { artifactId: 'blueprint-1', revision: 1 }));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith({ artifactId: 'script-1', revision: 1 }));
  });

  it('labels AI events and inserts a manual event in script order', async () => {
    const onDraftChange = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '甲乙丙丁戊己', selections: [{ id: 'generated-1', label: '后段事件', start: 4, end: 6 }] }} existingRef={null} blueprintRef={{ artifactId: 'blueprint-1', revision: 1 }} visualNodes={[]} sfxNodes={[]} onDraftChange={onDraftChange} onSave={() => {}} onClose={() => {}} />);

    expect(screen.getByText('台词事件（1）')).toBeVisible();
    expect(screen.getByText('AI 已根据台词自动拆分，你也可以选中台词手动补充事件。')).toBeVisible();
    expect(screen.getByText('AI 生成')).toBeVisible();
    const dialogue = screen.getByLabelText('改编台词') as HTMLTextAreaElement;
    dialogue.focus();
    dialogue.setSelectionRange(1, 3);
    fireEvent.click(screen.getByRole('button', { name: '将选中台词添加为事件' }));

    expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({ selections: [
      expect.objectContaining({ start: 1, end: 3, source: 'manual' }),
      expect.objectContaining({ id: 'generated-1', start: 4, end: 6 }),
    ] }));
    expect(screen.getByText('台词事件（2）')).toBeVisible();
    expect(screen.getByText('手动添加')).toBeVisible();
    await screen.findByText('用早晨疲惫引出咖啡');
  });

  it('recognizes manual events saved before source labels were introduced', () => {
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '旧的手动事件', selections: [{ id: 'event-legacy', label: '旧事件', start: 0, end: 2 }] }} existingRef={null} blueprintRef={null} visualNodes={[]} sfxNodes={[]} onDraftChange={() => {}} onSave={() => {}} onClose={() => {}} />);

    expect(screen.getByText('手动添加')).toBeVisible();
    expect(screen.queryByText('AI 生成')).not.toBeInTheDocument();
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

  it('restores and persists the topic owned by the current script branch', async () => {
    const onTopicChange = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '已有台词', selections: [] }} initialTopic="冰美式" existingRef={null} blueprintRef={{ artifactId: 'blueprint-1', revision: 1 }} visualNodes={[]} sfxNodes={[]} onTopicChange={onTopicChange} onDraftChange={() => {}} onSave={() => {}} onClose={() => {}} />);

    expect(screen.getByLabelText('新主题')).toHaveValue('冰美式');
    fireEvent.change(screen.getByLabelText('新主题'), { target: { value: '无糖气泡水' } });
    expect(onTopicChange).toHaveBeenCalledWith('无糖气泡水');
    await screen.findByText('用早晨疲惫引出咖啡');
  });

  it('uses the child branch settings to create a blueprint before generation and save', async () => {
    const branchRef = { artifactId: 'branch-blueprint', revision: 1 };
    const onEnsureBlueprint = vi.fn(async () => branchRef);
    const onSave = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '旧台词', selections: [] }} existingRef={null} blueprintRef={null} blueprintSettings={<div>当前脚本的改编规则</div>} onEnsureBlueprint={onEnsureBlueprint} visualNodes={[]} sfxNodes={[]} onDraftChange={() => {}} onSave={onSave} onClose={() => {}} />);

    expect(screen.getByText('当前脚本的改编规则')).toBeVisible();
    fireEvent.change(screen.getByLabelText('新主题'), { target: { value: '新品饮料' } });
    fireEvent.click(screen.getByRole('button', { name: '按蓝图生成' }));
    await waitFor(() => expect(generateRemixScriptDraft).toHaveBeenCalledWith('demo', branchRef, '新品饮料'));

    fireEvent.click(screen.getByRole('button', { name: '保存脚本版本' }));
    await waitFor(() => expect(saveRemixScriptArtifact).toHaveBeenCalledWith('demo', expect.any(Object), null, branchRef));
    expect(onEnsureBlueprint).toHaveBeenCalledTimes(2);
    expect(onSave).toHaveBeenCalledWith({ artifactId: 'script-1', revision: 1 }, branchRef);
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

  it('creates generated or uploaded visual nodes from the semantic selection affordance', () => {
    const onCreateVisual = vi.fn((mode: 'generate' | 'upload') => ({ id: `${mode}-1`, label: mode === 'generate' ? '生成画面' : '上传画面' }));
    const onDraftChange = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '喝咖啡', selections: [{ id: 'product', label: '产品登场', start: 1, end: 3 }] }} existingRef={null} blueprintRef={null} visualNodes={[]} sfxNodes={[]} onCreateVisual={onCreateVisual} onDraftChange={onDraftChange} onSave={() => {}} onClose={() => {}} />);

    fireEvent.click(screen.getByRole('button', { name: '生成 产品登场 素材' }));
    expect(onCreateVisual).toHaveBeenCalledWith('generate', 'product');
    expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({ selections: [expect.objectContaining({ visualNodeId: 'generate-1' })] }));
    expect(screen.getByText('补充画面节点已创建，但素材尚未完成。请先在画布生成或上传素材，再点击右下角「保存脚本版本」。')).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: '上传 产品登场 素材' }));
    expect(onCreateVisual).toHaveBeenCalledWith('upload', 'product');
    expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({ selections: [expect.objectContaining({ visualNodeId: 'upload-1' })] }));
  });

  it('shows the Recipe requirement beside the exact B-roll event', () => {
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '喝咖啡', selections: [{ id: 'product', label: '产品登场', start: 1, end: 3 }] }} existingRef={null} blueprintRef={null} visualNodes={[]} sfxNodes={[]} brollRequirements={[{ event_id: 'product', required: true, purpose: '必须看见产品', suggested_query: '咖啡杯特写' }]} onDraftChange={() => {}} onSave={() => {}} onClose={() => {}} />);

    expect(screen.getByText('必需补充画面（B-roll）· 必须看见产品 · 咖啡杯特写')).toBeInTheDocument();
  });

  it('attaches an existing canvas visual and stores its immutable media revision', async () => {
    const onAttachVisual = vi.fn(async () => 'f'.repeat(64));
    const onDraftChange = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '喝咖啡', selections: [{ id: 'product', label: '产品登场', start: 1, end: 3 }] }} existingRef={null} blueprintRef={null} visualNodes={[{ id: 'image-1', label: '产品图' }]} sfxNodes={[]} onAttachVisual={onAttachVisual} onDraftChange={onDraftChange} onSave={() => {}} onClose={() => {}} />);

    fireEvent.change(screen.getByLabelText('产品登场画面'), { target: { value: 'image-1' } });

    await waitFor(() => expect(onAttachVisual).toHaveBeenCalledWith('image-1'));
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({ selections: [expect.objectContaining({ visualNodeId: 'image-1', visualMediaRevision: 'f'.repeat(64) })] })));
  });

  it('rechecks an already-bound visual so a replaced node cannot reuse its old revision', async () => {
    const onAttachVisual = vi.fn(async () => 'b'.repeat(64));
    const onDraftChange = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '喝咖啡', selections: [{ id: 'product', label: '产品登场', start: 1, end: 3 }] }} existingRef={null} blueprintRef={null} visualNodes={[{ id: 'image-1', label: '产品图', mediaRevision: 'a'.repeat(64) }]} sfxNodes={[]} onAttachVisual={onAttachVisual} onDraftChange={onDraftChange} onSave={() => {}} onClose={() => {}} />);

    fireEvent.change(screen.getByLabelText('产品登场画面'), { target: { value: 'image-1' } });

    await waitFor(() => expect(onAttachVisual).toHaveBeenCalledWith('image-1'));
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({ selections: [expect.objectContaining({ visualMediaRevision: 'b'.repeat(64) })] })));
  });

  it('merges a completed async attachment into the latest draft without reverting newer edits', async () => {
    let release!: (revision: string) => void;
    const onAttachVisual = vi.fn(() => new Promise<string>((resolve) => { release = resolve; }));
    const onDraftChange = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '喝咖啡', selections: [{ id: 'product', label: '产品登场', start: 1, end: 3 }] }} existingRef={null} blueprintRef={null} visualNodes={[{ id: 'image-1', label: '产品图' }]} sfxNodes={[]} onAttachVisual={onAttachVisual} onDraftChange={onDraftChange} onSave={() => {}} onClose={() => {}} />);
    fireEvent.change(screen.getByLabelText('产品登场画面'), { target: { value: 'image-1' } });
    await waitFor(() => expect(onAttachVisual).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText('事件 1 名称'), { target: { value: '新品登场' } });
    release('f'.repeat(64));

    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({
      selections: [expect.objectContaining({ label: '新品登场', visualNodeId: 'image-1', visualMediaRevision: 'f'.repeat(64) })],
    })));
  });

  it('commits only the latest visual when attachment successes finish out of order', async () => {
    let releaseA!: (value: { mediaRevision: string; commit: () => void }) => void;
    let releaseB!: (value: { mediaRevision: string; commit: () => void }) => void;
    const commitA = vi.fn();
    const commitB = vi.fn();
    const onAttachVisual = vi.fn((nodeId: string) => new Promise<{ mediaRevision: string; commit: () => void }>((resolve) => {
      if (nodeId === 'image-a') releaseA = resolve;
      else releaseB = resolve;
    }));
    const onDraftChange = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '喝咖啡', selections: [{ id: 'product', label: '产品登场', start: 1, end: 3 }] }} existingRef={null} blueprintRef={null} visualNodes={[{ id: 'image-a', label: 'A' }, { id: 'image-b', label: 'B' }]} sfxNodes={[]} onAttachVisual={onAttachVisual} onDraftChange={onDraftChange} onSave={() => {}} onClose={() => {}} />);
    const select = screen.getByLabelText('产品登场画面');
    fireEvent.change(select, { target: { value: 'image-a' } });
    fireEvent.change(select, { target: { value: 'image-b' } });
    releaseB({ mediaRevision: 'b'.repeat(64), commit: commitB });
    await waitFor(() => expect(commitB).toHaveBeenCalledOnce());
    releaseA({ mediaRevision: 'a'.repeat(64), commit: commitA });

    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({ selections: [expect.objectContaining({ visualNodeId: 'image-b', visualMediaRevision: 'b'.repeat(64) })] })));
    expect(commitA).not.toHaveBeenCalled();
  });

  it('ignores a stale attachment rejection after a newer visual succeeds', async () => {
    let rejectA!: (cause: Error) => void;
    const commitB = vi.fn();
    const onAttachVisual = vi.fn((nodeId: string) => nodeId === 'image-a'
      ? new Promise<{ mediaRevision: string; commit: () => void }>((_resolve, reject) => { rejectA = reject; })
      : Promise.resolve({ mediaRevision: 'b'.repeat(64), commit: commitB }));
    const onDraftChange = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '喝咖啡', selections: [{ id: 'product', label: '产品登场', start: 1, end: 3 }] }} existingRef={null} blueprintRef={null} visualNodes={[{ id: 'image-a', label: 'A' }, { id: 'image-b', label: 'B' }]} sfxNodes={[]} onAttachVisual={onAttachVisual} onDraftChange={onDraftChange} onSave={() => {}} onClose={() => {}} />);
    const select = screen.getByLabelText('产品登场画面');
    fireEvent.change(select, { target: { value: 'image-a' } });
    fireEvent.change(select, { target: { value: 'image-b' } });
    await waitFor(() => expect(commitB).toHaveBeenCalledOnce());
    rejectA(new Error('旧请求失败'));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({ selections: [expect.objectContaining({ visualNodeId: 'image-b', visualMediaRevision: 'b'.repeat(64) })] }));
  });
});
