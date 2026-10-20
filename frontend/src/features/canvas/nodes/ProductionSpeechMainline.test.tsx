import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { projectProductionTimeline } from '../compose/productionComposeModel';
import { ProductionSpeechMainline } from './ProductionSpeechMainline';
import type { ProductionPlanV2 } from '../remix/productionPlanV2Model';
const plan = (): ProductionPlanV2 => ({ schema_version: 2, goal: '', output: { width: 1080, height: 1920, fps_num: 30, fps_den: 1 }, beats: [{ id: 'b', purpose: '对白', duration_frames: 90, turns: [{ id: 't1', speaker: '甲', text: '第一句' }, { id: 't2', speaker: '乙', text: '第二句' }] }, { id: 'silent', purpose: '产品', duration_frames: 60, turns: [] }], placements: [], resources: [], provenance: {} });
describe('production speech mainline', () => {
 it('shows draft time and silent sections without inventing word evidence', () => {
  render(<ProductionSpeechMainline plan={plan()} change={() => {}} />);
  expect(screen.getByText('本段没有台词，无需 SRT 或语音校准。')).toBeTruthy();
  expect(screen.getAllByText('节目时间：待校准')).toHaveLength(2);
  expect(screen.getAllByRole('button', { name: '展开词时间' }).every((button) => (button as HTMLButtonElement).disabled)).toBe(true);
 });
 it('adds subtitle using the clicked second line context', () => {
  const draft = plan(); render(<ProductionSpeechMainline plan={draft} change={(edit) => edit(draft)} />);
  fireEvent.click(screen.getAllByRole('button', { name: '添加字幕' })[1]);
  expect(draft.placements[0]).toMatchObject({ turn_id: 't2', text: '第二句', start: { kind: 'turn', ref_id: 't2' } });
 });
});

function timedPlan() { const draft = plan(); draft.beats[0].turns = [draft.beats[0].turns[0]]; draft.resources = [{ id: 'r', node_id: 'video', media_kind: 'video', media_path: 'video.mp4', media_hash: 'hash', duration_us: 3000000, has_audio: true }]; draft.speech_takes = [{ id: 'take', beat_id: 'b', resource_id: 'r', turn_ids: ['t1'], origin_placement_id: 'main', source_in_us: 0, source_out_us: null, speed_num: 1, speed_den: 1, lead_in_frames: 0 }]; draft.speech_cues = [{ id: 'cue', turn_id: 't1', take_id: 'take', text: '第一句', source_start_us: 1000000, source_end_us: 2000000, timing_source: 'manual', timing_status: 'ready', word_ids: [], evidence_ref: { artifact_id: 'e', revision: 1 } }]; draft.placements = [{ id: 'main', kind: 'visual', role: 'primary', track_id: 'visual:primary', resource_id: 'r', speech_take_id: 'take', start: { kind: 'take', ref_id: 'take', edge: 'start' }, end: { kind: 'take', ref_id: 'take', edge: 'end' } }]; return draft; }
it('shows a migrated turn subtitle on its calibrated cue and opens that subtitle instead of duplicating it', () => {
 const draft = timedPlan(); const onChoosePlacement = vi.fn();
 draft.placements.push({ id: 'caption:t1', kind: 'subtitle', role: 'dialogue', track_id: 'subtitles', turn_id: 't1',
   start: { kind: 'turn', ref_id: 't1', edge: 'start' }, end: { kind: 'turn', ref_id: 't1', edge: 'end' }, font_size: 32 });
 render(<ProductionSpeechMainline plan={draft} change={(edit) => edit(draft)} onChoosePlacement={onChoosePlacement} />);
 expect(screen.getByText('字幕：第一句')).toBeInTheDocument();
 expect(screen.queryByText('尚未安排镜头与字幕')).not.toBeInTheDocument();
 fireEvent.click(screen.getByRole('button', { name: '编辑字幕' }));
 expect(onChoosePlacement).toHaveBeenCalledWith('caption:t1');
 expect(draft.placements.filter((item) => item.kind === 'subtitle')).toHaveLength(1);
 expect(draft.placements.find((item) => item.id === 'caption:t1')).toMatchObject({ font_size: 32, start: { kind: 'turn', ref_id: 't1' } });
});
it('keeps repeated subtitle clicks idempotent for a newly added cue caption', () => {
 const draft = timedPlan();
 render(<ProductionSpeechMainline plan={draft} change={(edit) => edit(draft)} />);
 const button = screen.getByRole('button', { name: '添加字幕' });
 fireEvent.click(button); fireEvent.click(button);
 expect(draft.placements.filter((item) => item.kind === 'subtitle')).toHaveLength(1);
 expect(draft.placements.find((item) => item.kind === 'subtitle')).toMatchObject({ start: { kind: 'cue', ref_id: 'cue' } });
});
it('shows a whole-turn caption across multiple cues without changing its authored range', () => {
 const draft = timedPlan(); const onChoosePlacement = vi.fn();
 draft.speech_cues!.push({ ...structuredClone(draft.speech_cues![0]), id: 'later-cue', text: '后半句', source_start_us: 2_100_000, source_end_us: 2_400_000 });
 draft.placements.push({ id: 'whole-turn-caption', kind: 'subtitle', role: 'dialogue', track_id: 'subtitles', turn_id: 't1', text: '整句字幕',
   start: { kind: 'turn', ref_id: 't1', edge: 'start' }, end: { kind: 'turn', ref_id: 't1', edge: 'end' } });
 render(<ProductionSpeechMainline plan={draft} change={(edit) => edit(draft)} onChoosePlacement={onChoosePlacement} />);
 expect(screen.getAllByText('字幕：整句字幕')).toHaveLength(2);
 fireEvent.click(screen.getAllByRole('button', { name: '编辑字幕' })[1]);
 expect(onChoosePlacement).toHaveBeenCalledWith('whole-turn-caption');
 expect(draft.placements.filter((item) => item.kind === 'subtitle')).toHaveLength(1);
 expect(draft.placements.find((item) => item.id === 'whole-turn-caption')!.start).toEqual({ kind: 'turn', ref_id: 't1', edge: 'start' });
});
it('switches the main shot while preserving the source take on surrounding windows', () => {
 const draft = timedPlan(); render(<ProductionSpeechMainline plan={draft} change={(edit) => edit(draft)} />);
 fireEvent.click(screen.getByRole('button', { name: '切换主镜头' }));
 expect(draft.placements.filter((item) => item.speech_take_id === 'take')).toHaveLength(2);
 expect(draft.placements.find((item) => item.start.kind === 'cue' && item.start.edge === 'start')).toMatchObject({ role: 'primary', end: { kind: 'cue', ref_id: 'cue', edge: 'end' } });
 expect(draft.speech_takes).toHaveLength(1);
});
it('saves manual source times through the evidence callback', async () => {
 const onImportSrt = vi.fn().mockResolvedValue(undefined); render(<ProductionSpeechMainline plan={timedPlan()} change={() => {}} onImportSrt={onImportSrt} />);
 fireEvent.click(screen.getByRole('button', { name: '修改台词源时间' }));
 fireEvent.change(screen.getByLabelText('台词源开始秒'), { target: { value: '0.5' } });
 fireEvent.click(screen.getByRole('button', { name: '确认人工时间并保存证据' }));
 expect(onImportSrt).toHaveBeenCalledWith('take', '', [{ text: '第一句', turn_id: 't1', cue_id: 'cue', source_start_us: 500000, source_end_us: 2000000 }], 'manual');
});

it('normalizes an untagged take origin before cutting a trimmed double speed source', () => {
 const draft = timedPlan(); const take = draft.speech_takes![0];
 take.source_in_us = 1000000; take.source_out_us = 7000000; take.speed_num = 2;
 draft.resources[0].duration_us = 8000000;
 draft.speech_cues![0].source_start_us = 3000000; draft.speech_cues![0].source_end_us = 5000000;
 const origin = draft.placements[0]; delete origin.speech_take_id;
 origin.start = { kind: 'beat', ref_id: 'b', edge: 'start' }; origin.end = { kind: 'beat', ref_id: 'b', edge: 'end' };
 origin.source_in_us = 0; origin.speed_num = 1;
 render(<ProductionSpeechMainline plan={draft} change={(edit) => edit(draft)} />);
 fireEvent.click(screen.getByRole('button', { name: '切换主镜头' }));
 const windows = draft.placements.filter((item) => item.speech_take_id === 'take');
 expect(windows).toHaveLength(2);
 expect(windows[0]).toMatchObject({ start: { kind: 'take', ref_id: 'take', edge: 'start' }, end: { kind: 'cue', ref_id: 'cue', edge: 'start' } });
 expect(windows[1]).toMatchObject({ start: { kind: 'cue', ref_id: 'cue', edge: 'end' }, end: { kind: 'take', ref_id: 'take', edge: 'end' } });
 for (const visual of windows) { expect(visual.source_in_us).toBeUndefined(); expect(visual.speed_num).toBeUndefined(); }
 const projected = projectProductionTimeline(draft);
 expect(projected.placements.find((item) => item.id === windows[0].id)).toMatchObject({ startFrame: 0, endFrame: 30 });
 expect(projected.placements.find((item) => item.id === windows[1].id)).toMatchObject({ startFrame: 60, endFrame: 90 });
 // Right window begins two program seconds into the take: 1s + 2s * 2 = source 5s.
 expect(take.source_in_us + 60 / 30 * take.speed_num / take.speed_den * 1e6).toBe(5000000);
});
