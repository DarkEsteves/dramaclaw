import { memo, useEffect } from 'react';
import { Handle, Position, useUpdateNodeInternals, type NodeProps } from '@xyflow/react';
import { FileText } from 'lucide-react';

import type { TranscriptNodeData, TranscriptWord } from '@/features/canvas/domain/canvasNodes';
import { NodeHeader, NODE_HEADER_FLOATING_POSITION_CLASS } from '@/features/canvas/ui/NodeHeader';
import { NodeResizeHandle } from '@/features/canvas/ui/NodeResizeHandle';
import { CANVAS_NODE_PANEL_SURFACE_CLASS, canvasNodeFrameClass } from '@/features/canvas/ui/nodeFrameStyles';
import { useCanvasStore } from '@/stores/canvasStore';

type TranscriptNodeProps = NodeProps & { id: string; data: TranscriptNodeData; selected?: boolean };

const LOW_CONFIDENCE = 0.6;

function timestamp(us: number): string {
  const milliseconds = Math.max(0, Math.floor(us / 1_000));
  const minutes = Math.floor(milliseconds / 60_000);
  const seconds = Math.floor(milliseconds / 1_000) % 60;
  const remainder = milliseconds % 1_000;
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(remainder).padStart(3, '0')}`;
}

function isLowConfidence(word: TranscriptWord): boolean {
  return typeof word.confidence === 'number' && word.confidence < LOW_CONFIDENCE;
}

export const TranscriptNode = memo(({ id, data, selected }: TranscriptNodeProps) => {
  const updateNodeInternals = useUpdateNodeInternals();
  const setSelectedNode = useCanvasStore((state) => state.setSelectedNode);
  useEffect(() => updateNodeInternals(id), [id, updateNodeInternals]);

  const statusLabel = data.status === 'completed' ? 'Complete' : data.status === 'failed' ? 'Failed' : 'Needs review';
  const statusClass = data.status === 'completed' ? 'text-green-300' : data.status === 'failed' ? 'text-red-300' : 'text-amber-300';

  return (
    <div className="group relative w-[420px]" onClick={() => setSelectedNode(id)}>
      <Handle type="target" position={Position.Left} id="media" className="!h-2 !w-2 !border-0 !bg-[rgb(148,163,184)]" />
      <Handle type="source" position={Position.Right} id="transcript" className="!h-2 !w-2 !border-0 !bg-[rgb(45,212,191)]" />
      <NodeHeader className={NODE_HEADER_FLOATING_POSITION_CLASS} icon={<FileText className="h-4 w-4" />} titleText={data.displayName || 'Transcript'} editable={false} />
      <NodeResizeHandle minWidth={320} minHeight={220} maxWidth={760} maxHeight={900} />
      <div className={`flex max-h-[520px] flex-col overflow-hidden rounded-[var(--node-radius)] border ${CANVAS_NODE_PANEL_SURFACE_CLASS} ${canvasNodeFrameClass({ selected })}`}>
        <div className="flex items-center justify-between border-b border-white/[0.08] px-3 py-2 text-[12px]">
          <span className="text-text-muted">{data.artifactRef ? `${data.artifactRef.artifactId} · r${data.artifactRef.revision}` : 'No transcript artifact'}</span>
          <span className={statusClass}>{statusLabel}</span>
        </div>
        <div className="ui-scrollbar overflow-auto p-2">
          {data.words.map((word, index) => (
            <div key={`${word.startUs}-${index}`} className="flex items-start gap-2 rounded-md px-2 py-1 text-[12px] text-text-dark">
              <span className="w-[156px] shrink-0 font-mono text-[11px] text-text-muted">{timestamp(word.startUs)} – {timestamp(word.endUs)}</span>
              <span>{word.text}</span>
              {isLowConfidence(word) && <span className="text-amber-300">Low confidence</span>}
            </div>
          ))}
          {data.words.length === 0 && <p className="px-2 py-3 text-[12px] text-text-muted">No aligned words</p>}
          {data.unresolved?.map((item) => <p key={item} className="px-2 py-1 text-[12px] text-amber-300">Unresolved: {item}</p>)}
        </div>
      </div>
    </div>
  );
});

TranscriptNode.displayName = 'TranscriptNode';
