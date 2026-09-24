import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  nodes: [{ id: 'script', type: 'scriptNode', data: {
    remixTopic: '薯条',
    referenceBlueprintRef: { artifactId: 'blueprint-1', revision: 2 },
    nodeRevision: 4,
  } as Record<string, unknown> }],
  edges: [],
  updateNodeData: vi.fn((id: string, patch: Record<string, unknown>) => {
    const node = state.nodes.find((candidate) => candidate.id === id);
    if (node) node.data = { ...node.data, ...patch };
  }),
}));

vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: { getState: () => state } }));
vi.mock('@/lib/url-params', () => ({ readUrl: () => ({ project: 'demo' }) }));
vi.mock('@/api/remix', () => ({
  submitRemixProcessAction: vi.fn(),
  fetchRemixOperation: vi.fn(),
  fetchRemixTaskStatus: vi.fn(),
  fetchRemixArtifact: vi.fn(),
}));

import { fetchRemixArtifact, fetchRemixOperation, submitRemixProcessAction } from '@/api/remix';
import { resumeScriptDraftGeneration, startScriptDraftGeneration } from './scriptDraftTask';

describe('script draft generation task controller', () => {
  beforeEach(() => {
    state.nodes[0].data = {
      remixTopic: '薯条',
      referenceBlueprintRef: { artifactId: 'blueprint-1', revision: 2 },
      nodeRevision: 4,
    };
    vi.clearAllMocks();
    vi.mocked(submitRemixProcessAction).mockResolvedValue({
      operation_id: 'operation-1', task_id: 'task-1', task_key: 'key', task_type: 'remix_generate_script_draft',
    });
    vi.mocked(fetchRemixOperation).mockResolvedValue({
      operation_id: 'operation-1', status: 'needs_confirmation', task_id: 'task-1',
      input_refs: [{ artifact_id: 'blueprint-1', revision: 2 }],
      output_refs: [{ artifact_id: 'draft-1', revision: 1 }],
    } as never);
    vi.mocked(fetchRemixArtifact).mockResolvedValue({
      id: 'draft-1', revision: 1, schema: 'remix_script.v1',
      upstream_refs: [{ artifact_id: 'blueprint-1', revision: 2 }],
      payload: {
        dialogue: '女：薯条。', segments: [{ segment_id: 'segment-1', start_char: 2, end_char: 5, speaker: '女', source: 'speaking_video' }], blueprint_mappings: [], media_requirements: [],
        selections: [{ id: 'generated-1', label: '产品亮相', start: 2, end: 4 }],
      },
    });
  });

  it('queues the generation and applies the completed editable draft', async () => {
    const result = await startScriptDraftGeneration('script', { artifactId: 'blueprint-1', revision: 2 }, '薯条');

    expect(submitRemixProcessAction).toHaveBeenCalledWith('demo', expect.objectContaining({
      action: 'generate_script_draft',
      blueprintRef: { artifactId: 'blueprint-1', revision: 2 },
      newTopic: '薯条',
      nodeRevision: 5,
    }));
    expect(result).toEqual({
      dialogue: '女：薯条。', selections: [{ id: 'generated-1', label: '产品亮相', start: 2, end: 4 }],
      segments: [{ id: 'segment-1', start: 2, end: 5, speaker: '女', source: 'speaking_video' }],
    });
    expect(state.nodes[0].data).toMatchObject({
      remixScriptGenerationOperationId: null,
      remixScriptGenerationStatus: 'completed',
      remixScriptDraft: result,
      remixScriptRef: { artifactId: 'draft-1', revision: 1 },
      remixScriptLastSavedRef: { artifactId: 'draft-1', revision: 1 },
    });
  });

  it('preserves an undecided media mode from a generated script draft', async () => {
    vi.mocked(fetchRemixArtifact).mockResolvedValue({
      id: 'draft-1', revision: 1, schema: 'remix_script.v1',
      upstream_refs: [{ artifact_id: 'blueprint-1', revision: 2 }],
      payload: {
        dialogue: '你每天喝什么？', segments: [], blueprint_mappings: [], media_requirements: [],
        selections: [{ id: 'generated-1', label: '提问', start: 0, end: 8 }],
      },
    });

    const result = await startScriptDraftGeneration('script', { artifactId: 'blueprint-1', revision: 2 }, '薯条');

    expect(result?.segments).toEqual([]);
  });

  it('resumes after reload without resubmitting', async () => {
    state.nodes[0].data = {
      ...state.nodes[0].data,
      remixScriptGenerationOperationId: 'operation-1',
      remixScriptGenerationBlueprintRef: { artifactId: 'blueprint-1', revision: 2 },
      remixScriptGenerationTopic: '薯条',
      remixScriptGenerationStatus: 'running',
    };

    await resumeScriptDraftGeneration('script');

    expect(submitRemixProcessAction).not.toHaveBeenCalled();
    expect(fetchRemixOperation).toHaveBeenCalledWith('demo', 'operation-1');
    expect(state.nodes[0].data.remixScriptDraft).toEqual(expect.objectContaining({ dialogue: '女：薯条。' }));
    expect(state.nodes[0].data.remixScriptRef).toEqual({ artifactId: 'draft-1', revision: 1 });
  });

  it('does not apply an old result after the topic changes', async () => {
    state.nodes[0].data = {
      ...state.nodes[0].data,
      remixTopic: '咖啡',
      remixScriptGenerationOperationId: 'operation-1',
      remixScriptGenerationBlueprintRef: { artifactId: 'blueprint-1', revision: 2 },
      remixScriptGenerationTopic: '薯条',
      remixScriptGenerationStatus: 'running',
    };

    await resumeScriptDraftGeneration('script');

    expect(fetchRemixArtifact).not.toHaveBeenCalled();
    expect(state.nodes[0].data).toMatchObject({
      remixScriptGenerationOperationId: null,
      remixScriptGenerationStatus: 'failed',
    });
  });
});
