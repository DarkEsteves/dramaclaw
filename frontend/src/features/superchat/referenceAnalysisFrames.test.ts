import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useSuperChat } from './use-superchat';
import { REFERENCE_ANALYSIS_STATUS_EVENT } from '@/features/freezone/referenceAnalysisEvents';

vi.mock('@/lib/api', () => ({ api: { post: vi.fn(async () => ({})) } }));

describe('reference analysis websocket events', () => {
  const originalWebSocket = globalThis.WebSocket;
  afterEach(() => { vi.restoreAllMocks(); Object.defineProperty(globalThis, 'WebSocket', { value: originalWebSocket, writable: true, configurable: true }); });
  it('forwards only the active canvas and agent scope without starting an agent turn', async () => {
    const sent: unknown[] = [];
    const sockets: TestWebSocket[] = [];
    class TestWebSocket {
      static OPEN = 1;
      readyState = 1;
      onopen: (() => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: (() => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      constructor() { sockets.push(this); }
      send(data: string) { sent.push(JSON.parse(data)); }
      close() {}
    }
    Object.defineProperty(globalThis, 'WebSocket', { value: TestWebSocket, writable: true, configurable: true });
    const received = vi.fn();
    window.addEventListener(REFERENCE_ANALYSIS_STATUS_EVENT, received);
    const hook = renderHook(() => useSuperChat({ project: 'project', displayName: 'Tester', surface: 'freezone', freezoneCanvasId: 'canvas', freezoneAgentId: 'main' }));
    try {
      await waitFor(() => expect(sockets).toHaveLength(1));
      await waitFor(() => expect(sockets[0].onmessage).toBeTypeOf('function'));
      const scope = { kind: 'project', id: 'project', surface: 'freezone', canvasId: 'canvas', agentId: 'main' };
      const run = { run_id: 'run', project_id: 'project', canvas_id: 'canvas', agent_id: 'main', status: 'analysis_ready' };
      act(() => {
        sockets[0].onopen?.();
        sockets[0].onmessage?.({ data: JSON.stringify({ type: 'reference.remix.status', scope: { ...scope, canvasId: 'other' }, run }) } as MessageEvent);
        sockets[0].onmessage?.({ data: JSON.stringify({ type: 'reference.remix.status', scope: { ...scope, agentId: 'other' }, run }) } as MessageEvent);
        sockets[0].onmessage?.({ data: JSON.stringify({ type: 'reference.remix.status', scope, run }) } as MessageEvent);
      });
      expect(received).toHaveBeenCalledTimes(1);
      expect((received.mock.calls[0][0] as CustomEvent).detail).toEqual({ run });
      expect(sent.filter((frame) => (frame as { type: string }).type === 'chat.send')).toHaveLength(0);
      expect(hook.result.current.busy).toBe(false);
      act(() => {
        const completed = { ...run, status: 'completed', workflow_draft_id: 'draft', workflow_draft_revision: 1 };
        sockets[0].onmessage?.({ data: JSON.stringify({ type: 'reference.remix.status', scope, run: completed }) } as MessageEvent);
        sockets[0].onmessage?.({ data: JSON.stringify({ type: 'reference.remix.status', scope, run: completed }) } as MessageEvent);
      });
      expect(sent.filter((frame) => (frame as { type: string }).type === 'scope.set')).toHaveLength(2);
    } finally { window.removeEventListener(REFERENCE_ANALYSIS_STATUS_EVENT, received); hook.unmount(); }
  });
});
