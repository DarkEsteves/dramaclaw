import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { CanvasEdge, CanvasNode } from '@/features/canvas/domain/canvasNodes';

const api = vi.hoisted(() => ({ fetchRemixArtifact: vi.fn(), createRemixArtifact: vi.fn(), compileProductionPlan: vi.fn() }));
vi.mock('@/api/remix', () => api);
import { operationReusableForNode, prepareNodeRemix, remixGraphFingerprint } from './prepareNodeRemix';

const media = (kind: 'image' | 'video' | 'audio', hash: string) => ({ media_path: `input/${kind}`, media_hash: hash.repeat(64), url: `/${kind}`, size: 1, media_kind: kind, duration_us: kind === 'image' ? 0 : 4_000_000, has_audio: kind === 'video' });
const node = (id: string, type: string, data: Record<string, unknown>) => ({ id, type, data, position: { x: 0, y: 0 } }) as CanvasNode;
const edge = (source: string, target: string) => ({ id: `${source}-${target}`, source, target }) as CanvasEdge;

describe('prepareNodeRemix', () => {
  it('recovers only an active exact-version operation with the same media hashes', () => {
    const artifact = { id: 'manifest', schema: 'composition_manifest.v1', revision: 2, payload: { media_bindings: [
      { media_hash: `sha256:${'b'.repeat(64)}` }, { media_hash: `sha256:${'a'.repeat(64)}` },
    ] } };
    const exact = { operation_id: 'op', status: 'pending_reconciliation', task_id: 'task', output_refs: [], producer_version: 'remix-render.v1', normalized_parameters: { input_fingerprint: 'graph-1' }, node_revision: 7, input_refs: [{ artifact_id: 'manifest', revision: 2 }], input_media_hashes: [`sha256:${'a'.repeat(64)}`, `sha256:${'b'.repeat(64)}`] };
    expect(operationReusableForNode(exact, artifact, 7, 'graph-1')).toBe(true);
    expect(operationReusableForNode({ ...exact, status: 'confirming' }, artifact, 7, 'graph-1')).toBe(true);
    expect(operationReusableForNode({ ...exact, status: 'cancelling' }, artifact, 7, 'graph-1')).toBe(true);
    expect(operationReusableForNode({ ...exact, status: 'stale' }, artifact, 7, 'graph-1')).toBe(false);
    expect(operationReusableForNode({ ...exact, producer_version: 'remix-render.v0' }, artifact, 7)).toBe(false);
    expect(operationReusableForNode({ ...exact, input_media_hashes: [`sha256:${'a'.repeat(64)}`] }, artifact, 7)).toBe(false);
    expect(operationReusableForNode(exact, artifact, 8)).toBe(false);
  });
  beforeEach(() => { api.fetchRemixArtifact.mockReset(); api.createRemixArtifact.mockReset(); api.compileProductionPlan.mockReset(); });

  it('compiles a saved production plan from connected exact-version media', async () => {
    const ref = { artifactId: 'plan', revision: 2 };
    const still = { ...media('image', 'c'), url: '/static/projects/demo/input/image' };
    const plan = { schema_version: 1, goal: '短片', output: { width: 1080, height: 1920, fps: 30 }, beats: [
      { id: 'beat', purpose: '展示', duration_frames: 90, turns: [], visuals: [{ id: 'picture', role: 'primary', description: '静物', media: {
        node_id: 'still', media_hash: still.media_hash, media_path: still.media_path, media_kind: 'image', duration_us: still.duration_us, has_audio: false,
      } }], audio: [] },
    ] };
    const nodes = [node('still', 'imageGenNode', { imageUrl: still.url, remixMedia: still }),
      node('plan-node', 'scriptNode', { remixRole: 'production_plan', productionPlanRef: ref, productionPlanDraft: plan, productionPlanDirty: false }),
      node('compose', 'videoComposeNode', {})];
    const edges = [edge('still', 'plan-node'), edge('plan-node', 'compose')];
    api.fetchRemixArtifact.mockResolvedValue({ schema: 'production_plan.v1', payload: plan });
    api.compileProductionPlan.mockResolvedValue({ manifest_version: 3, layers: [], audio_events: [], subtitle_events: [], media_bindings: [], manual_overrides: [] });
    api.createRemixArtifact.mockResolvedValue({ id: 'manifest', revision: 1 });
    expect(await prepareNodeRemix('demo', nodes, edges, 'compose')).toEqual({
      manifestRef: { artifactId: 'manifest', revision: 1 }, events: [], selections: [], unresolvedRequiredEvents: [],
    });
    expect(api.compileProductionPlan).toHaveBeenCalledWith('demo', ref);
    nodes[0].data.imageUrl = '/static/projects/demo/input/new-image';
    await expect(prepareNodeRemix('demo', nodes, edges, 'compose')).rejects.toThrow(/已变化/);
  });

  it('uses the confirmed production timeline manifest as the compose input', async () => {
    const planRef = { artifactId: 'plan', revision: 2 };
    const manifestRef = { artifactId: 'arranged', revision: 1 };
    const plan = { schema_version: 1, goal: '短片', output: { width: 1080, height: 1920, fps: 30 }, beats: [] };
    const nodes = [node('plan', 'scriptNode', { remixRole: 'production_plan', productionPlanRef: planRef,
      productionPlanDraft: plan, productionPlanDirty: false }),
    node('timeline', 'semanticTimelineNode', { productionPlanNodeId: 'plan', compiledProductionPlanRef: planRef,
      artifactRef: manifestRef, remixTimingSchema: 'composition_manifest.v1' }),
    node('compose', 'videoComposeNode', { remixRole: 'production_compose' })];
    const edges = [edge('plan', 'timeline'), edge('timeline', 'compose')];
    api.fetchRemixArtifact.mockResolvedValue({ schema: 'composition_manifest.v1', revision: 1,
      upstream_refs: [{ artifact_id: 'plan', revision: 2 }], payload: { layers: [], subtitle_events: [], audio_events: [], manual_overrides: [] } });
    expect(await prepareNodeRemix('demo', nodes, edges, 'compose')).toMatchObject({ manifestRef, unresolvedRequiredEvents: [] });
    expect(api.compileProductionPlan).not.toHaveBeenCalled();
    nodes[0].data.productionPlanRef = { artifactId: 'plan', revision: 3 };
    await expect(prepareNodeRemix('demo', nodes, edges, 'compose')).rejects.toThrow(/方案已变化/);
  });

  it('builds a versioned composition from native speaking video sound without narration', async () => {
    const scriptRef = { artifactId: 'script', revision: 2 };
    const blueprintRef = { artifactId: 'blueprint', revision: 1 };
    const timingRef = { artifactId: 'timing', revision: 1 };
    const takeMedia = media('video', 'b');
    const nodes = [node('reference', 'videoNode', { remixRole: 'reference', videoUrl: '/video', remixMedia: media('video', 'a') }),
      node('story', 'videoStoryNode', { referenceBlueprintRef: blueprintRef }),
      node('take', 'videoNode', { remixRole: 'performance', videoUrl: '/video', remixMedia: takeMedia }),
      node('script', 'scriptNode', { remixScriptRef: scriptRef, remixBlueprintRef: blueprintRef,
        remixScriptDraft: { dialogue: '男：你好', segments: [{ id: 'one', start: 2, end: 4, speaker: '男', source: 'speaking_video', takeNodeId: 'take', takeMediaRevision: takeMedia.media_hash }] } }),
      node('timeline', 'semanticTimelineNode', { artifactRef: timingRef, remixTimingSchema: 'performance_timing.v2' }),
      node('compose', 'videoComposeNode', {})];
    const edges = [edge('reference', 'story'), edge('story', 'script'), edge('take', 'script'), edge('script', 'timeline'), edge('take', 'timeline'), edge('timeline', 'compose')];
    api.fetchRemixArtifact.mockImplementation((_project: string, ref: { artifactId: string }) => ref.artifactId === 'timing'
      ? Promise.resolve({ schema: 'performance_timing.v2', upstream_refs: [{ artifact_id: 'script', revision: 2 }], payload: { script_revision: 2, fps: 30, duration_frames: 120,
        takes: [{ segment_id: 'one', source: 'speaking_video', media_hash: `sha256:${takeMedia.media_hash}`, start_frame: 0, end_frame: 120, caption: '你好', word_cues: [{ start_char: 0, end_char: 2, start_frame: 5, end_frame: 90 }] }],
        events: [], unresolved_required_events: [], override_conflicts: [] } })
      : ref.artifactId === 'script' ? Promise.resolve({ schema: 'remix_script.v1', upstream_refs: [{ artifact_id: 'blueprint', revision: 1 }], payload: { dialogue: '男：你好',
        segments: [{ segment_id: 'one', source: 'speaking_video', take_node_id: 'take', take_media_revision: takeMedia.media_hash }], selections: [], events: [] } })
        : Promise.resolve({ schema: 'reference_blueprint.v1', payload: { media_hash: 'a'.repeat(64) } }));
    api.createRemixArtifact.mockResolvedValue({ id: 'manifest', revision: 1 });
    await prepareNodeRemix('demo', nodes, edges, 'compose');
    expect(api.createRemixArtifact).toHaveBeenCalledWith('demo', 'composition_manifest.v1', expect.objectContaining({ manifest_version: 3,
      audio_events: [expect.objectContaining({ media_id: 'take-one', role: 'speech' })],
      layers: [expect.objectContaining({ media_id: 'take-one', z_order: 0 })] }), [timingRef, scriptRef, blueprintRef]);
  });

  it('walks connected nodes and publishes a versioned manifest from timing and media', async () => {
    const scriptRef = { artifactId: 'script-1', revision: 1 };
    const timingRef = { artifactId: 'timing-1', revision: 1 };
    const transcriptRef = { artifactId: 'words-1', revision: 1 };
    const blueprintRef = { artifactId: 'blueprint-1', revision: 1 };
    const nodes = [
      node('video', 'videoNode', { remixRole: 'reference', remixMedia: media('video', 'a') }),
      node('background', 'imageGenNode', { remixRole: 'background', imageUrl: '/image', remixMedia: media('image', 'f') }),
      node('audio', 'audioNode', { remixRole: 'narration', audioUrl: '/audio', remixMedia: media('audio', 'b') }),
      node('script', 'scriptNode', { remixScriptRef: scriptRef }),
      node('story', 'videoStoryNode', { referenceBlueprintRef: blueprintRef }),
      node('words', 'transcriptNode', { artifactRef: transcriptRef }),
      node('timeline', 'semanticTimelineNode', { artifactRef: timingRef }),
      node('compose', 'videoComposeNode', {}),
    ];
    const edges = [edge('video', 'timeline'), edge('background', 'script'), edge('audio', 'timeline'), edge('story', 'script'), edge('script', 'timeline'), edge('words', 'timeline'), edge('timeline', 'compose')];
    api.fetchRemixArtifact.mockImplementation((_project: string, ref: { artifactId: string }) => ref.artifactId === 'timing-1'
      ? Promise.resolve({ schema: 'performance_timing.v1', upstream_refs: [{ artifact_id: 'script-1', revision: 1 }, { artifact_id: 'words-1', revision: 1 }], payload: { media_hash: `sha256:${'b'.repeat(64)}`, fps: 30, events: [{ event_id: 'e1', start_frame: 0, end_frame: 30 }], unresolved_required_events: [], override_conflicts: [] } })
      : ref.artifactId === 'script-1' ? Promise.resolve({ schema: 'remix_script.v1', upstream_refs: [{ artifact_id: 'blueprint-1', revision: 1 }], payload: { dialogue: '你好', selections: [{ selection_id: 'e1', start_char: 0, end_char: 2 }], events: [{ event_id: 'e1', selection_id: 'e1', caption: '你好' }] } })
        : ref.artifactId === 'words-1' ? Promise.resolve({ schema: 'word_timeline.v1', upstream_refs: [{ artifact_id: 'script-1', revision: 1 }], payload: { status: 'completed', media_hash: `sha256:${'b'.repeat(64)}`, script_revision: 1, transcript: '你好', words: [{ text: '你', start_char: 0, end_char: 1, start_us: 0, end_us: 400_000 }, { text: '好', start_char: 1, end_char: 2, start_us: 400_000, end_us: 1_000_000 }] } })
        : Promise.resolve({ schema: 'reference_blueprint.v1', payload: { media_hash: 'a'.repeat(64) } }));
    api.createRemixArtifact.mockResolvedValue({ id: 'manifest-1', revision: 1 });
    expect(await prepareNodeRemix('demo', nodes, edges, 'compose')).toEqual({ manifestRef: { artifactId: 'manifest-1', revision: 1 }, events: [{ selectionId: 'e1', startFrame: 0, endFrame: 30 }], selections: [{ id: 'e1', label: '你好', start: 0, end: 2 }], unresolvedRequiredEvents: [] });
    expect(api.createRemixArtifact).toHaveBeenCalledWith('demo', 'composition_manifest.v1', expect.objectContaining({ subtitle_events: [{ text: '你好', start_frame: 0, end_frame: 30, word_cues: [{ start_char: 0, end_char: 1, start_frame: 0, end_frame: 12 }, { start_char: 1, end_char: 2, start_frame: 12, end_frame: 30 }] }] }), [timingRef, scriptRef, blueprintRef, transcriptRef]);

    nodes[1].data.imageUrl = '/replacement.png';
    await expect(prepareNodeRemix('demo', nodes, edges, 'compose')).rejects.toThrow(/已变化/);
    expect(api.createRemixArtifact).toHaveBeenCalledTimes(1);
  });

  it('prepares a complete primary-shot manifest while ignoring an empty legacy background upload', async () => {
    const scriptRef = { artifactId: 'script-1', revision: 1 };
    const timingRef = { artifactId: 'timing-1', revision: 1 };
    const transcriptRef = { artifactId: 'words-1', revision: 1 };
    const blueprintRef = { artifactId: 'blueprint-1', revision: 1 };
    const nodes = [
      node('video', 'videoNode', { remixRole: 'reference', remixMedia: media('video', 'a') }),
      node('old-background', 'uploadNode', { remixRole: 'background' }),
      node('legacy-still', 'imageGenNode', { remixRole: 'primary', remixEventId: 'e1', remixScriptRef: scriptRef, imageUrl: '/image', remixMedia: media('image', 'e') }),
      node('primary', 'videoNode', { remixRole: 'primary', remixEventId: 'e1', remixScriptRef: scriptRef, videoUrl: '/video', remixMedia: media('video', 'f') }),
      node('audio', 'audioNode', { remixRole: 'narration', audioUrl: '/audio', remixMedia: media('audio', 'b') }),
      node('script', 'scriptNode', { remixScriptRef: scriptRef }),
      node('story', 'videoStoryNode', { referenceBlueprintRef: blueprintRef }),
      node('words', 'transcriptNode', { artifactRef: transcriptRef }),
      node('timeline', 'semanticTimelineNode', { artifactRef: timingRef }),
      node('compose', 'videoComposeNode', {}),
    ];
    const edges = [edge('video', 'timeline'), edge('old-background', 'script'), edge('legacy-still', 'script'), edge('primary', 'script'), edge('audio', 'timeline'), edge('story', 'script'), edge('script', 'timeline'), edge('words', 'timeline'), edge('timeline', 'compose')];
    api.fetchRemixArtifact.mockImplementation((_project: string, ref: { artifactId: string }) => ref.artifactId === 'timing-1'
      ? Promise.resolve({ schema: 'performance_timing.v1', upstream_refs: [{ artifact_id: 'script-1', revision: 1 }, { artifact_id: 'words-1', revision: 1 }], payload: { media_hash: `sha256:${'b'.repeat(64)}`, fps: 30, events: [{ event_id: 'e1', start_frame: 5, end_frame: 30 }], unresolved_required_events: [], override_conflicts: [] } })
      : ref.artifactId === 'script-1' ? Promise.resolve({ schema: 'remix_script.v1', upstream_refs: [{ artifact_id: 'blueprint-1', revision: 1 }], payload: { dialogue: '你好', selections: [{ selection_id: 'e1', start_char: 0, end_char: 2 }], events: [{ event_id: 'e1', selection_id: 'e1', caption: '你好' }] } })
        : ref.artifactId === 'words-1' ? Promise.resolve({ schema: 'word_timeline.v1', upstream_refs: [{ artifact_id: 'script-1', revision: 1 }], payload: { status: 'completed', media_hash: `sha256:${'b'.repeat(64)}`, script_revision: 1, transcript: '你好', words: [] } })
          : Promise.resolve({ schema: 'reference_blueprint.v1', payload: { media_hash: 'a'.repeat(64) } }));
    api.createRemixArtifact.mockResolvedValue({ id: 'manifest-1', revision: 1 });

    await prepareNodeRemix('demo', nodes, edges, 'compose');
    expect(api.createRemixArtifact).toHaveBeenCalledWith('demo', 'composition_manifest.v1', expect.objectContaining({
      media_bindings: expect.arrayContaining([expect.objectContaining({ media_id: 'primary-e1', kind: 'video', purpose: 'a-roll' })]),
      layers: [expect.objectContaining({ row_id: 'primary:e1', start_frame: 0, end_frame: 120, z_order: 0 })],
    }), [timingRef, scriptRef, blueprintRef, transcriptRef]);
  });

  it('changes the input fingerprint when an upstream artifact revision changes', () => {
    const nodes = [node('script', 'scriptNode', { remixScriptRef: { artifactId: 'script', revision: 1 } }), node('compose', 'videoComposeNode', {})];
    const edges = [edge('script', 'compose')];
    const before = remixGraphFingerprint(nodes, edges, 'compose');
    const after = remixGraphFingerprint([node('script', 'scriptNode', { remixScriptRef: { artifactId: 'script', revision: 2 } }), nodes[1]], edges, 'compose');
    expect(after).not.toBe(before);
  });

  it('changes the input fingerprint when a user changes a B-roll decision', () => {
    const script = node('script', 'scriptNode', { remixScriptRef: { artifactId: 'script', revision: 1 }, remixBrollDecisions: { product: true } });
    const compose = node('compose', 'videoComposeNode', {});
    const edges = [edge('script', 'compose')];
    const before = remixGraphFingerprint([script, compose], edges, 'compose');
    const after = remixGraphFingerprint([node('script', 'scriptNode', { ...script.data, remixBrollDecisions: { product: false } }), compose], edges, 'compose');
    expect(after).not.toBe(before);
  });

  it('changes the fingerprint when a deep story blueprint revision changes', () => {
    const compose = node('compose', 'videoComposeNode', {});
    const script = node('script', 'scriptNode', { remixScriptRef: { artifactId: 'script', revision: 1 } });
    const story = node('story', 'videoStoryNode', { referenceBlueprintRef: { artifactId: 'blueprint', revision: 1 } });
    const edges = [edge('story', 'script'), edge('script', 'compose')];
    const before = remixGraphFingerprint([story, script, compose], edges, 'compose');
    const after = remixGraphFingerprint([node('story', 'videoStoryNode', { referenceBlueprintRef: { artifactId: 'blueprint', revision: 2 } }), script, compose], edges, 'compose');
    expect(after).not.toBe(before);
  });

  it('publishes a connected image node only when its selected media revision still matches', async () => {
    const scriptRef = { artifactId: 'script-1', revision: 1 };
    const timingRef = { artifactId: 'timing-1', revision: 1 };
    const transcriptRef = { artifactId: 'words-1', revision: 1 };
    const blueprintRef = { artifactId: 'blueprint-1', revision: 1 };
    const image = { ...media('image', 'c'), url: '/static/projects/demo/input/image' };
    const nodes = [
      node('video', 'videoNode', { remixRole: 'reference', remixMedia: media('video', 'a') }),
      node('background', 'imageGenNode', { remixRole: 'background', imageUrl: '/image', remixMedia: media('image', 'f') }),
      node('audio', 'audioNode', { remixRole: 'narration', audioUrl: '/audio', remixMedia: media('audio', 'b') }),
      node('visual', 'imageGenNode', { remixRole: 'visual', remixMedia: image, imageUrl: image.url }),
      node('script', 'scriptNode', { remixScriptRef: scriptRef }),
      node('story', 'videoStoryNode', { referenceBlueprintRef: blueprintRef }),
      node('words', 'transcriptNode', { artifactRef: transcriptRef }),
      node('timeline', 'semanticTimelineNode', { artifactRef: timingRef }),
      node('compose', 'videoComposeNode', {}),
    ];
    const edges = [edge('video', 'timeline'), edge('background', 'script'), edge('audio', 'timeline'), edge('visual', 'script'), edge('story', 'script'), edge('script', 'timeline'), edge('words', 'timeline'), edge('timeline', 'compose')];
    api.fetchRemixArtifact.mockImplementation((_project: string, ref: { artifactId: string }) => ref.artifactId === 'timing-1'
      ? Promise.resolve({ schema: 'performance_timing.v1', upstream_refs: [{ artifact_id: 'script-1', revision: 1 }, { artifact_id: 'words-1', revision: 1 }], payload: { media_hash: `sha256:${'b'.repeat(64)}`, fps: 30, events: [{ event_id: 'e1', start_frame: 0, end_frame: 30 }], unresolved_required_events: [], override_conflicts: [] } })
      : ref.artifactId === 'script-1' ? Promise.resolve({ schema: 'remix_script.v1', upstream_refs: [{ artifact_id: 'blueprint-1', revision: 1 }], payload: { dialogue: '你好', selections: [{ selection_id: 'e1', start_char: 0, end_char: 2 }], events: [{ event_id: 'e1', selection_id: 'e1', caption: '你好', visual_node_id: 'visual', visual_media_revision: image.media_hash }] } })
        : ref.artifactId === 'words-1' ? Promise.resolve({ schema: 'word_timeline.v1', upstream_refs: [{ artifact_id: 'script-1', revision: 1 }], payload: { status: 'completed', media_hash: `sha256:${'b'.repeat(64)}`, script_revision: 1, transcript: '你好', words: [] } })
          : Promise.resolve({ schema: 'reference_blueprint.v1', payload: { media_hash: 'a'.repeat(64) } }));
    api.createRemixArtifact.mockResolvedValue({ id: 'manifest-1', revision: 1 });

    await prepareNodeRemix('demo', nodes, edges, 'compose');

    expect(api.createRemixArtifact).toHaveBeenCalledWith('demo', 'composition_manifest.v1', expect.objectContaining({
      media_bindings: expect.arrayContaining([expect.objectContaining({ media_id: 'visual-visual', kind: 'image', project_path: image.media_path, media_hash: `sha256:${image.media_hash}` })]),
    }), [timingRef, scriptRef, blueprintRef, transcriptRef]);
  });

  it('changes the fingerprint when the same upstream nodes are rewired', () => {
    const nodes = [node('a', 'videoNode', {}), node('b', 'scriptNode', {}), node('compose', 'videoComposeNode', {})];
    const first = remixGraphFingerprint(nodes, [edge('a', 'b'), edge('b', 'compose')], 'compose');
    const rewired = remixGraphFingerprint(nodes, [edge('b', 'a'), edge('a', 'compose')], 'compose');
    expect(first).not.toBe(rewired);
  });

  it('changes the fingerprint when narration is cleared or replaced without clearing old media', () => {
    const old = node('audio', 'audioNode', { remixRole: 'narration', audioUrl: '/take-a.wav', remixMedia: media('audio', 'b') });
    const compose = node('compose', 'videoComposeNode', {});
    const edges = [edge('audio', 'compose')];
    const before = remixGraphFingerprint([old, compose], edges, 'compose');
    const cleared = remixGraphFingerprint([node('audio', 'audioNode', { ...old.data, audioUrl: null }), compose], edges, 'compose');
    const replaced = remixGraphFingerprint([node('audio', 'audioNode', { ...old.data, audioUrl: '/take-b.wav' }), compose], edges, 'compose');
    expect(cleared).not.toBe(before);
    expect(replaced).not.toBe(before);
  });

  it('changes the fingerprint when a visual source URL is replaced but stale remix metadata remains', () => {
    const staleMedia = media('video', 'a');
    const visual = node('visual', 'videoNode', { remixRole: 'visual', videoUrl: '/take-a.mp4', previewImageUrl: '/take-a.jpg', remixMedia: staleMedia });
    const compose = node('compose', 'videoComposeNode', {});
    const edges = [edge('visual', 'compose')];
    const before = remixGraphFingerprint([visual, compose], edges, 'compose');
    const after = remixGraphFingerprint([node('visual', 'videoNode', { ...visual.data, videoUrl: '/take-b.mp4', previewImageUrl: '/take-b.jpg' }), compose], edges, 'compose');
    expect(after).not.toBe(before);
  });

  it('keeps a prompt edit in the durable graph fingerprint even before a replacement image arrives', () => {
    const compose = node('compose', 'videoComposeNode', {});
    const visual = node('visual', 'imageGenNode', { prompt: 'old prompt', imageUrl: '/same.png', remixVisualOutputCurrent: true });
    const edges = [edge('visual', 'compose')];
    const before = remixGraphFingerprint([visual, compose], edges, 'compose');
    const after = remixGraphFingerprint([node('visual', 'imageGenNode', { ...visual.data, prompt: 'new prompt', remixVisualOutputCurrent: false }), compose], edges, 'compose');
    expect(after).not.toBe(before);
    expect(remixGraphFingerprint([node('visual', 'imageGenNode', { ...visual.data, prompt: 'new prompt', remixVisualOutputCurrent: false }), compose], edges, 'compose')).toBe(after);
  });

  it('refuses to prepare an existing compose after narration audio was cleared', async () => {
    const nodes = [
      node('video', 'videoNode', { remixRole: 'reference', remixMedia: media('video', 'a') }),
      node('audio', 'audioNode', { remixRole: 'narration', audioUrl: null, remixMedia: media('audio', 'b') }),
      node('script', 'scriptNode', { remixScriptRef: { artifactId: 'script', revision: 1 } }),
      node('story', 'videoStoryNode', { referenceBlueprintRef: { artifactId: 'blueprint', revision: 1 } }),
      node('words', 'transcriptNode', { artifactRef: { artifactId: 'words', revision: 1 } }),
      node('timeline', 'semanticTimelineNode', { artifactRef: { artifactId: 'timing', revision: 1 } }),
      node('compose', 'videoComposeNode', { remixManifestRef: { artifactId: 'old-manifest', revision: 1 } }),
    ];
    const edges = [edge('video', 'timeline'), edge('audio', 'timeline'), edge('story', 'script'), edge('script', 'timeline'), edge('words', 'timeline'), edge('timeline', 'compose')];
    await expect(prepareNodeRemix('demo', nodes, edges, 'compose')).rejects.toThrow('当前配音没有音频');
    expect(api.fetchRemixArtifact).not.toHaveBeenCalled();
    expect(api.createRemixArtifact).not.toHaveBeenCalled();
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
