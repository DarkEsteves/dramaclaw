export function audioToolbarState(audioUrl: string | null | undefined): {
  canTranscribe: boolean;
  mediaLabel: '上传音频' | '音频用途';
} {
  const hasAudio = Boolean(audioUrl?.trim());
  return { canTranscribe: hasAudio, mediaLabel: hasAudio ? '音频用途' : '上传音频' };
}
