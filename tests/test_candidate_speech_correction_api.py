import pytest
from fastapi import HTTPException

from test_candidate_speech_correction import candidate


@pytest.mark.asyncio
async def test_agent_cannot_claim_listening_confirmation():
    from novelvideo.api.routes import reference_material as routes

    body = routes.MaterialSpeechCorrectionBody(work_ref={"artifact_id": "w", "revision": 1},
        step_id="s", node_id="n", observation_ref={"artifact_id": "o", "revision": 1},
        target_signature="t", confirmed=True, command_id="c",
        corrections=[{"word_id": "word", "text": "文字", "reason": "核对"}])
    with pytest.raises(HTTPException) as caught:
        await routes.material_speech_correction("p", "c", body,
                                               {"credential_kind": "agent_session"})
    assert caught.value.status_code == 403


@pytest.mark.asyncio
async def test_current_canvas_is_rechecked_before_user_correction(tmp_path, monkeypatch):
    from novelvideo.api.routes import reference_material as routes
    from novelvideo.freezone.remix.candidate_speech_correction import read_candidate_speech_review
    ctx, canvas, store, ref, observation = candidate(tmp_path, monkeypatch)
    summary = read_candidate_speech_review(store, ctx, canvas, canvas_id="canvas",
        work_ref=ref, step_id="video", node_id="n", observation_ref=observation)["summary"]
    async def scope(*args, **kwargs):
        return ctx, canvas, store, {}
    monkeypatch.setattr(routes, "_scope", scope)
    monkeypatch.setattr("novelvideo.freezone.canvas_store.read_canvas",
                        lambda *args: {**canvas, "owner_principal_id": "other"})
    body = routes.MaterialSpeechCorrectionBody(work_ref=ref, step_id="video", node_id="n",
        observation_ref=observation, target_signature=summary["target_signature"], confirmed=True,
        command_id="c", corrections=[{"word_id": summary["recognized_words"][-1]["id"],
                                     "text": "之一", "reason": "试听"}])
    with pytest.raises(HTTPException) as caught:
        await routes.material_speech_correction("project", "canvas", body, {})
    assert caught.value.status_code == 403


@pytest.mark.asyncio
async def test_readonly_status_uses_current_authorized_context(tmp_path, monkeypatch):
    from novelvideo.api.routes import reference_material as routes
    import novelvideo.api.egress_binding as egress_binding
    ctx, canvas, store, ref, observation = candidate(tmp_path, monkeypatch)
    async def scope(*args, **kwargs):
        assert args[3] == ref and args[4] == 'viewer'
        return ctx, canvas, store, {}
    monkeypatch.setattr(routes, '_scope', scope)
    async def local_admission(**_kwargs):
        return None
    monkeypatch.setattr(egress_binding, 'build_request_egress_context', local_admission)
    monkeypatch.setattr('novelvideo.freezone.remix.source_speech.configured_provider_signature', lambda: 'provider')
    result = await routes.material_speech_status('project', 'canvas',
        routes.MaterialSpeechStatusBody(work_ref=ref, step_id='video', node_id='n', language='zh'), {})
    assert result['data']['status'] == 'ready'
    assert result['data']['summary']['observation_ref'] == observation
