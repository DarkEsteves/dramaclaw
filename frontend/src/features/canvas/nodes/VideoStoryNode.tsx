// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  Handle,
  Position,
  useUpdateNodeInternals,
  type NodeProps,
} from '@xyflow/react';
import { AlertTriangle, Expand, FileVideo2, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import {
  CANVAS_NODE_TYPES,
  VIDEO_STORY_NODE_DEFAULT_SIZE,
  type VideoStoryNodeData,
  type VideoStoryRow,
} from '@/features/canvas/domain/canvasNodes';
import { isRenderableImageSrc, resolveImageDisplayUrl } from '@/features/canvas/application/imageData';
import { aspectRatioFromImageDimensions } from '@/features/canvas/application/imageNodeSizing';
import { localizeNodeDisplayName } from '@/features/canvas/domain/nodeDisplay';
import {
  NodeHeader,
  NODE_HEADER_FLOATING_POSITION_CLASS,
} from '@/features/canvas/ui/NodeHeader';
import { NodeResizeHandle } from '@/features/canvas/ui/NodeResizeHandle';
import { NodeGenerationOverlay } from '@/features/canvas/ui/NodeGenerationOverlay';
import { EditableTableCell } from '@/features/canvas/ui/EditableTableCell';
import { CANVAS_NODE_PANEL_SURFACE_CLASS, canvasNodeFrameClass } from '@/features/canvas/ui/nodeFrameStyles';
import { useCanvasStore } from '@/stores/canvasStore';
import { connectedBlueprintSources } from '@/features/canvas/remix/blueprintModel';
import { createScriptFromBlueprint } from '@/features/canvas/remix/createScriptFromBlueprint';
import { canvasEventBus } from '@/features/canvas/application/canvasServices';
import { reanalyzeVideoStory } from '@/features/canvas/application/videoAnalyzeStory';
import { analyzeReferenceVideo } from '@/features/canvas/application/analyzeReferenceVideo';
import { ReferenceEventList, type ReferenceEvent, type ReferenceEventPatch } from '@/features/canvas/remix/ReferenceEventList';

type VideoStoryNodeProps = NodeProps & {
  id: string;
  data: VideoStoryNodeData;
  selected?: boolean;
};

const MIN_WIDTH = 480;
const MIN_HEIGHT = 240;
const MAX_WIDTH = 1600;
const MAX_HEIGHT = 1200;

interface ColumnDef {
  key: keyof VideoStoryRow;
  labelKey: string;
  /** Tailwind min-width class for the table cell. */
  widthClass: string;
  /** Fixed width used by the fullscreen detail table so long text wraps. */
  widthPx: number;
  /** True for narrative-style long text columns. */
  wide?: boolean;
}

const V = 'node.videoStory';

const COLUMNS: ColumnDef[] = [
  { key: 'shotNumber', labelKey: `${V}.columns.shotNumber`, widthClass: 'min-w-[60px]', widthPx: 64 },
  { key: 'startTime', labelKey: `${V}.columns.startTime`, widthClass: 'min-w-[90px]', widthPx: 96 },
  { key: 'endTime', labelKey: `${V}.columns.endTime`, widthClass: 'min-w-[90px]', widthPx: 96 },
  { key: 'duration', labelKey: `${V}.columns.duration`, widthClass: 'min-w-[70px]', widthPx: 80 },
  { key: 'visualDescription', labelKey: `${V}.columns.visualDescription`, widthClass: 'min-w-[220px]', widthPx: 320, wide: true },
  { key: 'narrative', labelKey: `${V}.columns.narrative`, widthClass: 'min-w-[220px]', widthPx: 280, wide: true },
  { key: 'shotSize', labelKey: `${V}.columns.shotSize`, widthClass: 'min-w-[80px]', widthPx: 90 },
  { key: 'cameraAngle', labelKey: `${V}.columns.cameraAngle`, widthClass: 'min-w-[100px]', widthPx: 110 },
  { key: 'cameraMovement', labelKey: `${V}.columns.cameraMovement`, widthClass: 'min-w-[120px]', widthPx: 120 },
  { key: 'focalAndDof', labelKey: `${V}.columns.focalAndDof`, widthClass: 'min-w-[120px]', widthPx: 140 },
  { key: 'lighting', labelKey: `${V}.columns.lighting`, widthClass: 'min-w-[120px]', widthPx: 140 },
  { key: 'backgroundMusic', labelKey: `${V}.columns.backgroundMusic`, widthClass: 'min-w-[140px]', widthPx: 160 },
  { key: 'voiceAndSfx', labelKey: `${V}.columns.voiceAndSfx`, widthClass: 'min-w-[140px]', widthPx: 160 },
  { key: 'imagePrompt', labelKey: `${V}.columns.imagePrompt`, widthClass: 'min-w-[260px]', widthPx: 320, wide: true },
  { key: 'videoMotionPrompt', labelKey: `${V}.columns.videoMotionPrompt`, widthClass: 'min-w-[240px]', widthPx: 300, wide: true },
  { key: 'keyframeUrl', labelKey: `${V}.columns.keyframeUrl`, widthClass: 'min-w-[120px]', widthPx: 140 },
];
interface StoryCellProps {
  row: VideoStoryRow;
  col: ColumnDef;
  rowIndex: number;
  onCommit?: (nextValue: string) => void;
  onPreviewKeyframe?: (url: string, shot: string, aspectRatio: string) => void;
  onExportKeyframe?: (url: string, shot: string, aspectRatio: string) => void;
}

function StoryCell({ row, col, rowIndex, onCommit, onPreviewKeyframe, onExportKeyframe }: StoryCellProps) {
  const [imageAspectRatio, setImageAspectRatio] = useState('1:1');
  const value = row[col.key];

  if (col.key === 'keyframeUrl') {
    const url = typeof value === 'string' ? value : null;
    if (!url || !isRenderableImageSrc(url)) return <span className="text-text-muted/60">—</span>;
    const shot = String(row.shotNumber ?? rowIndex + 1);
    return (
      <div className="flex flex-col items-start gap-1.5">
        <button type="button" aria-label={`查看关键帧 ${shot}`} onClick={() => onPreviewKeyframe?.(url, shot, imageAspectRatio)} className="nodrag rounded-[var(--ui-radius-lg)] border border-[var(--ui-border-soft)] p-0.5 hover:border-[rgb(var(--accent-rgb)/0.55)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[rgb(var(--accent-rgb))]">
          <img src={resolveImageDisplayUrl(url)} alt={`第 ${shot} 镜关键帧缩略图`} onLoad={(event) => setImageAspectRatio(aspectRatioFromImageDimensions(event.currentTarget.naturalWidth, event.currentTarget.naturalHeight) ?? '1:1')} className="h-16 max-w-28 rounded-[var(--ui-radius-lg)] object-contain" draggable={false} />
        </button>
        <button type="button" aria-label={`放到画布 · 关键帧 ${shot}`} onClick={() => onExportKeyframe?.(url, shot, imageAspectRatio)} className="nodrag text-xs text-text-dark underline-offset-2 hover:underline">放到画布</button>
      </div>
    );
  }

  const initialText =
    value == null
      ? ''
      : typeof value === 'string' || typeof value === 'number'
        ? String(value)
        : JSON.stringify(value);

  if (!onCommit) {
    if (initialText.length === 0) {
      return <span className="text-text-muted/60">—</span>;
    }
    return (
      <span className={col.wide ? 'whitespace-pre-wrap' : ''}>{initialText}</span>
    );
  }

  return <EditableTableCell value={initialText} onCommit={onCommit} emptyPlaceholder="—" />;
}

interface StoryTableProps {
  rows: VideoStoryRow[];
  compact?: boolean;
  onCellCommit?: (rowIndex: number, colKey: keyof VideoStoryRow, nextValue: string) => void;
  onPreviewKeyframe?: (url: string, shot: string, aspectRatio: string) => void;
  onExportKeyframe?: (url: string, shot: string, aspectRatio: string) => void;
}

function StoryTable({ rows, compact, onCellCommit, onPreviewKeyframe, onExportKeyframe }: StoryTableProps) {
  const { t } = useTranslation();

  return (
    <div className="ui-scrollbar nowheel h-full w-full overflow-auto rounded border border-[rgba(255,255,255,0.08)]">
      <table className="min-w-full border-collapse text-left text-[12px] text-text-dark">
        <thead className="sticky top-0 z-10 bg-bg-dark/95 backdrop-blur">
          <tr>
            {COLUMNS.map((col) => (
              <th
                key={col.key as string}
                className={`${col.widthClass} border-b border-[rgba(255,255,255,0.1)] px-3 py-2 text-[11px] font-medium uppercase tracking-wide text-text-muted`}
              >
                {t(col.labelKey)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, idx) => (
            <tr
              key={idx}
              className="border-b border-[rgba(255,255,255,0.06)] align-top hover:bg-bg-dark/40"
            >
              {COLUMNS.map((col) => (
                <td
                  key={col.key as string}
                  className={`${col.widthClass} px-3 ${compact ? 'py-2' : 'py-3'} align-top`}
                >
                  <StoryCell
                    row={row}
                    col={col}
                    rowIndex={idx}
                    onPreviewKeyframe={onPreviewKeyframe}
                    onExportKeyframe={onExportKeyframe}
                    onCommit={
                      onCellCommit
                        ? (next) => onCellCommit(idx, col.key, next)
                        : undefined
                    }
                  />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface FullscreenStoryTableProps {
  rows: VideoStoryRow[];
  events: ReferenceEvent[];
  onCellCommit: (rowIndex: number, colKey: keyof VideoStoryRow, nextValue: string) => void;
  onOverrideChange: (eventId: string, patch: ReferenceEventPatch) => void;
  onPreviewKeyframe: (url: string, shot: string, aspectRatio: string) => void;
  onExportKeyframe: (url: string, shot: string, aspectRatio: string) => void;
}

function eventSeconds(timeUs: number | null | undefined): string {
  return typeof timeUs === 'number' ? `${(timeUs / 1_000_000).toFixed(1)} 秒` : '暂无';
}

function FullscreenStoryTable({ rows, events, onCellCommit, onOverrideChange, onPreviewKeyframe, onExportKeyframe }: FullscreenStoryTableProps) {
  const { t } = useTranslation();
  const itemCount = Math.max(rows.length, events.length);
  const metaColumnWidths = [360, 300, 300];
  const tableWidth = metaColumnWidths.reduce((total, width) => total + width, 0)
    + COLUMNS.reduce((total, column) => total + column.widthPx, 0);
  const headerClass = 'border-b border-[rgba(255,255,255,0.1)] px-3 py-2 text-[11px] font-medium uppercase tracking-wide text-text-muted';

  return (
    <div className="flex h-full min-h-0 flex-col text-xs text-text-dark">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/[0.08] px-3 py-2 text-text-muted">
        <span>完整拆解字段 · {itemCount} 个事件</span>
      </div>
      <div className="ui-scrollbar nowheel min-h-0 flex-1 overflow-auto">
        <table aria-label="原片拆解完整字段" className="table-fixed border-collapse text-left text-[12px]" style={{ width: tableWidth }}>
          <colgroup>
            {metaColumnWidths.map((width, index) => <col key={`meta-${index}`} style={{ width }} />)}
            {COLUMNS.map((column) => <col key={column.key as string} style={{ width: column.widthPx }} />)}
          </colgroup>
          <thead className="sticky top-0 z-10 bg-bg-dark/95 backdrop-blur">
            <tr>
              <th className={`${headerClass} min-w-[360px]`}>原片事件证据</th>
              <th className={`${headerClass} min-w-[300px]`}>同期台词与词语时间</th>
              <th className={`${headerClass} min-w-[300px]`}>模型判断与叙事作用</th>
              {COLUMNS.map((column) => (
                <th key={column.key as string} className={`${headerClass} ${column.widthClass}`}>{t(column.labelKey)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {Array.from({ length: itemCount }, (_, index) => {
              const row = rows[index] ?? ({} as VideoStoryRow);
              const event = events[index];
              const eventId = event?.event_id ?? `event-${index + 1}`;
              const observedFrame = event?.observed_frame;
              const frameUrl = event?.frame_url ?? observedFrame?.url;
              const displayRow = row.keyframeUrl && isRenderableImageSrc(row.keyframeUrl)
                ? row : { ...row, keyframeUrl: frameUrl };
              const spokenText = event?.spoken_text ?? event?.spoken_range?.text;
              return (
                <tr key={eventId} className="border-b border-white/[0.06] align-top hover:bg-bg-dark/40">
                  <td className="px-3 py-3 align-top">
                    {event ? <div className="grid gap-1.5 whitespace-pre-wrap break-words">
                      <div className="flex gap-3">
                        {frameUrl && <img src={frameUrl} alt={`事件画面 ${index + 1}`} className="h-24 w-16 shrink-0 rounded-[4px] bg-surface-field object-cover" />}
                        <div className="grid content-start gap-1">
                          <span>{event.user_visual_observation ?? event.visual_observation ?? '暂无画面描述'}</span>
                          <span className="text-text-muted">事件 {eventSeconds(event.start_us)}–{eventSeconds(event.end_us)}</span>
                          <span className="font-mono text-[11px] text-text-muted">画面 {eventSeconds(observedFrame?.time_us ?? event.analysis_frame_time_us)} · PTS {(observedFrame?.pts ?? event.frame_pts?.join('、')) || '未知'} · {observedFrame?.time_base ?? '未知 time_base'}</span>
                        </div>
                      </div>
                      <label className="text-[11px] text-text-muted">画面描述 · 可修正
                        <textarea aria-label={`事件 ${index + 1} 画面描述`} value={event.user_visual_observation ?? event.visual_observation ?? ''} onChange={(change) => onOverrideChange(eventId, { visualObservation: change.target.value })} rows={2} className="ui-field mt-1 w-full resize-y px-2 py-1.5 text-xs text-text-dark" />
                      </label>
                      <label className="text-[11px] text-text-muted">画面字幕 · 实际可见文字
                        <input aria-label={`事件 ${index + 1} 画面字幕`} value={event.user_visible_text ?? event.visible_text ?? ''} onChange={(change) => onOverrideChange(eventId, { visibleText: change.target.value })} className="ui-field mt-1 w-full px-2 py-1.5 text-xs text-text-dark" />
                      </label>
                      <details className="text-[11px] text-text-muted"><summary className="cursor-pointer">查看模型原文</summary><p>{event.visual_observation || '无画面描述'}</p><p>{event.visible_text || '无画面字幕'}</p></details>
                      <span><span className="text-text-muted">代表帧时间：</span>{event.frame_times_us?.map(eventSeconds).join('、') || '暂无'}</span>
                      <span><span className="text-text-muted">证据状态：</span>{event.note_status || '正常'}</span>
                    </div> : <span className="text-text-muted/60">—</span>}
                  </td>
                  <td className="px-3 py-3 align-top">
                    {event ? <div className="grid gap-1.5 whitespace-pre-wrap break-words">
                      <span>{spokenText || '无同期台词'}</span>
                      <span className="text-[11px] text-text-muted">{event.word_spans?.map((word) => `${word.text} ${eventSeconds(word.start_us)}–${eventSeconds(word.end_us)}`).join('；') || '暂无逐词时间'}</span>
                      {event.user_visible_text === undefined && (event.transcript_warnings ?? []).map((warning, warningIndex) => <span key={`${warningIndex}-${warning.transcript_word}`} className="text-amber-300">疑似错词：{warning.transcript_word} → {warning.caption_word}</span>)}
                    </div> : <span className="text-text-muted/60">—</span>}
                  </td>
                  <td className="px-3 py-3 align-top">
                    {event ? <div className="grid gap-2 whitespace-pre-wrap break-words">
                      <span><span className="text-text-muted">模型原判断：</span>{event.narrative_inference || '待补充'}</span>
                      <label className="text-[11px] text-text-muted">叙事作用 · 你的判断
                        <input
                          aria-label={`事件 ${index + 1} 叙事作用`}
                          value={event.user_narrative_role ?? event.narrative_inference ?? ''}
                          onChange={(change) => onOverrideChange(eventId, { narrativeRole: change.target.value })}
                          className="mt-1 w-full rounded-[12px] border border-border-dark bg-surface-field px-2 py-1 text-text-dark"
                        />
                      </label>
                    </div> : <span className="text-text-muted/60">—</span>}
                  </td>
                  {COLUMNS.map((column) => (
                    <td key={column.key as string} className="overflow-hidden px-3 py-3 align-top break-words">
                      <StoryCell row={displayRow} col={column} rowIndex={index} onCommit={(nextValue) => onCellCommit(index, column.key, nextValue)} onPreviewKeyframe={onPreviewKeyframe} onExportKeyframe={onExportKeyframe} />
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function EmptyStoryState({ rawResult }: { rawResult?: Record<string, unknown> | null }) {
  const { t } = useTranslation();

  return (
    <div className="flex h-full w-full items-center justify-center p-6">
      <div className="flex max-w-[460px] flex-col items-center gap-3 text-center">
        <div className="text-sm font-medium text-text-dark">{t(`${V}.emptyTitle`)}</div>
        <div className="text-[12px] leading-5 text-text-muted/80">
          {t(`${V}.emptyDetail`)}
        </div>
        <details className="w-full rounded-md border border-white/[0.08] bg-bg-dark/45 text-left">
          <summary className="cursor-pointer list-none px-3 py-2 text-[11px] font-medium text-text-dark/82 transition-colors hover:text-text-dark">
            {t(`${V}.viewRaw`)}
          </summary>
          <pre className="ui-scrollbar max-h-[120px] overflow-auto border-t border-white/[0.06] p-3 text-[11px] leading-5 text-text-muted/86">
{rawResult ? JSON.stringify(rawResult, null, 2) : t(`${V}.rawEmpty`)}
          </pre>
        </details>
      </div>
    </div>
  );
}

function ErrorStoryState({ message }: { message: string }) {
  const { t } = useTranslation();

  return (
    <div className="flex h-full w-full items-center justify-center p-6">
      <div className="flex max-w-[420px] flex-col items-center gap-3 text-center">
        <AlertTriangle className="h-7 w-7 text-red-300/90" />
        <div className="text-sm font-medium text-red-200">{t(`${V}.parseFailed`)}</div>
        <div className="max-h-[88px] overflow-auto break-words text-[12px] leading-5 text-red-200/82 [overflow-wrap:anywhere]">
          {message}
        </div>
      </div>
    </div>
  );
}

export const VideoStoryNode = memo(({ id, data, selected }: VideoStoryNodeProps) => {
  const { t } = useTranslation();
  const updateNodeInternals = useUpdateNodeInternals();
  const setSelectedNode = useCanvasStore((state) => state.setSelectedNode);
  const updateNodeData = useCanvasStore((state) => state.updateNodeData);
  const canvasNodes = useCanvasStore((state) => state.nodes);
  const canvasEdges = useCanvasStore((state) => state.edges);
  const addNode = useCanvasStore((state) => state.addNode);
  const findNodePosition = useCanvasStore((state) => state.findNodePosition);
  const requestFocusNode = useCanvasStore((state) => state.requestFocusNode);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [keyframePreview, setKeyframePreview] = useState<{ url: string; shot: string; aspectRatio: string } | null>(null);
  const keyframePreviewRef = useRef<HTMLDivElement>(null);
  const [scriptError, setScriptError] = useState<string | null>(null);
  const blueprintSource = useMemo(() => connectedBlueprintSources(id, canvasNodes, canvasEdges), [id, canvasNodes, canvasEdges]);
  const reanalyzeBlueprint = useCallback(() => {
    if (!blueprintSource?.videoUrl) return;
    updateNodeData(id, { sourceVideoUrl: blueprintSource.videoUrl, referenceBlueprintRef: null });
    const videoSource = canvasNodes.find((node) => node.type === CANVAS_NODE_TYPES.video && node.data.videoUrl === blueprintSource.videoUrl);
    if (videoSource) {
      analyzeReferenceVideo(videoSource.id, { videoUrl: blueprintSource.videoUrl, targetStoryId: id });
    } else {
      reanalyzeVideoStory(id, { durationSec: blueprintSource.durationUs / 1_000_000 });
    }
  }, [blueprintSource, canvasNodes, id, updateNodeData]);
  useEffect(() => canvasEventBus.subscribe('remix-node/action', ({ nodeId, action }) => {
    if (nodeId === id && action === 'analyze') reanalyzeBlueprint();
  }), [id, reanalyzeBlueprint]);

  const resolvedTitle = useMemo(
    () => localizeNodeDisplayName(CANVAS_NODE_TYPES.videoStory, data, t),
    [data, t],
  );
  const ownNode = canvasNodes.find((node) => node.id === id);
  // React Flow passes auto-measured dimensions through width/height as well.
  // Only dimensions persisted on the node represent a deliberate resize.
  const explicitWidth = ownNode?.width ?? (typeof ownNode?.style?.width === 'number' ? ownNode.style.width : undefined);
  const explicitHeight = ownNode?.height ?? (typeof ownNode?.style?.height === 'number' ? ownNode.style.height : undefined);
  const resolvedWidth = Math.max(MIN_WIDTH, Math.round(explicitWidth ?? VIDEO_STORY_NODE_DEFAULT_SIZE.width));
  const resolvedHeight = Math.max(MIN_HEIGHT, Math.round(explicitHeight ?? VIDEO_STORY_NODE_DEFAULT_SIZE.height));

  useEffect(() => {
    updateNodeInternals(id);
  }, [id, resolvedHeight, resolvedWidth, updateNodeInternals]);

  // Escape closes the topmost detail layer first.
  useEffect(() => {
    if (!isFullscreen && !keyframePreview) return;
    const handleKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      if (keyframePreview) setKeyframePreview(null);
      else setIsFullscreen(false);
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [isFullscreen, keyframePreview]);
  useEffect(() => {
    if (keyframePreview) keyframePreviewRef.current?.focus();
  }, [keyframePreview?.url]);

  const cardToneClass = canvasNodeFrameClass({ selected });

  const rows = Array.isArray(data.rows) ? data.rows : [];
  const isAnalyzing = Boolean(data.isAnalyzing);
  const hasError = Boolean(data.analysisError);
  const hasRows = rows.length > 0;
  const referenceEvents = Array.isArray(data.referenceEvents) ? data.referenceEvents as ReferenceEvent[] : [];
  const hasReferenceEvents = referenceEvents.length > 0;
  const nextStep = isAnalyzing ? '分析原片中…'
    : hasError ? '重新分析原片'
      : hasReferenceEvents || hasRows ? '新建作品方案' : '分析原片';
  const stageLabel: Record<string, string> = { preparing: '准备原片', extracting: '提取画面证据', transcribing: '转写同期台词', analyzing: '对照画面与台词' };

  // 表格单元格编辑：把编辑后的值写回 data.rows[idx][colKey]。
  // keyframeUrl 列在 StoryCell 里走只读分支，不会调到这里。
  const handleCellCommit = useCallback(
    (rowIndex: number, colKey: keyof VideoStoryRow, nextValue: string) => {
      const existing = rows[rowIndex];
      if (!existing) return;
      const prevRaw = existing[colKey];
      const prev = typeof prevRaw === 'string' ? prevRaw : prevRaw == null ? '' : String(prevRaw);
      if (prev === nextValue) return;
      const nextRows = rows.map((row, index) =>
        index === rowIndex ? { ...row, [colKey]: nextValue } : row,
      );
      updateNodeData(id, { rows: nextRows, referenceBlueprintRef: null });
    },
    [id, rows, updateNodeData],
  );

  const handleReferenceOverride = useCallback((eventId: string, patch: ReferenceEventPatch) => {
    const contentChanged = patch.visualObservation !== undefined || patch.visibleText !== undefined || patch.narrativeRole !== undefined;
    const nextEvents = referenceEvents.map((event, index) => {
      if ((event.event_id ?? `event-${index + 1}`) !== eventId) return event;
      return {
        ...event,
        ...(patch.visualObservation !== undefined ? { user_visual_observation: patch.visualObservation } : {}),
        ...(patch.visibleText !== undefined ? { user_visible_text: patch.visibleText } : {}),
        ...(patch.narrativeRole !== undefined ? { user_narrative_role: patch.narrativeRole } : {}),
        ...(contentChanged ? { review_state: 'needs_review' as const } : {}),
        ...(patch.reviewState !== undefined ? { review_state: patch.reviewState } : {}),
      };
    });
    updateNodeData(id, { referenceEvents: nextEvents, referenceBlueprintRef: null, blueprintDirty: true });
  }, [id, referenceEvents, updateNodeData]);


  const exportKeyframe = useCallback((url: string, shot: string, aspectRatio: string) => {
    const imageNodeId = addNode(CANVAS_NODE_TYPES.upload, findNodePosition(id, 360, 360), {
      displayName: `原片关键帧 · 第 ${shot} 镜`,
      imageUrl: url,
      previewImageUrl: url,
      aspectRatio,
      imageOnly: true,
      sourceStoryNodeId: id,
      sourceStoryShotNumber: shot,
    });
    setKeyframePreview(null);
    setIsFullscreen(false);
    setSelectedNode(imageNodeId);
    requestFocusNode(imageNodeId);
  }, [addNode, findNodePosition, id, requestFocusNode, setSelectedNode]);

  const previewKeyframe = useCallback((url: string, shot: string, aspectRatio: string) => {
    setKeyframePreview({ url, shot, aspectRatio });
  }, []);

  useEffect(() => canvasEventBus.subscribe('remix-node/action', ({ nodeId, action }) => {
    if (nodeId === id && action === 'continue_story') {
      setScriptError(null);
      try {
        createScriptFromBlueprint(id);
      } catch (cause) {
        setScriptError(cause instanceof Error ? cause.message : '无法创建作品方案。');
      }
    }
  }), [id]);

  return (
    <div
      className="group relative h-full w-full overflow-visible"
      style={{ width: resolvedWidth, height: resolvedHeight }}
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
        className="!h-2 !w-2 !border-0 !bg-[rgb(45,212,191)]"
      />

      <NodeHeader
        className={NODE_HEADER_FLOATING_POSITION_CLASS}
        icon={<FileVideo2 className="h-4 w-4" />}
        titleText={resolvedTitle}
        editable
        onTitleChange={(nextTitle) => updateNodeData(id, { displayName: nextTitle })}
      />

      <NodeResizeHandle
        minWidth={MIN_WIDTH}
        minHeight={MIN_HEIGHT}
        maxWidth={MAX_WIDTH}
        maxHeight={MAX_HEIGHT}
      />

      <div
        className={`relative flex h-full w-full flex-col overflow-hidden rounded-[var(--node-radius)] border ${CANVAS_NODE_PANEL_SURFACE_CLASS} transition-colors ${cardToneClass}`}
      >
        <div className="flex items-center justify-between border-b border-[rgba(255,255,255,0.08)] px-3 py-2">
          <div className="flex items-center gap-2 text-[12px] text-text-muted">
            {isAnalyzing ? (
              <span>{stageLabel[String(data.remixAnalysisStage)] ?? t(`${V}.parsing`)}</span>
            ) : hasError ? (
              <span className="text-red-300">{t(`${V}.parseFailed`)}</span>
            ) : hasReferenceEvents ? (
              <span>{referenceEvents.length} 个原片事件</span>
            ) : hasRows ? (
              <span>{t(`${V}.rowCount`, { count: rows.length })}</span>
            ) : (
              <span>{t(`${V}.emptyTitle`)}</span>
            )}
          </div>
          <button
            type="button"
            className="inline-flex h-6 items-center gap-1 rounded border border-[rgba(255,255,255,0.18)] bg-bg-dark/60 px-2 text-[11px] text-text-dark hover:border-[rgba(255,255,255,0.32)] disabled:cursor-not-allowed disabled:opacity-45 disabled:hover:border-[rgba(255,255,255,0.18)]"
            onClick={(event) => {
              event.stopPropagation();
              setIsFullscreen(true);
            }}
            disabled={!hasRows && !hasReferenceEvents}
          >
            <Expand className="h-3 w-3" />
            {t(`${V}.fullscreen`)}
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-hidden p-2">
          {scriptError && <p role="alert" className="mb-2 rounded-[var(--ui-radius-md)] border border-red-400/20 bg-red-400/10 px-3 py-2 text-xs text-red-300">{scriptError}</p>}
          {isAnalyzing ? (
            <div className="h-full w-full" />
          ) : hasError ? (
            <ErrorStoryState message={data.analysisError ?? t(`${V}.unknownError`)} />
          ) : hasReferenceEvents ? (
            <ReferenceEventList events={referenceEvents} videoUrl={data.sourceVideoUrl} onOverrideChange={handleReferenceOverride} />
          ) : hasRows ? (
            <StoryTable rows={rows} compact onCellCommit={handleCellCommit} onPreviewKeyframe={previewKeyframe} onExportKeyframe={exportKeyframe} />
          ) : (
            <EmptyStoryState rawResult={data.rawResult ?? null} />
          )}
        </div>
        <div className="border-t border-[var(--ui-border-soft)] px-3 py-2 text-[11px] text-text-muted">
          下一步：{nextStep}
        </div>

        {isAnalyzing && (
          <NodeGenerationOverlay
            startedAt={data.analysisStartedAt ?? null}
            durationMs={90000}
            hasBackground={false}
            messageKey="canvas.analysisProgress"
          />
        )}
      </div>

      {typeof document !== 'undefined' && isFullscreen && createPortal(
        <div
          role="dialog"
          aria-modal="true"
          aria-label={`${resolvedTitle}全屏校对`}
          className="fixed inset-0 z-[220] flex flex-col bg-black/85 p-6"
          onClick={(event) => event.stopPropagation()}
        >
          <div className="mb-3 flex items-center justify-between text-text-dark">
            <div className="flex items-center gap-3">
              <FileVideo2 className="h-5 w-5" />
              <span className="text-base font-medium">{resolvedTitle}</span>
              <span className="text-sm text-text-muted">
                {hasReferenceEvents ? `${referenceEvents.length} 个原片事件` : t(`${V}.rowCountTotal`, { count: rows.length })}
              </span>
            </div>
            <button
              type="button"
              className="inline-flex h-8 items-center gap-1 rounded border border-[rgba(255,255,255,0.2)] bg-bg-dark/60 px-3 text-sm text-text-dark hover:border-[rgba(255,255,255,0.36)]"
              onClick={() => setIsFullscreen(false)}
            >
              <X className="h-4 w-4" />
              {t(`${V}.close`)}
            </button>
          </div>
          <div className="flex-1 overflow-hidden rounded-lg border border-[rgba(255,255,255,0.12)] bg-surface-dark/95">
            {hasReferenceEvents ? (
              <FullscreenStoryTable
                rows={rows}
                events={referenceEvents}
                onOverrideChange={handleReferenceOverride}
                onCellCommit={handleCellCommit}
                onPreviewKeyframe={previewKeyframe}
                onExportKeyframe={exportKeyframe}
              />
            ) : (
              <StoryTable rows={rows} onCellCommit={handleCellCommit} onPreviewKeyframe={previewKeyframe} onExportKeyframe={exportKeyframe} />
            )}
          </div>
        </div>,
        document.body,
      )}

      {typeof document !== 'undefined' && keyframePreview && createPortal(
        <div ref={keyframePreviewRef} role="dialog" aria-modal="true" aria-label="关键帧预览" tabIndex={-1} className="fixed inset-0 z-[230] flex flex-col bg-black/90 p-4 text-text-dark outline-none" onClick={() => setKeyframePreview(null)}>
          <div className="mx-auto flex w-full max-w-5xl items-center justify-between gap-3 py-2">
            <div><p className="font-medium">第 {keyframePreview.shot} 镜 · 原片关键帧</p><p className="text-xs text-text-muted">可作为图片节点放到画布，连接到需要参考图的节点。</p></div>
            <button type="button" aria-label="关闭关键帧预览" onClick={() => setKeyframePreview(null)} className="tap-button tap-button-quiet-primary"><X className="h-4 w-4" /></button>
          </div>
          <div className="flex min-h-0 flex-1 items-center justify-center" onClick={(event) => event.stopPropagation()}>
            <img src={resolveImageDisplayUrl(keyframePreview.url)} alt={`第 ${keyframePreview.shot} 镜关键帧`} onLoad={(event) => { const aspectRatio = aspectRatioFromImageDimensions(event.currentTarget.naturalWidth, event.currentTarget.naturalHeight); if (aspectRatio && aspectRatio !== keyframePreview.aspectRatio) setKeyframePreview((current) => current && current.url === keyframePreview.url ? { ...current, aspectRatio } : current); }} className="max-h-full max-w-full object-contain" />
          </div>
          <div className="mx-auto flex w-full max-w-5xl justify-end py-2"><button type="button" onClick={(event) => { event.stopPropagation(); exportKeyframe(keyframePreview.url, keyframePreview.shot, keyframePreview.aspectRatio); }} className="tap-button tap-button-quiet-primary">放到画布</button></div>
        </div>,
        document.body,
      )}

    </div>
  );
});

VideoStoryNode.displayName = 'VideoStoryNode';
