// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CANVAS_NODE_TYPES, type CanvasNode } from "@/features/canvas/domain/canvasNodes";
import { NodeActionToolbar } from "@/features/canvas/ui/NodeActionToolbar";
import type { ModelOption } from "@/features/canvas/ui/ProviderModelPicker";
import { useCanvasStore } from "@/stores/canvasStore";
import { zhT } from "../../helpers/i18n-fixtures";

const catalog = vi.hoisted(() => ({ models: [] as ModelOption[], isLoading: false, isFallback: false, error: null }));
const submit = vi.hoisted(() => vi.fn());
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: zhT, i18n: { language: "zh" } }) }));
vi.mock("@xyflow/react", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@xyflow/react")>()),
  NodeToolbar: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
vi.mock("@/lib/queries/generation-credit-cost", () => ({
  useGenerationCreditCost: () => ({ data: undefined, error: null }),
}));
vi.mock("@/features/canvas/hooks/useFreezoneVideoModels", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/features/canvas/hooks/useFreezoneVideoModels")>()),
  useFreezoneVideoModels: () => catalog,
  readFreezoneVideoModels: () => catalog.models,
}));
vi.mock("@/api/ops", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/ops")>()),
  submitFreezoneVideoEdit: submit,
}));

const depth: ModelOption = {
  id: "depth-id", providerId: "newapi", apiModel: "newapi_Dav", label: "Depth model",
  processingKind: "depth", supportedModes: ["video_edit"], referenceVideoMax: 1,
};
function showToolbar(videoUrl: string | null = "/static/source.mp4") {
  const node = { id: "source", type: CANVAS_NODE_TYPES.video, position: { x: 0, y: 0 },
    data: { videoUrl, aspectRatio: "16:9", displayName: "视频" } } as CanvasNode;
  useCanvasStore.getState().setCanvasData([node], []);
  const noop = vi.fn();
  render(<NodeActionToolbar node={node} onOpenMultiAngleEditor={noop} onOpenLightEditor={noop}
    onOpenDerivedMedia={noop} onOpenScene360={noop} onOpenUpscale={noop} onOpenOutpaint={noop}
    onSpawnGridActionNode={noop} onOpenRedraw={noop} onOpenErase={noop} onOpenRotate={noop} />);
  return screen.getByRole("button", { name: "深度动作捕捉" });
}

describe("depth toolbar entry", () => {
  beforeEach(() => {
    catalog.models = [depth];
    catalog.isLoading = false;
    catalog.isFallback = false;
    submit.mockClear();
  });
  it("creates the configured video-edit node on click without submitting", () => {
    const button = showToolbar();
    expect(button).toHaveAttribute("aria-disabled", "false");
    fireEvent.click(button);
    const state = useCanvasStore.getState();
    expect(state.nodes).toHaveLength(2);
    expect(state.nodes[1].data).toMatchObject({ model: depth.id, genMode: "videoEdit", prompt: "" });
    expect(state.edges).toHaveLength(1);
    expect(submit).not.toHaveBeenCalled();
  });
  it("disables the entry without a video and shows the reason", () => {
    const button = showToolbar(null);
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).toHaveAttribute("title", "请先上传视频");
    fireEvent.click(button);
    expect(useCanvasStore.getState().nodes).toHaveLength(1);
  });
  it("disables the entry when a depth model is not configured", () => {
    catalog.models = [];
    const button = showToolbar();
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).toHaveAttribute("title", "未配置可用的深度视频模型，请联系管理员");
    fireEvent.click(button);
    expect(useCanvasStore.getState().nodes).toHaveLength(1);
  });
});
