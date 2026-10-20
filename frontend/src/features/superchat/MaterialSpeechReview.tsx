import { useEffect, useState } from 'react';

type Ref = { artifact_id: string; revision: number };
type Word = { id: string; text: string; start_us: number; end_us: number };
export interface MaterialSpeechReviewSummary {
  observation_ref: Ref;
  target_signature: string;
  target_text: string;
  recognized_text: string;
  recognized_words: Word[];
  corrected_words?: Word[];
  target_turns?: Array<{ id: string; speaker?: string; text: string }>;
  differences?: Array<{ kind: string; target_text: string; observed_text: string; start_us: number | null; end_us: number | null }>;
  recognized_segments: Array<{ text: string; start_us: number; end_us: number }>;
  source_duration_us?: number | null;
  timing_granularity: 'word' | 'segment' | 'none';
  transcript_match: string;
  audio_review: string;
  lip_review: string;
}
export interface MaterialSpeechCorrectionInput {
  observation_ref: Ref;
  target_signature: string;
  confirmed: boolean;
  corrections: Array<{ word_id: string; text: string; reason: string }>;
}

const seconds = (us: number) => (us / 1_000_000).toFixed(3);
const reviewLabel = (status: string, pending: string) =>
  status === 'passed' ? '已审看' : status === 'rejected' ? '需返工' : pending;

export function MaterialSpeechReview({ summary, onSave, onSeek, busy = false, error }: {
  summary: MaterialSpeechReviewSummary;
  onSave: (input: MaterialSpeechCorrectionInput) => void | Promise<void>;
  busy?: boolean;
  error?: string | null;
  onSeek?: (seconds: number) => void;
}) {
  const correctedById = new Map(summary.corrected_words?.map((word) => [word.id, word.text]));
  const savedWords = summary.recognized_words.map((word) => correctedById.get(word.id) ?? word.text);
  const [words, setWords] = useState(savedWords);
  const [reviewed, setReviewed] = useState(false);
  const basis = JSON.stringify([summary.observation_ref, summary.target_signature, summary.recognized_words, summary.corrected_words]);
  useEffect(() => {
    setWords(savedWords);
    setReviewed(false);
  }, [basis]); // Scope is the exact observation and intended dialogue.
  const corrections = summary.recognized_words.flatMap((word, index) =>
    words[index] !== word.text
      ? [{ word_id: word.id, text: (words[index] ?? '').trim(), reason: '试听后校正识别文字' }]
      : []);
  const changed = words.some((text, index) => text !== savedWords[index]);
  const valid = changed && corrections.length > 0 && corrections.every((row) => row.text);
  const matches = ['exact', 'normalized_match'].includes(summary.transcript_match);
  return <section aria-label="素材对白核对" className="space-y-2 border-t border-[var(--ui-border-soft)] pt-2">
    <p className="text-xs text-text-muted">完整素材 {summary.source_duration_us == null ? '时长未知' : `${seconds(summary.source_duration_us)} 秒`} · 时间为原素材时间</p>
    <div><span className="text-text-muted">方案台词：</span>{summary.target_turns?.length
      ? summary.target_turns.map((turn) => <p key={turn.id}>{turn.speaker ? `${turn.speaker}：` : ''}{turn.text}</p>)
      : <p>{summary.target_text}</p>}</div>
    <div><span className="text-text-muted">原始识别：</span><p>{summary.recognized_text || '没有识别到对白'}</p></div>
    <p className={matches ? 'text-success' : 'text-warning'}>
      {matches ? '识别文字与方案一致' : '识别文字有差异，请试听对应素材核对'}
    </p>
    <p className="text-xs text-text-muted">
      声音：{reviewLabel(summary.audio_review, '待试听')} · 口型：{reviewLabel(summary.lip_review, '待审看')}
    </p>
    {summary.differences?.map((difference, index) => <div key={index} className="text-xs">
      <p>{difference.kind === 'missing' ? '可能漏说' : difference.kind === 'extra' ? '可能多说' : '文字差异'}：
        {difference.observed_text || '未识别到'} → {difference.target_text || '方案无此内容'}</p>
      {onSeek && difference.start_us != null ? <button type="button" className="tap-button"
        aria-label={`试听差异 ${seconds(difference.start_us)} 秒`}
        onClick={() => onSeek(difference.start_us! / 1_000_000)}>试听此处 · {seconds(difference.start_us)} 秒</button> : null}
    </div>)}
    {summary.timing_granularity === 'segment' ? <p className="text-warning text-xs">
      当前识别只提供句级时间，暂不能按单个词编排效果。
    </p> : summary.timing_granularity === 'word' ? <div className="max-h-40 space-y-1 overflow-y-auto">
      {summary.recognized_words.map((word, index) => <div key={word.id} className="flex items-center gap-2">
        <input aria-label={`识别词 ${index + 1}`} className="min-w-0 flex-1 rounded-md bg-[var(--ui-surface-field)] px-2 py-1"
          value={words[index] ?? word.text} disabled={busy}
          onChange={(event) => {
            const text = event.target.value;
            setWords((current) => current.map((value, i) => i === index ? text : value));
            setReviewed(false);
          }} />
        <span className="whitespace-nowrap text-xs text-text-muted">{seconds(word.start_us)} – {seconds(word.end_us)} 秒</span>
      </div>)}
    </div> : null}
    {valid ? <label className="flex items-center gap-2 text-xs">
      <input type="checkbox" checked={reviewed} disabled={busy}
        onChange={(event) => setReviewed(event.target.checked)} />
      已试听并核对本次修改
    </label> : null}
    {summary.timing_granularity === 'word' ? <button type="button" className="tap-button"
      disabled={busy || !valid || !reviewed}
      onClick={() => void onSave({ observation_ref: summary.observation_ref, target_signature: summary.target_signature,
        corrections, confirmed: reviewed })}>
      {busy ? '保存中…' : '核对并保存'}
    </button> : null}
    {error ? <p role="alert" className="text-warning text-xs">{error}</p> : null}
  </section>;
}
