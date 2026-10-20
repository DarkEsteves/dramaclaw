import { describe, expect, it, vi } from 'vitest';

vi.mock('./client', () => ({ apiCall: vi.fn(async (_path: string, _options?: unknown) => ({})), apiBlob: vi.fn(async () => new Blob()) }));

import { apiBlob, apiCall } from './client';
import { migrateProductionPlan, previewProductionFrame, projectProductionPlan, confirmRemixOperation, createRemixArtifact, fetchLatestRemixArtifact, fetchRemixOperation, fetchRemixTaskStatus, generateRemixBrollPlan, generateRemixScriptDraft, listRemixOperations, reconcileRemixOperation, renderRemixManifest, submitRemixProcessAction, uploadRemixMedia } from './remix';

describe('remix API contract', () => {
  it('migrates and projects exact plan revisions', async () => {
    await migrateProductionPlan('demo', { artifactId: 'plan', revision: 7 });
    expect(apiCall).toHaveBeenCalledWith('projects/demo/freezone/remix/production-plans/migrate', { method: 'POST', json: { plan_ref: { artifact_id: 'plan', revision: 7 } } });
    await projectProductionPlan('demo', { artifactId: 'plan-v2', revision: 2 });
    expect(apiCall).toHaveBeenCalledWith('projects/demo/freezone/remix/production-plans/project', { method: 'POST', json: { plan_ref: { artifact_id: 'plan-v2', revision: 2 } } });
    await fetchLatestRemixArtifact('demo', 'plan-v2');
    expect(apiCall).toHaveBeenCalledWith('projects/demo/freezone/remix/artifacts/plan-v2/latest');
  });

  it('requests a binary preview from one exact plan revision and frame', async () => {
    await previewProductionFrame('demo', { artifactId: 'plan-v2', revision: 4 }, 123);
    expect(apiBlob).toHaveBeenCalledWith('projects/demo/freezone/remix/production-plans/preview-frame', {
      method: 'POST', json: { plan_ref: { artifact_id: 'plan-v2', revision: 4 }, frame: 123 }, timeout: false,
    });
  });

  it('sends exact artifact refs and render revision to the backend routes', async () => {
    await createRemixArtifact('demo', 'remix_script.v1', { dialogue: 'hi' }, [{ artifactId: 'source', revision: 2 }]);
    expect(apiCall).toHaveBeenCalledWith('projects/demo/freezone/remix/artifacts', {
      method: 'POST',
      json: { schema: 'remix_script.v1', payload: { dialogue: 'hi' }, upstream_refs: [{ artifact_id: 'source', revision: 2 }] },
    });

    await renderRemixManifest('demo', { manifestRef: { artifactId: 'manifest', revision: 4 }, idempotencyKey: 'once', nodeRevision: 3 });
    expect(apiCall).toHaveBeenCalledWith('projects/demo/freezone/remix/artifacts/manifest/render', {
      method: 'POST', json: { revision: 4, idempotency_key: 'once', node_revision: 3, input_fingerprint: '', regenerate: false },
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

    await submitRemixProcessAction('demo', {
      action: 'plan_broll', scriptRef: { artifactId: 'script', revision: 3 },
      idempotencyKey: 'broll:node:script:3', nodeRevision: 6,
    });
    expect(apiCall).toHaveBeenCalledWith('projects/demo/freezone/remix/actions', {
      method: 'POST', json: {
        action: 'plan_broll',
        payload: { script_ref: { artifact_id: 'script', revision: 3 } },
        idempotency_key: 'broll:node:script:3', node_revision: 6,
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
    await reconcileRemixOperation('demo', 'operation-1');
    expect(apiCall).toHaveBeenCalledWith('projects/demo/freezone/remix/operations/operation-1/reconcile', { method: 'POST' });
  });

  it('generates a draft from an exact saved blueprint revision', async () => {
    await generateRemixScriptDraft('demo', { artifactId: 'blueprint-1', revision: 3 }, '新咖啡');
    expect(apiCall).toHaveBeenCalledWith('projects/demo/freezone/remix/script-drafts', {
      method: 'POST', json: { blueprint_ref: { artifact_id: 'blueprint-1', revision: 3 }, new_topic: '新咖啡' }, timeout: false,
    });
  });

  it('plans B-roll from an exact saved script revision', async () => {
    await generateRemixBrollPlan('demo', { artifactId: 'script-1', revision: 4 });
    expect(apiCall).toHaveBeenCalledWith('projects/demo/freezone/remix/broll-plans', {
      method: 'POST', json: { script_ref: { artifact_id: 'script-1', revision: 4 } }, timeout: false,
    });
  });

  it('queues script draft generation from an exact blueprint and topic', async () => {
    await submitRemixProcessAction('demo', {
      action: 'generate_script_draft',
      blueprintRef: { artifactId: 'blueprint-1', revision: 3 },
      newTopic: '把肌酸改成薯条',
      idempotencyKey: 'draft-key',
      nodeRevision: 5,
    });
    expect(apiCall).toHaveBeenCalledWith('projects/demo/freezone/remix/actions', {
      method: 'POST',
      json: {
        action: 'generate_script_draft',
        payload: { blueprint_ref: { artifact_id: 'blueprint-1', revision: 3 }, new_topic: '把肌酸改成薯条' },
        idempotency_key: 'draft-key',
        node_revision: 5,
      },
    });
  });

  it('queues production plan drafting with the selected source snapshot', async () => {
    await submitRemixProcessAction('demo', {
      action: 'generate_production_plan_draft', topic: '黑咖啡',
      analysisContext: { summary: '先提问' }, idempotencyKey: 'plan-key', nodeRevision: 2,
    });
    expect(apiCall).toHaveBeenCalledWith('projects/demo/freezone/remix/actions', {
      method: 'POST', json: {
        action: 'generate_production_plan_draft', payload: { topic: '黑咖啡', analysis_context: { summary: '先提问' } },
        idempotency_key: 'plan-key', node_revision: 2,
      },
    });
  });

  it('passes a selected speech take to alignment without requiring a legacy placement', async () => {
    await submitRemixProcessAction('demo', {
      action: 'align_production_speech', mediaPath: 'media/two-people.mp4',
      planRef: { artifactId: 'plan', revision: 6 }, turnId: 'host', takeId: 'take-1',
      idempotencyKey: 'take-align', nodeRevision: 6,
    });
    expect(apiCall).toHaveBeenCalledWith('projects/demo/freezone/remix/actions', {
      method: 'POST', json: {
        action: 'align_production_speech',
        payload: { media_path: 'media/two-people.mp4', plan_ref: { artifact_id: 'plan', revision: 6 }, turn_id: 'host', take_id: 'take-1' },
        idempotency_key: 'take-align', node_revision: 6,
      },
    });
  });
});
