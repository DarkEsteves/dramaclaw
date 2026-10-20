from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from novelvideo.chat.store import ChatScope, ChatStore
from novelvideo.freezone import canvas_store
from novelvideo.freezone.canvas_context_reader import saved_context_execution
from novelvideo.freezone.remix.reference_commission import create_reference_commission, read_reference_commission
from novelvideo.freezone.remix.store import RemixArtifactStore


@pytest.fixture
def discussion(tmp_path, monkeypatch):
    from novelvideo.chat import reference_remix_continuation as continuation
    from novelvideo.chat.reference_remix_continuation import ReferenceRemixStore

    ctx = SimpleNamespace(state_dir=tmp_path / 'state', output_dir=tmp_path / 'output',
                          project_id='p', requester_user_id='u', requester_username='u')
    store = RemixArtifactStore(ctx.output_dir, 'p', created_by='u')
    commission = create_reference_commission(
        store, canvas_id='c', requester_user_id='u', analysis_node_id='analysis',
        goal='original', analysis_evidence={'task_id': 't', 'result_fingerprint': 'h'},
    )
    runs = ReferenceRemixStore(tmp_path / 'runs.db')
    runs.create(dict(run_id='r', project_id='p', requester_user_id='u', canvas_id='c',
                     agent_id='main', idempotency_key='r', status='awaiting_direction',
                     commission_ref=commission['commission_ref'], commission_phase='awaiting_direction'))
    monkeypatch.setattr(continuation.coordinator, 'store', runs)
    scope = ChatScope(kind='project', id='project-name', surface='freezone', canvas_id='c',
                      agent_id='main', state_dir=str(ctx.state_dir))
    canvas = {'project_id': 'p', 'canvas_id': 'c', 'revision': 1, 'nodes': [], 'edges': []}
    monkeypatch.setattr(canvas_store, 'read_canvas', lambda *_: canvas)
    return ctx, store, commission, scope, canvas


def _bound(ctx, scope, message, turn_id='human-turn'):
    identity = SimpleNamespace(surface='freezone', requester_user_id='u', project_id='p',
                               canvas_id='c', agent_id='main')
    return saved_context_execution(execution_context=identity, turn_id=turn_id,
                                   state_dir=ctx.state_dir, human_message_id=message['id'],
                                   chat_scope_id=scope.id)


async def _commit(ctx, commission, requirements=None, preserve_replace=None, turn_id='human-turn'):
    from novelvideo.chat.reference_direction_commit import ensure_confirmed_direction_for_entry

    return await ensure_confirmed_direction_for_entry(
        ctx=ctx, canvas_id='c', agent_id='main', turn_id=turn_id,
        commission_ref=commission['commission_ref'], goal='保留节奏，换成黑咖啡产品',
        requested_operation={'kind': 'prepare_production_nodes', 'run_after_create': False,
                             'plan_count': 1}, requirements=requirements,
        preserve_replace=preserve_replace,
    )


@pytest.mark.asyncio
async def test_native_submission_uses_deterministic_checks_without_codex(discussion, monkeypatch):
    from novelvideo.chat import reference_direction_commit as boundary

    ctx, store, commission, scope, _ = discussion
    message = ChatStore().append_message(
        'u', scope, 'user', '保留原片人物外观，产品换成黑咖啡。现在创建作品方案节点。',
        turn_id='human-turn',
    )
    async def forbidden(**_kwargs):
        raise AssertionError('native direction admission must not call a second model')
    monkeypatch.setattr(boundary, 'verify_direction_submission', forbidden, raising=False)
    choices = [{'id': 'character:lead', 'action': 'preserve', 'entity_kind': 'character',
                'entity_id': 'lead', 'visual_identity': True, 'evidence': '保留原片人物外观'}]
    with _bound(ctx, scope, message):
        result = await _commit(ctx, commission,
                               requirements={'output_requirements': {'width': 1080, 'height': 1920}},
                               preserve_replace=choices)
    saved = read_reference_commission(store, result['commission_ref'], latest=True)
    assert saved['phase'] == 'drafting_plan'
    assert saved['direction']['requirements'] == {'output_requirements': {'width': 1080, 'height': 1920}}
    assert saved['direction']['preserve_replace'] == choices


@pytest.mark.asyncio
async def test_invalid_typed_requirements_fail_before_publication(discussion):
    ctx, store, commission, scope, _ = discussion
    message = ChatStore().append_message('u', scope, 'user', '现在创建方案节点。', turn_id='human-turn')
    with _bound(ctx, scope, message), pytest.raises(HTTPException) as error:
        await _commit(ctx, commission, requirements={'source_turn_ids': ['invented']})
    assert error.value.status_code == 422
    assert error.value.detail['code'] == 'direction_submission_invalid'
    assert read_reference_commission(store, commission['commission_ref'], latest=True)['direction_revision'] == 0


@pytest.mark.asyncio
async def test_preserve_replace_requires_exact_human_evidence(discussion):
    ctx, store, commission, scope, _ = discussion
    message = ChatStore().append_message('u', scope, 'user', '产品换成黑咖啡。', turn_id='human-turn')
    choice = {'id': 'character:lead', 'action': 'preserve', 'entity_kind': 'character',
              'entity_id': 'lead', 'visual_identity': True, 'evidence': '保留原片人物外观'}
    with _bound(ctx, scope, message), pytest.raises(HTTPException) as error:
        await _commit(ctx, commission, preserve_replace=[choice])
    assert error.value.detail['code'] == 'direction_submission_invalid'
    assert read_reference_commission(store, commission['commission_ref'], latest=True)['direction_revision'] == 0


@pytest.mark.asyncio
async def test_missing_or_changed_human_turn_is_rejected(discussion):
    ctx, store, commission, scope, _ = discussion
    message = ChatStore().append_message('u', scope, 'user', '现在创建方案节点。', turn_id='human-turn')
    with pytest.raises(HTTPException) as error:
        await _commit(ctx, commission, turn_id='forged-turn')
    assert error.value.status_code == 403
    assert read_reference_commission(store, commission['commission_ref'], latest=True)['direction_revision'] == 0
    with _bound(ctx, scope, message):
        ChatStore().append_message('u', scope, 'user', '等等，先别创建。', turn_id='withdrawal')
        with pytest.raises(HTTPException) as changed:
            await _commit(ctx, commission)
    assert changed.value.detail['code'] == 'human_turn_changed'


@pytest.mark.asyncio
async def test_current_direction_goal_mismatch_stays_on_existing_update_flow(discussion):
    from novelvideo.freezone.remix.reference_commission import AuthenticatedDecisionEvidence, advance_reference_commission
    from novelvideo.chat.reference_direction_commit import ensure_confirmed_direction_for_entry

    ctx, store, commission, scope, _ = discussion
    confirmed = advance_reference_commission(
        store, commission['commission_ref'],
        {'type': 'direction_confirmed', 'direction': {'target_goal': '已有目标'}},
        commission['commission_ref']['revision'],
        host_evidence=AuthenticatedDecisionEvidence('p', 'c', 'u', 'receipt'),
    )
    message = ChatStore().append_message('u', scope, 'user', '改目标。', turn_id='human-turn')
    with _bound(ctx, scope, message), pytest.raises(HTTPException) as error:
        await ensure_confirmed_direction_for_entry(
            ctx=ctx, canvas_id='c', agent_id='main', turn_id='human-turn',
            commission_ref=confirmed['commission_ref'], goal='新的目标',
            requested_operation={'kind': 'prepare_production_nodes', 'run_after_create': False, 'plan_count': 1},
        )
    assert error.value.detail['code'] == 'confirmed_goal_changed'
