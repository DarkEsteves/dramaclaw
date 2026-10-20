"""Adoption reuses full-source observations after real material acceptance."""
from copy import deepcopy
import hashlib
import pytest

from test_material_work import plan_payload
from test_source_speech import SCOPE, resource
from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.freezone.remix.source_speech import observe_source_speech


def setup(tmp_path, *, accepted=True, segment=False, mismatch=False, trim=False, single_turn=False):
    from novelvideo.freezone.remix.material_work import prepare_material_work, accept_material_candidate
    store = RemixArtifactStore(tmp_path, 'project', created_by='alice')
    media = {**resource(tmp_path), 'id': 'voice', 'node_id': 'voice-node', 'media_kind': 'audio'}
    plan = plan_payload()
    plan['beats'][0]['timing_mode'] = 'auto'
    plan['beats'][0]['turns'] = [{'id': 'one', 'speaker': 'A', 'text': 'one'}, {'id': 'two', 'speaker': 'B', 'text': 'two'}]
    plan['beats'].append({'id': 'after', 'purpose': 'after', 'duration_frames': 24, 'turns': []})
    plan['resources'] = [media]
    plan['placements'].append({'id': 'speech', 'kind': 'audio', 'role': 'speech', 'track_id': 'speech', 'resource_id': 'voice',
        'start': {'kind': 'beat', 'ref_id': 'opening', 'edge': 'start'}, 'end': {'kind': 'beat', 'ref_id': 'opening', 'edge': 'end'}})
    plan['speech_takes'] = [{'id': 'take', 'beat_id': 'opening', 'resource_id': 'voice', 'origin_placement_id': 'speech', 'turn_ids': ['one', 'two']}]
    if single_turn:
        plan['beats'][0]['turns'] = [{'id': 'one', 'speaker': 'A', 'text': single_turn if isinstance(single_turn, str) else 'one two'}]
        plan['speech_takes'][0]['turn_ids'] = ['one']
    if trim:
        plan['beats'][0]['turns'] = plan['beats'][0]['turns'][:1]
        plan['speech_takes'][0].update(turn_ids=['one'], source_out_us=1_000_000)
    saved = store.create('production_plan.v2', plan, [])
    pref = {'artifact_id': saved['id'], 'revision': 1}
    prepared = prepare_material_work(store, pref,
        [{'id': 'dialogue', 'take_ids': ['take'], 'turn_ids': plan['speech_takes'][0]['turn_ids'], 'description': 'dialogue', 'media_kind': 'audio', 'final_step_id': 'voice'}],
        [{'id': 'voice', 'label': 'voice', 'source': 'existing', 'node_id': 'voice-node', 'media_kind': 'audio'}], 'prepare')
    wref = prepared['work_ref']
    if accepted:
        decision = {'requester_user_id': 'alice', 'canvas_id': 'canvas', 'project_id': 'project', 'media_hash': media['media_hash']}
        review = store.create('material_review.v1', {'resource': media, 'step_id': 'voice', 'work_ref': wref,
            'technical': {'status': 'passed'}, 'content': {'status': 'passed'}, 'actor_kind': 'user',
            'decision_receipt_id': hashlib.sha256(store._canonical(decision)).hexdigest(), 'user_decision': decision}, [wref])
        wref = accept_material_candidate(store, wref, 'voice', media, {'artifact_id': review['id'], 'revision': 1}, 'accept')['work_ref']
    calls = []
    class Engine:
        def transcribe(self, path, language):
            calls.append(path.read_bytes())
            words = [{'text': 'won' if mismatch else 'one', 'start': .1, 'end': 1.0}, {'text': 'two', 'start': 3.2, 'end': 4.1}]
            if single_turn == 'one one two':
                words.insert(1, {'text': 'one', 'start': 1.4, 'end': 1.9})
            return {'status': 'completed', 'transcript': single_turn if isinstance(single_turn, str) else 'one two', 'words': [] if segment else words,
                    'segments': [{'text': 'one two', 'start': .1, 'end': 4.1}] if segment else []}
    observation = observe_source_speech(store, scope=SCOPE, resource=media, language='en', provider_signature='test', aligner=Engine())
    return store, pref, wref, observation, calls


def adopt(store, pref, wref, observation, command='adopt', **kwargs):
    from novelvideo.freezone.remix.source_speech_adoption import adopt_source_observation
    return adopt_source_observation(store, plan_ref=pref, take_id='take', observation_ref=observation,
        work_ref=wref, scope=SCOPE, command_id=command, requester_user_id='alice', **kwargs)


def test_unaccepted_recognition_cannot_become_formal_timing(tmp_path):
    store, pref, wref, obs, _ = setup(tmp_path, accepted=False)
    with pytest.raises(ValueError, match='accepted|acceptance'):
        adopt(store, pref, wref, obs)
    assert store.read_latest(pref['artifact_id'])['revision'] == 1


def test_adoption_keeps_measured_words_pauses_one_playback_and_reflows_following_beat(tmp_path):
    from novelvideo.freezone.remix.production_plan_v2 import ProductionPlanV2
    from novelvideo.freezone.remix.production_timing import project_speech_mainline
    store, pref, wref, obs, calls = setup(tmp_path)
    result = adopt(store, pref, wref, obs)
    assert len(calls) == 1
    saved = store.read(result['plan_ref']['artifact_id'], result['plan_ref']['revision'])
    words = [w for t in saved['payload']['beats'][0]['turns'] for w in t['speech_timing']['words']]
    raw = store.read(obs['artifact_id'], obs['revision'])['payload']['recognized_words']
    assert [(w['id'], w['start_us'], w['end_us']) for w in words] == [('take:' + w['id'], w['start_us'], w['end_us']) for w in raw]
    projection = project_speech_mainline(ProductionPlanV2.model_validate(saved['payload']))
    assert len(projection['speech_takes']) == 1
    assert projection['beat_ranges']['opening'] == (0, 98)
    assert projection['beat_ranges']['after'] == (98, 122)
    assert projection['speech_cues'][1]['start_frame'] == 77
    assert adopt(store, pref, wref, obs) == result
    assert 'audio_reviewed' not in saved['payload']


def test_replacement_or_scope_mismatch_cannot_reuse_observation(tmp_path):
    store, pref, wref, obs, _ = setup(tmp_path)
    (tmp_path / 'candidate.wav').write_bytes(b'replaced')
    with pytest.raises(ValueError, match='changed'):
        adopt(store, pref, wref, obs)


def test_segment_only_and_wrong_text_do_not_fabricate_formal_word_timing(tmp_path):
    store, pref, wref, obs, _ = setup(tmp_path, segment=True)
    with pytest.raises(ValueError, match='word timing'):
        adopt(store, pref, wref, obs)
    assert store.read_latest(pref['artifact_id'])['revision'] == 1


def test_target_change_rematches_same_observation_without_recognition(tmp_path):
    store, pref, wref, obs, calls = setup(tmp_path)
    saved = store.read(pref['artifact_id'], 1)
    changed = deepcopy(saved['payload'])
    changed['beats'][0]['turns'][0]['text'] = 'different'
    new = store.revise(saved['id'], 1, changed, [])
    with pytest.raises(ValueError, match='changed|differs'):
        adopt(store, {'artifact_id': new['id'], 'revision': 2}, wref, obs)
    assert len(calls) == 1


def test_new_producer_marker_alone_cannot_authorize_fabricated_words(tmp_path):
    store, pref, wref, obs, _ = setup(tmp_path)
    result = adopt(store, pref, wref, obs)
    timeline = store.read(result['timeline_refs'][0]['artifact_id'], 1)
    forged = deepcopy(timeline['payload'])
    forged['words'][0]['end_us'] += 100_000
    producer = RemixArtifactStore(tmp_path, 'project', created_by='alice', producer_version='remix-adopt-source-speech.v1')
    with pytest.raises(ValueError, match='observation|measured|derived'):
        producer.create('word_timeline.v1', forged, timeline['upstream_refs'])


def correction(store, pref, wref, obs):
    from novelvideo.freezone.remix.candidate_speech_correction import PRODUCER
    from novelvideo.freezone.remix.speech_review_summary import build_speech_review_summary
    raw = store.read(obs['artifact_id'], 1)['payload']
    turns = [{k: t[k] for k in ('id', 'speaker', 'text')} for t in store.read(pref['artifact_id'], pref['revision'])['payload']['beats'][0]['turns']]
    summary = build_speech_review_summary(raw, target_turns=turns, observation_ref=obs)
    words = deepcopy(raw['recognized_words'])
    words[0]['text'] = 'one'
    payload = {'schema_version': 1, 'authorization_scope': SCOPE, 'observation_ref': obs, 'work_ref': wref,
        'plan_ref': pref, 'step_id': 'voice', 'node_id': 'voice-node', 'resource_signature': raw['resource_signature'],
        'target_signature': summary['target_signature'], 'target_turns': turns,
        'corrections': [{'word_id': words[0]['id'], 'text': 'one', 'reason': 'listened'}], 'corrected_words': words,
        'actor_kind': 'user', 'actor_id': 'alice', 'command_id': 'correct'}
    payload['decision_receipt'] = hashlib.sha256(store._canonical(payload)).hexdigest()
    saved = RemixArtifactStore(store.project_dir, 'project', created_by='alice', producer_version=PRODUCER).create(
        'candidate_speech_correction.v1', payload, [pref, wref, obs])
    return {'artifact_id': saved['id'], 'revision': 1}


def test_authenticated_candidate_correction_preserves_real_word_times(tmp_path):
    store, pref, wref, obs, calls = setup(tmp_path, mismatch=True)
    cref = correction(store, pref, wref, obs)
    result = adopt(store, pref, wref, obs, correction_ref=cref)
    timeline = store.read(result['timeline_refs'][0]['artifact_id'], 1)
    assert timeline['payload']['words'][0]['text'] == 'one'
    assert timeline['payload']['words'][0]['end_us'] == 1_000_000
    assert timeline['payload']['correction_ref'] == cref
    assert len(calls) == 1
    assert store.read(obs['artifact_id'], 1)['payload']['recognized_words'][0]['text'] == 'won'


def test_adoption_recovers_after_plan_publication_failure_without_asr(tmp_path, monkeypatch):
    store, pref, wref, obs, calls = setup(tmp_path)
    original = RemixArtifactStore._publish
    failures = []
    def fail_after_plan(self, **kwargs):
        saved = original(self, **kwargs)
        if kwargs['schema'] == 'production_plan.v2' and not failures:
            failures.append(1)
            raise OSError('after plan publication')
        return saved
    monkeypatch.setattr(RemixArtifactStore, '_publish', fail_after_plan)
    with pytest.raises(OSError):
        adopt(store, pref, wref, obs)
    result = adopt(store, pref, wref, obs)
    assert result['plan_ref']['revision'] == 2
    assert store.read_latest(pref['artifact_id'])['revision'] == 2
    assert len(calls) == 1


def test_native_adoption_uses_host_scope_and_is_registered_on_speech_lane(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from novelvideo.freezone.remix.source_speech_adoption import prepare_source_observation_adoption
    import novelvideo.task_backend.runners.remix  # noqa: F401 - register native task
    from novelvideo.task_backend.registry import get_project_task_runner, project_task_lane
    store, pref, wref, obs, _ = setup(tmp_path)
    ctx = SimpleNamespace(project_id='project', output_dir=tmp_path, owner_id='owner', owner_type='user',
        requester_user_id='alice', is_home_node=True, effective_role='editor')
    canvas = {'project_id': 'project', 'access_model': 'shared', 'nodes': [{'id': 'plan', 'type': 'scriptNode',
        'data': {'productionPlanV2Ref': {'artifactId': pref['artifact_id'], 'revision': 1}}}]}
    monkeypatch.setattr('novelvideo.freezone.remix.material_execution.canvas_material_resource',
        lambda *args: store.read(pref['artifact_id'], pref['revision'])['payload']['resources'][0])
    prepared = prepare_source_observation_adoption(store, ctx, canvas, canvas_id='canvas',
        plan_ref=pref, take_id='take', observation_ref=obs, work_ref=wref)
    assert prepared['task_payload']['scope'] == SCOPE
    assert prepared['producer_version'] == 'remix-adopt-source-speech.v1'
    assert get_project_task_runner('remix_adopt_source_speech')
    assert project_task_lane('remix_adopt_source_speech') == 'default'


def test_native_adoption_recovers_exact_result_after_notification_failure(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.remix.source_speech_adoption import prepare_source_observation_adoption
    from novelvideo.task_backend.runners.remix import run_remix_adopt_source_speech
    store, pref, wref, obs, calls = setup(tmp_path)
    ctx = SimpleNamespace(project_id='project', output_dir=tmp_path, state_dir=tmp_path / 'state', owner_id='owner', owner_type='user',
        requester_user_id='alice', is_home_node=True, effective_role='editor')
    canvas = {'project_id': 'project', 'access_model': 'shared', 'nodes': [{'id': 'plan', 'type': 'scriptNode',
        'data': {'productionPlanV2Ref': {'artifactId': pref['artifact_id'], 'revision': 1}}}]}
    monkeypatch.setattr('novelvideo.freezone.canvas_store.read_canvas', lambda *args: canvas)
    monkeypatch.setattr('novelvideo.freezone.remix.material_execution.canvas_material_resource',
        lambda *args: store.read(pref['artifact_id'], 1)['payload']['resources'][0])
    prepared = prepare_source_observation_adoption(store, ctx, canvas, canvas_id='canvas',
        plan_ref=pref, take_id='take', observation_ref=obs, work_ref=wref)
    operations = RemixOperationStore(tmp_path, 'project')
    operation = operations.start(idempotency_key='native-adopt', input_refs=prepared['input_refs'],
        producer_version=prepared['producer_version'], normalized_parameters=prepared['normalized_parameters'], node_revision=1)
    fence = operations.claim_submission(operation['operation_id'], owner='submitter')
    operations.bind_task(operation['operation_id'], task_id='native-task', owner='submitter', fencing_token=fence)
    envelope = {'payload': {**prepared['task_payload'], 'operation_id': operation['operation_id']}, '__run_task_id': 'native-task'}
    from contextlib import contextmanager
    locks = []
    artifact_lock = RemixArtifactStore._locked
    operation_get = RemixOperationStore.get
    @contextmanager
    def tracked_lock(self):
        with artifact_lock(self) as guard:
            locks.append(True)
            try:
                yield guard
            finally:
                locks.pop()
    def no_inverted_lock(self, *args):
        if locks:
            pytest.fail('artifact -> operation mutex order can deadlock operation -> artifact result publication')
        return operation_get(self, *args)
    monkeypatch.setattr(RemixArtifactStore, '_locked', tracked_lock)
    monkeypatch.setattr(RemixOperationStore, 'get', no_inverted_lock)
    original = RemixOperationStore.publish_result_unverified
    failures = []
    def fail_notification(self, *args, **kwargs):
        if not failures:
            failures.append(1)
            raise OSError('notification unavailable')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(RemixOperationStore, 'publish_result_unverified', fail_notification)
    with pytest.raises(OSError):
        run_remix_adopt_source_speech(envelope, ctx)
    result = run_remix_adopt_source_speech(envelope, ctx)
    assert result['original_plan_ref'] == pref
    assert result['plan_ref']['revision'] == 2
    assert len(calls) == 1
    assert operations.get(operation['operation_id'])['output_refs'][0]['revision'] == 2
    assert canvas['nodes'][0]['data']['productionPlanV2Ref']['revision'] == 1
    operations.claim_confirmation(operation['operation_id'], canvas_id='canvas', node_id='plan')
    operations.finish_confirmation(operation['operation_id'])
    assert run_remix_adopt_source_speech(envelope, ctx) == result
    assert operations.get(operation['operation_id'])['status'] == 'completed'


def test_turn_edit_invalidates_only_changed_take_and_returns_unmapped_word_anchor(tmp_path):
    from novelvideo.freezone.remix.production_commands import apply_production_commands
    store, pref, wref, obs, _ = setup(tmp_path)
    result = adopt(store, pref, wref, obs)
    saved = store.read(result['plan_ref']['artifact_id'], 2)
    plan = deepcopy(saved['payload'])
    # An independent picture remains accepted and its exact material work persists.
    plan['placements'][0]['content'] = None
    original_work = store.read(wref['artifact_id'], wref['revision'])
    outcome = apply_production_commands(store, result['plan_ref'], [{'kind': 'edit_turn', 'target_id': 'one',
        'payload': {'text': 'changed', 'invalidate_speech_timing': True}}], 'edit')
    changed = store.read(outcome['plan_ref']['artifact_id'], outcome['plan_ref']['revision'])
    assert all(t.get('speech_timing') is None for t in changed['payload']['beats'][0]['turns'])
    assert store.read(wref['artifact_id'], wref['revision']) == original_work


def test_observation_change_requires_explicit_word_anchor_remapping(tmp_path):
    from novelvideo.freezone.remix.source_speech_adoption import SourceSpeechRemapRequired
    store, pref, wref, obs, _ = setup(tmp_path)
    result = adopt(store, pref, wref, obs)
    saved = store.read(result['plan_ref']['artifact_id'], 2)
    plan = deepcopy(saved['payload'])
    word = plan['beats'][0]['turns'][0]['speech_timing']['words'][0]['id']
    plan['placements'][0]['start'] = {'kind': 'word', 'ref_id': word, 'edge': 'start'}
    revised = store.revise(saved['id'], 2, plan, saved['upstream_refs'])
    current_ref = {'artifact_id': revised['id'], 'revision': revised['revision']}
    class NewEngine:
        def transcribe(self, path, language):
            return {'status': 'completed', 'transcript': 'one two', 'words': [
                {'text': 'one', 'start': .1, 'end': 1.0}, {'text': 'two', 'start': 3.2, 'end': 4.1}]}
    source = revised['payload']['resources'][0]
    other = observe_source_speech(store, scope=SCOPE, resource=source, language='en', provider_signature='new', aligner=NewEngine())
    with pytest.raises(SourceSpeechRemapRequired) as caught:
        adopt(store, current_ref, wref, other, command='new-observation')
    assert caught.value.targets == [{'entity_kind': 'placement', 'entity_id': 'picture', 'edge': 'start', 'word_id': word}]
    assert store.read_latest(saved['id'])['revision'] == 3


def test_shared_observation_has_distinct_word_anchors_for_distinct_playback_takes(tmp_path):
    from novelvideo.freezone.remix.material_work import prepare_material_work, accept_material_candidate
    from novelvideo.freezone.remix.source_speech_adoption import adopt_source_observation
    store, pref, wref, obs, calls = setup(tmp_path)
    first = adopt(store, pref, wref, obs)
    saved = store.read(first['plan_ref']['artifact_id'], 2)
    plan = deepcopy(saved['payload'])
    plan['beats'][1]['turns'] = [{'id': 'again', 'speaker': 'A', 'text': 'one two'}]
    plan['placements'].append({**plan['placements'][1], 'id': 'speech2',
        'start': {'kind': 'beat', 'ref_id': 'after', 'edge': 'start'}, 'end': {'kind': 'beat', 'ref_id': 'after', 'edge': 'end'}})
    plan['speech_takes'].append({**plan['speech_takes'][0], 'id': 'take2', 'beat_id': 'after', 'turn_ids': ['again'], 'origin_placement_id': 'speech2'})
    revised = store.revise(saved['id'], 2, plan, saved['upstream_refs'])
    pref2 = {'artifact_id': revised['id'], 'revision': revised['revision']}
    second_work = prepare_material_work(store, pref2,
        [{'id': 'second', 'take_ids': ['take2'], 'turn_ids': ['again'], 'description': 'replay', 'media_kind': 'audio', 'final_step_id': 'second'}],
        [{'id': 'second', 'label': 'replay', 'source': 'existing', 'node_id': 'voice-node', 'media_kind': 'audio'}], 'second-work')['work_ref']
    media = revised['payload']['resources'][0]
    decision = {'requester_user_id': 'alice', 'canvas_id': 'canvas', 'project_id': 'project', 'media_hash': media['media_hash']}
    review = store.create('material_review.v1', {'resource': media, 'step_id': 'second', 'work_ref': second_work,
        'technical': {'status': 'passed'}, 'content': {'status': 'passed'}, 'actor_kind': 'user',
        'decision_receipt_id': hashlib.sha256(store._canonical(decision)).hexdigest(), 'user_decision': decision}, [second_work])
    accepted = accept_material_candidate(store, second_work, 'second', media, {'artifact_id': review['id'], 'revision': 1}, 'second-accept')['work_ref']
    result = adopt_source_observation(store, plan_ref=pref2, take_id='take2', observation_ref=obs, work_ref=accepted,
        scope=SCOPE, requester_user_id='alice', command_id='second-adopt')
    after = store.read(result['plan_ref']['artifact_id'], result['plan_ref']['revision'])['payload']
    first_words = {w['id'] for t in after['beats'][0]['turns'] for w in t['speech_timing']['words']}
    second_words = {w['id'] for w in after['beats'][1]['turns'][0]['speech_timing']['words']}
    assert not first_words & second_words
    timeline = store.read(result['timeline_refs'][0]['artifact_id'], 1)['payload']
    raw_ids = {w['id'] for w in store.read(obs['artifact_id'], 1)['payload']['recognized_words']}
    assert set(timeline['source_word_ids'].values()) == raw_ids
    assert len(calls) == 1


def test_formal_adoption_replaces_previous_program_word_windows(tmp_path):
    store, pref, wref, obs, _ = setup(tmp_path)
    saved = store.read(pref['artifact_id'], 1)
    plan = deepcopy(saved['payload'])
    plan['beats'][0]['turns'][0].update(start_frame=0, end_frame=24, timing_basis='manual')
    plan['beats'][0]['turns'][0]['words'] = [{'id': 'estimated-word', 'text': 'one', 'start_frame': 0, 'end_frame': 20}]
    revised = store.revise(saved['id'], 1, plan, [])
    pref = {'artifact_id': revised['id'], 'revision': revised['revision']}
    result = adopt(store, pref, wref, obs)
    turn = store.read(result['plan_ref']['artifact_id'], result['plan_ref']['revision'])['payload']['beats'][0]['turns'][0]
    assert turn['words'] == []
    assert 'start_frame' not in turn
    assert turn['speech_timing']['words'][0]['start_us'] == 100_000


def test_explicit_take_crop_reuses_complete_observation_without_cutting_recognition(tmp_path):
    store, pref, wref, obs, calls = setup(tmp_path, trim=True)
    result = adopt(store, pref, wref, obs)
    timeline = store.read(result['timeline_refs'][0]['artifact_id'], 1)['payload']
    assert timeline['source_out_us'] == 4_100_000
    assert [w['text'] for w in timeline['words']] == ['one']
    assert store.read(obs['artifact_id'], 1)['payload']['recognized_words'][-1]['end_us'] == 4_100_000
    assert len(calls) == 1


def test_raw_text_mismatch_requires_real_candidate_correction(tmp_path):
    store, pref, wref, obs, _ = setup(tmp_path, mismatch=True)
    with pytest.raises(ValueError, match='differs'):
        adopt(store, pref, wref, obs)
    assert store.read_latest(pref['artifact_id'])['revision'] == 1


def test_adoption_can_reuse_explicit_artifact_guard_without_recursive_lock(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.source_speech_adoption import adopt_source_observation
    store, pref, wref, obs, _ = setup(tmp_path)
    with store._locked() as guard:
        def no_nested_lock(*args):
            pytest.fail('non-reentrant artifact mutex acquired recursively')
        monkeypatch.setattr(RemixArtifactStore, '_locked', no_nested_lock)
        result = adopt_source_observation(store, plan_ref=pref, take_id='take', observation_ref=obs,
            work_ref=wref, scope=SCOPE, command_id='held-guard', requester_user_id='alice', _artifact_guard=guard)
        assert result['plan_ref']['revision'] == 2


def test_adopted_plan_cannot_relabel_immutable_source_word_identity(tmp_path):
    store, pref, wref, obs, _ = setup(tmp_path)
    result = adopt(store, pref, wref, obs)
    saved = store.read(result['plan_ref']['artifact_id'], 2)
    plan = deepcopy(saved['payload'])
    plan['beats'][0]['turns'][0]['speech_timing']['words'][0]['id'] = 'invented-id'
    plan['speech_cues'][0]['word_ids'] = ['invented-id']
    with pytest.raises(ValueError, match='source word identit'):
        store.revise(saved['id'], 2, plan, saved['upstream_refs'])


def test_adopted_cue_cannot_claim_text_absent_from_measured_words(tmp_path):
    store, pref, wref, obs, _ = setup(tmp_path)
    result = adopt(store, pref, wref, obs)
    saved = store.read(result['plan_ref']['artifact_id'], 2)
    plan = deepcopy(saved['payload'])
    plan['speech_cues'][0]['text'] = 'invented words'
    with pytest.raises(ValueError, match='cue text'):
        store.revise(saved['id'], 2, plan, saved['upstream_refs'])


@pytest.mark.parametrize('phase', ['notification', 'plan', 'receipt', 'partial_timelines'])
@pytest.mark.parametrize('failure_kind', ['os', 'busy', 'lease'])
def test_registered_runner_keeps_committed_result_reconcilable(tmp_path, monkeypatch, phase, failure_kind):
    from types import SimpleNamespace
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.remix.source_speech_adoption import prepare_source_observation_adoption, reconcile_source_speech_adoption
    from novelvideo.task_backend.registry import get_project_task_runner
    import novelvideo.task_backend.runners.remix  # noqa: F401 - register runner
    store, pref, wref, obs, calls = setup(tmp_path)
    ctx = SimpleNamespace(project_id='project', output_dir=tmp_path, state_dir=tmp_path / 'state', owner_id='owner', owner_type='user',
        requester_user_id='alice', is_home_node=True, effective_role='editor')
    canvas = {'project_id': 'project', 'access_model': 'shared', 'nodes': [{'id': 'plan', 'type': 'scriptNode',
        'data': {'productionPlanV2Ref': {'artifactId': pref['artifact_id'], 'revision': 1}}}]}
    monkeypatch.setattr('novelvideo.freezone.canvas_store.read_canvas', lambda *args: canvas)
    monkeypatch.setattr('novelvideo.freezone.remix.material_execution.canvas_material_resource',
        lambda *args: store.read(pref['artifact_id'], 1)['payload']['resources'][0])
    prepared = prepare_source_observation_adoption(store, ctx, canvas, canvas_id='canvas',
        plan_ref=pref, take_id='take', observation_ref=obs, work_ref=wref)
    operations = RemixOperationStore(tmp_path, 'project')
    operation = operations.start(idempotency_key='recover', input_refs=prepared['input_refs'],
        producer_version=prepared['producer_version'], normalized_parameters=prepared['normalized_parameters'], node_revision=1)
    fence = operations.claim_submission(operation['operation_id'], owner='submitter')
    operations.bind_task(operation['operation_id'], task_id='task', owner='submitter', fencing_token=fence)
    original = RemixOperationStore.publish_result_unverified
    failures = []
    from novelvideo.freezone.canvas_lock import CanvasLockBusy
    from novelvideo.ports.canvas_mutex import CanvasLeaseLost
    failure = OSError('durable publication unavailable') if failure_kind == 'os' else (CanvasLockBusy('canvas') if failure_kind == 'busy' else CanvasLeaseLost('canvas'))
    def fail_notify(self, *args, **kwargs):
        if not failures:
            failures.append(1)
            raise failure
        return original(self, *args, **kwargs)
    if phase == 'notification':
        monkeypatch.setattr(RemixOperationStore, 'publish_result_unverified', fail_notify)
    elif phase == 'receipt':
        atomic = RemixArtifactStore._atomic_json
        def fail_receipt(self, path, value, guard):
            if path.parent.name == '_source_speech_adoptions' and not failures:
                failures.append(1)
                raise failure
            return atomic(self, path, value, guard)
        monkeypatch.setattr(RemixArtifactStore, '_atomic_json', fail_receipt)
    else:
        publish = RemixArtifactStore._publish
        timeline_count = []
        def fail_plan(self, **kwargs):
            if kwargs['schema'] == 'word_timeline.v1':
                timeline_count.append(1)
            should_fail = kwargs['schema'] == 'production_plan.v2' if phase == 'plan' else kwargs['schema'] == 'word_timeline.v1' and len(timeline_count) == 2
            if should_fail and not failures:
                failures.append(1)
                raise failure
            return publish(self, **kwargs)
        monkeypatch.setattr(RemixArtifactStore, '_publish', fail_plan)
    with pytest.raises(TimeoutError):
        get_project_task_runner('remix_adopt_source_speech')({'payload': {**prepared['task_payload'],
            'operation_id': operation['operation_id']}, '__run_task_id': 'task'}, ctx)
    current = operations.get(operation['operation_id'])
    assert current['status'] == 'pending_reconciliation'
    recovered = reconcile_source_speech_adoption(ctx, current)
    assert recovered['output_refs'][0]['revision'] == 2
    assert len(calls) == 1


def test_adoption_preserves_valid_custom_cue_placement_and_selection_anchors(tmp_path):
    from novelvideo.freezone.remix.production_plan_v2 import ProductionPlanV2
    from novelvideo.freezone.remix.production_timing import project_speech_mainline
    store, pref, wref, obs, calls = setup(tmp_path)
    saved = store.read(pref['artifact_id'], pref['revision'])
    plan = deepcopy(saved['payload'])
    plan['speech_cues'] = [
        {'id': 'planned-line-one', 'take_id': 'take', 'turn_id': 'one', 'text': 'one'},
        {'id': 'planned-line-two', 'take_id': 'take', 'turn_id': 'two', 'text': 'two'},
    ]
    start = {'kind': 'cue', 'ref_id': 'planned-line-one', 'edge': 'start'}
    end = {'kind': 'cue', 'ref_id': 'planned-line-one', 'edge': 'end'}
    plan['placements'][0].update(start=start, end=end)
    plan['selections'] = [{'id': 'sentence', 'label': 'sentence', 'start': start, 'end': end}]
    revised = store.revise(saved['id'], saved['revision'], plan, saved['upstream_refs'])
    pref = {'artifact_id': revised['id'], 'revision': revised['revision']}
    result = adopt(store, pref, wref, obs)
    after = store.read(result['plan_ref']['artifact_id'], result['plan_ref']['revision'])['payload']
    assert [cue['id'] for cue in after['speech_cues']] == ['planned-line-one', 'planned-line-two']
    assert after['placements'][0] == revised['payload']['placements'][0]
    assert after['selections'] == revised['payload']['selections']
    projection = project_speech_mainline(ProductionPlanV2.model_validate(after))
    assert [(cue['id'], cue['start_frame'], cue['end_frame']) for cue in projection['speech_cues']] == [
        ('planned-line-one', 2, 24), ('planned-line-two', 77, 98)]
    assert len(calls) == 1


def test_adoption_preserves_unambiguous_multiple_cues_in_same_turn(tmp_path):
    store, pref, wref, obs, _ = setup(tmp_path, single_turn=True)
    saved = store.read(pref['artifact_id'], pref['revision'])
    plan = deepcopy(saved['payload'])
    plan['speech_cues'] = [
        {'id': 'first-word', 'take_id': 'take', 'turn_id': 'one', 'text': 'one'},
        {'id': 'second-word', 'take_id': 'take', 'turn_id': 'one', 'text': 'two'},
    ]
    revised = store.revise(saved['id'], saved['revision'], plan, saved['upstream_refs'])
    pref = {'artifact_id': revised['id'], 'revision': revised['revision']}
    result = adopt(store, pref, wref, obs)
    after = store.read(result['plan_ref']['artifact_id'], result['plan_ref']['revision'])['payload']
    raw = store.read(obs['artifact_id'], obs['revision'])['payload']['recognized_words']
    assert [(cue['id'], cue['text'], cue['word_ids'], cue['source_start_us'], cue['source_end_us'])
            for cue in after['speech_cues']] == [
        ('first-word', 'one', ['take:' + raw[0]['id']], 100_000, 1_000_000),
        ('second-word', 'two', ['take:' + raw[1]['id']], 3_200_000, 4_100_000)]


def test_ambiguous_cue_segmentation_reports_all_targets_before_any_publication(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.source_speech_adoption import SourceSpeechRemapRequired
    store, pref, wref, obs, _ = setup(tmp_path, single_turn=True)
    saved = store.read(pref['artifact_id'], pref['revision'])
    plan = deepcopy(saved['payload'])
    # These saved draft segments split a measured token. No timing can be invented.
    plan['speech_cues'] = [
        {'id': 'fragment-one', 'take_id': 'take', 'turn_id': 'one', 'text': 'o'},
        {'id': 'fragment-two', 'take_id': 'take', 'turn_id': 'one', 'text': 'ne two'},
    ]
    start = {'kind': 'cue', 'ref_id': 'fragment-one', 'edge': 'start'}
    end = {'kind': 'cue', 'ref_id': 'fragment-two', 'edge': 'end'}
    plan['placements'][0].update(start=start, end=end)
    plan['selections'] = [{'id': 'sentence', 'label': 'sentence', 'start': start, 'end': end}]
    revised = store.revise(saved['id'], saved['revision'], plan, saved['upstream_refs'])
    pref = {'artifact_id': revised['id'], 'revision': revised['revision']}
    publications = []
    original_publish = RemixArtifactStore._publish
    original_atomic = RemixArtifactStore._atomic_json
    def track_publish(self, **kwargs):
        publications.append(kwargs['schema'])
        return original_publish(self, **kwargs)
    def track_atomic(self, path, value, guard):
        publications.append(str(path))
        return original_atomic(self, path, value, guard)
    monkeypatch.setattr(RemixArtifactStore, '_publish', track_publish)
    monkeypatch.setattr(RemixArtifactStore, '_atomic_json', track_atomic)
    with pytest.raises(SourceSpeechRemapRequired) as caught:
        adopt(store, pref, wref, obs)
    assert caught.value.targets == [
        {'entity_kind': 'cue', 'entity_id': 'fragment-one', 'edge': 'range', 'cue_id': 'fragment-one'},
        {'entity_kind': 'cue', 'entity_id': 'fragment-two', 'edge': 'range', 'cue_id': 'fragment-two'},
        {'entity_kind': 'placement', 'entity_id': 'picture', 'edge': 'start', 'cue_id': 'fragment-one'},
        {'entity_kind': 'placement', 'entity_id': 'picture', 'edge': 'end', 'cue_id': 'fragment-two'},
        {'entity_kind': 'selection', 'entity_id': 'sentence', 'edge': 'start', 'cue_id': 'fragment-one'},
        {'entity_kind': 'selection', 'entity_id': 'sentence', 'edge': 'end', 'cue_id': 'fragment-two'},
    ]
    assert publications == []
    assert store.read_latest(pref['artifact_id'])['revision'] == pref['revision']


def test_adoption_keeps_unrelated_take_cues_and_reports_removed_word_anchors(tmp_path):
    from novelvideo.freezone.remix.source_speech_adoption import SourceSpeechRemapRequired
    store, pref, wref, obs, _ = setup(tmp_path)
    saved = store.read(pref['artifact_id'], pref['revision'])
    plan = deepcopy(saved['payload'])
    plan['beats'][1]['turns'] = [{'id': 'again', 'speaker': 'A', 'text': 'one two'}]
    plan['placements'].append({**plan['placements'][1], 'id': 'speech2',
        'start': {'kind': 'beat', 'ref_id': 'after', 'edge': 'start'},
        'end': {'kind': 'beat', 'ref_id': 'after', 'edge': 'end'}})
    plan['speech_takes'].append({**plan['speech_takes'][0], 'id': 'take2', 'beat_id': 'after',
        'turn_ids': ['again'], 'origin_placement_id': 'speech2'})
    plan['speech_cues'] = [
        {'id': 'planned-line-one', 'take_id': 'take', 'turn_id': 'one', 'text': 'one'},
        {'id': 'unrelated', 'take_id': 'take2', 'turn_id': 'again', 'text': 'one two'},
    ]
    revised = store.revise(saved['id'], saved['revision'], plan, saved['upstream_refs'])
    pref = {'artifact_id': revised['id'], 'revision': revised['revision']}
    result = adopt(store, pref, wref, obs)
    saved = store.read(result['plan_ref']['artifact_id'], result['plan_ref']['revision'])
    unrelated = next(c for c in revised['payload']['speech_cues'] if c['id'] == 'unrelated')
    assert next(c for c in saved['payload']['speech_cues'] if c['id'] == 'unrelated') == unrelated
    plan = deepcopy(saved['payload'])
    word = plan['beats'][0]['turns'][0]['speech_timing']['words'][0]['id']
    plan['placements'][0]['start'] = {'kind': 'word', 'ref_id': word, 'edge': 'start'}
    revised = store.revise(saved['id'], saved['revision'], plan, saved['upstream_refs'])
    pref = {'artifact_id': revised['id'], 'revision': revised['revision']}
    class NewEngine:
        def transcribe(self, path, language):
            return {'status': 'completed', 'transcript': 'one two', 'words': [
                {'text': 'one', 'start': .1, 'end': 1.0}, {'text': 'two', 'start': 3.2, 'end': 4.1}]}
    other = observe_source_speech(store, scope=SCOPE, resource=revised['payload']['resources'][0],
        language='en', provider_signature='new', aligner=NewEngine())
    before = list(store.root.rglob('*.json'))
    with pytest.raises(SourceSpeechRemapRequired) as caught:
        adopt(store, pref, wref, other, command='new-observation')
    assert caught.value.targets == [
        {'entity_kind': 'placement', 'entity_id': 'picture', 'edge': 'start', 'word_id': word}]
    assert list(store.root.rglob('*.json')) == before
    assert store.read_latest(pref['artifact_id'])['payload'] == revised['payload']


def test_repeated_words_require_explicit_cue_range_before_publication(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.source_speech_adoption import SourceSpeechRemapRequired
    store, pref, wref, obs, _ = setup(tmp_path, single_turn='one one two')
    saved = store.read(pref['artifact_id'], pref['revision'])
    plan = deepcopy(saved['payload'])
    plan['speech_cues'] = [
        {'id': 'which-one', 'take_id': 'take', 'turn_id': 'one', 'text': 'one'},
        {'id': 'ending', 'take_id': 'take', 'turn_id': 'one', 'text': 'one two'},
    ]
    revised = store.revise(saved['id'], saved['revision'], plan, saved['upstream_refs'])
    pref = {'artifact_id': revised['id'], 'revision': revised['revision']}
    def no_publication(*args, **kwargs):
        pytest.fail('ambiguous repeated words must be resolved before publication')
    monkeypatch.setattr(RemixArtifactStore, '_publish', no_publication)
    monkeypatch.setattr(RemixArtifactStore, '_atomic_json', no_publication)
    with pytest.raises(SourceSpeechRemapRequired) as caught:
        adopt(store, pref, wref, obs)
    assert caught.value.targets == [
        {'entity_kind': 'cue', 'entity_id': 'which-one', 'edge': 'range', 'cue_id': 'which-one'}]
    assert store.read_latest(pref['artifact_id'])['revision'] == pref['revision']
