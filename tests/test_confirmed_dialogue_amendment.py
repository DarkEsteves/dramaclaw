"""Authenticated spoken-text amendments must yield a usable immutable snapshot."""
from copy import deepcopy

import pytest

from test_reference_decision_ingress import setup_plan_pending, setup_pending, message, apply
from novelvideo.chat.reference_decision_ingress import current_requirement_amendment
from novelvideo.freezone.remix.reference_director import list_director_artifacts
from novelvideo.freezone.remix.production_requirements import resolve_production_requirements


def amendment(store, scope, msg, requirements, *, intent='revise'):
    candidate = current_requirement_amendment(store, scope=scope, requester_user_id='u',
        credential_kind='browser_session', message=msg)
    c = list_director_artifacts(store, 'c', 'u')['commissions'][0]
    canvas = {'project_id': 'p', 'canvas_id': 'c', 'nodes': [{'data': {
        'productionPlanV2Ref': c['plan_ref']}}]}
    return apply(store, scope, msg, canvas=canvas, expected_decision_ref=candidate['decision_ref'],
        interpretation={'intent': intent, 'requirements': requirements})


def confirmed(text):
    return {'dialogue': {'mode': 'use_confirmed_text', 'confirmed_turns': [{'speaker': '', 'text': text}]}}


def test_first_and_replacement_text_reach_bound_plan_snapshot(tmp_path):
    store, _, plan_ref = setup_plan_pending(tmp_path)
    old = list_director_artifacts(store, 'c', 'u')['commissions'][0]
    fingerprints = []
    for text in ('欢迎来到新世界。', 'We begin again, together.'):
        scope, msg = message(tmp_path, f'请把口播改为「{text}」')
        result = amendment(store, scope, msg, confirmed(text))
        assert result is not None
        c = result['commission']
        resolved = resolve_production_requirements(store, commission=c)
        assert resolved['locked_turns'] == [{'speaker': '', 'text': text}]
        assert c['direction']['locked_turns'] == resolved['locked_turns']
        assert resolved['requirements_provenance']['user_message_id'].endswith(f":{msg['id']}")
        assert c['plan_ref'] == plan_ref and c['analysis_evidence'] == old['analysis_evidence']
        assert c['phase'] == 'awaiting_plan_review' and c.get('review_ref') is None
        fingerprints.append(resolved['requirements_fingerprint'])
    assert fingerprints[0] != fingerprints[1]


@pytest.mark.parametrize('intent,requirements', [
    ('ambiguous', confirmed('欢迎来到新世界。')),
    ('revise', {'dialogue': {'mode': 'use_confirmed_text'}}),
    ('revise', confirmed('fabricated speech')),
    ('revise', confirmed('   ')),
    ('revise', {'dialogue': {'mode': 'use_confirmed_text'}, 'semantic': [{'id': 'speech', 'text': '欢迎来到新世界。'}]}),
])
def test_ambiguous_missing_or_forged_text_never_commits(tmp_path, intent, requirements):
    store, _, _ = setup_plan_pending(tmp_path)
    old = deepcopy(list_director_artifacts(store, 'c', 'u')['commissions'][0])
    scope, msg = message(tmp_path, '请把口播改为「欢迎来到新世界。」')
    assert amendment(store, scope, msg, requirements, intent=intent) is None
    assert list_director_artifacts(store, 'c', 'u')['commissions'][0] == old


def test_missing_replacement_text_cannot_reuse_old_locked_turns(tmp_path):
    store, _, _ = setup_plan_pending(tmp_path)
    scope, msg = message(tmp_path, '请使用口播「Original speech.」')
    first = amendment(store, scope, msg, confirmed('Original speech.'))
    assert first is not None
    saved = deepcopy(list_director_artifacts(store, 'c', 'u')['commissions'][0])
    scope, msg = message(tmp_path, '请把口播改为「Different speech.」')
    assert amendment(store, scope, msg, {'dialogue': {'mode': 'use_confirmed_text'}}) is None
    assert list_director_artifacts(store, 'c', 'u')['commissions'][0] == saved


@pytest.mark.parametrize('mode', ['rewrite', 'unspecified', 'preserve_source'])
def test_other_modes_do_not_accept_confirmed_text(tmp_path, mode):
    store, _, _ = setup_plan_pending(tmp_path)
    scope, msg = message(tmp_path, '欢迎来到新世界。')
    requirements = confirmed(msg['content'])
    requirements['dialogue']['mode'] = mode
    assert amendment(store, scope, msg, requirements) is None
    assert amendment(store, scope, msg, {'dialogue': {'mode': mode}}) is not None


def test_direction_approval_with_spoken_text_is_resolvable(tmp_path):
    store, d = setup_pending(tmp_path)
    scope, msg = message(tmp_path, '可以，口播改为「欢迎来到新世界。」')
    result = apply(store, scope, msg, expected_decision_ref=d['decision_ref'],
        interpretation={'intent': 'approve_with_changes', 'requirements': confirmed('欢迎来到新世界。')})
    assert result is not None
    assert resolve_production_requirements(store, commission=result['commission'])['locked_turns'] == [
        {'speaker': '', 'text': '欢迎来到新世界。'}]


def test_unrelated_requirement_retains_explicitly_unchanged_speech(tmp_path):
    store, _, _ = setup_plan_pending(tmp_path)
    scope, msg = message(tmp_path, '请使用口播「Original speech.」')
    assert amendment(store, scope, msg, confirmed('Original speech.')) is not None
    scope, msg = message(tmp_path, '请改为三个镜头，口播不变。')
    result = amendment(store, scope, msg, {**confirmed('Original speech.'), 'primary_shots': {'min': 3, 'max': 3}})
    assert resolve_production_requirements(store, commission=result['commission'])['locked_turns'] == [
        {'speaker': '', 'text': 'Original speech.'}]


def test_host_transition_rejects_forged_or_missing_spoken_evidence(tmp_path):
    from novelvideo.freezone.remix.reference_commission import AuthenticatedDecisionEvidence, advance_reference_commission
    store, d = setup_pending(tmp_path)
    ref = d['decision']['commission_ref']
    for requirements, content in ((confirmed('Forged'), 'Different'),
                                  (confirmed('Forged'), None),
                                  ({'dialogue': {'mode': 'use_confirmed_text'}}, 'Anything')):
        with pytest.raises(ValueError, match='confirmed'):
            advance_reference_commission(store, ref,
                {'type': 'direction_confirmed', 'direction': {'requirements': requirements,
                    'locked_turns': [{'speaker': '', 'text': 'Old'}]}}, ref['revision'],
                host_evidence=AuthenticatedDecisionEvidence('p', 'c', 'u', 'host-message', content))
        assert store.read_latest(ref['artifact_id'])['revision'] == ref['revision']


def test_pending_plan_amendment_uses_same_confirmed_text_path(tmp_path):
    store, decision, plan_ref = setup_plan_pending(tmp_path)
    scope, msg = message(tmp_path, '请把口播改为「欢迎来到新世界。」')
    result = apply(store, scope, msg, expected_decision_ref=decision['decision_ref'],
        canvas={'project_id': 'p', 'canvas_id': 'c', 'nodes': [{'data': {'productionPlanV2Ref': plan_ref}}]},
        interpretation={'intent': 'revise', 'requirements': confirmed('欢迎来到新世界。')})
    assert result is not None
    assert resolve_production_requirements(store, commission=result['commission'])['locked_turns'] == [
        {'speaker': '', 'text': '欢迎来到新世界。'}]


def test_manual_input_cannot_disagree_with_typed_confirmed_text(tmp_path):
    from novelvideo.freezone.remix.production_requirements import validate_native_requirements
    from novelvideo.freezone.remix.store import RemixArtifactStore
    store = RemixArtifactStore(tmp_path, 'p', created_by='u')
    with pytest.raises(ValueError, match='confirmed'):
        validate_native_requirements(store, {'requirements': confirmed('New'),
            'locked_turns': [{'speaker': '', 'text': 'Old'}]})


def confirmed_lines(*texts):
    return {'dialogue': {'mode': 'use_confirmed_text',
        'confirmed_turns': [{'speaker': '', 'text': text} for text in texts]}}


@pytest.mark.parametrize('entry', ['chat', 'host'])
@pytest.mark.parametrize('before,after,content,accepted', [
    (('Hello world.', 'Goodbye world.'), ('Hello world.', 'See you tomorrow.'),
     '第一句不变，第二句改为「See you tomorrow.」', True),
    (('Hello world.', 'Goodbye world.'), ('Hello world.', 'Forged replacement.'),
     '第一句不变，第二句改为「See you tomorrow.」', False),
    (('Hello world.', 'Goodbye world.'), ('Goodbye world.',), '删除第一句。', True),
    (('Hello world.', 'Goodbye world.'), ('Goodbye world.', 'Hello world.'), '调整一下。', False),
    (('Hello world.', 'Goodbye world.'), ('Goodbye world.', 'Hello world.'),
     '最后加一句「Hello world.」，其余按新顺序。', True),
    (('Again.', 'Again.'), ('Again.', 'Changed.'), '第二句改为「Changed.」', True),
    (('Again.', 'Again.'), ('Again.',), '删除一句。', True),
    (('Again.', 'Again.'), ('Again.', 'Again.', 'Again.'), '调整一下。', False),
    (('Again.', 'Again.'), ('Again.', 'Again.', 'Again.'), '再加一句「Again.」', True),
    (('Again.', 'Again.'), ('Again.', 'Again.', 'Again.', 'Again.'), '再加一句「Again.」', False),
    (('Old.',), ('New second.', 'New first.'), '改为「New first.」然后「New second.」', False),
])
def test_partial_turn_provenance_preserves_order_and_occurrence_counts(
        tmp_path, entry, before, after, content, accepted):
    from novelvideo.freezone.remix.production_requirements import apply_requirement_amendment
    from novelvideo.freezone.remix.reference_commission import AuthenticatedDecisionEvidence

    store, _, _ = setup_plan_pending(tmp_path)
    scope, msg = message(tmp_path, '请使用口播：' + ' '.join(before))
    initial = amendment(store, scope, msg, confirmed_lines(*before))
    assert initial is not None
    saved = deepcopy(list_director_artifacts(store, 'c', 'u')['commissions'][0])
    scope, msg = message(tmp_path, content)
    requirements = confirmed_lines(*after)
    if entry == 'chat':
        result = amendment(store, scope, msg, requirements)
        assert (result is not None) == accepted
        changed = result['commission'] if result else None
    else:
        def direct():
            return apply_requirement_amendment(store, commission_ref=saved['commission_ref'],
                requirements=requirements,
                canvas={'project_id': 'p', 'canvas_id': 'c', 'nodes': [
                    {'data': {'productionPlanV2Ref': saved['plan_ref']}}]},
                host_evidence=AuthenticatedDecisionEvidence('p', 'c', 'u', f'host-message:{msg["id"]}', content))
        if accepted:
            changed = direct()
        else:
            with pytest.raises(ValueError, match='confirmed'):
                direct()
            changed = None
    if changed:
        assert resolve_production_requirements(store, commission=changed)['locked_turns'] == requirements['dialogue']['confirmed_turns']
        assert changed['plan_ref'] == saved['plan_ref']
    else:
        assert list_director_artifacts(store, 'c', 'u')['commissions'][0] == saved


def test_changing_retained_speaker_requires_current_message_evidence(tmp_path):
    store, _, _ = setup_plan_pending(tmp_path)
    scope, msg = message(tmp_path, '请使用口播「Hello world.」「Goodbye world.」')
    assert amendment(store, scope, msg, confirmed_lines('Hello world.', 'Goodbye world.'))
    requirements = confirmed_lines('Hello world.', 'Goodbye world.')
    requirements['dialogue']['confirmed_turns'][0]['speaker'] = 'Forged speaker'
    scope, msg = message(tmp_path, '第一句换一个说话人。')
    assert amendment(store, scope, msg, requirements) is None
    scope, msg = message(tmp_path, '第一句由 Forged speaker 说「Hello world.」，其余不变。')
    assert amendment(store, scope, msg, requirements) is not None


@pytest.mark.parametrize('entry', ['chat', 'host'])
@pytest.mark.parametrize('before,after,content,accepted', [
    (('Hello world.', 'See you tomorrow.'), ('See you tomorrow.', 'Hello world.'),
     '把「See you tomorrow.」移到第一句，另一句保持不变。', True),
    (('Hello world.', 'See you tomorrow.'), ('See you tomorrow.', 'Hello world.'),
     '把「Hello world.」移到最后一句，另一句保持不变。', True),
    (('A.', 'B.', 'C.'), ('C.', 'A.', 'B.'), '把「C.」移到第一句。', True),
    (('A.', 'B.', 'C.'), ('B.', 'C.', 'A.'), '把「A.」移到最后一句。', True),
    (('A.', 'B.', 'A.'), ('B.', 'A.', 'A.'), '把「B.」移到第一句。', True),
    (('A.', 'B.', 'A.'), ('B.', 'A.', 'A.', 'A.'), '把「B.」移到第一句。', False),
    (('A.', 'B.'), ('B.', 'A.'), '把「Forged.」移到第一句。', False),
    (('A.', 'B.'), ('B.', 'A.', 'C.'), '把「B.」移到第一句。', False),
])
def test_moved_turn_can_use_current_evidence_before_retained_turns(
        tmp_path, entry, before, after, content, accepted):
    test_partial_turn_provenance_preserves_order_and_occurrence_counts(
        tmp_path, entry, before, after, content, accepted)


@pytest.mark.parametrize('entry', ['chat', 'host'])
def test_confirmed_speech_message_evidence_has_bounded_input(tmp_path, entry):
    test_partial_turn_provenance_preserves_order_and_occurrence_counts(tmp_path, entry,
        ('Old.',), ('New.',), 'New.' + ' ' * 200_000, False)


@pytest.mark.parametrize('entry', ['chat', 'host'])
@pytest.mark.parametrize('current_count,accepted', [(40, True), (39, False)])
def test_duplicate_allocation_at_turn_limit_is_bounded(tmp_path, entry, current_count, accepted):
    test_partial_turn_provenance_preserves_order_and_occurrence_counts(tmp_path, entry,
        ('Again.',) * 40, ('Again.',) * 80, ' '.join(['Again.'] * current_count), accepted)


@pytest.mark.parametrize('entry', ['chat', 'host'])
def test_confirmed_turn_count_limit_is_enforced(tmp_path, entry):
    test_partial_turn_provenance_preserves_order_and_occurrence_counts(tmp_path, entry,
        ('Again.',), ('Again.',) * 81, ' '.join(['Again.'] * 81), False)
