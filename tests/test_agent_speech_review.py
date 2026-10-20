from copy import deepcopy
import pytest
from test_production_plan_v2 import _plan
from novelvideo.freezone.remix.store import RemixArtifactStore, RemixArtifactConflict


def setup(tmp_path, text='为什么？', raw='为什魔', timed=True):
    store = RemixArtifactStore(tmp_path, 'demo', created_by='user-1')
    plan = _plan()
    plan['beats'][0]['turns'][0]['text'] = text
    plan['speech_takes'] = [{'id': 'take', 'beat_id': 'hook', 'resource_id': 'music', 'turn_ids': ['question'], 'origin_placement_id': 'voice', 'source_in_us': 0, 'source_out_us': None, 'speed_num': 1, 'speed_den': 1, 'lead_in_frames': 0}]
    source = store.create('production_plan.v2', plan, [])
    ref = {'artifact_id': source['id'], 'revision': 1}
    trusted = RemixArtifactStore(tmp_path, 'demo', producer_version='remix-align-production-speech.v1')
    rec = trusted.create('word_timeline.v1', {'media_hash': 'sha256:'+'b'*64, 'language': 'zh', 'status': 'script_mismatch', 'plan_ref': ref, 'turn_id': 'question', 'placement_id': 'voice', 'source_in_us': 0, 'source_out_us': 10_000_000, 'words': [], 'recognized_transcript': raw, 'recognized_words': [{'text': raw, 'start_char': 0, 'end_char': len(raw), **({'start_us': 500_000, 'end_us': 6_000_000} if timed else {})}]}, [ref])
    return store, ref, {'artifact_id': rec['id'], 'revision': 1}


def service():
    from novelvideo.freezone.remix import agent_speech_review
    return agent_speech_review


def test_agent_word_change_requires_authenticated_evidence_and_keeps_raw(tmp_path):
    api = service()
    store, ref, rec = setup(tmp_path)
    review = api.get_speech_review(store, ref, 'take', rec)
    word_id = review['recognized_words'][0]['id']
    preview = api.preview_speech_correction(store, ref, 'take', rec, [{'word_id': word_id, 'text': '为什么', 'reason': 'ASR typo'}], actor_id='agent-1', actor_kind='agent')
    assert 'audio_review_unavailable' in preview['payload']['blockers']
    assert preview['payload']['differences'] == [{'word_id': word_id, 'before': '为什魔', 'after': '为什么', 'reason': 'ASR typo', 'change_kind': 'lexical'}]
    with pytest.raises(ValueError, match='evidence'):
        api.apply_speech_correction(store, {'artifact_id': preview['id'], 'revision': 1}, None, 'apply-1')
    receipt = api.create_speech_correction_review(store, {'artifact_id': preview['id'], 'revision': 1}, actor_id='user-1', actor_kind='user', accepted=True)
    receipt_ref = {'artifact_id': receipt['id'], 'revision': 1}
    result = api.apply_speech_correction(store, {'artifact_id': preview['id'], 'revision': 1}, receipt_ref, 'apply-1')
    assert result['revision'] == 2
    timing = result['payload']['beats'][0]['turns'][0]['speech_timing']
    assert timing['words'][0]['id'] == word_id
    assert timing['words'][0]['end_us'] == 6_000_000
    assert store.read(rec['artifact_id'], 1)['payload']['recognized_words'][0]['text'] == '为什魔'
    assert api.apply_speech_correction(store, {'artifact_id': preview['id'], 'revision': 1}, receipt_ref, 'apply-1')['revision'] == 2


def test_punctuation_applies_without_claiming_listening(tmp_path):
    api = service()
    store, ref, rec = setup(tmp_path, raw='为什么')
    preview = api.preview_speech_correction(store, ref, 'take', rec, [], actor_id='agent-1', actor_kind='agent')
    assert preview['payload']['blockers'] == []
    result = api.apply_speech_correction(store, {'artifact_id': preview['id'], 'revision': 1}, None, 'punctuation')
    assert result['payload']['speech_cues'][0]['source_end_us'] == 6_000_000
    evidence = store.read(**{'artifact_id': result['payload']['speech_cues'][0]['evidence_ref']['artifact_id'], 'revision': 1})
    assert evidence['payload']['actor_kind'] == 'agent'
    assert evidence['payload']['correction_source'] == 'punctuation'


def test_agent_cannot_issue_user_receipt_and_stale_preview_conflicts(tmp_path):
    api = service()
    store, ref, rec = setup(tmp_path, raw='为什么')
    preview = api.preview_speech_correction(store, ref, 'take', rec, [], actor_id='agent-1', actor_kind='agent')
    pref = {'artifact_id': preview['id'], 'revision': 1}
    with pytest.raises(ValueError, match='authenticated user'):
        api.create_speech_correction_review(store, pref, actor_id='agent-1', actor_kind='agent', accepted=True)
    payload = deepcopy(store.read(ref['artifact_id'], 1)['payload'])
    payload['goal'] = 'changed'
    store.revise(ref['artifact_id'], 1, payload, [])
    with pytest.raises(RemixArtifactConflict):
        api.apply_speech_correction(store, pref, None, 'stale')


def test_receipt_cannot_hide_real_missing_words(tmp_path):
    api = service()
    store, ref, rec = setup(tmp_path, text='为什么好喝？', raw='为什么')
    preview = api.preview_speech_correction(store, ref, 'take', rec, [], actor_id='agent-1', actor_kind='agent')
    assert any(item.startswith('target_or_timing_mismatch') for item in preview['payload']['blockers'])
    pref = {'artifact_id': preview['id'], 'revision': 1}
    receipt = api.create_speech_correction_review(store, pref, actor_id='user-1', actor_kind='user', accepted=True)
    with pytest.raises(ValueError, match='evidence'):
        api.apply_speech_correction(store, pref, {'artifact_id': receipt['id'], 'revision': 1}, 'missing')


def test_preview_rejects_invented_word_times_and_listening_claims(tmp_path):
    api = service()
    store, ref, rec = setup(tmp_path)
    word_id = api.get_speech_review(store, ref, 'take', rec)['recognized_words'][0]['id']
    with pytest.raises(ValueError, match='word_id'):
        api.preview_speech_correction(store, ref, 'take', rec, [{'word_id': word_id, 'text': '为什么', 'reason': 'heard', 'start_us': 0, 'listened': True}], actor_id='agent-1')


def test_paginated_raw_words_and_no_speech_skip(tmp_path):
    api = service()
    store, ref, rec = setup(tmp_path)
    first = api.get_speech_review(store, ref, 'take', rec, limit=1)
    assert first['total_words'] == 1 and first['next_offset'] is None
    assert api.get_speech_review(store, ref, 'take', rec, offset=1)['recognized_words'] == []


def test_missing_word_times_are_explicit_blocker(tmp_path):
    api = service()
    store, ref, rec = setup(tmp_path)
    trusted = RemixArtifactStore(tmp_path, 'demo', producer_version='remix-align-production-speech.v1')
    payload = deepcopy(store.read(rec['artifact_id'], 1)['payload'])
    payload['recognized_words'] = []
    empty = trusted.create('word_timeline.v1', payload, [ref])
    preview = api.preview_speech_correction(store, ref, 'take', {'artifact_id': empty['id'], 'revision': 1}, [], actor_id='agent-1')
    assert 'word_timing_required' in preview['payload']['blockers']
    assert preview['payload']['corrected_words'] == []


def test_multispeaker_same_source_orders_turns_and_keeps_six_seconds(tmp_path):
    api = service()
    store, ref, rec = setup(tmp_path, raw='为什么')
    plan = deepcopy(store.read(ref['artifact_id'], 1)['payload'])
    plan['speech_takes'][0]['turn_ids'] = ['question', 'answer']
    source = store.revise(ref['artifact_id'], 1, plan, [])
    ref = {'artifact_id': source['id'], 'revision': 2}
    trusted = RemixArtifactStore(tmp_path, 'demo', producer_version='remix-align-production-speech.v1')
    payload = deepcopy(store.read(rec['artifact_id'], 1)['payload'])
    payload.update(plan_ref=ref, group_turn_ids=['question', 'answer'], recognized_transcript='为什么因为好喝', recognized_words=[
        {'text': '为什么', 'start_char': 0, 'end_char': 3, 'start_us': 500_000, 'end_us': 2_000_000},
        {'text': '因为好喝', 'start_char': 3, 'end_char': 7, 'start_us': 3_000_000, 'end_us': 6_000_000}])
    rec = trusted.create('word_timeline.v1', payload, [ref])
    preview = api.preview_speech_correction(store, ref, 'take', {'artifact_id': rec['id'], 'revision': 1}, [], actor_id='agent-1')
    assert [row['turn_id'] for row in preview['payload']['cues']] == ['question', 'answer']
    result = api.apply_speech_correction(store, {'artifact_id': preview['id'], 'revision': 1}, None, 'multi')
    assert result['payload']['speech_cues'][-1]['source_end_us'] == 6_000_000


def test_store_rejects_fabricated_preview_word_times(tmp_path):
    api = service()
    store, ref, rec = setup(tmp_path, raw='为什么')
    preview = api.preview_speech_correction(store, ref, 'take', rec, [], actor_id='agent-1')
    forged = deepcopy(preview['payload'])
    forged['corrected_words'][0]['end_us'] = 9_000_000
    with pytest.raises(ValueError, match='immutable recognition'):
        store.create('speech_correction_preview.v1', forged, [ref, rec])


def test_existing_valid_word_identity_survives_new_preview(tmp_path):
    api = service()
    store, ref, rec = setup(tmp_path, raw='为什么')
    preview = api.preview_speech_correction(store, ref, 'take', rec, [], actor_id='agent-1')
    accepted = api.apply_speech_correction(store, {'artifact_id': preview['id'], 'revision': 1}, None, 'first')
    payload = deepcopy(accepted['payload'])
    payload['beats'][0]['turns'][0]['speech_timing']['words'][0]['id'] = 'existing-word-anchor'
    payload['speech_cues'][0]['word_ids'] = ['existing-word-anchor']
    revised = store.revise(accepted['id'], 2, payload, accepted['upstream_refs'])
    reviewed = api.get_speech_review(store, {'artifact_id': revised['id'], 'revision': 3}, 'take', rec)
    assert reviewed['recognized_words'][0]['id'] == 'existing-word-anchor'
