import { beforeEach, describe, expect, it } from 'vitest';
import { useCanvasStore } from '@/stores/canvasStore';
import { CANVAS_NODE_TYPES } from '@/features/canvas/domain/canvasNodes';

describe('production plan node input version', () => {
  beforeEach(() => {
    useCanvasStore.getState().setCanvasData([{
      id: 'plan', type: CANVAS_NODE_TYPES.script,
      position: { x: 0, y: 0 },
      data: {
        remixRole: 'production_plan', remixNodeRevision: 1,
        processingPayload: { topic: '黑咖啡', requirements: { dialogue: { mode: 'preserve_source' } },
          requirements_fingerprint: 'old', requirements_provenance: { kind: 'direction' } },
      },
    }] as never, []);
  });

  it('keeps the topic but versions and unbinds withdrawn constraints', () => {
    useCanvasStore.getState().updateNodeData('plan', {
      processingPayload: { topic: '黑咖啡', requirements: { dialogue: { mode: 'rewrite' } } },
    } as never);
    const data = useCanvasStore.getState().nodes[0].data as Record<string, unknown>;
    const payload = data.processingPayload as Record<string, unknown>;
    expect(payload.topic).toBe('黑咖啡');
    expect(payload.requirements).toEqual({ dialogue: { mode: 'rewrite' } });
    expect(payload.requirements_fingerprint).toBeUndefined();
    expect(payload.requirements_provenance).toBeUndefined();
    expect(data.remixDirectionEdited).toBe(true);
    expect(data.remixNodeRevision).toBe(2);
  });
});
