import type { RemixJourneyStep } from './remixJourney';

export type RemixPublishedAction =
  | 'continue_story'
  | 'edit_script'
  | 'plan_broll'
  | 'compile'
  | 'create_compose'
  | 'prepare_compose'
  | 'save_manifest'
  | 'render_export';

export interface RemixJourneyActionPorts {
  focus: (nodeId: string) => void;
  notify: (message: string) => void;
  analyzeReference: (nodeId: string) => void;
  publish: (nodeId: string, action: RemixPublishedAction) => void;
  createNarration: (nodeId: string) => void;
  alignNarration: (nodeId: string) => Promise<unknown> | void;
  createTimeline: (nodeId: string) => void;
  createCompose: (nodeId: string) => void;
}

/** Execute the resolver's single visible action through injected canvas ports. */
export async function advanceRemixJourneyStep(
  step: RemixJourneyStep,
  ports: RemixJourneyActionPorts,
): Promise<void> {
  ports.focus(step.targetNodeId);
  if (!step.action) {
    if (step.inputHint) ports.notify(step.inputHint);
    return;
  }
  switch (step.action) {
    case 'analyze_reference': ports.analyzeReference(step.targetNodeId); return;
    case 'continue_story': ports.publish(step.targetNodeId, 'continue_story'); return;
    case 'edit_script': ports.publish(step.targetNodeId, 'edit_script'); return;
    case 'plan_broll': ports.publish(step.targetNodeId, 'plan_broll'); return;
    case 'create_narration': ports.createNarration(step.targetNodeId); return;
    case 'align_narration': await ports.alignNarration(step.targetNodeId); return;
    case 'create_timeline': ports.createTimeline(step.targetNodeId); return;
    case 'compile_timeline': ports.publish(step.targetNodeId, 'compile'); return;
    case 'create_compose': ports.createCompose(step.targetNodeId); return;
    case 'prepare_manifest': ports.publish(step.targetNodeId, 'prepare_compose'); return;
    case 'save_manifest': ports.publish(step.targetNodeId, 'save_manifest'); return;
    case 'render_export': ports.publish(step.targetNodeId, 'render_export'); return;
  }
}
