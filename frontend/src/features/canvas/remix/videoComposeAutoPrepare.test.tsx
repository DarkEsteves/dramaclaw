import { StrictMode } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const updateNodeData = vi.fn();
const prepareNodeRemix = vi.hoisted(() => vi.fn());
const remixGraphFingerprint = vi.hoisted(() => vi.fn(() => 'current-fingerprint'));
const renderRemixManifest = vi.hoisted(() => vi.fn());
const fetchRemixArtifact = vi.hoisted(() => vi.fn());
const graphState = vi.hoisted(() => ({ nodes: [] as unknown[], edges: [] as unknown[] }));
vi.mock('@xyflow/react', () => ({ Handle: ({ id }: { id: string }) => <div data-handle={id} />, Position: { Left: 'left', Right: 'right' }, useUpdateNodeInternals: () => () => {} }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('@/stores/canvasStore', () => ({ useCanvasStore: Object.assign((selector: (state: unknown) => unknown) => selector({ updateNodeData, setSelectedNode: vi.fn() }), { getState: () => graphState }) }));
vi.mock('@/features/canvas/application/useUpstreamGraph', () => ({ useUpstreamNodes: () => [{ id: 'timing', type: 'semanticTimelineNode', data: {} }] }));
vi.mock('@/features/canvas/domain/nodeDisplay', () => ({ localizeNodeDisplayName: () => 'Compose' }));
vi.mock('@/features/canvas/ui/NodeHeader', () => ({ NodeHeader: () => null, NODE_HEADER_FLOATING_POSITION_CLASS: '' }));
vi.mock('@/features/canvas/ui/AddNodeToChatButton', () => ({ AddNodeToChatButton: () => null }));
vi.mock('@/features/canvas/compose/VideoComposeModal', () => ({ VideoComposeModal: () => null, buildInitialTimeline: vi.fn(), reconcileDraftWithUpstream: vi.fn() }));
vi.mock('@/features/canvas/remix/prepareNodeRemix', () => ({ prepareNodeRemix, remixGraphFingerprint }));
vi.mock('@/lib/url-params', () => ({ readUrl: () => ({ project: 'demo', canvas: 'default' }) }));
vi.mock('@/api/remix', () => ({
  listRemixOperations: vi.fn(async () => ({ items: [] })),
  fetchRemixOperation: vi.fn(), fetchRemixArtifact, reviseRemixArtifact: vi.fn(), confirmRemixOperation: vi.fn(), renderRemixManifest,
}));
vi.mock('@/features/freezone/canvasSyncRuntime', () => ({ flushFreezoneCanvasRuntime: vi.fn(async () => true), applyRemoteFreezoneCanvas: vi.fn() }));
vi.mock('@/api/canvas', () => ({ getFreezoneCanvas: vi.fn() }));

import { VideoComposeNode } from '../nodes/VideoComposeNode';

describe('VideoComposeNode automatic preparation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    graphState.nodes = [];
    graphState.edges = [];
    remixGraphFingerprint.mockReturnValue('current-fingerprint');
    fetchRemixArtifact.mockReset();
  });
  it('publishes one manifest under StrictMode while preparation is pending', async () => {
    let release!: (value: { manifestRef: { artifactId: string; revision: number }; events: []; selections: []; unresolvedRequiredEvents: [] }) => void;
    prepareNodeRemix.mockImplementation(() => new Promise((resolve) => { release = resolve; }));
    render(<StrictMode><VideoComposeNode id="compose" type="videoComposeNode" data={{ displayName: 'Compose', autoPrepareRequested: true }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} /></StrictMode>);
    expect(document.querySelector('[data-handle="target"]')).toBeTruthy();
    expect(document.querySelector('[data-handle="source"]')).toBeTruthy();
    await waitFor(() => expect(prepareNodeRemix).toHaveBeenCalled());
    expect(prepareNodeRemix).toHaveBeenCalledTimes(1);
    release({ manifestRef: { artifactId: 'manifest', revision: 1 }, events: [], selections: [], unresolvedRequiredEvents: [] });
  });

  it('does not export an already prepared manifest after narration is cleared', async () => {
    graphState.nodes = [{ id: 'audio', type: 'audioNode', data: { audioUrl: null, remixMedia: { media_hash: 'old' } } }];
    remixGraphFingerprint.mockReturnValue('cleared-narration-fingerprint');
    render(<VideoComposeNode id="compose" type="videoComposeNode" data={{
      displayName: 'Compose', remixManifestRef: { artifactId: 'manifest', revision: 1 },
      remixInputFingerprint: 'prepared-with-audio-fingerprint', remixNodeRevision: 1,
      unresolvedRequiredEvents: [], remixEvents: [], remixSelections: [],
    }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);
    fireEvent.click(screen.getByRole('button', { name: 'videoCompose.node.remixSubmit' }));
    await waitFor(() => expect(screen.getByText('上游素材或产物已变化，请重新生成合成清单')).toBeTruthy());
    expect(renderRemixManifest).not.toHaveBeenCalled();
  });

  it('shows exact-result preview and MP4, JSON, and SRT downloads for the rendered revision', async () => {
    fetchRemixArtifact.mockResolvedValue({ id: 'manifest', schema: 'composition_manifest.v1', revision: 2, upstream_refs: [], payload: {
      output_spec: { width: 1080, height: 1920, fps: 30, duration_frames: 60 }, media_bindings: [], layers: [],
      subtitle_events: [{ text: '咖啡', start_frame: 0, end_frame: 30 }],
      audio_events: [{ media_id: 'voice', role: 'narration', start_frame: 0, end_frame: 60, volume: 1 }], manual_overrides: [], source_versions: [],
    } });
    render(<VideoComposeNode id="compose" type="videoComposeNode" data={{
      displayName: 'Compose', remixManifestRef: { artifactId: 'manifest', revision: 2 },
      remixRenderManifestRef: { artifactId: 'manifest', revision: 2 }, remixInputFingerprint: 'current-fingerprint',
      remixNodeRevision: 2, unresolvedRequiredEvents: [], remixEvents: [], remixSelections: [],
      resultVideoUrl: '/api/v1/projects/demo/media/freezone/remix_renders/output.mp4',
    }} selected={false} draggable={true} dragging={false} selectable={true} deletable={true} zIndex={0} isConnectable={true} positionAbsoluteX={0} positionAbsoluteY={0} />);
    expect(await screen.findByText('预览与导出一致')).toBeTruthy();
    expect(screen.getByText('下载 MP4').getAttribute('href')).toContain('/files/');
    expect(screen.getByText('下载 JSON').getAttribute('download')).toBe('manifest-r2.json');
    expect(screen.getByText('下载 SRT').getAttribute('href')).toContain('application/x-subrip');
  });
});
