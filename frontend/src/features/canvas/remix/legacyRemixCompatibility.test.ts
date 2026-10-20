import { beforeEach, describe, expect, it } from 'vitest';

import type { CanvasEdge, CanvasNode } from '@/features/canvas/domain/canvasNodes';
import { useCanvasStore } from '@/stores/canvasStore';
import fixture from './fixtures/legacy-remix-canvas.json';

describe('legacy remix graph compatibility', () => {
  beforeEach(() => useCanvasStore.getState().setCanvasData([], []));

  it('hydrates existing transcription, arrangement and direct plan branches without dropping nodes or edges', () => {
    useCanvasStore.getState().hydrateCanvasDraft({
      nodes: fixture.nodes as CanvasNode[],
      edges: fixture.edges as CanvasEdge[],
      mutation: { userEditsSinceHydrate: 0, lastMutationSource: null, pendingClearIntent: false },
    });
    const state = useCanvasStore.getState();
    expect(state.nodes.map((node) => node.id)).toEqual(expect.arrayContaining(fixture.nodes.map((node) => node.id)));
    expect(state.edges.map((edge) => edge.id)).toEqual(expect.arrayContaining(fixture.edges.map((edge) => edge.id)));
    expect(state.nodes.find((node) => node.id === 'words')?.data.artifactRef).toEqual({ artifactId: 'word-timeline', revision: 1 });
    expect(state.nodes.find((node) => node.id === 'timeline')?.data.artifactRef).toEqual({ artifactId: 'timing', revision: 3 });
    expect(state.nodes.find((node) => node.id === 'legacy-arrangement')?.data.productionPlanNodeId).toBe('legacy-plan');
    expect(state.edges.some((edge) => edge.source === 'legacy-arrangement' && edge.target === 'legacy-plan-compose')).toBe(true);
    expect(state.nodes.find((node) => node.id === 'plan')?.data.productionPlanV2Ref).toEqual({ artifactId: 'plan', revision: 4 });
  });
});
