import { describe, expect, it } from 'vitest';
import type { CanvasEdge, CanvasNode } from '@/features/canvas/domain/canvasNodes';
import { nextRemixJourneyStep } from './remixJourney';

const ref = (artifactId: string) => ({ artifactId, revision: 1 });
const node = (id: string, type: CanvasNode['type'], data: Record<string, unknown>): CanvasNode => ({
  id, type, data, position: { x: 0, y: 0 },
} as CanvasNode);
const edge = (source: string, target: string): CanvasEdge => ({ id: `${source}-${target}`, source, target });

function completeThroughScript() {
  const nodes = [
    node('reference', 'videoNode', { videoUrl: '/media/reference.mp4', remixRole: 'reference', remixMedia: { media_kind: 'video', media_hash: 'a'.repeat(64), media_path: 'reference.mp4', has_audio: true } }),
    node('story', 'videoStoryNode', { rows: [], sourceVideoUrl: '/media/reference.mp4', referenceBlueprintRef: ref('blueprint'), referenceEvents: [{ event_id: 'hook' }] }),
    node('script', 'scriptNode', { remixScriptRef: ref('script'), remixBlueprintRef: ref('blueprint'), remixScriptDraft: { dialogue: '新品登场', selections: [{ id: 'product', label: '产品登场', start: 0, end: 4 }] } }),
  ];
  const edges = [edge('reference', 'story'), edge('story', 'script')];
  return { nodes, edges };
}

describe('reference video remix canvas journey', () => {
  it('does not add remix controls to an ordinary uploaded video', () => {
    expect(nextRemixJourneyStep([node('ordinary', 'videoNode', { videoUrl: '/media/clip.mp4' })], [], 'ordinary')).toBeNull();
  });

  it('plans upload, analysis, topic script and saved script in order', () => {
    expect(nextRemixJourneyStep([node('reference', 'videoNode', { remixRole: 'reference' })], [], 'reference')).toMatchObject({ kind: 'input', targetNodeId: 'reference', label: '上传参考视频' });

    const reference = node('reference', 'videoNode', { videoUrl: '/media/reference.mp4', remixRole: 'reference' });
    expect(nextRemixJourneyStep([reference], [], 'reference')).toMatchObject({ kind: 'action', action: 'analyze_reference', label: '分析参考视频' });

    const story = node('story', 'videoStoryNode', { sourceVideoUrl: '/media/reference.mp4', isAnalyzing: true, rows: [] });
    expect(nextRemixJourneyStep([reference, story], [edge('reference', 'story')], 'story')).toMatchObject({ kind: 'wait', label: '等待原片分析' });

    story.data = { ...story.data, isAnalyzing: false, referenceEvents: [{ event_id: 'hook' }] };
    expect(nextRemixJourneyStep([reference, story], [edge('reference', 'story')], 'story')).toMatchObject({ kind: 'action', action: 'continue_story', label: '确认分析并生成脚本' });

    story.data = { ...story.data, referenceBlueprintRef: ref('blueprint') };
    const script = node('script', 'scriptNode', { remixBlueprintRef: ref('blueprint'), remixScriptDraft: { dialogue: '', selections: [] } });
    expect(nextRemixJourneyStep([reference, story, script], [edge('reference', 'story'), edge('story', 'script')], 'script')).toMatchObject({ kind: 'action', action: 'edit_script', label: '填写新主题并生成脚本' });
  });

  it('supports manual narration upload without requiring a voice provider', () => {
    const graph = completeThroughScript();
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'script')).toMatchObject({ kind: 'action', action: 'plan_broll', label: '规划 B-roll' });
    graph.nodes[2].data.remixBrollPlanRef = ref('broll-plan');
    graph.nodes[2].data.remixBrollScriptRef = ref('script');
    graph.nodes[2].data.remixBrollRequirements = [];
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'script')).toMatchObject({ kind: 'action', action: 'create_narration', label: '创建旁白节点' });
    graph.nodes.push(node('audio', 'audioNode', { remixRole: 'narration', remixScriptRef: ref('script'), text: '新品登场' }));
    graph.edges.push(edge('script', 'audio'));
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'audio')).toEqual(expect.objectContaining({ kind: 'input', targetNodeId: 'audio', label: '生成或上传旁白音频', inputHint: expect.stringContaining('上传') }));
  });

  it('resolves the journey from the selected script branch when one story has sibling scripts', () => {
    const graph = completeThroughScript();
    graph.nodes[2].data.remixBrollPlanRef = ref('first-plan');
    graph.nodes[2].data.remixBrollScriptRef = ref('script');
    graph.nodes[2].data.remixBrollRequirements = [];
    graph.nodes.push(node('script-two', 'scriptNode', {
      remixScriptRef: ref('script-two'),
      remixBlueprintRef: ref('blueprint'),
      remixScriptDraft: { dialogue: '第二个分支', selections: [{ id: 'second', label: '第二个事件', start: 0, end: 5 }] },
      remixBrollPlanRef: null,
      remixBrollScriptRef: null,
      remixBrollRequirements: [],
    }));
    graph.edges.push(edge('story', 'script-two'));

    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'script-two')).toMatchObject({
      action: 'plan_broll',
      targetNodeId: 'script-two',
      label: '规划 B-roll',
    });
  });

  it('requires the saved B-roll binding to match the live connected visual node', () => {
    const graph = completeThroughScript();
    const script = graph.nodes[2];
    script.data.remixBrollPlanRef = ref('broll');
    script.data.remixBrollScriptRef = ref('script');
    script.data.remixBrollRequirements = [{ event_id: 'product', required: true }];
    (script.data.remixScriptDraft as { selections: Array<Record<string, unknown>> }).selections[0] = {
      id: 'product', label: '产品登场', start: 0, end: 4,
      visualNodeId: 'visual', visualMediaRevision: 'c'.repeat(64),
    };
    const visual = node('visual', 'uploadNode', { imageUrl: '/visual.png', remixRole: 'visual', remixMedia: { media_kind: 'image', media_path: 'visual.png', media_hash: 'c'.repeat(64), url: '/visual.png' } });
    graph.nodes.push(visual); graph.edges.push(edge('visual', 'script'));
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'script')).toMatchObject({ action: 'create_narration' });

    visual.data.remixRole = 'background';
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'script')).toMatchObject({ label: '补齐必需 B-roll' });
    visual.data.remixRole = 'visual'; visual.data.remixMedia = { media_kind: 'image', media_path: 'visual.png', media_hash: 'd'.repeat(64), url: '/visual.png' };
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'script')).toMatchObject({ label: '补齐必需 B-roll' });
    visual.data.remixMedia = { media_kind: 'image', media_path: 'visual.png', media_hash: 'c'.repeat(64), url: '/old.png' };
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'script')).toMatchObject({ label: '补齐必需 B-roll' });
    visual.data.remixMedia = { media_kind: 'image', media_path: 'visual.png', media_hash: 'c'.repeat(64), url: '/visual.png' };
    graph.edges = graph.edges.filter((item) => item.source !== 'visual');
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'script')).toMatchObject({ label: '补齐必需 B-roll' });
    graph.nodes = graph.nodes.filter((item) => item.id !== 'visual');
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'script')).toMatchObject({ label: '补齐必需 B-roll' });
  });

  it('stops for missing required B-roll and then advances alignment through editable MP4', () => {
    const graph = completeThroughScript();
    const draft = graph.nodes[2].data.remixScriptDraft as { selections: Array<Record<string, unknown>> };
    graph.nodes[2].data.remixBrollPlanRef = ref('broll-plan');
    graph.nodes[2].data.remixBrollScriptRef = ref('script');
    graph.nodes[2].data.remixBrollRequirements = [{ event_id: 'product', required: true }];
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'script')).toMatchObject({ kind: 'input', targetNodeId: 'script', label: '补齐必需 B-roll' });
    draft.selections[0] = { ...draft.selections[0], visualNodeId: 'visual' };
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'script')).toMatchObject({ kind: 'input', label: '补齐必需 B-roll' });
    draft.selections[0].visualMediaRevision = 'c'.repeat(64);
    graph.nodes.push(node('visual', 'uploadNode', { imageUrl: '/visual.png', remixRole: 'visual', remixMedia: { media_kind: 'image', media_path: 'visual.png', media_hash: 'c'.repeat(64), url: '/visual.png' } }));
    graph.edges.push(edge('visual', 'script'));

    graph.nodes.push(
      node('audio', 'audioNode', { audioUrl: '/media/voice.wav', remixRole: 'narration', remixScriptRef: ref('script'), remixMedia: { media_kind: 'audio', media_hash: 'b'.repeat(64), media_path: 'voice.wav', url: '/media/voice.wav' } }),
    );
    graph.edges.push(edge('script', 'audio'));
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'audio')).toMatchObject({ action: 'align_narration' });

    graph.nodes.push(node('words', 'transcriptNode', { artifactRef: ref('words'), alignedScriptRef: ref('script'), status: 'completed', sourceMediaPath: 'voice.wav', sourceMediaHash: 'b'.repeat(64) }));
    graph.edges.push(edge('script', 'words'), edge('audio', 'words'));
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'words')).toMatchObject({ action: 'create_timeline' });

    graph.nodes.push(node('timeline', 'semanticTimelineNode', { artifactRef: ref('timing'), compiledMediaHash: `sha256:${'b'.repeat(64)}`, events: [] }));
    graph.edges.push(edge('script', 'timeline'), edge('words', 'timeline'));
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'timeline')).toMatchObject({ action: 'create_background' });
    graph.nodes.push(node('wrong-background', 'uploadNode', { imageUrl: '/wrong.png', remixRole: 'background', remixMedia: { media_kind: 'image', media_path: 'wrong.png', media_hash: 'e'.repeat(64), url: '/wrong.png' } }));
    graph.edges.push(edge('wrong-background', 'timeline'));
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'timeline')).toMatchObject({ action: 'create_background' });
    graph.nodes.pop(); graph.edges.pop();
    graph.nodes.push(node('background', 'uploadNode', { remixRole: 'background' }));
    graph.edges.push(edge('background', 'script'));
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'background')).toMatchObject({ kind: 'input', label: '上传成片背景 / A-roll' });
    Object.assign(graph.nodes[graph.nodes.length - 1].data, { imageUrl: '/background.png' });
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'background')).toMatchObject({ action: 'bind_background' });
    Object.assign(graph.nodes[graph.nodes.length - 1].data, { remixMedia: { media_kind: 'image', media_path: 'background.png', media_hash: 'd'.repeat(64), url: '/background.png' } });
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'timeline')).toMatchObject({ action: 'create_compose' });

    graph.nodes.push(node('compose', 'videoComposeNode', {}));
    graph.edges.push(edge('timeline', 'compose'));
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'compose')).toMatchObject({ action: 'prepare_manifest' });
    graph.nodes[graph.nodes.length - 1].data.remixManifestRef = ref('manifest');
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'compose')).toMatchObject({ kind: 'wait', label: '加载合成清单' });
    graph.nodes[graph.nodes.length - 1].data.remixManifestLoadedRef = ref('manifest');
    graph.nodes[graph.nodes.length - 1].data.remixManifestLoadState = 'ready';
    graph.nodes[graph.nodes.length - 1].data.remixManifestDirty = false;
    graph.nodes[graph.nodes.length - 1].data.remixNodeRevision = 1;
    graph.nodes[graph.nodes.length - 1].data.remixInputFingerprint = 'current';
    graph.nodes[graph.nodes.length - 1].data.remixInputsCurrent = true;
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'compose')).toMatchObject({ action: 'render_export' });
    graph.nodes[graph.nodes.length - 1].data.resultVideoUrl = '/media/final.mp4';
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'compose')).toMatchObject({ kind: 'complete', label: '成片可编辑并可下载 MP4' });
  });

  it('surfaces dirty, conflicting, unresolved and stale manifests before export', () => {
    const graph = completeThroughScript();
    graph.nodes[2].data.remixBrollPlanRef = ref('broll');
    graph.nodes[2].data.remixBrollScriptRef = ref('script');
    graph.nodes[2].data.remixBrollRequirements = [];
    graph.nodes.push(
      node('audio', 'audioNode', { audioUrl: '/voice.wav', remixRole: 'narration', remixScriptRef: ref('script'), remixMedia: { media_kind: 'audio', media_hash: 'b'.repeat(64), media_path: 'voice.wav', url: '/voice.wav' } }),
      node('words', 'transcriptNode', { artifactRef: ref('words'), alignedScriptRef: ref('script'), status: 'completed', sourceMediaPath: 'voice.wav', sourceMediaHash: 'b'.repeat(64) }),
      node('timeline', 'semanticTimelineNode', { artifactRef: ref('timing'), compiledMediaHash: `sha256:${'b'.repeat(64)}`, events: [] }),
      node('background', 'uploadNode', { imageUrl: '/background.png', remixRole: 'background', remixMedia: { media_kind: 'image', media_path: 'background.png', media_hash: 'd'.repeat(64), url: '/background.png' } }),
      node('compose', 'videoComposeNode', { remixManifestRef: ref('manifest'), remixManifestLoadedRef: ref('manifest'), remixManifestLoadState: 'ready', remixManifestDirty: false, remixNodeRevision: 1, remixInputFingerprint: 'current', remixInputsCurrent: true }),
    );
    graph.edges.push(edge('script', 'audio'), edge('script', 'words'), edge('audio', 'words'), edge('script', 'timeline'), edge('words', 'timeline'), edge('background', 'script'), edge('timeline', 'compose'));
    const compose = graph.nodes[graph.nodes.length - 1];
    compose.data.remixManifestDirty = true; compose.data.remixManifestConflicts = ['仍待处理的字幕冲突'];
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'compose')).toMatchObject({ action: 'resolve_manifest_conflicts', label: '处理清单冲突' });
    compose.data.remixManifestConflicts = [];
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'compose')).toMatchObject({ action: 'save_manifest', label: '保存合成清单修改' });
    compose.data.remixManifestDirty = false; compose.data.remixManifestConflicts = ['字幕冲突'];
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'compose')).toMatchObject({ kind: 'action', action: 'resolve_manifest_conflicts', label: '处理清单冲突' });
    compose.data.remixManifestConflicts = []; compose.data.unresolvedRequiredEvents = ['product'];
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'compose')).toMatchObject({ kind: 'input', targetNodeId: 'timeline', label: '修正未解析事件' });
    compose.data.unresolvedRequiredEvents = []; compose.data.remixInputsCurrent = false;
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'compose')).toMatchObject({ action: 'prepare_manifest', label: '上游已变化，更新清单' });
    compose.data.remixInputsCurrent = true; compose.data.remixManifestLoadState = 'failed';
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'compose')).toMatchObject({
      kind: 'action', action: 'prepare_manifest', label: '重新生成合成清单', targetNodeId: 'compose',
    });
  });
});
