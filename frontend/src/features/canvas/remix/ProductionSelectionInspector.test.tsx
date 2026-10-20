import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { ProductionPlanV2 } from './productionPlanV2Model';
import { ProductionSelectionInspector } from './ProductionSelectionInspector';

const plan: ProductionPlanV2 = { schema_version: 2, goal: '咖啡短片',
  output: { width: 1080, height: 1920, fps_num: 30, fps_den: 1 },
  beats: [{ id: 'beat', purpose: '开场', duration_frames: 90, turns: [
    { id: 'line', speaker: '甲', text: '每天一杯黑咖啡' },
  ] }], resources: [], placements: [
    { id: 'first', kind: 'visual', role: 'overlay', track_id: 'visual:overlay', description: '咖啡',
      start: { kind: 'beat', ref_id: 'beat', edge: 'start' }, end: { kind: 'beat', ref_id: 'beat', edge: 'end' } },
    { id: 'second', kind: 'visual', role: 'overlay', track_id: 'visual:overlay', description: '咖啡',
      start: { kind: 'beat', ref_id: 'beat', edge: 'start' }, end: { kind: 'beat', ref_id: 'beat', edge: 'end' } },
  ], provenance: {} };

describe('production selection inspector', () => {
  it('changes the selected use without editing another use of the same content', () => {
    let current = structuredClone(plan);
    const change = vi.fn((edit: (next: ProductionPlanV2) => void) => {
      const next = structuredClone(current); edit(next); current = next;
    });
    render(<ProductionSelectionInspector plan={plan} placementId="first" change={change}
      onChooseMedia={vi.fn()} anchorOptions={[{ label: '台词开始', anchor: { kind: 'turn', ref_id: 'line', edge: 'start' } }]} />);
    fireEvent.change(screen.getByLabelText('选中素材开始范围'), {
      target: { value: JSON.stringify({ kind: 'turn', ref_id: 'line', edge: 'start' }) },
    });
    expect(current.placements[0].start).toEqual({ kind: 'turn', ref_id: 'line', edge: 'start' });
    expect(current.placements[1].start).toEqual(plan.placements[1].start);
  });

  it('shows the exact selected author text and offers to choose media', () => {
    const textPlan = structuredClone(plan);
    textPlan.placements[0].start = { kind: 'text', ref_id: 'line', char_offset: 0,
      text_hash: 'a'.repeat(64), affinity: 'after' };
    textPlan.placements[0].end = { kind: 'text', ref_id: 'line', char_offset: 4,
      text_hash: 'a'.repeat(64), affinity: 'before' };
    const onChooseMedia = vi.fn();
    render(<ProductionSelectionInspector plan={textPlan} placementId="first" change={vi.fn()}
      onChooseMedia={onChooseMedia} anchorOptions={[]} />);
    expect(screen.getByText('出现于台词：每天一杯')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '为这次使用选择素材' }));
    expect(onChooseMedia).toHaveBeenCalledWith('first');
  });
});
