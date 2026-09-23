import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  nodes: [{
    id: 'script',
    data: {
      remixScriptRef: { artifactId: 'script-1', revision: 1 },
      nodeRevision: 4,
      remixBrollPlanStatus: null,
    } as Record<string, unknown>,
  }],
  updateNodeData: vi.fn((id: string, patch: Record<string, unknown>) => {
    const node = state.nodes.find((candidate) => candidate.id === id);
    if (node) node.data = { ...node.data, ...patch };
  }),
}));

vi.mock('@/stores/canvasStore', () => ({
  useCanvasStore: { getState: () => state },
}));
vi.mock('@/lib/url-params', () => ({ readUrl: () => ({ project: 'demo' }) }));
vi.mock('@/api/remix', () => ({
  submitRemixProcessAction: vi.fn(),
  fetchRemixOperation: vi.fn(),
  fetchRemixTaskStatus: vi.fn(),
  fetchRemixArtifact: vi.fn(),
}));

import { fetchRemixArtifact, fetchRemixOperation, fetchRemixTaskStatus, submitRemixProcessAction } from '@/api/remix';
import { resumeBrollPlan, startBrollPlan } from './brollPlanTask';

const completedOperation = {
  operation_id: 'operation-1',
  status: 'needs_confirmation',
  task_id: 'task-1',
  input_refs: [{ artifact_id: 'script-1', revision: 1 }],
  output_refs: [{ artifact_id: 'plan-1', revision: 1 }],
};

describe('B-roll planning task controller', () => {
  beforeEach(() => {
    state.nodes[0].data = {
      remixScriptRef: { artifactId: 'script-1', revision: 1 },
      nodeRevision: 4,
      remixBrollPlanStatus: null,
    };
    state.updateNodeData.mockClear();
    vi.clearAllMocks();
    vi.mocked(submitRemixProcessAction).mockResolvedValue({
      operation_id: 'operation-1', task_id: 'task-1', task_key: 'key', task_type: 'remix_plan_broll',
    });
    vi.mocked(fetchRemixOperation).mockResolvedValue(completedOperation as never);
    vi.mocked(fetchRemixArtifact).mockResolvedValue({
      id: 'plan-1', revision: 1, schema: 'remix_script.v1',
      upstream_refs: [{ artifact_id: 'script-1', revision: 1 }],
      payload: { media_requirements: [{ event_id: 'product', required: true, purpose: '展示产品', suggested_query: '产品特写' }] },
    });
  });

  it('submits a durable action and applies its completed artifact', async () => {
    await startBrollPlan('script');

    expect(submitRemixProcessAction).toHaveBeenCalledWith('demo', {
      action: 'plan_broll',
      scriptRef: { artifactId: 'script-1', revision: 1 },
      idempotencyKey: 'broll:script:script-1:1:5',
      nodeRevision: 5,
    });
    expect(state.updateNodeData).toHaveBeenCalledWith('script', expect.objectContaining({
      remixBrollOperationId: 'operation-1',
      remixBrollOperationScriptRef: { artifactId: 'script-1', revision: 1 },
      remixBrollPlanStatus: 'running',
    }));
    expect(state.nodes[0].data).toMatchObject({
      remixBrollOperationId: null,
      remixBrollPlanRef: { artifactId: 'plan-1', revision: 1 },
      remixBrollScriptRef: { artifactId: 'script-1', revision: 1 },
      remixBrollPlanStatus: 'completed',
    });
  });

  it('uses a fresh idempotency key when retrying the same script revision', async () => {
    vi.mocked(fetchRemixOperation).mockResolvedValueOnce({
      operation_id: 'operation-1', status: 'failed', input_refs: [{ artifact_id: 'script-1', revision: 1 }], output_refs: [],
    } as never);

    await startBrollPlan('script');

    vi.mocked(submitRemixProcessAction).mockResolvedValueOnce({
      operation_id: 'operation-2', task_id: 'task-2', task_key: 'key-2', task_type: 'remix_plan_broll',
    });
    vi.mocked(fetchRemixOperation).mockResolvedValueOnce({ ...completedOperation, operation_id: 'operation-2' } as never);
    await startBrollPlan('script');

    expect(vi.mocked(submitRemixProcessAction).mock.calls.map(([, request]) => request.idempotencyKey)).toEqual([
      'broll:script:script-1:1:5',
      'broll:script:script-1:1:6',
    ]);
  });

  it('deduplicates repeated clicks while the first submission is pending', async () => {
    let release: ((value: { operation_id: string; task_id: string; task_key: string; task_type: string }) => void) | undefined;
    vi.mocked(submitRemixProcessAction).mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }));

    const first = startBrollPlan('script');
    const second = startBrollPlan('script');

    expect(submitRemixProcessAction).toHaveBeenCalledOnce();
    release?.({ operation_id: 'operation-1', task_id: 'task-1', task_key: 'key', task_type: 'remix_plan_broll' });
    await Promise.all([first, second]);
    expect(state.nodes[0].data.remixBrollPlanStatus).toBe('completed');
  });

  it('resumes a persisted operation after remount', async () => {
    state.nodes[0].data = {
      ...state.nodes[0].data,
      remixBrollOperationId: 'operation-1',
      remixBrollOperationScriptRef: { artifactId: 'script-1', revision: 1 },
      remixBrollPlanStatus: 'running',
    };

    await resumeBrollPlan('script');

    expect(submitRemixProcessAction).not.toHaveBeenCalled();
    expect(fetchRemixOperation).toHaveBeenCalledWith('demo', 'operation-1');
    expect(state.nodes[0].data.remixBrollPlanRef).toEqual({ artifactId: 'plan-1', revision: 1 });
  });

  it('turns a worker failure into a retryable node state', async () => {
    state.nodes[0].data = {
      ...state.nodes[0].data,
      remixBrollOperationId: 'operation-1',
      remixBrollOperationScriptRef: { artifactId: 'script-1', revision: 1 },
      remixBrollPlanStatus: 'running',
    };
    vi.mocked(fetchRemixOperation).mockResolvedValue({
      operation_id: 'operation-1', status: 'pending', task_id: 'task-1',
      task_type: 'remix_plan_broll', task_scope: 'operation-1', output_refs: [],
      input_refs: [{ artifact_id: 'script-1', revision: 1 }],
    } as never);
    vi.mocked(fetchRemixTaskStatus).mockResolvedValue({ status: 'failed', error: '模型服务不可用' });

    await resumeBrollPlan('script');

    expect(state.nodes[0].data).toMatchObject({
      remixBrollOperationId: null,
      remixBrollPlanStatus: 'failed',
      remixBrollPlanError: '模型服务不可用',
    });
  });

  it('never applies an older operation to a newer saved script', async () => {
    state.nodes[0].data = {
      ...state.nodes[0].data,
      remixScriptRef: { artifactId: 'script-1', revision: 2 },
      remixBrollOperationId: 'operation-1',
      remixBrollOperationScriptRef: { artifactId: 'script-1', revision: 1 },
      remixBrollPlanStatus: 'running',
    };

    await resumeBrollPlan('script');

    expect(fetchRemixArtifact).not.toHaveBeenCalled();
    expect(state.nodes[0].data.remixBrollPlanRef).toBeUndefined();
    expect(state.nodes[0].data).toMatchObject({
      remixBrollOperationId: null,
      remixBrollPlanStatus: 'failed',
    });
  });
});
