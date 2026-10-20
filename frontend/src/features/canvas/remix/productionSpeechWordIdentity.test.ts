import { describe, expect, it } from 'vitest';
import { identifyRecognizedWords } from './productionSpeechWordIdentity';

describe('source word identity', () => {
  it('keeps a uniquely positioned word when recognition moves later in the source', () => {
    const previous = [{ id: 'keyword', text: '空杯', start_char: 5, end_char: 7, start_us: 1_000_000, end_us: 1_300_000 }];
    expect(identifyRecognizedWords('turn', 'new-evidence', 1,
      [{ text: '空杯', start_char: 5, end_char: 7, start_us: 1_700_000, end_us: 2_000_000 }], previous))
      .toEqual([{ id: 'keyword', text: '空杯', start_char: 5, end_char: 7, start_us: 1_700_000, end_us: 2_000_000 }]);
  });

  it('does not attach an old word ID to a different repeated word', () => {
    const previous = [
      { id: 'first', text: '杯', start_us: 100_000, end_us: 200_000 },
      { id: 'second', text: '杯', start_us: 300_000, end_us: 400_000 },
    ];
    const identified = identifyRecognizedWords('turn', 'new-evidence', 2,
      [{ text: '杯', start_us: 500_000, end_us: 600_000 }, { text: '杯', start_us: 700_000, end_us: 800_000 }], previous);
    expect(identified.map((word) => word.id)).toEqual(['turn:spoken:new-evidence:2:0', 'turn:spoken:new-evidence:2:1']);
  });
});
