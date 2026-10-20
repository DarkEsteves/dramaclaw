import { describe, expect, it } from 'vitest';
import { normalizeMessage } from '@/features/superchat/message';

const legacyReply = JSON.stringify({
  message: '作品方案已提交，正在后台起草。',
  mode: 'mutation',
  canvas_receipts: [{ bridge_key: 'bridge-1', revision: null }],
});

describe('saved canvas replies', () => {
  it('projects only the prose from an exact legacy assistant envelope with host outcome', () => {
    const message = normalizeMessage({ id: 'saved-1', role: 'assistant', content: legacyReply,
      metadata: { operation_outcome: { disposition: 'completed' } },
      parts: [{ type: 'text', text: legacyReply }, { type: 'canvas_feedback', event: { type: 'canvas_feedback' } }] });
    expect(message?.text).toBe('作品方案已提交，正在后台起草。');
    expect(message?.parts?.filter((part) => part.type === 'text').map((part) => part.text)).toEqual(['作品方案已提交，正在后台起草。']);
    expect(message?.parts?.some((part) => part.type === 'canvas_feedback')).toBe(true);
  });

  it('leaves ordinary JSON and user text intact', () => {
    expect(normalizeMessage({ role: 'assistant', content: legacyReply })?.text).toBe(legacyReply);
    expect(normalizeMessage({ role: 'user', content: legacyReply,
      metadata: { operation_outcome: {} } })?.text).toBe(legacyReply);
  });
});
