import { describe, expect, it } from 'vitest';
import { canExportManifest, markPreviewStale } from './manifestEditor';

describe('remix manifest editor', () => {
  it('marks an exact preview stale after an edit and blocks unresolved required events', () => {
    expect(markPreviewStale({ revision: 3, previewRevision: 3 })).toEqual({ revision: 3, previewRevision: null });
    expect(canExportManifest({ unresolvedRequiredEvents: ['product reveal'], conflicts: [] })).toBe(false);
    expect(canExportManifest({ unresolvedRequiredEvents: [], conflicts: [] })).toBe(true);
  });
});
