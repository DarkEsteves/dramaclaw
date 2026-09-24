import { describe, expect, it } from 'vitest';
import { suggestBlueprintRules, upgradeBlueprintRuleDraft } from './blueprintModel';

describe('suggestBlueprintRules', () => {
  it('uses the model analysis of reusable structure without copying the source plot', () => {
    const rules = suggestBlueprintRules({ rawResult: { video_story: {
      summary: '健身女子每天吃肌酸，最后把补剂塞给男方。',
      adaptation_structure: {
        narrative_role: '用问答揭示信息，再以调侃和反转完成收尾。',
        reusable_beats: ['提问', '回答', '生活场景举例', '调侃', '反转'],
      },
    } } });

    expect(rules).toEqual({
      narrativeRole: '用问答揭示信息，再以调侃和反转完成收尾。',
      preserveRule: '保留“提问 → 回答 → 生活场景举例 → 调侃 → 反转”的叙事节奏。',
      replaceRule: '',
    });
  });

  it('abstracts older event analyses instead of echoing characters and product details', () => {
    const rules = suggestBlueprintRules({
      rawResult: { video_story: { summary: '健身女子每天吃肌酸，最后把补剂塞给男方。' } },
      referenceEvents: [
        { narrative_inference: '开场抛出问题，引出女方最离不开之物' },
        { narrative_inference: '女方亮出答案肌酸并报出每日用量' },
        { narrative_inference: '插入日常画面，强调天天服用' },
        { narrative_inference: '男方调侃对方像用面粉一样用补剂' },
        { narrative_inference: '女方递出罐子，反称对方更需要' },
      ],
    });

    expect(rules.narrativeRole).toBe('通过提问、揭晓、举例佐证、调侃、反转推进故事。');
    expect(rules.preserveRule).toBe('保留“提问 → 揭晓 → 举例佐证 → 调侃 → 反转”的叙事节奏。');
    expect(JSON.stringify(rules)).not.toMatch(/肌酸|补剂|女方|男方|替换人物/);
  });

  it('does not invent rules when the analysis has no recognizable structure', () => {
    expect(suggestBlueprintRules({ rawResult: { video_story: { summary: '人物在房间里拿着一个盒子。' } } })).toEqual({
      narrativeRole: '', preserveRule: '', replaceRule: '',
    });
  });

  it('upgrades only untouched old defaults while preserving deliberate edits', () => {
    const old = { narrativeRole: '原片剧情', preserveRule: '保留原片剧情', replaceRule: '自动替换人物产品' };
    const next = { narrativeRole: '提问后反转', preserveRule: '保留提问与反转', replaceRule: '' };
    expect(upgradeBlueprintRuleDraft({
      blueprintNarrativeRole: old.narrativeRole,
      blueprintPreserveRule: '用户自己写的保留规则',
      blueprintReplaceRule: old.replaceRule,
    }, next, old)).toEqual({ blueprintNarrativeRole: next.narrativeRole, blueprintReplaceRule: '' });
    expect(upgradeBlueprintRuleDraft({ blueprintNarrativeRole: old.narrativeRole, blueprintRulesTouched: true }, next, old)).toEqual({});
    expect(upgradeBlueprintRuleDraft({ blueprintNarrativeRole: old.narrativeRole, referenceBlueprintRef: { artifactId: 'saved', revision: 1 } }, next, old)).toEqual({});
    expect(upgradeBlueprintRuleDraft({
      blueprintNarrativeRole: old.narrativeRole, blueprintPreserveRule: old.preserveRule, blueprintReplaceRule: old.replaceRule,
      referenceBlueprintRef: { artifactId: 'saved', revision: 1 },
    }, next, old)).toEqual({
      blueprintNarrativeRole: next.narrativeRole, blueprintPreserveRule: next.preserveRule, blueprintReplaceRule: '',
    });
  });
});
