import { describe, expect, it } from 'vitest';
import { buildCanvasContextRequestResponses, extractCanvasContextRequestEnvelopes } from './chatNodeReferences';
import type { CanvasNode } from '@/features/canvas/domain/canvasNodes';

async function details(data: Record<string, unknown>) {
  const nodes = [{ id: 'analysis', type: 'videoStoryNode', position: { x: 0, y: 0 }, data }] as CanvasNode[];
  const result = await buildCanvasContextRequestResponses({ project: 'p', canvasId: 'c', nodes, edges: [], ontologyContext: null,
    envelopes: extractCanvasContextRequestEnvelopes([{ schema_version: 'canvas_context_request.v1', requests: [{ type: 'node_detail', node_id: 'analysis' }] }]) });
  return (result?.[0].data as { nodes: Array<Record<string, unknown>> }).nodes[0];
}

describe('read-only reference analysis context', () => {
  it('exposes completed result once without treating it as editable node parameters', async () => {
    const node = await details({ sourceVideoUrl: '/video.mp4', isAnalyzing: false, rawResult: { video_story: { summary: '提问再反转' } },
      referenceAnalysisRunId: 'run' });
    expect(node.reference_analysis).toMatchObject({ status: 'completed', analysis_node_id: 'analysis', summary: '提问再反转', run_id: 'run' });
    expect((node.reference_analysis as Record<string, unknown>).next_action).toBe('freezone_get_production_progress');
    expect(node.parameters).not.toHaveProperty('rawResult');
  });
  it('reports running analysis with event waiting guidance', async () => {
    const node = await details({ isAnalyzing: true, rawResult: null });
    expect(node.reference_analysis).toMatchObject({ status: 'running' });
    expect((node.reference_analysis as Record<string, unknown>).next_action).toBe('await_completion_event');
  });
});
