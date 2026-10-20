"""Sentence timing imported from source subtitles, without invented word evidence."""
from __future__ import annotations

import re
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class CueEvidenceRow(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)

    @model_validator(mode='after')
    def interval(self):
        if self.end_us <= self.start_us:
            raise ValueError('cue evidence interval must be non-empty')
        return self


class CorrectedWord(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    text: str = Field(min_length=1)
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)

    @model_validator(mode='after')
    def interval(self):
        if self.end_us <= self.start_us:
            raise ValueError('corrected word interval must be non-empty')
        return self


class SpeechCueEvidenceV1(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    take_id: str = Field(min_length=1)
    media_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    basis: Literal['srt_import', 'manual']
    cues: list[CueEvidenceRow] = Field(min_length=1)
    source_plan_ref: dict[str, Any]
    recognition_ref: dict[str, Any] | None = None
    original_srt: str | None = Field(default=None, max_length=1_000_000)
    actor_id: str | None = None
    actor_kind: Literal['agent', 'user', 'system'] | None = None
    correction_source: Literal['punctuation', 'user_confirmation', 'audio_review'] | None = None
    review_ref: dict[str, Any] | None = None
    correction_reasons: list[str] = Field(default_factory=list)
    corrected_words: list[CorrectedWord] | None = Field(default=None, min_length=1, max_length=20_000)

    @model_validator(mode='after')
    def source(self):
        ref = self.source_plan_ref
        if set(ref) != {'artifact_id', 'revision'} or not isinstance(ref['artifact_id'], str) or not ref['artifact_id'] or type(ref['revision']) is not int or ref['revision'] < 1:
            raise ValueError('source_plan_ref must be an exact revision')
        recognition_ref = self.recognition_ref
        if recognition_ref is not None and (self.basis != 'srt_import'
            or set(recognition_ref) != {'artifact_id', 'revision'}
            or not isinstance(recognition_ref['artifact_id'], str) or not recognition_ref['artifact_id']
            or type(recognition_ref['revision']) is not int or recognition_ref['revision'] < 1):
            raise ValueError('recognition_ref requires an exact SRT import source revision')
        if self.corrected_words is not None:
            if recognition_ref is None:
                raise ValueError('corrected words require recognition_ref')
            if any(word.start_us < previous.start_us or word.end_us < previous.end_us
                   for previous, word in zip(self.corrected_words, self.corrected_words[1:])):
                raise ValueError('corrected word intervals must remain ordered')
        if len({row.id for row in self.cues}) != len(self.cues):
            raise ValueError('cue evidence IDs must be unique')
        if self.basis == 'srt_import':
            if self.original_srt is None:
                raise ValueError('SRT evidence requires original_srt')
            rows = import_source_srt(self.original_srt, self.take_id)
            if len(rows) != len(self.cues):
                raise ValueError('accepted cues differ from original SRT')
            for original, accepted in zip(rows, self.cues, strict=True):
                prefix = split_srt_speaker_prefix(original['text'])
                matching_text = original['text'] == accepted.text or prefix is not None and prefix[1] == accepted.text
                if not matching_text or (original['source_start_us'], original['source_end_us']) != (accepted.start_us, accepted.end_us):
                    raise ValueError('accepted cues differ from original SRT')
        elif self.original_srt is not None:
            raise ValueError('manual evidence cannot declare an SRT import')
        return self


_TIMESTAMP = r'(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})'
_INTERVAL = re.compile(r'^' + _TIMESTAMP + r'\s+-->\s+' + _TIMESTAMP + r'$')


def import_source_srt(text: str, take_id: str, *, duration_us: int | None = None) -> list[dict[str, Any]]:
    if not isinstance(text, str) or not text.strip() or len(text) > 1_000_000:
        raise ValueError('SRT must contain subtitle rows within the size limit')
    text = text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n').strip()
    rows = []
    for block in re.split(r'\n[ \t]*\n', text):
        lines = block.split('\n')
        if len(lines) < 3 or not lines[0].strip().isdigit():
            raise ValueError('SRT rows require an index, source interval and text')
        match = _INTERVAL.fullmatch(lines[1].strip())
        if match is None:
            raise ValueError('invalid SRT timestamp')
        values = [int(value) for value in match.groups()]
        def us(offset):
            h, m, s, ms = values[offset:offset + 4]
            return ((h * 3600 + m * 60 + s) * 1000 + ms) * 1000
        start, end = us(0), us(4)
        subtitle = '\n'.join(lines[2:]).strip()
        if not subtitle or end <= start or duration_us is not None and end > duration_us:
            raise ValueError('SRT cue must fit its source media')
        rows.append({'take_id': take_id, 'text': subtitle, 'source_start_us': start,
                     'source_end_us': end, 'word_ids': [], 'timing_source': 'srt_import', 'timing_status': 'review'})
    return rows


def export_program_srt(cues: list[dict[str, Any]]) -> str:
    def timestamp(value):
        ms = int(value) // 1000
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f'{h:02}:{m:02}:{s:02},{ms:03}'
    return '\n\n'.join(f"{index}\n{timestamp(row.get('start_us', row.get('source_start_us')))} --> {timestamp(row.get('end_us', row.get('source_end_us')))}\n{row['text']}" for index, row in enumerate(cues, 1)) + '\n'


def upgrade_plan_speech(plan: Any) -> dict[str, Any]:
    """Preserve legacy sentence identities and evidence pending server review.

    Embedded timing alone cannot prove trusted ASR provenance or that the
    entire source was recognized. Historical words remain available, but this
    local compatibility helper must never promote them to accepted ready cues.
    """
    from copy import deepcopy
    import hashlib
    payload = deepcopy(plan if isinstance(plan, dict) else plan.model_dump(mode='json'))
    takes = payload.setdefault('speech_takes', [])
    cues = payload.setdefault('speech_cues', [])
    resources = {row['id']: row for row in payload.get('resources', [])}
    placements = {row['id']: row for row in payload.get('placements', [])}
    placement_beats: dict[str, set[str]] = {}
    ambiguous_origins: set[str] = set()
    for beat in payload['beats']:
        seen: set[str] = set()
        previous = None
        for turn in beat.get('turns', []):
            origin = turn.get('speech_placement_id') or (turn.get('speech_timing') or {}).get('placement_id')
            if origin:
                if origin != previous and origin in seen:
                    ambiguous_origins.add(origin)
                seen.add(origin)
                previous = origin
                placement_beats.setdefault(origin, set()).add(beat['id'])
    for beat in payload['beats']:
        for turn in beat.get('turns', []):
            if any(cue['turn_id'] == turn['id'] for cue in cues):
                continue
            timing = turn.get('speech_timing')
            origin = turn.get('speech_placement_id') or (timing or {}).get('placement_id')
            placement = placements.get(origin)
            resource = resources.get((placement or {}).get('resource_id'))
            if placement is None or resource is None or origin in ambiguous_origins or len(placement_beats.get(origin, [])) != 1:
                continue
            take = next((row for row in takes if row['beat_id'] == beat['id'] and row['origin_placement_id'] == origin), None)
            if take is None:
                take = {'id': f"take:{beat['id']}:{origin}", 'beat_id': beat['id'], 'resource_id': resource['id'],
                        'turn_ids': [], 'origin_placement_id': origin, 'source_in_us': placement.get('source_in_us', 0),
                        'source_out_us': placement.get('source_out_us'), 'speed_num': placement.get('speed_num', 1),
                        'speed_den': placement.get('speed_den', 1), 'lead_in_frames': 0}
                takes.append(take)
            if turn['id'] not in take['turn_ids']:
                take['turn_ids'].append(turn['id'])
            words = (timing or {}).get('words', [])
            valid = (bool(words) and timing.get('placement_id') == origin and timing.get('media_hash') == resource['media_hash']
                     and timing.get('text') == turn['text']
                     and timing.get('text_hash') == hashlib.sha256(turn['text'].encode()).hexdigest())
            start = min((row['start_us'] for row in words), default=None) if valid else None
            end = max((row['end_us'] for row in words), default=None) if valid else None
            valid = valid and take['source_in_us'] <= start < end <= (take['source_out_us'] or resource['duration_us'])
            cues.append({'id': f"cue:{turn['id']}", 'turn_id': turn['id'], 'take_id': take['id'], 'text': turn['text'],
                         'source_start_us': start if valid else None, 'source_end_us': end if valid else None,
                         'timing_source': 'asr', 'timing_status': 'review' if valid else 'draft',
                         'word_ids': [row['id'] for row in words] if valid else [],
                         'evidence_ref': timing.get('evidence_ref') if valid else None})
    return payload


def build_speech_cues(plan: Any) -> list[dict[str, Any]]:
    return upgrade_plan_speech(plan)['speech_cues']


def split_srt_speaker_prefix(text: str) -> tuple[str, str] | None:
    """Read an explicit speaker label while retaining the original import text."""
    match = re.match(r'^([^:\uFF1A\n]+)[:\uFF1A]\s*([\s\S]+)$', text)
    return (match.group(1).strip(), match.group(2).strip()) if match else None
