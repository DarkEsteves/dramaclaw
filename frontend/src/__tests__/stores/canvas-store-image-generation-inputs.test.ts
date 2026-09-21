// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { beforeEach, describe, expect, it } from 'vitest';

import { CANVAS_NODE_TYPES, type CanvasNode } from '@/features/canvas/domain/canvasNodes';
import { useCanvasStore } from '@/stores/canvasStore';

function node(id: string, type: CanvasNode['type'], data: CanvasNode['data'] = {}): CanvasNode {
  return { id, type, position: { x: 0, y: 0 }, data } as CanvasNode;
}

describe('canvasStore image generation input edges', () => {
  beforeEach(() => useCanvasStore.getState().setCanvasData([
    node('reference', CANVAS_NODE_TYPES.upload, { imageUrl: '/reference.png' }),
    node('image', CANVAS_NODE_TYPES.imageGen, {
      prompt: 'old prompt',
      remixVisualRequestRevision: 4,
      remixVisualOutputCurrent: true,
      isGenerating: true,
      generationTaskKey: 'old-task',
    }),
    node('compose', CANVAS_NODE_TYPES.videoCompose, {
      remixInputsCurrent: true,
      remixOperationId: 'old-operation',
      resultVideoUrl: '/old.mp4',
    }),
  ], [{ id: 'e-image-compose', source: 'image', target: 'compose' }]));

  it('adding and removing a generic upstream edge durably invalidates the image request', () => {
    const store = useCanvasStore.getState();
    const edgeId = store.addEdge('reference', 'image');
    expect(edgeId).toBe('e-reference-image');
    expect(useCanvasStore.getState().nodes.find((item) => item.id === 'image')?.data).toMatchObject({
      remixVisualRequestRevision: 5,
      remixVisualOutputCurrent: false,
      isGenerating: false,
      generationTaskKey: null,
    });
    expect(useCanvasStore.getState().nodes.find((item) => item.id === 'compose')?.data).toMatchObject({
      remixInputsCurrent: false,
      remixOperationId: null,
      resultVideoUrl: null,
    });

    useCanvasStore.getState().deleteEdge(edgeId!);
    expect(useCanvasStore.getState().nodes.find((item) => item.id === 'image')?.data.remixVisualRequestRevision).toBe(6);
  });

  it('manual connect and React Flow edge removal use the same durable invalidation', () => {
    useCanvasStore.getState().onConnect({ source: 'reference', target: 'image', sourceHandle: null, targetHandle: null });
    expect(useCanvasStore.getState().nodes.find((item) => item.id === 'image')?.data.remixVisualRequestRevision).toBe(5);
    const edgeId = useCanvasStore.getState().edges.find((edge) => edge.source === 'reference' && edge.target === 'image')!.id;
    useCanvasStore.getState().onEdgesChange([{ id: edgeId, type: 'remove' }]);
    expect(useCanvasStore.getState().nodes.find((item) => item.id === 'image')?.data.remixVisualRequestRevision).toBe(6);
  });

  it('generic request-field edits bump revision while result and status patches do not', () => {
    useCanvasStore.getState().updateNodeData('image', {
      generationError: 'provider failed',
      isGenerating: false,
      imageUrl: '/result.png',
    });
    expect(useCanvasStore.getState().nodes.find((item) => item.id === 'image')?.data.remixVisualRequestRevision).toBe(4);

    useCanvasStore.getState().updateNodeData('image', { model: 'new-model' });
    expect(useCanvasStore.getState().nodes.find((item) => item.id === 'image')?.data).toMatchObject({
      model: 'new-model',
      remixVisualRequestRevision: 5,
      remixVisualOutputCurrent: false,
      generationTaskKey: null,
    });
  });
});
