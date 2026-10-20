import { describe, expect, it } from 'vitest';
import type { CanvasEdge, CanvasNode } from '@/stores/canvasStore';
import type { ReferenceAnalysisRun } from '@/api/referenceAnalysisRuns';
import { referenceAnalysisRunMatchesScope, mergeReferenceAnalysisCanvas, mergeReferenceAnalysisMetadata, shouldRefreshReferenceAnalysis } from './referenceAnalysisEvents';

const run = { run_id: 'run-1', project_id: 'project-1', canvas_id: 'canvas-1', agent_id: 'main', source_node_id: 'video', analysis_node_id: 'story', status: 'analysis_ready' } as ReferenceAnalysisRun;
const node = (id: string, data: Record<string, unknown> = {}, x = 0): CanvasNode => ({ id, type: id === 'story' ? 'videoStoryNode' : 'videoNode', position: { x, y: 0 }, data } as CanvasNode);
const edge = (id: string, source = 'video', target = 'story'): CanvasEdge => ({ id, source, target });

describe('reference analysis events', () => {
  it('only handles the current project, canvas and agent', () => {
    expect(referenceAnalysisRunMatchesScope(run, 'project-1', 'canvas-1', 'main')).toBe(true);
    expect(referenceAnalysisRunMatchesScope(run, 'project-2', 'canvas-1', 'main')).toBe(false);
    expect(referenceAnalysisRunMatchesScope(run, 'project-1', 'canvas-2', 'main')).toBe(false);
    expect(referenceAnalysisRunMatchesScope(run, 'project-1', 'canvas-1', 'agent-2')).toBe(false);
  });
  it('refreshes the persisted analysis node at submission and after completion', () => {
    expect(shouldRefreshReferenceAnalysis({ ...run, status: 'analyzing' })).toBe(true);
    expect(shouldRefreshReferenceAnalysis(run)).toBe(true);
    expect(shouldRefreshReferenceAnalysis({ ...run, status: 'resume_failed' })).toBe(true);
    expect(shouldRefreshReferenceAnalysis({ ...run, analysis_node_id: '' })).toBe(false);
  });
  it('keeps new server nodes and analysis links while preserving unrelated local edits', () => {
    const source = node('video', { videoUrl: 'source.mp4' }, 700);
    const local = node('manual', { videoUrl: 'manual.mp4' }, 900);
    const analysis = node('story', { rows: [{ narrative: 'result' }], isAnalyzing: false }, 300);
    const result = mergeReferenceAnalysisCanvas(
      [node('video'), analysis, node('unrelated-server')], [edge('analysis'), edge('unrelated', 'video', 'unrelated-server')],
      [source, local], [edge('manual-edge', 'video', 'manual')], run, { nodes: [node('video')], edges: [], revision: 1 },
    );
    expect(result.nodes).toEqual(expect.arrayContaining([source, local, analysis, node('unrelated-server')].map((item) => expect.objectContaining({ id: item.id, position: item.position, data: expect.objectContaining(item.data) }))));
    expect(result.edges).toEqual(expect.arrayContaining([edge('manual-edge', 'video', 'manual'), edge('analysis'), edge('unrelated', 'video', 'unrelated-server')].map((item) => expect.objectContaining(item))));
  });
  it('updates result fields without overwriting a moved node or manual title edits', () => {
    const base = node('story', { rows: [], isAnalyzing: true, displayName: '原片拆解' }, 300);
    const local = node('story', { rows: [], isAnalyzing: true, displayName: '我的拆解' }, 900);
    const remote = node('story', { rows: [{ narrative: 'generated' }], isAnalyzing: false, rawResult: { ready: true }, displayName: '原片拆解' }, 300);
    const result = mergeReferenceAnalysisCanvas([remote], [], [local], [], run, { nodes: [base], edges: [], revision: 1 });
    expect(result.nodes[0]).toMatchObject({ position: { x: 900 }, data: { rows: [{ narrative: 'generated' }], isAnalyzing: false, displayName: '我的拆解', rawResult: { ready: true } } });
  });
  it('does not resurrect a deleted analysis node or source link', () => {
    const analysis = node('story');
    const result = mergeReferenceAnalysisCanvas([node('video'), analysis], [edge('analysis')], [node('video')], [], run, { nodes: [node('video'), analysis], edges: [edge('analysis')], revision: 1 });
    expect(result).toMatchObject({ nodes: [node('video')], edges: [] });
  });
  it('stops before applying when local and server edited the same result field', () => {
    const base = node('story', { rows: [] });
    const local = node('story', { rows: [{ narrative: 'manual' }] });
    const remote = node('story', { rows: [{ narrative: 'generated' }] });
    expect(() => mergeReferenceAnalysisCanvas([remote], [], [local], [], run, { nodes: [base], edges: [], revision: 1 })).toThrow('同时修改');
  });
  it('merges an existing plan updated on the server alongside independent local edits', () => {
    const basePlan = node('plan', { productionPlanRef: { revision: 1 } });
    const newPlan = node('plan', { productionPlanRef: { revision: 2 } });
    const localPlan = { ...basePlan, position: { x: 700, y: 0 } };
    const result = mergeReferenceAnalysisCanvas([newPlan], [], [localPlan], [], run, { nodes: [basePlan], edges: [], revision: 1 });
    expect(result.nodes).toMatchObject([{ ...newPlan, position: { x: 700, y: 0 } }]);
  });
  it('allows deletion when the server only reordered fields or changed selection', () => {
    const base = node('story', { rows: [], displayName: '拆解' });
    const remote = { ...node('story', { displayName: '拆解', rows: [] }), selected: true };
    const result = mergeReferenceAnalysisCanvas([remote], [], [], [], run, { nodes: [base], edges: [], revision: 1 });
    expect(result.nodes).toEqual([]);
  });
  it('recognizes frontend hydration defaults as unchanged when the backend fills the analysis result', () => {
    const base = node('story', { rows: [], isAnalyzing: true });
    const local = node('story', { rows: [], isAnalyzing: true, referenceBlueprintRef: null });
    const remote = node('story', { rows: [], isAnalyzing: false, referenceBlueprintRef: { artifactId: 'blueprint', revision: 1 } });
    const result = mergeReferenceAnalysisCanvas([remote], [], [local], [], run, { nodes: [base], edges: [], revision: 1 });
    expect(result.nodes[0].data).toMatchObject({ isAnalyzing: false, referenceBlueprintRef: { artifactId: 'blueprint', revision: 1 } });
  });
  it('preserves local bookmarks alongside independent server metadata changes', () => {
    expect(mergeReferenceAnalysisMetadata({ viewportBookmarks: { one: null }, goal: 'old' }, { viewportBookmarks: { one: { x: 12 } }, goal: 'old' }, { viewportBookmarks: { one: null }, goal: 'new' }))
      .toEqual({ viewportBookmarks: { one: { x: 12 } }, goal: 'new' });
  });
  it('rejects conflicting metadata before an automatic refresh can replace local bookmarks', () => {
    expect(() => mergeReferenceAnalysisMetadata({ viewportBookmarks: { one: null } }, { viewportBookmarks: { one: { x: 12 } } }, { viewportBookmarks: { one: { x: 24 } } })).toThrow('同时修改');
  });
});
