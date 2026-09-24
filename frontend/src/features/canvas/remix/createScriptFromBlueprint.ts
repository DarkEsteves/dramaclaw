import { CANVAS_NODE_TYPES } from '@/features/canvas/domain/canvasNodes';
import { useCanvasStore } from '@/stores/canvasStore';
import { legacySuggestBlueprintRules, suggestBlueprintRules, upgradeBlueprintRuleDraft } from './blueprintModel';
import { requestRemixScriptEditor } from './remixScriptEditorRequests';

/** Create a script branch from the analyzed reference story. */
export function createScriptFromBlueprint(storyId: string): string {
  const store = useCanvasStore.getState();
  const story = store.nodes.find((node) => node.id === storyId);
  const storyData = story?.data ?? {};
  const cloneRef = (value: unknown) => value && typeof value === 'object'
    ? { ...(value as Record<string, unknown>) }
    : null;
  const seedRef = cloneRef(storyData.referenceBlueprintRef);
  const suggestions = suggestBlueprintRules(storyData);
  const defaults = seedRef ? { narrativeRole: '', preserveRule: '', replaceRule: '' } : suggestions;
  const upgrades = upgradeBlueprintRuleDraft(storyData, suggestions, legacySuggestBlueprintRules(storyData), Boolean(seedRef));
  const childId = store.addNode(CANVAS_NODE_TYPES.script, store.findNodePosition(storyId, 420, 360), {
    remixRole: 'adaptation_script',
    blueprintNarrativeRole: upgrades.blueprintNarrativeRole ?? (typeof storyData.blueprintNarrativeRole === 'string' ? storyData.blueprintNarrativeRole : defaults.narrativeRole),
    blueprintPreserveRule: upgrades.blueprintPreserveRule ?? (typeof storyData.blueprintPreserveRule === 'string' ? storyData.blueprintPreserveRule : defaults.preserveRule),
    blueprintReplaceRule: upgrades.blueprintReplaceRule ?? (typeof storyData.blueprintReplaceRule === 'string' ? storyData.blueprintReplaceRule : defaults.replaceRule),
    referenceEvidenceRef: cloneRef(storyData.referenceEvidenceRef),
    sourceMediaPath: typeof storyData.sourceMediaPath === 'string' ? storyData.sourceMediaPath : null,
    remixBlueprintSeedRef: seedRef,
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
