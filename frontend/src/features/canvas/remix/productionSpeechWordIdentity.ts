import type { PlanSourceWordV2 } from './productionPlanV2Model';

type RecognizedWord = Omit<PlanSourceWordV2, 'id'>;

/** Keep a word identity only when its text position can be matched uniquely. */
export function identifyRecognizedWords(
  turnId: string, evidenceId: string, evidenceRevision: number,
  recognized: RecognizedWord[], previous: PlanSourceWordV2[],
): PlanSourceWordV2[] {
  const charKey = (word: RecognizedWord) => Number.isInteger(word.start_char) && Number.isInteger(word.end_char)
    ? `${word.start_char}:${word.end_char}:${word.text}` : null;
  const oldByChar = new Map<string, PlanSourceWordV2[]>();
  const newByChar = new Map<string, RecognizedWord[]>();
  for (const word of previous) {
    const key = charKey(word);
    if (key) oldByChar.set(key, [...oldByChar.get(key) ?? [], word]);
  }
  for (const word of recognized) {
    const key = charKey(word);
    if (key) newByChar.set(key, [...newByChar.get(key) ?? [], word]);
  }
  const oldByText = new Map<string, PlanSourceWordV2[]>();
  const newByText = new Map<string, RecognizedWord[]>();
  for (const word of previous) oldByText.set(word.text, [...oldByText.get(word.text) ?? [], word]);
  for (const word of recognized) newByText.set(word.text, [...newByText.get(word.text) ?? [], word]);
  const used = new Set<string>();
  return recognized.map((word, index) => {
    const key = charKey(word);
    const exact = key && oldByChar.get(key)?.length === 1 && newByChar.get(key)?.length === 1 ? oldByChar.get(key)?.[0] : null;
    const byText = oldByText.get(word.text)?.length === 1 && newByText.get(word.text)?.length === 1
      ? oldByText.get(word.text)?.[0] : null;
    const previousWord = exact ?? byText;
    const id = previousWord && !used.has(previousWord.id) ? previousWord.id
      : `${turnId}:spoken:${evidenceId}:${evidenceRevision}:${index}`;
    used.add(id);
    return { ...word, id };
  });
}
