import { describe, expect, it, vi } from 'vitest';
vi.mock('@/api/remix', () => ({ createRemixArtifact: vi.fn(async () => ({ id: 'script-1', revision: 1 })) }));
vi.mock('@/api/client', () => ({ apiCall: vi.fn(async () => ({ id: 'script-1', revision: 2 })) }));
import { createRemixArtifact } from '@/api/remix';
import { apiCall } from '@/api/client';
import { canRecordRemixScriptSave, latestWritableRemixScriptRef, saveRemixScriptArtifact } from './remixScriptArtifact';

describe('saveRemixScriptArtifact', () => {
  const draft = { dialogue: '咖啡', selections: [{ id: 'product', label: '产品', start: 0, end: 2 }] };
  it('creates first script revision with blueprint reference', async () => {
    await saveRemixScriptArtifact('demo', draft, null, { artifactId: 'blueprint', revision: 1 });
    expect(createRemixArtifact).toHaveBeenCalledWith('demo', 'remix_script.v1', expect.objectContaining({ selections: [{ selection_id: 'product', start_char: 0, end_char: 2, script_revision: 1 }] }), [{ artifactId: 'blueprint', revision: 1 }]);
  });
  it('revises existing artifact and advances exact selection revision', async () => {
    await saveRemixScriptArtifact('demo', draft, { artifactId: 'script-1', revision: 1 }, null);
    expect(apiCall).toHaveBeenCalledWith('projects/demo/freezone/remix/artifacts/script-1', expect.objectContaining({ method: 'PUT', json: expect.objectContaining({ expected_revision: 1, payload: expect.objectContaining({ selections: [{ selection_id: 'product', start_char: 0, end_char: 2, script_revision: 2 }] }) }) }));
  });
  it('uses the latest locally saved revision after an earlier draft was superseded', () => {
    expect(latestWritableRemixScriptRef(
      { artifactId: 'script-1', revision: 28 }, { artifactId: 'script-1', revision: 29 },
    )).toEqual({ artifactId: 'script-1', revision: 29 });
    expect(latestWritableRemixScriptRef(
      { artifactId: 'new-script', revision: 1 }, { artifactId: 'old-script', revision: 99 },
    )).toEqual({ artifactId: 'new-script', revision: 1 });
  });
  it('records a completed save after the active draft was edited while preserving a newer branch', () => {
    const base = { artifactId: 'script-1', revision: 29 };
    const saved = { artifactId: 'script-1', revision: 30 };
    expect(canRecordRemixScriptSave(base, null, base, saved)).toBe(true);
    expect(canRecordRemixScriptSave(base, { artifactId: 'script-1', revision: 28 }, base, saved)).toBe(true);
    expect(canRecordRemixScriptSave(base, { artifactId: 'new-script', revision: 1 }, base, saved)).toBe(false);
    expect(canRecordRemixScriptSave(base, null, { artifactId: 'script-1', revision: 31 }, saved)).toBe(false);
  });
});
