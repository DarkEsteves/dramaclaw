import { describe, expect, it } from 'vitest';
import { isLegacyRoleLabelledRemixAudio, parseDialogueTurns } from './dialogueTurns';

describe('parseDialogueTurns', () => {
  it('keeps speaking order while excluding role labels from TTS text', () => {
    expect(parseDialogueTurns('男：你喝什么？\n女：无糖气泡水。\n女：要冰的。')).toEqual({
      turns: [
        { speaker: '男', text: '你喝什么？' },
        { speaker: '女', text: '无糖气泡水。' },
        { speaker: '女', text: '要冰的。' },
      ],
      speakers: ['男', '女'],
    });
  });

  it('leaves an ordinary one-person narration on the existing audio path', () => {
    expect(parseDialogueTurns('清晨，城市醒来。')).toBeNull();
  });

  it('refuses partially labelled dialogue instead of sending labels to speech', () => {
    expect(() => parseDialogueTurns('男：你喝什么？\n无糖气泡水。')).toThrow('每句台词');
  });

  it('recognizes only legacy remix narration as unsafe for single-voice TTS', () => {
    expect(isLegacyRoleLabelledRemixAudio({ remixScriptRef: { artifactId: 's', revision: 1 }, text: '男：你好？\n女：你好。' })).toBe(true);
    expect(isLegacyRoleLabelledRemixAudio({ text: '男：你好？\n女：你好。' })).toBe(false);
    expect(isLegacyRoleLabelledRemixAudio({ remixScriptRef: { artifactId: 's', revision: 1 }, text: '男：你好？', remixDialogueTurns: [{ speaker: '男', text: '你好？' }] })).toBe(false);
  });
});
