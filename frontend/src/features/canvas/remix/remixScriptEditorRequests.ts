type OpenEditor = () => void;

const pendingNodeIds = new Set<string>();
const listeners = new Map<string, Set<OpenEditor>>();

/**
 * Requests the remix editor without writing transient UI state into canvas data.
 * A newly-created node consumes the pending request when it mounts.
 */
export function requestRemixScriptEditor(nodeId: string): void {
  const handlers = listeners.get(nodeId);
  if (!handlers?.size) {
    pendingNodeIds.add(nodeId);
    return;
  }
  pendingNodeIds.delete(nodeId);
  handlers.forEach((handler) => handler());
}

export function consumeRemixScriptEditorRequest(nodeId: string): boolean {
  if (!pendingNodeIds.has(nodeId)) return false;
  pendingNodeIds.delete(nodeId);
  return true;
}

export function subscribeRemixScriptEditorRequest(nodeId: string, handler: OpenEditor): () => void {
  const handlers = listeners.get(nodeId) ?? new Set<OpenEditor>();
  handlers.add(handler);
  listeners.set(nodeId, handlers);
  return () => {
    const current = listeners.get(nodeId);
    current?.delete(handler);
    if (current?.size === 0) listeners.delete(nodeId);
  };
}
