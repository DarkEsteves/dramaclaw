"""Local timing is advisory; impossible authored intervals remain review errors."""

from copy import deepcopy

import pytest

from test_production_plan import native_plan


def test_speech_estimate_counts_pronunciation_and_explicit_pause():
    from novelvideo.freezone.remix.production_plan_diagnostics import estimate_speech_duration

    text = '上班来一杯，健身前来一杯，做饭的时候也得先来一杯。'
    normal = estimate_speech_duration(text, language='zh')
    fast = estimate_speech_duration(text, language='zh', pace='fast')
    padded = estimate_speech_duration(text, language='zh', padding_seconds=0.8)
    assert normal['pronunciation_units'] == 22
    assert normal['estimated_seconds'] == pytest.approx(22 / 5.25)
    assert fast['estimated_seconds'] < normal['estimated_seconds']
    assert padded['estimated_seconds'] == pytest.approx(normal['estimated_seconds'] + 0.8)


def test_speech_estimate_exposes_low_confidence_and_unsupported_text():
    from novelvideo.freezone.remix.production_plan_diagnostics import estimate_speech_duration

    mixed = estimate_speech_duration('喝 Black Coffee 123', language='zh')
    assert mixed['estimated_seconds'] is not None
    assert mixed['confidence'] == 'low'
    assert estimate_speech_duration('!!!')['confidence'] == 'unsupported'


def test_speech_estimate_distinguishes_authored_pace_and_default_pause():
    from novelvideo.freezone.remix.production_plan_diagnostics import measure_plan_speech

    plan = native_plan()
    plan['beats'][0]['turns'] = [{'id': 'line', 'speaker': '甲', 'text': '黑咖啡'}]
    beat = next(row for row in measure_plan_speech(plan) if row['entity_kind'] == 'beat')
    assert beat['estimate']['pace_basis'] == 'default_normal'
    assert beat['estimate']['pause_basis'] == 'default_zero'
    plan['beats'][0]['speech_pace'] = 'normal'
    plan['beats'][0]['extra_pause_seconds'] = 0
    beat = next(row for row in measure_plan_speech(plan) if row['entity_kind'] == 'beat')
    assert beat['estimate']['pace_basis'] == 'authored'
    assert beat['estimate']['pause_basis'] == 'authored'


def test_multiple_visuals_do_not_double_count_one_spoken_turn_or_beat_pause():
    from novelvideo.freezone.remix.production_plan_diagnostics import measure_plan_speech

    plan = native_plan()
    plan['beats'][0]['turns'] = [{'id': 'line', 'speaker': '甲', 'text': '黑咖啡'}]
    plan['beats'][0]['extra_pause_seconds'] = 1
    extra = deepcopy(plan['placements'][0])
    extra['id'] = 'reaction'
    plan['placements'].append(extra)
    rows = measure_plan_speech(plan)
    assert [row['entity_id'] for row in rows if row['entity_kind'] == 'turn'] == ['line']
    beat = next(row for row in rows if row['entity_kind'] == 'beat')
    assert beat['estimate']['estimated_seconds'] == pytest.approx(3 / 5.25 + 1)


def test_diagnostics_report_dense_dialogue_and_zero_length_graphic_without_media():
    from novelvideo.freezone.remix.production_plan_diagnostics import collect_plan_diagnostics

    plan = native_plan()
    plan['beats'][0]['duration_frames'] = 100
    plan['beats'][0]['turns'] = [{
        'id': 'line', 'speaker': '甲',
        'text': '上班来一杯，健身前来一杯，做饭的时候也得先来一杯。',
    }]
    plan['placements'].append({
        'id': 'title', 'kind': 'graphic', 'role': 'effect', 'track_id': 'graphics',
        'start': {'kind': 'beat', 'ref_id': 'hook', 'edge': 'end'},
        'end': {'kind': 'beat', 'ref_id': 'hook', 'edge': 'end'},
        'graphic': {'template': 'title_bar', 'text': '结束'},
    })
    before = deepcopy(plan)
    diagnostics = collect_plan_diagnostics(plan)
    assert plan == before
    assert any(item['code'] == 'speech_density' and item['entity_id'] == 'hook'
               and item['severity'] == 'warning' for item in diagnostics)
    assert any(item['code'] == 'invalid_timing' and item['entity_id'] == 'title'
               and item['severity'] == 'error' for item in diagnostics)
    assert all(item['code'] != 'unbound_media' for item in diagnostics)


def test_graphic_animation_cannot_outlast_its_resolved_interval():
    from novelvideo.freezone.remix.production_plan_diagnostics import collect_plan_diagnostics

    plan = native_plan()
    plan['placements'].append({
        'id': 'title', 'kind': 'graphic', 'role': 'effect', 'track_id': 'graphics',
        'start': {'kind': 'beat', 'ref_id': 'hook', 'edge': 'start'},
        'end': {'kind': 'beat', 'ref_id': 'hook', 'edge': 'start', 'offset_frames': 20},
        'graphic': {'template': 'title_bar', 'text': '结束',
                    'enter_frames': 15, 'exit_frames': 15},
    })
    assert any(item['code'] == 'graphic_animation_exceeds_interval'
               for item in collect_plan_diagnostics(plan))


def test_new_spoken_draft_gets_estimated_duration_without_changing_fixed_or_saved_time():
    from novelvideo.freezone.remix.production_plan_draft_normalization import initialize_draft_timing

    plan = native_plan()
    plan['beats'] = [
        {'id': 'spoken', 'purpose': '介绍', 'turns': [
            {'id': 'line', 'text': '上班来一杯，健身前来一杯，做饭的时候也得先来一杯。'}]},
        {'id': 'pause', 'purpose': '停顿', 'duration_frames': 30,
         'timing_mode': 'fixed', 'turns': []},
    ]
    plan['placements'] = []
    before = deepcopy(plan)
    initialized, _ = initialize_draft_timing(plan)
    assert plan == before
    assert initialized['beats'][0]['duration_frames'] == 126
    assert initialized['beats'][1]['duration_frames'] == 30


def test_unspoken_or_unmeasurable_draft_needs_an_authored_duration():
    from novelvideo.freezone.remix.production_plan_draft_normalization import initialize_draft_timing

    plan = native_plan()
    del plan['beats'][0]['duration_frames']
    with pytest.raises(ValueError, match='duration_frames'):
        initialize_draft_timing(plan)
