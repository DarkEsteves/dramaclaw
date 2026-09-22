import { FastForward, Loader2 } from 'lucide-react';
import { UiChipButton } from '@/components/ui';
import { TOOLBAR_TEXT_BUTTON_CLASS } from '@/features/canvas/ui/nodeToolbarStyles';
import type { RemixJourneyStep } from './remixJourney';

export function RemixJourneyButton({ nodeId, step, onContinue }: { nodeId: string; step: RemixJourneyStep; onContinue: () => void }) {
  if (step.targetNodeId !== nodeId) return null;
  const waiting = step.kind === 'wait';
  const label = waiting || step.kind === 'complete' ? step.label : `继续改编 · ${step.label}`;
  return (
    <UiChipButton
      className={TOOLBAR_TEXT_BUTTON_CLASS}
      disabled={waiting}
      title={step.inputHint ?? step.label}
      onClick={(event) => { event.stopPropagation(); onContinue(); }}
    >
      {waiting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FastForward className="h-3.5 w-3.5" />}
      {label}
    </UiChipButton>
  );
}
