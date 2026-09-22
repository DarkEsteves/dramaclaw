import { CANVAS_NODE_TYPES } from '@/features/canvas/domain/canvasNodes';
import { useCanvasStore } from '@/stores/canvasStore';
import { requestRemixScriptEditor } from './remixScriptEditorRequests';

/** Create a new script branch from a saved reference blueprint. */
export function createScriptFromBlueprint(storyId: string): string {
  const store = useCanvasStore.getState();
  const story = store.nodes.find((node) => node.id === storyId);
  const storyData = story?.data ?? {};
  const cloneRef = (value: unknown) => value && typeof value === 'object'
    ? { ...(value as Record<string, unknown>) }
    : null;
  const childId = store.addNode(CANVAS_NODE_TYPES.script, store.findNodePosition(storyId, 420, 360), {
    remixRole: 'adaptation_script',
    blueprintNarrativeRole: typeof storyData.blueprintNarrativeRole === 'string' ? storyData.blueprintNarrativeRole : '',
    blueprintPreserveRule: typeof storyData.blueprintPreserveRule === 'string' ? storyData.blueprintPreserveRule : '',
    blueprintReplaceRule: typeof storyData.blueprintReplaceRule === 'string' ? storyData.blueprintReplaceRule : '',
    referenceEvidenceRef: cloneRef(storyData.referenceEvidenceRef),
    sourceMediaPath: typeof storyData.sourceMediaPath === 'string' ? storyData.sourceMediaPath : null,
    remixBlueprintSeedRef: cloneRef(storyData.referenceBlueprintRef),
    referenceBlueprintRef: null,
    remixBlueprintRef: null,
    blueprintRevision: 0,
    blueprintDirty: true,
  });
  if (!store.addEdge(storyId, childId)) {
    store.deleteNodes([childId]);
    throw new Error('改编脚本连线失败，请检查节点连接规则。');
  }
  store.setSelectedNode(childId);
  store.requestFocusNode(childId);
  requestRemixScriptEditor(childId);
  return childId;
}
