import type { AudioNodeData, AudioVoiceRef } from '@/features/canvas/domain/canvasNodes';
import type { DialogueTurn } from './dialogueTurns';

function validVoiceRef(ref: AudioVoiceRef | undefined): boolean {
  return Boolean(ref && (ref.scope !== 'user_custom' || ref.voiceId?.trim()));
}

export function missingDialogueSpeakers(
  turns: DialogueTurn[],
  voices: AudioNodeData['remixSpeakerVoices'],
): string[] {
  return [...new Set(turns.map((turn) => turn.speaker))]
    .filter((speaker) => !validVoiceRef(voices?.[speaker]?.ref));
}
