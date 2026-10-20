import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { ProductionPlanV2 } from './productionPlanV2Model';
import { ProductionScriptContent } from './ProductionScriptContent';

const plan: ProductionPlanV2 = { schema_version: 2, goal: '访谈',
  output: { width: 1080, height: 1920, fps_num: 30, fps_den: 1 },
  beats: [{ id: 'beat', purpose: '提问与回答', duration_frames: 150, turns: [
    { id: 'question', speaker: '甲', text: '今天喝什么？' },
    { id: 'answer', speaker: '乙', text: '黑咖啡。' },
  ] }], resources: [], placements: [], provenance: {} };

describe('production script content', () => {
  it('keeps dialogue and material context in the same script section', () => {
    render(<ProductionScriptContent plan={plan} onAddRange={vi.fn()} />);
    expect(screen.getByText('提问与回答')).toBeVisible();
    expect(screen.getByText('今天喝什么？')).toBeVisible();
    expect(screen.getByText('黑咖啡。')).toBeVisible();
    expect(screen.getByText(/未添加画面/)).toBeVisible();
  });

  it('shows each picture and sound beside its dialogue with independent use status', () => {
    const onSelectPlacement = vi.fn();
    const expanded: ProductionPlanV2 = { ...plan,
      resources: [{ id: 'media', node_id: 'image-node', media_kind: 'image', media_path: 'a.png',
        media_hash: 'a'.repeat(64), duration_us: 0, has_audio: false }],
      placements: [
        { id: 'first', kind: 'visual', role: 'overlay', track_id: 'visual:overlay', resource_id: 'media',
          description: '咖啡特写', start: { kind: 'turn', ref_id: 'answer', edge: 'start' },
          end: { kind: 'turn', ref_id: 'answer', edge: 'end' } },
        { id: 'second', kind: 'visual', role: 'overlay', track_id: 'visual:overlay', resource_id: 'media',
          description: '再次出现的咖啡', start: { kind: 'turn', ref_id: 'answer', edge: 'start' },
          end: { kind: 'turn', ref_id: 'answer', edge: 'end' } },
        { id: 'sound', kind: 'audio', role: 'sfx', track_id: 'audio:sfx', description: '杯子声',
          start: { kind: 'turn', ref_id: 'answer', edge: 'start' },
          end: { kind: 'turn', ref_id: 'answer', edge: 'end' } },
      ] };
    render(<ProductionScriptContent plan={expanded} onAddRange={vi.fn()}
      onSelectPlacement={onSelectPlacement} previewUrls={{ first: '/preview.png', second: '/preview.png' }} />);
    expect(screen.getByText('咖啡特写')).toBeVisible();
    expect(screen.getByText('再次出现的咖啡')).toBeVisible();
    expect(screen.getByText('杯子声')).toBeVisible();
    expect(screen.getAllByAltText('咖啡特写')).toHaveLength(1);
    expect(screen.getByText('待添加素材')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: /再次出现的咖啡/ }));
    expect(onSelectPlacement).toHaveBeenCalledWith('second');
  });

  it('uses the actual DOM text range for an emoji-safe material action', () => {
    const onAddRange = vi.fn();
    render(<ProductionScriptContent plan={{ ...plan, beats: [{ ...plan.beats[0], turns: [
      { id: 'question', speaker: '甲', text: '好😀喝咖啡' },
    ] }] }} onAddRange={onAddRange} />);
    const text = screen.getByText('好😀喝咖啡').firstChild!;
    const range = document.createRange();
    range.setStart(text, 1); range.setEnd(text, 5);
    const selection = window.getSelection()!;
    selection.removeAllRanges(); selection.addRange(range);
    fireEvent.mouseUp(screen.getByLabelText('可选取台词范围'));
    const addPicture = screen.getByRole('button', { name: '选中台词添加画面' });
    fireEvent.mouseUp(addPicture);
    fireEvent.click(addPicture);
    expect(onAddRange).toHaveBeenCalledWith('visual', {
      start: { turn_id: 'question', char_offset: 1 },
      end: { turn_id: 'question', char_offset: 4 },
    }, '😀喝咖');
    selection.removeAllRanges();
  });

  it('keeps the originally selected words when a later plan render changes the text', () => {
    const onAddRange = vi.fn();
    const first = { ...plan, beats: [{ ...plan.beats[0], turns: [{ id: 'question', speaker: '甲', text: '今天喝咖啡' }] }] };
    const { rerender } = render(<ProductionScriptContent plan={first} onAddRange={onAddRange} />);
    const text = screen.getByText('今天喝咖啡').firstChild!;
    const range = document.createRange();
    range.setStart(text, 2); range.setEnd(text, 5);
    const selection = window.getSelection()!;
    selection.removeAllRanges(); selection.addRange(range);
    fireEvent.mouseUp(screen.getByLabelText('可选取台词范围'));
    rerender(<ProductionScriptContent plan={{ ...first, beats: [{ ...first.beats[0], turns: [
      { id: 'question', speaker: '甲', text: '今天喝红茶' },
    ] }] }} onAddRange={onAddRange} />);
    fireEvent.click(screen.getByRole('button', { name: '选中台词添加画面' }));
    expect(onAddRange).toHaveBeenCalledWith('visual', {
      start: { turn_id: 'question', char_offset: 2 }, end: { turn_id: 'question', char_offset: 5 },
    }, '喝咖啡');
    selection.removeAllRanges();
  });
});
