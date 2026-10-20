"""A reviewed plan crosses into chat once, then only a real user can approve it."""
# ruff: noqa: F811
from __future__ import annotations

from test_plan_confirmation_contract import reviewed_plan  # noqa: F401
from test_reference_director_api import setup_api  # noqa: F401

from novelvideo.chat.plan_confirmation_delivery import (
    find_plan_presentation,
    metadata_for_delivered_plan,
    prepare_plan_presentation_target,
)
from novelvideo.chat.reference_decision_ingress import (
    apply_user_confirmation,
    pending_user_decision,
)
from novelvideo.chat.store import ChatScope, ChatStore
from novelvideo.freezone import canvas_store
from novelvideo.freezone.remix.reference_director import list_director_artifacts


def test_reviewed_plan_is_presented_once_and_approved_from_saved_user_reply(
    reviewed_plan, tmp_path,
):
    _, store, commission, _ = reviewed_plan
    scope = ChatScope(kind='project', id='p', surface='freezone', canvas_id='c',
                      state_dir=str(tmp_path))
    chat = ChatStore()
    original_plan_ref = commission['plan_ref']
    original_canvas = canvas_store.read_canvas(tmp_path, 'c')
    target = prepare_plan_presentation_target(
        store, commission['commission_ref'], agent_id='main')
    assert target['plan_ref'] == original_plan_ref
    assert target['review_ref'] == commission['review_ref']

    description = '已审核作品方案：产品开场三秒，横屏 24fps。请确认或告诉我哪里要改。'
    assistant = chat.append_message(
        'alice', scope, 'assistant', description,
        metadata=metadata_for_delivered_plan(
            store, target, assistant_delivered=True, message=description),
        idempotency_key='present-reviewed-plan',
    )
    repeated = chat.append_message(
        'alice', scope, 'assistant', description,
        metadata=metadata_for_delivered_plan(
            store, target, assistant_delivered=True, message=description),
        idempotency_key='present-reviewed-plan',
    )
    assert assistant['id'] == repeated['id']
    current = list_director_artifacts(store, 'c', 'u', limit=None)
    decisions = [row for row in current['decisions'] if row['kind'] == 'plan']
    assert len(decisions) == 1 and decisions[0]['status'] == 'pending'
    assert len([row for row in chat.list_messages('alice', scope)
                if row['role'] == 'assistant' and row['content'] == description]) == 1

    user = chat.append_message('alice', scope, 'user', '这个方案可以，照这个继续。')
    assert find_plan_presentation(
        chat, 'alice', scope, decision_ref=decisions[0]['decision_ref'],
        plan_ref=original_plan_ref, review_ref=commission['review_ref'],
        requester_user_id='u', before_message_id=user['id'],
    )['id'] == assistant['id']
    pending = pending_user_decision(
        store, scope=scope, requester_user_id='u',
        credential_kind='browser_session', message=user,
    )
    assert pending['decision_ref'] == decisions[0]['decision_ref']
    confirmed = apply_user_confirmation(
        store, scope=scope, requester_user_id='u',
        credential_kind='browser_session', message=user,
        interpretation={'intent': 'approve'},
        expected_decision_ref=pending['decision_ref'],
    )
    assert confirmed['commission']['phase'] == 'preparing_materials'
    assert confirmed['commission']['plan_ref'] == original_plan_ref
    assert confirmed['commission']['decisions'][-1]['message_id'] == (
        f"chat-message:u:c:main:{user['id']}")
    assert apply_user_confirmation(
        store, scope=scope, requester_user_id='u',
        credential_kind='browser_session', message=user,
        interpretation={'intent': 'approve'},
        expected_decision_ref=pending['decision_ref'],
    )['replayed'] is True
    assert store.read_latest(original_plan_ref['artifact_id'])['revision'] == original_plan_ref['revision']
    assert canvas_store.read_canvas(tmp_path, 'c') == original_canvas
