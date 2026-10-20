import { describe, expect, it } from 'vitest';
import { legacyScriptToPlan } from './legacyScriptToPlan';

describe('legacyScriptToPlan', () => {
  it('keeps every speaker and spoken line exactly in order', () => {
    const plan = legacyScriptToPlan('女：晚上喝咖啡吗？\n男：换成黑咖啡。', '黑咖啡');
    expect(plan.goal).toBe('黑咖啡');
    expect(plan.beats[0].turns).toEqual([
      { speaker: '女', text: '晚上喝咖啡吗？' },
      { speaker: '男', text: '换成黑咖啡。' },
    ]);
    expect(plan.beats[0].audio[0].role).toBe('speech');
  });

  it('treats unlabelled lines as narration without rewriting them', () => {
    const plan = legacyScriptToPlan('清晨，喝一杯黑咖啡。', '晨间提效');
    expect(plan.beats[0].turns).toEqual([{ speaker: '旁白', text: '清晨，喝一杯黑咖啡。' }]);
    expect(plan.beats[0].audio[0].role).toBe('narration');
  });
});
