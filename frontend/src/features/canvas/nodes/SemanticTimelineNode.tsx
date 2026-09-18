import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { ScrollText } from 'lucide-react';
import type { SemanticTimelineNodeData } from '@/features/canvas/domain/canvasNodes';
import { NodeHeader, NODE_HEADER_FLOATING_POSITION_CLASS } from '@/features/canvas/ui/NodeHeader';
import { CANVAS_NODE_PANEL_SURFACE_CLASS, canvasNodeFrameClass } from '@/features/canvas/ui/nodeFrameStyles';

export const SemanticTimelineNode = memo(({ data, selected }: NodeProps & { data: SemanticTimelineNodeData; selected?: boolean }) => (
  <div className="group relative w-[380px]">
    <Handle type="target" position={Position.Left} id="timing" className="!h-2 !w-2 !border-0 !bg-[rgb(148,163,184)]" />
    <Handle type="source" position={Position.Right} id="composition" className="!h-2 !w-2 !border-0 !bg-[rgb(45,212,191)]" />
    <NodeHeader className={NODE_HEADER_FLOATING_POSITION_CLASS} icon={<ScrollText className="h-4 w-4" />} titleText={data.displayName || 'Semantic timeline'} editable={false} />
    <div className={`max-h-[440px] overflow-auto rounded-[var(--node-radius)] border p-3 ${CANVAS_NODE_PANEL_SURFACE_CLASS} ${canvasNodeFrameClass({ selected })}`}>
      {data.events.map((event) => <div key={event.selectionId} className="border-b border-white/[0.06] py-2 text-[12px] text-text-dark"><div>{event.label}</div>{event.invalidReason ? <div className="text-amber-300">{event.invalidReason}</div> : <div className="text-text-muted">frames {event.startFrame}–{event.endFrame}</div>}</div>)}
      {data.events.length === 0 && <div className="text-[12px] text-text-muted">No compiled events</div>}
    </div>
  </div>
));
SemanticTimelineNode.displayName = 'SemanticTimelineNode';
