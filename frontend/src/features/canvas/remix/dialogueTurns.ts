export interface DialogueTurn {
  speaker: string;
  text: string;
}

export interface ParsedDialogue {
  turns: DialogueTurn[];
  speakers: string[];
}

export interface AudioDialogueInput {
  dialogueVoiceMode?: 'single' | 'dialogue';
  remixScriptRef?: unknown;
  remixDialogueTurns?: DialogueTurn[];
  text?: string;
}

export interface ResolvedAudioDialogue extends ParsedDialogue {
  enabled: boolean;
  error: string | null;
}

const SPEAKER_LINE = /^([^：:\n]{1,24})[：:]\s*(.*)$/u;

/** Parse a labelled script without ever treating speaker names as spoken text. */
export function parseDialogueTurns(dialogue: string): ParsedDialogue | null {
  const lines = dialogue.split(/\r?\n/u).map((line) => line.trim()).filter(Boolean);
  const labelled = lines.map((line) => line.match(SPEAKER_LINE));
  if (!labelled.some(Boolean)) return null;
  if (labelled.some((match) => !match || !match[1].trim() || !match[2].trim())) {
    throw new Error('每句台词都需要写成“角色：台词”，且台词不能为空。');
  }
  const turns = (labelled as RegExpMatchArray[]).map((match) => ({
    speaker: match[1].trim(), text: match[2].trim(),
  }));
  return { turns, speakers: [...new Set(turns.map((turn) => turn.speaker))] };
}

/** One voice mode is the historical default; labelled scripts default to role voices. */
export function resolveAudioDialogue(data: AudioDialogueInput): ResolvedAudioDialogue {
  const enabled = data.dialogueVoiceMode === 'dialogue'
    || (data.dialogueVoiceMode !== 'single' && Boolean(data.remixDialogueTurns?.length));
  if (!enabled) return { enabled: false, turns: [], speakers: [], error: null };
  if (data.remixScriptRef && data.remixDialogueTurns?.length) {
    const turns = data.remixDialogueTurns;
    return { enabled: true, turns, speakers: [...new Set(turns.map((turn) => turn.speaker))], error: null };
  }
  try {
    const parsed = parseDialogueTurns(data.text ?? '');
    if (!parsed) throw new Error('每句台词都需要写成“角色：台词”。');
    if (parsed.turns.length > 40) throw new Error('多角色配音最多支持 40 句台词。');
    return { enabled: true, ...parsed, error: null };
  } catch (error) {
    return {
      enabled: true, turns: [], speakers: [],
      error: error instanceof Error ? error.message : '请检查角色台词格式。',
    };
  }
}

/** A linked script may be rendered with one voice, but role names stay silent. */
export function singleVoiceAudioText(data: AudioDialogueInput, originalText: string): string {
  if (data.dialogueVoiceMode === 'single' && data.remixScriptRef && data.remixDialogueTurns?.length) {
    return data.remixDialogueTurns.map((turn) => turn.text).join('\n');
  }
  return originalText;
}

/** Guard older remix nodes that still hold labelled dialogue in a single-voice text field. */
export function isLegacyRoleLabelledRemixAudio(data: {
  remixScriptRef?: unknown;
  text?: string;
  remixDialogueTurns?: DialogueTurn[];
}): boolean {
  if (!data.remixScriptRef || data.remixDialogueTurns?.length || !data.text?.trim()) return false;
  try {
    return parseDialogueTurns(data.text) !== null;
  } catch {
    return true;
  }
}
