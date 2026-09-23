import { describe, expect, it } from 'vitest';
import { transcriptToolbarState } from './transcriptToolbarState';

const transcript = (data: Record<string, unknown>) => ({
  id: 'transcript',
  type: 'transcriptNode',
  position: { x: 0, y: 0 },
  data,
});

describe('transcript toolbar state', () => {
  it('puts transcription actions in the toolbar for a connected audio node', () => {
    const nodes = [
      { id: 'audio', type: 'audioNode', position: { x: 0, y: 0 }, data: { remixMedia: { media_path: 'voice.wav', media_kind: 'audio' } } },
      transcript({ words: [], status: 'needs_review' }),
    ];
    const state = transcriptToolbarState(nodes as never, [{ source: 'audio', target: 'transcript' }] as never, 'transcript');

    expect(state).toMatchObject({
      canTranscribe: true,
      canAlign: false,
      canExport: false,
      busy: false,
      transcribeLabel: '开始转写',
    });
  });

  it('exposes alignment, correction, and export after a saved result', () => {
    const nodes = [
      { id: 'audio', type: 'audioNode', position: { x: 0, y: 0 }, data: { remixMedia: { media_path: 'voice.wav', media_kind: 'audio' } } },
      { id: 'script', type: 'scriptNode', position: { x: 0, y: 0 }, data: { remixScriptRef: { artifactId: 'script-1', revision: 2 } } },
      transcript({ words: [{ text: '你', startUs: 0, endUs: 1 }], status: 'completed', artifactRef: { artifactId: 'words-1', revision: 1 } }),
    ];
    const state = transcriptToolbarState(nodes as never, [
      { source: 'audio', target: 'transcript' },
      { source: 'script', target: 'transcript' },
    ] as never, 'transcript');

    expect(state).toMatchObject({
      canTranscribe: true,
      canAlign: true,
      canExport: true,
      transcribeLabel: '重新转写',
    });
  });

  it('disables processing actions while a task is running', () => {
    const nodes = [transcript({ words: [], status: 'needs_review', operationId: 'operation-1' })];
    expect(transcriptToolbarState(nodes as never, [], 'transcript').busy).toBe(true);
  });
});
