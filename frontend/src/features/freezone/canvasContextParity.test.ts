import { describe, expect, it } from 'vitest';
import fixture from '../../../../tests/fixtures/canvas-context-read.json';
import { buildCanvasContextRequestResponses, extractCanvasContextRequestEnvelopes } from './chatNodeReferences';
import type { CanvasNode, CanvasEdge } from '@/features/canvas/domain/canvasNodes';

async function responses(nodes = fixture.canvas.nodes as CanvasNode[], requests = Object.keys(fixture.parity).map(node_id => ({ type: 'node_detail', node_id }))) {
  return buildCanvasContextRequestResponses({ project: 'p', canvasId: 'c', nodes, edges: fixture.canvas.edges as CanvasEdge[], ontologyContext: null,
    envelopes: extractCanvasContextRequestEnvelopes([{ schema_version: 'canvas_context_request.v1', requests }]) });
}

describe('saved canvas semantic projection parity', () => {
  it('compares IDs, types and exact native analysis/plan references field by field', async () => {
    const result = await responses();
    for (const response of result ?? []) {
      const expected = fixture.parity[response.node_id as keyof typeof fixture.parity];
      const node = (response.data as { nodes: Record<string, unknown>[] }).nodes[0];
      for (const [field, value] of Object.entries(expected)) expect(node[field], field).toEqual(value);
    }
  });
  it('reports deleted/missing node as null rather than an empty analysis', async () => {
    const result = await responses([], [{ type: 'node_detail', node_id: 'analysis' }]);
    expect(result?.[0].data).toBeNull();
  });
  it('preserves bound upstream media and action semantics in the browser projection', async () => {
    const result = await responses();
    const plan = (result?.find(r => r.node_id === 'plan')?.data as { nodes: Record<string, unknown>[] }).nodes[0];
    expect(plan.reference_media).toMatchObject([{ node_id: 'video', media_type: 'video', source_url: '/source.mp4', mention: '@视频1' }]);
    expect(plan.action_summary).toBeDefined();
    expect(fixture.coverage.excluded).toContain('dynamic_action_catalog');
  });
});
