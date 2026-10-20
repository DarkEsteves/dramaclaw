import { emptyProductionPlan, type ProductionPlan } from './productionPlanModel';

export function legacyScriptToPlan(dialogue: string, topic: string): ProductionPlan {
  const turns = dialogue.split(/\r?\n/).filter((line) => line.trim()).map((line) => {
    const match = /^\s*([^：:\n]{1,24})[：:]\s*(\S.*)$/.exec(line);
    return match ? { speaker: match[1].trim(), text: match[2] }
      : { speaker: '旁白', text: line };
  });
  const plan = emptyProductionPlan();
  plan.goal = topic.trim() || '沿用原改编脚本';
  plan.beats[0].purpose = '保留已有台词，补充整体规划';
  plan.beats[0].turns = turns;
  plan.beats[0].duration_frames = Math.min(1800, Math.max(90, turns.length * 75));
  plan.beats[0].audio = [{
    id: crypto.randomUUID(),
    role: turns.every((turn) => turn.speaker === '旁白') ? 'narration' : 'speech',
    description: '按已确认台词制作人声',
    media: null,
  }];
  return plan;
}
