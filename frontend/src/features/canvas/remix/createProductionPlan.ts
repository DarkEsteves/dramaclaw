import { CANVAS_NODE_TYPES } from '@/features/canvas/domain/canvasNodes';
import { useCanvasStore } from '@/stores/canvasStore';
import { legacyScriptToPlan } from './legacyScriptToPlan';
import type { ProductionPlan } from './productionPlanModel';
import { emptyProductionPlanV2 } from './productionPlanV2Model';

function createPlanForStory(storyId: string, draft?: ProductionPlan, lockedTurns?: ProductionPlan['beats'][number]['turns']): string {
  const store = useCanvasStore.getState();
  const story = store.nodes.find((node) => node.id === storyId);
  if (!story) throw new Error('找不到原片分析节点');
  const blueprint = story.data.referenceBlueprintRef;
  const blueprintRef = blueprint && typeof blueprint === 'object'
    && typeof (blueprint as { artifactId?: unknown }).artifactId === 'string'
    && Number.isInteger((blueprint as { revision?: unknown }).revision)
    ? blueprint as { artifactId: string; revision: number } : null;
  const editable = emptyProductionPlanV2('', blueprintRef);
  if (draft) {
    editable.goal = draft.goal;
    editable.beats = draft.beats.map((beat) => ({ id: beat.id, purpose: beat.purpose,
      duration_frames: beat.duration_frames, turns: beat.turns.map((turn) => ({ id: crypto.randomUUID(), speaker: turn.speaker, text: turn.text })) }));
    editable.placements = draft.beats.flatMap((beat) => beat.audio.map((cue) => ({
      id: cue.id, kind: 'audio' as const, role: cue.role === 'speech' ? 'speech' as const : 'narration' as const,
      track_id: 'audio:speech', description: cue.description,
      start: { kind: 'beat' as const, ref_id: beat.id, edge: 'start' as const },
      end: { kind: 'beat' as const, ref_id: beat.id, edge: 'end' as const },
    })));
  }
  const id = store.addNode(CANVAS_NODE_TYPES.script, store.findNodePosition(storyId, 520, 380), {
    displayName: '作品方案', remixRole: 'production_plan',
    productionPlanV2Draft: editable, productionPlanV2Dirty: Boolean(draft),
    productionPlanOpenOnCreate: true,
    productionPlanSourceStoryId: storyId,
    productionPlanBlueprintRef: blueprintRef ? { ...blueprintRef } : null,
    ...(lockedTurns ? { productionPlanLockedTurns: lockedTurns } : {}),
  });
  if (!store.addEdge(storyId, id)) {
    store.deleteNodes([id]);
    throw new Error('作品方案连线失败，请检查节点连接规则');
  }
  store.setSelectedNode(id);
  store.requestFocusNode(id);
  return id;
}

export function createProductionPlanFromStory(storyId: string): string {
  return createPlanForStory(storyId);
}

export function convertLegacyScriptToPlan(scriptNodeId: string): string {
  const store = useCanvasStore.getState();
  const script = store.nodes.find((node) => node.id === scriptNodeId && node.type === CANVAS_NODE_TYPES.script);
  if (!script || script.data.remixRole === 'production_plan') throw new Error('找不到可转换的旧脚本');
  const storyId = store.edges.find((edge) => edge.target === scriptNodeId
    && store.nodes.some((node) => node.id === edge.source && node.type === CANVAS_NODE_TYPES.videoStory))?.source;
  if (!storyId) throw new Error('旧脚本未连接原片分析节点');
  const dialogue = (script.data.remixScriptDraft as { dialogue?: unknown } | undefined)?.dialogue;
  if (typeof dialogue !== 'string' || !dialogue.trim()) throw new Error('旧脚本还没有可转换的台词');
  const draft = legacyScriptToPlan(dialogue, typeof script.data.remixTopic === 'string' ? script.data.remixTopic : '');
  return createPlanForStory(storyId, draft, draft.beats.flatMap((beat) => beat.turns.map((turn) => ({ ...turn }))));
}
