// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  Handle,
  Position,
  useUpdateNodeInternals,
  type NodeProps,
} from "@xyflow/react";
import { Film, X } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useCanvasStore } from "@/stores/canvasStore";
import { useUpstreamNodes } from "@/features/canvas/application/useUpstreamGraph";
import {
  CANVAS_NODE_TYPES,
  isVideoNode,
  type CanvasNodeData,
  type VideoComposeNodeData,
} from "@/features/canvas/domain/canvasNodes";
import { localizeNodeDisplayName } from "@/features/canvas/domain/nodeDisplay";
import {
  NodeHeader,
  NODE_HEADER_FLOATING_POSITION_CLASS,
} from "@/features/canvas/ui/NodeHeader";
import {
  CANVAS_NODE_INPUT_SURFACE_CLASS,
  CANVAS_NODE_PANEL_SURFACE_CLASS,
  canvasNodeFrameClass,
} from "@/features/canvas/ui/nodeFrameStyles";
import { AddNodeToChatButton } from "@/features/canvas/ui/AddNodeToChatButton";
import { readUrl } from "@/lib/url-params";
import {
  buildInitialTimeline,
  reconcileDraftWithUpstream,
  resolveUnknownClipDurations,
  VideoComposeModal,
} from "@/features/canvas/compose/VideoComposeModal";
import {
  buildComposePayload,
  hasExportableClips,
  hasOverlappingVideoClips,
  type ComposeTimelineState,
} from "@/features/canvas/compose/timelineModel";
import { fetchFreezoneJobResult, submitFreezoneVideoCompose } from "@/api/ops";
import { awaitTaskCompletion } from "@/api/tasks";
import { orderedComposeSeedNodeIds } from "@/features/canvas/compose/composeInputOrdering";
import {
  publishNodeActionAccepted,
  publishNodeActionError,
  publishNodeActionSuccess,
  subscribeNodeAction,
} from "@/features/canvas/application/nodeActionResult";
import { generationTaskDescriptor } from "@/features/canvas/application/resumeGeneration";
import { RemixEditor } from "@/features/canvas/remix/RemixEditor";
import { canvasEventBus } from "@/features/canvas/application/canvasServices";
import { NODE_CONTEXT_CONTROL_TRIGGER_CLASS } from "@/features/canvas/ui/nodeControlStyles";
import { canExportManifest, manifestToSrt, previewStatus, resolveManifestOverrideConflict, textDownloadUrl, type ManifestOverrideConflict, type ManifestPayload } from "@/features/canvas/remix/manifestEditor";
import { confirmRemixOperation, fetchRemixArtifact, fetchRemixOperation, listRemixOperations, reconcileRemixOperation, renderRemixManifest, reviseRemixArtifact, transitionRemixOperation, type RemixArtifact, type RemixTaskRef } from "@/api/remix";
import { RemixWorkbench } from "@/features/canvas/remix/RemixWorkbench";
import { boundBrollVisualMedia } from "@/features/canvas/remix/brollCanvasBinding";
import { primaryVisualEventsForScript } from "@/features/canvas/remix/remixJourney";
import { operationReusableForNode, prepareNodeRemix, remixGraphFingerprint } from "@/features/canvas/remix/prepareNodeRemix";
import { applyRemixInvalidation } from "@/features/canvas/remix/spawnRemixDownstream";
import { captureFreezoneCanvasScope, flushFreezoneCanvasRuntime, applyRemoteFreezoneCanvas } from "@/features/freezone/canvasSyncRuntime";
import { getFreezoneCanvas } from "@/api/canvas";
import { captureCanvasBaseline, mergeConfirmedCanvasWithLocalChanges } from "@/features/canvas/remix/workbenchCanvasMerge";
import { submitProductionComposeRender } from "@/features/canvas/compose/productionComposeRender";

type VideoComposeNodeProps = NodeProps & {
  id: string;
  data: VideoComposeNodeData;
  selected?: boolean;
};

const NODE_WIDTH = 240;
const NODE_HEIGHT = 136;
const MIN_UPSTREAM_VIDEOS = 2;
const MIN_AUTO_COMPOSE_VIDEOS = 1;
const MIN_AUTO_COMPOSE_MEDIA = 2;

export const VideoComposeNode = memo(
  ({ id, data, selected }: VideoComposeNodeProps) => {
    const { t } = useTranslation();
    const updateNodeInternals = useUpdateNodeInternals();
    const setSelectedNode = useCanvasStore((state) => state.setSelectedNode);
    const requestFocusNode = useCanvasStore((state) => state.requestFocusNode);
    const updateNodeData = useCanvasStore((state) => state.updateNodeData);
    const currentRemixInputFingerprint = useCanvasStore((state) => remixGraphFingerprint(state.nodes, state.edges, id));
    const canvasEdges = useCanvasStore((state) => state.edges);
    const upstreamNodes = useUpstreamNodes(id);
    const [isEditorOpen, setEditorOpen] = useState(false);
    const [isProductionComposeOpen, setProductionComposeOpen] = useState(false);
    const [isRemixWorkbenchOpen, setRemixWorkbenchOpen] = useState(false);
    const [remixKey, setRemixKey] = useState<string>(() => globalThis.crypto?.randomUUID?.() ?? `remix-${Date.now()}`);
    const [remixTask, setRemixTask] = useState<RemixTaskRef | null>(null);
    const [remixError, setRemixError] = useState<string | null>(null);
    const [remixOperationStatus, setRemixOperationStatus] = useState<string | null>(null);
    const [isRemixSubmitting, setRemixSubmitting] = useState(false);
    const [isRemixPreparing, setRemixPreparing] = useState(false);
    const [manifestArtifact, setManifestArtifact] = useState<RemixArtifact | null>(null);
    const [manifestDraft, setManifestDraft] = useState<ManifestPayload | null>(null);
    const [loadedManifestRefKey, setLoadedManifestRefKey] = useState<string | null>(null);
    const [manifestDirty, setManifestDirty] = useState(false);
    const [isManifestSaving, setManifestSaving] = useState(false);
    const prepareClaimRef = useRef(false);
    const submitRemixRef = useRef<null | (() => void)>(null);
    const saveManifestRef = useRef<null | (() => void)>(null);
    const [autoRenderRequested, setAutoRenderRequested] = useState(false);
    const [autoSaveRequested, setAutoSaveRequested] = useState(false);
    const [recoveredRemixOperationId, setRecoveredRemixOperationId] = useState<string | null>(null);

    const seedNodeIds = useMemo(
      () => orderedComposeSeedNodeIds(upstreamNodes, data.compositionInputOrder),
      [data.compositionInputOrder, upstreamNodes],
    );
    const videoCount = useMemo(
      () =>
        upstreamNodes.filter(
          (node) => isVideoNode(node) && Boolean(node.data.videoUrl),
        ).length,
      [upstreamNodes],
    );
    const canOpen = videoCount >= MIN_UPSTREAM_VIDEOS;
    const canAutoCompose =
      videoCount >= MIN_AUTO_COMPOSE_VIDEOS && seedNodeIds.length >= MIN_AUTO_COMPOSE_MEDIA;
    const hasSemanticTimeline = upstreamNodes.some((node) => node.type === CANVAS_NODE_TYPES.semanticTimeline);
    const hasProductionPlan = upstreamNodes.some((node) => node.type === CANVAS_NODE_TYPES.script && node.data.remixRole === 'production_plan');
    const productionPlanMode = Boolean(data.productionPlanNodeId && data.productionPlanV2Ref);
    const showRemixStaging = !productionPlanMode && (hasSemanticTimeline || hasProductionPlan) && !data.remixWorkbenchDraft;

    const resolvedTitle = useMemo(
      () => localizeNodeDisplayName(CANVAS_NODE_TYPES.videoCompose, data, t),
      [data, t],
    );
    const cardToneClass = canvasNodeFrameClass({ selected });

    const project = readUrl().project;
    const canvasId = readUrl().canvas ?? "default";
    const remixBackgrounds = showRemixStaging ? upstreamNodes.filter((node) => (node.data.remixRole === 'background' || node.data.remixRole === 'a-roll')
      && (node.data.imageUrl || node.data.previewImageUrl || node.data.videoUrl)) : [];
    const remixBackground = remixBackgrounds.length === 1 ? remixBackgrounds[0] : null;
    const remixScript = showRemixStaging ? upstreamNodes.find((node) => node.type === CANVAS_NODE_TYPES.script && node.data.remixRole === 'adaptation_script') : null;
    const currentScriptRef = remixScript?.data.remixScriptRef as { artifactId?: string; revision?: number } | undefined;
    const remixPrimaryNodes = showRemixStaging ? upstreamNodes.filter((node) => node.type === CANVAS_NODE_TYPES.video && node.data.remixRole === 'primary'
      && (node.data.remixScriptRef as { artifactId?: string; revision?: number } | undefined)?.artifactId === currentScriptRef?.artifactId
      && (node.data.remixScriptRef as { revision?: number } | undefined)?.revision === currentScriptRef?.revision) : [];
    const backgroundHasSource = Boolean(remixBackground?.data.imageUrl || remixBackground?.data.previewImageUrl || remixBackground?.data.videoUrl);
    let backgroundBound = false;
    if (project && remixBackground && backgroundHasSource) {
      try { boundBrollVisualMedia(project, remixBackground); backgroundBound = true; }
      catch { /* The staging card points to the background node for binding. */ }
    }
    const neededPrimaryEvents = remixScript ? primaryVisualEventsForScript(remixScript, upstreamNodes, canvasEdges) : [];
    const requiredPrimaryNodes = remixPrimaryNodes.filter((node) => neededPrimaryEvents.some((event) => event.id === node.data.remixEventId));
    const primaryNeedsAttention = requiredPrimaryNodes.find((node) => {
      if (!project) return true;
      try { boundBrollVisualMedia(project, node); return false; }
      catch { return true; }
    });
    const scriptSelections = (remixScript?.data.remixScriptDraft as { selections?: unknown[] } | undefined)?.selections;
    const primaryReady = Array.isArray(scriptSelections) && scriptSelections.length > 0
      && neededPrimaryEvents.every((event) => requiredPrimaryNodes.some((node) => node.data.remixEventId === event.id))
      && !primaryNeedsAttention;
    const canPrepareRemix = backgroundBound || (!remixBackground && primaryReady);
    const primaryStateKey = JSON.stringify(remixPrimaryNodes.map((node) => [node.id, node.data.imageUrl, node.data.videoUrl, node.data.remixMedia]));
    useEffect(() => {
      setRemixError(null);
    }, [remixBackground?.id, remixBackground?.data.imageUrl, remixBackground?.data.previewImageUrl, remixBackground?.data.videoUrl, remixBackground?.data.remixMedia, primaryStateKey]);
    const manifestRefKey = data.remixManifestRef ? `${data.remixManifestRef.artifactId}:${data.remixManifestRef.revision}` : null;
    const manifestReady = Boolean(manifestRefKey && loadedManifestRefKey === manifestRefKey
      && manifestArtifact
      && manifestArtifact.id === data.remixManifestRef?.artifactId
      && manifestArtifact.revision === data.remixManifestRef?.revision);
    const remixInputsCurrent = typeof data.remixInputFingerprint === 'string'
      && data.remixInputFingerprint.length > 0
      && currentRemixInputFingerprint === data.remixInputFingerprint;

    useEffect(() => {
      if (data.remixInputsCurrent === remixInputsCurrent) return;
      updateNodeData(id, { remixInputsCurrent });
    }, [data.remixInputsCurrent, id, remixInputsCurrent, updateNodeData]);

    useEffect(() => {
      if (!project || !data.remixManifestRef) { setManifestArtifact(null); setManifestDraft(null); setLoadedManifestRefKey(null); updateNodeData(id, { remixManifestLoadedRef: null, remixManifestLoadState: 'idle', remixManifestDirty: false }); return; }
      let cancelled = false;
      setRemixKey(globalThis.crypto?.randomUUID?.() ?? `remix-${Date.now()}`);
      updateNodeData(id, { remixManifestLoadedRef: null, remixManifestLoadState: 'loading' });
      void Promise.resolve(fetchRemixArtifact(project, data.remixManifestRef)).then((artifact) => {
        if (cancelled) return;
        if (!artifact) return;
        setManifestArtifact(artifact); setManifestDraft(artifact.payload as ManifestPayload); setLoadedManifestRefKey(`${artifact.id}:${artifact.revision}`); setManifestDirty(false);
        updateNodeData(id, { remixManifestLoadedRef: { artifactId: artifact.id, revision: artifact.revision }, remixManifestLoadState: 'ready', remixManifestDirty: false });
      }).catch((error) => { if (!cancelled) { setRemixError(error instanceof Error ? error.message : String(error)); updateNodeData(id, { remixManifestLoadedRef: null, remixManifestLoadState: 'failed' }); } });
      return () => { cancelled = true; };
    }, [project, data.remixManifestRef?.artifactId, data.remixManifestRef?.revision, id, updateNodeData]);

    useEffect(() => {
      if (!project || !data.remixManifestRef || !manifestArtifact || (data.remixWorkbenchDraft && !data.remixInputFingerprint) || data.remixOperationId) return;
      let cancelled = false;
      void listRemixOperations(project).then(({ items }) => {
        if (cancelled) return;
        const match = items.find((item) => item.canvas_id === canvasId && item.node_id === id && item.task_type === 'remix_render'
          && operationReusableForNode(item, manifestArtifact, data.remixNodeRevision ?? 0, data.remixInputFingerprint));
        if (match) setRecoveredRemixOperationId(match.operation_id);
      }).catch(() => {});
      return () => { cancelled = true; };
    }, [project, canvasId, id, data.remixManifestRef?.artifactId, data.remixManifestRef?.revision, data.remixNodeRevision, data.remixWorkbenchDraft, data.remixOperationId, manifestArtifact]);

    useEffect(() => {
      const operationId = data.remixOperationId ?? recoveredRemixOperationId;
      if (!project || !operationId || (data.remixWorkbenchDraft && !data.remixInputFingerprint) || data.resultVideoUrl) return;
      let cancelled = false;
      let running = false;
      let terminal = false;
      const poll = async () => {
        if (running || cancelled || terminal) return;
        running = true;
        try {
          let operation = await fetchRemixOperation(project, operationId);
          if (cancelled) return;
          if (['pending_reconciliation', 'confirming', 'cancelling'].includes(operation.status)) {
            operation = await reconcileRemixOperation(project, operationId);
            if (cancelled) return;
          }
          setRemixOperationStatus(operation.status);
          if (operation.status === 'stale') { terminal = true; setRemixError('导出结果已过期，请明确重新导出。'); return; }
          if (operation.status === 'failed' || operation.status === 'cancelled') { terminal = true; setRemixError(`合成${operation.status === 'failed' ? '失败' : '已取消'}`); return; }
          if (operation.status !== 'needs_confirmation') return;
          const state = useCanvasStore.getState();
          const current = state.nodes.find((node) => node.id === id)?.data as VideoComposeNodeData | undefined;
          if (!current?.remixManifestRef || operation.input_refs?.length !== 1
            || operation.input_refs[0].artifact_id !== current.remixManifestRef.artifactId
            || operation.input_refs[0].revision !== current.remixManifestRef.revision
            || operation.node_revision !== current.remixNodeRevision
            || current.remixInputsCurrent !== true
            || operation.normalized_parameters?.input_fingerprint !== current.remixInputFingerprint) { setRemixError('合成已完成，但上游版本已变化；结果保留在任务记录中'); return; }
          if (await flushFreezoneCanvasRuntime(project, canvasId) !== true) throw new Error('画布尚未保存，请稍后重试结果确认');
          const baseline = captureCanvasBaseline(state.nodes, state.edges);
          const confirmed = await confirmRemixOperation(project, operationId, canvasId, id);
          if (confirmed.status !== 'completed') { setRemixError('结果已过期，保留在任务记录中'); return; }
          const remote = await getFreezoneCanvas(project, canvasId);
          if (!applyRemoteFreezoneCanvas(project, canvasId, remote, (remoteNodes, remoteEdges, localNodes, localEdges) =>
            mergeConfirmedCanvasWithLocalChanges(remoteNodes, remoteEdges, localNodes, localEdges, baseline, id))) throw new Error('结果已确认，但画布同步失败；请刷新查看');
          setRemixError(null);
        } catch (caught) { if (!cancelled) setRemixError(caught instanceof Error ? caught.message : String(caught)); }
        finally { running = false; }
      };
      void poll();
      const timer = window.setInterval(() => void poll(), 3_000);
      return () => { cancelled = true; window.clearInterval(timer); };
    }, [project, canvasId, id, data.remixOperationId, recoveredRemixOperationId, data.remixWorkbenchDraft, data.resultVideoUrl]);

    const handlePrepareRemix = useCallback(async () => {
      if (!project || prepareClaimRef.current) return;
      const failedManifestLoad = data.remixManifestLoadState === 'failed';
      if (manifestDirty || (data.remixManifestRef && !manifestReady && !failedManifestLoad)) {
        setRemixError(manifestDirty ? '请先保存清单修改，再更新清单' : '当前清单仍在加载，请稍后再试');
        return;
      }
      prepareClaimRef.current = true;
      setRemixPreparing(true);
      setRemixError(null);
      try {
        const state = useCanvasStore.getState();
        const prepared = await prepareNodeRemix(project, state.nodes, state.edges, id, failedManifestLoad ? null : data.remixManifestRef);
        updateNodeData(id, { remixManifestRef: prepared.manifestRef, remixManifestLoadedRef: null, remixManifestLoadState: 'loading', remixManifestDirty: false, remixEvents: prepared.events, remixSelections: prepared.selections, unresolvedRequiredEvents: prepared.unresolvedRequiredEvents, remixManifestConflicts: prepared.conflicts ?? [], remixInputFingerprint: remixGraphFingerprint(state.nodes, state.edges, id), remixInputsCurrent: true, remixNodeRevision: (data.remixNodeRevision ?? 0) + 1, remixOperationId: null, resultVideoUrl: null });
        setRecoveredRemixOperationId(null);
        setRemixKey(globalThis.crypto?.randomUUID?.() ?? `remix-${Date.now()}`);
      } catch (caught) {
        setRemixError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        prepareClaimRef.current = false;
        setRemixPreparing(false);
      }
    }, [project, id, updateNodeData, data.remixNodeRevision, data.remixManifestRef, data.remixManifestLoadState, manifestDirty, manifestReady]);
    useEffect(() => canvasEventBus.subscribe('remix-node/action', ({ nodeId, action }) => {
      if (nodeId === id && action === 'prepare_compose') void handlePrepareRemix();
      if (nodeId === id && action === 'render_export') setAutoRenderRequested(true);
      if (nodeId === id && action === 'save_manifest') setAutoSaveRequested(true);
      if (nodeId === id && action === 'resolve_manifest_conflicts') setSelectedNode(id);
    }), [id, handlePrepareRemix, setSelectedNode]);

    useEffect(() => {
      if (!autoRenderRequested || !manifestReady || !submitRemixRef.current) return;
      setAutoRenderRequested(false);
      submitRemixRef.current();
    }, [autoRenderRequested, manifestReady]);

    useEffect(() => {
      if (!autoSaveRequested || !manifestReady || !saveManifestRef.current) return;
      setAutoSaveRequested(false);
      saveManifestRef.current();
    }, [autoSaveRequested, manifestReady]);

    useEffect(() => {
      if (!data.autoPrepareRequested || (!hasSemanticTimeline && !hasProductionPlan)) return;
      if (data.remixManifestRef && !manifestReady) return;
      updateNodeData(id, { autoPrepareRequested: false });
      void handlePrepareRemix();
    }, [data.autoPrepareRequested, data.remixManifestRef, hasSemanticTimeline, hasProductionPlan, id, handlePrepareRemix, manifestReady, updateNodeData]);

    useEffect(() => {
      updateNodeInternals(id);
    }, [id, updateNodeInternals]);

    useEffect(() => {
      if (!productionPlanMode || !data.productionComposeOpenOnCreate) return;
      setProductionComposeOpen(true);
      updateNodeData(id, { productionComposeOpenOnCreate: false });
    }, [productionPlanMode, data.productionComposeOpenOnCreate, id, updateNodeData]);

    useEffect(() => {
      if (!productionPlanMode || !data.resultVideoUrl || !data.productionPlanRenderingRef) return;
      const rendered = data.productionPlanRenderedRef as { artifactId: string; revision: number } | undefined;
      const requested = data.productionPlanRenderingRef as { artifactId: string; revision: number };
      if (rendered?.artifactId === requested.artifactId && rendered.revision === requested.revision) return;
      updateNodeData(id, { productionPlanRenderedRef: requested });
    }, [productionPlanMode, data.resultVideoUrl, data.productionPlanRenderingRef, data.productionPlanRenderedRef, id, updateNodeData]);


    const handleOpen = useCallback(() => {
      if (!project) return false;
      if (productionPlanMode) { setSelectedNode(id); setProductionComposeOpen(true); return true; }
      if (!canOpen) return false;
      setSelectedNode(id);
      setEditorOpen(true);
      return true;
    }, [canOpen, id, project, productionPlanMode, setSelectedNode]);

    const handleAutoCompose = useCallback(async () => {
      if (productionPlanMode) {
        const planNodeId = typeof data.productionPlanNodeId === "string" ? data.productionPlanNodeId : null;
        if (!project || !planNodeId) throw new Error("作品方案尚未关联视频合成节点");
        const isCurrentScope = captureFreezoneCanvasScope(project, canvasId);
        const assertScope = () => { if (!isCurrentScope()) throw new Error("所属画布已切换，请从原项目任务记录查看导出结果"); };
        assertScope();
        updateNodeData(id, { isGenerating: true, generationStartedAt: Date.now(), generationError: null });
        try {
          const { saved, task } = await submitProductionComposeRender({
            project, canvasId, planNodeId, composeNodeId: id,
          });
          assertScope();
          updateNodeData(id, { generationTaskKey: task.task_key, generationTaskType: task.task_type,
            generationTaskJobId: task.job_id });
          await awaitTaskCompletion(task.task_key, project, { taskType: task.task_type });
          assertScope();
          const started = Date.now();
          let videoUrl: string | null = null;
          while (Date.now() - started < 60_000) {
            assertScope();
            const current = useCanvasStore.getState().nodes.find((node) => node.id === id)?.data;
            const rendered = current?.productionPlanRenderedRef as { artifactId?: string; revision?: number } | undefined;
            if (rendered?.artifactId === saved.artifactId && rendered.revision === saved.revision
              && typeof current?.resultVideoUrl === "string" && current.resultVideoUrl) {
              videoUrl = current.resultVideoUrl;
              break;
            }
            if (current?.remixInputsCurrent === false) throw new Error("合成过程中作品方案或素材已变化，结果请从任务记录查看");
            await new Promise((resolve) => setTimeout(resolve, 500));
          }
          if (!videoUrl) throw new Error("视频任务已完成，但成片尚未回填；请从任务记录检查");
          updateNodeData(id, { isGenerating: false, generationStartedAt: null, generationError: null });
          return { videoUrl, output_url: videoUrl, task_key: task.task_key, task_type: task.task_type, job_id: task.job_id, operation_id: task.operation_id };
        } catch (error) {
          if (isCurrentScope()) updateNodeData(id, { isGenerating: false, generationStartedAt: null, generationError: error instanceof Error ? error.message : String(error) });
          throw error;
        }
      }
      if (!project || !canAutoCompose) {
        throw new Error("自动合成至少需要 1 个视频和共计 2 个已完成媒体节点");
      }
      const draft = data.draftTimeline as ComposeTimelineState | undefined;
      const timeline = await resolveUnknownClipDurations(
        draft?.tracks?.length
          ? reconcileDraftWithUpstream(draft, seedNodeIds)
          : buildInitialTimeline(seedNodeIds),
      );
      if (!hasExportableClips(timeline)) throw new Error("视频合成没有可用素材");
      if (hasOverlappingVideoClips(timeline)) {
        throw new Error("视频轨道存在重叠片段，请先在时间线中调整");
      }
      updateNodeData(id, {
        isGenerating: true,
        generationStartedAt: Date.now(),
        generationError: null,
      });
      try {
        const ref = await submitFreezoneVideoCompose(
          project,
          buildComposePayload(timeline, {
            title: resolvedTitle,
            canvasId,
            nodeId: id,
            fps: 30,
          }),
        );
        updateNodeData(id, generationTaskDescriptor(ref));
        await awaitTaskCompletion(ref.task_key, project, {
          taskType: ref.task_type,
        });
        const result = await fetchFreezoneJobResult(
          project,
          "freezone_video_compose",
          ref.job_id,
        );
        if (!result.url) throw new Error("视频合成完成但未返回成片地址");
        const resultCoverUrl = result.cover_url ?? timeline.cover?.url ?? null;
        const store = useCanvasStore.getState();
        const existingResult = store.nodes.find((node) =>
          isVideoNode(node) && node.data.composeSourceNodeId === id,
        );
        if (existingResult && isVideoNode(existingResult)) {
          store.updateNodeData(existingResult.id, {
            videoUrl: result.url,
            previewImageUrl: resultCoverUrl,
          });
        } else {
          const position = store.findNodePosition(id, 580, 380);
          const resultNodeId = store.addNode(CANVAS_NODE_TYPES.video, position, {
            videoUrl: result.url,
            previewImageUrl: resultCoverUrl,
            displayName: t("videoCompose.node.resultName"),
            sourceFileName: null,
            composeSourceNodeId: id,
          } as Partial<CanvasNodeData>);
          store.addEdge(id, resultNodeId);
        }
        updateNodeData(id, {
          resultVideoUrl: result.url,
          previewImageUrl: resultCoverUrl,
          resolution: timeline.resolution,
          draftTimeline: timeline,
          isGenerating: false,
          generationStartedAt: null,
          generationError: null,
        });
        return {
          videoUrl: result.url,
          output_url: result.url,
          task_key: ref.task_key,
          task_type: ref.task_type,
          job_id: ref.job_id,
        };
      } catch (error) {
        updateNodeData(id, {
          isGenerating: false,
          generationStartedAt: null,
          generationError: error instanceof Error ? error.message : String(error),
        });
        throw error;
      }
    }, [
      canAutoCompose,
      canvasId,
      data.draftTimeline,
      data.productionPlanNodeId,
      id,
      project,
      productionPlanMode,
      resolvedTitle,
      seedNodeIds,
      t,
      updateNodeData,
    ]);

    useEffect(
      () => subscribeNodeAction(({ nodeId, action, requestId }) => {
        if (nodeId !== id) return;
        if (action === "open_video_compose_modal") {
          publishNodeActionAccepted(requestId, id, action);
          if (!handleOpen()) {
            publishNodeActionError(requestId, id, action, "视频合成至少需要 2 个已完成视频输入");
            return;
          }
          publishNodeActionSuccess(requestId, id, action, { openedUiAction: true });
          return;
        }
        if (action !== "auto_compose_video") return;
        publishNodeActionAccepted(requestId, id, action);
        void handleAutoCompose()
          .then((output) => publishNodeActionSuccess(requestId, id, action, output))
          .catch((error) => publishNodeActionError(requestId, id, action, error));
      }),
      [handleAutoCompose, handleOpen, id],
    );

    if (productionPlanMode) {
      const planNodeId = String(data.productionPlanNodeId);
      const planNode = upstreamNodes.find((node) => node.id === planNodeId);
      const plan = planNode?.data.productionPlanV2Draft as { goal?: string; beats?: unknown[] } | undefined;
      const saved = planNode?.data.productionPlanV2Ref as { artifactId: string; revision: number } | undefined;
      const rendered = data.productionPlanRenderedRef as { artifactId: string; revision: number } | undefined;
      const currentVideo = Boolean(data.resultVideoUrl && saved && rendered?.artifactId === saved.artifactId
        && rendered?.revision === saved.revision && !planNode?.data.productionPlanV2Dirty);
      return <div className="group relative h-full w-full overflow-visible" style={{ width: NODE_WIDTH, height: NODE_HEIGHT }} onClick={() => setSelectedNode(id)}>
        <Handle type="target" position={Position.Left} id="target" className="!h-2 !w-2 !border-0 !bg-[rgb(148,163,184)]" />
        <Handle type="source" position={Position.Right} id="source" className="!h-2 !w-2 !border-0 !bg-[rgb(148,163,184)]" />
        <AddNodeToChatButton nodeId={id} />
        <NodeHeader className={NODE_HEADER_FLOATING_POSITION_CLASS} icon={<Film className="h-4 w-4" />} titleText={resolvedTitle} editable onTitleChange={(next) => updateNodeData(id, { displayName: next })} />
        <div className={`flex h-full w-full flex-col justify-center gap-2 rounded-[var(--node-radius)] border px-4 py-3 ${CANVAS_NODE_INPUT_SURFACE_CLASS} ${cardToneClass}`}>
          <button type="button" onClick={(event) => { event.stopPropagation(); setProductionComposeOpen(true); }} className="nodrag h-9 rounded-[12px] border border-white/15 bg-white/[0.04] text-[13px] text-text-dark hover:bg-white/[0.08]">打开视频合成</button>
          {Boolean(data.productionPlanLegacyComposeNodeId) && <button type="button" className="nodrag text-[11px] text-primary hover:underline" onClick={(event) => {
            event.stopPropagation();
            const legacyId = String(data.productionPlanLegacyComposeNodeId);
            useCanvasStore.setState((current) => ({ nodes: current.nodes.map((node) => node.id === legacyId ? { ...node, hidden: false } : node) }));
            setSelectedNode(legacyId); requestFocusNode(legacyId);
          }}>查看旧版编排与成片</button>}
          <div className="truncate text-center text-[11px] text-text-muted">{data.remixOperationId && !data.resultVideoUrl ? '正在导出' : currentVideo ? '当前版本成片' : data.resultVideoUrl || data.remixPreviousVideoUrl ? '旧版成片' : '待编排'} · {plan?.beats?.length ?? 0} 段</div>
          {plan?.goal && <div className="truncate text-center text-[11px] text-text-muted">{plan.goal}</div>}
        </div>
        {isProductionComposeOpen && project && <VideoComposeModal project={project} canvasId={canvasId} seedNodeIds={[]} productionPlanNodeId={planNodeId} composeNodeId={id} onClose={() => setProductionComposeOpen(false)} onComposed={() => undefined} />}
      </div>;
    }

    if (data.remixManifestRef && (data.remixInputFingerprint || !data.remixWorkbenchDraft)) {
      const conflicts = data.remixManifestConflicts ?? [];
      const structuredConflicts = conflicts.filter((conflict): conflict is ManifestOverrideConflict => typeof conflict !== 'string');
      const conflictMessage = (conflict: string | ManifestOverrideConflict) => typeof conflict === 'string' ? conflict : conflict.message;
      const inputsCurrent = remixInputsCurrent;
      const exportable = manifestReady && inputsCurrent && canExportManifest({ unresolvedRequiredEvents: data.unresolvedRequiredEvents, conflicts }) && !manifestDirty;
      const nodeRevision = data.remixNodeRevision;
      const blockReason = !Number.isInteger(nodeRevision) || (nodeRevision ?? 0) < 0
        ? t('videoCompose.node.remixRevisionMissing')
        : (data.unresolvedRequiredEvents ?? []).join(', ');
      const previewState = previewStatus(data.remixManifestRef, data.remixRenderManifestRef, currentRemixInputFingerprint, data.remixInputFingerprint);
      const saveManifest = async () => {
        if (!project || !manifestReady || !manifestArtifact || !manifestDraft || !manifestDirty || conflicts.length) return;
        setManifestSaving(true); setRemixError(null);
        try {
          const saved = await reviseRemixArtifact(project, data.remixManifestRef!, manifestDraft,
            (manifestArtifact.upstream_refs ?? []).map((ref) => ({ artifactId: ref.artifact_id, revision: ref.revision })));
          const ref = { artifactId: saved.id, revision: saved.revision };
          setManifestArtifact(saved); setManifestDraft(saved.payload as ManifestPayload); setManifestDirty(false);
          updateNodeData(id, { remixManifestRef: ref, remixManifestLoadedRef: ref, remixManifestLoadState: 'ready', remixManifestDirty: false, remixNodeRevision: (nodeRevision ?? 0) + 1, remixOperationId: null, resultVideoUrl: null, remixManifestConflicts: conflicts });
          setRemixTask(null); setRecoveredRemixOperationId(null); setRemixKey(globalThis.crypto?.randomUUID?.() ?? `remix-${Date.now()}`);
        } catch (error) { setRemixError(error instanceof Error ? error.message : String(error)); }
        finally { setManifestSaving(false); }
      };
      const resolveConflict = (conflictKey: string, resolution: 'keep_override' | 'accept_upstream') => {
        if (!manifestDraft) return;
        try {
          const resolved = resolveManifestOverrideConflict(manifestDraft, structuredConflicts, conflictKey, resolution);
          setManifestDraft(resolved.payload);
          setManifestDirty(true);
          updateNodeData(id, { remixManifestConflicts: resolved.conflicts, remixManifestDirty: true });
          setRemixError(null);
        } catch (error) { setRemixError(error instanceof Error ? error.message : String(error)); }
      };
      const acceptAllUpstream = () => {
        if (!manifestDraft) return;
        let payload = manifestDraft;
        let remaining = structuredConflicts;
        for (const conflict of structuredConflicts) {
          const resolved = resolveManifestOverrideConflict(payload, remaining, conflict.key, 'accept_upstream');
          payload = resolved.payload;
          remaining = resolved.conflicts;
        }
        if (conflicts.some((conflict) => typeof conflict === 'string')) payload = { ...payload, manual_overrides: [] };
        setManifestDraft(payload);
        setManifestDirty(true);
        updateNodeData(id, { remixManifestConflicts: [], remixManifestDirty: true });
        setRemixError(null);
      };
      const submit = async (regenerate = false) => {
        if (!project || !exportable || nodeRevision == null) return;
        if (data.remixInputFingerprint && currentRemixInputFingerprint !== data.remixInputFingerprint) {
          setRemixError('上游素材或产物已变化，请重新生成合成清单');
          return;
        }
        setRemixSubmitting(true); setRemixError(null);
        setRemixOperationStatus('pending');
        try {
          if (await flushFreezoneCanvasRuntime(project, canvasId) !== true) throw new Error('画布尚未保存，请稍后重试');
          const task = await renderRemixManifest(project, { manifestRef: data.remixManifestRef!, idempotencyKey: remixKey, nodeRevision, canvasId, nodeId: id, inputFingerprint: data.remixInputFingerprint, regenerate });
          setRemixTask(task);
          updateNodeData(id, { remixOperationId: task.operation_id,
            ...(data.resultVideoUrl ? { remixPreviousVideoUrl: data.resultVideoUrl, resultVideoUrl: null } : {}) });
          if (await flushFreezoneCanvasRuntime(project, canvasId) !== true) throw new Error('任务已提交，但画布尚未保存；请刷新后从任务记录恢复');
          setRemixKey(globalThis.crypto?.randomUUID?.() ?? `remix-${Date.now()}`);
        }
        catch (error) { setRemixError(error instanceof Error ? error.message : String(error)); }
        finally { setRemixSubmitting(false); }
      };
      submitRemixRef.current = () => { void submit(false); };
      saveManifestRef.current = () => { void saveManifest(); };
      if (data.remixRole === 'production_compose') {
        const arrangement = upstreamNodes.find((node) => node.type === CANVAS_NODE_TYPES.semanticTimeline
          && node.data.productionPlanNodeId);
        const plan = upstreamNodes.find((node) => node.type === CANVAS_NODE_TYPES.script
          && node.data.remixRole === 'production_plan');
        const returnTarget = arrangement ?? plan;
        const spec = manifestReady ? manifestDraft?.output_spec : null;
        const seconds = spec?.fps && spec.duration_frames ? (spec.duration_frames / spec.fps).toFixed(1) : null;
        const shownVideoUrl = data.resultVideoUrl ?? data.remixPreviousVideoUrl;
        const oldPreview = Boolean(shownVideoUrl && (!data.resultVideoUrl || previewState !== 'current'));
        const renderInProgress = Boolean(data.remixOperationId && !data.resultVideoUrl
          && !['failed', 'cancelled', 'stale'].includes(remixOperationStatus ?? ''));
        const status = renderInProgress ? '正在合成视频' : shownVideoUrl ? oldPreview ? '旧版预览' : '成片已生成' : '等待渲染';
        return <div className="group relative h-full w-full overflow-visible" style={{ width: NODE_WIDTH, height: NODE_HEIGHT }} onClick={() => setSelectedNode(id)}>
          <Handle type="target" position={Position.Left} id="target" className="!h-2 !w-2 !border-0 !bg-[rgb(148,163,184)]" />
          <Handle type="source" position={Position.Right} id="source" className="!h-2 !w-2 !border-0 !bg-[rgb(148,163,184)]" />
          <AddNodeToChatButton nodeId={id} />
          <NodeHeader className={NODE_HEADER_FLOATING_POSITION_CLASS} icon={<Film className="h-4 w-4" />} titleText={resolvedTitle} editable onTitleChange={(next) => updateNodeData(id, { displayName: next })} />
          <div className={`flex h-full w-full flex-col items-center justify-center gap-3 rounded-[var(--node-radius)] border px-5 py-4 text-center ${CANVAS_NODE_INPUT_SURFACE_CLASS} ${cardToneClass}`}>
            <button type="button" onClick={(event) => { event.stopPropagation(); setProductionComposeOpen(true); }} className="nodrag flex h-10 w-full items-center justify-center rounded-[12px] border border-white/15 bg-white/[0.04] px-4 text-[13px] text-text-dark transition-colors hover:bg-white/[0.08]">打开视频合成</button>
            <span className="text-[12px] text-text-muted/90">{status} · {seconds ? `${seconds} 秒` : '合成清单'}</span>
            {Boolean(data.productionPlanNewComposeNodeId) && <button type="button" className="nodrag text-[11px] text-primary hover:underline" onClick={(event) => {
              event.stopPropagation();
              setProductionComposeOpen(false);
              const nextId = String(data.productionPlanNewComposeNodeId);
              useCanvasStore.setState((current) => ({ nodes: current.nodes.map((node) => node.id === id ? { ...node, hidden: true } : node) }));
              setSelectedNode(nextId); requestFocusNode(nextId);
            }}>返回新版视频合成</button>}
          </div>
          {isProductionComposeOpen && createPortal(<div role="dialog" aria-label="视频合成" aria-modal="true" className="fixed inset-0 z-[1000] flex items-center justify-center bg-black/75 p-4" onClick={() => setProductionComposeOpen(false)}>
            <div className="nodrag nowheel flex max-h-[90vh] w-full max-w-[900px] flex-col overflow-hidden rounded-2xl border border-[var(--ui-border-strong)] bg-[var(--ui-surface-modal)] text-text-dark shadow-2xl" onClick={(event) => event.stopPropagation()}>
              <header className="flex items-center justify-between border-b border-[var(--ui-border-soft)] px-6 py-4"><div><h2 className="text-base font-semibold">视频合成</h2><p className="mt-1 text-xs text-text-muted">检查编排清单并渲染成片</p></div><button type="button" aria-label="关闭视频合成" className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS} onClick={() => setProductionComposeOpen(false)}><X className="h-4 w-4" /></button></header>
              <div className="ui-scrollbar grid min-h-0 gap-6 overflow-y-auto px-6 py-5 md:grid-cols-[minmax(0,1fr)_280px]">
                <div className="space-y-4">
                  <div><div className="text-sm font-semibold">{status}</div><div className="mt-2 text-xs text-text-muted">{manifestReady && manifestDraft
                    ? `${seconds ?? '—'} 秒 · 画面 ${manifestDraft.layers.length} · 声音 ${manifestDraft.audio_events.length} · 字幕 ${manifestDraft.subtitle_events.length}`
                    : data.remixManifestLoadState === 'failed' ? '合成清单加载失败' : '正在读取编排清单…'}</div></div>
                  {oldPreview && <p className="text-xs text-amber-300">预览已过期，重新渲染后更新</p>}
                  {!exportable && manifestReady && <p className="text-xs text-amber-300">{!inputsCurrent ? '上游方案或素材已变化，请更新编排' : conflicts.map(conflictMessage).join('；') || blockReason}</p>}
                  {remixError && <p role="alert" className="text-xs text-red-300">{remixError}</p>}
                  <div className="flex flex-wrap gap-2">
              <button type="button" disabled={!exportable || nodeRevision == null || isRemixSubmitting || renderInProgress || !project || !remixKey || remixOperationStatus === 'stale'} onClick={() => void submit(false)} className="nodrag h-8 rounded-[12px] bg-primary px-3 text-[12px] font-semibold text-primary-foreground disabled:opacity-50">{isRemixSubmitting ? '提交中…' : '渲染导出'}</button>
              {returnTarget && <button type="button" className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS} onClick={() => { setProductionComposeOpen(false); setSelectedNode(returnTarget.id); requestFocusNode(returnTarget.id); if (arrangement) updateNodeData(arrangement.id, { productionTimelineOpenOnCreate: true }); }}>{arrangement ? '返回成片编排' : '返回作品方案'}</button>}
              <button type="button" disabled={isRemixPreparing || !project || (!manifestReady && data.remixManifestLoadState !== 'failed')} onClick={() => void handlePrepareRemix()} className={`${NODE_CONTEXT_CONTROL_TRIGGER_CLASS} disabled:opacity-50`}>更新编排</button>
              {renderInProgress && <button type="button" className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS} onClick={() => { if (project) void transitionRemixOperation(project, data.remixOperationId!, 'cancelled', 'user_cancelled').then(() => updateNodeData(id, { remixOperationId: null })); }}>取消导出</button>}
                  </div>
                  {shownVideoUrl && <div className="flex gap-3 text-xs text-primary underline"><a href={shownVideoUrl} target="_blank" rel="noreferrer">{oldPreview ? '查看旧版预览' : '预览成片'}</a><a href={shownVideoUrl.replace('/media/', '/files/')} download>下载 MP4</a></div>}
                </div>
                {shownVideoUrl && manifestReady ? <video className="nodrag max-h-[60vh] w-full rounded-[12px] bg-black" src={shownVideoUrl} controls />
                  : <div className="flex min-h-64 items-center justify-center rounded-[12px] border border-dashed border-white/10 bg-black/10 text-xs text-text-muted">{data.remixOperationId ? '渲染完成后可在这里预览' : '尚未生成预览'}</div>}
              </div>
            </div>
          </div>, document.body)}
        </div>;
      }
      return <div className="group relative w-[380px]" onClick={() => setSelectedNode(id)}>
        <Handle type="target" position={Position.Left} id="target" className="!h-2 !w-2 !border-0 !bg-[rgb(148,163,184)]" />
        <Handle type="source" position={Position.Right} id="source" className="!h-2 !w-2 !border-0 !bg-[rgb(45,212,191)]" />
        <NodeHeader className={NODE_HEADER_FLOATING_POSITION_CLASS} icon={<Film className="h-4 w-4" />} titleText={resolvedTitle} editable onTitleChange={(next) => updateNodeData(id, { displayName: next })} />
        <div className={`rounded-[var(--node-radius)] border p-3 ${CANVAS_NODE_INPUT_SURFACE_CLASS} ${cardToneClass}`}>
          <div className="mb-2 flex items-center justify-between gap-2 text-[12px] text-text-muted"><span>合成清单 · 版本 {data.remixManifestRef.revision}</span><span className={manifestReady && previewState === 'current' ? 'text-green-300' : manifestReady && previewState === 'stale' ? 'text-amber-300' : ''}>{data.remixManifestLoadState === 'failed' ? '清单加载失败' : !manifestReady ? '正在加载清单…' : previewState === 'current' ? '预览与导出一致' : previewState === 'stale' ? '预览已过期' : '尚未生成预览'}</span></div>
          <RemixEditor selections={data.remixSelections ?? []} events={data.remixEvents ?? []} manifest={manifestReady ? manifestDraft : null} disabled={isManifestSaving} onManifestChange={(next) => { if (!manifestReady || isManifestSaving) return; applyRemixInvalidation(id, 'style'); setManifestDraft(next); setManifestDirty(true); updateNodeData(id, { remixManifestDirty: true }); }} />
          <div className="mt-3 flex items-center gap-2 border-t border-white/10 pt-3">
            <button type="button" disabled={!exportable || nodeRevision == null || isRemixSubmitting || !project || !remixKey || remixOperationStatus === 'stale'} onClick={() => void submit(false)} className="nodrag h-8 rounded-[12px] bg-primary px-3 text-[12px] font-semibold text-primary-foreground disabled:opacity-50">{isRemixSubmitting ? t('videoCompose.node.remixSubmitting') : t('videoCompose.node.remixSubmit')}</button>
            {data.remixOperationId && !data.resultVideoUrl && remixOperationStatus !== 'stale' ? <button type="button" className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS} onClick={() => { if (project) void transitionRemixOperation(project, data.remixOperationId!, 'cancelled', 'user_cancelled').then(() => updateNodeData(id, { remixOperationId: null })); }}>取消导出</button> : null}
            {['pending_reconciliation', 'confirming', 'cancelling'].includes(remixOperationStatus ?? '') && data.remixOperationId ? <button type="button" className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS} onClick={() => { if (project) void reconcileRemixOperation(project, data.remixOperationId!).then((operation) => setRemixOperationStatus(operation.status)); }}>重新检查任务</button> : null}
            {(remixError || remixOperationStatus === 'stale' || ['pending_reconciliation', 'confirming', 'cancelling'].includes(remixOperationStatus ?? '')) && exportable ? <button type="button" className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS} onClick={() => void submit(true)}>强制重新导出</button> : null}
            {manifestDirty ? <button type="button" disabled={isManifestSaving || conflicts.length > 0 || !manifestReady} onClick={() => void saveManifest()} className={`${NODE_CONTEXT_CONTROL_TRIGGER_CLASS} disabled:opacity-50`}>{isManifestSaving ? '保存中…' : '保存清单修改'}</button> : null}
            <button type="button" disabled={isRemixPreparing || !project || (!manifestReady && data.remixManifestLoadState !== 'failed') || manifestDirty} onClick={() => void handlePrepareRemix()} className={`${NODE_CONTEXT_CONTROL_TRIGGER_CLASS} disabled:opacity-50`}>{data.remixManifestLoadState === 'failed' ? '重新生成合成清单' : '更新清单'}</button>
          </div>
          {manifestDirty ? <div className="mt-2 text-[12px] text-amber-300">修改尚未保存；保存后才能预览或导出该版本。</div> : null}
          {conflicts.length ? <div className="mt-2 space-y-2 text-[12px] text-amber-300"><div>手工修改与新清单冲突：</div>{conflicts.map((conflict, index) => <div key={typeof conflict === 'string' ? `${conflict}-${index}` : conflict.key} className="rounded-[8px] border border-amber-300/20 p-2"><div>{conflictMessage(conflict)}</div>{typeof conflict !== 'string' ? <div className="mt-1 flex gap-2">{conflict.canRebase ? <button type="button" className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS} onClick={() => resolveConflict(conflict.key, 'keep_override')}>保留手工值</button> : null}<button type="button" className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS} onClick={() => resolveConflict(conflict.key, 'accept_upstream')}>采用上游值</button></div> : null}</div>)}<button type="button" className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS} onClick={acceptAllUpstream}>全部采用上游</button></div> : null}
          {(!exportable || nodeRevision == null) && !manifestDirty ? <div className="mt-2 text-[12px] text-amber-300">{t('videoCompose.node.remixBlocked', { reason: conflicts.map(conflictMessage).join('；') || blockReason })}</div> : null}
          {remixTask ? <div className="mt-2 text-[12px] text-green-300">{t('videoCompose.node.remixTask', { task: remixTask.task_key })}</div> : null}
          {data.remixOperationId && !data.resultVideoUrl && remixOperationStatus !== 'stale' ? <div className="mt-2 text-[12px] text-text-muted">合成任务处理中，完成后会回填到本节点。</div> : null}
          {['pending_reconciliation', 'confirming', 'cancelling'].includes(remixOperationStatus ?? '') ? <div className="mt-2 text-[12px] text-amber-300">任务正在恢复确认，可重新检查或强制重新导出。</div> : null}
          {data.resultVideoUrl && manifestReady ? <div className="mt-2 space-y-2"><video className="nodrag aspect-[9/16] max-h-60 w-full rounded-[8px] bg-black" src={data.resultVideoUrl} controls /><div className="flex flex-wrap gap-3 text-[12px] text-primary underline"><a href={data.resultVideoUrl} target="_blank" rel="noreferrer">预览成片</a><a href={data.resultVideoUrl.replace('/media/', '/files/')} download>下载 MP4</a>{manifestArtifact ? <><a href={textDownloadUrl(JSON.stringify(manifestArtifact.payload, null, 2), 'application/json')} download={`manifest-r${manifestArtifact.revision}.json`}>下载 JSON</a><a href={textDownloadUrl(manifestToSrt(manifestArtifact.payload as ManifestPayload), 'application/x-subrip')} download={`subtitles-r${manifestArtifact.revision}.srt`}>下载 SRT</a></> : null}</div></div> : null}
          {remixError ? <div className="mt-2 text-[12px] text-red-300">{remixError}</div> : null}
        </div>
      </div>;
    }

    submitRemixRef.current = null;
    saveManifestRef.current = null;

    return (
      <div
        className="group relative h-full w-full overflow-visible"
        style={{ width: showRemixStaging ? 340 : NODE_WIDTH, height: showRemixStaging ? 190 : NODE_HEIGHT }}
        onClick={() => setSelectedNode(id)}
      >
        <Handle
          type="target"
          position={Position.Left}
          id="target"
          className="!h-2 !w-2 !border-0 !bg-[rgb(148,163,184)]"
        />
        <Handle
          type="source"
          position={Position.Right}
          id="source"
          className="!h-2 !w-2 !border-0 !bg-[rgb(148,163,184)]"
        />

        <AddNodeToChatButton nodeId={id} />

        <NodeHeader
          className={NODE_HEADER_FLOATING_POSITION_CLASS}
          icon={<Film className="h-4 w-4" />}
          titleText={resolvedTitle}
          editable
          onTitleChange={(next) => updateNodeData(id, { displayName: next })}
        />

        <div
          className={`relative flex h-full w-full flex-col overflow-hidden rounded-[var(--node-radius)] border ${showRemixStaging ? CANVAS_NODE_PANEL_SURFACE_CLASS : CANVAS_NODE_INPUT_SURFACE_CLASS} transition-colors ${cardToneClass}`}
        >
          {showRemixStaging ? <div className="flex h-full flex-col justify-center gap-2 px-4 py-3">
            <div className="text-[13px] font-semibold text-text-dark">准备合成清单</div>
            <p className="text-[12px] leading-5 text-text-dark/70">
              {remixBackgrounds.length > 1 ? '当前有多个成片底画面，请只保留一个。'
                : remixBackground && !backgroundBound ? '成片底画面已选择，请先关联这份素材。'
                : primaryNeedsAttention ? '主画面尚未完成，请先生成并关联对应事件的画面。'
                : !canPrepareRemix ? '从改编脚本继续创建主画面，画面就绪后即可合成。'
                : '主画面已就绪，可以生成清单并检查画面与字幕。'}
            </p>
            {remixBackgrounds.length <= 1 && <button
              type="button"
              disabled={isRemixPreparing || (!canPrepareRemix && !remixBackground && !primaryNeedsAttention && !remixScript)}
              onClick={(event) => {
                event.stopPropagation();
                if (canPrepareRemix) { void handlePrepareRemix(); return; }
                const target = remixBackground?.id ?? primaryNeedsAttention?.id ?? remixScript?.id;
                if (target) { setSelectedNode(target); requestFocusNode(target); }
              }}
              className="nodrag h-8 self-start rounded-[10px] border border-white/15 bg-white/[0.06] px-3 text-[12px] font-medium text-text-dark transition-colors hover:bg-white/[0.1] disabled:opacity-45"
            >
              {isRemixPreparing ? '正在生成清单…' : canPrepareRemix ? '生成合成清单' : remixBackground ? '前往底画面节点' : primaryNeedsAttention ? '前往主画面节点' : '前往脚本节点'}
            </button>}
            {remixError && canPrepareRemix ? <p role="alert" className="line-clamp-2 text-[11px] text-red-300">{remixError}</p> : null}
          </div> : <div className="flex h-full w-full flex-col items-center justify-center gap-3 px-5 py-4 text-center">
            <button
              type="button"
              disabled={!canOpen}
              onClick={(event) => {
                event.stopPropagation();
                handleOpen();
              }}
              className="flex h-10 w-full items-center justify-center rounded-[12px] border border-white/15 bg-white/[0.04] px-4 text-center text-[13px] text-text-dark transition-colors hover:bg-white/[0.08] disabled:cursor-not-allowed disabled:opacity-45"
            >
              {t("videoCompose.node.open")}
            </button>
            <span className="text-[12px] text-text-muted/90">
              {t("videoCompose.node.hint", { min: MIN_UPSTREAM_VIDEOS })}
            </span>
            {data.remixWorkbenchDraft && <button
              type="button"
              onClick={(event) => { event.stopPropagation(); setRemixWorkbenchOpen(true); }}
              className="rounded-md px-3 py-1.5 text-[12px] text-primary hover:bg-white/[0.06]"
            >
              旧版单节点改编
            </button>}
            {!hasSemanticTimeline && !hasProductionPlan && <span className="text-center text-[11px] text-text-muted">参考改编：先连接语义时间轴或作品方案节点</span>}
            {remixError && <span role="alert" className="text-[11px] text-red-300">{remixError}</span>}
          </div>}
        </div>

        {isEditorOpen && project && (
          <VideoComposeModal
            project={project}
            canvasId={canvasId}
            seedNodeIds={seedNodeIds}
            initialTimeline={
              (data.draftTimeline as ComposeTimelineState | undefined) ?? null
            }
            onPersistDraft={(timeline) =>
              updateNodeData(id, { draftTimeline: timeline })
            }
            onClose={() => setEditorOpen(false)}
            onComposed={(url, coverUrl) => {
              // 合成完成：在本节点下游新建一个视频节点承载结果，并连边、聚焦。
              // 封面（若设置）写进结果视频节点 + 本合成节点的 previewImageUrl。
              const store = useCanvasStore.getState();
              const position = store.findNodePosition(id, 580, 380);
              const newId = store.addNode(CANVAS_NODE_TYPES.video, position, {
                videoUrl: url,
                previewImageUrl: coverUrl,
                displayName: t("videoCompose.node.resultName"),
                sourceFileName: null,
                composeSourceNodeId: id,
              } as Partial<CanvasNodeData>);
              store.addEdge(id, newId);
              store.setSelectedNode(newId);
              store.requestFocusNode(newId);
              updateNodeData(id, {
                resultVideoUrl: url,
                previewImageUrl: coverUrl,
              });
              setEditorOpen(false);
            }}
          />
        )}
        {isRemixWorkbenchOpen && project && (
          <RemixWorkbench
            project={project}
            canvasId={canvasId}
            nodeId={id}
            initialDraft={data.remixWorkbenchDraft ?? { cues: [] }}
            onPersist={(draft) => updateNodeData(id, {
              remixWorkbenchDraft: draft,
              remixManifestRef: draft.manifestRef ?? null,
              remixNodeRevision: draft.nodeRevision,
              resultVideoUrl: draft.resultPath ? `/api/v1/projects/${encodeURIComponent(project)}/media/${draft.resultPath.split('/').map(encodeURIComponent).join('/')}` : null,
            })}
            onClose={() => setRemixWorkbenchOpen(false)}
          />
        )}
      </div>
    );
  },
);

VideoComposeNode.displayName = "VideoComposeNode";
