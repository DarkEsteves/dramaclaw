import type { BrollVisualAttachment } from './brollCanvasBinding';
import type { RemixScriptDraft } from './remixScriptModel';

/** Fill immutable media revisions after pending canvas visuals finish generating or uploading. */
export async function resolvePendingBrollDraft(
  draft: RemixScriptDraft,
  readyVisualNodeIds: string[],
  resolveVisual: (nodeId: string) => Promise<BrollVisualAttachment>,
): Promise<RemixScriptDraft | null> {
  const pendingIds = new Set(draft.selections
    .filter((selection) => selection.visualNodeId && !selection.visualMediaRevision && readyVisualNodeIds.includes(selection.visualNodeId))
    .map((selection) => selection.visualNodeId!));
  if (pendingIds.size === 0) return null;

  const revisions = new Map<string, string>();
  for (const nodeId of pendingIds) {
    const attachment = await resolveVisual(nodeId);
    attachment.commit();
    revisions.set(nodeId, attachment.mediaRevision);
  }
  return {
    ...draft,
    selections: draft.selections.map((selection) => {
      const revision = selection.visualNodeId ? revisions.get(selection.visualNodeId) : undefined;
      return revision ? { ...selection, visualMediaRevision: revision } : selection;
    }),
  };
}
