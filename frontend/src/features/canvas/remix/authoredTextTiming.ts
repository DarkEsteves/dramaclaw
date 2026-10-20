/** Persist author text positions as Unicode code points, never DOM UTF-16 units. */
export async function hashAuthorText(text: string): Promise<string> {
  if (!globalThis.crypto?.subtle) throw new Error('当前浏览器无法校验台词文字，请使用安全连接');
  const digest = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

export function codePointOffset(text: string, utf16Position: number): number {
  if (!Number.isInteger(utf16Position) || utf16Position < 0 || utf16Position > text.length)
    throw new RangeError('DOM text offset is outside the text');
  let utf16 = 0;
  let points = 0;
  for (const character of text) {
    if (utf16Position === utf16) return points;
    utf16 += character.length;
    points += 1;
    if (utf16Position < utf16) throw new RangeError('DOM selection splits a surrogate pair');
  }
  return points;
}

export function utf16Offset(text: string, codePointPosition: number): number {
  if (!Number.isInteger(codePointPosition) || codePointPosition < 0)
    throw new RangeError('text code point offset is outside the text');
  let position = 0;
  let points = 0;
  for (const character of text) {
    if (points === codePointPosition) return position;
    position += character.length;
    points += 1;
  }
  if (points === codePointPosition) return position;
  throw new RangeError('text code point offset is outside the text');
}

interface SourceWord { id: string; text: string; start_char?: number | null; end_char?: number | null; start_us: number; end_us: number }
interface BoundaryInput { text: string; charOffset: number; affinity: 'before' | 'after'; words: SourceWord[]; cueStartUs?: number | null; cueEndUs?: number | null }
interface BoundaryResult { sourceUs: number | null; quality: 'measured' | 'manual' | 'estimated' | null; sourceWordIds: string[]; issueCode: string | null }

function roundEven(value: number): number {
  const floor = Math.floor(value);
  const remainder = value - floor;
  return remainder < 0.5 ? floor : remainder > 0.5 ? floor + 1 : floor % 2 === 0 ? floor : floor + 1;
}

export function locateTextBoundary({ text, charOffset, affinity, words, cueStartUs, cueEndUs }: BoundaryInput): BoundaryResult {
  const length = Array.from(text).length;
  if (!Number.isInteger(charOffset) || charOffset < 0 || charOffset > length) throw new RangeError('char_offset is outside the turn text');
  const result = (sourceUs: number | null, quality: BoundaryResult['quality'], sourceWordIds: string[], issueCode: string | null = null): BoundaryResult =>
    ({ sourceUs, quality, sourceWordIds, issueCode });
  if (charOffset === 0 && cueStartUs != null) return result(cueStartUs, 'manual', []);
  if (charOffset === length && cueEndUs != null) return result(cueEndUs, 'manual', []);
  const valid = words.filter((word) => Number.isInteger(word.start_char) && Number.isInteger(word.end_char)
    && word.start_char! >= 0 && word.start_char! < word.end_char! && word.end_char! <= length
    && Number.isInteger(word.start_us) && Number.isInteger(word.end_us) && word.start_us >= 0 && word.end_us > word.start_us);
  const exact = valid.filter((word) => affinity === 'before' ? word.end_char === charOffset : word.start_char === charOffset);
  if (exact.length === 1) return result(affinity === 'before' ? exact[0].end_us : exact[0].start_us, 'measured', [exact[0].id]);
  if (exact.length > 1) return result(null, null, [], 'ambiguous_word_evidence');
  const interior = valid.filter((word) => word.start_char! < charOffset && charOffset < word.end_char!);
  if (interior.length === 1) {
    const word = interior[0];
    return result(word.start_us + roundEven((word.end_us - word.start_us) * (charOffset - word.start_char!) / (word.end_char! - word.start_char!)),
      'estimated', [word.id]);
  }
  return result(null, null, [], interior.length ? 'ambiguous_word_evidence' : 'missing_word_evidence');
}
