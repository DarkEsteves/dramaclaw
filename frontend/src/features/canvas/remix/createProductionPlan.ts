import { CANVAS_NODE_TYPES } from '@/features/canvas/domain/canvasNodes';
import { useCanvasStore } from '@/stores/canvasStore';
import { emptyProductionPlan } from './productionPlanModel';

export function createProductionPlanFromStory(storyId: string): string {
  const store = useCanvasStore.getState();
  const story = store.nodes.find((node) => node.id === storyId);
  if (!story) throw new Error('找不到原片分析节点');
  const blueprint = story.data.referenceBlueprintRef;
  const id = store.addNode(CANVAS_NODE_TYPES.script, store.findNodePosition(storyId, 520, 380), {
    displayName: '作品方案', remixRole: 'production_plan',
    productionPlanDraft: emptyProductionPlan(), productionPlanDirty: true,
    productionPlanOpenOnCreate: true,
    productionPlanSourceStoryId: storyId,
    productionPlanBlueprintRef: blueprint && typeof blueprint === 'object' ? { ...blueprint } : null,
  });
  if (!store.addEdge(storyId, id)) {
    store.deleteNodes([id]);
    throw new Error('作品方案连线失败，请检查节点连接规则');
  }
  store.setSelectedNode(id);
  store.requestFocusNode(id);
  return id;
}
