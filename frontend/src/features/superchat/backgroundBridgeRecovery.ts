import type { ServerFrame } from './types';
export type BridgeRecoveryPage = {
  frames: ServerFrame[];
  messages: unknown[];
  events: Array<{ id: number; turn_id: string; type: string; payload: Record<string, unknown>; created_at: string }>;
  receipts: Array<{ request_id: string; kind: string; turn_id: string; status: string; result: Record<string, unknown> }>;
  cursor: number;
  event_cursor: string;
  has_more: boolean;
};
let tabId: string | undefined;
/** Memory belongs to this tab, unlike localStorage (and cloned sessionStorage). */
export function browserRecoveryId(): string {
  return tabId ??= `browser-${crypto.randomUUID()}`;
}
export function canonicalBridgeRequestId(key: string): string {
  return key.replace(/\.executor\.[A-Za-z0-9_-]+$/, '');
}
function canvasFeedbackRunId(event: Record<string, unknown>): string | null {
  const direct = event.workflowRunId ?? event.workflow_run_id;
  if (typeof direct === 'string' && direct) return direct;
  const steps = event.commandResults ?? event.command_results;
  if (!Array.isArray(steps)) return null;
  const ids = new Set(steps.map(step => (step as { output?: { workflow_run_id?: unknown } })?.output?.workflow_run_id).filter((id): id is string => typeof id === 'string' && !!id));
  return ids.size === 1 ? [...ids][0] : null;
}
function canvasFeedbackTerminal(event: Record<string, unknown>): boolean {
  if (event.cancelled === true || event.terminal_status === 'cancelled') return true;
  const steps = event.commandResults ?? event.command_results;
  if (!Array.isArray(steps)) return false;
  if (steps.some(step => step?.type === 'validate' && step?.status === 'error' && step?.label === '已取消')) return true;
  const actions = steps.filter(step => step?.type === 'run_workflow' || step?.type === 'run_node_action');
  return actions.length > 0 && actions.every(step => (step.status === 'success' || step.status === 'error') && step.output?.reason !== 'native_action_accepted' && step.output?.reason !== 'background_generation_running');
}
function preserveTerminalCanvasParts(previous: ChatMessagePart[] | undefined, incoming: ChatMessagePart[] | undefined): ChatMessagePart[] | undefined {
  if (!previous?.length || !incoming) return incoming;
  const parts = [...incoming];
  for (const oldPart of previous) {
    if (oldPart.type !== 'canvas_feedback' || !canvasFeedbackTerminal(oldPart.event as Record<string, unknown>)) continue;
    const oldEvent = oldPart.event as Record<string, unknown>;
    const oldKey = oldEvent.bridgeKey ?? oldEvent.bridge_key ?? (typeof oldEvent.key === 'string' ? oldEvent.key.replace(/^bridge:/, '') : null);
    if (typeof oldKey !== 'string') continue;
    const index = parts.findIndex(part => {
      if (part.type !== 'canvas_feedback') return false;
      const event = part.event as Record<string, unknown>;
      const key = event.bridgeKey ?? event.bridge_key ?? (typeof event.key === 'string' ? event.key.replace(/^bridge:/, '') : null);
      const oldRun = canvasFeedbackRunId(oldEvent), run = canvasFeedbackRunId(event);
      return typeof key === 'string' && canonicalBridgeRequestId(key) === canonicalBridgeRequestId(oldKey)
        && (!oldRun || !run || oldRun === run);
    });
    if (index < 0) parts.push(oldPart);
    else if (parts[index].type === 'canvas_feedback' && !canvasFeedbackTerminal(parts[index].event as Record<string, unknown>)) parts[index] = oldPart;
  }
  return parts;
}
export function createBackgroundBridgeRecovery(options: {
  pull: (cursor: number, eventCursor: string, signal: AbortSignal) => Promise<BridgeRecoveryPage>;
  receive: (page: BridgeRecoveryPage) => void;
  denied: () => void;
}) {
  let cursor = 0;
  let eventCursor = '';
  let stopped = false;
  let inFlight = false;
  const controller = new AbortController();
  return {
    async tick() {
      if (stopped || inFlight) return;
      inFlight = true;
      try {
        let more = true;
        let pages = 0;
        while (more && !stopped && pages < 2) {
          pages++;
          const page = await options.pull(cursor, eventCursor, controller.signal);
          if (stopped) return;
          options.receive(page);
          cursor = page.cursor;
          eventCursor = page.event_cursor;
          more = page.has_more;
        }
      } catch (error) {
        const status = (error as { response?: { status?: number } })?.response?.status;
        if (!stopped && (status === 401 || status === 403)) {
          stopped = true;
          controller.abort();
          options.denied();
        }
      } finally {
        inFlight = false;
      }
    },
    stop() { stopped = true; controller.abort(); },
  };
}

import { normalizeMessage, mergeClarificationPresentation } from './message';
import type { ChatMessage, ChatMessagePart } from './types';

/** Incremental recovery never treats a bounded page as a complete history. */
export function mergeBridgeRecoveryMessages(current: ChatMessage[], page: BridgeRecoveryPage, optimisticUserTurns: ReadonlySet<string> = new Set()): ChatMessage[] {
  let next = [...current];
  const oldestLoadedTime = current.length ? Math.min(...current.map(message => message.timestamp)) : null;
  const loadedTurns = new Set(current.map(message => message.turnId).filter(Boolean));
  for (const raw of page.messages) {
    const message = normalizeMessage(raw);
    if (!message) continue;
    // Recovery is not history pagination: do not resurrect older, unloaded turns.
    if (oldestLoadedTime !== null && message.timestamp < oldestLoadedTime
      && !loadedTurns.has(message.turnId) && !current.some(item => item.id === message.id)) continue;
    const index = next.findIndex(item => item.id === message.id || (message.turnId && item.turnId === message.turnId && item.role === message.role && (
      (message.role === 'assistant' && item.id === `assistant-${message.turnId}`)
      // The caller supplies only turns sent by this actor in this exact scope.
      || (message.role === 'user' && optimisticUserTurns.has(message.turnId) && item.id === `user-${message.turnId}`)
    )));
    if (index < 0) next.push(message);
    else next[index] = { ...next[index], ...message, parts: preserveTerminalCanvasParts(next[index].parts, message.parts ?? next[index].parts), uiEvents: message.uiEvents ?? next[index].uiEvents };
  }
  const assistant = (turnId: string) => {
    let index = next.findIndex(item => item.turnId === turnId && item.role === 'assistant');
    if (index < 0) {
      const origin = next.find(item => item.turnId === turnId);
      if (!origin) return -1;
      next.push({ id: `assistant-${turnId}`, role: 'assistant', turnId, text: '', timestamp: origin.timestamp });
      index = next.length - 1;
    }
    return index;
  };
  for (const row of page.events) {
    const index = assistant(row.turn_id);
    if (index < 0) continue;
    const message = next[index];
    let event: Record<string, unknown> = { ...row.payload, recovery_event_id: row.id, turn_id: row.turn_id };
    const events = (message.uiEvents ?? []).filter(value => (value as { recovery_event_id?: number })?.recovery_event_id !== row.id);
    events.push(event);
    let parts = message.parts;
    if (row.type === 'assistant.message_parts' && Array.isArray(row.payload.parts)) {
      // This is the latest committed mutable snapshot, even when its clock is older.
      parts = preserveTerminalCanvasParts(message.parts, normalizeMessage({ ...message, parts: row.payload.parts })?.parts);
    } else {
      const type = row.type.startsWith('assistant.clarification.') ? 'clarification'
        : row.type.startsWith('skill_studio.') ? 'skill_studio' : null;
      if (type) {
        const id = `recovery-event-${row.id}`;
        const identity = typeof event.bridge_key === 'string' ? canonicalBridgeRequestId(event.bridge_key) : null;
        const previous = parts?.find(part => part.type === 'clarification' && identity && typeof (part.event as { bridge_key?: string })?.bridge_key === 'string' && canonicalBridgeRequestId((part.event as { bridge_key: string }).bridge_key) === identity);
        if (type === 'clarification' && previous && previous.type !== 'text') event = mergeClarificationPresentation(previous.event as Record<string, unknown>, event);
        parts = (parts ?? []).filter(part => part.id !== id && !(identity && part.type !== 'text' && typeof (part.event as { bridge_key?: string })?.bridge_key === 'string' && canonicalBridgeRequestId((part.event as { bridge_key: string }).bridge_key) === identity));
        parts.push({ id, type, event } as ChatMessagePart);
      }
    }
    next[index] = { ...message, parts, uiEvents: events };
  }
  for (const receipt of page.receipts) {
    const index = assistant(receipt.turn_id);
    if (index < 0) continue;
    const message = next[index];
    const id = `bridge-receipt-${receipt.request_id}`;
    const type = receipt.kind === 'canvas_command' ? 'canvas_feedback' : receipt.kind === 'canvas_context' ? 'canvas_context' : receipt.kind === 'clarification_event' ? 'clarification' : 'skill_studio';
    const identity = (part: ChatMessagePart) => {
      if (part.type === 'text') return null;
      const event = part.event as { bridge_key?: string; bridgeKey?: string; key?: string };
      const key = event?.bridge_key ?? event?.bridgeKey ?? event?.key?.replace(/^bridge:/, '');
      return typeof key === 'string' ? canonicalBridgeRequestId(key) : null;
    };
    const receiptRunId = canvasFeedbackRunId(receipt.result);
    const previous = message.parts?.find(part => {
      if (part.type !== type || identity(part) !== canonicalBridgeRequestId(receipt.request_id)) return false;
      if (type !== 'canvas_feedback') return true;
      const priorRunId = canvasFeedbackRunId(part.event as Record<string, unknown>);
      return !priorRunId || !receiptRunId || priorRunId === receiptRunId;
    });
    const priorEvent = previous && previous.type !== 'text' && previous.event && typeof previous.event === 'object'
      ? previous.event as Record<string, unknown> : {};
    if (type === 'canvas_feedback' && canvasFeedbackTerminal(priorEvent) && !canvasFeedbackTerminal(receipt.result)) continue;
    const partId = previous?.id ?? (type === 'canvas_feedback' && receiptRunId ? `${id}:${receiptRunId}` : id);
    let event = {
      ...priorEvent, ...receipt.result,
      request_id: receipt.request_id, bridge_key: priorEvent.bridge_key ?? priorEvent.bridgeKey ?? receipt.request_id,
      bridgeKey: priorEvent.bridgeKey ?? priorEvent.bridge_key ?? receipt.request_id,
      turn_id: receipt.turn_id, turnId: receipt.turn_id,
      ...(receiptRunId ? { workflowRunId: receiptRunId } : {}),
      terminal_status: receipt.status,
      ...(receipt.kind === 'canvas_command' ? {
        type: 'canvas.command.result', key: typeof priorEvent.key === 'string' ? priorEvent.key : `bridge:${receipt.request_id}`,
        applied: receipt.result.applied_count ?? 0, openedUiActions: receipt.result.opened_ui_actions ?? 0,
        errors: receipt.result.errors ?? [], commandResults: receipt.result.command_results ?? [],
        cancelled: receipt.status === 'cancelled', plans: [],
      } : receipt.kind === 'canvas_context' ? {
        type: 'canvas.context.result', key: `bridge:${receipt.request_id}`, turnId: receipt.turn_id,
        bridgeKey: receipt.request_id, labels: ['画布上下文'], errors: receipt.result.errors ?? [],
        status: receipt.status === 'applied' ? 'done' : 'failed',
      } : receipt.kind === 'clarification_event' ? {
        type: 'assistant.clarification.request', submitted: receipt.status === 'applied' && receipt.result.ok === true && receipt.result.status === 'clarification_frontend_result' && receipt.result.clarification_status !== 'pending_user_input',
      } : {
        type: receipt.result.draft ? 'skill_studio.draft' : 'skill_studio.questions', submitted: true,
      }),
    };
    if (receipt.kind === 'clarification_event') event = mergeClarificationPresentation(priorEvent, event) as typeof event;
    next[index] = { ...message, parts: [...(message.parts ?? []).filter(part => part.id !== partId && part !== previous), { id: partId, type, event } as ChatMessagePart] };
  }
  const owned = new Map<string, string>();
  for (const frame of page.frames) {
    if ('request_id' in frame && typeof frame.request_id === 'string' && 'bridge_key' in frame && typeof frame.bridge_key === 'string') owned.set(frame.request_id, frame.bridge_key);
  }
  const bindPending = (event: unknown) => {
    if (!event || typeof event !== 'object') return event;
    const value = event as Record<string, unknown>;
    const key = typeof value.bridge_key === 'string' ? value.bridge_key : typeof value.bridgeKey === 'string' ? value.bridgeKey : null;
    if (!key || value.submitted === true) return event;
    const alias = owned.get(canonicalBridgeRequestId(key));
    if (!alias) return null;
    return { ...value, ...('bridgeKey' in value ? { bridgeKey: alias } : { bridge_key: alias }) };
  };
  next = next.map(message => ({
    ...message,
    parts: message.parts?.flatMap(part => {
      if (!['canvas_approval', 'clarification', 'skill_studio'].includes(part.type) || part.type === 'text') return [part];
      const event = bindPending(part.event);
      return event ? [{ ...part, event } as ChatMessagePart] : [];
    }),
    uiEvents: message.uiEvents?.flatMap(event => {
      const type = (event as { type?: string })?.type ?? '';
      if (!type.startsWith('assistant.clarification.') && !type.startsWith('skill_studio.')) return [event];
      const bound = bindPending(event);
      return bound ? [bound] : [];
    }),
  }));
  return next.sort((a, b) => a.timestamp - b.timestamp);
}

/** Closed chat still services canvas tools; interactive cards mount the existing UI. */
export function deliverClosedCanvasRecovery(page: BridgeRecoveryPage, handlers: {
  command: (frame: ServerFrame) => void;
  context: (frame: ServerFrame) => void;
  openChat: () => void;
}) {
  for (const frame of page.frames) {
    if (frame.type === 'canvas.command') handlers.command(frame);
    else if (frame.type === 'canvas.context.request') handlers.context(frame);
    else if (frame.type === 'assistant.clarification.event' || frame.type === 'skill_studio.event') handlers.openChat();
  }
}
const contextAliases = new Set<string>();
export function claimContextRecoveryAlias(alias: string): boolean {
  if (contextAliases.has(alias)) return false;
  contextAliases.add(alias);
  if (contextAliases.size > 1000) contextAliases.delete(contextAliases.values().next().value!);
  return true;
}

const contextResults = new Map<string, { actor: string | null; result: Record<string, unknown> }>();
export function rememberContextRecoveryResult(result: Record<string, unknown>, actor: string | null) {
  if (typeof result.bridge_key !== 'string') return;
  contextResults.set(result.bridge_key, { actor, result });
  if (contextResults.size > 1000) contextResults.delete(contextResults.keys().next().value!);
}
export function contextRecoveryResult(alias: string, actor: string | null): Record<string, unknown> | null {
  const receipt = contextResults.get(alias);
  return receipt?.actor === actor ? receipt.result : null;
}


/** Each scope owns its timer and at most one bounded drain; slow peers never block it. */
export function startIndependentBridgeRecovery(options: {
  scopeIds: () => string[];
  createRecovery: (scopeId: string) => ReturnType<typeof createBackgroundBridgeRecovery>;
}) {
  type Owner = { recovery: ReturnType<typeof createBackgroundBridgeRecovery>; running: boolean; timer?: ReturnType<typeof setTimeout> };
  const owners = new Map<string, Owner>();
  let stopped = false;
  const stopOwner = (owner: Owner) => { clearTimeout(owner.timer); owner.recovery.stop(); };
  const tick = (scope: string, owner: Owner) => {
    if (stopped || owner.running) return;
    clearTimeout(owner.timer);
    owner.running = true;
    void owner.recovery.tick().finally(() => {
      owner.running = false;
      if (!stopped && owners.get(scope) === owner) owner.timer = setTimeout(() => tick(scope, owner), 10_000);
    });
  };
  const reconcile = (force: boolean) => {
    if (stopped) return;
    const scopes = new Set(options.scopeIds());
    for (const [scope, owner] of owners) {
      if (!scopes.has(scope)) { stopOwner(owner); owners.delete(scope); }
    }
    for (const scope of scopes) {
      let owner = owners.get(scope);
      const fresh = !owner;
      if (!owner) {
        owner = { recovery: options.createRecovery(scope), running: false };
        owners.set(scope, owner);
      }
      if (fresh || force) tick(scope, owner);
    }
  };
  const membershipTimer = setInterval(() => reconcile(false), 10_000);
  reconcile(false);
  return {
    wake() { reconcile(true); },
    stop() { stopped = true; clearInterval(membershipTimer); for (const owner of owners.values()) stopOwner(owner); owners.clear(); },
  };
}
