import { describe, expect, it } from 'vitest';
import { isLegacyRoleLabelledRemixAudio, parseDialogueTurns, resolveAudioDialogue, singleVoiceAudioText } from './dialogueTurns';

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

describe('resolveAudioDialogue', () => {
  it('uses the same role parser for a manually entered audio node', () => {
    expect(resolveAudioDialogue({ dialogueVoiceMode: 'dialogue', text: 's:wew\nw:fwefw' })).toEqual({
      enabled: true,
      turns: [{ speaker: 's', text: 'wew' }, { speaker: 'w', text: 'fwefw' }],
      speakers: ['s', 'w'],
      error: null,
    });
  });

  it('keeps ordinary text on the original single-voice path', () => {
    expect(resolveAudioDialogue({ text: 's:wew\nw:fwefw' }).enabled).toBe(false);
  });

  it('blocks partial dialogue instead of retaining old turns', () => {
    const result = resolveAudioDialogue({ dialogueVoiceMode: 'dialogue', text: 's:wew\nmissing label' });
    expect(result.enabled).toBe(true);
    expect(result.turns).toEqual([]);
    expect(result.error).toContain('每句台词');
  });

  it('keeps a linked script in dialogue mode without changing its saved turns', () => {
    const turns = [{ speaker: '男', text: '你好？' }, { speaker: '女', text: '你好。' }];
    expect(resolveAudioDialogue({ remixScriptRef: { artifactId: 's', revision: 1 }, remixDialogueTurns: turns, text: '男：你好？\n女：你好。' }).turns).toEqual(turns);
  });

  it('allows a linked script to use one voice without speaking role labels', () => {
    const turns = [{ speaker: '男', text: '你好？' }, { speaker: '女', text: '你好。' }];
    expect(resolveAudioDialogue({ dialogueVoiceMode: 'single', remixDialogueTurns: turns, text: '男：你好？\n女：你好。' })).toEqual({
      enabled: false, turns: [], speakers: [], error: null,
    });
    expect(singleVoiceAudioText({ dialogueVoiceMode: 'single', remixScriptRef: { artifactId: 's', revision: 1 }, remixDialogueTurns: turns, text: '男：你好？\n女：你好。' }, '男：你好？\n女：你好。')).toBe('你好？\n你好。');
    expect(singleVoiceAudioText({ dialogueVoiceMode: 'single', remixDialogueTurns: turns }, '普通节点新输入')).toBe('普通节点新输入');
  });
});
