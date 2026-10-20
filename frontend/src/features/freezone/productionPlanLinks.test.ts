import { beforeEach, expect, it } from 'vitest';
import { useCanvasStore } from '@/stores/canvasStore';
import { applyCanvasChatCommands, type CanvasChatCommandEnvelope } from './canvasChatCommands';
import { getCanvasNodePrimaryOutputRole } from './canvasNodeSemantics';

beforeEach(() => useCanvasStore.setState({ nodes: [], edges: [] }));

it('creates the native reference analysis to plan to compose graph in one approved batch', () => {
  const envelope: CanvasChatCommandEnvelope = { schema_version: 'canvas_chat_commands.v1', commands: [
    { type: 'create_node', client_id: 'reference', node_type: 'videoStoryNode' },
    { type: 'create_node', client_id: 'plan', node_type: 'scriptNode', data: { remixRole: 'production_plan' } },
    { type: 'create_node', client_id: 'compose', node_type: 'videoComposeNode' },
    { type: 'create_edge', source: 'reference', target: 'plan', link_type: 'derived_from' },
    { type: 'create_edge', source: 'plan', target: 'compose', link_type: 'composition_input_for' },
  ] };
  const result = applyCanvasChatCommands([envelope]);
  expect(result.errors).toEqual([]);
  expect(useCanvasStore.getState().edges).toHaveLength(2);
  const plan = useCanvasStore.getState().nodes.find((node) => node.type === 'scriptNode')!;
  expect(getCanvasNodePrimaryOutputRole({ nodeType: plan.type, data: plan.data })).toBe('production_plan');
});

it('continues rejecting an ordinary script as composition material', () => {
  const result = applyCanvasChatCommands([{ schema_version: 'canvas_chat_commands.v1', commands: [
    { type: 'create_node', client_id: 'script', node_type: 'scriptNode' },
    { type: 'create_node', client_id: 'compose', node_type: 'videoComposeNode' },
    { type: 'create_edge', source: 'script', target: 'compose', link_type: 'composition_input_for' },
  ] }]);
  expect(result.errors.length).toBeGreaterThan(0);
  expect(useCanvasStore.getState().nodes).toHaveLength(0);
});
