import { describe, expect, it } from 'vitest';
import type { CanvasNode, CanvasEdge } from '@/features/canvas/domain/canvasNodes';
import { captureCanvasBaseline, mergeConfirmedCanvasWithLocalChanges } from './workbenchCanvasMerge';

const node = (id: string, data: Record<string, unknown>) => ({ id, type: 'videoComposeNode', position: { x: 0, y: 0 }, data }) as CanvasNode;
const edge = (id: string) => ({ id, source: 'compose', target: 'other' }) as CanvasEdge;

describe('confirmed remix canvas merge', () => {
  it('keeps edits made after the pre-confirm flush while adopting render result and remote revision content', () => {
    const baselineNodes = [node('compose', { remixManifestRef: { artifactId: 'm', revision: 1 }, remixNodeRevision: 2 }), node('other', { prompt: 'before' })];
    const baselineEdges = [edge('e1')];
    const baseline = captureCanvasBaseline(baselineNodes, baselineEdges);
    const remoteNodes = [node('compose', { ...baselineNodes[0].data, resultVideoUrl: '/result.mp4' }), baselineNodes[1], node('remote-added', {})];
    const localNodes = [baselineNodes[0], node('other', { prompt: 'after' }), node('local-added', {})];
    const merged = mergeConfirmedCanvasWithLocalChanges(remoteNodes, baselineEdges, localNodes, [...baselineEdges, edge('e2')], baseline, 'compose');
    expect(merged.nodes.find((item) => item.id === 'compose')?.data).toHaveProperty('resultVideoUrl', '/result.mp4');
    expect(merged.nodes.find((item) => item.id === 'other')?.data).toHaveProperty('prompt', 'after');
    expect(merged.nodes.map((item) => item.id)).toEqual(['compose', 'other', 'remote-added', 'local-added']);
    expect(merged.edges.map((item) => item.id)).toEqual(['e1', 'e2']);
  });
  it('preserves a local title edit on the render node and the confirmed result URL', () => {
    const base = node('compose', { displayName: 'before', remixManifestRef: { artifactId: 'm', revision: 1 }, remixNodeRevision: 2 });
    const remote = node('compose', { ...base.data, resultVideoUrl: '/result.mp4' });
    const local = node('compose', { ...base.data, displayName: 'after' });
    const merged = mergeConfirmedCanvasWithLocalChanges([remote], [], [local], [], captureCanvasBaseline([base], []), 'compose');
    expect(merged.nodes[0].data).toMatchObject({ displayName: 'after', resultVideoUrl: '/result.mp4' });
  });
  it('does not resurrect a node removed remotely when it was unchanged locally', () => {
    const removed = node('removed', { prompt: 'old' });
    const merged = mergeConfirmedCanvasWithLocalChanges([], [], [removed], [], captureCanvasBaseline([removed], []), 'compose');
    expect(merged.nodes).toEqual([]);
  });
});
