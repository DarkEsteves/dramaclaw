"""Server-owned speech previews and authenticated correction receipts.

There is deliberately no model assertion for having listened to audio. Until an
actual audio-review adapter exists, lexical changes require a user receipt.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import re
import uuid
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field
from .speech_correction import _spoken, preview_recognized_words_to_plan_cues
from .speech_cues import export_program_srt


class SpeechCorrectionPreviewV1(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source_plan_ref: dict[str, Any]
    recognition_ref: dict[str, Any]
    take_id: str
    actor_id: str = Field(min_length=1)
    actor_kind: Literal['agent', 'user', 'system']
    corrections: list[dict[str, Any]]
    differences: list[dict[str, Any]]
    corrected_words: list[dict[str, Any]]
    cues: list[dict[str, Any]]
    blockers: list[str]
    lexical_change: bool


class SpeechCorrectionReviewV1(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    preview_ref: dict[str, Any]
    actor_id: str = Field(min_length=1)
    actor_kind: Literal['user']
    accepted: bool


def _ref(artifact):
    return {'artifact_id': artifact['id'], 'revision': artifact['revision']}


def _read(store, ref, schema):
    if not isinstance(ref, dict) or set(ref) != {'artifact_id', 'revision'}:
        raise ValueError('exact artifact revision required')
    store._validate_id(ref['artifact_id'])
    store._validate_revision(ref['revision'])
    artifact = store._read(ref['artifact_id'], ref['revision'])
    if artifact['schema'] != schema:
        raise ValueError(f'{schema} required')
    return artifact


def _source(store, plan_ref, take_id, recognition_ref):
    from .store import _speech_source_signature
    plan = _read(store, plan_ref, 'production_plan.v2')['payload']
    take = next((row for row in plan.get('speech_takes', []) if row['id'] == take_id), None)
    if take is None:
        raise ValueError('speech take not found')
    resource = next(row for row in plan['resources'] if row['id'] == take['resource_id'])
    rec = _read(store, recognition_ref, 'word_timeline.v1')
    raw = rec['payload']
    original_plan = _read(store, raw['plan_ref'], 'production_plan.v2')['payload']
    if (rec['producer_version'] != 'remix-align-production-speech.v1'
            or raw.get('status') != 'script_mismatch'
            or raw['media_hash'] != 'sha256:' + resource['media_hash']
            or raw.get('placement_id') != take['origin_placement_id']
            or raw.get('turn_id') != take['turn_ids'][0]
            or (raw.get('group_turn_ids') or [raw.get('turn_id')]) != take['turn_ids']
            or raw.get('source_in_us') != 0 or raw.get('source_out_us') != resource['duration_us']
            or not any(ref['artifact_id'] == raw['plan_ref']['artifact_id'] and ref['revision'] == raw['plan_ref']['revision'] for ref in rec['upstream_refs'])
            or _speech_source_signature(original_plan, take_id) != _speech_source_signature(plan, take_id)):
        raise ValueError('recognition differs from exact take source or trusted full-source alignment')
    turns = {row['id']: row for beat in plan['beats'] for row in beat.get('turns', [])}
    existing = {}
    for turn_id in take['turn_ids']:
        for accepted in (turns[turn_id].get('speech_timing') or {}).get('words', []):
            key = (accepted['start_us'], accepted['end_us'])
            existing.setdefault(key, []).append(accepted['id'])
    words = []
    for index, word in enumerate(raw.get('recognized_words', [])):
        # Identity derives from immutable recognition revision and token index.
        identity = hashlib.sha256(f"{recognition_ref['artifact_id']}:{recognition_ref['revision']}:{index}".encode()).hexdigest()[:24]
        candidates = existing.get((word.get('start_us'), word.get('end_us')), [])
        word_id = candidates[0] if len(candidates) == 1 else 'speech-word-' + identity
        words.append({**word, 'id': word_id})
    return plan, take, resource, [turns[key] for key in take['turn_ids']], words


def get_speech_review(store, plan_ref, take_id, recognition_ref, *, offset=0, limit=200):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('invalid speech review pagination')
    plan, take, resource, turns, words = _source(store, plan_ref, take_id, recognition_ref)
    return {'source_plan_ref': plan_ref, 'recognition_ref': recognition_ref, 'take': take,
            'target_turns': turns, 'source_duration_us': resource['duration_us'],
            'recognized_words': words[offset:offset + limit], 'total_words': len(words),
            'next_offset': offset + limit if offset + limit < len(words) else None,
            'audio_review_available': False}


def _build_preview(store, plan_ref, take_id, recognition_ref, corrections, *, actor_id, actor_kind='agent'):
    plan, take, resource, turns, words = _source(store, plan_ref, take_id, recognition_ref)
    if not actor_id or actor_kind not in {'agent', 'user', 'system'}:
        raise ValueError('server actor required')
    by_id = {word['id']: word for word in words}
    seen = set()
    lexical = False
    differences = []
    for change in corrections:
        if set(change) != {'word_id', 'text', 'reason'} or change['word_id'] not in by_id or change['word_id'] in seen:
            raise ValueError('correction requires one known stable word_id, text and reason')
        if not isinstance(change['text'], str) or not change['text'].strip() or not isinstance(change['reason'], str) or not change['reason'].strip():
            raise ValueError('correction text and reason required')
        seen.add(change['word_id'])
        word = by_id[change['word_id']]
        changed_spelling = _spoken(word['text']) != _spoken(change['text'])
        lexical |= changed_spelling
        differences.append({'word_id': word['id'], 'before': word['text'], 'after': change['text'],
                            'reason': change['reason'], 'change_kind': 'lexical' if changed_spelling else 'punctuation'})
        word['text'] = change['text']
    blockers = ['audio_review_unavailable'] if lexical else []
    cues = []
    if not words or any(type(word.get('start_us')) is not int or type(word.get('end_us')) is not int for word in words):
        blockers.append('word_timing_required')
    else:
        try:
            cues = preview_recognized_words_to_plan_cues(words, turns, resource['duration_us'], take['source_in_us'], take.get('source_out_us'))
        except ValueError as exc:
            blockers.append('target_or_timing_mismatch: ' + str(exc))
    payload = {'source_plan_ref': plan_ref, 'recognition_ref': recognition_ref, 'take_id': take_id,
               'actor_id': actor_id, 'actor_kind': actor_kind, 'corrections': corrections, 'differences': differences,
               'corrected_words': words, 'cues': cues, 'blockers': blockers, 'lexical_change': lexical}
    return payload


def preview_speech_correction(store, plan_ref, take_id, recognition_ref, corrections, *, actor_id, actor_kind='agent'):
    payload = _build_preview(store, plan_ref, take_id, recognition_ref, corrections, actor_id=actor_id, actor_kind=actor_kind)
    return store.create('speech_correction_preview.v1', payload, [plan_ref, recognition_ref])


def create_speech_correction_review(store, preview_ref, *, actor_id, actor_kind, accepted):
    if actor_kind != 'user' or not actor_id:
        raise ValueError('authenticated user receipt required')
    _read(store, preview_ref, 'speech_correction_preview.v1')
    return store.create('speech_correction_review.v1', {'preview_ref': preview_ref, 'actor_id': actor_id,
                        'actor_kind': 'user', 'accepted': accepted}, [preview_ref])


def apply_speech_correction(store, preview_ref, review_ref, command_id):
    from .contracts import validate_payload
    from .store import RemixArtifactConflict
    if not isinstance(command_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', command_id):
        raise ValueError('invalid speech correction command_id')
    preview_artifact = _read(store, preview_ref, 'speech_correction_preview.v1')
    preview = preview_artifact['payload']
    plan_ref = preview['source_plan_ref']
    receipt = None
    if review_ref is not None:
        receipt = _read(store, review_ref, 'speech_correction_review.v1')
        if receipt['payload']['preview_ref'] != preview_ref or not receipt['payload']['accepted'] or not any(ref['artifact_id'] == preview_ref['artifact_id'] and ref['revision'] == preview_ref['revision'] for ref in receipt['upstream_refs']):
            raise ValueError('accepted exact-preview user evidence required')
    blockers = [item for item in preview['blockers'] if item != 'audio_review_unavailable' or receipt is None]
    if blockers:
        raise ValueError('speech correction requires evidence: ' + ', '.join(blockers))
    plan, take, resource, turns, words = _source(store, plan_ref, preview['take_id'], preview['recognition_ref'])
    # Recompute the exact preview from immutable source; persisted payload alone
    # cannot authorize changing words or fabricating time.
    changes = {row['word_id']: row for row in preview['corrections']}
    expected_words = [{**word, **({'text': changes[word['id']]['text']} if word['id'] in changes else {})} for word in words]
    if expected_words != preview['corrected_words']:
        raise ValueError('preview differs from immutable recognition evidence')
    projected = preview_recognized_words_to_plan_cues(expected_words, turns, resource['duration_us'], take['source_in_us'], take.get('source_out_us'))
    if projected != preview['cues']:
        raise ValueError('preview cues differ from source evidence')
    with store._locked() as guard:
        current = store._read(plan_ref['artifact_id'], store._latest_revision(plan_ref['artifact_id']))
        request = {'preview_ref': preview_ref, 'review_ref': review_ref}
        receipt_dir = store.root / '_speech_correction_commands'
        if receipt_dir.is_symlink():
            raise ValueError('invalid speech command storage')
        receipt_dir.mkdir(exist_ok=True)
        receipt_path = receipt_dir / (hashlib.sha256(command_id.encode()).hexdigest() + '.json')
        command_receipt = store._read_json(receipt_path) if receipt_path.exists() else None
        if command_receipt:
            if command_receipt['request'] != request:
                raise ValueError('speech command_id already used for another request')
            try:
                saved_result = store._read(plan_ref['artifact_id'], command_receipt['revision'])
            except FileNotFoundError:
                saved_result = None
            if saved_result is not None:
                if saved_result['content_hash'] != command_receipt['content_hash']:
                    raise ValueError('speech command result was replaced')
                return saved_result
        if current['revision'] != plan_ref['revision']:
            raise RemixArtifactConflict('speech correction preview is stale')
        actor = receipt['payload'] if receipt else preview
        evidence = {'take_id': take['id'], 'media_hash': resource['media_hash'], 'basis': 'srt_import',
                    'source_plan_ref': plan_ref, 'recognition_ref': preview['recognition_ref'],
                    'original_srt': export_program_srt(projected),
                    'cues': [{'id': f"cue:{row['turn_id']}", 'turn_id': row['turn_id'], 'text': row['text'],
                              'start_us': row['source_start_us'], 'end_us': row['source_end_us']} for row in projected],
                    'corrected_words': [{key: word[key] for key in ('text', 'start_us', 'end_us')} for word in expected_words],
                    'actor_id': actor['actor_id'], 'actor_kind': actor['actor_kind'],
                    'correction_source': 'user_confirmation' if receipt else 'punctuation',
                    'review_ref': review_ref, 'correction_reasons': [row['reason'] for row in preview['corrections']]}
        refs = store._reference_list([plan_ref, preview['recognition_ref'], preview_ref] + ([review_ref] if review_ref else []), store.project_id)
        evidence = validate_payload('speech_cue_evidence.v1', evidence)
        eid = uuid.uuid4().hex
        store._validate_v2_lineage('speech_cue_evidence.v1', evidence, refs, target_artifact_id=eid)
        saved = store._publish(artifact_id=eid, schema='speech_cue_evidence.v1', revision=1, payload=evidence,
                               upstream_refs=refs, created_by=actor['actor_id'], guard=guard)
        eref = _ref(saved)
        updated = deepcopy(plan)
        all_turns = {row['id']: row for beat in updated['beats'] for row in beat.get('turns', [])}
        updated['speech_cues'] = [cue for cue in updated.get('speech_cues', []) if cue['take_id'] != take['id']]
        for row in projected:
            turn = all_turns[row['turn_id']]
            selected = [{key: word[key] for key in ('id', 'text', 'start_us', 'end_us')} for word in row['words']]
            turn['speech_timing'] = {'placement_id': take['origin_placement_id'], 'media_hash': resource['media_hash'],
                                     'text': turn['text'], 'text_hash': hashlib.sha256(turn['text'].encode()).hexdigest(),
                                     'evidence_ref': eref, 'words': selected}
            updated['speech_cues'].append({'id': f"cue:{turn['id']}", 'take_id': take['id'], 'turn_id': turn['id'],
                    'text': turn['text'], 'source_start_us': row['source_start_us'], 'source_end_us': row['source_end_us'],
                    'word_ids': [word['id'] for word in selected], 'timing_source': 'srt_import', 'timing_status': 'ready', 'evidence_ref': eref})
        updated = validate_payload('production_plan.v2', updated)
        plan_refs = store._reference_list(list(current['upstream_refs']) + [eref, preview_ref] + ([review_ref] if review_ref else []), store.project_id)
        store._validate_upstream_refs(plan_refs)
        store._validate_v2_lineage('production_plan.v2', updated, plan_refs, target_artifact_id=current['id'], previous_plan=current)
        store._atomic_json(receipt_path, {'request': request, 'revision': current['revision'] + 1,
            'content_hash': 'sha256:' + hashlib.sha256(store._canonical(updated)).hexdigest()}, guard)
        return store._publish(artifact_id=current['id'], schema='production_plan.v2', revision=current['revision'] + 1,
                              payload=updated, upstream_refs=plan_refs, created_by=current['created_by'], guard=guard)


def validate_speech_review_lineage(store, schema, payload, refs):
    """Validate preview/receipt source dependencies while the store lock is held."""
    def declared(ref):
        if not any(row['artifact_id'] == ref['artifact_id'] and row['revision'] == ref['revision'] for row in refs):
            raise ValueError('speech review exact source ref must be included in upstream_refs')
    if schema == 'speech_correction_preview.v1':
        declared(payload['source_plan_ref'])
        declared(payload['recognition_ref'])
        expected = _build_preview(store, payload['source_plan_ref'], payload['take_id'], payload['recognition_ref'],
                                  payload['corrections'], actor_id=payload['actor_id'], actor_kind=payload['actor_kind'])
        if payload != expected:
            raise ValueError('speech preview differs from immutable recognition evidence')
    elif schema == 'speech_correction_review.v1':
        declared(payload['preview_ref'])
        _read(store, payload['preview_ref'], 'speech_correction_preview.v1')


def validate_correction_provenance(store, payload, refs):
    """Keep legacy manual evidence valid; new provenance must resolve a preview."""
    source = payload.get('correction_source')
    if source is None:
        return
    previews = [store._read(ref['artifact_id'], ref['revision']) for ref in refs]
    previews = [artifact for artifact in previews if artifact['schema'] == 'speech_correction_preview.v1']
    if len(previews) != 1:
        raise ValueError('correction provenance requires one exact speech preview')
    preview_artifact = previews[0]
    preview = preview_artifact['payload']
    if source == 'audio_review':
        raise ValueError('audio review adapter is unavailable; authenticated user evidence required')
    actor = preview
    if source == 'user_confirmation':
        ref = payload.get('review_ref')
        if ref is None or not any(row['artifact_id'] == ref['artifact_id'] and row['revision'] == ref['revision'] for row in refs):
            raise ValueError('correction requires authenticated exact-preview review evidence')
        receipt = _read(store, ref, 'speech_correction_review.v1')['payload']
        if not receipt['accepted'] or receipt['preview_ref'] != _ref(preview_artifact):
            raise ValueError('correction requires accepted exact-preview review evidence')
        actor = receipt
    elif preview['lexical_change'] or payload.get('review_ref') is not None:
        raise ValueError('lexical correction cannot declare punctuation provenance')
    blockers = [row for row in preview['blockers'] if row != 'audio_review_unavailable' or source != 'user_confirmation']
    if blockers:
        raise ValueError('correction preview has unresolved evidence blockers')
    if (payload.get('actor_id') != actor['actor_id'] or payload.get('actor_kind') != actor['actor_kind']
            or payload['source_plan_ref'] != preview['source_plan_ref']
            or payload['recognition_ref'] != preview['recognition_ref']
            or payload['take_id'] != preview['take_id']
            or payload.get('correction_reasons') != [row['reason'] for row in preview['corrections']]
            or payload['corrected_words'] != [{key: word[key] for key in ('text', 'start_us', 'end_us')} for word in preview['corrected_words']]):
        raise ValueError('correction provenance differs from exact preview or actor receipt')
