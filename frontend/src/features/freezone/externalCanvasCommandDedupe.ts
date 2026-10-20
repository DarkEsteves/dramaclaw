import { canonicalBridgeRequestId } from "@/features/superchat/backgroundBridgeRecovery";
import type { ServerFrame } from "@/features/superchat/types";
import {
  readCanvasCommandReceipt,
  bindCanvasCommandCallbackAlias,
  type CanvasCommandToolResultPayload,
} from "@/features/freezone/canvasCommandToolResult";

type ExternalCanvasCommandClaim = {
  externalMcpCommand: boolean;
  bridgeKey: string | null;
  accepted: boolean;
  terminalReceipt?: CanvasCommandToolResultPayload;
};

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

/**
 * Realtime websocket delivery and reconnect polling can observe the same pending
 * external-MCP bridge file. Claim its bridge key in one synchronous place so
 * only the first delivery reaches the approval flow. Hermes frames do not carry
 * the external marker and intentionally bypass this guard.
 */
export function claimExternalCanvasCommand(
  seenBridgeKeys: Set<string>,
  frame: ServerFrame,
  explicitlyExternal = false,
): ExternalCanvasCommandClaim {
  const frameRecord = record(frame);
  const envelope = record(frameRecord?.envelope);
  const externalMcpCommand =
    explicitlyExternal ||
    frameRecord?.external_mcp_command === true ||
    frameRecord?.externalMcpCommand === true ||
    envelope?.external_mcp_command === true ||
    envelope?.externalMcpCommand === true;
  const bridgeKey =
    typeof frameRecord?.bridge_key === "string"
      ? frameRecord.bridge_key
      : typeof frameRecord?.bridgeKey === "string"
        ? frameRecord.bridgeKey
        : null;

  const requestId = typeof frameRecord?.request_id === "string" ? frameRecord.request_id : bridgeKey ? canonicalBridgeRequestId(bridgeKey) : null;
  const owned = Boolean(requestId && bridgeKey && bridgeKey.includes(".executor."));
  if ((!externalMcpCommand && !owned) || !bridgeKey || !requestId) {
    return { externalMcpCommand, bridgeKey, accepted: true };
  }
  bindCanvasCommandCallbackAlias(bridgeKey);
  const terminalReceipt = readCanvasCommandReceipt(bridgeKey);
  const scopeMatches = !terminalReceipt || ["project_id", "canvas_id", "agent_id"].every((key) =>
    frameRecord?.[key] == null || (terminalReceipt as unknown as Record<string, unknown>)[key] === frameRecord[key]);
  if (terminalReceipt && scopeMatches) {
    seenBridgeKeys.add(requestId);
    return { externalMcpCommand, bridgeKey, accepted: false, terminalReceipt };
  }
  if (seenBridgeKeys.has(requestId)) {
    return { externalMcpCommand, bridgeKey, accepted: false };
  }
  seenBridgeKeys.add(requestId);
  return { externalMcpCommand, bridgeKey, accepted: true };
}

/**
 * Seen keys normally suppress reconnect polling. A stored terminal receipt is
 * different: the command was executed, but the server may not have received
 * its result, so omit that key and let the server redeliver it for replay.
 */
export function confirmedExternalCanvasCommandKeys(
  seenBridgeKeys: Set<string>,
): string[] {
  return Array.from(seenBridgeKeys).filter(
    (bridgeKey) => readCanvasCommandReceipt(bridgeKey) === null,
  );
}
