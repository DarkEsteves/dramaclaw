"""A plan approval can address only a saved, scoped ordinary assistant message."""
from __future__ import annotations

from novelvideo.chat.plan_confirmation_delivery import (
    find_plan_presentation,
    metadata_for_delivered_plan,
    prepare_plan_presentation_target,
)
from novelvideo.chat.store import ChatScope, ChatStore
from test_reference_decision_ingress import setup_plan_pending


def _scope(tmp_path):
    return ChatScope(kind='project', id='p', surface='freezone', canvas_id='c',
                     agent_id='main', state_dir=str(tmp_path / 'state'))


def test_plan_needs_completed_ordinary_reply_before_user_can_approve(tmp_path):
    store, decision, plan_ref = setup_plan_pending(tmp_path, presented=False)
    scope, chat = _scope(tmp_path), ChatStore()
    user = chat.append_message('name', scope, 'user', '确认')
    assert find_plan_presentation(chat, 'name', scope, decision_ref=decision['decision_ref'],
                                  plan_ref=plan_ref, review_ref=store.read_latest(
                                      decision['decision']['commission_ref']['artifact_id'])['payload']['review_ref'],
                                  before_message_id=user['id']) is None


def test_completed_plan_reply_saves_exact_association_and_old_version_cannot_match(tmp_path):
    store, decision, plan_ref = setup_plan_pending(tmp_path, presented=False)
    scope, chat = _scope(tmp_path), ChatStore()
    target = prepare_plan_presentation_target(store,
        decision['decision']['commission_ref'], agent_id='main')
    metadata = metadata_for_delivered_plan(store, target, assistant_delivered=True,
                                            message='这是已审核的作品方案。请告诉我修改意见或确认。')
    assistant = chat.append_message('name', scope, 'assistant',
        '这是已审核的作品方案。请告诉我修改意见或确认。', metadata=metadata)
    user = chat.append_message('name', scope, 'user', '确认')
    found = find_plan_presentation(chat, 'name', scope, decision_ref=decision['decision_ref'],
        plan_ref=plan_ref, review_ref=target['review_ref'], before_message_id=user['id'])
    assert found and found['id'] == assistant['id']
    assert find_plan_presentation(chat, 'name', scope, decision_ref=decision['decision_ref'],
        plan_ref={**plan_ref, 'revision': plan_ref['revision'] + 1},
        review_ref=target['review_ref'], before_message_id=user['id']) is None
    assert metadata_for_delivered_plan(store, target, assistant_delivered=False,
                                      message='操作未完成') == {}
