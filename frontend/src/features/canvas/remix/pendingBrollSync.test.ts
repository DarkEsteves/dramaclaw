import { describe, expect, it, vi } from 'vitest';
import { resolvePendingBrollDraft } from './pendingBrollSync';

describe('resolvePendingBrollDraft', () => {
  it('resolves pending visual revisions once and applies them to every matching event', async () => {
    const commit = vi.fn();
    const resolveVisual = vi.fn(async () => ({ mediaRevision: 'a'.repeat(64), commit }));
    const result = await resolvePendingBrollDraft({
      dialogue: '甲乙丙丁',
      selections: [
        { id: 'a', label: '甲', start: 0, end: 1, visualNodeId: 'visual-1' },
        { id: 'b', label: '乙', start: 1, end: 2, visualNodeId: 'visual-1' },
        { id: 'c', label: '丙', start: 2, end: 3 },
      ],
    }, ['visual-1'], resolveVisual);

    expect(resolveVisual).toHaveBeenCalledTimes(1);
    expect(commit).toHaveBeenCalledTimes(1);
    expect(result?.selections).toEqual([
      expect.objectContaining({ id: 'a', visualMediaRevision: 'a'.repeat(64) }),
      expect.objectContaining({ id: 'b', visualMediaRevision: 'a'.repeat(64) }),
      expect.objectContaining({ id: 'c' }),
    ]);
    expect(result?.selections[2]).not.toHaveProperty('visualMediaRevision');
  });

  it('does nothing when no pending visual is ready', async () => {
    const resolveVisual = vi.fn();
    const result = await resolvePendingBrollDraft({ dialogue: '甲', selections: [{ id: 'a', label: '甲', start: 0, end: 1, visualNodeId: 'visual-1' }] }, [], resolveVisual);
    expect(result).toBeNull();
    expect(resolveVisual).not.toHaveBeenCalled();
  });
});
