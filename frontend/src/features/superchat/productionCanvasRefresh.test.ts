import { beforeEach, describe, expect, it, vi } from 'vitest';
import { getFreezoneCanvas, type FreezoneCanvasPayload } from '@/api/canvas';
import { registerFreezoneCanvasRuntime, type RemoteCanvasMerge } from '@/features/freezone/canvasSyncRuntime';
import { useCanvasStore, type CanvasNode } from '@/stores/canvasStore';
import { refreshProductionCanvas } from './productionCanvasRefresh';
vi.mock('@/api/canvas', () => ({ getFreezoneCanvas: vi.fn() }));
const node = (id: string, label: string) => ({ id, type: 'imageNode', position: { x: 0, y: 0 }, data: { label } } as CanvasNode);
describe('production canvas refresh', () => {
  beforeEach(() => { vi.clearAllMocks(); useCanvasStore.setState({ nodes: [], edges: [] }); });
  it('merges server nodes and metadata while preserving unsaved fields', async () => {
    const base = node('existing', 'saved');
    const local = node('existing', 'my draft');
    useCanvasStore.setState({ nodes: [local], edges: [] });
    const apply = vi.fn((_remote: FreezoneCanvasPayload, _merge?: RemoteCanvasMerge) => true);
    const unregister = registerFreezoneCanvasRuntime('p', 'c', apply, undefined, undefined, undefined, undefined,
      () => ({ nodes: [base], edges: [], revision: 1, metadata: { title: 'saved' }, localMetadata: { title: 'my title' } }));
    vi.mocked(getFreezoneCanvas).mockResolvedValue({ revision: 2, nodes: [base, node('server', 'new result')], edges: [], metadata: { title: 'saved', agentResult: 'ready' } } as never);
    try {
      await refreshProductionCanvas('p', 'c');
      expect(apply).toHaveBeenCalledTimes(1);
      const merged = apply.mock.calls[0][1]!([], [], [], []);
      expect(merged).toMatchObject({ nodes: [{ id: 'existing', data: { label: 'my draft' } }, { id: 'server' }], metadata: { title: 'my title', agentResult: 'ready' } });
    } finally { unregister(); }
  });
  it('rejects conflicting content before any graph or save baseline is applied', async () => {
    const base = node('n', 'saved');
    useCanvasStore.setState({ nodes: [node('n', 'my draft')], edges: [] });
    const apply = vi.fn();
    const unregister = registerFreezoneCanvasRuntime('p', 'c', apply, undefined, undefined, undefined, undefined,
      () => ({ nodes: [base], edges: [], revision: 1 }));
    vi.mocked(getFreezoneCanvas).mockResolvedValue({ revision: 2, nodes: [node('n', 'server edit')], edges: [] } as never);
    try {
      await expect(refreshProductionCanvas('p', 'c')).rejects.toThrow(/同时修改/);
      expect(apply).not.toHaveBeenCalled();
      expect(useCanvasStore.getState().nodes[0].data.label).toBe('my draft');
    } finally { unregister(); }
  });
  it('does not apply a late canvas to a new registration for the same URL', async () => {
    let release!: (remote: Awaited<ReturnType<typeof getFreezoneCanvas>>) => void;
    vi.mocked(getFreezoneCanvas).mockReturnValue(new Promise((resolve) => { release = resolve; }));
    const oldApply = vi.fn(), newApply = vi.fn();
    const stopOld = registerFreezoneCanvasRuntime('p', 'c', oldApply);
    const pending = refreshProductionCanvas('p', 'c');
    stopOld();
    const stopNew = registerFreezoneCanvasRuntime('p', 'c', newApply);
    try {
      release({ revision: 2, nodes: [node('stale', 'old result')], edges: [] } as never);
      await pending;
      expect(oldApply).not.toHaveBeenCalled(); expect(newApply).not.toHaveBeenCalled();
    } finally { stopNew(); }
  });
  it('does not apply a late graph after the account or production lifetime changed', async () => {
    let release!: (remote: Awaited<ReturnType<typeof getFreezoneCanvas>>) => void;
    vi.mocked(getFreezoneCanvas).mockReturnValue(new Promise((resolve) => { release = resolve; }));
    const apply = vi.fn();
    const unregister = registerFreezoneCanvasRuntime('p', 'c', apply);
    let valid = true;
    const pending = refreshProductionCanvas('p', 'c', () => valid);
    try {
      valid = false;
      release({ revision: 2, nodes: [], edges: [] } as never);
      await pending;
      expect(apply).not.toHaveBeenCalled();
    } finally { unregister(); }
  });
});
