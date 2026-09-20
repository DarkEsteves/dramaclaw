import { describe, expect, it } from 'vitest';
import { missingDialogueSpeakers } from './dialogueVoicePolicy';

describe('dialogue voice readiness', () => {
  const turns = [{ speaker: '男', text: '你好' }, { speaker: '女', text: '再见' }];

  it('requires a distinct configured voice entry for every speaker', () => {
    expect(missingDialogueSpeakers(turns, { 男: { ref: { scope: 'user_custom', voiceId: 'm' }, label: '声线 M' } })).toEqual(['女']);
  });

  it('rejects incomplete custom voice selections', () => {
    expect(missingDialogueSpeakers(turns, {
      男: { ref: { scope: 'user_custom' }, label: '无效' },
      女: { ref: { scope: 'user_custom', voiceId: 'f' }, label: '声线 F' },
    })).toEqual(['男']);
  });
});
