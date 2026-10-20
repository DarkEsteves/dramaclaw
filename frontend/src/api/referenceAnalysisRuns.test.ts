import { beforeEach, describe, expect, it, vi } from 'vitest';
import { apiCall } from './client';
import { listReferenceAnalysisRuns, retryReferenceAnalysisRun } from './referenceAnalysisRuns';
vi.mock('./client', () => ({ apiCall: vi.fn() }));

describe('reference analysis API', () => {
  beforeEach(() => vi.clearAllMocks());
  it('passes the active agent scope when listing persisted runs', async () => {
    vi.mocked(apiCall).mockResolvedValue({ runs: [{ run_id: 'run' }] });
    expect(await listReferenceAnalysisRuns('project', 'canvas', 'agent-2')).toEqual([{ run_id: 'run' }]);
    expect(apiCall).toHaveBeenCalledWith('projects/project/freezone/canvases/canvas/reference-analysis-runs?agent_id=agent-2');
  });
  it('uses the unwrapped retry run returned by the existing API client', async () => {
    const run = { run_id: 'run', status: 'resuming' };
    vi.mocked(apiCall).mockResolvedValue(run);
    expect(await retryReferenceAnalysisRun('project', 'canvas', 'run')).toEqual(run);
    expect(apiCall).toHaveBeenCalledWith('projects/project/freezone/canvases/canvas/reference-analysis-runs/run/retry', { method: 'POST' });
  });
});
