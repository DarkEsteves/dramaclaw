export function audioToolbarState(audioUrl: string | null | undefined): {
  canTranscribe: boolean;
  canConfigureMedia: boolean;
} {
  const hasAudio = Boolean(audioUrl?.trim());
  return { canTranscribe: hasAudio, canConfigureMedia: hasAudio };
}
