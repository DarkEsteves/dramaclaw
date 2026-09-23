import type { CanvasEdge, CanvasNode } from '@/features/canvas/domain/canvasNodes';
import { sourceMediaForTranscript, sourceScriptForTranscript } from './transcriptModel';

export interface TranscriptToolbarState {
  canTranscribe: boolean;
  canAlign: boolean;
  canExport: boolean;
  busy: boolean;
  transcribeLabel: '开始转写' | '重新转写';
}

export function transcriptToolbarState(nodes: CanvasNode[], edges: CanvasEdge[], nodeId: string): TranscriptToolbarState {
  const node = nodes.find((candidate) => candidate.id === nodeId);
  const words = Array.isArray(node?.data.words) ? node.data.words : [];
  const hasArtifact = Boolean(node?.data.artifactRef);
  const hasSource = Boolean(sourceMediaForTranscript(nodeId, nodes, edges));
  const busy = Boolean(node?.data.operationId || node?.data.managedOperationId);
  return {
    canTranscribe: hasSource,
    canAlign: hasSource && Boolean(sourceScriptForTranscript(nodeId, nodes, edges)),
    canExport: hasArtifact,
    busy,
    transcribeLabel: words.length > 0 ? '重新转写' : '开始转写',
  };
}
