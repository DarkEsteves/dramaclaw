export interface DialogueTurn {
  speaker: string;
  text: string;
}

export interface ParsedDialogue {
  turns: DialogueTurn[];
  speakers: string[];
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
