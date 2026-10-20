import { describe, expect, it } from 'vitest';
import { captureRegisteredFreezoneCanvasSession, getFreezoneCanvasBaseline, registerFreezoneCanvasRuntime } from './canvasSyncRuntime';

describe('registered canvas session', () => {
  it('returns the saved baseline only for the registered project and canvas', () => {
    const baseline = { nodes: [], edges: [], revision: 4 };
    const unregister = registerFreezoneCanvasRuntime('demo', 'canvas', () => true, undefined, undefined, undefined, undefined, () => baseline);
    expect(getFreezoneCanvasBaseline('demo', 'canvas')).toEqual(baseline);
    expect(getFreezoneCanvasBaseline('other', 'canvas')).toBeNull();
    unregister();
    expect(getFreezoneCanvasBaseline('demo', 'canvas')).toBeNull();
  });
  it('invalidates old captures on replacement, even for the same project and canvas IDs', () => {
    const beforeMount = captureRegisteredFreezoneCanvasSession('demo');
    const unregisterA = registerFreezoneCanvasRuntime('demo', 'canvas', () => true);
    expect(beforeMount.isCurrent()).toBe(false);
    const a = captureRegisteredFreezoneCanvasSession('demo');
    const unregisterB = registerFreezoneCanvasRuntime('demo', 'canvas', () => true);
    const b = captureRegisteredFreezoneCanvasSession('demo');
    expect(a.id).not.toBe(b.id);
    expect(a.isCurrent()).toBe(false);
    expect(b.isCurrent()).toBe(true);
    unregisterA();
    expect(b.isCurrent()).toBe(true);
    unregisterB();
    expect(b.isCurrent()).toBe(false);
    expect(beforeMount.isCurrent()).toBe(false);
  });
});
