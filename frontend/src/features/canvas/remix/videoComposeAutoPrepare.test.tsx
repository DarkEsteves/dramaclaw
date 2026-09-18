import { StrictMode } from 'react';
import { render, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

const updateNodeData = vi.fn();
const prepareNodeRemix = vi.hoisted(() => vi.fn());
vi.mock('@xyflow/react', () => ({ Handle: () => null, Position: { Left: 'left', Right: 'right' }, useUpdateNodeInternals: () => () => {} }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: Object.assign((selector: (state: unknown) => unknown) => selector({ updateNodeData, setSelectedNode: vi.fn() }), { getState: () => ({ nodes: [], edges: [] }) }) }));
vi.mock('@/features/canvas/application/useUpstreamGraph', () => ({ useUpstreamNodes: () => [{ id: 'timing', type: 'semanticTimelineNode', data: {} }] }));
vi.mock('@/features/canvas/domain/nodeDisplay', () => ({ localizeNodeDisplayName: () => 'Compose' }));
vi.mock('@/features/canvas/ui/NodeHeader', () => ({ NodeHeader: () => null, NODE_HEADER_FLOATING_POSITION_CLASS: '' }));
vi.mock('@/features/canvas/ui/AddNodeToChatButton', () => ({ AddNodeToChatButton: () => null }));
vi.mock('@/features/canvas/compose/VideoComposeModal', () => ({ VideoComposeModal: () => null, buildInitialTimeline: vi.fn(), reconcileDraftWithUpstream: vi.fn() }));
vi.mock('@/features/canvas/remix/prepareNodeRemix', () => ({ prepareNodeRemix, remixGraphFingerprint: () => 'fingerprint' }));
vi.mock('@/lib/url-params', () => ({ readUrl: () => ({ project: 'demo', canvas: 'default' }) }));

import { VideoComposeNode } from '../nodes/VideoComposeNode';

describe('VideoComposeNode automatic preparation', () => {
  it('publishes one manifest under StrictMode while preparation is pending', async () => {
    let release!: (value: { manifestRef: { artifactId: string; revision: number }; events: []; selections: []; unresolvedRequiredEvents: [] }) => void;
    prepareNodeRemix.mockImplementation(() => new Promise((resolve) => { release = resolve; }));
    render(<StrictMode><VideoComposeNode id="compose" type="videoComposeNode" data={{ displayName: 'Compose', autoPrepareRequested: true }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} /></StrictMode>);
    await waitFor(() => expect(prepareNodeRemix).toHaveBeenCalled());
    expect(prepareNodeRemix).toHaveBeenCalledTimes(1);
    release({ manifestRef: { artifactId: 'manifest', revision: 1 }, events: [], selections: [], unresolvedRequiredEvents: [] });
  });
});
