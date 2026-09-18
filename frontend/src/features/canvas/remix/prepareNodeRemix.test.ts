import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { CanvasEdge, CanvasNode } from '@/features/canvas/domain/canvasNodes';

const api = vi.hoisted(() => ({ fetchRemixArtifact: vi.fn(), createRemixArtifact: vi.fn() }));
vi.mock('@/api/remix', () => api);
import { prepareNodeRemix, remixGraphFingerprint } from './prepareNodeRemix';

const media = (kind: 'video' | 'audio', hash: string) => ({ media_path: `input/${kind}`, media_hash: hash.repeat(64), url: `/${kind}`, size: 1, media_kind: kind, duration_us: 4_000_000, has_audio: kind === 'video' });
const node = (id: string, type: string, data: Record<string, unknown>) => ({ id, type, data, position: { x: 0, y: 0 } }) as CanvasNode;
const edge = (source: string, target: string) => ({ id: `${source}-${target}`, source, target }) as CanvasEdge;

describe('prepareNodeRemix', () => {
  beforeEach(() => { api.fetchRemixArtifact.mockReset(); api.createRemixArtifact.mockReset(); });

  it('walks connected nodes and publishes a versioned manifest from timing and media', async () => {
    const scriptRef = { artifactId: 'script-1', revision: 1 };
    const timingRef = { artifactId: 'timing-1', revision: 1 };
    const transcriptRef = { artifactId: 'words-1', revision: 1 };
    const blueprintRef = { artifactId: 'blueprint-1', revision: 1 };
    const nodes = [
      node('video', 'videoNode', { remixRole: 'reference', remixMedia: media('video', 'a') }),
      node('audio', 'audioNode', { remixRole: 'narration', remixMedia: media('audio', 'b') }),
      node('script', 'scriptNode', { remixScriptRef: scriptRef }),
      node('story', 'videoStoryNode', { referenceBlueprintRef: blueprintRef }),
      node('words', 'transcriptNode', { artifactRef: transcriptRef }),
      node('timeline', 'semanticTimelineNode', { artifactRef: timingRef }),
      node('compose', 'videoComposeNode', {}),
    ];
    const edges = [edge('video', 'timeline'), edge('audio', 'timeline'), edge('story', 'script'), edge('script', 'timeline'), edge('words', 'timeline'), edge('timeline', 'compose')];
    api.fetchRemixArtifact.mockImplementation((_project: string, ref: { artifactId: string }) => ref.artifactId === 'timing-1'
      ? Promise.resolve({ schema: 'performance_timing.v1', upstream_refs: [{ artifact_id: 'script-1', revision: 1 }, { artifact_id: 'words-1', revision: 1 }], payload: { media_hash: `sha256:${'b'.repeat(64)}`, fps: 30, events: [{ event_id: 'e1', start_frame: 0, end_frame: 30 }], unresolved_required_events: [], override_conflicts: [] } })
      : ref.artifactId === 'script-1' ? Promise.resolve({ schema: 'remix_script.v1', upstream_refs: [{ artifact_id: 'blueprint-1', revision: 1 }], payload: { dialogue: '你好', selections: [{ selection_id: 'e1', start_char: 0, end_char: 2 }], events: [{ event_id: 'e1', caption: '你好' }] } })
        : Promise.resolve({ schema: 'reference_blueprint.v1', payload: { media_hash: 'a'.repeat(64) } }));
    api.createRemixArtifact.mockResolvedValue({ id: 'manifest-1', revision: 1 });
    expect(await prepareNodeRemix('demo', nodes, edges, 'compose')).toEqual({ manifestRef: { artifactId: 'manifest-1', revision: 1 }, events: [{ selectionId: 'e1', startFrame: 0, endFrame: 30 }], selections: [{ id: 'e1', label: '你好', start: 0, end: 2 }], unresolvedRequiredEvents: [] });
    expect(api.createRemixArtifact).toHaveBeenCalledWith('demo', 'composition_manifest.v1', expect.objectContaining({ subtitle_events: [{ text: '你好', start_frame: 0, end_frame: 30 }] }), [timingRef, scriptRef, blueprintRef]);
  });

  it('changes the input fingerprint when an upstream artifact revision changes', () => {
    const nodes = [node('script', 'scriptNode', { remixScriptRef: { artifactId: 'script', revision: 1 } }), node('compose', 'videoComposeNode', {})];
    const edges = [edge('script', 'compose')];
    const before = remixGraphFingerprint(nodes, edges, 'compose');
    const after = remixGraphFingerprint([node('script', 'scriptNode', { remixScriptRef: { artifactId: 'script', revision: 2 } }), nodes[1]], edges, 'compose');
    expect(after).not.toBe(before);
  });

  it('changes the fingerprint when the same upstream nodes are rewired', () => {
    const nodes = [node('a', 'videoNode', {}), node('b', 'scriptNode', {}), node('compose', 'videoComposeNode', {})];
    const first = remixGraphFingerprint(nodes, [edge('a', 'b'), edge('b', 'compose')], 'compose');
    const rewired = remixGraphFingerprint(nodes, [edge('b', 'a'), edge('a', 'compose')], 'compose');
    expect(first).not.toBe(rewired);
  });

  it('refuses timing compiled against a different narration file', async () => {
    const nodes = [
      node('video', 'videoNode', { remixRole: 'reference', remixMedia: media('video', 'a') }),
      node('script', 'scriptNode', { remixScriptRef: { artifactId: 'script', revision: 1 } }),
      node('story', 'videoStoryNode', { referenceBlueprintRef: { artifactId: 'blueprint', revision: 1 } }),
      node('words', 'transcriptNode', { artifactRef: { artifactId: 'words', revision: 1 } }),
      node('timeline', 'semanticTimelineNode', { artifactRef: { artifactId: 'timing', revision: 1 } }),
      node('compose', 'videoComposeNode', {}),
    ];
    api.fetchRemixArtifact.mockImplementation((_project: string, ref: { artifactId: string }) => ref.artifactId === 'timing'
      ? Promise.resolve({ schema: 'performance_timing.v1', payload: { media_hash: `sha256:${'b'.repeat(64)}` }, upstream_refs: [{ artifact_id: 'script', revision: 1 }, { artifact_id: 'words', revision: 1 }] })
      : ref.artifactId === 'script' ? Promise.resolve({ schema: 'remix_script.v1', payload: { dialogue: 'hi', events: [] }, upstream_refs: [{ artifact_id: 'blueprint', revision: 1 }] })
        : Promise.resolve({ schema: 'reference_blueprint.v1', payload: { media_hash: 'a'.repeat(64) } }));
    await expect(prepareNodeRemix('demo', nodes, [edge('video', 'timeline'), edge('story', 'script'), edge('script', 'timeline'), edge('words', 'timeline'), edge('timeline', 'compose')], 'compose')).rejects.toThrow(/语音已变化/);
    expect(api.createRemixArtifact).not.toHaveBeenCalled();
  });
});
