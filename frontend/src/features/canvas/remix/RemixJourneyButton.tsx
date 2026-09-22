import { FastForward } from 'lucide-react';
import { UiChipButton } from '@/components/ui';
import { TOOLBAR_TEXT_BUTTON_CLASS } from '@/features/canvas/ui/nodeToolbarStyles';
import type { RemixJourneyStep } from './remixJourney';

export function RemixJourneyButton({ nodeId, step, onContinue }: { nodeId: string; step: RemixJourneyStep; onContinue: () => void }) {
  if (step.targetNodeId !== nodeId) return null;
  const label = step.kind === 'complete' ? step.label : `继续改编 · ${step.label}`;
  return (
    <UiChipButton
      className={TOOLBAR_TEXT_BUTTON_CLASS}
      title={step.inputHint ?? step.label}
      onClick={(event) => { event.stopPropagation(); onContinue(); }}
    >
      <FastForward className="h-3.5 w-3.5" />
      {label}
    </UiChipButton>
  );
}
