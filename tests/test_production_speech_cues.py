import pytest

from novelvideo.freezone.remix.speech_cues import import_source_srt, export_program_srt


def test_source_srt_preserves_overlap_multiline_and_bom():
    rows = import_source_srt('\ufeff1\r\n00:00:00,500 --> 00:00:04,100\r\nHello\r\nworld\r\n\r\n2\r\n00:00:03,000 --> 00:00:05,800\r\nReply\r\n', 'take', duration_us=6_400_000)
    assert [(row['source_start_us'], row['source_end_us']) for row in rows] == [(500_000, 4_100_000), (3_000_000, 5_800_000)]
    assert rows[0]['text'] == 'Hello\nworld'
    assert rows[0]['word_ids'] == []
    assert export_program_srt(rows).startswith('1\n00:00:00,500 --> 00:00:04,100')


@pytest.mark.parametrize('interval', ['00:00:02,000 --> 00:00:01,000', '00:00:00,000 --> 00:00:07,000', '-00:00:00,500 --> 00:00:01,000'])
def test_source_srt_rejects_invalid_source_time(interval):
    with pytest.raises(ValueError):
        import_source_srt(f'1\n{interval}\nHello', 'take', duration_us=6_400_000)


def test_import_evidence_reparses_original_srt():
    from novelvideo.freezone.remix.contracts import validate_payload
    payload = {'take_id': 'take', 'media_hash': 'b' * 64, 'basis': 'srt_import',
               'source_plan_ref': {'artifact_id': 'plan', 'revision': 1},
               'original_srt': '1\n00:00:00,500 --> 00:00:01,000\nHello',
               'cues': [{'id': 'cue', 'turn_id': 'question', 'text': 'Hello', 'start_us': 500_000, 'end_us': 1_000_000}]}
    validate_payload('speech_cue_evidence.v1', payload)
    payload['cues'][0]['end_us'] = 2_000_000
    with pytest.raises(ValueError, match='original SRT'):
        validate_payload('speech_cue_evidence.v1', payload)


def test_corrected_srt_links_trusted_raw_recognition_of_same_take(tmp_path):
    from test_production_plan_v2 import _plan
    from novelvideo.freezone.remix.store import RemixArtifactStore

    store = RemixArtifactStore(tmp_path, 'demo')
    trusted = RemixArtifactStore(tmp_path, 'demo', producer_version='remix-align-production-speech.v1')
    plan = _plan()
    plan['speech_takes'] = [{'id': 'take', 'beat_id': 'hook', 'resource_id': 'music', 'turn_ids': ['question'],
                             'origin_placement_id': 'voice', 'source_in_us': 0, 'source_out_us': None,
                             'speed_num': 1, 'speed_den': 1, 'lead_in_frames': 0}]
    source = store.create('production_plan.v2', plan, [])
    source_ref = {'artifact_id': source['id'], 'revision': 1}
    recognition = trusted.create('word_timeline.v1', {
        'media_hash': 'sha256:' + 'b' * 64, 'language': 'zh', 'status': 'script_mismatch',
        'plan_ref': source_ref, 'turn_id': 'question', 'placement_id': 'voice',
        'source_in_us': 0, 'source_out_us': 10_000_000, 'words': [],
        'recognized_transcript': '为什魔',
        'recognized_words': [{'text': '为什魔', 'start_char': 0, 'end_char': 3,
                              'start_us': 500_000, 'end_us': 1_000_000}],
    }, [source_ref])
    recognition_ref = {'artifact_id': recognition['id'], 'revision': 1}
    evidence = {'take_id': 'take', 'media_hash': 'b' * 64, 'basis': 'srt_import',
                'source_plan_ref': source_ref, 'recognition_ref': recognition_ref,
                'original_srt': '1\n00:00:00,500 --> 00:00:01,000\n为什么？',
                'cues': [{'id': 'cue', 'turn_id': 'question', 'text': '为什么？',
                          'start_us': 500_000, 'end_us': 1_000_000}]}
    saved = store.create('speech_cue_evidence.v1', evidence, [source_ref, recognition_ref])
    assert saved['payload']['recognition_ref'] == recognition_ref
    corrected = dict(evidence, corrected_words=[{'text': '为什么', 'start_us': 500_000, 'end_us': 900_000},
                                                  {'text': '？', 'start_us': 900_000, 'end_us': 1_000_000}])
    saved_words = store.create('speech_cue_evidence.v1', corrected, [source_ref, recognition_ref])
    assert len(saved_words['payload']['corrected_words']) == 2
    import hashlib
    from copy import deepcopy
    word_evidence_ref = {'artifact_id': saved_words['id'], 'revision': 1}
    accepted_plan = deepcopy(plan)
    accepted_turn = accepted_plan['beats'][0]['turns'][0]
    accepted_turn['speech_timing'] = {
        'placement_id': 'voice', 'media_hash': 'b' * 64,
        'text': '为什么？', 'text_hash': hashlib.sha256('为什么？'.encode()).hexdigest(),
        'evidence_ref': word_evidence_ref,
        'words': [
            {'id': 'word-1', 'text': '为什么', 'start_us': 500_000, 'end_us': 900_000},
            {'id': 'word-2', 'text': '？', 'start_us': 900_000, 'end_us': 1_000_000},
        ],
    }
    accepted_plan['speech_cues'] = [{
        'id': 'cue', 'turn_id': 'question', 'take_id': 'take', 'text': '为什么？',
        'source_start_us': 500_000, 'source_end_us': 1_000_000,
        'timing_source': 'srt_import', 'timing_status': 'ready',
        'word_ids': ['word-1', 'word-2'], 'evidence_ref': word_evidence_ref,
    }]
    accepted = store.revise(source['id'], 1, accepted_plan, [word_evidence_ref])
    assert accepted['payload']['speech_cues'][0]['word_ids'] == ['word-1', 'word-2']
    missing_word_ids = deepcopy(accepted_plan)
    missing_word_ids['speech_cues'][0]['word_ids'] = []
    with pytest.raises(ValueError, match='cue word IDs'):
        store.revise(source['id'], 2, missing_word_ids, [word_evidence_ref])
    dropped_timing = deepcopy(accepted_plan)
    del dropped_timing['beats'][0]['turns'][0]['speech_timing']
    with pytest.raises(ValueError, match='missing words|corrected speech timing'):
        store.revise(source['id'], 2, dropped_timing, [word_evidence_ref])
    altered = deepcopy(accepted_plan)
    altered['beats'][0]['turns'][0]['speech_timing']['words'][0]['start_us'] = 400_000
    with pytest.raises(ValueError, match='corrected source words'):
        store.revise(source['id'], 2, altered, [word_evidence_ref])
    with pytest.raises(ValueError, match='corrected word text'):
        store.create('speech_cue_evidence.v1', dict(corrected, corrected_words=[
            {'text': '不匹配', 'start_us': 500_000, 'end_us': 1_000_000},
        ]), [source_ref, recognition_ref])
    with pytest.raises(ValueError, match='corrected word'):
        store.create('speech_cue_evidence.v1', dict(corrected, corrected_words=[
            {'text': '为什么', 'start_us': 500_000, 'end_us': 11_000_000},
        ]), [source_ref, recognition_ref])
    with pytest.raises(ValueError, match='recognition_ref'):
        store.create('speech_cue_evidence.v1', evidence, [source_ref])
    mismatched = dict(evidence, media_hash='c' * 64)
    with pytest.raises(ValueError, match='source differs'):
        store.create('speech_cue_evidence.v1', mismatched, [source_ref, recognition_ref])
    other_take = trusted.create('word_timeline.v1', {
        **recognition['payload'], 'placement_id': 'other-voice',
    }, [source_ref])
    other_ref = {'artifact_id': other_take['id'], 'revision': 1}
    with pytest.raises(ValueError, match='recognition_ref differs'):
        store.create('speech_cue_evidence.v1', dict(evidence, recognition_ref=other_ref), [source_ref, other_ref])


def test_corrected_words_must_match_ordered_turns_and_exact_cue_envelopes(tmp_path):
    from test_production_plan_v2 import _plan
    from novelvideo.freezone.remix.store import RemixArtifactStore

    store = RemixArtifactStore(tmp_path, 'demo')
    trusted = RemixArtifactStore(tmp_path, 'demo', producer_version='remix-align-production-speech.v1')
    plan = _plan()
    plan['beats'][0]['turns'][0]['text'] = '黑'
    plan['beats'][0]['turns'][1]['text'] = '咖啡'
    plan['speech_takes'] = [{'id': 'take', 'beat_id': 'hook', 'resource_id': 'music',
                             'turn_ids': ['question', 'answer'], 'origin_placement_id': 'voice',
                             'source_in_us': 0, 'source_out_us': None, 'speed_num': 1,
                             'speed_den': 1, 'lead_in_frames': 0}]
    source = store.create('production_plan.v2', plan, [])
    ref = {'artifact_id': source['id'], 'revision': 1}
    recognition = trusted.create('word_timeline.v1', {
        'media_hash': 'sha256:' + 'b' * 64, 'language': 'zh', 'status': 'script_mismatch',
        'plan_ref': ref, 'turn_id': 'question', 'group_turn_ids': ['question', 'answer'],
        'placement_id': 'voice', 'source_in_us': 0, 'source_out_us': 10_000_000,
        'words': [], 'recognized_transcript': '黑咖菲',
        'recognized_words': [{'text': '黑咖菲', 'start_char': 0, 'end_char': 3,
                              'start_us': 100_000, 'end_us': 400_000}],
    }, [ref])
    recognition_ref = {'artifact_id': recognition['id'], 'revision': 1}
    evidence = {'take_id': 'take', 'media_hash': 'b' * 64, 'basis': 'srt_import',
                'source_plan_ref': ref, 'recognition_ref': recognition_ref,
                'corrected_words': [{'text': '黑', 'start_us': 100_000, 'end_us': 200_000},
                                    {'text': '咖啡', 'start_us': 200_000, 'end_us': 400_000}],
                'original_srt': '1\n00:00:00,200 --> 00:00:00,400\n黑\n\n2\n00:00:00,100 --> 00:00:00,200\n咖啡',
                'cues': [{'id': 'first', 'turn_id': 'question', 'text': '黑', 'start_us': 200_000, 'end_us': 400_000},
                         {'id': 'second', 'turn_id': 'answer', 'text': '咖啡', 'start_us': 100_000, 'end_us': 200_000}]}
    with pytest.raises(ValueError, match='ordered plan turns|corrected'):
        store.create('speech_cue_evidence.v1', evidence, [ref, recognition_ref])


def test_old_recognition_reuse_requires_unchanged_take_and_group(tmp_path):
    from copy import deepcopy
    from test_production_plan_v2 import _plan
    from novelvideo.freezone.remix.store import RemixArtifactStore

    store = RemixArtifactStore(tmp_path, 'demo')
    trusted = RemixArtifactStore(tmp_path, 'demo', producer_version='remix-align-production-speech.v1')
    plan = _plan()
    plan['speech_takes'] = [{'id': 'take', 'beat_id': 'hook', 'resource_id': 'music',
                             'turn_ids': ['question', 'answer'], 'origin_placement_id': 'voice',
                             'source_in_us': 0, 'source_out_us': None, 'speed_num': 1,
                             'speed_den': 1, 'lead_in_frames': 0}]
    source = store.create('production_plan.v2', plan, [])
    ref1 = {'artifact_id': source['id'], 'revision': 1}
    recognition = trusted.create('word_timeline.v1', {
        'media_hash': 'sha256:' + 'b' * 64, 'language': 'zh', 'status': 'script_mismatch',
        'plan_ref': ref1, 'turn_id': 'question', 'group_turn_ids': ['question', 'answer'],
        'placement_id': 'voice', 'source_in_us': 0, 'source_out_us': 10_000_000,
        'words': [], 'recognized_transcript': '为什魔因为好喝',
        'recognized_words': [{'text': '为什魔因为好喝', 'start_char': 0, 'end_char': 7,
                              'start_us': 500_000, 'end_us': 1_000_000}],
    }, [ref1])
    recognition_ref = {'artifact_id': recognition['id'], 'revision': 1}
    revised = deepcopy(plan)
    revised['goal'] = '只改画面目标'
    store.revise(source['id'], 1, revised, [])
    ref2 = {'artifact_id': source['id'], 'revision': 2}
    evidence = {'take_id': 'take', 'media_hash': 'b' * 64, 'basis': 'srt_import',
                'source_plan_ref': ref2, 'recognition_ref': recognition_ref,
                'corrected_words': [{'text': '为什么', 'start_us': 500_000, 'end_us': 700_000},
                                    {'text': '因为好喝', 'start_us': 700_000, 'end_us': 1_000_000}],
                'original_srt': '1\n00:00:00,500 --> 00:00:00,700\n为什么？\n\n2\n00:00:00,700 --> 00:00:01,000\n因为好喝。',
                'cues': [{'id': 'first', 'turn_id': 'question', 'text': '为什么？', 'start_us': 500_000, 'end_us': 700_000},
                         {'id': 'second', 'turn_id': 'answer', 'text': '因为好喝。', 'start_us': 700_000, 'end_us': 1_000_000}]}
    store.create('speech_cue_evidence.v1', evidence, [ref2, recognition_ref])
    changed = deepcopy(revised)
    changed['speech_takes'][0]['turn_ids'] = ['question']
    store.revise(source['id'], 2, changed, [])
    ref3 = {'artifact_id': source['id'], 'revision': 3}
    one_turn = dict(evidence, source_plan_ref=ref3,
                    corrected_words=[{'text': '为什么', 'start_us': 500_000, 'end_us': 700_000}],
                    original_srt='1\n00:00:00,500 --> 00:00:00,700\n为什么？',
                    cues=[evidence['cues'][0]])
    with pytest.raises(ValueError, match='recognition_ref differs|group'):
        store.create('speech_cue_evidence.v1', one_turn, [ref3, recognition_ref])


def test_corrected_word_matching_uses_nfkc(tmp_path):
    from test_production_plan_v2 import _plan
    from novelvideo.freezone.remix.store import RemixArtifactStore

    store = RemixArtifactStore(tmp_path, 'demo')
    trusted = RemixArtifactStore(tmp_path, 'demo', producer_version='remix-align-production-speech.v1')
    plan = _plan()
    plan['beats'][0]['turns'][0]['text'] = 'ＡＢＣ？'
    plan['speech_takes'] = [{'id': 'take', 'beat_id': 'hook', 'resource_id': 'music',
                             'turn_ids': ['question'], 'origin_placement_id': 'voice',
                             'source_in_us': 0, 'source_out_us': None, 'speed_num': 1,
                             'speed_den': 1, 'lead_in_frames': 0}]
    source = store.create('production_plan.v2', plan, [])
    ref = {'artifact_id': source['id'], 'revision': 1}
    recognition = trusted.create('word_timeline.v1', {
        'media_hash': 'sha256:' + 'b' * 64, 'language': 'zh', 'status': 'script_mismatch',
        'plan_ref': ref, 'turn_id': 'question', 'placement_id': 'voice',
        'source_in_us': 0, 'source_out_us': 10_000_000, 'words': [],
        'recognized_transcript': 'ABX',
        'recognized_words': [{'text': 'ABX', 'start_char': 0, 'end_char': 3,
                              'start_us': 500_000, 'end_us': 1_000_000}],
    }, [ref])
    recognition_ref = {'artifact_id': recognition['id'], 'revision': 1}
    evidence = {'take_id': 'take', 'media_hash': 'b' * 64, 'basis': 'srt_import',
                'source_plan_ref': ref, 'recognition_ref': recognition_ref,
                'corrected_words': [{'text': 'ABC', 'start_us': 500_000, 'end_us': 900_000},
                                    {'text': '？', 'start_us': 900_000, 'end_us': 1_000_000}],
                'original_srt': '1\n00:00:00,500 --> 00:00:01,000\nＡＢＣ？',
                'cues': [{'id': 'cue', 'turn_id': 'question', 'text': 'ＡＢＣ？',
                          'start_us': 500_000, 'end_us': 1_000_000}]}
    saved = store.create('speech_cue_evidence.v1', evidence, [ref, recognition_ref])
    assert saved['payload']['corrected_words'][0]['text'] == 'ABC'


@pytest.mark.parametrize("basis", ["manual", "srt_import"])
def test_manual_evidence_requires_project_lineage_and_exact_accepted_row(tmp_path, basis):
    from test_production_plan_v2 import _plan
    from novelvideo.freezone.remix.store import RemixArtifactStore
    store = RemixArtifactStore(tmp_path, 'demo')
    plan = _plan()
    plan['speech_takes'] = [{'id': 'take', 'beat_id': 'hook', 'resource_id': 'music', 'turn_ids': ['question'],
                             'origin_placement_id': 'voice', 'source_in_us': 0, 'source_out_us': None,
                             'speed_num': 1, 'speed_den': 1, 'lead_in_frames': 0}]
    source = store.create('production_plan.v2', plan, [])
    ref = {'artifact_id': source['id'], 'revision': 1}
    evidence = {'take_id': 'take', 'media_hash': 'b' * 64, 'basis': basis, 'source_plan_ref': ref,
                'cues': [{'id': 'cue', 'turn_id': 'question', 'text': '为什么？', 'start_us': 500_000, 'end_us': 1_000_000}]}
    if basis == 'srt_import':
        evidence['original_srt'] = '1\n00:00:00,500 --> 00:00:01,000\n为什么？'
    with pytest.raises(ValueError, match='upstream_refs'):
        store.create('speech_cue_evidence.v1', evidence, [])
    saved = store.create('speech_cue_evidence.v1', evidence, [ref])
    evidence_ref = {'artifact_id': saved['id'], 'revision': 1}
    plan['speech_cues'] = [{'id': 'cue', 'turn_id': 'question', 'take_id': 'take', 'text': '为什么？',
                            'source_start_us': 500_000, 'source_end_us': 1_000_000, 'timing_source': 'manual',
                            'timing_status': 'ready', 'word_ids': [], 'evidence_ref': evidence_ref}]
    plan['speech_cues'][0]['timing_source'] = basis
    store.revise(source['id'], 1, plan, [evidence_ref])
    plan['speech_cues'][0]['source_end_us'] = 2_000_000
    with pytest.raises(ValueError, match='differs'):
        store.revise(source['id'], 2, plan, [evidence_ref])
    other = RemixArtifactStore(tmp_path / 'other', 'other')
    with pytest.raises(FileNotFoundError):
        other.create('speech_cue_evidence.v1', evidence, [ref])


def test_legacy_upgrade_is_idempotent_and_does_not_invent_unknown_time():
    from novelvideo.freezone.remix.speech_cues import upgrade_plan_speech
    from test_production_plan_v2 import _plan
    import hashlib
    plan = _plan()
    turn = plan['beats'][0]['turns'][0]
    turn['speech_placement_id'] = 'score'
    turn['speech_timing'] = {'placement_id': 'score', 'media_hash': 'b' * 64, 'text': turn['text'],
                             'text_hash': hashlib.sha256(turn['text'].encode()).hexdigest(),
                             'evidence_ref': {'artifact_id': 'words', 'revision': 1},
                             'words': [{'id': 'word', 'text': '为什么', 'start_us': 500_000, 'end_us': 900_000}]}
    upgraded = upgrade_plan_speech(plan)
    assert upgraded['speech_cues'][0]['source_start_us'] == 500_000
    assert upgraded['speech_cues'][0]['word_ids'] == ['word']
    assert upgraded == upgrade_plan_speech(upgraded)
    assert plan.get('speech_takes') is None


def test_take_alignment_source_survives_deleted_display_and_rejects_other_instance():
    from test_production_plan_v2 import _plan
    from novelvideo.freezone.remix.production_plan_v2 import production_speech_source
    plan = _plan()
    plan['speech_takes'] = [{'id': 'take', 'beat_id': 'hook', 'resource_id': 'music', 'turn_ids': ['question'],
                            'origin_placement_id': 'deleted-voice', 'source_in_us': 0, 'source_out_us': None,
                            'speed_num': 1, 'speed_den': 1, 'lead_in_frames': 0}]
    turn, source, resource = production_speech_source(plan, 'question', take_id='take')
    assert (turn.id, source.id, resource.id) == ('question', 'deleted-voice', 'music')
    with pytest.raises(ValueError, match='same speech instance'):
        production_speech_source(plan, 'question', placement_id='base', take_id='take')
    with pytest.raises(ValueError, match='belonging'):
        production_speech_source(plan, 'answer', take_id='take')


def test_srt_import_rejects_text_outside_bound_spoken_turn(tmp_path):
    from test_production_plan_v2 import _plan
    from novelvideo.freezone.remix.store import RemixArtifactStore
    store = RemixArtifactStore(tmp_path, 'demo')
    plan = _plan()
    plan['speech_takes'] = [{'id': 'take', 'beat_id': 'hook', 'resource_id': 'music', 'turn_ids': ['question'],
                            'origin_placement_id': 'voice', 'source_in_us': 0, 'source_out_us': None,
                            'speed_num': 1, 'speed_den': 1, 'lead_in_frames': 0}]
    saved = store.create('production_plan.v2', plan, [])
    ref = {'artifact_id': saved['id'], 'revision': 1}
    evidence = {'take_id': 'take', 'media_hash': 'b' * 64, 'basis': 'srt_import', 'source_plan_ref': ref,
                'original_srt': '1\n00:00:00,500 --> 00:00:01,000\n伪造台词',
                'cues': [{'id': 'cue', 'turn_id': 'question', 'text': '伪造台词', 'start_us': 500_000, 'end_us': 1_000_000}]}
    with pytest.raises(ValueError, match='spoken turn'):
        store.create('speech_cue_evidence.v1', evidence, [ref])


def test_ready_cue_rejects_evidence_from_independent_plan(tmp_path):
    from test_production_plan_v2 import _plan
    from novelvideo.freezone.remix.store import RemixArtifactStore
    store = RemixArtifactStore(tmp_path, 'demo')
    plan = _plan()
    plan['speech_takes'] = [{'id': 'take', 'beat_id': 'hook', 'resource_id': 'music', 'turn_ids': ['question'],
                            'origin_placement_id': 'voice', 'source_in_us': 0, 'source_out_us': None,
                            'speed_num': 1, 'speed_den': 1, 'lead_in_frames': 0}]
    first = store.create('production_plan.v2', plan, [])
    second = store.create('production_plan.v2', plan, [])
    ref = {'artifact_id': first['id'], 'revision': 1}
    evidence = store.create('speech_cue_evidence.v1', {'take_id': 'take', 'media_hash': 'b' * 64, 'basis': 'manual',
                             'source_plan_ref': ref, 'cues': [{'id': 'cue', 'turn_id': 'question', 'text': '为什么？',
                                                            'start_us': 500_000, 'end_us': 1_000_000}]}, [ref])
    evidence_ref = {'artifact_id': evidence['id'], 'revision': 1}
    plan['speech_cues'] = [{'id': 'cue', 'turn_id': 'question', 'take_id': 'take', 'text': '为什么？',
                            'source_start_us': 500_000, 'source_end_us': 1_000_000, 'timing_source': 'manual',
                            'timing_status': 'ready', 'word_ids': [], 'evidence_ref': evidence_ref}]
    with pytest.raises(ValueError, match='same production plan'):
        store.revise(second['id'], 1, plan, [evidence_ref])


def test_conflict_branch_preserves_accepted_cue_evidence_with_explicit_lineage(tmp_path):
    from test_production_plan_v2 import _plan
    from novelvideo.freezone.remix.store import RemixArtifactStore

    store = RemixArtifactStore(tmp_path, 'demo')
    plan = _plan()
    plan['speech_takes'] = [{'id': 'take', 'beat_id': 'hook', 'resource_id': 'music', 'turn_ids': ['question'],
                             'origin_placement_id': 'voice', 'source_in_us': 0, 'source_out_us': None,
                             'speed_num': 1, 'speed_den': 1, 'lead_in_frames': 0}]
    first = store.create('production_plan.v2', plan, [])
    first_ref = {'artifact_id': first['id'], 'revision': 1}
    evidence = store.create('speech_cue_evidence.v1', {
        'take_id': 'take', 'media_hash': 'b' * 64, 'basis': 'manual', 'source_plan_ref': first_ref,
        'cues': [{'id': 'cue', 'turn_id': 'question', 'text': '为什么？', 'start_us': 500_000, 'end_us': 1_000_000}],
    }, [first_ref])
    evidence_ref = {'artifact_id': evidence['id'], 'revision': 1}
    plan['speech_cues'] = [{'id': 'cue', 'turn_id': 'question', 'take_id': 'take', 'text': '为什么？',
                            'source_start_us': 500_000, 'source_end_us': 1_000_000, 'timing_source': 'manual',
                            'timing_status': 'ready', 'word_ids': [], 'evidence_ref': evidence_ref}]
    saved = store.revise(first['id'], 1, plan, [evidence_ref])
    branch_source_ref = {'artifact_id': saved['id'], 'revision': saved['revision']}
    plan['provenance']['branch_source_ref'] = branch_source_ref
    branch = store.create('production_plan.v2', plan, [branch_source_ref, evidence_ref])
    plan['goal'] = '分支后继续编辑'
    store.revise(branch['id'], 1, plan, [branch_source_ref, evidence_ref])


def test_client_word_evidence_cannot_be_accepted_as_asr(tmp_path):
    import hashlib
    from test_production_plan_v2 import _plan
    from novelvideo.freezone.remix.store import RemixArtifactStore
    store = RemixArtifactStore(tmp_path, 'demo')
    plan = _plan()
    saved = store.create('production_plan.v2', plan, [])
    source_ref = {'artifact_id': saved['id'], 'revision': 1}
    digest = hashlib.sha256('为什么？'.encode()).hexdigest()
    evidence = store.create('word_timeline.v1', {'media_hash': 'sha256:' + 'b' * 64, 'language': 'zh',
                            'status': 'completed', 'turn_id': 'question', 'placement_id': 'score',
                            'plan_ref': source_ref, 'text_hash': digest,
                            'words': [{'text': '为什么', 'start_char': 0, 'end_char': 3, 'start_us': 500_000, 'end_us': 1_000_000}]}, [source_ref])
    ref = {'artifact_id': evidence['id'], 'revision': 1}
    plan['beats'][0]['turns'][0]['speech_timing'] = {'placement_id': 'score', 'media_hash': 'b' * 64,
        'text': '为什么？', 'text_hash': digest, 'evidence_ref': ref,
        'words': [{'id': 'word', 'text': '为什么', 'start_us': 500_000, 'end_us': 1_000_000}]}
    with pytest.raises(ValueError, match='server alignment'):
        store.revise(saved['id'], 1, plan, [ref])


@pytest.mark.parametrize('prefix,valid', [('甲：', True), ('甲: ', True), ('乙：', False)])
def test_srt_speaker_prefix_must_match_bound_speaker(tmp_path, prefix, valid):
    from test_production_plan_v2 import _plan
    from novelvideo.freezone.remix.store import RemixArtifactStore
    store = RemixArtifactStore(tmp_path, 'demo')
    plan = _plan()
    plan['speech_takes'] = [{'id': 'take', 'beat_id': 'hook', 'resource_id': 'music', 'turn_ids': ['question'],
                            'origin_placement_id': 'voice', 'source_in_us': 0, 'source_out_us': None,
                            'speed_num': 1, 'speed_den': 1, 'lead_in_frames': 0}]
    source = store.create('production_plan.v2', plan, [])
    ref = {'artifact_id': source['id'], 'revision': 1}
    evidence = {'take_id': 'take', 'media_hash': 'b' * 64, 'basis': 'srt_import', 'source_plan_ref': ref,
                'original_srt': f'1\n00:00:00,500 --> 00:00:01,000\n{prefix}为什么？',
                'cues': [{'id': 'cue', 'turn_id': 'question', 'text': '为什么？', 'start_us': 500_000, 'end_us': 1_000_000}]}
    if valid:
        saved = store.create('speech_cue_evidence.v1', evidence, [ref])
        assert saved['payload']['original_srt'] == evidence['original_srt']
        assert saved['payload']['cues'][0]['text'] == '为什么？'
    else:
        with pytest.raises(ValueError, match='speaker'):
            store.create('speech_cue_evidence.v1', evidence, [ref])


def test_previously_accepted_legacy_asr_evidence_can_migrate_but_new_forgery_cannot(tmp_path):
    import hashlib
    import json
    from copy import deepcopy
    from test_production_plan_v2 import _plan
    from novelvideo.freezone.remix.store import RemixArtifactStore
    store = RemixArtifactStore(tmp_path, 'demo')
    trusted = RemixArtifactStore(tmp_path, 'demo', producer_version='remix-align-production-speech.v1')
    plan = _plan()
    original = store.create('production_plan.v2', plan, [])
    source_ref = {'artifact_id': original['id'], 'revision': 1}
    digest = hashlib.sha256('为什么？'.encode()).hexdigest()
    words = {'media_hash': 'sha256:' + 'b' * 64, 'language': 'zh', 'status': 'completed',
             'turn_id': 'question', 'placement_id': 'score', 'plan_ref': source_ref, 'text_hash': digest,
             'words': [{'text': '为什么', 'start_char': 0, 'end_char': 3, 'start_us': 500_000, 'end_us': 1_000_000}]}
    evidence = trusted.create('word_timeline.v1', words, [source_ref])
    ref = {'artifact_id': evidence['id'], 'revision': 1}
    plan['beats'][0]['turns'][0]['speech_timing'] = {'placement_id': 'score', 'media_hash': 'b' * 64,
        'text': '为什么？', 'text_hash': digest, 'evidence_ref': ref,
        'words': [{'id': 'word', 'text': '为什么', 'start_us': 500_000, 'end_us': 1_000_000}]}
    store.revise(original['id'], 1, plan, [ref])
    # Reproduce a historical runner artifact already accepted before producer marking.
    envelope_path = store.root / evidence['id'] / '1.json'
    envelope = json.loads(envelope_path.read_text())
    envelope['producer_version'] = 'remix-store.v1'
    envelope_path.write_text(json.dumps(envelope))
    from novelvideo.freezone.remix.speech_cues import upgrade_plan_speech
    migrated = upgrade_plan_speech(plan)
    saved = store.revise(original['id'], 2, migrated, [ref])
    assert saved['revision'] == 3
    assert saved['payload']['speech_cues'][0]['timing_status'] == 'review'
    forged = store.create('word_timeline.v1', words, [source_ref])
    forged_ref = {'artifact_id': forged['id'], 'revision': 1}
    changed = deepcopy(migrated)
    changed['beats'][0]['turns'][0]['speech_timing']['evidence_ref'] = forged_ref
    changed['speech_cues'][0]['evidence_ref'] = forged_ref
    with pytest.raises(ValueError, match='server alignment'):
        store.revise(original['id'], 3, changed, [forged_ref])
    changed = deepcopy(migrated)
    changed['beats'][0]['turns'][0]['speech_timing']['words'][0]['end_us'] += 1
    changed['speech_cues'][0]['source_end_us'] += 1
    with pytest.raises(ValueError, match='server alignment'):
        store.revise(original['id'], 3, changed, [ref])


@pytest.mark.parametrize('recognized_end_us', [2_000_000, 10_000_000])
def test_legacy_upgrade_never_claims_unverified_or_partial_asr_ready(recognized_end_us):
    import hashlib
    from test_production_plan_v2 import _plan
    from novelvideo.freezone.remix.speech_cues import upgrade_plan_speech, build_speech_cues
    plan = _plan()
    turn = plan['beats'][0]['turns'][0]
    turn['speech_placement_id'] = 'score'
    turn['speech_timing'] = {'placement_id': 'score', 'media_hash': 'b' * 64, 'text': turn['text'],
                             'text_hash': hashlib.sha256(turn['text'].encode()).hexdigest(),
                             'source_in_us': 0, 'source_out_us': recognized_end_us,
                             'evidence_ref': {'artifact_id': 'legacy-word', 'revision': 1},
                             'words': [{'id': 'legacy-word-id', 'text': '为什么', 'start_us': 500_000, 'end_us': 900_000}]}
    upgraded = upgrade_plan_speech(plan)
    assert upgraded['speech_cues'][0]['timing_status'] == 'review'
    assert upgraded['speech_cues'][0]['source_start_us'] == 500_000
    assert upgraded['speech_cues'][0]['word_ids'] == ['legacy-word-id']
    assert upgraded['beats'][0]['turns'][0]['speech_timing'] == turn['speech_timing']
    assert build_speech_cues(plan)[0]['timing_status'] == 'review'


@pytest.mark.parametrize('groups', [[['answer'], ['question']], [['question', 'third'], ['answer']]])
def test_backend_rejects_reversed_or_interleaved_speech_take_order(groups):
    from test_production_plan_v2 import _plan
    from novelvideo.freezone.remix.production_plan_v2 import ProductionPlanV2, compile_production_plan_v2
    plan = _plan()
    plan['beats'][0]['turns'].append({'id': 'third', 'speaker': '甲', 'text': '第三句'})
    plan['speech_takes'] = [{'id': f'take-{index}', 'beat_id': 'hook', 'resource_id': 'music', 'turn_ids': ids,
                            'origin_placement_id': f'voice-{index}', 'source_in_us': 0, 'source_out_us': None,
                            'speed_num': 1, 'speed_den': 1, 'lead_in_frames': 0} for index, ids in enumerate(groups)]
    with pytest.raises(ValueError, match='order|interleav'):
        ProductionPlanV2.model_validate(plan)
    with pytest.raises(ValueError, match='order|interleav'):
        compile_production_plan_v2(plan)
