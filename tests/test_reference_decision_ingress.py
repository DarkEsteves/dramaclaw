import pytest

from novelvideo.chat.reference_decision_ingress import apply_user_confirmation, pending_user_decision
from novelvideo.chat.store import ChatScope, ChatStore
from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.freezone.remix.reference_commission import create_reference_commission
from novelvideo.freezone.remix.reference_director import prepare_reference_decision, list_director_artifacts


def setup_pending(tmp_path, *, canvas='c', actor='u', suffix='a'):
    store = RemixArtifactStore(tmp_path / 'output', 'p', created_by=actor)
    c = create_reference_commission(store, canvas_id=canvas, requester_user_id=actor,
        analysis_node_id=suffix, goal='g', analysis_evidence={'task_id': suffix, 'result_fingerprint': 'h'})
    d = prepare_reference_decision(store, c['commission_ref'], 'direction', {'direction': '保留节奏'}, '方向', suffix)
    return store, d


def message(tmp_path, content='确认方向', role='user'):
    scope = ChatScope(kind='project', id='p', surface='freezone', canvas_id='c', state_dir=str(tmp_path / 'state'))
    return scope, ChatStore().append_message('name', scope, role, content)


def apply(store, scope, msg, **kwargs):
    credential_kind = kwargs.pop('credential_kind', 'browser_session')
    decision = pending_user_decision(store, scope=scope, requester_user_id='u',
                                     credential_kind=credential_kind, message=msg)
    kwargs.setdefault('interpretation', {'intent': 'approve'})
    kwargs.setdefault('expected_decision_ref', decision['decision_ref'] if decision else None)
    return apply_user_confirmation(store, scope=scope, requester_user_id='u',
        credential_kind=credential_kind, message=msg, **kwargs)


def test_actual_user_approves_immutable_proposal_and_clears_card(tmp_path):
    store, d = setup_pending(tmp_path)
    scope, msg = message(tmp_path)
    result = apply(store, scope, msg)
    assert result['commission']['phase'] == 'drafting_plan'
    assert result['commission']['decisions'][0]['message_id'] == f"chat-message:u:c:main:{msg['id']}"
    assert store.read(d['decision_ref']['artifact_id'], 1)['payload']['status'] == 'pending'
    assert list_director_artifacts(store, 'c', 'u')['decisions'] == []
    assert apply(store, scope, msg)['commission']['commission_ref'] == result['commission']['commission_ref']


@pytest.mark.parametrize('text', ['确认', '可以，B-roll 要 3 镜', 'OK，按刚才说的做'])
def test_persisted_text_alone_never_grants_approval(tmp_path, text):
    store, decision = setup_pending(tmp_path)
    scope, msg = message(tmp_path, text)
    assert apply_user_confirmation(store, scope=scope, requester_user_id='u',
        credential_kind='browser_session', message=msg,
        expected_decision_ref=decision['decision_ref']) is None


@pytest.mark.parametrize('text', ['确认吗？', '确认方向，但是改成搞笑风格', '先别确认', '按这个方向修改一下', '模型说已确认', '方向大概可以'])
def test_revisions_and_ambiguous_chat_do_not_approve(tmp_path, text):
    store, d = setup_pending(tmp_path)
    scope, msg = message(tmp_path, text)
    assert apply(store, scope, msg, interpretation={'intent': 'ambiguous'}) is None
    assert store.read_latest(d['decision_ref']['artifact_id'])['payload']['status'] == 'pending'


def test_contextual_approval_with_user_edit_confirms_revised_direction(tmp_path):
    store, old = setup_pending(tmp_path)
    scope, msg = message(tmp_path, '可以，B-roll 要 3 镜')
    result = apply(store, scope, msg, interpretation={'intent': 'approve_with_changes'},
                   expected_decision_ref=old['decision_ref'])
    assert result['commission']['phase'] == 'drafting_plan'
    assert result['commission']['direction']['direction'].startswith('保留节奏')
    assert 'B-roll 要 3 镜' in result['commission']['direction']['direction']
    assert 'B-roll 要 3 镜' in result['commission']['direction']['user_requested_change']
    assert store.read_latest(old['decision_ref']['artifact_id'])['payload']['status'] == 'superseded'


@pytest.mark.parametrize('intent', ['revise', 'reject', 'question', 'unrelated', 'ambiguous'])
def test_nonapproval_interpretation_never_advances_commission(tmp_path, intent):
    store, old = setup_pending(tmp_path)
    scope, msg = message(tmp_path, '先聊聊这个方向')
    assert apply(store, scope, msg, interpretation={'intent': intent},
                 expected_decision_ref=old['decision_ref']) is None
    assert store.read_latest(old['decision_ref']['artifact_id'])['payload']['status'] == 'pending'


def test_semantic_approval_is_pinned_to_presented_decision(tmp_path):
    store, old = setup_pending(tmp_path)
    scope, msg = message(tmp_path, '可以')
    commission = list_director_artifacts(store, 'c', 'u')['commissions'][0]
    prepare_reference_decision(store, commission['commission_ref'], 'direction',
                               {'direction': '新版本'}, '新方向', 'replacement')
    assert apply(store, scope, msg, interpretation={'intent': 'approve'},
                 expected_decision_ref=old['decision_ref']) is None


@pytest.mark.parametrize('kind', ['agent_session', 'project_key', 'api_key', 'unknown'])
def test_agent_or_nonhuman_credentials_do_not_approve(tmp_path, kind):
    store, _ = setup_pending(tmp_path)
    scope, msg = message(tmp_path)
    assert apply(store, scope, msg, credential_kind=kind) is None


def test_assistant_message_does_not_approve(tmp_path):
    store, _ = setup_pending(tmp_path)
    scope, msg = message(tmp_path, role='assistant')
    assert apply(store, scope, msg) is None


def test_multiple_matching_proposals_do_not_guess(tmp_path):
    store, _ = setup_pending(tmp_path)
    setup_pending(tmp_path, suffix='b')
    scope, msg = message(tmp_path, '确认')
    assert apply(store, scope, msg) is None


def test_other_actor_canvas_and_director_surface_do_not_match(tmp_path):
    store, _ = setup_pending(tmp_path, actor='other')
    setup_pending(tmp_path, canvas='other', suffix='b')
    scope, msg = message(tmp_path)
    assert apply(store, scope, msg) is None
    assert apply(store, ChatScope(kind='project', id='p', surface='director'), msg) is None


@pytest.mark.asyncio
async def test_route_hook_keeps_direction_reply_as_original_chat(tmp_path, monkeypatch):
    from novelvideo.api.routes import chat, reference_director
    from types import SimpleNamespace
    store, _ = setup_pending(tmp_path)
    scope, msg = message(tmp_path)
    async def context(*args, **kwargs):
        return SimpleNamespace(requester_user_id='u'), {}, store
    monkeypatch.setattr(reference_director, '_context', context)
    async def interpret(**kwargs):
        raise AssertionError('direction discussion must not call the interpreter')
    monkeypatch.setattr(chat, 'interpret_reference_reply', interpret, raising=False)
    text = await chat._reference_user_confirmation_context(
        user={'credential_kind':'browser_session'}, scope=scope, message=msg, agent_text='确认方向')
    assert text == '确认方向'
    assert list_director_artifacts(store, 'c', 'u')['commissions'][0]['phase'] == 'awaiting_direction'


@pytest.mark.asyncio
async def test_route_hook_uses_canonical_project_scope_when_chat_storage_uses_project_name(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from novelvideo.api.routes import chat, reference_director
    store, _ = setup_pending(tmp_path)
    scope, msg = message(tmp_path, '确认')
    storage_scope = replace(scope, id='project-name')

    async def context(*args, **kwargs):
        assert args[:2] == ('p', 'c')
        return SimpleNamespace(requester_user_id='u'), {}, store

    monkeypatch.setattr(reference_director, '_context', context)
    async def interpret(**kwargs):
        raise AssertionError('direction discussion must not call the interpreter')
    monkeypatch.setattr(chat, 'interpret_reference_reply', interpret, raising=False)
    text = await chat._reference_user_confirmation_context(
        user={'credential_kind': 'browser_session'}, scope=storage_scope,
        message=msg, agent_text='确认', project_id=scope.id,
    )
    assert text == '确认'
    assert list_director_artifacts(store, 'c', 'u')['commissions'][0]['phase'] == 'awaiting_direction'


@pytest.mark.asyncio
async def test_route_hook_errors_preserve_normal_user_chat(tmp_path, monkeypatch, caplog):
    from novelvideo.api.routes import chat, reference_director
    scope, msg = message(tmp_path)
    async def denied(*args, **kwargs):
        raise PermissionError('decision scope denied')
    monkeypatch.setattr(reference_director, '_context', denied)
    text = await chat._reference_user_confirmation_context(
        user={'credential_kind':'browser_session'}, scope=scope, message=msg, agent_text='确认方向')
    assert text == '确认方向'
    assert 'reference director confirmation ingress failed' in caplog.text


@pytest.mark.asyncio
async def test_direction_discussion_does_not_need_interpreter(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from novelvideo.api.routes import chat, reference_director
    store, _ = setup_pending(tmp_path)
    scope, msg = message(tmp_path, '可以 B-roll 要 3 镜')

    async def context(*args, **kwargs):
        return SimpleNamespace(requester_user_id='u'), {}, store

    async def unavailable(**kwargs):
        raise RuntimeError('model unavailable')

    monkeypatch.setattr(reference_director, '_context', context)
    monkeypatch.setattr(chat, 'interpret_reference_reply', unavailable, raising=False)
    text = await chat._reference_user_confirmation_context(
        user={'credential_kind': 'browser_session'}, scope=scope,
        message=msg, agent_text=msg['content'],
    )
    assert text == msg['content']
    assert list_director_artifacts(store, 'c', 'u')['commissions'][0]['phase'] == 'awaiting_direction'


def test_chat_ids_from_different_agents_are_not_replayed(tmp_path):
    from dataclasses import replace
    store, _ = setup_pending(tmp_path)
    scope, msg = message(tmp_path)
    first = apply(store, scope, msg)
    store2, _ = setup_pending(tmp_path, suffix='b')
    second = apply(store2, replace(scope, agent_id='agent-2'), msg)
    assert second['commission']['commission_ref']['artifact_id'] != first['commission']['commission_ref']['artifact_id']


def test_explicit_remix_direction_is_a_saved_card_before_requesting_confirmation(tmp_path):
    from novelvideo.chat.reference_decision_ingress import ensure_explicit_direction_decision
    store = RemixArtifactStore(tmp_path / 'output', 'p', created_by='u')
    commission = create_reference_commission(store, canvas_id='c', requester_user_id='u',
        analysis_node_id='analysis', goal='黑咖啡复刻',
        analysis_evidence={'task_id': 't', 'result_fingerprint': 'h'})
    run = {'run_id': 'run-coffee', 'project_id': 'p', 'canvas_id': 'c', 'requester_user_id': 'u',
           'analysis_node_id': 'analysis', 'goal': '黑咖啡复刻',
           'inputs': {'remix_direction': '产品换成黑咖啡，对话重写，其他不变'}}
    first = ensure_explicit_direction_decision(store, commission, run)
    assert first['created'] is True
    assert first['decision']['proposal']['remix_direction'] == run['inputs']['remix_direction']
    second = ensure_explicit_direction_decision(store, commission, run)
    assert second['created'] is False
    assert second['decision_ref'] == first['decision_ref']
    assert len(list_director_artifacts(store, 'c', 'u')['decisions']) == 1
    scope, msg = message(tmp_path, '确认')
    assert apply(store, scope, msg)['commission']['phase'] == 'drafting_plan'


def test_generic_remix_does_not_invent_direction_card(tmp_path):
    from novelvideo.chat.reference_decision_ingress import ensure_explicit_direction_decision
    store = RemixArtifactStore(tmp_path / 'output', 'p', created_by='u')
    commission = create_reference_commission(store, canvas_id='c', requester_user_id='u',
        analysis_node_id='analysis', goal='复刻',
        analysis_evidence={'task_id': 't', 'result_fingerprint': 'h'})
    run = {'run_id': 'run-generic', 'project_id': 'p', 'canvas_id': 'c', 'requester_user_id': 'u',
           'analysis_node_id': 'analysis', 'goal': '复刻', 'inputs': {}}
    assert ensure_explicit_direction_decision(store, commission, run) is None
    assert list_director_artifacts(store, 'c', 'u')['decisions'] == []


def test_explicit_direction_replaces_stale_pending_proposal(tmp_path):
    from novelvideo.chat.reference_decision_ingress import ensure_explicit_direction_decision
    store = RemixArtifactStore(tmp_path / 'output', 'p', created_by='u')
    commission = create_reference_commission(store, canvas_id='c', requester_user_id='u',
        analysis_node_id='analysis', goal='复刻',
        analysis_evidence={'task_id': 't', 'result_fingerprint': 'h'})
    old = prepare_reference_decision(store, commission['commission_ref'], 'direction',
        {'remix_direction': '保留原产品'}, '保留原产品', 'old-direction')
    run = {'run_id': 'new-direction', 'project_id': 'p', 'canvas_id': 'c',
           'requester_user_id': 'u', 'analysis_node_id': 'analysis', 'goal': '复刻',
           'inputs': {'remix_direction': '产品换成黑咖啡，对话重写'}}
    new = ensure_explicit_direction_decision(store, commission, run)
    assert new['created'] is True
    assert store.read_latest(old['decision_ref']['artifact_id'])['payload']['status'] == 'superseded'
    pending = list_director_artifacts(store, 'c', 'u')['decisions']
    assert [row['decision_ref']['artifact_id'] for row in pending] == [new['decision_ref']['artifact_id']]
    scope, msg = message(tmp_path, '确认')
    result = apply(store, scope, msg)
    assert result['commission']['phase'] == 'drafting_plan'
    assert result['commission']['direction']['remix_direction'] == run['inputs']['remix_direction']
    with pytest.raises(ValueError, match='superseded'):
        from novelvideo.freezone.remix.reference_director import confirm_reference_decision
        confirm_reference_decision(store, old['decision_ref'], project_id='p', canvas_id='c', requester_user_id='u')


def test_agent_revised_direction_replaces_previous_pending_proposal(tmp_path):
    store, old = setup_pending(tmp_path)
    commission = list_director_artifacts(store, 'c', 'u')['commissions'][0]
    new = prepare_reference_decision(store, commission['commission_ref'], 'direction',
        {'direction': '保留节奏，产品改成黑咖啡'}, '黑咖啡方向', 'revised-direction')
    assert store.read_latest(old['decision_ref']['artifact_id'])['payload']['status'] == 'superseded'
    assert [row['decision_ref']['artifact_id'] for row in list_director_artifacts(store, 'c', 'u')['decisions']] == [new['decision_ref']['artifact_id']]
    scope, msg = message(tmp_path, '确认')
    assert apply(store, scope, msg)['commission']['direction'] == new['decision']['proposal']


def setup_plan_pending(tmp_path, *, presented=True):
    from novelvideo.freezone.remix import reference_commission as cmod
    from novelvideo.freezone.remix.production_review import review_production_plan, REVIEW_CATEGORIES
    store, decision = setup_pending(tmp_path)
    c = cmod.advance_reference_commission(store, decision['decision']['commission_ref'],
        {'type':'direction_confirmed','direction':{'target_goal':'g'}}, 1,
        host_evidence=cmod.AuthenticatedDecisionEvidence('p','c','u','original-direction'))
    p = store.create('production_plan.v2', {'schema_version':2,'goal':'g',
        'output':{'width':1920,'height':1080,'fps_num':24,'fps_den':1},
        'beats':[{'id':'b','purpose':'intro','duration_frames':48}]}, [])
    pref = {'artifact_id':p['id'],'revision':1,'project_id':'p'}
    c = cmod.advance_reference_commission(store,c['commission_ref'],{'type':'plan_ready','plan_ref':pref},2)
    findings = [{'category':x,'entity_kind':'beat','entity_id':'b','verdict':'pass',
        'reason':'Concrete beat meets confirmed direction','evidence':['b:intro']} for x in REVIEW_CATEGORIES]
    r = review_production_plan(store,pref,c['commission_ref'],findings)
    c = cmod.advance_reference_commission(store,c['commission_ref'],
        {'type':'plan_reviewed','review_ref':{'artifact_id':r['id'],'revision':1,'project_id':'p'}},3)
    d = prepare_reference_decision(store,c['commission_ref'],'plan',{'plan_ref':pref},'制作方案','plan')
    if presented:
        from novelvideo.chat.plan_confirmation_delivery import (
            metadata_for_delivered_plan, prepare_plan_presentation_target,
        )
        target = prepare_plan_presentation_target(store, c['commission_ref'], agent_id='main')
        scope = ChatScope(kind='project', id='p', surface='freezone', canvas_id='c',
                          state_dir=str(tmp_path / 'state'))
        text = '当前作品方案已审核：开场产品介绍，横屏 24fps。请确认或提出修改意见。'
        ChatStore().append_message('name', scope, 'assistant', text,
            metadata=metadata_for_delivered_plan(store, target, assistant_delivered=True,
                                                 message=text))
    return store, d, pref


def test_unpresented_plan_cannot_be_confirmed_from_a_short_user_reply(tmp_path):
    store, decision, _ = setup_plan_pending(tmp_path, presented=False)
    scope, msg = message(tmp_path, '确认')
    assert pending_user_decision(store, scope=scope, requester_user_id='u',
        credential_kind='browser_session', message=msg) is None
    assert apply_user_confirmation(store, scope=scope, requester_user_id='u',
        credential_kind='browser_session', message=msg, interpretation={'intent': 'approve'},
        expected_decision_ref=decision['decision_ref']) is None


@pytest.mark.asyncio
async def test_route_interprets_plan_reply_against_its_exact_presented_message(tmp_path, monkeypatch):
    from novelvideo.api.routes import chat, reference_director
    from types import SimpleNamespace

    store, _, _ = setup_plan_pending(tmp_path)
    scope = ChatScope(kind='project', id='p', surface='freezone', canvas_id='c',
                      state_dir=str(tmp_path / 'state'))
    ChatStore().append_message('name', scope, 'assistant', '这是一条后续的无关提示。')
    _, msg = message(tmp_path, '我希望改一下开场镜头')

    async def context(*_args, **_kwargs):
        return SimpleNamespace(requester_user_id='u'), {}, store

    async def interpret(**_kwargs):
        raise AssertionError('ordinary plan replies must not call a second model')

    monkeypatch.setattr(reference_director, '_context', context)
    monkeypatch.setattr(chat, 'interpret_reference_reply', interpret, raising=False)
    result = await chat._reference_user_confirmation_context(
        user={'credential_kind': 'browser_session'}, scope=scope,
        message=msg, agent_text=msg['content'], username='name',
    )
    assert 'HOST_REFERENCE_DECISION_CONTEXT' in result
    assert 'confirm_exact_presented_plan' in result
    assert list_director_artifacts(store, 'c', 'u')['commissions'][0]['phase'] == 'awaiting_plan_confirmation'


@pytest.mark.asyncio
async def test_plan_confirmation_interpreter_schema_failure_tells_agent_how_to_recover(tmp_path, monkeypatch):
    from novelvideo.api.routes import chat, reference_director
    from types import SimpleNamespace

    store, decision, _ = setup_plan_pending(tmp_path)
    scope, msg = message(tmp_path, '确认')

    async def context(*_args, **_kwargs):
        return SimpleNamespace(requester_user_id='u'), {}, store

    async def interpret(**_kwargs):
        raise RuntimeError("400 invalid_json_schema: Missing 'source_turn_ids'")

    monkeypatch.setattr(reference_director, '_context', context)
    monkeypatch.setattr(chat, 'interpret_reference_reply', interpret, raising=False)
    result = await chat._reference_user_confirmation_context(
        user={'credential_kind': 'browser_session'}, scope=scope,
        message=msg, agent_text=msg['content'], username='name',
    )
    assert '[HOST_REFERENCE_DECISION_CONTEXT]' in result
    assert 'confirmation_applied' in result
    assert '[HOST_REFERENCE_DECISION_RECEIPT]' not in result
    assert store.read_latest(decision['decision_ref']['artifact_id'])['payload']['status'] == 'pending'


@pytest.mark.asyncio
async def test_plan_confirmation_invalid_model_output_is_not_called_user_ambiguity(tmp_path, monkeypatch):
    from novelvideo.api.routes import chat, reference_director
    from types import SimpleNamespace

    store, decision, _ = setup_plan_pending(tmp_path)
    scope, msg = message(tmp_path, '确认')

    async def context(*_args, **_kwargs):
        return SimpleNamespace(requester_user_id='u'), {}, store

    async def interpret(**_kwargs):
        return None

    monkeypatch.setattr(reference_director, '_context', context)
    monkeypatch.setattr(chat, 'interpret_reference_reply', interpret, raising=False)
    result = await chat._reference_user_confirmation_context(
        user={'credential_kind': 'browser_session'}, scope=scope,
        message=msg, agent_text=msg['content'], username='name',
    )
    assert '[HOST_REFERENCE_DECISION_CONTEXT]' in result
    assert 'confirmation_applied' in result
    assert '[HOST_REFERENCE_DECISION_RECEIPT]' not in result
    assert store.read_latest(decision['decision_ref']['artifact_id'])['payload']['status'] == 'pending'


@pytest.mark.parametrize('text', ['确认方案', '方案没问题，开始制作', '确认'])
def test_plan_approval_keeps_old_direction_and_exact_reviewed_plan(tmp_path, text):
    store, d, pref = setup_plan_pending(tmp_path)
    scope, msg = message(tmp_path, text)
    result = apply(store, scope, msg)
    assert result['commission']['phase'] == 'preparing_materials'
    assert result['commission']['plan_ref'] == pref
    assert result['commission']['decisions'][0]['message_id'] == 'original-direction'
    assert result['commission']['decisions'][-1]['plan_ref'] == pref
    assert list_director_artifacts(store, 'c', 'u')['decisions'] == []


def test_plan_change_in_user_reply_requires_new_review(tmp_path):
    store, decision, _ = setup_plan_pending(tmp_path)
    scope, msg = message(tmp_path, '可以，但把第二段改短一点')
    assert apply(store, scope, msg, interpretation={'intent': 'approve_with_changes'},
                 expected_decision_ref=decision['decision_ref']) is None
    assert list_director_artifacts(store, 'c', 'u')['commissions'][0]['phase'] == 'awaiting_plan_confirmation'
