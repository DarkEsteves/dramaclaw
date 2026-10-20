import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ReferenceEventList } from './ReferenceEventList';

describe('ReferenceEventList', () => {
  it('keeps wheel scrolling inside the event list instead of panning the canvas', () => {
    const { container } = render(<ReferenceEventList events={[]} />);
    expect(container.firstElementChild).toHaveClass('nowheel');
  });

  it('scrolls the explanatory text with events so it cannot cover event rows', () => {
    render(<ReferenceEventList events={[]} />);
    expect(screen.getByText(/按原片时间排列/)).not.toHaveClass('sticky');
  });

  it('shows timed visual and spoken evidence with separate narrative inference', () => {
    render(<ReferenceEventList videoUrl="/clip.mp4" events={[{
      start_us: 8_200_000, end_us: 11_500_000,
      frame_url: '/frame.png', frame_pts: [82], frame_times_us: [8_500_000],
      spoken_text: '喝第一口咖啡', word_spans: [{ text: '咖啡', start_us: 9_000_000, end_us: 9_500_000 }],
      visual_observation: '人物拿起杯子', narrative_inference: '产品登场', note_status: 'needs_review',
    }]} />);

    expect(screen.getByText('08.2–11.5 秒')).toBeTruthy();
    expect(screen.getByText('喝第一口咖啡')).toBeTruthy();
    expect(screen.getByText('人物拿起杯子')).toBeTruthy();
    expect(screen.getByText('产品登场')).toBeTruthy();
    expect(screen.getByAltText('事件画面 1').getAttribute('src')).toBe('/frame.png');
    fireEvent.click(screen.getByRole('button', { name: /08.2–11.5 秒/ }));
    expect(screen.getByLabelText('原片事件预览')).toBeTruthy();
    expect(screen.getByText(/画面时间/)).toBeTruthy();
  });

  it('marks an event without visual evidence and explains where speech corrections happen', () => {
    render(<ReferenceEventList events={[{
      start_us: 4_000_000, end_us: 5_000_000, frame_times_us: [],
      spoken_text: 'Christine', note_status: 'missing_frame',
    }]} />);
    expect(screen.getByText('画面未核实')).toBeTruthy();
    expect(screen.getByText(/转写节点保存校字/)).toBeTruthy();
  });

  it('shows a likely subtitle and transcript mismatch without changing the spoken text', () => {
    render(<ReferenceEventList events={[{
      start_us: 4_000_000, end_us: 6_000_000,
      spoken_text: 'Christine, five grams a day.',
      visible_text: 'CREATINE FIVE GRAMS',
      transcript_warnings: [{ transcript_word: 'Christine', caption_word: 'CREATINE' }],
    }]} />);
    expect(screen.getByText('Christine, five grams a day.')).toBeTruthy();
    expect(screen.getByText(/Christine.*CREATINE/)).toBeTruthy();
  });

  it('lets reviewers correct the visual description, visible caption, and narrative role while keeping timed evidence', () => {
    const onOverrideChange = vi.fn();
    render(<ReferenceEventList onOverrideChange={onOverrideChange} events={[{
      event_id: 'event-1', start_us: 4_000_000, end_us: 6_000_000,
      observed_frame: { pts: 360_000, time_base: '1/90000', time_us: 4_000_000, url: '/frame.png' },
      spoken_range: { text: 'maybe this works', start_us: 4_100_000, end_us: 5_500_000 },
      visual_observation: '人物展示杯子', visible_text: 'WRONG LABEL', narrative_inference: '模型认为这是产品揭晓',
      user_narrative_role: '制造悬念', review_state: 'needs_review',
    }]} />);

    expect(screen.getByText(/PTS 360000 · 1\/90000/)).toBeTruthy();
    expect(screen.getByText('制造悬念')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /04.0–06.0 秒/ }));
    fireEvent.click(screen.getByText('查看时间与证据'));
    expect(screen.getByText(/模型叙事判断：模型认为这是产品揭晓/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText('事件 1 画面描述'), { target: { value: '人物拿着杯子，并未展示' } });
    fireEvent.change(screen.getByLabelText('事件 1 画面字幕'), { target: { value: 'CREATINE' } });
    fireEvent.change(screen.getByLabelText('事件 1 叙事作用'), { target: { value: '明确产品价值' } });

    expect(onOverrideChange).toHaveBeenNthCalledWith(1, 'event-1', { visualObservation: '人物拿着杯子，并未展示' });
    expect(onOverrideChange).toHaveBeenNthCalledWith(2, 'event-1', { visibleText: 'CREATINE' });
    expect(onOverrideChange).toHaveBeenNthCalledWith(3, 'event-1', { narrativeRole: '明确产品价值' });
    expect(onOverrideChange).toHaveBeenCalledTimes(3);
    expect(screen.queryByLabelText('事件 1 核对状态')).toBeNull();
  });

  it('shows the corrected narrative role in the event summary while retaining the model judgment as evidence', () => {
    render(<ReferenceEventList events={[{
      event_id: 'event-1', start_us: 0, end_us: 1_000_000,
      visual_observation: '人物拿着杯子', narrative_inference: '模型误判为产品展示',
      user_narrative_role: '开场提问', review_state: 'reviewed',
    }]} />);

    expect(screen.getByText('开场提问')).toBeTruthy();
    expect(screen.queryByText('模型误判为产品展示')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /00.0–01.0 秒/ }));
    fireEvent.click(screen.getByText('查看时间与证据'));
    expect(screen.getByText(/模型叙事判断：模型误判为产品展示/)).toBeTruthy();
  });

  it('shows analysis content without a review count or bulk acknowledgement action', () => {
    render(<ReferenceEventList events={[
      { event_id: 'pending-1', start_us: 0, end_us: 1_000_000, review_state: 'needs_review' },
      { event_id: 'uncertain-1', start_us: 1_000_000, end_us: 2_000_000, review_state: 'reviewed_uncertain' },
      { event_id: 'pending-2', start_us: 2_000_000, end_us: 3_000_000 },
    ]} />);

    expect(screen.queryByText(/项待核对/)).toBeNull();
    expect(screen.queryByRole('button', { name: '全部标记为已核对' })).toBeNull();
  });

  it('hides the bulk action after every event has been reviewed', () => {
    render(<ReferenceEventList events={[
      { start_us: 0, end_us: 1_000_000, review_state: 'reviewed' },
      { start_us: 1_000_000, end_us: 2_000_000, review_state: 'reviewed_uncertain' },
    ]} />);

    expect(screen.queryByText('已全部核对')).toBeNull();
    expect(screen.queryByRole('button', { name: '全部标记为已核对' })).toBeNull();
  });
});
