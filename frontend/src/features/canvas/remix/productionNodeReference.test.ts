import { describe, expect, it } from 'vitest';
import type { CanvasNode } from '@/features/canvas/domain/canvasNodes';
import {
  buildCanvasContextRequestResponses, buildCanvasNodeReferenceAttachment,
  buildCanvasNodeReferenceContext, extractCanvasContextRequestEnvelopes,
} from '@/features/freezone/chatNodeReferences';

const node: CanvasNode = {
  id: 'plan-node', type: 'scriptNode', position: { x: 0, y: 0 },
  data: {
    remixRole: 'production_plan', prompt: '', displayName: '作品方案',
    productionPlanV2Ref: { artifactId: 'saved-plan-123', revision: 7 },
    productionPlanV2Draft: {
      schema_version: 2, goal: '人工编辑后保留的目标',
      output: { width: 640, height: 360, fps_num: 24, fps_den: 1, background: '#000000' },
      beats: [], resources: [], placements: [], speech_takes: [], speech_cues: [], selections: [],
    },
    productionPlanV2Dirty: false, productionPlanV2Conflict: false,
  },
};
const expected = {
  remix_role: 'production_plan', schema: 'production_plan.v2',
  plan_ref: { artifact_id: 'saved-plan-123', revision: 7 },
  goal: '人工编辑后保留的目标',
  output: { width: 640, height: 360, fps_num: 24, fps_den: 1, background: '#000000' },
  dirty: false, conflict: false,
};

describe('native production plan references', () => {
  it('carries the exact saved identity in references and prompt context despite an empty generic prompt', () => {
    const attachment = buildCanvasNodeReferenceAttachment('p', 'c', [node])!;
    const payload = JSON.parse(attachment.content!);
    expect(payload.nodes[0].production_plan).toEqual(expected);
    expect(payload.display_nodes[0].production_plan).toEqual(expected);
    const context = buildCanvasNodeReferenceContext([attachment]);
    expect(context).toContain(`production_plan_json: ${JSON.stringify(expected)}`);
    expect(context).toContain('not the canvas node_id');
  });

  it.each([
    { productionPlanV2Dirty: true, productionPlanV2Conflict: false },
    { productionPlanV2Dirty: false, productionPlanV2Conflict: true },
  ])('exposes saved ref and pending local state through compact node detail', async (flags) => {
    const responses = await buildCanvasContextRequestResponses({
      project: 'p', canvasId: 'c', nodes: [{ ...node, data: { ...node.data, ...flags } }],
      edges: [], ontologyContext: null,
      envelopes: extractCanvasContextRequestEnvelopes([
        { schema_version: 'canvas_context_request.v1', requests: [{ type: 'node_detail', node_id: node.id }] },
      ]),
    });
    const detail = (responses![0].data as { nodes: Array<Record<string, unknown>> }).nodes[0];
    expect(detail.production_plan).toEqual({ ...expected,
      dirty: flags.productionPlanV2Dirty, conflict: flags.productionPlanV2Conflict });
    expect(detail.parameters ?? {}).not.toHaveProperty('productionPlanV2Ref');
    expect(detail.parameters ?? {}).not.toHaveProperty('productionPlanV2Draft');
  });

  it.each([undefined, { artifactId: '', revision: 1 }, { artifactId: 'saved-plan-123', revision: 0 }])(
    'does not invent an artifact identity for an unsaved or invalid native plan', (ref) => {
      const attachment = buildCanvasNodeReferenceAttachment('p', 'c', [
        { ...node, data: { ...node.data, productionPlanV2Ref: ref } },
      ])!;
      const metadata = JSON.parse(attachment.content!).nodes[0].production_plan;
      expect(metadata).toMatchObject({ remix_role: 'production_plan', schema: null, plan_ref: null });
      expect(JSON.stringify(metadata)).not.toContain('plan-node');
    },
  );

  it('bounds author summaries and never forwards whole drafts, word evidence or private fields', () => {
    const attachment = buildCanvasNodeReferenceAttachment('p', 'c', [{ ...node, data: { ...node.data,
      productionPlanV2Draft: {
        schema_version: 2, goal: '目标'.repeat(5_000),
        output: { ...expected.output, private_key: 'secret-output' },
        words: [{ text: 'private-word-evidence' }],
        private_key: 'secret-draft',
      },
    } }])!;
    const metadata = JSON.parse(attachment.content!).nodes[0].production_plan;
    expect(metadata.goal.length).toBeLessThanOrEqual(243);
    expect(JSON.stringify(metadata).length).toBeLessThan(700);
    expect(metadata.output).toEqual(expected.output);
    expect(JSON.stringify(metadata)).not.toMatch(/secret|words|private/);
  });

  it('preserves generic script references without attaching native plan metadata', () => {
    const attachment = buildCanvasNodeReferenceAttachment('p', 'c', [{
      ...node, data: { prompt: '普通脚本输入', displayName: '脚本' },
    }])!;
    const item = JSON.parse(attachment.content!).nodes[0];
    expect(item).not.toHaveProperty('production_plan');
    expect(item.text_content).toBe('普通脚本输入');
    expect(buildCanvasNodeReferenceContext([attachment])).not.toContain('_production_plan_json:');
  });
});
