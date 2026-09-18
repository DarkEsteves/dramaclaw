import { describe, expect, it } from 'vitest';
import { projectMediaPathFromUrl } from './remixMediaPath';

describe('projectMediaPathFromUrl', () => {
  it('uses a canvas video already stored inside the active project', () => {
    expect(projectMediaPathFromUrl('demo', '/static/projects/demo/freezone/_uploads/clip.mp4?v=1#t=0.1'))
      .toBe('freezone/_uploads/clip.mp4');
  });

  it('rejects other projects and external URLs', () => {
    expect(projectMediaPathFromUrl('demo', '/static/projects/peer/freezone/_uploads/clip.mp4')).toBeNull();
    expect(projectMediaPathFromUrl('demo', 'https://example.com/static/projects/demo/freezone/a.mp4')).toBeNull();
  });
});
