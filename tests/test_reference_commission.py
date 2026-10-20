import pytest
from novelvideo.freezone.remix import reference_commission as commission
from novelvideo.freezone.remix.store import RemixArtifactStore, RemixArtifactConflict


def test_commission_waits_for_host_direction_and_persists_versions(tmp_path):
    store = RemixArtifactStore(tmp_path, 'p')
    first = commission.create_reference_commission(store, canvas_id='c', requester_user_id='u', analysis_node_id='a', goal='remix', analysis_evidence={'task_id': 'task', 'result_fingerprint': 'hash'})
    assert first['phase'] == 'awaiting_direction'
    assert first['next_action']['kind'] == 'discuss_direction'
    assert first['next_action']['tool'] is None
    ref = first['commission_ref']
    with pytest.raises(PermissionError):
        commission.advance_reference_commission(store, ref, {'type': 'direction_confirmed', 'direction': {'keep': ['story']}}, 1)
    evidence = commission.AuthenticatedDecisionEvidence('p', 'c', 'u', 'message-1')
    second = commission.advance_reference_commission(store, ref, {'type': 'direction_confirmed', 'direction': {'keep': ['story']}}, 1, host_evidence=evidence)
    assert second['phase'] == 'drafting_plan'
    assert second['next_action'] == {
        'owner': 'agent', 'kind': 'prepare_production_nodes',
        'tool': 'freezone_prepare_production_nodes',
    }
    assert commission.read_reference_commission(store, ref)['phase'] == 'awaiting_direction'
    with pytest.raises(RemixArtifactConflict):
        commission.advance_reference_commission(store, ref, {'type': 'direction_confirmed', 'direction': {}}, 1, host_evidence=evidence)
    wrong = commission.AuthenticatedDecisionEvidence('p', 'other', 'u', 'message-2')
    with pytest.raises(PermissionError):
        commission.advance_reference_commission(store, second['commission_ref'], {'type': 'direction_confirmed', 'direction': {}}, 2, host_evidence=wrong)


def test_confirmed_direction_rejects_invalid_preservation_before_new_revision(tmp_path):
    store = RemixArtifactStore(tmp_path, 'p')
    first = commission.create_reference_commission(
        store, canvas_id='c', requester_user_id='u', analysis_node_id='a',
        goal='remix', analysis_evidence={'task_id': 'task', 'result_fingerprint': 'hash'},
    )
    evidence = commission.AuthenticatedDecisionEvidence('p', 'c', 'u', 'message-1')
    with pytest.raises(ValueError, match='preserve'):
        commission.advance_reference_commission(
            store, first['commission_ref'],
            {'type': 'direction_confirmed', 'direction': {'preserve': [42]}}, 1,
            host_evidence=evidence,
        )
    assert store.read_latest(first['commission_ref']['artifact_id'])['revision'] == 1


def test_plan_decision_binds_exact_saved_plan(tmp_path):
    store = RemixArtifactStore(tmp_path, 'p')
    result = commission.create_reference_commission(store, canvas_id='c', requester_user_id='u', analysis_node_id='a', goal='g', analysis_evidence={'task_id': 't', 'result_fingerprint': 'h'})
    auth = commission.AuthenticatedDecisionEvidence('p', 'c', 'u', 'm')
    result = commission.advance_reference_commission(store, result['commission_ref'], {'type': 'direction_confirmed', 'direction': {'keep': ['story']}}, 1, host_evidence=auth)
    with pytest.raises(ValueError, match='saved'):
        commission.advance_reference_commission(store, result['commission_ref'], {'type': 'plan_ready', 'plan_ref': {'artifact_id':'missing', 'revision':1}}, 2)
    with pytest.raises(ValueError):
        commission.read_reference_commission(RemixArtifactStore(tmp_path, 'other'), result['commission_ref'])


def test_commission_rejects_direction_without_matching_decision_receipt():
    with pytest.raises(ValueError, match='receipt'):
        commission.ReferenceCommission(project_id='p', canvas_id='c', requester_user_id='u', analysis_node_id='a',
            goal='g', analysis_evidence={'task_id':'t','result_fingerprint':'h'}, phase='drafting_plan',
            direction={'target_goal':'g'}, direction_revision=1)


def test_review_then_host_confirmation_enables_production_and_no_dialogue_skip(tmp_path):
    from novelvideo.freezone.remix.production_review import review_production_plan, REVIEW_CATEGORIES
    store = RemixArtifactStore(tmp_path, 'p')
    auth = commission.AuthenticatedDecisionEvidence('p','c','u','message')
    c = commission.create_reference_commission(store, canvas_id='c', requester_user_id='u', analysis_node_id='a',goal='g',analysis_evidence={'task_id':'t','result_fingerprint':'h'})
    c = commission.advance_reference_commission(store,c['commission_ref'],{'type':'direction_confirmed','direction':{'target_goal':'g'}},1,host_evidence=auth)
    p = store.create('production_plan.v2', {'schema_version':2,'goal':'g','output':{'width':1920,'height':1080,'fps_num':24,'fps_den':1},'beats':[{'id':'b','purpose':'intro','duration_frames':48}]},[])
    pref = {'artifact_id':p['id'],'revision':1,'project_id':'p'}
    c = commission.advance_reference_commission(store,c['commission_ref'],{'type':'plan_ready','plan_ref':pref},2)
    findings = [{'category':x,'entity_kind':'beat','entity_id':'b','verdict':'pass','reason':'Concrete beat meets confirmed direction','evidence':['b:intro']} for x in REVIEW_CATEGORIES]
    r = review_production_plan(store,pref,c['commission_ref'],findings)
    rref = {'artifact_id':r['id'],'revision':1,'project_id':'p'}
    c = commission.advance_reference_commission(store,c['commission_ref'],{'type':'plan_reviewed','review_ref':rref},3)
    with pytest.raises(PermissionError):
        commission.advance_reference_commission(store,c['commission_ref'],{'type':'plan_confirmed','plan_ref':pref},4)
    c = commission.advance_reference_commission(store,c['commission_ref'],{'type':'plan_confirmed','plan_ref':pref},4,host_evidence=auth)
    assert c['phase'] == 'preparing_materials'
    c = commission.advance_reference_commission(store,c['commission_ref'],{'type':'speech_aligned','plan_ref':pref},5)
    assert c['phase'] == 'composing'


def confirmed_plan(store):
    from novelvideo.freezone.remix.production_review import review_production_plan, REVIEW_CATEGORIES
    auth = commission.AuthenticatedDecisionEvidence('p','c','u','message')
    c = commission.create_reference_commission(store, canvas_id='c', requester_user_id='u', analysis_node_id='a',goal='g',analysis_evidence={'task_id':'t','result_fingerprint':'h'})
    c = commission.advance_reference_commission(store,c['commission_ref'],{'type':'direction_confirmed','direction':{'target_goal':'g'}},1,host_evidence=auth)
    p = store.create('production_plan.v2', {'schema_version':2,'goal':'g','output':{'width':1920,'height':1080,'fps_num':24,'fps_den':1},'beats':[{'id':'b','purpose':'intro','duration_frames':48}]},[])
    pref = {'artifact_id':p['id'],'revision':1,'project_id':'p'}
    c = commission.advance_reference_commission(store,c['commission_ref'],{'type':'plan_ready','plan_ref':pref},2)
    findings = [{'category':x,'entity_kind':'beat','entity_id':'b','verdict':'pass','reason':'Concrete beat meets confirmed direction','evidence':['b:intro']} for x in REVIEW_CATEGORIES]
    r = review_production_plan(store,pref,c['commission_ref'],findings)
    c = commission.advance_reference_commission(store,c['commission_ref'],{'type':'plan_reviewed','review_ref':{'artifact_id':r['id'],'revision':1,'project_id':'p'}},3)
    c = commission.advance_reference_commission(store,c['commission_ref'],{'type':'plan_confirmed','plan_ref':pref},4,host_evidence=auth)
    return c, p


def test_changed_creative_plan_can_return_to_review_preserving_direction(tmp_path):
    store = RemixArtifactStore(tmp_path, 'p')
    c, p = confirmed_plan(store)
    changed = p['payload']
    changed['beats'][0]['purpose'] = 'new opening'
    p = store.revise(p['id'], 1, changed, [])
    pref = {'artifact_id':p['id'],'revision':2,'project_id':'p'}
    revised = commission.advance_reference_commission(store,c['commission_ref'],{'type':'plan_ready','plan_ref':pref},5)
    assert revised['phase'] == 'awaiting_plan_review'
    assert revised['direction_revision'] == c['direction_revision']
    assert revised['decisions'] == c['decisions']
    assert revised.get('review_ref') is None
    assert all(revised.get(field) is None for field in ('work_ref','manifest_ref','output_review_ref'))


def test_timing_only_change_recompiles_without_creative_reconfirmation(tmp_path):
    store = RemixArtifactStore(tmp_path, 'p')
    c, p = confirmed_plan(store)
    c = commission.advance_reference_commission(store,c['commission_ref'],{'type':'speech_aligned','plan_ref':c['plan_ref']},5)
    changed = p['payload']
    changed['beats'][0]['duration_frames'] = 96
    p = store.revise(p['id'], 1, changed, [])
    pref = {'artifact_id':p['id'],'revision':2,'project_id':'p'}
    revised = commission.advance_reference_commission(store,c['commission_ref'],{'type':'plan_ready','plan_ref':pref},6)
    assert revised['phase'] == 'composing'
    assert revised['review_ref'] == c['review_ref']
    assert revised['decisions'] == c['decisions']


def test_old_manifest_cannot_complete_after_timing_revision(tmp_path):
    from novelvideo.freezone.remix.production_plan_v2 import compile_production_plan_v2
    store = RemixArtifactStore(tmp_path, 'p')
    c, p = confirmed_plan(store)
    c = commission.advance_reference_commission(store,c['commission_ref'],{'type':'speech_aligned','plan_ref':c['plan_ref']},5)
    manifest = compile_production_plan_v2(p['payload'])
    manifest['source_versions'] = [{'artifact_id':p['id'], 'revision':1}]
    saved = store.create('composition_manifest.v2', manifest, [c['plan_ref']])
    mref = {'artifact_id':saved['id'], 'revision':1, 'project_id':'p'}
    c = commission.advance_reference_commission(store,c['commission_ref'],{'type':'composition_ready','manifest_ref':mref},6)
    changed = p['payload']
    changed['beats'][0]['duration_frames'] = 96
    store.revise(p['id'], 1, changed, [])
    with pytest.raises(ValueError, match='exact current plan'):
        commission.advance_reference_commission(store,c['commission_ref'],{'type':'output_reviewed','review_ref':{'artifact_id':'old-passed-review','revision':1}},7)


def test_silent_media_plan_cannot_skip_material_acceptance(tmp_path):
    from novelvideo.freezone.remix.production_review import review_production_plan, REVIEW_CATEGORIES
    store = RemixArtifactStore(tmp_path, 'p')
    c, p = confirmed_plan(store)
    draft = p['payload']
    draft['placements'] = [{'id':'picture','kind':'visual','role':'primary','track_id':'main',
        'start':{'kind':'beat','ref_id':'b','edge':'start'},'end':{'kind':'beat','ref_id':'b','edge':'end'}}]
    changed = store.revise(p['id'],1,draft,[])
    pref = {'artifact_id':changed['id'],'revision':2,'project_id':'p'}
    c = commission.advance_reference_commission(store,c['commission_ref'],{'type':'plan_ready','plan_ref':pref},5)
    findings = [{'category':x,'entity_kind':'beat','entity_id':'b','verdict':'pass','reason':'Product image is required before composition','evidence':['picture:product']} for x in REVIEW_CATEGORIES]
    review = review_production_plan(store,pref,c['commission_ref'],findings)
    c = commission.advance_reference_commission(store,c['commission_ref'],{'type':'plan_reviewed','review_ref':{'artifact_id':review['id'],'revision':1,'project_id':'p'}},6)
    c = commission.advance_reference_commission(store,c['commission_ref'],{'type':'plan_confirmed','plan_ref':pref},7,
        host_evidence=commission.AuthenticatedDecisionEvidence('p','c','u','new-plan-approved'))
    with pytest.raises(ValueError,match='material'):
        commission.advance_reference_commission(store,c['commission_ref'],{'type':'speech_aligned','plan_ref':pref},8)


def confirmed_bound_picture(tmp_path):
    from PIL import Image
    from novelvideo.freezone.remix.material_work import prepare_material_work, accept_material_candidate
    from novelvideo.freezone.remix.material_execution import review_material_candidate, confirm_material_review
    from novelvideo.freezone.remix.production_review import review_production_plan, REVIEW_CATEGORIES

    store = RemixArtifactStore(tmp_path, 'p', created_by='u')
    c, plan = confirmed_plan(store)
    payload = plan['payload']
    payload['placements'] = [{'id':'picture','kind':'visual','role':'primary','track_id':'main',
        'start':{'kind':'beat','ref_id':'b','edge':'start'},'end':{'kind':'beat','ref_id':'b','edge':'end'}}]
    plan = store.revise(plan['id'], 1, payload, [])
    pref = {'artifact_id':plan['id'],'revision':2,'project_id':'p'}
    c = commission.advance_reference_commission(store,c['commission_ref'],{'type':'plan_ready','plan_ref':pref},5)
    findings = [{'category':category,'entity_kind':'beat','entity_id':'b','verdict':'pass',
        'reason':'Opening needs the product photograph','evidence':['picture:product']} for category in REVIEW_CATEGORIES]
    reviewed = review_production_plan(store,pref,c['commission_ref'],findings)
    c = commission.advance_reference_commission(store,c['commission_ref'],{'type':'plan_reviewed',
        'review_ref':{'artifact_id':reviewed['id'],'revision':1,'project_id':'p'}},6)
    c = commission.advance_reference_commission(store,c['commission_ref'],{'type':'plan_confirmed','plan_ref':pref},7,
        host_evidence=commission.AuthenticatedDecisionEvidence('p','c','u','approve-picture'))
    prepared = prepare_material_work(store, {'artifact_id':plan['id'],'revision':2},
        [{'id':'product','placement_ids':['picture'],'description':'Product photograph','media_kind':'image','final_step_id':'image'}],
        [{'id':'image','label':'Product photograph','media_kind':'image','source':'upload'}], 'prepare-picture')
    Image.new('RGB',(16,12),'red').save(tmp_path/'picture.png')
    canvas = {'nodes':[{'id':'image-node','data':{'imageUrl':'/static/projects/p/picture.png'}}]}
    candidate = review_material_candidate(store,prepared['work_ref'],'image',canvas,'image-node')
    approved = confirm_material_review(store,candidate['review_ref'],canvas,canvas_id='c',requester_user_id='u')
    resource = approved['review']['resource']
    accepted = accept_material_candidate(store,prepared['work_ref'],'image',resource,approved['review_ref'],'accept-picture')
    payload = plan['payload']
    payload['resources'] = [resource]
    payload['placements'][0]['resource_id'] = resource['id']
    bound = store.revise(plan['id'],2,payload,[])
    return store, c, bound, accepted, resource


def test_retry_refined_plan_retains_original_host_approval(tmp_path):
    from novelvideo.freezone.remix.material_work import retry_material_step
    from novelvideo.freezone.remix.production_plan_v2 import compile_production_plan_v2
    store, c, bound, accepted, resource = confirmed_bound_picture(tmp_path)
    original_decisions = c['decisions']
    original_review = c['review_ref']
    confirmed_ref = original_decisions[-1]['plan_ref']
    assert bound['revision'] > confirmed_ref['revision']
    c = commission.advance_reference_commission(store,c['commission_ref'],
        {'type':'materials_ready','work_ref':accepted['work_ref']},c['commission_ref']['revision'])
    c = commission.advance_reference_commission(store,c['commission_ref'],
        {'type':'speech_aligned','plan_ref':c['plan_ref']},c['commission_ref']['revision'])
    manifest = compile_production_plan_v2(bound['payload'])
    manifest['source_versions'] = [{'artifact_id':bound['id'],'revision':bound['revision']}]
    saved = store.create('composition_manifest.v2',manifest,[c['plan_ref']])
    c = commission.advance_reference_commission(store,c['commission_ref'],
        {'type':'composition_ready','manifest_ref':{'artifact_id':saved['id'],'revision':1,'project_id':'p'}},
        c['commission_ref']['revision'])
    retry = retry_material_step(store,accepted['work_ref'],'image','Replace the photograph','later-retry')
    rewound = commission.advance_reference_commission(store,c['commission_ref'],
        {'type':'materials_invalidated','work_ref':retry['work_ref']},c['commission_ref']['revision'])
    assert rewound['phase'] == 'preparing_materials'
    assert rewound['plan_ref'] == c['plan_ref'] and rewound['plan_ref'] != confirmed_ref
    assert rewound['decisions'] == original_decisions
    assert rewound['review_ref'] == original_review
    assert rewound.get('manifest_ref') is None and rewound.get('output_review_ref') is None
    assert rewound['work_ref'] == retry['work_ref']
    assert commission.read_reference_commission(store,rewound['commission_ref'])['decisions'] == original_decisions
    with pytest.raises(ValueError,match='material'):
        commission.advance_reference_commission(store,rewound['commission_ref'],
            {'type':'materials_ready','work_ref':retry['work_ref']},rewound['commission_ref']['revision'])


@pytest.mark.parametrize('change', ['creative', 'other_plan', 'other_direction', 'unconfirmed_review', 'receipt'])
def test_refined_approval_lineage_rejects_changed_identity_under_store_lock(tmp_path,change):
    from copy import deepcopy
    store = RemixArtifactStore(tmp_path,'p')
    c, p = confirmed_plan(store)
    payload = deepcopy(p['payload'])
    payload['beats'][0]['duration_frames'] = 96
    if change in {'creative','unconfirmed_review'}:
        payload['beats'][0]['purpose'] = 'Changed creative opening'
    revised = store.revise(p['id'],1,payload,[])
    data = {key:deepcopy(c[key]) for key in commission.ReferenceCommission.model_fields if key in c}
    data['plan_ref'] = {'artifact_id':revised['id'],'revision':2,'project_id':'p'}
    if change == 'other_plan':
        other = store.create('production_plan.v2',p['payload'],[])
        data['plan_ref'] = {'artifact_id':other['id'],'revision':1,'project_id':'p'}
    elif change == 'other_direction':
        other, _ = confirmed_plan(store)
        data['review_ref'] = other['review_ref']
    elif change == 'unconfirmed_review':
        from novelvideo.freezone.remix.production_review import review_production_plan, REVIEW_CATEGORIES
        findings = [{'category':category,'entity_kind':'beat','entity_id':'b','verdict':'pass',
            'reason':'Changed opening has a fresh review but no human approval','evidence':['b:changed']}
            for category in REVIEW_CATEGORIES]
        reviewed = review_production_plan(store,data['plan_ref'],c['commission_ref'],findings)
        data['review_ref'] = {'artifact_id':reviewed['id'],'revision':1,'project_id':'p'}
    elif change == 'receipt':
        data['decisions'][-1]['message_id'] = 'fabricated-reconfirmation'
        data['decisions'][-1]['plan_ref'] = data['plan_ref']
    refs = [data['plan_ref'],data['review_ref']]
    with pytest.raises(ValueError,match='approval|receipt'):
        store.revise(c['commission_ref']['artifact_id'],c['commission_ref']['revision'],data,refs)
    assert commission.read_reference_commission(store,c['commission_ref'],latest=True)['decisions'] == c['decisions']


def test_retry_invalidated_accepted_work_cannot_resume_materials(tmp_path):
    from novelvideo.freezone.remix.material_work import retry_material_step
    store, c, bound, accepted, resource = confirmed_bound_picture(tmp_path)
    old_ref = accepted['work_ref']
    assert store.read(old_ref['artifact_id'],old_ref['revision'])['payload']['steps'][0]['status'] == 'accepted'
    import shutil
    baseline_dir = tmp_path.parent / (tmp_path.name + '-accepted-baseline')
    shutil.copytree(tmp_path,baseline_dir)
    baseline_store = RemixArtifactStore(baseline_dir,'p',created_by='u')
    ready = commission.advance_reference_commission(baseline_store,c['commission_ref'],{'type':'materials_ready','work_ref':old_ref},8)
    assert ready['phase'] == 'aligning_speech'
    # A later material revision invalidates acceptance while the bound old image remains on the plan.
    retry = retry_material_step(store,old_ref,'image','Product photograph needs replacement','retry-picture')
    assert retry['work']['steps'][0]['status'] == 'rejected'
    assert store.read_latest(bound['id'])['payload']['resources'] == [resource]
    with pytest.raises(RemixArtifactConflict,match='material work changed'):
        commission.advance_reference_commission(store,c['commission_ref'],{'type':'materials_ready','work_ref':old_ref},8)
    assert commission.read_reference_commission(store,c['commission_ref'],latest=True)['phase'] == 'preparing_materials'


def selectable_work(store, plan, command_id='prepare-selection'):
    from novelvideo.freezone.remix.material_work import prepare_material_work
    return prepare_material_work(store, {'artifact_id': plan['id'], 'revision': plan['revision']},
        [{'id': 'r', 'description': 'Reference image', 'media_kind': 'image', 'final_step_id': 'image'}],
        [{'id': 'image', 'label': 'Image', 'media_kind': 'image', 'source': 'upload'}], command_id)['work_ref']


def selection_canvas(plan):
    return {'project_id': 'p', 'canvas_id': 'c', 'nodes': [
        {'id': 'a', 'type': 'videoStoryNode', 'data': {}},
        {'id': 'plan', 'type': 'scriptNode', 'data': {'productionPlanV2Ref': {'artifactId': plan['id'], 'revision': plan['revision']}}}],
        'edges': [{'source': 'a', 'target': 'plan'}]}


def select(store, c, work_ref, canvas, command='select'):
    return commission.select_material_work(store, c['commission_ref'], work_ref, command,
        host_evidence=commission.AuthenticatedDecisionEvidence('p', 'c', 'u', command), canvas=canvas)


def test_material_selection_records_prior_work_without_advancing_phase_and_replays(tmp_path):
    store = RemixArtifactStore(tmp_path, 'p', created_by='u')
    c, p = confirmed_plan(store)
    first, second = selectable_work(store, p), selectable_work(store, p, 'second')
    canvas = selection_canvas(p)
    selected = select(store, c, first, canvas)
    assert selected['phase'] == 'preparing_materials'
    assert selected['work_ref'] == {**first, 'project_id': 'p'}
    assert selected['decisions'][:-1] == c['decisions']
    assert selected['decisions'][-1]['previous_work_ref'] is None
    assert select(store, c, first, canvas) == selected
    with pytest.raises(RemixArtifactConflict, match='command'):
        select(store, c, second, canvas)
    with pytest.raises(RemixArtifactConflict, match='Commission changed'):
        select(store, c, second, canvas, 'new-command')
    changed = select(store, selected, second, canvas, 'switch')
    assert changed['decisions'][-1]['previous_work_ref'] == selected['work_ref']
    assert commission.read_reference_commission(store, selected['commission_ref'])['work_ref'] == selected['work_ref']


@pytest.mark.parametrize('lost_after_fsync', [1, 2, 3], ids=['epoch', 'payload', 'envelope'])
def test_material_selection_cannot_publish_after_canvas_lease_loss(tmp_path, monkeypatch, lost_after_fsync):
    from novelvideo.freezone.remix import store as store_module

    store = RemixArtifactStore(tmp_path, 'p', created_by='u')
    c, p = confirmed_plan(store)
    work = selectable_work(store, p)

    class CanvasGuard:
        lost = False

        def reassert(self):
            if self.lost:
                raise PermissionError('canvas lease lost')

    canvas_guard = CanvasGuard()
    fsync_count = 0
    real_fsync = store_module.os.fsync

    def lose_canvas_lease_after_fsync(fd):
        nonlocal fsync_count
        real_fsync(fd)
        fsync_count += 1
        if fsync_count == lost_after_fsync:
            canvas_guard.lost = True

    monkeypatch.setattr(store_module.os, 'fsync', lose_canvas_lease_after_fsync)
    with pytest.raises(PermissionError, match='canvas lease lost'):
        commission.select_material_work(store, c['commission_ref'], work, 'lease-loss',
            host_evidence=commission.AuthenticatedDecisionEvidence('p', 'c', 'u', 'lease-loss'),
            canvas=selection_canvas(p), _canvas_guard=canvas_guard)

    assert canvas_guard.lost
    assert commission.read_reference_commission(store, c['commission_ref'], latest=True) == c
    assert not store._path(c['commission_ref']['artifact_id'], c['commission_ref']['revision'] + 1, '.json').exists()


@pytest.mark.parametrize('change', ['project', 'canvas', 'private', 'creator', 'plan', 'plan_node', 'material_node', 'stale_work', 'stale_plan', 'phase'])
def test_material_selection_rejects_wrong_scope_identity_or_head(tmp_path, change):
    store = RemixArtifactStore(tmp_path, 'p', created_by='u')
    c, p = confirmed_plan(store)
    work_ref = selectable_work(store, p)
    canvas = selection_canvas(p)
    if change == 'project':
        work_ref['project_id'] = 'other'
    elif change == 'canvas':
        canvas['canvas_id'] = 'other'
    elif change == 'private':
        canvas.update(access_model='private', owner_principal_id='other')
    elif change == 'creator':
        other_store = RemixArtifactStore(tmp_path, 'p', created_by='other')
        work_ref = selectable_work(other_store, p, 'other-creator')
    elif change == 'plan':
        other = store.create('production_plan.v2', p['payload'], [])
        work_ref = selectable_work(store, other, 'other-plan')
    elif change == 'plan_node':
        canvas['edges'] = []
    elif change == 'material_node':
        work = store.read(work_ref['artifact_id'], 1)
        work['payload']['steps'][0]['node_id'] = 'absent'
        changed = store.revise(work['id'], 1, work['payload'], [work['payload']['plan_ref']])
        work_ref['revision'] = changed['revision']
    elif change == 'stale_work':
        work = store.read(work_ref['artifact_id'], 1)
        store.revise(work['id'], 1, work['payload'], [work['payload']['plan_ref']])
    elif change == 'stale_plan':
        draft = p['payload']
        draft['beats'][0]['purpose'] = 'unapproved'
        store.revise(p['id'], 1, draft, [])
    elif change == 'phase':
        c = commission.advance_reference_commission(store, c['commission_ref'], {'type': 'speech_aligned', 'plan_ref': c['plan_ref']}, c['commission_ref']['revision'])
    with pytest.raises((ValueError, PermissionError)):
        select(store, c, work_ref, canvas)
    assert commission.read_reference_commission(store, c['commission_ref'], latest=True).get('work_ref') is None


def test_material_selection_accepts_compatible_media_timing_revision_but_checks_canvas_head(tmp_path):
    store = RemixArtifactStore(tmp_path, 'p', created_by='u')
    c, p = confirmed_plan(store)
    work = selectable_work(store, p)
    draft = p['payload']
    draft['beats'][0]['duration_frames'] = 96
    p = store.revise(p['id'], 1, draft, [])
    with pytest.raises(RemixArtifactConflict, match='canvas'):
        select(store, c, work, selection_canvas({**p, 'revision': 1}))
    result = select(store, c, work, selection_canvas(p))
    assert result['work_ref']['artifact_id'] == work['artifact_id']
    assert result['plan_ref']['revision'] == 2


def test_material_selection_event_requires_authenticated_host(tmp_path):
    store = RemixArtifactStore(tmp_path, 'p', created_by='u')
    c, p = confirmed_plan(store)
    work = selectable_work(store, p)
    with pytest.raises(PermissionError):
        commission.advance_reference_commission(store, c['commission_ref'],
            {'type': 'material_work_selected', 'work_ref': work, 'command_id': 'event'}, c['commission_ref']['revision'])


def test_material_ready_cannot_silently_replace_an_explicit_selection(tmp_path):
    store, c, bound, accepted, _ = confirmed_bound_picture(tmp_path)
    other = selectable_work(store, bound, 'other-incomplete')
    c = select(store, c, other, selection_canvas(bound))
    with pytest.raises(ValueError, match='selected work'):
        commission.advance_reference_commission(store, c['commission_ref'],
            {'type': 'materials_ready', 'work_ref': accepted['work_ref']}, c['commission_ref']['revision'])
    assert commission.read_reference_commission(store, c['commission_ref'], latest=True)['work_ref'] == c['work_ref']


def test_legacy_work_proposal_is_read_only_and_requires_unique_provable_candidate(tmp_path):
    store = RemixArtifactStore(tmp_path, 'p', created_by='u')
    c, p = confirmed_plan(store)
    one, two = selectable_work(store, p), selectable_work(store, p, 'second-legacy')
    foreign = selectable_work(RemixArtifactStore(tmp_path, 'p', created_by='other'), p, 'foreign-legacy')
    args = dict(host_evidence=commission.AuthenticatedDecisionEvidence('p', 'c', 'u', 'legacy'), canvas=selection_canvas(p))
    unique = commission.propose_material_work_selection(store, c['commission_ref'], [one, foreign], **args)
    assert unique['work_ref'] is None
    assert unique['suggested_work_ref'] == {**one, 'project_id': 'p'}
    ambiguous = commission.propose_material_work_selection(store, c['commission_ref'], [one, two], **args)
    assert ambiguous['suggested_work_ref'] is None
    assert ambiguous['blockers'] == [{'owner': 'user', 'kind': 'work_selection_required', 'action_id': None}]
    assert commission.read_reference_commission(store, c['commission_ref'], latest=True)['commission_ref'] == c['commission_ref']


def test_selection_canvas_rejects_acceptance_from_another_canvas(tmp_path):
    store, c, plan, accepted, _ = confirmed_bound_picture(tmp_path)
    work = store.read(accepted['work_ref']['artifact_id'], accepted['work_ref']['revision'])
    canvas = selection_canvas(plan)
    canvas['canvas_id'] = 'other'
    canvas['nodes'].append({'id': 'image-node', 'type': 'imageNode', 'data': {}})
    with pytest.raises(PermissionError, match='acceptance'):
        commission._validate_selection_canvas(store, {**c, 'canvas_id': 'other'}, plan, work, canvas)


def test_concurrent_selection_commands_share_cas_and_durable_receipt(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store = RemixArtifactStore(tmp_path, 'p', created_by='u')
    c, p = confirmed_plan(store)
    work = selectable_work(store, p)
    canvas = selection_canvas(p)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: select(store, c, work, canvas, 'same-command'), range(2)))
    assert results[0] == results[1]
    assert results[0]['commission_ref']['revision'] == c['commission_ref']['revision'] + 1
    assert len([row for row in results[0]['decisions'] if row['type'] == 'material_work_selected']) == 1


def test_outline_ready_keeps_commission_out_of_full_plan_review(tmp_path):
    store = RemixArtifactStore(tmp_path, 'p')
    current = commission.create_reference_commission(store, canvas_id='c', requester_user_id='u', analysis_node_id='a', goal='g', analysis_evidence={'task_id': 't', 'result_fingerprint': 'h'})
    current = commission.advance_reference_commission(store, current['commission_ref'], {'type': 'direction_confirmed', 'direction': {'keep': ['story']}}, 1,
        host_evidence=commission.AuthenticatedDecisionEvidence('p', 'c', 'u', 'm'))
    saved = store.create('production_plan.v2', {'schema_version': 2, 'authoring_stage': 'outline', 'goal': 'g',
        'output': {'width': 720, 'height': 1280, 'fps_num': 30, 'fps_den': 1},
        'beats': [{'id': 'b1', 'purpose': 'opening', 'duration_frames': 120}]}, [])
    current = commission.advance_reference_commission(store, current['commission_ref'], {'type': 'plan_ready',
        'plan_ref': {'artifact_id': saved['id'], 'revision': saved['revision']}}, 2)
    assert current['phase'] == 'drafting_plan'
    assert current['plan_ref']['artifact_id'] == saved['id']
    assert current['next_action']['kind'] == 'inspect_outline'
