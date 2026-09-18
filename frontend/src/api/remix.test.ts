import { describe, expect, it, vi } from 'vitest';

vi.mock('./client', () => ({ apiCall: vi.fn(async (_path: string, _options?: unknown) => ({})) }));

import { apiCall } from './client';
import { confirmRemixOperation, createRemixArtifact, fetchRemixOperation, fetchRemixTaskStatus, listRemixOperations, renderRemixManifest, submitRemixProcessAction, uploadRemixMedia } from './remix';

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

  it('uploads a media file and retrieves a render operation', async () => {
    const file = new File(['video'], 'clip.mp4', { type: 'video/mp4' });
    await uploadRemixMedia('demo', file);
    expect(apiCall).toHaveBeenCalledWith('projects/demo/freezone/remix/media', expect.objectContaining({ method: 'POST', body: expect.any(FormData) }));
    await fetchRemixOperation('demo', 'operation-1');
    expect(apiCall).toHaveBeenCalledWith('projects/demo/freezone/remix/operations/operation-1');
    await fetchRemixTaskStatus('demo', 'remix_render', 'job-1');
    expect(apiCall).toHaveBeenCalledWith('projects/demo/tasks/remix_render/0?scope=job-1');
    await confirmRemixOperation('demo', 'operation-1', 'default', 'compose-1');
    expect(apiCall).toHaveBeenCalledWith('projects/demo/freezone/remix/operations/operation-1/confirm', { method: 'POST', json: { canvas_id: 'default', node_id: 'compose-1' } });
    await listRemixOperations('demo');
    expect(apiCall).toHaveBeenCalledWith('projects/demo/freezone/remix/operations?limit=50');
  });
});
