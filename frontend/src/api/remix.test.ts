import { describe, expect, it, vi } from 'vitest';

vi.mock('./client', () => ({ apiCall: vi.fn(async (_path: string, _options?: unknown) => ({})) }));

import { apiCall } from './client';
import { createRemixArtifact, renderRemixManifest, submitRemixProcessAction } from './remix';

describe('remix API contract', () => {
  it('sends exact artifact refs and render revision to the backend routes', async () => {
    await createRemixArtifact('demo', 'remix_script.v1', { dialogue: 'hi' }, [{ artifactId: 'source', revision: 2 }]);
    expect(apiCall).toHaveBeenCalledWith('projects/demo/freezone/remix/artifacts', {
      method: 'POST',
      json: { schema: 'remix_script.v1', payload: { dialogue: 'hi' }, upstream_refs: [{ artifact_id: 'source', revision: 2 }] },
    });

    await renderRemixManifest('demo', { manifestRef: { artifactId: 'manifest', revision: 4 }, idempotencyKey: 'once', nodeRevision: 3 });
    expect(apiCall).toHaveBeenCalledWith('projects/demo/freezone/remix/artifacts/manifest/render', {
      method: 'POST', json: { revision: 4, idempotency_key: 'once', node_revision: 3 },
    });

    await submitRemixProcessAction('demo', {
      action: 'align_script', mediaPath: 'media/voice.wav', scriptRef: { artifactId: 'script', revision: 2 },
      idempotencyKey: 'align-once', nodeRevision: 5,
    });
    expect(apiCall).toHaveBeenCalledWith('projects/demo/freezone/remix/actions', {
      method: 'POST', json: {
        action: 'align_script',
        payload: { media_path: 'media/voice.wav', script_ref: { artifact_id: 'script', revision: 2 } },
        idempotency_key: 'align-once', node_revision: 5,
      },
    });
  });
});
