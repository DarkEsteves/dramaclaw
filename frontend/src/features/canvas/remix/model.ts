import { rebaseSelectionAfterEdit, type CodePointSpan } from './selection';

export interface NamedSelection extends CodePointSpan {
  id: string;
  label: string;
  invalidReason?: string;
}

/** Rebase stable named selections without guessing at repeated text. */
export function rebaseNamedSelections(oldScript: string, newScript: string, selections: NamedSelection[]): NamedSelection[] {
  return selections.map((selection) => {
    const rebased = rebaseSelectionAfterEdit(oldScript, newScript, selection);
    return rebased
      ? { ...selection, ...rebased, invalidReason: undefined }
      : { ...selection, invalidReason: 'Script edit changed this selection' };
  });
}
