import { CANVAS_NODE_TYPES } from '@/features/canvas/domain/canvasNodes';
import { useCanvasStore } from '@/stores/canvasStore';

/** Enter script editing directly from a saved reference blueprint. */
export function createScriptFromBlueprint(storyId: string): string {
  const store = useCanvasStore.getState();
  const existing = store.nodes.find((node) => node.type === CANVAS_NODE_TYPES.script
    && store.edges.some((edge) => edge.source === storyId && edge.target === node.id));
  if (existing) {
    store.updateNodeData(existing.id, { openRemixEditorRequested: true });
    store.setSelectedNode(existing.id);
    store.requestFocusNode(existing.id);
    return existing.id;
  }

  const childId = store.addNode(CANVAS_NODE_TYPES.script, store.findNodePosition(storyId, 420, 360), {
    openRemixEditorRequested: true,
  });
  if (!store.addEdge(storyId, childId)) {
    store.deleteNodes([childId]);
    throw new Error('改编脚本连线失败，请检查节点连接规则。');
  }
  store.setSelectedNode(childId);
  store.requestFocusNode(childId);
  return childId;
}
