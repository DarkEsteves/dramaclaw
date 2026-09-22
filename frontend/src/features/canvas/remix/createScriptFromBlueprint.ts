import { CANVAS_NODE_TYPES } from '@/features/canvas/domain/canvasNodes';
import { useCanvasStore } from '@/stores/canvasStore';
import { requestRemixScriptEditor } from './remixScriptEditorRequests';

/** Create a new script branch from a saved reference blueprint. */
export function createScriptFromBlueprint(storyId: string): string {
  const store = useCanvasStore.getState();
  const childId = store.addNode(CANVAS_NODE_TYPES.script, store.findNodePosition(storyId, 420, 360));
  if (!store.addEdge(storyId, childId)) {
    store.deleteNodes([childId]);
    throw new Error('改编脚本连线失败，请检查节点连接规则。');
  }
  store.setSelectedNode(childId);
  store.requestFocusNode(childId);
  requestRemixScriptEditor(childId);
  return childId;
}
