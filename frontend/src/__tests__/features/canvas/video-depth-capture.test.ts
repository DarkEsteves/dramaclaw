// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { beforeEach, describe, expect, it } from "vitest";
import { CANVAS_NODE_TYPES, type CanvasNode } from "@/features/canvas/domain/canvasNodes";
import { createDepthVideoNode, findDepthVideoModel } from "@/features/canvas/application/videoDepthCapture";
import type { ModelOption } from "@/features/canvas/ui/ProviderModelPicker";
import { useCanvasStore } from "@/stores/canvasStore";

const depth: ModelOption = {
  id: "depth-catalog-id", catalogId: "depth-catalog-id", providerId: "newapi",
  apiModel: "newapi_Dav", label: "Renamed depth model", processingKind: "depth",
  supportedModes: ["video_edit"], referenceVideoMax: 1, supportsGenerateAudio: false,
};

function seedSource(videoUrl: string | null = "/static/source.mp4") {
  const source = {
    id: "source", type: CANVAS_NODE_TYPES.video, position: { x: 0, y: 0 }, selected: true,
    data: { videoUrl, aspectRatio: "9:16", displayName: "原视频", durationMs: 7000 },
  } as CanvasNode;
  useCanvasStore.getState().setCanvasData([source], []);
  return source;
}

describe("depth capture shortcut", () => {
  beforeEach(() => seedSource());

  it("finds the configured purpose independently of model names, requiring video-edit capability", () => {
    expect(findDepthVideoModel([{ ...depth, processingKind: undefined }, depth])).toBe(depth);
    expect(findDepthVideoModel([{ ...depth, supportedModes: ["text_to_video"] }])).toBeUndefined();
    expect(findDepthVideoModel([{ ...depth, referenceVideoMax: 0 }])).toBeUndefined();
    expect(findDepthVideoModel([])).toBeUndefined();
  });

  it("prepares and focuses an ordinary video-edit node without changing the source or generating", () => {
    const sourceData = { ...useCanvasStore.getState().nodes[0].data };
    const id = createDepthVideoNode("source", depth, "深度动作捕捉");
    expect(id).toBeTruthy();
    const state = useCanvasStore.getState();
    const created = state.nodes.find((node) => node.id === id);
    expect(created?.type).toBe(CANVAS_NODE_TYPES.video);
    expect(created?.data).toMatchObject({
      model: depth.id, genMode: "videoEdit", prompt: "", modelParams: {},
      generateAudio: false, videoUrl: null, isGenerating: false, aspectRatio: "9:16",
    });
    expect(created?.data).not.toHaveProperty("isUpscaleNode");
    expect(created?.selected).toBe(true);
    expect(state.edges).toEqual([expect.objectContaining({ source: "source", target: id })]);
    expect(state.selectedNodeId).toBe(id);
    expect(state.pendingFocusNodeId).toBe(id);
    expect(state.nodes.find((node) => node.id === "source")?.data).toEqual(sourceData);
  });

  it("does nothing when source media is absent or the model is incompatible", () => {
    seedSource(null);
    expect(createDepthVideoNode("source", depth, "深度动作捕捉")).toBeNull();
    expect(createDepthVideoNode("removed-source", depth, "深度动作捕捉")).toBeNull();
    seedSource();
    expect(createDepthVideoNode("source", { ...depth, supportedModes: ["text_to_video"] }, "深度动作捕捉"))
      .toBeNull();
    expect(useCanvasStore.getState().nodes).toHaveLength(1);
    expect(useCanvasStore.getState().edges).toHaveLength(0);
  });
});
