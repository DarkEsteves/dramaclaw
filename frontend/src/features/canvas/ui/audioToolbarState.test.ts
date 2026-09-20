import { describe, expect, it } from 'vitest';
import { audioToolbarState } from './audioToolbarState';

describe('audio toolbar state', () => {
  it('blocks media actions while audio is empty', () => {
    expect(audioToolbarState(null)).toEqual({ canTranscribe: false, canConfigureMedia: false });
  });

  it('enables transcription and media settings when an audio file exists', () => {
    expect(audioToolbarState('/projects/demo/voice.wav')).toEqual({ canTranscribe: true, canConfigureMedia: true });
  });
});
