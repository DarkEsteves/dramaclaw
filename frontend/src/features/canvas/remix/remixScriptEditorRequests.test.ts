import { describe, expect, it, vi } from 'vitest';
import {
  consumeRemixScriptEditorRequest,
  requestRemixScriptEditor,
  subscribeRemixScriptEditorRequest,
} from './remixScriptEditorRequests';

describe('remixScriptEditorRequests', () => {
  it('keeps a request outside canvas data until the target node mounts', () => {
    requestRemixScriptEditor('script-new');
    expect(consumeRemixScriptEditorRequest('script-new')).toBe(true);
    expect(consumeRemixScriptEditorRequest('script-new')).toBe(false);
  });

  it('delivers a request directly to an already mounted target', () => {
    const open = vi.fn();
    const unsubscribe = subscribeRemixScriptEditorRequest('script-existing', open);
    requestRemixScriptEditor('script-existing');
    expect(open).toHaveBeenCalledOnce();
    expect(consumeRemixScriptEditorRequest('script-existing')).toBe(false);
    unsubscribe();
  });
});
