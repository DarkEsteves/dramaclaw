from copy import deepcopy

import pytest
from pydantic import ValidationError

from novelvideo.freezone.remix.reference_commission import (
    AuthenticatedDecisionEvidence, advance_reference_commission, create_reference_commission,
)
from novelvideo.freezone.remix.store import RemixArtifactStore, RemixArtifactConflict
from test_reference_decision_ingress import setup_pending, message, apply, setup_plan_pending

pytest_plugins = ["test_api_freezone_workflow_runs"]


def independent_visual_plan(count=3):
    from test_production_plan import native_plan
    payload = native_plan()
    payload['placements'] = [{**payload['placements'][0], 'id': f'shot-{i}',
        'start': {'kind': 'beat', 'ref_id': 'hook', 'edge': 'start', 'offset_frames': i * 30},
        'end': {'kind': 'beat', 'ref_id': 'hook', 'edge': 'start', 'offset_frames': (i + 1) * 30}}
        for i in range(count)]
    payload['beats'][0]['duration_frames'] = count * 30
    return payload


def test_shot_requirement_counts_saved_visuals_not_goal(tmp_path):
    from novelvideo.freezone.remix.production_requirements import check_production_requirements
    payload = independent_visual_plan(4)
    payload['goal'] = '五个镜头'
    checks = check_production_requirements(payload, {'primary_shots': {'min': 5, 'max': 5}}, project_dir=tmp_path)
    assert any(row['code'] == 'primary_shot_count_mismatch' and row['status'] == 'failed' for row in checks)


def test_semantic_requirement_ids_must_be_unique():
    from novelvideo.freezone.remix.production_requirements import normalize_requirements

    with pytest.raises(ValueError, match='semantic requirement ids must be unique'):
        normalize_requirements({'semantic': [
            {'id': 'scene', 'text': 'Keep the original scene'},
            {'id': 'scene', 'text': 'Replace only the product'},
        ]})


@pytest.mark.parametrize('count', [2, 3, 7])
def test_independent_visuals_within_one_beat_pass_without_generated_assets(tmp_path, count):
    from novelvideo.freezone.remix.production_requirements import check_production_requirements
    checks = check_production_requirements(independent_visual_plan(count),
        {'primary_shots': {'min': count, 'max': count}}, project_dir=tmp_path)
    assert not any(row['status'] == 'failed' for row in checks)


def test_overlay_and_simultaneous_visuals_do_not_inflate_shots(tmp_path):
    from novelvideo.freezone.remix.production_requirements import check_production_requirements
    payload = independent_visual_plan(1)
    payload['placements'] += [{**payload['placements'][0], 'id': 'split-screen'},
                              {**payload['placements'][0], 'id': 'overlay', 'role': 'overlay'}]
    checks = check_production_requirements(payload, {'primary_shots': {'min': 3}}, project_dir=tmp_path)
    assert any(row['code'] == 'primary_shot_count_mismatch' and row['status'] == 'failed' for row in checks)


def test_invalid_visual_interval_cannot_satisfy_shot_count(tmp_path):
    from novelvideo.freezone.remix.production_requirements import check_production_requirements
    payload = independent_visual_plan(2)
    payload['placements'][1]['end']['offset_frames'] = 90
    checks = check_production_requirements(payload, {'primary_shots': {'min': 2}}, project_dir=tmp_path)
    assert any(row['code'] == 'primary_shot_timing_invalid' and row['entity_id'] == 'shot-1'
               and row['status'] == 'failed' for row in checks)


def test_duration_uses_rational_frame_rate_without_rounding(tmp_path):
    from novelvideo.freezone.remix.production_requirements import check_production_requirements
    payload = independent_visual_plan(1)
    payload['output'].update(fps_num=30000, fps_den=1001)
    checks = check_production_requirements(payload, {'duration_us': {'max': 1_000_999}}, project_dir=tmp_path)
    assert any(row['code'] == 'duration_mismatch' and row['status'] == 'failed' for row in checks)
    checks = check_production_requirements(payload, {'duration_us': {'min': 1_001_000, 'max': 1_001_000}}, project_dir=tmp_path)
    assert not any(row['status'] == 'failed' for row in checks)


def test_program_duration_frame_bounds_are_not_clip_limits():
    from novelvideo.freezone.remix.production_requirements import duration_frame_bounds

    assert duration_frame_bounds(
        {'duration_us': {'min': 18_100_000, 'max': 18_100_000}},
        fps_num=30, fps_den=1) == (543, 543)
    assert duration_frame_bounds(
        {'duration_us': {'max': 1_000_999}},
        fps_num=30000, fps_den=1001) == (None, 29)
    assert duration_frame_bounds({}, fps_num=30, fps_den=1) == (None, None)
    with pytest.raises(ValueError, match='duration_not_representable_at_fps'):
        duration_frame_bounds(
            {'duration_us': {'min': 1_000_000, 'max': 1_000_000}},
            fps_num=30000, fps_den=1001)


def test_source_timing_reads_saved_evidence_duration_not_last_event(tmp_path):
    from novelvideo.freezone.remix.production_requirements import resolve_source_timing

    store = RemixArtifactStore(tmp_path, 'p', created_by='u')
    evidence = store.create('reference_evidence.v1', {
        'media_hash': 'sha256:' + 'a' * 64,
        'time_base': '1/30000', 'duration_us': 18_100_000,
        'cuts': [], 'representative_frames': [],
    }, [])
    ref = {'artifact_id': evidence['id'], 'revision': 1}
    source = {'evidence_ref': ref, 'events': [
        {'event_id': 'last', 'start_us': 12_000_000, 'end_us': 17_500_000},
    ]}
    assert resolve_source_timing(store, source) == {
        'duration_us': 18_100_000, 'evidence_ref': ref,
        'event_ranges': [{'event_id': 'last', 'start_us': 12_000_000, 'end_us': 17_500_000}],
    }


def test_unspecified_shot_count_has_no_source_default(tmp_path):
    from novelvideo.freezone.remix.production_requirements import check_production_requirements
    assert not any(row['status'] == 'failed' for row in
        check_production_requirements(independent_visual_plan(1), {}, project_dir=tmp_path))


def test_unspecified_primary_shots_do_not_create_a_target_count_gate(tmp_path):
    from novelvideo.freezone.remix.production_requirements import check_production_requirements

    checks = check_production_requirements(
        independent_visual_plan(8), {}, project_dir=tmp_path
    )

    assert not any(
        row['code'] == 'primary_shot_count_mismatch'
        for row in checks
    )


def test_locked_spoken_dialogue_is_independent_from_screen_subtitles(tmp_path):
    from novelvideo.freezone.remix.production_requirements import check_production_requirements
    payload = independent_visual_plan(1)
    payload['beats'][0]['turns'] = [{'id': 'line', 'text': 'A synonymous rewrite.'}]
    payload['placements'].append({'id': 'caption', 'kind': 'subtitle', 'role': 'dialogue', 'track_id': 'subtitles',
        'text': 'Actual speech.', 'start': {'kind': 'beat', 'ref_id': 'hook', 'edge': 'start'},
        'end': {'kind': 'beat', 'ref_id': 'hook', 'edge': 'end'}})
    checks = check_production_requirements(payload, {'dialogue': {'mode': 'preserve_source'}},
        project_dir=tmp_path, locked_turns=[{'speaker': '', 'text': 'Actual speech.'}])
    assert any(row['code'] == 'locked_dialogue_changed' and row['status'] == 'failed' for row in checks)


def test_output_dimensions_are_checked(tmp_path):
    from novelvideo.freezone.remix.production_requirements import check_production_requirements
    checks = check_production_requirements(independent_visual_plan(1),
        {'output_requirements': {'width': 1920}}, project_dir=tmp_path)
    assert any(row['code'] == 'output_requirement_mismatch' and row['status'] == 'failed' for row in checks)
    assert next(row for row in checks if row['code'] == 'output_requirement_mismatch')['path'] == '/output/width'


def commission_fixture(tmp_path, requirements):
    store = RemixArtifactStore(tmp_path, 'p', created_by='u')
    transcript = store.create('word_timeline.v1', {'language': 'en', 'media_hash': 'sha256:' + 'a'*64,
        'transcript': 'Actual speech.', 'words': [{'text': 'Actual speech.', 'start_char': 0,
        'end_char': 14, 'start_us': 0, 'end_us': 1000000}]}, [])
    analysis = store.create('reference_analysis.v1', {'media_path': 'source.mp4', 'media_hash': 'sha256:'+'a'*64,
        'evidence_ref': {}, 'transcript_ref': {'artifact_id': transcript['id'], 'revision': 1},
        'transcript_status': 'completed', 'story': {'subtitles': ['Different screen text']}, 'coverage': {}}, [])
    c = create_reference_commission(store, canvas_id='c', requester_user_id='u', analysis_node_id='analysis',
        goal='g', analysis_evidence={'task_id': 't', 'result_fingerprint': 'h',
        'analysis_ref': {'artifact_id': analysis['id'], 'revision': 1}})
    c = advance_reference_commission(store, c['commission_ref'],
        {'type': 'direction_confirmed', 'direction': {'direction': 'current', 'requirements': requirements}}, 1,
        host_evidence=AuthenticatedDecisionEvidence('p', 'c', 'u', 'real-message'))
    return store, c, transcript


@pytest.mark.parametrize('bounds', [{'min': 2, 'max': 3}, {'min': 4, 'max': 6}, {'min': 8, 'max': 11}, None])
def test_bounded_requirements_have_no_scenario_defaults(bounds):
    from novelvideo.freezone.remix.production_requirements import ProductionRequirements
    value = {} if bounds is None else {'primary_shots': bounds}
    assert ProductionRequirements.model_validate(value).model_dump(exclude_none=True) == value


@pytest.mark.parametrize('value', [{'primary_shots': {'min': 0}}, {'duration_us': {'min': 20, 'max': 10}},
                                  {'primary_shots': {'min': True}}, {'output_requirements': {'width': -1}}])
def test_invalid_ranges_are_rejected(value):
    from novelvideo.freezone.remix.production_requirements import ProductionRequirements
    with pytest.raises(ValidationError):
        ProductionRequirements.model_validate(value)


def test_source_dialogue_uses_exact_transcript_and_host_provenance(tmp_path):
    from novelvideo.freezone.remix.production_requirements import resolve_production_requirements
    store, c, transcript = commission_fixture(tmp_path, {'dialogue': {'mode': 'preserve_source'}})
    result = resolve_production_requirements(store, commission=c)
    assert result['locked_turns'] == [{'speaker': '', 'text': 'Actual speech.'}]
    assert result['requirements']['dialogue']['source_turn_ids'] == [f"{transcript['id']}:1:0:14"]
    assert result['requirements_provenance']['user_message_id'] == 'real-message'
    assert result['requirements_provenance']['direction_fingerprint'] == c['direction_fingerprint']
    assert result['requirements_fingerprint']
    stale = deepcopy(c)
    stale['direction_revision'] += 1
    with pytest.raises(RemixArtifactConflict, match='direction'):
        resolve_production_requirements(store, commission=stale)


def test_unknown_source_turn_and_missing_original_text_block(tmp_path):
    from novelvideo.freezone.remix.production_requirements import resolve_production_requirements
    store, c, _ = commission_fixture(tmp_path, {'dialogue': {'mode': 'preserve_source', 'source_turn_ids': ['invented']}})
    with pytest.raises(ValueError, match='source_turn_ids'):
        resolve_production_requirements(store, commission=c)
    store2, d = setup_pending(tmp_path / 'other')
    c2 = advance_reference_commission(store2, d['decision']['commission_ref'],
        {'type': 'direction_confirmed', 'direction': {'requirements': {'dialogue': {'mode': 'preserve_source'}}}}, 1,
        host_evidence=AuthenticatedDecisionEvidence('p','c','u','msg'))
    with pytest.raises(ValueError, match='analysis_ref'):
        resolve_production_requirements(store2, commission=c2)


@pytest.mark.parametrize('requirements', [{'primary_shots': {'min': 5}}, {},
    {'semantic': [{'id': 'product', 'text': 'Use the specified product'}]},
    {'semantic': [{'id': 'pace', 'text': 'Longer pauses at scene transitions'}]}])
def test_semantic_direction_change_preserves_authenticated_message(tmp_path, requirements):
    store, old = setup_pending(tmp_path)
    scope, msg = message(tmp_path, 'arbitrary language interpreted semantically')
    result = apply(store, scope, msg, interpretation={'intent': 'approve_with_changes', 'requirements': requirements},
                   expected_decision_ref=old['decision_ref'])
    assert result['commission']['direction']['requirements'] == requirements
    assert result['commission']['decisions'][-1]['message_id'].endswith(f":{msg['id']}")


def test_bound_plan_amendment_keeps_plan_and_analysis_and_clears_review(tmp_path):
    from novelvideo.freezone.remix.production_requirements import apply_requirement_amendment
    from novelvideo.freezone.remix.reference_director import list_director_artifacts
    store, _, plan_ref = setup_plan_pending(tmp_path)
    c = list_director_artifacts(store, 'c', 'u')['commissions'][0]
    result = apply_requirement_amendment(store, commission_ref=c['commission_ref'],
        requirements={'primary_shots': {'min': 3, 'max': 5}},
        host_evidence=AuthenticatedDecisionEvidence('p','c','u','chat-message:amendment'),
        canvas={'project_id':'p','canvas_id':'c','nodes':[{'data': {'productionPlanV2Ref': {'artifactId':plan_ref['artifact_id'],'revision':plan_ref['revision']}}}]} )
    assert result['plan_ref'] == plan_ref
    assert result['analysis_evidence'] == c['analysis_evidence']
    assert result['phase'] == 'awaiting_plan_review'
    assert result.get('review_ref') is None
    assert result['direction_revision'] == c['direction_revision'] + 1
    assert result['decisions'][-1]['message_id'] == 'chat-message:amendment'


def test_native_action_validates_requirements_and_snapshots_manual_input(tmp_path):
    from novelvideo.api.routes.remix import prepare_registered_action
    store = RemixArtifactStore(tmp_path, 'p', created_by='u')
    prepared = prepare_registered_action(store, 'generate_production_plan_draft',
        {'topic': 'g', 'analysis_context': {'summary': 'evidence'}, 'requirements': {'primary_shots': {'min': 7}}},
        idempotency_key='manual', node_revision=1)
    assert prepared['task_payload']['requirements'] == {'primary_shots': {'min': 7}}
    assert prepared['task_payload']['requirements_fingerprint']
    assert prepared['normalized_parameters']['payload'] == {k:v for k,v in prepared['task_payload'].items() if k != 'node_revision'}


def test_node_source_requirements_use_analysis_independently_of_direction(tmp_path):
    from novelvideo.api.routes.remix import prepare_registered_action
    from novelvideo.freezone.remix.production_requirements import hydrate_native_requirements, resolve_production_requirements
    store, c, _ = commission_fixture(tmp_path, {'dialogue': {'mode': 'preserve_source'}, 'primary_shots': {'min': 6}})
    canvas = {'project_id': 'p', 'canvas_id': 'c', 'nodes': [{'id': 'analysis', 'type': 'videoStoryNode', 'data': {'referenceAnalysisRef': c['analysis_evidence']['analysis_ref']}},
        {'id': 'plan', 'type': 'scriptNode', 'data': {'remixRole': 'production_plan',
            'processingPayload': {'requirements': {'dialogue': {'mode': 'preserve_source'},
                                                  'primary_shots': {'min': 6}}}}}],
        'edges': [{'source': 'analysis', 'target': 'plan'}]}
    payload = {'topic': 'g', 'analysis_context': {'summary': 'actual'}}
    hydrate_native_requirements(store, payload, canvas=canvas, node_id='plan', requester_user_id='u')
    resolved = resolve_production_requirements(store, commission=c)
    assert payload['requirements'] == resolved['requirements']
    assert payload['requirements_provenance']['kind'] == 'manual_source'
    assert 'commission_ref' not in payload['requirements_provenance']
    prepared = prepare_registered_action(store, 'generate_production_plan_draft', payload,
        idempotency_key='bound', node_revision=1)
    assert prepared['task_payload']['locked_turns'] == resolved['locked_turns']
    altered = deepcopy(payload)
    altered['locked_turns'][0]['text'] = 'fabricated'
    with pytest.raises(ValueError, match='locked_turns'):
        prepare_registered_action(store, 'generate_production_plan_draft', altered, idempotency_key='bad', node_revision=1)
    advance_reference_commission(store, c['commission_ref'], {'type': 'direction_confirmed', 'direction': {'direction': 'changed'}},
        c['commission_ref']['revision'], host_evidence=AuthenticatedDecisionEvidence('p','c','u','new-msg'))
    prepared = prepare_registered_action(store, 'generate_production_plan_draft', payload,
        idempotency_key='unchanged-node', node_revision=1)
    assert prepared['task_payload']['locked_turns'] == resolved['locked_turns']


@pytest.mark.asyncio
async def test_real_chat_entry_applies_amendment_without_pending_decision(tmp_path, monkeypatch):
    from novelvideo.api.routes import chat, reference_director
    from novelvideo.freezone.remix.reference_director import list_director_artifacts
    from types import SimpleNamespace
    store, decision, plan_ref = setup_plan_pending(tmp_path)
    # Supersede presentation decision; amendments still use the bound commission.
    saved = store.read_latest(decision['decision_ref']['artifact_id'])
    store.revise(saved['id'], saved['revision'], {**saved['payload'], 'status': 'superseded'}, saved['upstream_refs'])
    scope, msg = message(tmp_path, 'Please simplify the pace while retaining the product.')
    canvas = {'project_id':'p','canvas_id':'c','nodes':[{'id':'plan','data':{
        'productionPlanV2Ref':{'artifactId':plan_ref['artifact_id'],'revision':plan_ref['revision']}}}]}
    from novelvideo.freezone import canvas_store
    state_dir = tmp_path / 'state'
    canvas_store.save_canvas(state_dir,'c',base_revision=None,build_payload=lambda _:deepcopy(canvas))
    async def context(*args, **kwargs):
        return SimpleNamespace(requester_user_id='u',state_dir=state_dir), canvas, store
    async def interpret(**kwargs):
        assert kwargs['decision']['kind'] == 'requirement_amendment'
        return {'intent':'revise','requirements':{'semantic':[{'id':'pace','text':'Simplify the pace'}]}}
    monkeypatch.setattr(reference_director, '_context', context)
    monkeypatch.setattr(chat, 'interpret_reference_reply', interpret, raising=False)
    result = await chat._reference_user_confirmation_context(user={'credential_kind':'browser_session'},
        scope=scope,message=msg,agent_text=msg['content'])
    assert result == msg['content']
    c = list_director_artifacts(store,'c','u')['commissions'][0]
    assert c['phase'] == 'awaiting_plan_confirmation' and c['plan_ref'] == plan_ref


def test_native_action_input_is_deeply_frozen(tmp_path):
    from novelvideo.api.routes.remix import prepare_registered_action
    store = RemixArtifactStore(tmp_path, 'p', created_by='u')
    payload = {'topic': 'g', 'requirements': {'primary_shots': {'min': 4}}}
    prepared = prepare_registered_action(store, 'generate_production_plan_draft', payload, idempotency_key='frozen', node_revision=1)
    payload['requirements']['primary_shots']['min'] = 99
    assert prepared['task_payload']['requirements']['primary_shots']['min'] == 4


def test_old_direction_receipt_does_not_block_publication_to_unchanged_node(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.production_processing_settlement import _safe_plan_target
    store, c, _ = commission_fixture(tmp_path, {'primary_shots': {'min': 4}})
    from novelvideo.freezone.remix.production_requirements import resolve_production_requirements
    from novelvideo.freezone.remix import production_processing_settlement as settlement
    from types import SimpleNamespace
    payload = {'topic': 'g', **resolve_production_requirements(store, commission=c)}
    node = {'id': 'plan', 'type': 'scriptNode', 'data': {'remixRole': 'production_plan',
        'processingAction': 'generate_production_plan_draft', 'processingPayload': payload,
        'productionWorkflowSubmission': {'action':'generate_production_plan_draft','payload':deepcopy(payload),
            'nodeData':settlement._node_input({'data': {'remixRole':'production_plan','processingAction':'generate_production_plan_draft','processingPayload':payload}})}}}
    action = {'node_id':'plan','action':'generate_production_plan_draft','node_revision':1,'payload':payload}
    ctx = SimpleNamespace(project_id='p', requester_user_id='u')
    monkeypatch.setattr(settlement, '_operation_automation_current', lambda *a,**k: True)
    monkeypatch.setattr(settlement, '_plan_binding_current', lambda *a,**k: True)
    advance_reference_commission(store,c['commission_ref'], {'type':'direction_confirmed','direction':{'direction':'new'}},
        c['commission_ref']['revision'],host_evidence=AuthenticatedDecisionEvidence('p','c','u','new-msg'))
    assert _safe_plan_target(ctx,'c',{'project_id':'p','canvas_id':'c','nodes':[node],'edges':[]},node,action,
        {'artifactId':'result','revision':1},store)

def test_actual_workflow_api_queues_node_inputs_without_direction_comparison(workflow_run_client):
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.workflow_runs import read_workflow_run
    client = workflow_run_client
    store = RemixArtifactStore(client.state_dir, 'proj_demo', created_by='u-alice')
    c = create_reference_commission(store, canvas_id='default', requester_user_id='u-alice', analysis_node_id='analysis',
        goal='g',analysis_evidence={'task_id':'real-task','result_fingerprint':'h'})
    c = advance_reference_commission(store,c['commission_ref'], {'type':'direction_confirmed','direction':{
        'target_goal':'g','requirements':{'primary_shots':{'min':5,'max':8}}}},1,
        host_evidence=AuthenticatedDecisionEvidence('proj_demo','default','u-alice','actual-message'))
    from novelvideo.freezone.remix.production_requirements import resolve_production_requirements
    node_payload = {'topic': 'g\n声音方式：自动判断',
                    **resolve_production_requirements(store, commission=c)}
    node_payload['requirements'] = {'primary_shots': {'min': 2, 'max': 4}}
    canvas = canvas_store.default_canvas_payload(project_id='proj_demo',actor_id='u-alice')
    canvas['nodes'] = [{'id':'analysis','type':'videoStoryNode','data':{'rawResult':{'summary':'real'}}},
                       {'id':'plan','type':'scriptNode','data':{'remixRole':'production_plan',
                            'processingPayload': node_payload}}]
    canvas['edges'] = [{'source':'analysis','target':'plan'}]
    path = canvas_store.canvas_path(client.state_dir,'default')
    path.parent.mkdir(parents=True,exist_ok=True)
    canvas_store.atomic_write_json(path,canvas)
    response = client.post('/api/v1/projects/proj_demo/freezone/canvases/default/workflow-runs',json={
        'idempotency_key':'requirements-run','actions':[{'node_id':'plan','action':'generate_production_plan_draft',
        'node_revision':1,'payload':{**node_payload,'analysis_context':{'summary':'real'}}}]})
    assert response.status_code == 200,response.text
    queued = client.enqueued_tasks[-1]['payload']
    assert queued['topic'] == node_payload['topic']
    assert queued['requirements'] == {'primary_shots':{'min':2,'max':4}}
    assert 'requirements_provenance' not in queued
    operation = RemixOperationStore(client.state_dir, 'proj_demo').get(queued['operation_id'])
    assert 'requirements_provenance' not in operation['normalized_parameters']['payload']
    run = read_workflow_run(project_dir=client.state_dir,canvas_id='default',run_id=queued['workflow_run_id'])
    assert run['actions'][0]['payload']['requirements_fingerprint'] == queued['requirements_fingerprint']


def test_manual_source_dialogue_without_commission_uses_the_actual_canvas_analysis(tmp_path):
    from novelvideo.freezone.remix.production_requirements import hydrate_native_requirements
    store,c,_ = commission_fixture(tmp_path, {})
    # Remove the direction candidate from this manual canvas's scope.
    canvas = {'project_id':'p','canvas_id':'manual','nodes':[{'id':'source','type':'videoStoryNode','data':{
        'referenceAnalysisRef': c['analysis_evidence']['analysis_ref']}}, {'id':'plan','type':'scriptNode'}],
        'edges':[{'source':'source','target':'plan'}]}
    payload = {'topic':'g','requirements':{'dialogue':{'mode':'preserve_source'}}}
    hydrate_native_requirements(store,payload,canvas=canvas,node_id='plan',requester_user_id='u')
    assert payload['locked_turns'] == [{'speaker':'','text':'Actual speech.'}]
    assert payload['requirements_provenance']['analysis_node_id'] == 'source'


def test_updated_requirement_after_official_plan_edit_requires_fresh_review(tmp_path):
    from novelvideo.freezone.remix.production_commands import apply_production_command
    from novelvideo.freezone.remix.plan_revision_settlement import reconcile_plan_revision
    from novelvideo.freezone.remix.production_requirements import apply_requirement_amendment
    from novelvideo.freezone.remix.reference_director import list_director_artifacts
    store,_,pref = setup_plan_pending(tmp_path)
    c = list_director_artifacts(store,'c','u')['commissions'][0]
    changed = apply_production_command(store,{'command_id':'official-edit','plan_ref':{k:pref[k] for k in ('artifact_id','revision')},
        'kind':'edit_beat','target_id':'b','payload':{'purpose':'Introduce the product clearly'}})
    ref = {**changed['plan_ref'],'project_id':'p'}
    c = reconcile_plan_revision(store,commission_ref=c['commission_ref'],plan_ref=ref)['commission']
    amended = apply_requirement_amendment(store,commission_ref=c['commission_ref'],requirements={'primary_shots':{'min':8}},
        host_evidence=AuthenticatedDecisionEvidence('p','c','u','real-amendment-message'),
        canvas={'project_id':'p','canvas_id':'c','nodes':[{'id':'plan','data':{'productionPlanV2Ref':{'artifactId':ref['artifact_id'],'revision':ref['revision']}}}]})
    assert amended['plan_ref'] == ref and amended['phase'] == 'awaiting_plan_review'
    assert amended.get('review_ref') is None


@pytest.mark.parametrize('evidence', [None, AuthenticatedDecisionEvidence('other','c','u','message'),
    AuthenticatedDecisionEvidence('p','wrong','u','message'),AuthenticatedDecisionEvidence('p','c','other','message')])
def test_amendment_rejects_forged_or_cross_scope_host_evidence(tmp_path,evidence):
    from novelvideo.freezone.remix.production_requirements import apply_requirement_amendment
    from novelvideo.freezone.remix.reference_director import list_director_artifacts
    store,_,ref = setup_plan_pending(tmp_path)
    c = list_director_artifacts(store,'c','u')['commissions'][0]
    with pytest.raises(PermissionError):
        apply_requirement_amendment(store,commission_ref=c['commission_ref'],requirements={},host_evidence=evidence,
            canvas={'project_id':'p','canvas_id':'c','nodes':[{'data':{'productionPlanV2Ref':{'artifactId':ref['artifact_id'],'revision':ref['revision']}}}]})
    assert list_director_artifacts(store,'c','u')['commissions'][0]['commission_ref'] == c['commission_ref']


def test_host_never_approves_partial_or_invalid_semantic_requirements(tmp_path):
    from novelvideo.freezone.remix.reference_director import list_director_artifacts
    store,d = setup_pending(tmp_path)
    scope,msg = message(tmp_path,'Clear acceptance interpreted with an invalid structure')
    result = apply(store,scope,msg,interpretation={'intent':'approve','requirements':{'primary_shots':{'min':0}}},
        expected_decision_ref=d['decision_ref'])
    assert result is None
    assert list_director_artifacts(store,'c','u')['commissions'][0]['phase'] == 'awaiting_direction'


@pytest.mark.parametrize('count', [2, 5, 9])
def test_unconditional_approval_preserves_requirements_extracted_from_direction(tmp_path, count):
    from novelvideo.freezone.remix.reference_director import prepare_reference_decision, list_director_artifacts
    from novelvideo.freezone.remix.production_requirements import resolve_production_requirements
    store, initial = setup_pending(tmp_path)
    proposal = {'direction': f'Keep the product and create exactly {count} primary shots.'}
    decision = prepare_reference_decision(store, initial['decision']['commission_ref'], 'direction',
        proposal, proposal['direction'], 'natural-language-direction')
    scope, msg = message(tmp_path, 'Yes, proceed with that direction.')
    requirements = {'primary_shots': {'min': count, 'max': count}}
    result = apply(store, scope, msg, interpretation={'intent':'approve','requirements':requirements},
        expected_decision_ref=decision['decision_ref'])
    confirmed = result['commission']
    assert confirmed['direction'] == {**proposal, 'requirements': requirements}
    assert 'user_requested_change' not in confirmed['direction']
    assert confirmed['decisions'][-1]['message_id'] == f"chat-message:u:c:main:{msg['id']}"
    assert resolve_production_requirements(store, commission=confirmed)['requirements'] == requirements
    assert list_director_artifacts(store,'c','u')['decisions'] == []


@pytest.mark.asyncio
async def test_amendment_rechecks_live_canvas_after_semantic_interpretation(tmp_path, monkeypatch):
    from novelvideo.api.routes import chat, reference_director
    from novelvideo.freezone import canvas_store
    from novelvideo.freezone.remix.reference_director import list_director_artifacts
    from types import SimpleNamespace
    store,d,ref = setup_plan_pending(tmp_path)
    c = list_director_artifacts(store,'c','u')['commissions'][0]
    scope,msg = message(tmp_path,'Change the opening rhythm.')
    state_dir = tmp_path / 'state'
    canvas = {'project_id':'p','canvas_id':'c','revision':1,'nodes':[{'id':'plan','type':'scriptNode','data':{
        'productionPlanV2Ref':{'artifactId':ref['artifact_id'],'revision':ref['revision']}}}], 'edges':[]}
    canvas_store.save_canvas(state_dir,'c',base_revision=None,build_payload=lambda _:deepcopy(canvas))
    async def context(*args,**kwargs):
        return SimpleNamespace(requester_user_id='u',state_dir=state_dir), deepcopy(canvas), store
    calls = []
    completions = []
    async def interpret(**kwargs):
        calls.append(kwargs)
        canvas_store.save_canvas(state_dir,'c',base_revision=1,allow_empty_overwrite=True,save_source='manual_clear',
            build_payload=lambda existing: {**existing,'revision':2,'nodes':[]})
        completions.append(True)
        return {'intent':'revise','requirements':{'semantic':[{'id':'rhythm','text':'Change the opening rhythm'}]}}
    monkeypatch.setattr(reference_director,'_context',context)
    monkeypatch.setattr(chat,'interpret_reference_reply',interpret, raising=False)
    result = await chat._reference_user_confirmation_context(user={'credential_kind':'browser_session'},
        scope=scope,message=msg,agent_text=msg['content'])
    assert calls == [] and completions == []
    assert 'HOST_REFERENCE_DECISION_CONTEXT' in result
    assert canvas_store.read_canvas(state_dir,'c')['nodes'] != []
    assert 'HOST_REFERENCE_DECISION_RECEIPT' not in result
    latest = list_director_artifacts(store,'c','u')['commissions'][0]
    assert latest['commission_ref'] == c['commission_ref']
    assert latest['direction'] == c['direction']
    assert latest['review_ref'] == c['review_ref']


@pytest.mark.parametrize('interleave', ['before_publish', 'before_envelope_replace'])
def test_amendment_final_publication_fences_canvas_lease(tmp_path, monkeypatch, interleave):
    from novelvideo.freezone.remix.production_requirements import apply_requirement_amendment
    from novelvideo.freezone.remix.reference_director import list_director_artifacts
    store,_,ref = setup_plan_pending(tmp_path)
    c = list_director_artifacts(store,'c','u')['commissions'][0]
    class CanvasGuard:
        active = True
        def reassert(self):
            if not self.active:
                raise RuntimeError('canvas lease lost')
    canvas_guard = CanvasGuard()
    if interleave == 'before_publish':
        original = store._publish
        def publish(**kwargs):
            if kwargs['schema'] == 'reference_commission.v1':
                canvas_guard.active = False
            return original(**kwargs)
        monkeypatch.setattr(store,'_publish',publish)
    else:
        original = store._atomic_json
        def atomic(target,value,guard):
            if value.get('id') == c['commission_ref']['artifact_id'] and value.get('revision') == c['commission_ref']['revision'] + 1:
                canvas_guard.active = False
            return original(target,value,guard)
        monkeypatch.setattr(store,'_atomic_json',atomic)
    with pytest.raises(RuntimeError, match='canvas lease lost'):
        apply_requirement_amendment(store,commission_ref=c['commission_ref'],requirements={'primary_shots':{'min':3}},
            host_evidence=AuthenticatedDecisionEvidence('p','c','u','real-message'), _canvas_guard=canvas_guard,
            canvas={'project_id':'p','canvas_id':'c','nodes':[{'data':{'productionPlanV2Ref':{
                'artifactId':ref['artifact_id'],'revision':ref['revision']}}}]})
    latest = store.read_latest(c['commission_ref']['artifact_id'])
    assert latest['revision'] == c['commission_ref']['revision']
    assert latest['payload']['direction'] == c['direction']
    assert latest['payload']['review_ref'] == c['review_ref']
