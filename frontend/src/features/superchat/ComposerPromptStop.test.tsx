import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ComposerPromptStop } from './ComposerPromptStop';

describe('ComposerPromptStop', () => {
  it.each(['assistant clarification', 'Skill Studio question'])('keeps stop reachable during a busy %s without answering it', (prompt) => {
    const abort = vi.fn();
    const answer = vi.fn();
    render(<form onSubmit={answer}>
      <div>{prompt}</div>
      <ComposerPromptStop hasActiveComposerPrompt busy onAbort={abort} label="停止生成" />
    </form>);
    const stop = screen.getByRole('button', { name: '停止生成' });
    expect(abort).not.toHaveBeenCalled();
    fireEvent.keyDown(stop, { key: 'Escape' });
    expect(abort).not.toHaveBeenCalled();
    fireEvent.click(stop);
    expect(abort).toHaveBeenCalledTimes(1);
    expect(answer).not.toHaveBeenCalled();
    expect(screen.getByText(prompt)).toBeInTheDocument();
  });

  it.each([[true, false], [false, true], [false, false]])('has no extra stop for prompt=%s busy=%s', (prompt, busy) => {
    const abort = vi.fn();
    render(<ComposerPromptStop hasActiveComposerPrompt={prompt} busy={busy} onAbort={abort} label="停止生成" />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(abort).not.toHaveBeenCalled();
  });

  it('removes the control when the active turn ends and uses the current callback on a new turn', () => {
    const oldAbort = vi.fn();
    const currentAbort = vi.fn();
    const { rerender } = render(<ComposerPromptStop hasActiveComposerPrompt busy onAbort={oldAbort} label="停止生成" />);
    rerender(<ComposerPromptStop hasActiveComposerPrompt busy={false} onAbort={oldAbort} label="停止生成" />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    rerender(<ComposerPromptStop hasActiveComposerPrompt busy onAbort={currentAbort} label="停止生成" />);
    fireEvent.click(screen.getByRole('button', { name: '停止生成' }));
    expect(currentAbort).toHaveBeenCalledTimes(1);
    expect(oldAbort).not.toHaveBeenCalled();
  });
});
