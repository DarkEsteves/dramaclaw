import { createRef } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/api/remix', () => ({
  createRemixArtifact: vi.fn(async () => ({ id: 'blueprint-1', revision: 1 })),
  reviseRemixArtifact: vi.fn(async () => ({ id: 'already-saved', revision: 4 })),
  submitRemixProcessAction: vi.fn(async () => ({ operation_id: 'evidence-op' })),
  fetchRemixOperation: vi.fn(async () => ({ operation_id: 'evidence-op', status: 'needs_confirmation', output_refs: [{ artifact_id: 'evidence-1', revision: 1 }] })),
  fetchRemixArtifact: vi.fn(async () => ({ id: 'evidence-1', revision: 1, schema: 'reference_evidence.v1', payload: { media_hash: 'hash', duration_us: 2_000_000, cuts: [], representative_frames: [] } })),
}));
vi.mock('@/lib/url-params', () => ({ readUrl: () => ({ project: 'demo', canvas: 'default' }) }));

import { createRemixArtifact, fetchRemixArtifact, reviseRemixArtifact, submitRemixProcessAction } from '@/api/remix';
import { ReferenceBlueprintPanel, type ReferenceBlueprintPanelHandle } from './blueprintPanel';

const source = { mediaPath: 'reference.mp4', mediaHash: 'hash', durationUs: 2_000_000, videoUrl: '/reference.mp4', transcriptRef: { artifactId: 'transcript-1', revision: 2 } };
const rows = [{ startTime: '0', endTime: '1', visualDescription: '杯子特写', narrative: '引出产品' }];
const reviewedPayload = (reviewState = 'reviewed') => ({
  narrative_role: '整体作用', evidence_ref: { artifact_id: 'evidence-1', revision: 1 }, transcript_ref: { artifact_id: 'transcript-1', revision: 2 },
  preserve_replace_rules: [], observations: [], inferences: [], user_choices: [],
  events: [{
    event_id: 'event-1', start_us: 0, end_us: 1_000_000,
    observed_frame: { evidence_ref: { artifact_id: 'evidence-1', revision: 1 }, pts: 0, time_base: '1/1000', time_us: 0 },
    spoken_range: null, model: { visual_observation: '人物举杯', narrative_inference: '模型作用' },
    user_override: { narrative_role: '用户作用', review_state: reviewState },
  }],
});

describe('ReferenceBlueprintPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchRemixArtifact).mockImplementation(async (_project, ref) => ref.artifactId === 'evidence-1'
      ? { id: 'evidence-1', revision: 1, schema: 'reference_evidence.v1', payload: { media_hash: 'hash', duration_us: 2_000_000, cuts: [], representative_frames: [] } }
      : { id: ref.artifactId, revision: ref.revision, schema: 'reference_blueprint.v1', payload: reviewedPayload(), upstream_refs: [{ artifact_id: 'evidence-1', revision: 1 }, { artifact_id: 'transcript-1', revision: 2 }] });
  });

  it('keeps wheel scrolling inside the expanded blueprint settings', () => {
    const { container } = render(<ReferenceBlueprintPanel nodeId="story" source={source} rows={rows} draft={{}} onPatch={() => {}} onReanalyze={() => {}} />);
    expect(container.firstElementChild).toHaveClass('nowheel');
  });

  it('keeps adaptation rules visible and puts technical lineage under generation evidence', () => {
    render(<ReferenceBlueprintPanel nodeId="story" source={source} rows={rows} draft={{ referenceEvidenceRef: { artifactId: 'evidence-1', revision: 1 } }} onPatch={() => {}} onReanalyze={() => {}} />);
    expect(screen.getByLabelText('整体叙事作用')).toBeVisible();
    expect(screen.getByLabelText('保留规则')).toBeVisible();
    expect(screen.getByLabelText('替换规则')).toBeVisible();
    expect(screen.getByLabelText('整体叙事作用')).toHaveClass('ui-field');
    expect(screen.getByLabelText('保留规则')).toHaveClass('ui-field');
    expect(screen.getByLabelText('替换规则')).toHaveClass('ui-field');
    const summary = screen.getByText('查看生成依据');
    const details = summary.closest('details');
    expect(details).toBeTruthy();
    expect(details).not.toHaveAttribute('open');
    expect(details).toHaveTextContent('原片 reference.mp4');
    expect(details).toHaveTextContent('证据 evidence-1 · r1');
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
    const panel = createRef<ReferenceBlueprintPanelHandle>();
    render(<ReferenceBlueprintPanel ref={panel} nodeId="story" source={source} rows={rows} draft={{ referenceEvidenceRef: { artifactId: 'evidence-1', revision: 1 } }} onPatch={onPatch} onReanalyze={() => {}} />);
    fireEvent.change(screen.getByLabelText('整体叙事作用'), { target: { value: '先提问题后展示产品' } });
    fireEvent.change(screen.getByLabelText('保留规则'), { target: { value: '保留节奏' } });
    fireEvent.change(screen.getByLabelText('替换规则'), { target: { value: '替换品牌画面' } });
    await panel.current?.ensureSaved();
    await waitFor(() => expect(createRemixArtifact).toHaveBeenCalledWith('demo', 'reference_blueprint.v1', expect.objectContaining({ narrative_role: '先提问题后展示产品' }), [{ artifactId: 'evidence-1', revision: 1 }, { artifactId: 'transcript-1', revision: 2 }]));
    expect(onPatch).toHaveBeenCalledWith(expect.objectContaining({ referenceBlueprintRef: { artifactId: 'blueprint-1', revision: 1 } }));
  });

  it('reuses the saved blueprint when the rules have not changed', async () => {
    const panel = createRef<ReferenceBlueprintPanelHandle>();
    const saved = { artifactId: 'already-saved', revision: 3 };
    render(<ReferenceBlueprintPanel ref={panel} nodeId="story" source={source} rows={rows} draft={{ referenceBlueprintRef: saved, referenceEvidenceRef: { artifactId: 'evidence-1', revision: 1 } }} onPatch={() => {}} onReanalyze={() => {}} />);
    expect(await panel.current?.ensureSaved()).toEqual(saved);
    expect(fetchRemixArtifact).toHaveBeenCalledWith('demo', saved);
    expect(createRemixArtifact).not.toHaveBeenCalled();
  });

  it.each([
    ['legacy', { narrative_role: '旧蓝图', events: [{ shot: 1 }], preserve_replace_rules: [], observations: [], inferences: [], user_choices: [] }],
    ['unreviewed', reviewedPayload('needs_review')],
    ['broken reference', { ...reviewedPayload(), evidence_ref: { artifact_id: 'other-evidence', revision: 1 }, events: [{ ...reviewedPayload().events[0], observed_frame: { ...reviewedPayload().events[0].observed_frame, evidence_ref: { artifact_id: 'other-evidence', revision: 1 } } }] }],
  ])('rejects a clean saved %s blueprint before reuse', async (_label, payload) => {
    vi.mocked(fetchRemixArtifact).mockResolvedValue({
      id: 'already-saved', revision: 3, schema: 'reference_blueprint.v1', payload,
      upstream_refs: [{ artifact_id: 'evidence-1', revision: 1 }, { artifact_id: 'transcript-1', revision: 2 }],
    });
    const panel = createRef<ReferenceBlueprintPanelHandle>();
    render(<ReferenceBlueprintPanel ref={panel} nodeId="story" source={source} rows={rows} draft={{ referenceBlueprintRef: { artifactId: 'already-saved', revision: 3 }, referenceEvidenceRef: { artifactId: 'evidence-1', revision: 1 } }} onPatch={() => {}} onReanalyze={() => {}} />);

    await expect(panel.current?.ensureSaved()).rejects.toThrow();
    expect(createRemixArtifact).not.toHaveBeenCalled();
    expect(reviseRemixArtifact).not.toHaveBeenCalled();
  });

  it('saves user overrides as a new revision of the existing blueprint', async () => {
    const onPatch = vi.fn();
    const panel = createRef<ReferenceBlueprintPanelHandle>();
    const saved = { artifactId: 'already-saved', revision: 3 };
    render(<ReferenceBlueprintPanel ref={panel} nodeId="story" source={source} rows={[]} referenceEvents={[{
      event_id: 'event-1', start_us: 0, end_us: 1_000_000,
      observed_frame: { pts: 0, time_base: '1/1000', time_us: 0 },
      user_narrative_role: '用户角色', review_state: 'reviewed',
    }]} draft={{ referenceBlueprintRef: saved, referenceEvidenceRef: { artifactId: 'evidence-1', revision: 1 }, blueprintNarrativeRole: '整体作用', blueprintDirty: true }} onPatch={onPatch} onReanalyze={() => {}} />);

    expect(await panel.current?.ensureSaved()).toEqual({ artifactId: 'already-saved', revision: 4 });
    expect(reviseRemixArtifact).toHaveBeenCalledWith('demo', saved, expect.objectContaining({ events: [expect.objectContaining({ event_id: 'event-1' })] }), [{ artifactId: 'evidence-1', revision: 1 }, { artifactId: 'transcript-1', revision: 2 }]);
    expect(createRemixArtifact).not.toHaveBeenCalled();
    expect(onPatch).toHaveBeenCalledWith(expect.objectContaining({ referenceBlueprintRef: { artifactId: 'already-saved', revision: 4 }, blueprintDirty: false }));
  });

  it('restores event overrides from the exact saved revision after reload', async () => {
    vi.mocked(fetchRemixArtifact).mockResolvedValueOnce({
      id: 'already-saved', revision: 3, schema: 'reference_blueprint.v1', payload: {
        narrative_role: '保存的整体作用', preserve_replace_rules: [], observations: [], inferences: [], user_choices: [],
        events: [{
          event_id: 'event-1', start_us: 0, end_us: 1_000_000,
          observed_frame: { evidence_ref: { artifact_id: 'evidence-1', revision: 1 }, pts: 0, time_base: '1/1000', time_us: 0 },
          spoken_range: null, model: { visual_observation: '人物举杯', narrative_inference: '模型作用' },
          user_override: { narrative_role: '保存的用户作用', review_state: 'reviewed' },
        }],
      },
    });
    const onPatch = vi.fn();
    render(<ReferenceBlueprintPanel nodeId="story" source={source} rows={[]} referenceEvents={[]} draft={{ referenceBlueprintRef: { artifactId: 'already-saved', revision: 3 }, referenceEvidenceRef: { artifactId: 'evidence-1', revision: 1 } }} onPatch={onPatch} onReanalyze={() => {}} />);

    await waitFor(() => expect(onPatch).toHaveBeenCalledWith(expect.objectContaining({
      blueprintNarrativeRole: '保存的整体作用',
      referenceEvents: [expect.objectContaining({ event_id: 'event-1', user_narrative_role: '保存的用户作用', review_state: 'reviewed' })],
    })));
  });
});
