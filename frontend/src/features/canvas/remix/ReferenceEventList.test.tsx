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
    expect(screen.getByText(/转写节点校对/)).toBeTruthy();
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

  it('exposes immutable evidence beside editable narrative role and review state', () => {
    const onOverrideChange = vi.fn();
    render(<ReferenceEventList onOverrideChange={onOverrideChange} events={[{
      event_id: 'event-1', start_us: 4_000_000, end_us: 6_000_000,
      observed_frame: { pts: 360_000, time_base: '1/90000', time_us: 4_000_000, url: '/frame.png' },
      spoken_range: { text: 'maybe this works', start_us: 4_100_000, end_us: 5_500_000 },
      visual_observation: '人物展示杯子', narrative_inference: '模型认为这是产品揭晓',
      user_narrative_role: '制造悬念', review_state: 'needs_review',
    }]} />);

    expect(screen.getByText(/PTS 360000 · 1\/90000/)).toBeTruthy();
    expect(screen.getByText('模型认为这是产品揭晓')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('事件 1 叙事作用'), { target: { value: '明确产品价值' } });
    fireEvent.change(screen.getByLabelText('事件 1 核对状态'), { target: { value: 'reviewed_uncertain' } });

    expect(onOverrideChange).toHaveBeenNthCalledWith(1, 'event-1', { narrativeRole: '明确产品价值' });
    expect(onOverrideChange).toHaveBeenNthCalledWith(2, 'event-1', { reviewState: 'reviewed_uncertain' });
  });
});
