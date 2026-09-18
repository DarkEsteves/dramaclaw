import type { CanvasNode, CanvasEdge } from '@/features/canvas/domain/canvasNodes';

export interface CanvasBaseline { nodeSignatures: Map<string, string>; edgeSignatures: Map<string, string> }
export function captureCanvasBaseline(nodes: CanvasNode[], edges: CanvasEdge[]): CanvasBaseline {
  return {
    nodeSignatures: new Map(nodes.map((node) => [node.id, JSON.stringify(node)])),
    edgeSignatures: new Map(edges.map((edge) => [edge.id, JSON.stringify(edge)])),
  };
}

export function mergeConfirmedCanvasWithLocalChanges(remoteNodes: CanvasNode[], remoteEdges: CanvasEdge[], localNodes: CanvasNode[], localEdges: CanvasEdge[], baseline: CanvasBaseline, targetNodeId: string) {
  const localById = new Map(localNodes.map((node) => [node.id, node]));
  const remoteById = new Map(remoteNodes.map((node) => [node.id, node]));
  const nodes = remoteNodes.flatMap((remote) => {
    const local = localById.get(remote.id);
    if (!local && baseline.nodeSignatures.has(remote.id)) return [];
    if (!local || JSON.stringify(local) === baseline.nodeSignatures.get(remote.id)) return [remote];
    if (remote.id !== targetNodeId) return [local];
    const localData = local.data as Record<string, unknown>;
    const remoteData = remote.data as Record<string, unknown>;
    if (JSON.stringify(localData.remixManifestRef) !== JSON.stringify(remoteData.remixManifestRef)
      || localData.remixNodeRevision !== remoteData.remixNodeRevision) return [local];
    return [{ ...local, data: {
      ...localData,
      resultVideoUrl: remoteData.resultVideoUrl,
      remixRenderOperationId: remoteData.remixRenderOperationId,
      remixRenderManifestRef: remoteData.remixRenderManifestRef,
    } as CanvasNode['data'] }];
  });
  nodes.push(...localNodes.filter((node) => !remoteById.has(node.id)
    && (!baseline.nodeSignatures.has(node.id) || JSON.stringify(node) !== baseline.nodeSignatures.get(node.id))));
  const localEdgesById = new Map(localEdges.map((edge) => [edge.id, edge]));
  const remoteEdgesById = new Map(remoteEdges.map((edge) => [edge.id, edge]));
  const edges = remoteEdges.flatMap((remote) => {
    const local = localEdgesById.get(remote.id);
    if (!local && baseline.edgeSignatures.has(remote.id)) return [];
    return [local && JSON.stringify(local) !== baseline.edgeSignatures.get(remote.id) ? local : remote];
  });
  edges.push(...localEdges.filter((edge) => !remoteEdgesById.has(edge.id)
    && (!baseline.edgeSignatures.has(edge.id) || JSON.stringify(edge) !== baseline.edgeSignatures.get(edge.id))));
  return { nodes, edges };
}
