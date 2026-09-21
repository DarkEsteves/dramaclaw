/** Script offsets are Unicode code points; DOM selection offsets are UTF-16 units. */
export interface CodePointSpan {
  start: number;
  end: number;
}

function boundaries(text: string): number[] {
  const offsets = [0];
  let position = 0;
  for (const codePoint of text) {
    position += codePoint.length;
    offsets.push(position);
  }
  return offsets;
}

function validSpan(start: number, end: number, limit: number): void {
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end < start || end > limit) {
    throw new RangeError('selection range is outside the text');
  }
}

export function codePointSpanFromUtf16Range(text: string, start: number, end: number): CodePointSpan {
  validSpan(start, end, text.length);
  const offsets = boundaries(text);
  const cpStart = offsets.indexOf(start);
  const cpEnd = offsets.indexOf(end);
  if (cpStart < 0 || cpEnd < 0) {
    throw new RangeError('selection splits a surrogate pair');
  }
  return { start: cpStart, end: cpEnd };
}

export function utf16RangeFromCodePointSpan(text: string, start: number, end: number): CodePointSpan {
  const offsets = boundaries(text);
  validSpan(start, end, offsets.length - 1);
  return { start: offsets[start], end: offsets[end] };
}

/** Preserve only selections whose content was untouched by one contiguous edit. */
export function rebaseSelectionAfterEdit(
  oldText: string,
  newText: string,
  selection: CodePointSpan,
): CodePointSpan | null {
  const before = Array.from(oldText);
  const after = Array.from(newText);
  validSpan(selection.start, selection.end, before.length);
  if (oldText === newText) return selection;
  const selected = before.slice(selection.start, selection.end).join('');
  let occurrences = 0;
  let cursor = 0;
  while (selected && cursor <= newText.length) {
    const found = newText.indexOf(selected, cursor);
    if (found < 0) break;
    occurrences += 1;
    cursor = found + 1;
  }
  // Repeated text cannot be identity-mapped safely after an edit. Keeping the
  // old id would silently bind a semantic event to an arbitrary occurrence.
  if (occurrences !== 1) return null;
  let prefix = 0;
  while (prefix < before.length && prefix < after.length && before[prefix] === after[prefix]) {
    prefix += 1;
  }
  let suffix = 0;
  while (
    suffix < before.length - prefix && suffix < after.length - prefix &&
    before[before.length - 1 - suffix] === after[after.length - 1 - suffix]
  ) {
    suffix += 1;
  }
  const oldEditEnd = before.length - suffix;
  if (selection.end <= prefix) return selection;
  if (selection.start >= oldEditEnd) {
    const shift = after.length - before.length;
    return { start: selection.start + shift, end: selection.end + shift };
  }
  return null;
}
