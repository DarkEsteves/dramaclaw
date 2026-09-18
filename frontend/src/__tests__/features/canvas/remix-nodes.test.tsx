import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('@xyflow/react', () => ({
  Handle: ({ id }: { id: string }) => <div data-handle={id} />,
  Position: { Left: 'left', Right: 'right' },
  useUpdateNodeInternals: () => vi.fn(),
}));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@/stores/canvasStore', () => ({
  useCanvasStore: (selector: (state: { setSelectedNode: () => void; updateNodeData: () => void }) => unknown) =>
    selector({ setSelectedNode: vi.fn(), updateNodeData: vi.fn() }),
}));
vi.mock('@/features/canvas/ui/NodeHeader', () => ({
  NodeHeader: ({ titleText }: { titleText: string }) => <div>{titleText}</div>,
  NODE_HEADER_FLOATING_POSITION_CLASS: '',
}));
vi.mock('@/features/canvas/ui/NodeResizeHandle', () => ({ NodeResizeHandle: () => null }));
vi.mock('@/features/canvas/ui/nodeFrameStyles', () => ({
  CANVAS_NODE_PANEL_SURFACE_CLASS: '',
  canvasNodeFrameClass: () => '',
}));

import { CANVAS_NODE_TYPES } from '@/features/canvas/domain/canvasNodes';
import { canvasNodeDefinitions, getDownstreamSpawnTypes, isManualConnectionAllowed } from '@/features/canvas/domain/nodeRegistry';
import { TranscriptNode } from '@/features/canvas/nodes/TranscriptNode';
import { VideoStoryNode } from '@/features/canvas/nodes/VideoStoryNode';

describe('remix canvas nodes', () => {
  it('offers the planned remix nodes as connectable workflow steps', () => {
    expect(canvasNodeDefinitions[CANVAS_NODE_TYPES.transcript].visibleInMenu).toBe(true);
    expect(canvasNodeDefinitions[CANVAS_NODE_TYPES.semanticTimeline].visibleInMenu).toBe(true);
    expect(getDownstreamSpawnTypes(CANVAS_NODE_TYPES.video)).toContain(CANVAS_NODE_TYPES.transcript);
    expect(getDownstreamSpawnTypes(CANVAS_NODE_TYPES.audio)).toContain(CANVAS_NODE_TYPES.transcript);
    expect(getDownstreamSpawnTypes(CANVAS_NODE_TYPES.transcript)).toContain(CANVAS_NODE_TYPES.videoStory);
    expect(getDownstreamSpawnTypes(CANVAS_NODE_TYPES.script)).toContain(CANVAS_NODE_TYPES.semanticTimeline);
    expect(getDownstreamSpawnTypes(CANVAS_NODE_TYPES.semanticTimeline)).toContain(CANVAS_NODE_TYPES.videoCompose);
    expect(isManualConnectionAllowed(CANVAS_NODE_TYPES.audio, CANVAS_NODE_TYPES.transcript)).toBe(true);
  });
  it('registers transcript and output-capable reference blueprint ports', () => {
    const transcript = canvasNodeDefinitions[CANVAS_NODE_TYPES.transcript];
    const blueprint = canvasNodeDefinitions[CANVAS_NODE_TYPES.videoStory];

    expect(transcript.connectivity).toMatchObject({ sourceHandle: true, targetHandle: true });
    expect(transcript.createDefaultData()).toMatchObject({ words: [], status: 'needs_review' });
    expect(blueprint.connectivity).toMatchObject({ sourceHandle: true, targetHandle: true });
    expect(blueprint.createDefaultData()).toHaveProperty('referenceBlueprintRef', null);
    const timeline = canvasNodeDefinitions['semanticTimelineNode' as typeof CANVAS_NODE_TYPES.transcript];
    expect(timeline.connectivity).toMatchObject({ sourceHandle: true, targetHandle: true });
  });

  it('renders persisted artifact and editable word timing', () => {
    render(
      <TranscriptNode
        id="transcript-1"
        type="transcriptNode"
        zIndex={0}
        draggable
        dragging={false}
        selected={false}
        selectable
        deletable
        isConnectable
        positionAbsoluteX={0}
        positionAbsoluteY={0}
        data={{
          displayName: 'Transcript',
          artifactRef: { artifactId: 'artifact-1', revision: 2 },
          status: 'needs_review',
          words: [
            { text: 'Hello', startUs: 100_000, endUs: 450_000, confidence: 0.42 },
            { text: 'world', startUs: 500_000, endUs: 900_000, confidence: 0.98 },
          ],
          unresolved: ['product reveal'],
        }}
      />,
    );

    expect(screen.getByText('artifact-1 · r2')).toBeTruthy();
    expect(screen.getByDisplayValue('Hello')).toBeTruthy();
    expect(screen.getByText('00:00.100 – 00:00.450')).toBeTruthy();
    expect(screen.getByText('低置信度')).toBeTruthy();
  });

  it('renders a persisted reference blueprint artifact on the story node', () => {
    render(
      <VideoStoryNode
        id="blueprint-1"
        type="videoStoryNode"
        zIndex={0}
        draggable
        dragging={false}
        selected={false}
        selectable
        deletable
        isConnectable
        positionAbsoluteX={0}
        positionAbsoluteY={0}
        data={{
          displayName: 'Blueprint',
          rows: [],
          referenceBlueprintRef: { artifactId: 'blueprint-1', revision: 3 },
        }}
      />,
    );

    expect(screen.getByText('Blueprint blueprint-1 · r3')).toBeTruthy();
  });
});
