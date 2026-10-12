// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { CANVAS_NODE_TYPES, isVideoNode } from "@/features/canvas/domain/canvasNodes";
import {
  isVideoModeSupportedByModel,
  videoModelDefaultGenerateAudio,
} from "@/features/canvas/nodes/shared/videoModelCapabilities";
import type { ModelOption } from "@/features/canvas/ui/ProviderModelPicker";
import { useCanvasStore } from "@/stores/canvasStore";

/** Use catalog purpose rather than the display name or gateway alias. */
export function findDepthVideoModel(models: readonly ModelOption[]): ModelOption | undefined {
  return models.find((model) =>
    model.processingKind?.trim().toLowerCase() === "depth" &&
    isVideoModeSupportedByModel("videoEdit", model) &&
    model.referenceVideoMax !== 0
  );
}

/** Prepare an ordinary video-edit node; the user submits it through the existing form. */
export function createDepthVideoNode(
  sourceNodeId: string,
  model: ModelOption,
  displayName: string,
): string | null {
  const store = useCanvasStore.getState();
  const source = store.nodes.find((node) => node.id === sourceNodeId);
  if (!source || !isVideoNode(source) || !source.data.videoUrl?.trim()) return null;
  if (!findDepthVideoModel([model])) return null;

  const nodeId = store.addNode(
    CANVAS_NODE_TYPES.video,
    store.findNodePosition(source.id, 580, 380),
    {
      displayName,
      model: model.id,
      genMode: "videoEdit",
      prompt: "",
      modelParams: {},
      generateAudio: videoModelDefaultGenerateAudio(model),
      aspectRatio: source.data.aspectRatio || "16:9",
      videoUrl: null,
      isGenerating: false,
      count: 1,
      referenceOrder: [source.id],
    },
  );
  if (!store.addEdge(source.id, nodeId)) {
    store.deleteNode(nodeId);
    return null;
  }
  store.onNodesChange([
    ...store.nodes.filter((node) => node.selected).map((node) => ({
      id: node.id, type: "select" as const, selected: false,
    })),
    { id: nodeId, type: "select", selected: true },
  ]);
  store.setSelectedNode(nodeId);
  store.requestFocusNode(nodeId);
  return nodeId;
}
