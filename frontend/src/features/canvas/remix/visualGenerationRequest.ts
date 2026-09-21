// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import {
  CANVAS_NODE_TYPES,
  isImageGenNode,
  type CanvasEdge,
  type CanvasNode,
} from '@/features/canvas/domain/canvasNodes';

const VISUAL_GENERATION_REQUEST_FIELDS = new Set([
  'prompt',
  'model',
  'styleTemplateId',
  'aspectRatio',
  'requestAspectRatio',
  'size',
  'quality',
  'modelParams',
  'cameraSelection',
  'referenceImageUrl',
]);

export function changesVisualGenerationRequest(
  current: Record<string, unknown>,
  patch: Record<string, unknown>,
): boolean {
  if (Object.prototype.hasOwnProperty.call(patch, 'remixVisualRequestRevision')) return false;
  return Object.entries(patch).some(([key, value]) =>
    VISUAL_GENERATION_REQUEST_FIELDS.has(key) && !Object.is(current[key], value));
}

/**
 * Build the durable node patches for one image-generation input mutation.
 * Kept store-independent so toolbar/forms and generic graph edge mutations use
 * the same revision and downstream invalidation contract.
 */
export function visualGenerationRequestMutationPatches(
  nodes: readonly CanvasNode[],
  edges: readonly CanvasEdge[],
  sourceId: string,
  patch: Record<string, unknown>,
): Map<string, Record<string, unknown>> {
  const source = nodes.find((node) => node.id === sourceId);
  if (!source || !isImageGenNode(source)) return new Map();
  const revision = typeof source.data.remixVisualRequestRevision === 'number'
    ? source.data.remixVisualRequestRevision + 1 : 1;
  const patches = new Map<string, Record<string, unknown>>([[sourceId, {
    ...patch,
    remixVisualRequestRevision: revision,
    remixVisualOutputCurrent: false,
    isGenerating: false,
    generationStartedAt: null,
    generationTaskKey: null,
    generationTaskType: null,
    generationTaskJobId: null,
  }]]);
  const reachable = new Set<string>([sourceId]);
  const queue = [sourceId];
  while (queue.length) {
    const current = queue.shift()!;
    for (const edge of edges) {
      if (edge.source !== current || reachable.has(edge.target)) continue;
      reachable.add(edge.target);
      queue.push(edge.target);
    }
  }
  for (const node of nodes) {
    if (reachable.has(node.id) && node.type === CANVAS_NODE_TYPES.videoCompose) {
      patches.set(node.id, {
        remixInputsCurrent: false,
        remixOperationId: null,
        resultVideoUrl: null,
      });
    }
  }
  return patches;
}

export function applyVisualGenerationRequestPatches(
  nodes: readonly CanvasNode[],
  patches: ReadonlyMap<string, Record<string, unknown>>,
): CanvasNode[] {
  if (patches.size === 0) return [...nodes];
  return nodes.map((node) => {
    const patch = patches.get(node.id);
    return patch ? { ...node, data: { ...node.data, ...patch } } as CanvasNode : node;
  });
}
