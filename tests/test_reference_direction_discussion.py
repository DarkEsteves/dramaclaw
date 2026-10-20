from types import SimpleNamespace

import pytest

from novelvideo.chat.store import ChatScope


@pytest.mark.asyncio
@pytest.mark.parametrize('user_text', [
    '如果人物也换掉，会不会失去原来的感觉？',
    '先给我比较两种处理，暂时不要创建。',
    '时长不固定，重点是逻辑完整。音乐我还没想好。',
    '上一条只是举例，先不按那个产品做。',
])
async def test_pending_direction_is_ordinary_dialogue_without_preclassification(
        monkeypatch, user_text):
    from novelvideo.api.routes import chat, reference_director
    from novelvideo.chat import reference_decision_ingress
    scope = ChatScope(kind='project', id='p', surface='freezone',
                      canvas_id='c', agent_id='main')
    async def context(*args, **kwargs):
        return SimpleNamespace(requester_user_id='u'), {}, object()
    def pending(*args, **kwargs):
        return {'kind': 'direction', 'decision_ref': {'artifact_id': 'd', 'revision': 1}}
    def unexpected(*args, **kwargs):
        raise AssertionError('discussion must not classify or save a direction')
    monkeypatch.setattr(reference_director, '_context', context)
    monkeypatch.setattr(reference_decision_ingress, 'pending_user_decision', pending)
    monkeypatch.setattr(reference_decision_ingress, 'current_requirement_amendment', unexpected)
    monkeypatch.setattr(reference_decision_ingress, 'apply_user_confirmation', unexpected)
    monkeypatch.setattr(chat, 'interpret_reference_reply', unexpected, raising=False)
    result = await chat._reference_user_confirmation_context(
        user={'credential_kind': 'browser_session'}, scope=scope,
        message={'id': 12, 'role': 'user', 'content': user_text},
        agent_text=user_text, project_id='p', username='u', turn_id='turn-a',
    )
    assert result == user_text
