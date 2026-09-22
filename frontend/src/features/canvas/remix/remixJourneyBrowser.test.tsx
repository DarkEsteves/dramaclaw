import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { CanvasEdge, CanvasNode } from '@/features/canvas/domain/canvasNodes';
import { RemixJourneyButton } from './RemixJourneyButton';
import { advanceRemixJourneyStep } from './remixJourneyActions';
import { nextRemixJourneyStep } from './remixJourney';

const ref = (artifactId: string) => ({ artifactId, revision: 1 });
const node = (id: string, type: CanvasNode['type'], data: Record<string, unknown>): CanvasNode => ({ id, type, data, position: { x: 0, y: 0 } } as CanvasNode);
const edge = (source: string, target: string): CanvasEdge => ({ id: `${source}-${target}`, source, target });

describe('browser remix journey', () => {
  it('clicks the visible toolbar journey from Recipe B-roll output through manual audio and render', async () => {
    const nodes = [
      node('reference', 'videoNode', { videoUrl: '/reference.mp4', remixRole: 'reference' }),
      node('story', 'videoStoryNode', { referenceBlueprintRef: ref('blueprint'), referenceEvents: [{ event_id: 'hook' }] }),
      node('script', 'scriptNode', { remixScriptRef: ref('script'), remixScriptDraft: { dialogue: '新品登场', selections: [{ id: 'product', label: '产品', start: 0, end: 4 }] } }),
    ];
    const edges = [edge('reference', 'story'), edge('story', 'script')];
    const calls: string[] = [];
    const ports = {
      focus: vi.fn(), notify: vi.fn(),
      analyzeReference: vi.fn(),
      publish: vi.fn((nodeId: string, action: string) => {
        calls.push(action);
        const target = nodes.find((item) => item.id === nodeId)!;
        if (action === 'plan_broll') Object.assign(target.data, {
          remixBrollPlanRef: ref('broll-plan'), remixBrollScriptRef: ref('script'),
          remixBrollRequirements: [{ event_id: 'product', required: true, purpose: '展示产品', suggested_query: '产品特写' }],
        });
        if (action === 'compile') Object.assign(target.data, { artifactRef: ref('timing'), compiledMediaHash: `sha256:${'b'.repeat(64)}` });
        if (action === 'prepare_compose') Object.assign(target.data, { remixManifestRef: ref('manifest'), remixManifestLoadedRef: ref('manifest'), remixManifestLoadState: 'ready', remixManifestDirty: false, remixNodeRevision: 1, remixInputFingerprint: 'current', remixInputsCurrent: true });
        if (action === 'render_export') target.data.resultVideoUrl = '/final.mp4';
      }),
      createNarration: vi.fn(() => { nodes.push(node('audio', 'audioNode', { remixRole: 'narration', remixScriptRef: ref('script'), text: '新品登场' })); edges.push(edge('script', 'audio')); }),
      alignNarration: vi.fn(async () => { nodes.push(node('words', 'transcriptNode', { artifactRef: ref('words'), alignedScriptRef: ref('script'), status: 'completed', sourceMediaPath: 'voice.wav', sourceMediaHash: 'b'.repeat(64) })); edges.push(edge('script', 'words'), edge('audio', 'words')); }),
      createTimeline: vi.fn(() => { nodes.push(node('timeline', 'semanticTimelineNode', { events: [] })); edges.push(edge('script', 'timeline'), edge('words', 'timeline')); }),
      createBackground: vi.fn(() => { nodes.push(node('background', 'uploadNode', { remixRole: 'background' })); edges.push(edge('background', 'script')); }),
      bindBackground: vi.fn(async () => { Object.assign(nodes.find((item) => item.id === 'background')!.data, { imageUrl: '/background.png', remixMedia: { media_kind: 'image', media_path: 'background.png', media_hash: 'd'.repeat(64), url: '/background.png' } }); }),
      createCompose: vi.fn(() => { nodes.push(node('compose', 'videoComposeNode', {})); edges.push(edge('timeline', 'compose')); }),
    };
    const view = render(<div />);
    const clickNext = async (selected: string) => {
      const step = nextRemixJourneyStep(nodes, edges, selected)!;
      view.rerender(<RemixJourneyButton nodeId={selected} step={step} onContinue={() => { void advanceRemixJourneyStep(step, ports); }} />);
      fireEvent.click(screen.getByRole('button'));
      await Promise.resolve();
      return step;
    };

    expect((await clickNext('script')).action).toBe('plan_broll');
    expect(nodes[2].data.remixBrollRequirements).toEqual([expect.objectContaining({ event_id: 'product', required: true })]);
    expect((await clickNext('script')).label).toBe('补齐必需 B-roll');
    nodes.push(node('visual', 'uploadNode', { imageUrl: '/visual.png', remixRole: 'visual', remixMedia: { media_kind: 'image', media_path: 'visual.png', media_hash: 'c'.repeat(64), url: '/visual.png' } })); edges.push(edge('visual', 'script'));
    Object.assign((nodes[2].data.remixScriptDraft as { selections: Array<Record<string, unknown>> }).selections[0], { visualNodeId: 'visual', visualMediaRevision: 'c'.repeat(64) });
    expect((await clickNext('script')).action).toBe('create_narration');
    expect(nodes.some((item) => item.id === 'audio')).toBe(true);
    expect((await clickNext('audio')).label).toBe('生成或上传旁白音频');
    Object.assign(nodes.find((item) => item.id === 'audio')!.data, { audioUrl: '/voice.wav', remixMedia: { media_kind: 'audio', media_hash: 'b'.repeat(64), media_path: 'voice.wav', url: '/voice.wav' } });
    expect((await clickNext('audio')).action).toBe('align_narration');
    expect((await clickNext('script')).action).toBe('create_timeline');
    expect((await clickNext('timeline')).action).toBe('compile_timeline');
    expect((await clickNext('script')).action).toBe('create_background');
    expect((await clickNext('background')).label).toBe('上传成片背景 / A-roll');
    Object.assign(nodes.find((item) => item.id === 'background')!.data, { imageUrl: '/background.png' });
    expect((await clickNext('background')).action).toBe('bind_background');
    expect((await clickNext('timeline')).action).toBe('create_compose');
    expect((await clickNext('compose')).action).toBe('prepare_manifest');
    expect((await clickNext('compose')).action).toBe('render_export');
    expect(nextRemixJourneyStep(nodes, edges, 'compose')).toMatchObject({ kind: 'complete' });
    expect(calls).toEqual(['plan_broll', 'edit_script', 'compile', 'prepare_compose', 'render_export']);
  });
});
