import { describe, expect, it } from 'vitest';
import { audioToolbarState } from './audioToolbarState';

describe('audio toolbar state', () => {
  it('offers upload while audio is empty and blocks media actions', () => {
    expect(audioToolbarState(null)).toEqual({ canTranscribe: false, mediaLabel: '上传音频' });
  });

  it('enables transcription and media settings when an audio file exists', () => {
    expect(audioToolbarState('/projects/demo/voice.wav')).toEqual({ canTranscribe: true, mediaLabel: '音频用途' });
  });
});
