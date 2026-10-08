import { CANVAS_NODE_TYPES } from '@/features/canvas/domain/canvasNodes';
import { useCanvasStore } from '@/stores/canvasStore';
import { createProductionPlanFromStory } from './createProductionPlan';
import { requestRemixScriptEditor } from './remixScriptEditorRequests';

function scriptContentScore(data: Record<string, unknown>): number {
  if (data.remixScriptRef || data.productionPlanRef || data.remixScriptLastSavedRef) return 2;
  const dialogue = (data.remixScriptDraft as { dialogue?: unknown } | undefined)?.dialogue;
  const plan = data.productionPlanDraft as { goal?: unknown; beats?: Array<{ purpose?: unknown; turns?: unknown[] }> } | undefined;
  if (typeof dialogue === 'string' && dialogue.trim()) return 1;
  if (typeof plan?.goal === 'string' && plan.goal.trim()) return 1;
  if (plan?.beats?.some((beat) => (typeof beat.purpose === 'string' && beat.purpose.trim()) || beat.turns?.length)) return 1;
  return 0;
}

/** Create a script branch from the analyzed reference story. */
export function createScriptFromBlueprint(storyId: string): string {
  const store = useCanvasStore.getState();
  const story = store.nodes.find((node) => node.id === storyId);
  const connectedScripts = store.edges.filter((edge) => edge.source === storyId)
    .map((edge) => store.nodes.find((node) => node.id === edge.target && node.type === CANVAS_NODE_TYPES.script))
    .filter((node): node is NonNullable<typeof node> => Boolean(node))
    .sort((left, right) => Number(right.data.remixRole === 'production_plan') - Number(left.data.remixRole === 'production_plan')
      || scriptContentScore(right.data) - scriptContentScore(left.data));
  const existing = connectedScripts[0];
  if (existing) {
    store.setSelectedNode(existing.id);
    store.requestFocusNode(existing.id);
    if (existing.data.remixRole === 'production_plan') {
      store.updateNodeData(existing.id, { productionPlanOpenOnCreate: true });
    } else {
      requestRemixScriptEditor(existing.id);
    }
    return existing.id;
  }
  if (!story) throw new Error('找不到原片分析节点');
  return createProductionPlanFromStory(storyId);
}
