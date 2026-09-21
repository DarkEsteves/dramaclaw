import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { RemixJourneyButton } from './RemixJourneyButton';

describe('RemixJourneyButton', () => {
  it('shows one visible next step and explains required input', () => {
    const onContinue = vi.fn();
    render(<RemixJourneyButton step={{ kind: 'input', targetNodeId: 'audio', label: '生成或上传旁白音频', inputHint: '可直接上传已经录好的音频。' }} onContinue={onContinue} />);
    const button = screen.getByRole('button', { name: '继续改编 · 生成或上传旁白音频' });
    expect(screen.getAllByRole('button')).toHaveLength(1);
    expect(button).toHaveAttribute('title', '可直接上传已经录好的音频。');
    fireEvent.click(button);
    expect(onContinue).toHaveBeenCalledOnce();
  });

  it('labels a completed editable export without another hidden action', () => {
    render(<RemixJourneyButton step={{ kind: 'complete', targetNodeId: 'compose', label: '成片可编辑并可下载 MP4' }} onContinue={() => {}} />);
    expect(screen.getByRole('button', { name: '成片可编辑并可下载 MP4' })).toBeInTheDocument();
  });
});
