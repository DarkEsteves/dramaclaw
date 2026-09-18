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
