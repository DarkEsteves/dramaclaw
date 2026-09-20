import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/api/remix', () => ({
  createRemixArtifact: vi.fn(async () => ({ id: 'blueprint-1', revision: 1 })),
  submitRemixProcessAction: vi.fn(async () => ({ operation_id: 'evidence-op' })),
  fetchRemixOperation: vi.fn(async () => ({ operation_id: 'evidence-op', status: 'needs_confirmation', output_refs: [{ artifact_id: 'evidence-1', revision: 1 }] })),
  fetchRemixArtifact: vi.fn(async () => ({ id: 'evidence-1', revision: 1, schema: 'reference_evidence.v1', payload: { media_hash: 'hash', duration_us: 2_000_000, cuts: [], representative_frames: [] } })),
}));
vi.mock('@/lib/url-params', () => ({ readUrl: () => ({ project: 'demo', canvas: 'default' }) }));

import { createRemixArtifact, submitRemixProcessAction } from '@/api/remix';
import { ReferenceBlueprintPanel } from './blueprintPanel';

const source = { mediaPath: 'reference.mp4', mediaHash: 'hash', durationUs: 2_000_000, videoUrl: '/reference.mp4', transcriptRef: { artifactId: 'transcript-1', revision: 2 } };
const rows = [{ startTime: '0', endTime: '1', visualDescription: '杯子特写', narrative: '引出产品' }];

describe('ReferenceBlueprintPanel', () => {
  beforeEach(() => vi.clearAllMocks());

  it('keeps wheel scrolling inside the expanded blueprint settings', () => {
    const { container } = render(<ReferenceBlueprintPanel nodeId="story" source={source} rows={rows} draft={{}} onPatch={() => {}} onReanalyze={() => {}} />);
    expect(container.firstElementChild).toHaveClass('nowheel');
  });

  it('extracts full-video evidence and persists the resulting reference', async () => {
    const onPatch = vi.fn();
    render(<ReferenceBlueprintPanel nodeId="story" source={source} rows={rows} draft={{}} onPatch={onPatch} onReanalyze={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: '提取原片证据' }));
    await waitFor(() => expect(submitRemixProcessAction).toHaveBeenCalledWith('demo', expect.objectContaining({ action: 'analyze_reference_video', mediaPath: 'reference.mp4' })));
    await waitFor(() => expect(onPatch).toHaveBeenCalledWith(expect.objectContaining({ referenceEvidenceRef: { artifactId: 'evidence-1', revision: 1 } })));
  });

  it('does not start a second extraction while one-click analysis owns the task', () => {
    render(<ReferenceBlueprintPanel nodeId="story" source={source} rows={rows} draft={{ managedEvidenceOperationId: 'running-op' }} onPatch={() => {}} onReanalyze={() => {}} />);
    expect((screen.getByRole('button', { name: '提取中…' }) as HTMLButtonElement).disabled).toBe(true);
    expect(submitRemixProcessAction).not.toHaveBeenCalled();
  });

  it('saves an editable blueprint with explicit evidence lineage', async () => {
    const onPatch = vi.fn();
    render(<ReferenceBlueprintPanel nodeId="story" source={source} rows={rows} draft={{ referenceEvidenceRef: { artifactId: 'evidence-1', revision: 1 } }} onPatch={onPatch} onReanalyze={() => {}} />);
    fireEvent.change(screen.getByLabelText('整体叙事作用'), { target: { value: '先提问题后展示产品' } });
    fireEvent.change(screen.getByLabelText('保留规则'), { target: { value: '保留节奏' } });
    fireEvent.change(screen.getByLabelText('替换规则'), { target: { value: '替换品牌画面' } });
    fireEvent.click(screen.getByRole('button', { name: '保存参考蓝图' }));
    await waitFor(() => expect(createRemixArtifact).toHaveBeenCalledWith('demo', 'reference_blueprint.v1', expect.objectContaining({ narrative_role: '先提问题后展示产品' }), [{ artifactId: 'evidence-1', revision: 1 }, { artifactId: 'transcript-1', revision: 2 }]));
    expect(onPatch).toHaveBeenCalledWith(expect.objectContaining({ referenceBlueprintRef: { artifactId: 'blueprint-1', revision: 1 } }));
  });
});
