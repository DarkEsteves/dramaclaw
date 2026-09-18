import { describe, expect, it, vi } from 'vitest';

vi.mock('@/api/client', () => ({ apiCall: vi.fn(async () => ({ id: 'words-1', revision: 2 })) }));

import { apiCall } from '@/api/client';
import { reviseTranscriptArtifact } from './transcriptArtifactApi';

describe('reviseTranscriptArtifact', () => {
  it('keeps the original script dependency when correcting aligned words', async () => {
    await reviseTranscriptArtifact('demo', { artifactId: 'words-1', revision: 1 }, {
      status: 'completed', media_hash: 'hash', language: 'zh', transcript: '您好',
      words: [{ text: '您', start_us: 0, end_us: 500_000, start_char: 0, end_char: 1 }],
    }, [{ artifactId: 'script-1', revision: 3 }]);
    expect(apiCall).toHaveBeenCalledWith('projects/demo/freezone/remix/artifacts/words-1', {
      method: 'PUT',
      json: {
        expected_revision: 1,
        payload: expect.objectContaining({ transcript: '您好' }),
        upstream_refs: [{ artifact_id: 'script-1', revision: 3 }],
      },
    });
  });
});
