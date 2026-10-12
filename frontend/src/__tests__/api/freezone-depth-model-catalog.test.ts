// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiCall } from "@/api/client";
import { fetchFreezoneVideoModels } from "@/api/ops";

vi.mock("@/api/client", () => ({ apiCall: vi.fn(), apiClient: {} }));

describe("depth model catalog metadata", () => {
  beforeEach(() => vi.mocked(apiCall).mockReset());

  it.each(["processingKind", "processing_kind"])("preserves %s without changing model identity or parameter mappings", async (key) => {
    const request = { endpoint: "video/generations", parameters: [
      { key: "colormap", label: "颜色", control: "select", options: ["grayscale"],
        default: "grayscale", requestPath: "metadata.colormap" },
    ] };
    vi.mocked(apiCall).mockResolvedValue({ ok: true, data: [{
      id: "catalog-depth", catalogId: "catalog-depth", apiModel: "newapi_Dav",
      providerId: "newapi", label: "Custom depth model", [key]: "depth",
      supportedModes: ["video_edit"], request,
    }] });
    const models = await fetchFreezoneVideoModels("project-1");
    expect(models[0]).toMatchObject({
      id: "catalog-depth", apiModel: "newapi_Dav", processingKind: "depth",
      supportedModes: ["video_edit"], request,
    });
    expect(apiCall).toHaveBeenCalledWith("projects/project-1/freezone/video/models");
  });
});
