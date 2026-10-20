import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { MaterialSpeechReview } from './MaterialSpeechReview';

const summary = {
  observation_ref: { artifact_id: 'source', revision: 1 },
  target_signature: 'target',
  target_text: '黑咖啡，没有之一。',
  recognized_text: '黑咖啡没有知一',
  recognized_words: [
    { id: 'w1', text: '黑咖啡', start_us: 0, end_us: 1000000 },
    { id: 'w2', text: '没有', start_us: 1100000, end_us: 2000000 },
    { id: 'w3', text: '知一', start_us: 2100000, end_us: 3560000 },
  ],
  recognized_segments: [],
  source_duration_us: 4100000,
  timing_granularity: 'word' as const,
  transcript_match: 'review_required',
  audio_review: 'unavailable',
  lip_review: 'unavailable',
};

describe('MaterialSpeechReview', () => {
  it('shows target, raw transcript and original measured token times', () => {
    render(<MaterialSpeechReview summary={summary} onSave={vi.fn()} />);
    expect(screen.getByText('黑咖啡，没有之一。')).toBeTruthy();
    expect(screen.getByText('黑咖啡没有知一')).toBeTruthy();
    expect(screen.getByText(/完整素材.*4.100/)).toBeTruthy();
    expect(screen.getByText('2.100 – 3.560 秒')).toBeTruthy();
    expect(screen.getByText(/声音.*待试听/)).toBeTruthy();
  });

  it('submits one correction against the same observation after actual review', () => {
    const onSave = vi.fn();
    render(<MaterialSpeechReview summary={summary} onSave={onSave} />);
    fireEvent.change(screen.getByRole('textbox', { name: '识别词 3' }), { target: { value: '之一' } });
    const save = screen.getByRole('button', { name: '核对并保存' });
    expect(save.hasAttribute('disabled')).toBe(true);
    fireEvent.click(screen.getByRole('checkbox', { name: '已试听并核对本次修改' }));
    fireEvent.click(save);
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave).toHaveBeenCalledWith({
      observation_ref: summary.observation_ref,
      target_signature: 'target',
      confirmed: true,
      corrections: [{ word_id: 'w3', text: '之一', reason: '试听后校正识别文字' }],
    });
  });

  it('does not create word timing from sentence-only evidence', () => {
    render(<MaterialSpeechReview summary={{ ...summary, timing_granularity: 'segment',
      recognized_words: [], recognized_segments: [{ text: '黑咖啡没有之一', start_us: 0, end_us: 3560000 }] }}
      onSave={vi.fn()} />);
    expect(screen.getByText(/只提供句级时间/)).toBeTruthy();
    expect(screen.queryByRole('textbox', { name: '识别词 1' })).toBeNull();
  });

  it('clears review acknowledgement when correction text changes again', () => {
    render(<MaterialSpeechReview summary={summary} onSave={vi.fn()} />);
    fireEvent.change(screen.getByRole('textbox', { name: '识别词 3' }), { target: { value: '之一' } });
    fireEvent.click(screen.getByRole('checkbox', { name: '已试听并核对本次修改' }));
    expect(screen.getByRole('button', { name: '核对并保存' }).hasAttribute('disabled')).toBe(false);
    fireEvent.change(screen.getByRole('textbox', { name: '识别词 3' }), { target: { value: '知一' } });
    expect(screen.getByRole('button', { name: '核对并保存' }).hasAttribute('disabled')).toBe(true);
  });

  it('keeps pending listening separate from transcript agreement', () => {
    render(<MaterialSpeechReview summary={{ ...summary, transcript_match: 'normalized_match' }}
      onSave={vi.fn()} />);
    expect(screen.getByText('识别文字与方案一致')).toBeTruthy();
    expect(screen.getByText(/口型.*待审看/)).toBeTruthy();
  });
});

it('restores saved corrected words while retaining the raw transcript', () => {
  render(<MaterialSpeechReview summary={{ ...summary, corrected_words: summary.recognized_words.map((word) => word.id === 'w3' ? { ...word, text: '之一' } : word) }} onSave={vi.fn()} />);
  expect((screen.getByRole('textbox', { name: '识别词 3' }) as HTMLInputElement).value).toBe('之一');
  expect(screen.getByText('黑咖啡没有知一')).toBeTruthy();
  expect(screen.getByRole('button', { name: '核对并保存' }).hasAttribute('disabled')).toBe(true);
});
it('shows named dialogue and lets users seek to the measured difference', () => {
  const seek = vi.fn();
  render(<MaterialSpeechReview summary={{ ...summary, target_turns: [{ id: 't', speaker: '主持人', text: summary.target_text }], differences: [{ kind: 'replacement', target_text: '之一', observed_text: '知一', start_us: 2100000, end_us: 3560000 }] }} onSave={vi.fn()} onSeek={seek} />);
  expect(screen.getByText(/主持人/)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '试听差异 2.100 秒' }));
  expect(seek).toHaveBeenCalledWith(2.1);
});
