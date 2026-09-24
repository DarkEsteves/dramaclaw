import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useState } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
vi.mock('@/api/remix', () => ({
  fetchRemixArtifact: vi.fn(async () => ({ schema: 'reference_blueprint.v1', payload: { narrative_role: '用早晨疲惫引出咖啡', events: [], preserve_replace_rules: [], observations: [], inferences: [], user_choices: [] } })),
  generateRemixScriptDraft: vi.fn(async () => ({ dialogue: '新主题来了，喝一口咖啡。', selections: [{ id: 'generated-1', label: '产品登场', start: 9, end: 11 }] })),
}));
import { generateRemixScriptDraft } from '@/api/remix';
import { RemixScriptPanel } from './RemixScriptPanel';

describe('RemixScriptPanel', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows A-roll before B-roll with local status and keeps B-roll planning available', () => {
    const onPlanBroll = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '男：你好', selections: [
      { id: 'event-1', label: '开场', start: 2, end: 4 },
    ], segments: [{ id: 'segment-1', start: 2, end: 4, speaker: '男', source: 'speaking_video' }] }}
      existingRef={{ artifactId: 'script-1', revision: 1 }} blueprintRef={null} visualNodes={[]} sfxNodes={[]}
      onPlanBroll={onPlanBroll} onDraftChange={() => {}} onSave={() => {}} onClose={() => {}} />);
    const aroll = screen.getByRole('region', { name: 'A-roll 主画面与声音' });
    const broll = screen.getByRole('region', { name: 'B-roll 补充镜头与音效' });
    expect(aroll.compareDocumentPosition(broll) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(aroll).getByRole('heading', { name: '主画面与声音' }).compareDocumentPosition(within(aroll).getByText('A-roll')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(broll).getByRole('heading', { name: '补充镜头与音效' }).compareDocumentPosition(within(broll).getByText('B-roll')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(aroll).toHaveTextContent('已选 0/1 个片段');
    expect(broll).toHaveTextContent('待规划 · 1 个台词事件');
    fireEvent.click(screen.getByRole('button', { name: '规划 B-roll' }));
    expect(onPlanBroll).toHaveBeenCalledOnce();
  });

  it('applies arbitrary references from one segment to explicitly selected segments', () => {
    const onDraftChange = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '甲：你好\n乙：好\n丙：再见', selections: [], segments: [
      { id: 'one', start: 2, end: 4, speaker: '甲', source: 'speaking_video', referenceBindings: [{ nodeId: 'shot', purpose: '参考镜头运动' }] },
      { id: 'two', start: 7, end: 8, speaker: '乙', source: 'speaking_video', referenceBindings: [{ nodeId: 'voice', purpose: '参考音色' }], takeNodeId: 'old-take', takeMediaRevision: 'a'.repeat(64) },
      { id: 'three', start: 11, end: 13, speaker: '丙', source: 'speaking_video' },
    ] }} existingRef={null} blueprintRef={null} visualNodes={[]} referenceNodes={[{ id: 'shot', label: '原片镜头', kind: 'video' }, { id: 'voice', label: '声音样本', kind: 'audio' }]}
      sfxNodes={[]} onDraftChange={onDraftChange} onSave={() => {}} onClose={() => {}} />);
    fireEvent.click(screen.getAllByText(/参考素材（可选）/)[0]);
    fireEvent.click(screen.getAllByRole('button', { name: '将参考 1 应用到其他段落' })[0]);
    fireEvent.click(screen.getByRole('checkbox', { name: '应用到乙' }));
    fireEvent.click(screen.getByRole('button', { name: '应用到选中段落' }));
    expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({ segments: [
      expect.objectContaining({ id: 'one', referenceBindings: [{ nodeId: 'shot', purpose: '参考镜头运动' }] }),
      expect.objectContaining({ id: 'two', referenceBindings: [
        { nodeId: 'voice', purpose: '参考音色' }, { nodeId: 'shot', purpose: '参考镜头运动' },
      ], takeNodeId: undefined, takeMediaRevision: undefined }),
      expect.objectContaining({ id: 'three' }),
    ] }), 'media-binding');
  });

  it('lets one speaking video cover two neighboring speaker turns', () => {
    const onDraftChange = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '男：你好。\n女：再见。', selections: [], segments: [
      { id: 'one', start: 2, end: 5, speaker: '男', source: 'speaking_video' },
      { id: 'two', start: 8, end: 11, speaker: '女', source: 'speaking_video' },
    ] }} existingRef={null} blueprintRef={null} visualNodes={[]} sfxNodes={[]}
      onDraftChange={onDraftChange} onSave={() => {}} onClose={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: '与下一段共用一个视频片段' }));
    expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({ segments: [
      expect.objectContaining({ speaker: '多人', speechRanges: [{ start: 2, end: 5 }, { start: 8, end: 11 }] }),
    ] }), 'media-binding');
    expect(screen.getByText('你好。 再见。')).toBeInTheDocument();
  });

  it('lets the editor attach a generic video reference and create a speaking take', () => {
    const onDraftChange = vi.fn();
    const onCreateTake = vi.fn(() => 'take-1');
    const onConnectReference = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '男：你好', selections: [], segments: [
      { id: 'segment-1', start: 2, end: 4, speaker: '男', source: 'speaking_video' },
    ] }} existingRef={null} blueprintRef={null} visualNodes={[]} referenceNodes={[
      { id: 'shot', label: '原片镜头', kind: 'video' }, { id: 'voice', label: '声音样本', kind: 'audio' },
    ]} sfxNodes={[]} onConnectReference={onConnectReference}
      onCreateTake={onCreateTake} onDraftChange={onDraftChange} onSave={() => {}} onClose={() => {}} />);
    fireEvent.click(screen.getByText(/参考素材（可选）/));
    fireEvent.click(screen.getByRole('button', { name: '添加参考素材' }));
    fireEvent.change(screen.getByLabelText('参考 1 用途'), { target: { value: '参考镜头运动' } });
    fireEvent.click(screen.getByRole('button', { name: '添加参考素材' }));
    fireEvent.change(screen.getByLabelText('参考 2 用途'), { target: { value: '参考说话节奏' } });
    fireEvent.click(screen.getByRole('button', { name: '创建口播视频节点' }));
    expect(onConnectReference.mock.calls.map(([id]) => id)).toContain('voice');
    expect(onCreateTake).toHaveBeenCalledWith(expect.objectContaining({ referenceBindings: [
      { nodeId: 'shot', purpose: '参考镜头运动' }, { nodeId: 'voice', purpose: '参考说话节奏' },
    ] }));
    expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({ segments: [expect.objectContaining({ takeNodeId: 'take-1' })] }), 'media-binding');
  });

  it('creates a neutral reference node for a segment without requiring a category', () => {
    const segment = { id: 'segment-1', start: 2, end: 4, speaker: '男', source: 'speaking_video' as const };
    const onCreateReference = vi.fn(() => 'reference-node');
    const onDraftChange = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '男：你好', selections: [], segments: [segment] }}
      existingRef={null} blueprintRef={null} visualNodes={[]} sfxNodes={[]}
      onCreateReference={onCreateReference} onDraftChange={onDraftChange} onSave={() => {}} onClose={() => {}} />);
    fireEvent.click(screen.getByText(/参考素材（可选）/));
    fireEvent.click(screen.getByRole('button', { name: '创建图片节点' }));
    expect(onCreateReference).toHaveBeenCalledExactlyOnceWith('generate', segment, 0);
    expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({ segments: [
      expect.objectContaining({ referenceBindings: [{ nodeId: 'reference-node', purpose: '参考画面内容' }] }),
    ] }), 'media-binding');
  });

  it('displays legacy references as editable generic bindings', () => {
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '男：你好', selections: [], segments: [
      { id: 'segment-1', start: 2, end: 4, speaker: '男', source: 'speaking_video', personNodeId: 'portrait' },
    ] }} existingRef={null} blueprintRef={null} visualNodes={[]} referenceNodes={[
      { id: 'portrait', label: '人物照片', kind: 'image' },
    ]} sfxNodes={[]} onDraftChange={() => {}} onSave={() => {}} onClose={() => {}} />);
    fireEvent.click(screen.getByText(/参考素材（可选）/));
    expect(screen.getByLabelText('参考 1 素材')).toHaveValue('portrait');
    expect(screen.getByLabelText('参考 1 用途')).toHaveValue('保持人物形象');
  });

  it('explains the main picture decision without repeating the script or showing character offsets', () => {
    const dialogue = '你每天喝什么？我喝咖啡。';
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue, selections: [], segments: [
      { id: 'segment-1', start: 0, end: Array.from(dialogue).length, speaker: '口播', source: 'speaking_video' },
    ] }} existingRef={null} blueprintRef={null} visualNodes={[]} sfxNodes={[]} onDraftChange={() => {}} onSave={() => {}} onClose={() => {}} />);

    expect(screen.getByRole('heading', { name: '主画面与声音' })).toBeVisible();
    expect(screen.getByText('整段台词')).toBeVisible();
    expect(screen.getByText(`${Array.from(dialogue).length} 字`)).toBeVisible();
    expect(screen.queryByText(`0–${Array.from(dialogue).length} 字`)).not.toBeInTheDocument();
    expect(screen.getByText(/先决定台词由谁说、成片使用哪段基础画面和声音.*素材在对应的画布节点制作/)).toBeVisible();
    expect(screen.getByText(/在基础画面的台词位置.*插入补充镜头或音效/)).toBeVisible();
    const preview = screen.getByText('查看本段台词').closest('details');
    expect(preview).not.toHaveAttribute('open');
    fireEvent.click(screen.getByText('查看本段台词'));
    expect(preview).toHaveAttribute('open');
  });

  it('keeps optional references out of the primary decision', () => {
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '你好', selections: [], segments: [
      { id: 'segment-1', start: 0, end: 2, speaker: '口播', source: 'speaking_video' },
    ] }} existingRef={null} blueprintRef={null} visualNodes={[]} referenceNodes={[{ id: 'person', label: '新人物', kind: 'image' }]} sfxNodes={[]} onDraftChange={() => {}} onSave={() => {}} onClose={() => {}} />);
    const references = screen.getByText(/参考素材（可选）/).closest('details');
    expect(references).not.toHaveAttribute('open');
    expect(screen.getByText(/先决定台词由谁说/)).toBeVisible();
    fireEvent.click(screen.getByText(/参考素材（可选）/));
    expect(references).toHaveAttribute('open');
    expect(screen.getByText('未使用参考素材')).toBeVisible();
    expect(screen.getByText(/此按钮会在画布创建节点，生成视频请到该节点操作/)).toBeVisible();
  });

  it('does not treat an unlabelled script as a confirmed voiceover and lets the user choose a speaking video', () => {
    const onDraftChange = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '你每天喝什么？\n我喝咖啡。', selections: [], segments: [] }} existingRef={null} blueprintRef={null} visualNodes={[]} sfxNodes={[]} onDraftChange={onDraftChange} onSave={() => {}} onClose={() => {}} />);

    expect(screen.queryByText('01 · 旁白')).not.toBeInTheDocument();
    expect(screen.getByText(/生成台词不会替你决定口播视频还是独立旁白/)).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '制作整段口播视频' }));
    expect(onDraftChange).toHaveBeenCalledWith(expect.objectContaining({ segments: [expect.objectContaining({ source: 'speaking_video', speaker: '口播' })] }));
  });

  it('lets an existing single voiceover segment switch to a speaking video', () => {
    const onDraftChange = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '你每天喝什么？', selections: [], segments: [
      { id: 'segment-1', start: 0, end: 7, speaker: '旁白', source: 'voiceover' },
    ] }} existingRef={null} blueprintRef={null} visualNodes={[]} sfxNodes={[]} onDraftChange={onDraftChange} onSave={() => {}} onClose={() => {}} />);

    fireEvent.click(screen.getByRole('button', { name: '改成整段口播视频' }));
    expect(onDraftChange).toHaveBeenCalledWith(expect.objectContaining({ segments: [expect.objectContaining({ source: 'speaking_video', speaker: '口播' })] }), 'media-binding');
  });

  it('offers optional source references for an audio take while keeping voice choice in its node', () => {
    const onDraftChange = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '独立旁白', selections: [], segments: [
      { id: 'voice', start: 0, end: 4, speaker: '旁白', source: 'voiceover' },
    ] }} existingRef={null} blueprintRef={null} visualNodes={[]} sfxNodes={[]}
      referenceNodes={[{ id: 'sample', label: '音色样本', kind: 'audio' }]}
      onDraftChange={onDraftChange} onSave={() => {}} onClose={() => {}} />);
    fireEvent.click(screen.getByText(/参考素材（可选）/));
    expect(screen.getByText(/音色仍需在音频节点中选择/)).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '添加参考素材' }));
    expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({ segments: [expect.objectContaining({
      referenceBindings: [{ nodeId: 'sample', purpose: '参考声音表现' }],
    })] }), 'media-binding');
  });

  it('offers main visual creation inside the editor for a voiceover segment', () => {
    const onCreatePrimaryVisuals = vi.fn(() => true);
    const onClose = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '旁白台词。', selections: [
      { id: 'event-1', label: '开场', start: 0, end: 5 },
    ], segments: [
      { id: 'segment-1', start: 0, end: 5, speaker: '旁白', source: 'voiceover' },
    ] }} existingRef={null} blueprintRef={null} visualNodes={[]} sfxNodes={[]}
      missingPrimaryVisualCount={1} onCreatePrimaryVisuals={onCreatePrimaryVisuals}
      onDraftChange={() => {}} onSave={() => {}} onClose={onClose} />);

    fireEvent.click(screen.getByRole('button', { name: '创建缺少的主画面视频节点' }));
    expect(onCreatePrimaryVisuals).toHaveBeenCalledOnce();
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('plans B-roll beside script events only after the current draft is saved', () => {
    const draft = { dialogue: '新品咖啡登场', selections: [{ id: 'product', label: '产品登场', start: 0, end: 6 }] };
    const onPlanBroll = vi.fn();
    const props = { project: 'demo', initialDraft: draft, blueprintRef: null, visualNodes: [], sfxNodes: [], onDraftChange: () => {}, onSave: () => {}, onClose: () => {}, onPlanBroll };
    const { rerender } = render(<RemixScriptPanel {...props} existingRef={null} />);

    const action = screen.getByRole('button', { name: '规划 B-roll' });
    expect(screen.getByRole('heading', { name: '台词事件（1）' }).parentElement?.contains(action)).toBe(true);
    expect(action).toBeDisabled();
    expect(screen.getByText('保存当前台词后即可分析补充画面。')).toBeVisible();

    rerender(<RemixScriptPanel {...props} existingRef={{ artifactId: 'script-1', revision: 2 }} />);
    expect(action).toBeEnabled();
    fireEvent.click(action);
    expect(onPlanBroll).toHaveBeenCalledOnce();
  });

  it('does not plan the previous saved script while a replacement draft is generating', () => {
    const onPlanBroll = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '旧台词', selections: [
      { id: 'old-event', label: '旧事件', start: 0, end: 3 },
    ] }} existingRef={{ artifactId: 'old-script', revision: 1 }} blueprintRef={null}
      visualNodes={[]} sfxNodes={[]} generationRunning onPlanBroll={onPlanBroll}
      onDraftChange={() => {}} onSave={() => {}} onClose={() => {}} />);

    fireEvent.click(screen.getByRole('button', { name: '规划 B-roll' }));
    expect(screen.getByRole('button', { name: '规划 B-roll' })).toBeDisabled();
    expect(onPlanBroll).not.toHaveBeenCalled();
  });

  it('shows B-roll planning progress, failure and a reviewable result in the event section', () => {
    const draft = { dialogue: '新品咖啡登场', selections: [{ id: 'product', label: '产品登场', start: 0, end: 6 }] };
    const props = { project: 'demo', initialDraft: draft, existingRef: { artifactId: 'script-1', revision: 2 }, blueprintRef: null, visualNodes: [], sfxNodes: [], onDraftChange: () => {}, onSave: () => {}, onClose: () => {}, onPlanBroll: vi.fn() };
    const { rerender } = render(<RemixScriptPanel {...props} brollPlanStatus="running" />);
    expect(screen.getByRole('button', { name: '规划 B-roll 中…' })).toBeDisabled();

    rerender(<RemixScriptPanel {...props} brollPlanStatus="failed" brollPlanError="模型服务不可用" />);
    expect(screen.getByRole('alert')).toHaveTextContent('模型服务不可用');
    expect(screen.getByRole('button', { name: '重试规划 B-roll' })).toBeEnabled();

    rerender(<RemixScriptPanel {...props} brollPlanStatus="completed" brollPlanCurrent brollRequirements={[
      { event_id: 'product', required: true, purpose: '展示产品', suggested_query: '咖啡特写' },
    ]} />);
    expect(screen.getByText('已分析 1 个事件，建议 1 个补充画面。')).toBeVisible();
    expect(screen.getByRole('button', { name: '重新规划 B-roll' })).toBeEnabled();
    expect(screen.getByText(/AI 建议：需要补充画面.*咖啡特写/)).toBeVisible();
  });

  it('lets the user override an AI B-roll recommendation while retaining the suggestion', () => {
    const onBrollDecisionChange = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '新品咖啡登场', selections: [
      { id: 'product', label: '产品登场', start: 0, end: 6 },
    ] }} existingRef={{ artifactId: 'script-1', revision: 2 }} blueprintRef={null} visualNodes={[]} sfxNodes={[]}
      brollPlanStatus="completed" brollPlanCurrent brollRequirements={[
        { event_id: 'product', required: true, purpose: '展示产品', suggested_query: '咖啡特写' },
      ]} brollDecisions={{ product: false }} onBrollDecisionChange={onBrollDecisionChange}
      onDraftChange={() => {}} onSave={() => {}} onClose={() => {}} />);

    const decision = screen.getByRole('checkbox', { name: '产品登场需要 B-roll' });
    expect(decision).not.toBeChecked();
    expect(decision.compareDocumentPosition(screen.getByLabelText('产品登场画面')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByText(/AI 建议：需要补充画面.*咖啡特写/)).toBeVisible();
    fireEvent.click(decision);
    expect(onBrollDecisionChange).toHaveBeenCalledWith('product', true);
  });

  it('makes script generation the clear primary action after entering a topic', () => {
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '', selections: [] }} existingRef={null} blueprintRef={{ artifactId: 'blueprint-1', revision: 1 }} visualNodes={[]} sfxNodes={[]} onDraftChange={() => {}} onSave={() => {}} onClose={() => {}} />);

    const generateButton = screen.getByRole('button', { name: '生成改编台词' });
    expect(screen.getByText('填写新主题后，点击右侧按钮生成台词')).toBeVisible();
    expect(generateButton).toBeDisabled();
    expect(generateButton).toHaveClass('bg-primary');

    fireEvent.change(screen.getByLabelText('新主题'), { target: { value: '新品咖啡' } });
    expect(generateButton).toBeEnabled();
  });

  it('marks a selected phrase and automatically saves its media binding', async () => {
    const onAutoSave = vi.fn(async () => {});
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '喝下咖啡', selections: [] }} existingRef={null} blueprintRef={{ artifactId: 'blueprint-1', revision: 1 }} visualNodes={[{ id: 'video-1', label: '咖啡画面', mediaRevision: 'a'.repeat(64) }]} sfxNodes={[]} onAutoSave={onAutoSave} onDraftChange={() => {}} onSave={() => {}} onClose={() => {}} />);
    const dialogue = screen.getByLabelText('改编台词') as HTMLTextAreaElement;
    expect(await screen.findByText('用早晨疲惫引出咖啡')).toBeTruthy();
    const broll = screen.getByRole('region', { name: 'B-roll 补充镜头与音效' });
    expect(dialogue).not.toHaveAttribute('readonly');
    expect(within(broll).getByRole('button', { name: '从台词添加事件' })).toBeVisible();
    expect(screen.queryByRole('group', { name: '改编台词与事件标记' })).not.toBeInTheDocument();
    fireEvent.click(within(broll).getByRole('button', { name: '从台词添加事件' }));
    const picker = within(broll).getByRole('textbox', { name: '选择台词片段' }) as HTMLTextAreaElement;
    expect(picker).toHaveAttribute('readonly');
    expect(picker).toHaveValue(dialogue.value);
    expect(within(broll).getByRole('button', { name: '添加选中台词' })).toBeDisabled();
    picker.focus();
    picker.setSelectionRange(2, 4);
    fireEvent.select(picker);
    expect(within(broll).getByText('已选台词：咖啡')).toBeVisible();
    fireEvent.click(within(broll).getByRole('button', { name: '添加选中台词' }));
    expect(within(broll).queryByRole('textbox', { name: '选择台词片段' })).not.toBeInTheDocument();
    expect(screen.getByText('补充画面（B-roll）')).toBeVisible();
    expect(screen.getByText('手动添加')).toBeVisible();
    expect((screen.getByLabelText('事件 1 名称') as HTMLInputElement).closest('[tabindex="-1"]')).toHaveFocus();
    fireEvent.change(screen.getByLabelText('产品登场画面'), { target: { value: 'video-1' } });
    await waitFor(() => expect(onAutoSave).toHaveBeenLastCalledWith(expect.objectContaining({ selections: [expect.objectContaining({ start: 2, end: 4, visualNodeId: 'video-1', visualMediaRevision: 'a'.repeat(64) })] })));
    expect(screen.queryByRole('button', { name: '保存脚本版本' })).not.toBeInTheDocument();
  });

  it('labels AI events and inserts a manual event in script order', async () => {
    const onDraftChange = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '甲乙丙丁戊己', selections: [{ id: 'generated-1', label: '后段事件', start: 4, end: 6 }] }} existingRef={null} blueprintRef={{ artifactId: 'blueprint-1', revision: 1 }} visualNodes={[]} sfxNodes={[]} onDraftChange={onDraftChange} onSave={() => {}} onClose={() => {}} />);

    expect(screen.getByText('台词事件（1）')).toBeVisible();
    expect(screen.getByText('AI 已根据台词自动拆分；手动事件可从这里选择台词添加。')).toBeVisible();
    expect(screen.getByText('AI 生成')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '从台词添加事件' }));
    const picker = screen.getByRole('textbox', { name: '选择台词片段' }) as HTMLTextAreaElement;
    picker.focus();
    picker.setSelectionRange(1, 3);
    fireEvent.select(picker);
    fireEvent.click(screen.getByRole('button', { name: '添加选中台词' }));

    expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({ selections: [
      expect.objectContaining({ start: 1, end: 3, source: 'manual' }),
      expect.objectContaining({ id: 'generated-1', start: 4, end: 6 }),
    ] }));
    expect(screen.getByText('台词事件（2）')).toBeVisible();
    expect(screen.getByText('手动添加')).toBeVisible();
    await screen.findByText('用早晨疲惫引出咖啡');
  });

  it('uses the current dialogue in the B-roll picker and clears a stale selection after edits', () => {
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '旧台词', selections: [] }} existingRef={null} blueprintRef={null} visualNodes={[]} sfxNodes={[]} onDraftChange={() => {}} onSave={() => {}} onClose={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: '从台词添加事件' }));
    const picker = screen.getByRole('textbox', { name: '选择台词片段' }) as HTMLTextAreaElement;
    picker.setSelectionRange(0, 2);
    fireEvent.select(picker);
    expect(screen.getByRole('button', { name: '添加选中台词' })).toBeEnabled();

    fireEvent.change(screen.getByLabelText('改编台词'), { target: { value: '新台词' } });
    expect(picker).toHaveValue('新台词');
    expect(screen.getByRole('button', { name: '添加选中台词' })).toBeDisabled();
    expect(screen.getByText('尚未选中台词')).toBeVisible();
  });

  it('shows the dialogue covered by each event and updates it with the character range', () => {
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '甲乙丙丁戊己', selections: [{ id: 'generated-1', label: '中段事件', start: 1, end: 3 }] }} existingRef={null} blueprintRef={null} visualNodes={[]} sfxNodes={[]} onDraftChange={() => {}} onSave={() => {}} onClose={() => {}} />);

    const eventName = screen.getByLabelText('事件 1 名称');
    const dialoguePreview = screen.getByText('乙丙');
    expect(dialoguePreview).toBeVisible();
    expect(eventName.compareDocumentPosition(dialoguePreview) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    fireEvent.change(screen.getByLabelText('中段事件 结束字'), { target: { value: '5' } });
    expect(screen.getByText('乙丙丁戊')).toBeVisible();
  });

  it('recognizes manual events saved before source labels were introduced', () => {
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '旧的手动事件', selections: [{ id: 'event-legacy', label: '旧事件', start: 0, end: 2 }] }} existingRef={null} blueprintRef={null} visualNodes={[]} sfxNodes={[]} onDraftChange={() => {}} onSave={() => {}} onClose={() => {}} />);

    expect(screen.getByText('手动添加')).toBeVisible();
    expect(screen.queryByText('AI 生成')).not.toBeInTheDocument();
  });

  it('generates an editable script from the current blueprint and automatically saves it', async () => {
    const onAutoSave = vi.fn(async () => {});
    const onDraftChange = vi.fn();
    const onGenerateDraft = vi.fn(async () => ({ dialogue: '后台生成的新台词。', selections: [{ id: 'generated-1', label: '产品登场', start: 5, end: 8 }] }));
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '旧台词', selections: [] }} existingRef={null} blueprintRef={{ artifactId: 'blueprint-1', revision: 1 }} visualNodes={[]} sfxNodes={[]} onGenerateDraft={onGenerateDraft} onAutoSave={onAutoSave} onDraftChange={onDraftChange} onSave={() => {}} onClose={() => {}} />);
    fireEvent.change(screen.getByLabelText('新主题'), { target: { value: '新咖啡' } });
    fireEvent.click(screen.getByRole('button', { name: '生成改编台词' }));

    await waitFor(() => expect(onGenerateDraft).toHaveBeenCalledWith({ artifactId: 'blueprint-1', revision: 1 }, '新咖啡'));
    expect(generateRemixScriptDraft).not.toHaveBeenCalled();
    expect((await screen.findByLabelText('改编台词') as HTMLTextAreaElement).value).toBe('后台生成的新台词。');
    expect(onDraftChange).toHaveBeenCalledWith(expect.objectContaining({ selections: [expect.objectContaining({ label: '产品登场' })] }));
    await waitFor(() => expect(onAutoSave).toHaveBeenCalledWith({
      dialogue: '后台生成的新台词。', selections: [{ id: 'generated-1', label: '产品登场', start: 5, end: 8 }],
    }));
  });

  it('refreshes the open editor when a background generation finishes', async () => {
    const common = { project: 'demo', existingRef: null, blueprintRef: { artifactId: 'blueprint-1', revision: 1 }, visualNodes: [], sfxNodes: [], onDraftChange: () => {}, onSave: () => {}, onClose: () => {} };
    const view = render(<RemixScriptPanel {...common} generationRunning initialDraft={{ dialogue: '', selections: [] }} />);

    view.rerender(<RemixScriptPanel {...common} generationRunning={false} initialDraft={{
      dialogue: '后台任务生成的新台词',
      selections: [{ id: 'generated-1', label: '产品登场', start: 0, end: 4 }],
    }} />);

    await waitFor(() => expect(screen.getByLabelText('改编台词')).toHaveValue('后台任务生成的新台词'));
    expect(screen.getByText('台词事件（1）')).toBeVisible();
  });

  it('restores and persists the topic owned by the current script branch', async () => {
    const onTopicChange = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '已有台词', selections: [] }} initialTopic="冰美式" existingRef={null} blueprintRef={{ artifactId: 'blueprint-1', revision: 1 }} visualNodes={[]} sfxNodes={[]} onTopicChange={onTopicChange} onDraftChange={() => {}} onSave={() => {}} onClose={() => {}} />);

    expect(screen.getByLabelText('新主题')).toHaveValue('冰美式');
    fireEvent.change(screen.getByLabelText('新主题'), { target: { value: '无糖气泡水' } });
    expect(onTopicChange).toHaveBeenCalledWith('无糖气泡水');
    await screen.findByText('用早晨疲惫引出咖啡');
  });

  it('flushes a pending text edit when the editor closes', () => {
    const onAutoSave = vi.fn(async () => {});
    const view = render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '旧台词', selections: [] }} existingRef={null} blueprintRef={null} visualNodes={[]} sfxNodes={[]} onAutoSave={onAutoSave} onDraftChange={() => {}} onSave={() => {}} onClose={() => {}} />);

    fireEvent.change(screen.getByLabelText('改编台词'), { target: { value: '关闭前的新台词' } });
    expect(onAutoSave).not.toHaveBeenCalled();
    view.unmount();

    expect(onAutoSave).toHaveBeenCalledWith({ dialogue: '关闭前的新台词', selections: [] });
  });

  it('uses the child branch settings to create a blueprint before generation', async () => {
    const branchRef = { artifactId: 'branch-blueprint', revision: 1 };
    const onEnsureBlueprint = vi.fn(async () => branchRef);
    const onAutoSave = vi.fn(async () => {});
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '旧台词', selections: [] }} existingRef={null} blueprintRef={null} blueprintSettings={<div>当前脚本的改编规则</div>} onEnsureBlueprint={onEnsureBlueprint} visualNodes={[]} sfxNodes={[]} onAutoSave={onAutoSave} onDraftChange={() => {}} onSave={() => {}} onClose={() => {}} />);

    expect(screen.getByText('当前脚本的改编规则')).toBeVisible();
    fireEvent.change(screen.getByLabelText('新主题'), { target: { value: '新品饮料' } });
    fireEvent.click(screen.getByRole('button', { name: '生成改编台词' }));
    await waitFor(() => expect(generateRemixScriptDraft).toHaveBeenCalledWith('demo', branchRef, '新品饮料'));
    await waitFor(() => expect(onAutoSave).toHaveBeenCalled());
    expect(onEnsureBlueprint).toHaveBeenCalledOnce();
  });

  it('keeps the current draft when generation fails', async () => {
    vi.mocked(generateRemixScriptDraft).mockRejectedValueOnce(new Error('模型输出无效'));
    const onDraftChange = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '原稿', selections: [] }} existingRef={null} blueprintRef={{ artifactId: 'blueprint-1', revision: 1 }} visualNodes={[]} sfxNodes={[]} onDraftChange={onDraftChange} onSave={() => {}} onClose={() => {}} />);
    fireEvent.change(screen.getByLabelText('新主题'), { target: { value: '茶' } });
    fireEvent.click(screen.getByRole('button', { name: '生成改编台词' }));

    expect(await screen.findByRole('alert')).toHaveProperty('textContent', '模型输出无效');
    expect((screen.getByLabelText('改编台词') as HTMLTextAreaElement).value).toBe('原稿');
    expect(onDraftChange).not.toHaveBeenCalled();
  });

  it('creates generated or uploaded visual nodes from the semantic selection affordance', async () => {
    const onCreateVisual = vi.fn((mode: 'generate' | 'generateVideo' | 'upload') => ({ id: `${mode}-1`, label: mode === 'generate' ? '生成画面' : '上传画面' }));
    const onDraftChange = vi.fn();
    const onAutoSave = vi.fn(async () => {});
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '喝咖啡', selections: [{ id: 'product', label: '产品登场', start: 1, end: 3 }] }} existingRef={null} blueprintRef={null} visualNodes={[]} sfxNodes={[]} brollRequirements={[{ event_id: 'product', required: true, purpose: '展示产品', suggested_query: '咖啡杯产品特写，柔和侧光' }]} onCreateVisual={onCreateVisual} onAutoSave={onAutoSave} onDraftChange={onDraftChange} onSave={() => {}} onClose={() => {}} />);

    fireEvent.click(screen.getByRole('button', { name: '生成 产品登场 图片素材' }));
    expect(onCreateVisual).toHaveBeenCalledWith('generate', 'product', '产品登场', '咖啡杯产品特写，柔和侧光');
    expect(onDraftChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ selections: [expect.objectContaining({ visualNodeId: 'generate-1' })] }),
      'media-binding',
    );
    await waitFor(() => expect(onAutoSave).toHaveBeenLastCalledWith(expect.objectContaining({ selections: [expect.objectContaining({ visualNodeId: 'generate-1' })] })));
    expect(screen.getByText('补充画面节点已创建，脚本已自动保存。稍后在画布完成素材后会自动关联。')).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: '生成 产品登场 视频素材' }));
    expect(onCreateVisual).toHaveBeenCalledWith('generateVideo', 'product', '产品登场', '咖啡杯产品特写，柔和侧光');
    expect(onDraftChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ selections: [expect.objectContaining({ visualNodeId: 'generateVideo-1' })] }),
      'media-binding',
    );

    fireEvent.click(screen.getByRole('button', { name: '上传 产品登场 素材' }));
    expect(onCreateVisual).toHaveBeenCalledWith('upload', 'product', '产品登场', undefined);
    expect(onDraftChange).toHaveBeenLastCalledWith(
      expect.objectContaining({ selections: [expect.objectContaining({ visualNodeId: 'upload-1' })] }),
      'media-binding',
    );
    await waitFor(() => expect(onAutoSave).toHaveBeenLastCalledWith(expect.objectContaining({ selections: [expect.objectContaining({ visualNodeId: 'upload-1' })] })));
  });

  it('keeps every B-roll requirement visible when one event creates a visual node', () => {
    function Harness() {
      const [requirements, setRequirements] = useState([
        { event_id: 'first', required: true, purpose: '展示开场环境', suggested_query: '咖啡店外景' },
        { event_id: 'last', required: true, purpose: '展示结尾产品', suggested_query: '产品特写' },
      ]);
      return <RemixScriptPanel
        project="demo"
        initialDraft={{ dialogue: '开场台词和结尾台词', selections: [
          { id: 'first', label: '开场', start: 0, end: 4 },
          { id: 'last', label: '结尾', start: 5, end: 9 },
        ] }}
        existingRef={null}
        blueprintRef={null}
        visualNodes={[]}
        sfxNodes={[]}
        brollRequirements={requirements}
        brollPlanCurrent
        onCreateVisual={() => ({ id: 'generated-last', label: '生成画面' })}
        onDraftChange={(_draft, changeKind) => {
          if (changeKind !== 'media-binding') setRequirements([]);
        }}
        onSave={() => {}}
        onClose={() => {}}
      />;
    }

    render(<Harness />);
    fireEvent.click(screen.getByRole('button', { name: '生成 结尾 图片素材' }));

    expect(screen.getByText(/展示开场环境/)).toBeVisible();
    expect(screen.getByText(/展示结尾产品/)).toBeVisible();
  });

  it('shows the Recipe requirement beside the exact B-roll event', () => {
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '喝咖啡', selections: [{ id: 'product', label: '产品登场', start: 1, end: 3 }] }} existingRef={null} blueprintRef={null} visualNodes={[]} sfxNodes={[]} brollPlanCurrent brollRequirements={[{ event_id: 'product', required: true, purpose: '必须看见产品', suggested_query: '咖啡杯特写' }]} onDraftChange={() => {}} onSave={() => {}} onClose={() => {}} />);

    expect(screen.getByText('AI 建议：需要补充画面 · 必须看见产品 · 建议提示词：咖啡杯特写')).toBeInTheDocument();
  });

  it('attaches an existing canvas visual and stores its immutable media revision', async () => {
    const onAttachVisual = vi.fn(async () => 'f'.repeat(64));
    const onDraftChange = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '喝咖啡', selections: [{ id: 'product', label: '产品登场', start: 1, end: 3 }] }} existingRef={null} blueprintRef={null} visualNodes={[{ id: 'image-1', label: '产品图' }]} sfxNodes={[]} onAttachVisual={onAttachVisual} onDraftChange={onDraftChange} onSave={() => {}} onClose={() => {}} />);

    fireEvent.change(screen.getByLabelText('产品登场画面'), { target: { value: 'image-1' } });

    await waitFor(() => expect(onAttachVisual).toHaveBeenCalledWith('image-1'));
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({ selections: [expect.objectContaining({ visualNodeId: 'image-1', visualMediaRevision: 'f'.repeat(64) })] }), 'media-binding'));
  });

  it('rechecks an already-bound visual so a replaced node cannot reuse its old revision', async () => {
    const onAttachVisual = vi.fn(async () => 'b'.repeat(64));
    const onDraftChange = vi.fn();
    render(<RemixScriptPanel project="demo" initialDraft={{ dialogue: '喝咖啡', selections: [{ id: 'product', label: '产品登场', start: 1, end: 3 }] }} existingRef={null} blueprintRef={null} visualNodes={[{ id: 'image-1', label: '产品图', mediaRevision: 'a'.repeat(64) }]} sfxNodes={[]} onAttachVisual={onAttachVisual} onDraftChange={onDraftChange} onSave={() => {}} onClose={() => {}} />);

    fireEvent.change(screen.getByLabelText('产品登场画面'), { target: { value: 'image-1' } });

    await waitFor(() => expect(onAttachVisual).toHaveBeenCalledWith('image-1'));
    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({ selections: [expect.objectContaining({ visualMediaRevision: 'b'.repeat(64) })] }), 'media-binding'));
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
    }), 'media-binding'));
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

    await waitFor(() => expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({ selections: [expect.objectContaining({ visualNodeId: 'image-b', visualMediaRevision: 'b'.repeat(64) })] }), 'media-binding'));
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
    expect(onDraftChange).toHaveBeenLastCalledWith(expect.objectContaining({ selections: [expect.objectContaining({ visualNodeId: 'image-b', visualMediaRevision: 'b'.repeat(64) })] }), 'media-binding');
  });
});
