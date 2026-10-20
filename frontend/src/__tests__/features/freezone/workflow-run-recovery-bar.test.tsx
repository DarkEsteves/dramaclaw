import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  getFreezoneCanvas,
  listFreezoneWorkflowRuns,
  updateFreezoneWorkflowRun,
  type FreezoneWorkflowRun,
} from "@/api/canvas";
import { applyCanvasChatCommandsAsync, FREEZONE_WORKFLOW_RUN_UPDATED_EVENT } from "@/features/freezone/canvasChatCommands";
import {
  recoverableWorkflowNodeIds,
  resumableWorkflowNodeIds,
  staleResumableWorkflowRunIds,
  WorkflowRunRecoveryBar,
} from "@/features/freezone/WorkflowRunRecoveryBar";
import { CANVAS_NODE_TYPES } from "@/features/canvas/domain/canvasNodes";
import { useCanvasStore } from "@/stores/canvasStore";
import { useTaskCenterStore } from "@/task-center/store";
import { fetchRemixArtifact } from "@/api/remix";
import { buildProductionWorkflowAction, markProductionWorkflowSubmission } from "@/features/canvas/remix/productionWorkflowRuntime";
import { flushProductionPlanV2AutoSave } from "@/features/canvas/remix/productionPlanV2AutoSave";
import { applyRemoteFreezoneCanvas, getFreezoneCanvasBaseline } from "@/features/freezone/canvasSyncRuntime";
import { normalizeCanvasData } from "@/stores/canvasStore";
import type { ProductionPlanV2 } from "@/features/canvas/remix/productionPlanV2Model";

const failedRun: FreezoneWorkflowRun = {
  schema_version: "freezone_workflow_run.v1",
  run_id: "run-failed",
  project_id: "project-a",
  canvas_id: "canvas-a",
  status: "failed",
  resumable: true,
  created_at: "2026-07-21T00:00:00Z",
  started_at: "2026-07-21T00:00:00Z",
  updated_at: "2026-07-21T00:01:00Z",
  actions: [
    { node_id: "image-1", action: "generate_image", status: "completed" },
    { node_id: "video-1", action: "generate_video", status: "failed" },
    { node_id: "compose-1", action: "open_video_compose_modal", status: "blocked" },
  ],
};

vi.mock("@/api/canvas", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/canvas")>();
  return {
    ...actual,
    getFreezoneCanvas: vi.fn(),
    listFreezoneWorkflowRuns: vi.fn(),
    updateFreezoneWorkflowRun: vi.fn(),
  };
});

vi.mock("@/features/freezone/canvasChatCommands", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/features/freezone/canvasChatCommands")>();
  return {
    ...actual,
    applyCanvasChatCommandsAsync: vi.fn(),
  };
});

vi.mock("@/api/remix", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/api/remix")>(),
  fetchRemixArtifact: vi.fn(),
}));

vi.mock("@/features/freezone/canvasSyncRuntime", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/features/freezone/canvasSyncRuntime")>(),
  captureFreezoneCanvasScope: vi.fn(() => () => true),
  getFreezoneCanvasBaseline: vi.fn(() => ({ nodes: structuredClone(useCanvasStore.getState().nodes),
    edges: structuredClone(useCanvasStore.getState().edges), revision: 1 })),
  applyRemoteFreezoneCanvas: vi.fn((_project, _canvasId, remote, merge) => {
    const current = useCanvasStore.getState();
    const normalized = normalizeCanvasData(remote.nodes, remote.edges);
    const merged = merge(normalized.nodes, normalized.edges, current.nodes, current.edges);
    current.setCanvasData(merged.nodes, merged.edges);
    return true;
  }),
}));

vi.mock("@/features/canvas/remix/productionPlanV2AutoSave", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/features/canvas/remix/productionPlanV2AutoSave")>(),
  flushProductionPlanV2AutoSave: vi.fn(),
}));

describe("WorkflowRunRecoveryBar", () => {
  beforeEach(() => {
    useTaskCenterStore.getState().reset();
    useCanvasStore.getState().setCanvasData([
      {
        id: "image-1",
        type: CANVAS_NODE_TYPES.imageGen,
        position: { x: 0, y: 0 },
        data: {},
      },
      {
        id: "video-1",
        type: CANVAS_NODE_TYPES.video,
        position: { x: 400, y: 0 },
        data: {},
      },
      {
        id: "compose-1",
        type: CANVAS_NODE_TYPES.videoCompose,
        position: { x: 800, y: 0 },
        data: {},
      },
    ], []);
    vi.mocked(listFreezoneWorkflowRuns).mockReset();
    vi.mocked(getFreezoneCanvas).mockReset();
    vi.mocked(fetchRemixArtifact).mockReset();
    vi.mocked(getFreezoneCanvasBaseline).mockClear();
    vi.mocked(applyRemoteFreezoneCanvas).mockClear();
    vi.mocked(updateFreezoneWorkflowRun).mockReset();
    vi.mocked(applyCanvasChatCommandsAsync).mockReset();
    vi.mocked(flushProductionPlanV2AutoSave).mockReset().mockResolvedValue({ artifactId: 'plan', revision: 2 });
    vi.mocked(listFreezoneWorkflowRuns).mockResolvedValue({ runs: [failedRun] });
    vi.mocked(updateFreezoneWorkflowRun).mockResolvedValue({
      ...failedRun,
      status: "cancelled",
      resumable: false,
    });
    vi.mocked(applyCanvasChatCommandsAsync).mockResolvedValue({
      applied: 1,
      openedUiActions: 2,
      createdNodeIds: [],
      errors: [],
      commandResults: [],
    });
  });

  it("returns only unfinished workflow node ids", () => {
    expect(resumableWorkflowNodeIds(failedRun)).toEqual(["video-1", "compose-1"]);
    expect(recoverableWorkflowNodeIds(
      failedRun,
      new Set(["image-1", "video-1", "compose-1"]),
    )).toEqual(["video-1"]);
  });

  it("restores a completed editable plan after refresh without starting another task", async () => {
    useCanvasStore.getState().setCanvasData([{ id: 'plan-node', type: CANVAS_NODE_TYPES.script,
      position: { x: 0, y: 0 }, data: { remixRole: 'production_plan', processingAction: 'generate_production_plan_draft',
        processingPayload: { topic: '新片', analysis_context: { summary: '原片' } }, remixNodeRevision: 1 } }], []);
    vi.mocked(listFreezoneWorkflowRuns).mockResolvedValue({ runs: [{ ...failedRun, run_id: 'run-completed',
      status: 'completed', resumable: false, actions: [{ node_id: 'plan-node', action: 'generate_production_plan_draft',
        status: 'completed', artifact_status: 'valid', node_revision: 1,
        payload: { topic: '新片', analysis_context: { summary: '原片' } }, result_ref: { artifact_id: 'plan-artifact', revision: 1 } }] }] });
    const original = structuredClone(useCanvasStore.getState().nodes[0]);
    vi.mocked(getFreezoneCanvas).mockResolvedValue({ nodes: [{ ...original, data: { ...original.data,
      productionPlanV2Ref: { artifactId: 'plan-artifact', revision: 1 },
      productionPlanV2Draft: { schema_version: 2, goal: '新片', output: { width: 1920, height: 1080, fps_num: 24, fps_den: 1 },
        provenance: {}, beats: [], resources: [], placements: [], speech_takes: [] } } }], edges: [], revision: 2 } as never);
    render(<WorkflowRunRecoveryBar projectId="project-a" canvasId="canvas-a" />);
    await waitFor(() => expect(useCanvasStore.getState().nodes[0].data.productionPlanV2Ref).toEqual({ artifactId: 'plan-artifact', revision: 1 }));
    expect(applyCanvasChatCommandsAsync).not.toHaveBeenCalled();
    expect(fetchRemixArtifact).not.toHaveBeenCalled();
  });

  it("does not restore a fetched artifact into another project canvas with the same node ID", async () => {
    const oldNode = { id: 'plan-node', type: CANVAS_NODE_TYPES.script,
      position: { x: 0, y: 0 }, data: { remixRole: 'production_plan', processingAction: 'generate_production_plan_draft',
        processingPayload: { topic: '新片', analysis_context: { summary: '原片' } }, remixNodeRevision: 1 } };
    useCanvasStore.getState().setCanvasData([oldNode], []);
    vi.mocked(listFreezoneWorkflowRuns).mockResolvedValue({ runs: [{ ...failedRun, run_id: 'old-completed',
      status: 'completed', resumable: false, actions: [{ node_id: 'plan-node', action: 'generate_production_plan_draft',
        status: 'completed', artifact_status: 'valid', node_revision: 1,
        payload: { topic: '新片', analysis_context: { summary: '原片' } }, result_ref: { artifact_id: 'old-artifact', revision: 1 } }] }] });
    let releaseFetch!: (value: unknown) => void;
    vi.mocked(getFreezoneCanvas).mockImplementation(() => new Promise((resolve) => { releaseFetch = resolve as typeof releaseFetch; }));
    const view = render(<WorkflowRunRecoveryBar projectId="project-a" canvasId="canvas-a" />);
    await waitFor(() => expect(getFreezoneCanvas).toHaveBeenCalledWith('project-a', 'canvas-a'));

    view.rerender(<WorkflowRunRecoveryBar projectId="project-b" canvasId="canvas-b" />);
    useCanvasStore.getState().setCanvasData([{ ...oldNode, data: { ...oldNode.data, displayName: '新画布节点' } }], []);
    await act(async () => { releaseFetch({ nodes: [{ ...oldNode, data: { ...oldNode.data,
      productionPlanV2Ref: { artifactId: 'old-artifact', revision: 1 } } }], edges: [], revision: 2 }); });
    expect(useCanvasStore.getState().nodes[0].data.productionPlanV2Ref).toBeUndefined();
    expect(applyRemoteFreezoneCanvas).not.toHaveBeenCalled();
  });

  it("keeps a speech recovery alive while the same canvas refreshes its run list", async () => {
    const hash = 'a'.repeat(64);
    const draft: ProductionPlanV2 = { schema_version: 2, goal: '对话',
      output: { width: 1920, height: 1080, fps_num: 24, fps_den: 1 }, provenance: {},
      beats: [{ id: 'beat', purpose: '开场', duration_frames: 120, turns: [{ id: 'turn', speaker: '甲', text: '你好' }] }],
      resources: [{ id: 'media', node_id: 'video', media_kind: 'video', media_path: 'media/talk.mp4',
        media_hash: hash, duration_us: 4_000_000, has_audio: true }],
      placements: [{ id: 'visual', kind: 'visual', role: 'primary', track_id: 'visual', resource_id: 'media',
        start: { kind: 'beat', ref_id: 'beat', edge: 'start' }, end: { kind: 'beat', ref_id: 'beat', edge: 'end' } }],
      speech_takes: [{ id: 'take', beat_id: 'beat', resource_id: 'media', turn_ids: ['turn'], origin_placement_id: 'visual',
        source_in_us: 0, source_out_us: 4_000_000, speed_num: 1, speed_den: 1, lead_in_frames: 0 }] };
    useCanvasStore.getState().setCanvasData([{ id: 'plan-node', type: CANVAS_NODE_TYPES.script, position: { x: 0, y: 0 },
      data: { remixRole: 'production_plan', productionPlanV2Ref: { artifactId: 'plan', revision: 1 },
        productionPlanV2Draft: draft, productionPlanV2Dirty: false, processingAction: 'align_production_speech',
        processingPayload: { turn_id: 'turn', take_id: 'take', media_path: 'media/talk.mp4' }, remixNodeRevision: 1 } }], []);
    const state = useCanvasStore.getState();
    const descriptor = buildProductionWorkflowAction(state.nodes[0], state.nodes, state.edges)!;
    markProductionWorkflowSubmission(descriptor);
    const action = { node_id: 'plan-node', action: 'align_production_speech', status: 'completed' as const,
      artifact_status: 'valid' as const, node_revision: 1, payload: descriptor.payload,
      result_ref: { artifact_id: 'words', revision: 1 } };
    const completed = { ...failedRun, run_id: 'speech-completed', status: 'completed' as const,
      resumable: false, actions: [action] };
    vi.mocked(listFreezoneWorkflowRuns).mockResolvedValue({ runs: [completed] });
    const savedNode = structuredClone(useCanvasStore.getState().nodes[0]);
    let releaseFetch!: (value: unknown) => void;
    vi.mocked(getFreezoneCanvas).mockImplementation(() => new Promise((resolve) => { releaseFetch = resolve as typeof releaseFetch; }));
    render(<WorkflowRunRecoveryBar projectId="project-a" canvasId="canvas-a" />);
    await waitFor(() => expect(getFreezoneCanvas).toHaveBeenCalledWith('project-a', 'canvas-a'));

    act(() => { window.dispatchEvent(new CustomEvent(FREEZONE_WORKFLOW_RUN_UPDATED_EVENT, {
      detail: { projectId: 'project-a', canvasId: 'canvas-a', run: { ...completed, updated_at: '2026-07-21T00:02:00Z' } },
    })); });
    await act(async () => { releaseFetch({ nodes: [{ ...savedNode, data: { ...savedNode.data,
      productionWorkflowResultRef: { artifactId: 'words', revision: 1 } } }], edges: [], revision: 2 }); });
    await waitFor(() => expect(useCanvasStore.getState().nodes[0].data.productionWorkflowResultRef).toEqual({ artifactId: 'words', revision: 1 }));
    expect(fetchRemixArtifact).not.toHaveBeenCalled();
    expect(flushProductionPlanV2AutoSave).not.toHaveBeenCalled();
  });

  it("does not expose blocked downstream generation as another retry target", () => {
    const runWithBlockedGeneration: FreezoneWorkflowRun = {
      ...failedRun,
      actions: [
        { node_id: "video-1", action: "generate_video", status: "failed" },
        {
          node_id: "compose-1",
          action: "auto_compose_video",
          status: "blocked",
          error: "跳过 auto_compose_video：上游节点 video-1 未成功完成。",
        },
      ],
    };
    expect(recoverableWorkflowNodeIds(
      runWithBlockedGeneration,
      new Set(["video-1", "compose-1"]),
    )).toEqual(["video-1"]);
  });

  it("hides recovery records whose nodes were deleted from the canvas", async () => {
    useCanvasStore.getState().setCanvasData([], []);

    render(<WorkflowRunRecoveryBar projectId="project-a" canvasId="canvas-a" />);

    await waitFor(() => expect(listFreezoneWorkflowRuns).toHaveBeenCalled());
    expect(screen.queryByText("发现未完成的工作流")).not.toBeInTheDocument();
    await waitFor(() => expect(updateFreezoneWorkflowRun).toHaveBeenCalledWith(
      "project-a",
      "canvas-a",
      "run-failed",
      { status: "cancelled" },
    ));
  });

  it("identifies only runs whose unfinished nodes are all gone", () => {
    expect(staleResumableWorkflowRunIds([failedRun], new Set(["video-1"]))).toEqual([]);
    expect(staleResumableWorkflowRunIds([failedRun], new Set())).toEqual(["run-failed"]);
  });

  it("never auto-cancels an actively running workflow after its canvas results arrive", () => {
    const runningRun: FreezoneWorkflowRun = {
      ...failedRun,
      status: "running",
      runner_id: "canvas-runner:active",
    };
    useCanvasStore.getState().setCanvasData([
      {
        id: "image-1",
        type: CANVAS_NODE_TYPES.imageGen,
        position: { x: 0, y: 0 },
        data: { imageUrl: "/outputs/image.png" },
      },
      {
        id: "video-1",
        type: CANVAS_NODE_TYPES.video,
        position: { x: 400, y: 0 },
        data: { videoUrl: "/outputs/video.mp4" },
      },
      {
        id: "compose-1",
        type: CANVAS_NODE_TYPES.videoCompose,
        position: { x: 800, y: 0 },
        data: { resultVideoUrl: "/outputs/final.mp4" },
      },
    ], []);

    expect(staleResumableWorkflowRunIds(
      [runningRun],
      new Set(["image-1", "video-1", "compose-1"]),
    )).toEqual([]);
  });

  it("cancels a stale recovery record after its nodes later produce outputs", async () => {
    useCanvasStore.getState().setCanvasData([
      {
        id: "image-1",
        type: CANVAS_NODE_TYPES.imageGen,
        position: { x: 0, y: 0 },
        data: {},
      },
      {
        id: "video-1",
        type: CANVAS_NODE_TYPES.video,
        position: { x: 400, y: 0 },
        data: { videoUrl: "/outputs/video.mp4" },
      },
      {
        id: "compose-1",
        type: CANVAS_NODE_TYPES.videoCompose,
        position: { x: 800, y: 0 },
        data: { resultVideoUrl: "/outputs/final.mp4" },
      },
    ], []);

    render(<WorkflowRunRecoveryBar projectId="project-a" canvasId="canvas-a" />);

    await waitFor(() => expect(listFreezoneWorkflowRuns).toHaveBeenCalled());
    expect(screen.queryByText("发现未完成的工作流")).not.toBeInTheDocument();
    await waitFor(() => expect(updateFreezoneWorkflowRun).toHaveBeenCalledWith(
      "project-a",
      "canvas-a",
      "run-failed",
      { status: "cancelled" },
    ));
  });

  it("recovers a node when only a stale task handle remains", async () => {
    useCanvasStore.getState().setCanvasData([
      {
        id: "image-1",
        type: CANVAS_NODE_TYPES.imageGen,
        position: { x: 0, y: 0 },
        data: {},
      },
      {
        id: "video-1",
        type: CANVAS_NODE_TYPES.video,
        position: { x: 400, y: 0 },
        data: { isGenerating: false, generationTaskKey: "task-video-1" },
      },
      {
        id: "compose-1",
        type: CANVAS_NODE_TYPES.videoCompose,
        position: { x: 800, y: 0 },
        data: {},
      },
    ], []);

    render(<WorkflowRunRecoveryBar projectId="project-a" canvasId="canvas-a" />);

    await waitFor(() => expect(listFreezoneWorkflowRuns).toHaveBeenCalled());
    expect(await screen.findByText("发现未完成的工作流")).toBeInTheDocument();
  });

  it("hides but preserves a run while its generation task is active", async () => {
    useCanvasStore.getState().setCanvasData([
      {
        id: "image-1",
        type: CANVAS_NODE_TYPES.imageGen,
        position: { x: 0, y: 0 },
        data: {},
      },
      {
        id: "video-1",
        type: CANVAS_NODE_TYPES.video,
        position: { x: 400, y: 0 },
        data: { isGenerating: true, generationTaskKey: "task-video-1" },
      },
      {
        id: "compose-1",
        type: CANVAS_NODE_TYPES.videoCompose,
        position: { x: 800, y: 0 },
        data: {},
      },
    ], []);

    render(<WorkflowRunRecoveryBar projectId="project-a" canvasId="canvas-a" />);

    await waitFor(() => expect(listFreezoneWorkflowRuns).toHaveBeenCalled());
    expect(screen.queryByText("发现未完成的工作流")).not.toBeInTheDocument();
    expect(updateFreezoneWorkflowRun).not.toHaveBeenCalled();
  });

  it("does not present a healthy running workflow as recoverable", async () => {
    vi.mocked(listFreezoneWorkflowRuns).mockResolvedValueOnce({
      runs: [{ ...failedRun, status: "running" }],
    });

    render(<WorkflowRunRecoveryBar projectId="project-a" canvasId="canvas-a" />);

    await waitFor(() => expect(listFreezoneWorkflowRuns).toHaveBeenCalled());
    expect(screen.queryByText("发现未完成的工作流")).not.toBeInTheDocument();
  });

  it("reconciles a workflow immediately when its task-center state changes", async () => {
    const trackedRun: FreezoneWorkflowRun = {
      ...failedRun,
      actions: failedRun.actions.map((action) =>
        action.node_id === "video-1"
          ? { ...action, task_key: "task-video-1", status: "running" }
          : action
      ),
    };
    vi.mocked(listFreezoneWorkflowRuns).mockResolvedValue({ runs: [trackedRun] });
    render(<WorkflowRunRecoveryBar projectId="project-a" canvasId="canvas-a" />);
    await waitFor(() => expect(listFreezoneWorkflowRuns).toHaveBeenCalledTimes(1));
    await screen.findByText("发现未完成的工作流");

    act(() => {
      useTaskCenterStore.getState().upsert({
        task_key: "task-video-1",
        task_id: "task-id-1",
        task_type: "freezone_video",
        username: "alice",
        project: "demo",
        project_id: "project-a",
        episode: 0,
        beat_num: null,
        scope: "job-1",
        status: "completed",
        progress: 1,
        current_task: "completed",
        result: { video_url: "/outputs/video.mp4" },
        error: null,
        logs: [],
        created_at: "2026-07-21T00:00:00Z",
        updated_at: "2026-07-21T00:01:00Z",
        completed_at: "2026-07-21T00:01:00Z",
      });
    });

    await waitFor(() => expect(listFreezoneWorkflowRuns).toHaveBeenCalledTimes(2));
  });

  it("requires an explicit click before resuming unfinished nodes", async () => {
    render(<WorkflowRunRecoveryBar projectId="project-a" canvasId="canvas-a" />);

    expect(await screen.findByText("发现未完成的工作流")).toBeInTheDocument();
    expect(applyCanvasChatCommandsAsync).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "继续下游" }));

    await waitFor(() => {
      expect(applyCanvasChatCommandsAsync).toHaveBeenCalledWith(
        [expect.objectContaining({
          commands: [{
            type: "run_workflow",
            node_ids: ["video-1"],
            direction: "downstream",
            regenerate: false,
          }],
        })],
        { projectId: "project-a", canvasId: "canvas-a" },
      );
      expect(updateFreezoneWorkflowRun).not.toHaveBeenCalled();
    });
  });

  it("resumes a lease-interrupted run without regenerating finished outputs", async () => {
    // Issue #730: the lease expired while the image result was landing, so the
    // record still says running. Continuing must reuse that canvas output and
    // only resubmit the video that was never admitted.
    useCanvasStore.getState().updateNodeData("image-1", {
      imageUrl: "/static/project/image.png",
    });
    vi.mocked(listFreezoneWorkflowRuns).mockResolvedValue({
      runs: [{
        ...failedRun,
        run_id: "run-interrupted",
        status: "interrupted",
        actions: [
          { node_id: "image-1", action: "generate_image", status: "running" },
          { node_id: "video-1", action: "generate_video", status: "running" },
          { node_id: "compose-1", action: "open_video_compose_modal", status: "pending" },
        ],
      }],
    });
    render(<WorkflowRunRecoveryBar projectId="project-a" canvasId="canvas-a" />);

    expect(await screen.findByText("发现未完成的工作流")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "继续下游" }));

    await waitFor(() => {
      expect(applyCanvasChatCommandsAsync).toHaveBeenCalledWith(
        [expect.objectContaining({
          commands: [{
            type: "run_workflow",
            node_ids: ["video-1"],
            direction: "downstream",
            regenerate: false,
          }],
        })],
        { projectId: "project-a", canvasId: "canvas-a" },
      );
    });
  });

  it("keeps the old run resumable when a resume attempt cannot start", async () => {
    vi.mocked(applyCanvasChatCommandsAsync).mockResolvedValueOnce({
      applied: 0,
      openedUiActions: 0,
      createdNodeIds: [],
      errors: ["执行启动失败"],
      commandResults: [],
    });
    render(<WorkflowRunRecoveryBar projectId="project-a" canvasId="canvas-a" />);

    fireEvent.click(await screen.findByRole("button", { name: "继续下游" }));

    expect(await screen.findByText("执行启动失败")).toBeInTheDocument();
    expect(updateFreezoneWorkflowRun).not.toHaveBeenCalled();
  });

  it("can retry only failed nodes without running blocked downstream nodes", async () => {
    render(<WorkflowRunRecoveryBar projectId="project-a" canvasId="canvas-a" />);

    fireEvent.click(await screen.findByRole("button", { name: "仅重试失败" }));

    await waitFor(() => {
      expect(applyCanvasChatCommandsAsync).toHaveBeenCalledWith(
        [expect.objectContaining({
          commands: [{
            type: "run_workflow",
            node_ids: ["video-1"],
            direction: "node",
            regenerate: true,
          }],
        })],
        { projectId: "project-a", canvasId: "canvas-a" },
      );
      expect(updateFreezoneWorkflowRun).not.toHaveBeenCalled();
    });
  });

  it("shows stale blocked output and only regenerates it after an explicit click", async () => {
    const staleRun: FreezoneWorkflowRun = {
      ...failedRun,
      status: "running",
      actions: [{
        node_id: "compose-1",
        action: "render_manifest",
        status: "blocked",
        artifact_status: "stale",
        error: "input_media_changed",
        processing_operation_id: "operation-old",
      }],
    };
    vi.mocked(listFreezoneWorkflowRuns).mockResolvedValue({ runs: [staleRun] });

    render(<WorkflowRunRecoveryBar projectId="project-a" canvasId="canvas-a" />);

    expect(await screen.findByText("产物已过期，需要重新生成")).toBeInTheDocument();
    expect(applyCanvasChatCommandsAsync).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "重新生成过期结果" }));

    await waitFor(() => expect(applyCanvasChatCommandsAsync).toHaveBeenCalledWith(
      [expect.objectContaining({
        commands: [{
          type: "run_workflow",
          node_ids: ["compose-1"],
          direction: "node",
          regenerate: true,
        }],
      })],
      { projectId: "project-a", canvasId: "canvas-a" },
    ));
  });
});
