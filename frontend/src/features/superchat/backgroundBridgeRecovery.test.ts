import { describe, expect, it, vi } from 'vitest';
import { createBackgroundBridgeRecovery, browserRecoveryId } from './backgroundBridgeRecovery';

const page = (overrides = {}) => ({ frames: [], messages: [], events: [], receipts: [], cursor: 0, event_cursor: '', has_more: false, ...overrides });
describe('scoped durable bridge recovery', () => {
  it('keeps one browser identity for HTTP and websocket reconnects', () => {
    expect(browserRecoveryId()).toBe(browserRecoveryId());
    expect(browserRecoveryId()).toMatch(/^[\w-]{16,128}$/);
  });
  it('uses both committed cursors and drains bounded pages', async () => {
    const pull = vi.fn().mockResolvedValueOnce(page({ cursor: 12, event_cursor: 'r23', has_more: true })).mockResolvedValueOnce(page({ cursor: 13, event_cursor: 'r25' }));
    const receive = vi.fn();
    const recovery = createBackgroundBridgeRecovery({ pull, receive, denied: vi.fn() });
    await recovery.tick();
    expect(pull.mock.calls).toEqual([[0, '', expect.any(AbortSignal)], [12, 'r23', expect.any(AbortSignal)]]);
    expect(receive).toHaveBeenCalledTimes(2);
    recovery.stop();
  });
  it('discards responses after scope teardown', async () => {
    let resolve!: (value: ReturnType<typeof page>) => void;
    const receive = vi.fn();
    const recovery = createBackgroundBridgeRecovery({ pull: () => new Promise(r => { resolve = r; }), receive, denied: vi.fn() });
    const pending = recovery.tick();
    recovery.stop();
    resolve(page({ cursor: 8 }));
    await pending;
    expect(receive).not.toHaveBeenCalled();
  });
  it('stops on revoked auth and does not retry it', async () => {
    const pull = vi.fn().mockRejectedValue({ response: { status: 403 } });
    const denied = vi.fn();
    const recovery = createBackgroundBridgeRecovery({ pull, receive: vi.fn(), denied });
    await recovery.tick();
    await recovery.tick();
    expect(pull).toHaveBeenCalledTimes(1);
    expect(denied).toHaveBeenCalledTimes(1);
  });
});

import { mergeBridgeRecoveryMessages } from './backgroundBridgeRecovery';

it.each([false, true])('hides cards for unloaded turns even if recovery replays old messages (%s)', (withMessages) => {
  const recent = Array.from({ length: 50 }, (_, index) => ({
    id: `recent-${index}`, role: 'assistant' as const, turnId: `recent-${index}`,
    text: 'recent reply', timestamp: 2000 + index,
  }));
  const result = mergeBridgeRecoveryMessages(recent, page({
    messages: withMessages ? [{ id: 9, role: 'assistant', content: 'old reply', turn_id: 'old', timestamp: 1000 }] : [],
    events: [{ id: 7, turn_id: 'old', type: 'assistant.message_parts', payload: { parts: [{ id: 'text', type: 'text', text: 'old card' }] }, created_at: '2026-10-22T09:00:00Z' }],
    receipts: [{ request_id: 'old-command', kind: 'canvas_command', turn_id: 'old', status: 'applied', result: { applied_count: 1 } }],
  }));
  expect(result.map(message => message.id)).toEqual(recent.map(message => message.id));
  expect(result.flatMap(message => message.parts ?? [])).toEqual([]);
});

it('updates loaded cards without moving them and still recovers new messages', () => {
  const current = [
    { id: '9', role: 'assistant' as const, turnId: 'loaded', text: '', timestamp: 1000 },
    { id: '10', role: 'assistant' as const, turnId: 'recent', text: '', timestamp: 2000 },
  ];
  const result = mergeBridgeRecoveryMessages(current, page({
    messages: [{ id: 11, role: 'assistant', content: 'new reply', turn_id: 'new', timestamp: 3000 }],
    receipts: [{ request_id: 'command', kind: 'canvas_command', turn_id: 'loaded', status: 'applied', result: { applied_count: 1 } }],
  }));
  expect(result.map(message => message.id)).toEqual(['9', '10', '11']);
  expect(result[0]).toMatchObject({ timestamp: 1000, parts: [expect.objectContaining({ type: 'canvas_feedback' })] });
});

it('anchors a loaded unfinished turn to its user message and merges its later original reply', () => {
  const current = [{ id: '7', role: 'user' as const, turnId: 'loaded', text: 'request', timestamp: 1000 }];
  const recovered = mergeBridgeRecoveryMessages(current, page({
    receipts: [{ request_id: 'command', kind: 'canvas_command', turn_id: 'loaded', status: 'applied', result: { applied_count: 1, command_results: [{ commandIndex: 0, type: 'run_node_action', status: 'success' }] } }],
  }));
  expect(recovered.map(message => message.id)).toEqual(['7', 'assistant-loaded']);
  expect(recovered[1].timestamp).toBe(1000);
  const merged = mergeBridgeRecoveryMessages(recovered, page({ messages: [{ id: 9, role: 'assistant', content: 'original', turn_id: 'loaded', timestamp: 1100 }] }));
  expect(merged.map(message => message.id)).toEqual(['7', '9']);
  expect(merged[1].parts).toEqual(expect.arrayContaining([expect.objectContaining({ type: 'canvas_feedback', event: expect.objectContaining({ applied: 1 }) })]));
});

it('keeps the terminal executor card when an older canonical pending receipt arrives', () => {
  const current = [{ id: 'assistant-1', role: 'assistant' as const, turnId: 'turn-1', text: '', timestamp: 1, parts: [{
    id: 'canvas_feedback:bridge:request-1.executor.owner-1', type: 'canvas_feedback' as const,
    event: { key: 'bridge:request-1.executor.owner-1', bridgeKey: 'request-1.executor.owner-1', turnId: 'turn-1', workflowRunId: 'run-1', applied: 1, openedUiActions: 0, errors: [], commandResults: [
      { commandIndex: 0, type: 'run_node_action', status: 'success', nodeId: 'plan', action: 'generate_production_plan_draft', label: '生成方案', output: { workflow_run_id: 'run-1', workflow_run_status: 'completed' } },
    ] },
  }] }];
  const result = mergeBridgeRecoveryMessages(current, page({ receipts: [{ request_id: 'request-1', kind: 'canvas_command', turn_id: 'turn-1', status: 'applied', result: {
    applied_count: 0, workflow_run_id: 'run-1', background_execution_status: 'running', command_results: [
      { commandIndex: 0, type: 'run_node_action', status: 'pending', nodeId: 'plan', action: 'generate_production_plan_draft', label: '生成方案', output: { reason: 'native_action_accepted', workflow_run_id: 'run-1' } },
    ],
  } }] }));
  const cards = result.flatMap(message => message.parts ?? []).filter(part => part.type === 'canvas_feedback');
  expect(cards).toHaveLength(1);
  expect(cards[0]).toMatchObject({ event: { turnId: 'turn-1', bridgeKey: 'request-1.executor.owner-1', workflowRunId: 'run-1', applied: 1, commandResults: [{ status: 'success' }] } });
});

it('keeps separate terminal cards for distinct workflow runs', () => {
  const completed = { id: 'canvas_feedback:bridge:request.executor.owner', type: 'canvas_feedback' as const,
    event: { key: 'bridge:request.executor.owner', bridgeKey: 'request.executor.owner', turnId: 'turn', workflowRunId: 'run-old', applied: 1, commandResults: [{ commandIndex: 0, type: 'run_node_action', status: 'success', output: { workflow_run_id: 'run-old', workflow_run_status: 'completed' } }] } };
  const result = mergeBridgeRecoveryMessages([{ id: 'assistant-turn', role: 'assistant' as const, turnId: 'turn', text: '', timestamp: 1, parts: [completed] }], page({ receipts: [{ request_id: 'request', kind: 'canvas_command', turn_id: 'turn', status: 'applied', result: { workflow_run_id: 'run-new', command_results: [{ commandIndex: 0, type: 'run_node_action', status: 'pending', output: { workflow_run_id: 'run-new', reason: 'native_action_accepted' } }] } }] }));
  expect(result[0].parts?.filter(part => part.type === 'canvas_feedback')).toHaveLength(2);
});
it.each([true, false])('does not restore pending after a cancelled validation-only card (flag %s)', (withCancelledFlag) => {
  const cancelled = { id: 'canvas_feedback:bridge:request.executor.owner', type: 'canvas_feedback' as const,
    event: { key: 'bridge:request.executor.owner', bridgeKey: 'request.executor.owner', turnId: 'turn', applied: 0, ...(withCancelledFlag ? { cancelled: true } : {}), cancelReason: 'user', errors: ['已取消'], commandResults: [{ commandIndex: -1, type: 'validate', status: 'error', label: '已取消', error: '已取消' }] } };
  const result = mergeBridgeRecoveryMessages([{ id: 'assistant-turn', role: 'assistant' as const, turnId: 'turn', text: '', timestamp: 1, parts: [cancelled] }], page({ receipts: [{ request_id: 'request', kind: 'canvas_command', turn_id: 'turn', status: 'applied', result: { applied_count: 0, background_execution_status: 'running', command_results: [{ commandIndex: 0, type: 'run_node_action', status: 'pending', label: '生成方案', output: { reason: 'native_action_accepted' } }] } }] }));
  const cards = result[0].parts?.filter(part => part.type === 'canvas_feedback');
  expect(cards).toHaveLength(1);
  expect(cards?.[0]).toMatchObject({ event: { commandResults: [{ label: '已取消' }] } });
  expect((cards?.[0] as { event?: { cancelled?: boolean } } | undefined)?.event?.cancelled).toBe(withCancelledFlag ? true : undefined);
});

it('retains a terminal card against a late message-parts snapshot', () => {
  const terminal = { id: 'canvas_feedback:bridge:request.executor.owner', type: 'canvas_feedback' as const, event: { key: 'bridge:request.executor.owner', bridgeKey: 'request.executor.owner', workflowRunId: 'run', turnId: 'turn', applied: 1, commandResults: [{ commandIndex: 0, type: 'run_node_action', status: 'success', output: { workflow_run_id: 'run', workflow_run_status: 'completed' } }] } };
  const pending = { ...terminal, id: 'canvas_feedback:bridge:request', event: { ...terminal.event, key: 'bridge:request', bridgeKey: 'request', applied: 0, commandResults: [{ commandIndex: 0, type: 'run_node_action', status: 'pending', output: { workflow_run_id: 'run', reason: 'native_action_accepted' } }] } };
  const current = [{ id: 'assistant-turn', role: 'assistant' as const, turnId: 'turn', text: '', timestamp: 1, parts: [terminal] }];
  const result = mergeBridgeRecoveryMessages(current, page({ events: [{ id: 7, turn_id: 'turn', type: 'assistant.message_parts', payload: { parts: [pending] }, created_at: '' }] }));
  expect(result[0].parts?.filter(part => part.type === 'canvas_feedback')).toHaveLength(1);
  expect(result[0].parts?.[0]).toMatchObject({ event: { applied: 1, commandResults: [{ status: 'success' }] } });
});
it('replaces persisted messages and mutable event snapshots by stable IDs and original turn', () => {
  const existing = [{ id: '9', role: 'assistant' as const, text: 'old', turnId: 'origin', timestamp: 1 }];
  const first = mergeBridgeRecoveryMessages(existing, page({ messages: [{ id: 9, role: 'assistant', content: 'actual', turn_id: 'origin' }], events: [{ id: 7, turn_id: 'origin', type: 'assistant.message_parts', payload: { type: 'assistant.message_parts', parts: [{ id: 'plan', type: 'agent_plan', event: { status: 'running' } }] }, created_at: 'old' }] }));
  const second = mergeBridgeRecoveryMessages(first, page({ events: [{ id: 7, turn_id: 'origin', type: 'assistant.message_parts', payload: { type: 'assistant.message_parts', parts: [{ id: 'plan', type: 'agent_plan', event: { status: 'completed' } }] }, created_at: 'older' }] }));
  expect(second).toHaveLength(1);
  expect(second[0]).toMatchObject({ id: '9', turnId: 'origin', text: 'actual', parts: expect.arrayContaining([{ id: 'plan', type: 'agent_plan', event: { status: 'completed' } }]) });
});

import { deliverClosedCanvasRecovery } from './backgroundBridgeRecovery';
it('services external commands with a closed panel and only opens existing UI for real interactions', () => {
  const command = vi.fn(), context = vi.fn(), openChat = vi.fn();
  deliverClosedCanvasRecovery(page({ frames: [{ type: 'canvas.command', bridge_key: 'owned.executor.token' }, { type: 'canvas.context.request' }], receipts: [{ request_id: 'old', kind: 'canvas_command', status: 'applied', turn_id: 'original', result: {} }] }), { command, context, openChat });
  expect(command).toHaveBeenCalledTimes(1);
  expect(context).toHaveBeenCalledTimes(1);
  expect(openChat).not.toHaveBeenCalled();
  deliverClosedCanvasRecovery(page({ frames: [{ type: 'assistant.clarification.event' }] }), { command, context, openChat });
  expect(openChat).toHaveBeenCalledTimes(1);
});

import { claimContextRecoveryAlias } from './backgroundBridgeRecovery';
it('dedupes a read observed by closed-shell pull and mounted websocket but permits a new owner to reread', () => {
  expect(claimContextRecoveryAlias('context-test.executor.first')).toBe(true);
  expect(claimContextRecoveryAlias('context-test.executor.first')).toBe(false);
  expect(claimContextRecoveryAlias('context-test.executor.transferred')).toBe(true);
});
it('preserves two genuinely distinct persisted messages from one original turn', () => {
  const result = mergeBridgeRecoveryMessages([], page({ messages: [
    { id: 1, role: 'assistant', content: 'first', turn_id: 'original' },
    { id: 2, role: 'assistant', content: 'second', turn_id: 'original' },
  ] }));
  expect(result.map(message => message.id)).toEqual(['1', '2']);
});

it('cannot execute a history-only approval and rebinds only an actually owned frame', () => {
  const approval = { id: 'approval', type: 'canvas_approval', event: { bridgeKey: 'canonical', envelopes: [{ commands: [{ type: 'run_workflow', idempotency_key: 'native-once' }] }] } };
  const messages = [{ id: 5, role: 'assistant', content: 'approval', turn_id: 'original', parts: [approval] }];
  const unowned = mergeBridgeRecoveryMessages([], page({ messages }));
  expect(unowned[0].parts?.some(part => part.type === 'canvas_approval')).toBe(false);
  const owned = mergeBridgeRecoveryMessages([], page({ messages, frames: [{ type: 'canvas.command', request_id: 'canonical', bridge_key: 'canonical.executor.current' }] }));
  expect(owned[0].parts?.find(part => part.type === 'canvas_approval')).toMatchObject({ event: { bridgeKey: 'canonical.executor.current', envelopes: [{ commands: [{ idempotency_key: 'native-once', type: 'run_workflow' }] }] } });
});

import { rememberContextRecoveryResult, contextRecoveryResult } from './backgroundBridgeRecovery';
it('replays a real completed read after lost delivery without granting another actor its result', () => {
  const result = { bridge_key: 'read.executor.current', responses: [{ native_revision: 7, value: 'actual browser selection' }] };
  rememberContextRecoveryResult(result, 'human');
  expect(contextRecoveryResult('read.executor.current', 'human')).toBe(result);
  expect(contextRecoveryResult('read.executor.current', 'peer')).toBeNull();
  expect(contextRecoveryResult('read.executor.other', 'human')).toBeNull();
});

import { startIndependentBridgeRecovery } from './backgroundBridgeRecovery';
it('renews a healthy closed-dock owner below45seconds despite three12second scopes and pagination', async () => {
  vi.useFakeTimers();
  const healthyTimes: number[] = [];
  const stopped = vi.fn();
  let slowCalls = 0;
  const schedulers = startIndependentBridgeRecovery({
    scopeIds: () => ['healthy', 'slow-1', 'slow-2', 'paged'],
    createRecovery: scope => {
      const recovery = createBackgroundBridgeRecovery({
        pull: async cursor => {
          if (scope === 'healthy') { healthyTimes.push(Date.now()); return page({ cursor: cursor + 1, has_more: true }); }
          slowCalls++;
          return new Promise((resolve, reject) => setTimeout(() => {
            if (scope === 'paged') resolve(page({ cursor: cursor + 1, has_more: true }));
            else reject(new Error('network timeout'));
          }, 12_000));
        }, receive: () => {}, denied: stopped,
      });
      return recovery;
    },
  });
  try {
    await vi.advanceTimersByTimeAsync(44_000);
    expect(slowCalls).toBeGreaterThan(0);
    expect(healthyTimes.length).toBeGreaterThanOrEqual(4);
    for (let index = 1; index < healthyTimes.length; index++) expect(healthyTimes[index] - healthyTimes[index - 1]).toBeLessThan(45_000);
  } finally { schedulers.stop(); vi.useRealTimers(); }
});
it('bounds one cursor drain and continues at the committed cursors on the next heartbeat', async () => {
  const pull = vi.fn(async (cursor: number) => page({ cursor: cursor + 1, event_cursor: `r${cursor + 1}`, has_more: cursor < 3 }));
  const recovery = createBackgroundBridgeRecovery({ pull, receive: () => {}, denied: () => {} });
  await recovery.tick();
  expect(pull).toHaveBeenCalledTimes(2);
  await recovery.tick();
  expect(pull).toHaveBeenCalledTimes(4);
  expect(pull.mock.calls[2][0]).toBe(2);
  recovery.stop();
});

it('matches only the exact optimistic user identity and never merges distinct persisted rows or equal text', () => {
  const current = [
    { id: 'user-turn-a', role: 'user' as const, turnId: 'turn-a', text: 'equal', timestamp: 1 },
    { id: 'user-turn-b', role: 'user' as const, turnId: 'turn-b', text: 'equal', timestamp: 2 },
    { id: '99', role: 'user' as const, turnId: 'turn-a', text: 'equal', timestamp: 3 },
  ];
  const result = mergeBridgeRecoveryMessages(current, page({ messages: [{ id: 1, role: 'user', turn_id: 'turn-a', content: 'authoritative saved text' }] }), new Set(['turn-a']));
  expect(result).toHaveLength(3);
  expect(result.find(message => message.id === '1')?.text).toBe('authoritative saved text');
  expect(result.find(message => message.id === 'user-turn-b')?.text).toBe('equal');
  expect(result.some(message => message.id === '99')).toBe(true);
});

it('does not reconcile an optimistic message from an unowned actor or scope', () => {
  const current = [{ id: 'user-turn-a', role: 'user' as const, turnId: 'turn-a', text: 'equal', timestamp: 1 }];
  const result = mergeBridgeRecoveryMessages(current, page({ messages: [{ id: 1, role: 'user', turn_id: 'turn-a', content: 'equal' }] }), new Set());
  expect(result.map(message => message.id).sort()).toEqual(['1', 'user-turn-a']);
});


const loadedAssistant = (turnId = 'turn') => ({ id: `assistant-${turnId}`, role: 'assistant' as const, turnId, text: '', timestamp: 1 });

it.each(['waiting', 'timed_out', 'applied'])('does not certify a pending clarification receipt (%s)', status => {
  const result = mergeBridgeRecoveryMessages([loadedAssistant()], page({ receipts: [{ request_id: 'question', kind: 'clarification_event', turn_id: 'turn', status, result: { ok: true, clarification_status: 'pending_user_input', answers: {} } }] }));
  expect(result[0].parts?.some(part => part.type === 'clarification' && (part.event as { submitted?: boolean }).submitted)).toBe(false);
});
it('recovers actual applied clarification answers and preserves them against delayed history', () => {
  const receipt = { request_id: 'question', kind: 'clarification_event', turn_id: 'turn', status: 'applied', result: { ok: true, status: 'clarification_frontend_result', clarification_status: 'answered', answers: { duration: '30 seconds' } } };
  const current = mergeBridgeRecoveryMessages([loadedAssistant()], page({ receipts: [receipt] }));
  const result = mergeBridgeRecoveryMessages(current, page({ events: [{ id: 1, turn_id: 'turn', type: 'assistant.clarification.request', payload: { type: 'assistant.clarification.request', bridge_key: 'question.executor.delayed', answers: {} }, created_at: '' }] }));
  expect(result[0].parts).toHaveLength(1);
  expect(result[0].parts?.[0]).toMatchObject({ event: { submitted: true, answers: { duration: '30 seconds' } } });
});

it('keeps same-title clarification requests and original turns separate', () => {
  const receipt = (request_id: string, turn_id: string) => ({ request_id, turn_id, kind: 'clarification_event', status: 'applied', result: { ok: true, status: 'clarification_frontend_result', clarification_status: 'answered', title: 'Questions', answers: { duration: request_id } } });
  const result = mergeBridgeRecoveryMessages([loadedAssistant('one'), loadedAssistant('two')], page({ receipts: [receipt('first', 'one'), receipt('second', 'one'), receipt('first', 'two')] }));
  expect(result).toHaveLength(2);
  expect(result.find(message => message.turnId === 'one')?.parts).toHaveLength(2);
  expect(result.find(message => message.turnId === 'two')?.parts).toHaveLength(1);
});
it('accepts a later actual submitted answer with its real data', () => {
  const receipt = (duration: string) => ({ request_id: 'later-answer', turn_id: 'turn', kind: 'clarification_event', status: 'applied', result: { ok: true, status: 'clarification_frontend_result', clarification_status: 'answered', answers: { duration } } });
  const first = mergeBridgeRecoveryMessages([loadedAssistant()], page({ receipts: [receipt('30 seconds')] }));
  const result = mergeBridgeRecoveryMessages(first, page({ receipts: [receipt('60 seconds')] }));
  expect(result[0].parts?.[0]).toMatchObject({ event: { submitted: true, answers: { duration: '60 seconds' } } });
});

it('does not certify a failed clarification callback as an answer', () => {
  const result = mergeBridgeRecoveryMessages([loadedAssistant()], page({ receipts: [{ request_id: 'failed-question', kind: 'clarification_event', turn_id: 'turn', status: 'applied', result: { ok: false, status: 'clarification_frontend_result', clarification_status: 'answered', answers: { duration: 'uncommitted' } } }] }));
  expect(result[0].parts?.some(part => part.type === 'clarification' && (part.event as { submitted?: boolean }).submitted)).toBe(false);
});

it.each(['waiting', 'timed_out'])('preserves actual answers against a later pending receipt (%s)', status => {
  const receipt = { request_id: 'answered-question', kind: 'clarification_event', turn_id: 'turn', status: 'applied', result: { ok: true, status: 'clarification_frontend_result', clarification_status: 'answered', answers: { duration: '30 seconds' } } };
  const current = mergeBridgeRecoveryMessages([loadedAssistant()], page({ receipts: [receipt] }));
  const result = mergeBridgeRecoveryMessages(current, page({ receipts: [{ ...receipt, status, result: { ok: true, status: 'clarification_frontend_result', clarification_status: 'pending_user_input', answers: {} } }] }));
  expect(result[0].parts).toHaveLength(1);
  expect(result[0].parts?.[0]).toMatchObject({ event: { submitted: true, answers: { duration: '30 seconds' } } });
});
