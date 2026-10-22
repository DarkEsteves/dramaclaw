"""Dialogue coverage is conditional; explicit review failures never mean N/A."""
from copy import deepcopy

import pytest

from test_production_audio_sources import planned_video_dialogue
from test_production_review import passing_findings
from novelvideo.freezone.remix.production_review import evaluate_production_review


def silent_plan():
    source = planned_video_dialogue()
    source['beats'][0]['turns'] = []
    source['placements'][0]['source_audio'] = False
    return source


def no_dialogue_findings():
    return [row for row in passing_findings() if row['category'] != 'dialogue']


@pytest.mark.parametrize('mode', [None, 'unspecified'])
def test_silent_plan_does_not_need_invented_dialogue_pass(mode):
    direction = {'requirements': {'dialogue': {'mode': mode}}} if mode else {}
    result = evaluate_production_review(silent_plan(), direction, no_dialogue_findings())
    assert result['status'] == 'passed'
    assert not any(row['category'] == 'dialogue' for row in result['findings'])


@pytest.mark.parametrize('verdict', ['change', 'unsupported'])
def test_silent_plan_does_not_ignore_explicit_dialogue_failure(verdict):
    findings = no_dialogue_findings() + [{**passing_findings()[0], 'verdict': verdict}]
    assert evaluate_production_review(silent_plan(), {}, findings)['status'] == 'changes_required'


@pytest.mark.parametrize('mode', ['preserve_source', 'use_confirmed_text', 'rewrite'])
def test_explicit_dialogue_mode_cannot_be_bypassed_by_deleting_turns(mode):
    result = evaluate_production_review(silent_plan(), {'requirements': {'dialogue': {'mode': mode}}},
                                        no_dialogue_findings())
    assert result['status'] == 'changes_required'
    assert any(row['code'] == 'creative_review_incomplete' and 'dialogue' in row['message']
               for row in result['checks'])


@pytest.mark.parametrize('direction,locked', [
    ({'preserve': {'turns': {'ask': '这个怎么用？'}}}, None),
    ({'preserve_replace': [{'entity_kind': 'turn', 'entity_id': 'ask',
                           'action': 'replace', 'value': '新台词'}]}, None),
    ({'preserve_replace': [{'id': 'spoken', 'entity_kind': 'dialogue',
                           'entity_id': '中文对白', 'action': 'preserve'}]}, None),
    ({}, [{'speaker': '甲', 'text': '这个怎么用？'}]),
])
def test_confirmed_speech_constraints_keep_dialogue_category(direction, locked):
    result = evaluate_production_review(silent_plan(), direction, no_dialogue_findings(), locked_turns=locked)
    assert result['status'] == 'changes_required'
    assert any(row['code'] == 'creative_review_incomplete' for row in result['checks'])


def test_real_dialogue_still_needs_dialogue_finding():
    result = evaluate_production_review(planned_video_dialogue(), {}, no_dialogue_findings())
    assert any(row['code'] == 'creative_review_incomplete' and 'dialogue' in row['message']
               for row in result['checks'])


def test_silent_exemption_does_not_waive_audio_source_or_other_categories():
    source = silent_plan()
    source['placements'][0]['source_audio'] = True
    findings = no_dialogue_findings()
    result = evaluate_production_review(source, {}, findings)
    assert result['status'] == 'changes_required'
    assert any(row['code'] == 'planned_audio_source_review_missing' for row in result['checks'])
    audio = {'category': 'audio_visual_roles', 'entity_kind': 'placement', 'entity_id': 'demo-video',
             'verdict': 'pass', 'reason': 'Reviewed planned audible scene', 'evidence': ['Scene context'],
             'evidence_refs': [{'entity_kind': 'beat', 'entity_id': 'demo', 'field': 'purpose'}]}
    assert evaluate_production_review(source, {}, findings + [audio])['status'] == 'passed'
    incomplete = [row for row in findings if row['category'] != 'output'] + [audio]
    assert evaluate_production_review(source, {}, incomplete)['status'] == 'changes_required'


def test_freeform_semantic_requirement_is_not_waived_with_dialogue():
    direction = {'requirements': {'semantic': [{'id': 'speech', 'text': '保留开场提问'}]}}
    original = deepcopy(direction)
    result = evaluate_production_review(silent_plan(), direction, no_dialogue_findings())
    assert any(row['code'] == 'semantic_requirement_review_missing' for row in result['checks'])
    assert result['status'] == 'changes_required'
    assert direction == original
