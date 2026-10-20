import { beforeEach, describe, expect, it, vi } from 'vitest';
import { apiCall } from './client';
import { getReferenceDirectorState, confirmReferenceDecision, confirmReferenceSpeechCorrection, confirmReferenceMaterialReview, confirmReferenceOutputReview } from './referenceDirector';
vi.mock('./client', () => ({ apiCall: vi.fn() }));
describe('reference director API', () => {
  beforeEach(() => vi.clearAllMocks());
  it('reads state in the exact encoded project and canvas scope', async () => {
    const state = { commissions: [], decisions: [], material_work: [], speech_corrections: [] };
    vi.mocked(apiCall).mockResolvedValue(state);
    expect(await getReferenceDirectorState('project name', 'canvas/name')).toEqual(state);
    expect(apiCall).toHaveBeenCalledWith('projects/project%20name/freezone/canvases/canvas%2Fname/director/state');
  });
  it('confirms one saved decision with user credentials and no model-supplied receipt', async () => {
    vi.mocked(apiCall).mockResolvedValue({ commission: { phase: 'drafting_plan' } });
    await confirmReferenceDecision('project', 'canvas', 'decision');
    expect(apiCall).toHaveBeenCalledWith('projects/project/freezone/canvases/canvas/director/decisions/decision/confirm', { method: 'POST' });
  });
  it('applies a saved speech preview by artifact id, without posting replacement SRT', async () => {
    const saved = { plan: { id: 'plan', revision: 2 }, node_id: 'plan-node' };
    vi.mocked(apiCall).mockResolvedValue(saved);
    expect(await confirmReferenceSpeechCorrection('project', 'canvas', 'preview')).toEqual(saved);
    expect(apiCall).toHaveBeenCalledWith('projects/project/freezone/canvases/canvas/director/speech-corrections/preview/confirm', { method: 'POST' });
  });
});


it('confirms a material content review with the existing saved candidate identity', async () => {
  vi.clearAllMocks();
  vi.mocked(apiCall).mockResolvedValue({ review_ref: { artifact_id: 'review', revision: 2 } });
  await confirmReferenceMaterialReview('project', 'canvas', 'review');
  expect(apiCall).toHaveBeenCalledWith('projects/project/freezone/canvases/canvas/director/material-reviews/review/confirm', { method: 'POST' });
});


it('confirms a saved output review with explicit whole-media user evidence', async () => {
  vi.clearAllMocks();
  vi.mocked(apiCall).mockResolvedValue({ review_ref: { artifact_id: 'output', revision: 2 } });
  const ref = { artifact_id: 'output', revision: 1 };
  await confirmReferenceOutputReview('project', 'canvas', ref);
  expect(apiCall).toHaveBeenCalledWith('projects/project/freezone/canvases/canvas/director/output-review/confirm', { method: 'POST', json: { review_ref: ref, viewed_complete_media: true } });
});
