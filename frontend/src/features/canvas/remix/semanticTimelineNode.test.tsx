import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

const updateNodeData = vi.fn();
const upstreamNodes = [
  { id: 'script-1', type: 'scriptNode', data: { remixScriptRef: { artifactId: 'script-art', revision: 1 } } },
  { id: 'transcript-1', type: 'transcriptNode', data: { artifactRef: { artifactId: 'words-art', revision: 1 } } },
];
vi.mock('@xyflow/react', () => ({ Handle: () => null, Position: { Left: 'left', Right: 'right' } }));
vi.mock('@/features/canvas/ui/NodeHeader', () => ({ NodeHeader: () => null, NODE_HEADER_FLOATING_POSITION_CLASS: '' }));
vi.mock('@/features/canvas/application/useUpstreamGraph', () => ({ useUpstreamNodes: () => upstreamNodes }));
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: (selector: (state: unknown) => unknown) => selector({ updateNodeData, setSelectedNode: vi.fn() }) }));
vi.mock('@/lib/url-params', () => ({ readUrl: () => ({ project: 'demo' }) }));
vi.mock('@/api/remix', () => ({
  fetchRemixArtifact: vi.fn(async (_project, ref) => ref.artifactId === 'words-art'
    ? { schema: 'word_timeline.v1', payload: { status: 'completed', words: [{ start_us: 0, end_us: 1_000_000 }] } }
    : { schema: 'performance_timing.v1', payload: { events: [{ event_id: 'product', start_frame: 3, end_frame: 15 }], unresolved_required_events: [], override_conflicts: [] } }),
  submitRemixProcessAction: vi.fn(async () => ({ operation_id: 'op-1' })),
  fetchRemixOperation: vi.fn(async () => ({ operation_id: 'op-1', status: 'needs_confirmation', output_refs: [{ artifact_id: 'timing-art', revision: 1 }] })),
  confirmRemixOperation: vi.fn(async () => ({ operation_id: 'op-1', status: 'completed', output_refs: [{ artifact_id: 'timing-art', revision: 1 }] })),
}));

import { submitRemixProcessAction } from '@/api/remix';
import { SemanticTimelineNode } from '../nodes/SemanticTimelineNode';

describe('SemanticTimelineNode', () => {
  it('compiles connected script and speech alignment with editable event offset', async () => {
    render(<SemanticTimelineNode id="timeline-1" type="semanticTimelineNode" data={{ displayName: 'Semantic timeline', events: [{ selectionId: 'product', label: '产品登场' }], eventOverrides: {} }} selected={false} />);
    fireEvent.change(screen.getByLabelText('产品登场 偏移毫秒'), { target: { value: '250' } });
    fireEvent.click(screen.getByRole('button', { name: '重新编译' }));
    await waitFor(() => expect(submitRemixProcessAction).toHaveBeenCalledWith('demo', expect.objectContaining({
      action: 'compile_semantic_timeline',
      scriptRef: { artifactId: 'script-art', revision: 1 },
      wordTimelineRef: { artifactId: 'words-art', revision: 1 },
      editMap: [{ source_start_us: 0, source_end_us: 1_000_000, timeline_start_us: 0 }],
      overrides: [{ event_id: 'product', relative_offset_us: 250_000 }],
    })));
    await waitFor(() => expect(updateNodeData).toHaveBeenCalledWith('timeline-1', expect.objectContaining({ artifactRef: { artifactId: 'timing-art', revision: 1 } })));
  });
});
