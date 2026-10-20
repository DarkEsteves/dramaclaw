export interface SourceSrtRow { text: string; source_start_us: number; source_end_us: number }
export interface RecognizedTimedWord { text: string; start_us: number; end_us: number; start_char?: number; end_char?: number }
export interface PlannedDialogue { id: string; text: string }
export function recognizedWordsToPlanCues(words: RecognizedTimedWord[], turns: PlannedDialogue[], sourceDurationUs: number, sourceInUs = 0, sourceOutUs = sourceDurationUs): { srt: string; rows: Array<SourceSrtRow & { turn_id: string }> } {
 if (!words.length || !turns.length) throw new Error('没有可核对的识别词或作品方案台词');
 if (!Number.isSafeInteger(sourceDurationUs) || sourceDurationUs <= 0 || sourceInUs < 0 || sourceOutUs > sourceDurationUs || sourceOutUs <= sourceInUs) throw new Error('发声素材播放范围无效');
 const spoken = (value: string) => value.normalize('NFKC').replace(/[^\p{L}\p{N}]/gu, '').toLocaleLowerCase();
 for (let index = 0; index < words.length; index += 1) {
  const word = words[index]; const previous = words[index - 1];
  if (!word.text.trim() || !Number.isSafeInteger(word.start_us) || !Number.isSafeInteger(word.end_us)
    || word.start_us < sourceInUs || word.end_us > sourceOutUs || word.end_us <= word.start_us) throw new Error(`第 ${index + 1} 个识别词时间超出发声素材播放范围或无效`);
  if (previous && (word.start_us < previous.start_us || word.end_us < previous.end_us)) throw new Error(`第 ${index + 1} 个识别词时间顺序无效`);
 }
 const rawRows: Array<SourceSrtRow & { turn_id: string }> = [];
 let position = 0;
 for (const [turnIndex, turn] of turns.entries()) {
  const expected = spoken(turn.text);
  if (!expected) throw new Error(`作品方案第 ${turnIndex + 1} 句没有可核对的台词文字`);
  const first = position; let matched = '';
  while (position < words.length && matched.length < expected.length) {
   const next = spoken(words[position].text);
   if (next && !expected.startsWith(matched + next)) {
    const remaining = expected.slice(matched.length);
    throw new Error(next.startsWith(remaining) && remaining.length ? `第 ${position + 1} 个识别词跨越第 ${turnIndex + 1} 句台词边界，请拆分该词后核对` : `第 ${turnIndex + 1} 句与第 ${position + 1} 个识别词不一致，请按作品方案修正逐词文字`);
   }
   matched += next; position += 1;
  }
  if (matched !== expected) throw new Error(`第 ${turnIndex + 1} 句还缺少台词文字，请按作品方案补全识别词`);
  while (position < words.length && !spoken(words[position].text)) position += 1;
  const start = Math.floor(words[first].start_us / 1000) * 1000;
  const end = Math.ceil(words[position - 1].end_us / 1000) * 1000;
  if (start < sourceInUs || end > sourceOutUs) throw new Error(`第 ${turnIndex + 1} 句的逐词时间无法放入当前素材播放范围的 SRT 毫秒刻度`);
  rawRows.push({ turn_id: turn.id, text: turn.text, source_start_us: start, source_end_us: end });
 }
 if (position !== words.length) throw new Error(`第 ${position + 1} 个识别词没有对应的作品方案台词，请核对多余文字`);
 const srt = exportProgramSrt(rawRows.map((row) => ({ text: row.text, start_us: row.source_start_us, end_us: row.source_end_us })));
 return { srt, rows: parseSourceSrt(srt).map((row, index) => ({ ...row, turn_id: rawRows[index].turn_id })) };
}
export function recognizedWordsToSourceSrt(words: RecognizedTimedWord[], sourceDurationUs: number, transcript?: string): string {
 if (!words.length) throw new Error('本次识别没有真实词时间，请重新校准');
 if (!Number.isSafeInteger(sourceDurationUs) || sourceDurationUs <= 0) throw new Error('发声素材时长无效');
 const rows: Array<{ text: string; start_us: number; end_us: number }> = [];
 let current: typeof words = [];
 const flush = () => {
  if (!current.length) return;
  const first = current[0]; const last = current[current.length - 1];
  const hasSpans = typeof transcript === 'string' && current.every((word) => Number.isInteger(word.start_char) && Number.isInteger(word.end_char)
   && word.start_char! >= 0 && word.end_char! <= transcript.length && word.start_char! < word.end_char!);
  const text = (hasSpans ? current.map((word, index) => `${index ? transcript!.slice(current[index - 1].end_char!, word.start_char!) : ''}${word.text}`).join('') : current.map((word) => word.text).join('')).trim();
  rows.push({ text, start_us: first.start_us, end_us: last.end_us });
  current = [];
 };
 for (const word of words) {
  if (!word || !word.text?.trim() || !Number.isSafeInteger(word.start_us) || !Number.isSafeInteger(word.end_us)
    || word.start_us < 0 || word.end_us <= word.start_us || word.end_us > sourceDurationUs) throw new Error('识别词时间超出发声素材或无效');
  const previous = current[current.length - 1];
  if (previous && (word.start_us < previous.start_us || word.end_us < previous.end_us)) throw new Error('识别词时间顺序无效');
  if (previous && (/[。！？!?；;]\s*$/.test(previous.text) || word.start_us - previous.end_us >= 600_000)) flush();
  current.push(word);
 }
 flush();
 return exportProgramSrt(rows);
}
export function formatSrtTime(us: number): string {
 const ms = Math.round(us / 1000);
 return `${String(Math.floor(ms / 3600000)).padStart(2, '0')}:${String(Math.floor(ms / 60000) % 60).padStart(2, '0')}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')},${String(ms % 1000).padStart(3, '0')}`;
}
export function parseSourceSrt(text: string): SourceSrtRow[] {
 const timestamp = (value: string) => {
  const match = /^(\d{2,}):(\d{2}):(\d{2}),(\d{3})$/.exec(value);
  if (!match || Number(match[2]) > 59 || Number(match[3]) > 59) throw new Error('SRT 时间格式无效');
  return (Number(match[1]) * 3600000 + Number(match[2]) * 60000 + Number(match[3]) * 1000 + Number(match[4])) * 1000;
 };
 const blocks = text.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n').trim().split(/\n\s*\n/);
 if (text.length > 1_000_000) throw new Error('SRT 文件超过大小限制');
 if (!text.trim()) throw new Error('SRT 文件为空');
 return blocks.map((block, index) => {
  const lines = block.split('\n');
  if (lines.length < 3 || !/^\d+$/.test(lines[0].trim())) throw new Error('SRT 每行需要序号、时间和台词');
  lines.shift();
  const match = /^\s*(\S+)\s+-->\s+(\S+)\s*$/.exec(lines.shift() ?? '');
  if (!match) throw new Error(`第 ${index + 1} 行 SRT 缺少有效时间`);
  const start = timestamp(match[1]); const end = timestamp(match[2]); const content = lines.join('\n').trim();
  if (end <= start || !content) throw new Error(`第 ${index + 1} 行 SRT 时间或台词无效`);
  return { text: content, source_start_us: start, source_end_us: end };
 });
}
export function exportProgramSrt(rows: Array<{ text: string; start_us: number; end_us: number }>): string {
 if (rows.some((row) => row.start_us < 0 || row.end_us <= row.start_us)) throw new Error('节目台词时间无效');
 const ordered = rows.map((row, index) => ({ row, index })).sort((a, b) => a.row.start_us - b.row.start_us || a.row.end_us - b.row.end_us || a.index - b.index);
 return ordered.map(({ row }, index) => `${index + 1}\n${formatSrtTime(row.start_us)} --> ${formatSrtTime(row.end_us)}\n${row.text}`).join('\n\n') + '\n';
}

export function sourceSrtDialogue(text: string, speaker: string, turnText: string): string {
 const compact = (value: string) => value.replace(/\s/g, '');
 if (compact(turnText).includes(compact(text))) return text;
 const prefix = /^([^：:\n]+)[：:]\s*/.exec(text);
 if (!prefix || prefix[1].trim() !== speaker.trim()) throw new Error('SRT 说话者或台词与所选台词不匹配');
 const dialogue = text.slice(prefix[0].length).trim();
 if (!dialogue || !compact(turnText).includes(compact(dialogue))) throw new Error('SRT 台词与所选台词不匹配');
 return dialogue;
}
