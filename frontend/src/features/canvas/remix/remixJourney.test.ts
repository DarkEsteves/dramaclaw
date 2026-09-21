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
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'script')).toMatchObject({ kind: 'action', action: 'create_narration', label: '创建旁白节点' });
    graph.nodes.push(node('audio', 'audioNode', { remixRole: 'narration', remixScriptRef: ref('script'), text: '新品登场' }));
    graph.edges.push(edge('script', 'audio'));
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'audio')).toEqual(expect.objectContaining({ kind: 'input', targetNodeId: 'audio', label: '生成或上传旁白音频', inputHint: expect.stringContaining('上传') }));
  });

  it('stops for missing required B-roll and then advances alignment through editable MP4', () => {
    const graph = completeThroughScript();
    const draft = graph.nodes[2].data.remixScriptDraft as { selections: Array<Record<string, unknown>> };
    draft.selections[0] = { ...draft.selections[0], visualNodeId: 'visual' };
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'script')).toMatchObject({ kind: 'input', targetNodeId: 'script', label: '补齐必需 B-roll' });
    draft.selections[0].visualMediaRevision = 'c'.repeat(64);

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
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'timeline')).toMatchObject({ action: 'create_compose' });

    graph.nodes.push(node('compose', 'videoComposeNode', {}));
    graph.edges.push(edge('timeline', 'compose'));
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'compose')).toMatchObject({ action: 'prepare_manifest' });
    graph.nodes[graph.nodes.length - 1].data.remixManifestRef = ref('manifest');
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'compose')).toMatchObject({ action: 'render_export' });
    graph.nodes[graph.nodes.length - 1].data.resultVideoUrl = '/media/final.mp4';
    expect(nextRemixJourneyStep(graph.nodes, graph.edges, 'compose')).toMatchObject({ kind: 'complete', label: '成片可编辑并可下载 MP4' });
  });
});
