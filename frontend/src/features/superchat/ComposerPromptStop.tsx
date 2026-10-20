import { Button } from '@/components/ui/button';

/** The normal composer stop is hidden while a question occupies its place. */
export function ComposerPromptStop({ hasActiveComposerPrompt, busy, onAbort, label }: {
  hasActiveComposerPrompt: boolean;
  busy: boolean;
  onAbort: () => void;
  label: string;
}) {
  if (!hasActiveComposerPrompt || !busy) return null;
  return (
    <div className="flex justify-end px-3 pb-3">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="gap-2 rounded-full bg-foreground/10 text-foreground shadow-none hover:bg-foreground/15"
        onClick={onAbort}
        aria-label={label}
        title={label}
      >
        <span className="size-2.5 rounded-sm bg-current" aria-hidden />
        {label}
      </Button>
    </div>
  );
}
