import { beforeEach, describe, expect, it } from "vitest";
import {
  claimExternalCanvasCommand,
  confirmedExternalCanvasCommandKeys,
} from "@/features/freezone/externalCanvasCommandDedupe";

const RECEIPTS_KEY = "dramaclaw.canvas-command-receipts.v1";

describe("external canvas command delivery dedupe", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("accepts only the first websocket/poll delivery of one external MCP bridge", () => {
    const seen = new Set<string>();
    const liveFrame = {
      type: "canvas.command" as const,
      turn_id: "turn-live",
      bridge_key: "bridge-1",
      envelope: {
        schema_version: "canvas_chat_commands.v1",
        external_mcp_command: true,
        commands: [{ type: "create_node" }],
      },
    };
    const polledFrame = {
      ...liveFrame,
      turn_id: "external-agent:bridge-1",
    };

    expect(claimExternalCanvasCommand(seen, liveFrame)).toMatchObject({
      externalMcpCommand: true,
      bridgeKey: "bridge-1",
      accepted: true,
    });
    expect(claimExternalCanvasCommand(seen, polledFrame, true)).toMatchObject({
      externalMcpCommand: true,
      bridgeKey: "bridge-1",
      accepted: false,
    });
  });

  it("lets the polling delivery win the same race exactly once", () => {
    const seen = new Set<string>();
    const frame = {
      type: "canvas.command" as const,
      turn_id: "external-agent:bridge-2",
      bridge_key: "bridge-2",
      envelope: { schema_version: "canvas_chat_commands.v1", commands: [] },
    };

    expect(claimExternalCanvasCommand(seen, frame, true).accepted).toBe(true);
    expect(claimExternalCanvasCommand(seen, frame, true).accepted).toBe(false);
  });

  it("does not dedupe Hermes commands without the external MCP marker", () => {
    const seen = new Set<string>();
    const frame = {
      type: "canvas.command" as const,
      turn_id: "turn-hermes",
      bridge_key: "hermes-bridge",
      envelope: { schema_version: "canvas_chat_commands.v1", commands: [] },
    };

    expect(claimExternalCanvasCommand(seen, frame)).toEqual({
      externalMcpCommand: false,
      bridgeKey: "hermes-bridge",
      accepted: true,
    });
    expect(claimExternalCanvasCommand(seen, frame).accepted).toBe(true);
    expect(seen).toEqual(new Set());
  });

  it("returns a persisted terminal receipt instead of reapplying after reconnect", () => {
    const receipt = {
      type: "canvas.command.result",
      bridge_key: "bridge-reconnect",
      project_id: "project-a",
      canvas_id: "canvas-a",
      tool_call_status: "completed",
      canvas_apply_status: "applied",
      applied: true,
      cancelled: false,
      errors: [],
      applied_count: 1,
      opened_ui_actions: 0,
      created_node_ids: ["node-a"],
      command_results: [],
      message: "done",
    };
    window.localStorage.setItem(RECEIPTS_KEY, JSON.stringify({
      "bridge-reconnect": { storedAt: Date.now(), payload: receipt },
    }));
    const frame = {
      type: "canvas.command" as const,
      turn_id: "external-agent:bridge-reconnect",
      bridge_key: "bridge-reconnect",
      envelope: {
        schema_version: "canvas_chat_commands.v1",
        external_mcp_command: true,
        commands: [{ type: "create_node" }],
      },
    };

    const seen = new Set(["bridge-reconnect", "bridge-confirmed"]);
    expect(confirmedExternalCanvasCommandKeys(seen)).toEqual(["bridge-confirmed"]);
    expect(claimExternalCanvasCommand(seen, frame)).toMatchObject({
      accepted: false,
      bridgeKey: "bridge-reconnect",
      terminalReceipt: receipt,
    });
  });
  it("dedupes owned command across executor transfer and rebinds genuine receipt", () => {
    const seen = new Set<string>();
    const frame = { type: "canvas.command" as const, request_id: "request-a", bridge_key: "request-a.executor.old", project_id: "p", canvas_id: "c", agent_id: "main", envelope: { commands: [] } };
    expect(claimExternalCanvasCommand(seen, frame).accepted).toBe(true);
    expect(claimExternalCanvasCommand(seen, { ...frame, bridge_key: "request-a.executor.new" }).accepted).toBe(false);
    const receipt = { bridge_key: frame.bridge_key, project_id: "p", canvas_id: "c", agent_id: "main", canvas_apply_status: "applied", command_results: [{ native_id: "native-workflow" }] };
    window.localStorage.setItem(RECEIPTS_KEY, JSON.stringify({ [frame.bridge_key]: { storedAt: Date.now(), payload: receipt } }));
    expect(claimExternalCanvasCommand(new Set(), { ...frame, bridge_key: "request-a.executor.new" })).toMatchObject({ accepted: false, terminalReceipt: { ...receipt, bridge_key: "request-a.executor.new" } });
  });
  it("does not adopt a receipt from another scope or a nonterminal submission", () => {
    const frame = { type: "canvas.command" as const, request_id: "request-a", bridge_key: "request-a.executor.new", project_id: "p", canvas_id: "c", agent_id: "main" };
    const stored = (payload: object) => window.localStorage.setItem(RECEIPTS_KEY, JSON.stringify({ "request-a.executor.old": { storedAt: Date.now(), payload } }));
    stored({ bridge_key: "request-a.executor.old", project_id: "peer", canvas_id: "c", agent_id: "main", canvas_apply_status: "applied" });
    expect(claimExternalCanvasCommand(new Set(), frame).accepted).toBe(true);
    stored({ bridge_key: "request-a.executor.old", project_id: "p", canvas_id: "c", agent_id: "main", canvas_apply_status: "accepted" });
    expect(claimExternalCanvasCommand(new Set(), frame).accepted).toBe(true);
  });

});

it('does not replay a real receipt saved by another browser actor', async () => {
  const { useAuthStore } = await import('@/stores/auth-store');
  const { readCanvasCommandReceipt } = await import('@/features/freezone/canvasCommandToolResult');
  const prior = useAuthStore.getState().username;
  useAuthStore.setState({ username: 'second-actor' });
  try {
    window.localStorage.setItem(RECEIPTS_KEY, JSON.stringify({ 'actor-command': { storedAt: Date.now(), actor: 'first-actor', payload: { bridge_key: 'actor-command.executor.first', canvas_apply_status: 'applied' } } }));
    expect(readCanvasCommandReceipt('actor-command.executor.second')).toBeNull();
  } finally { useAuthStore.setState({ username: prior }); }
});
