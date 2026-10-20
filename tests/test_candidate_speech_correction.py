from copy import deepcopy

import pytest

from test_source_speech_api import setup_candidate


def candidate(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.material_work import prepare_material_work
    from novelvideo.freezone.remix.source_speech import observe_source_speech, candidate_scope
    ctx, canvas, store, ref, resource = setup_candidate(tmp_path, monkeypatch)
    work = store.read(ref["artifact_id"], ref["revision"])["payload"]
    plan = store.read(work["plan_ref"]["artifact_id"], work["plan_ref"]["revision"])
    plan["payload"]["beats"][0]["turns"] = [
        {"id": "guest", "speaker": "嘉宾", "text": "黑咖啡，没有之一。"}]
    plan = store.revise(plan["id"], plan["revision"], plan["payload"], [])
    plan_ref = {"artifact_id": plan["id"], "revision": plan["revision"]}
    canvas["nodes"][0]["data"]["productionPlanV2Ref"]["revision"] = plan["revision"]
    prepared = prepare_material_work(store, plan_ref,
        [{**work["requirements"][0], "turn_ids": ["guest"]}],
        [{"id": "video", "label": "对白", "media_kind": "video", "source": "existing", "node_id": "n"}],
        "spoken-candidate")
    class Engine:
        def transcribe(self, path, language):
            return {"status": "completed", "words": [
                {"text": "黑咖啡", "start": 0, "end": 1},
                {"text": "没有", "start": 1.1, "end": 2},
                {"text": "知一", "start": 2.1, "end": 3.56}]}
    observation_ref = observe_source_speech(store,
        scope=candidate_scope(ctx, canvas, "canvas"), resource=resource,
        language="zh", provider_signature="provider", aligner=Engine())
    return ctx, canvas, store, prepared["work_ref"], observation_ref


def test_candidate_review_and_saved_correction_preserve_raw_evidence(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.candidate_speech_correction import (
        read_candidate_speech_review, save_candidate_speech_correction)
    ctx, canvas, store, work_ref, observation_ref = candidate(tmp_path, monkeypatch)
    kwargs = dict(canvas_id="canvas", work_ref=work_ref, step_id="video",
                  node_id="n", observation_ref=observation_ref)
    review = read_candidate_speech_review(store, ctx, canvas, **kwargs)
    assert review["summary"]["transcript_match"] == "review_required"
    assert review["summary"]["audio_review"] == "unavailable"
    raw = deepcopy(store.read(observation_ref["artifact_id"], 1))
    word = review["summary"]["recognized_words"][-1]
    saved = save_candidate_speech_correction(store, ctx, canvas, **kwargs,
        target_signature=review["summary"]["target_signature"],
        corrections=[{"word_id": word["id"], "text": "之一", "reason": "试听后纠正同音字"}],
        confirmed=True, command_id="correct-once", actor_kind="user")
    payload = saved["correction"]["payload"]
    assert payload["corrected_words"][-1]["text"] == "之一"
    assert payload["corrected_words"][-1]["start_us"] == word["start_us"]
    assert payload["corrected_words"][-1]["end_us"] == word["end_us"]
    assert store.read(observation_ref["artifact_id"], 1) == raw
    assert store.read(work_ref["artifact_id"], work_ref["revision"])["payload"]["steps"][0]["status"] == "planned"
    replay = save_candidate_speech_correction(store, ctx, canvas, **kwargs,
        target_signature=review["summary"]["target_signature"],
        corrections=[{"word_id": word["id"], "text": "之一", "reason": "试听后纠正同音字"}],
        confirmed=True, command_id="correct-once", actor_kind="user")
    assert replay["correction_ref"] == saved["correction_ref"]
    restored = read_candidate_speech_review(store, ctx, canvas, **kwargs)["summary"]
    assert restored["correction_ref"] == saved["correction_ref"]
    assert restored["corrected_words"][-1]["text"] == "之一"
    assert restored["recognized_words"][-1]["text"] == "知一"


@pytest.mark.parametrize("change", ["unconfirmed", "agent", "target", "word", "still_mismatch", "private"])
def test_correction_requires_exact_current_source_and_real_user(tmp_path, monkeypatch, change):
    from novelvideo.freezone.remix.candidate_speech_correction import (
        read_candidate_speech_review, save_candidate_speech_correction)
    ctx, canvas, store, work_ref, observation_ref = candidate(tmp_path, monkeypatch)
    kwargs = dict(canvas_id="canvas", work_ref=work_ref, step_id="video",
                  node_id="n", observation_ref=observation_ref)
    review = read_candidate_speech_review(store, ctx, canvas, **kwargs)
    body = dict(target_signature=review["summary"]["target_signature"], confirmed=True,
        actor_kind="user", command_id="correct",
        corrections=[{"word_id": review["summary"]["recognized_words"][-1]["id"],
                      "text": "之一", "reason": "试听"}])
    if change == "unconfirmed": body["confirmed"] = False
    if change == "agent": body["actor_kind"] = "agent"
    if change == "target": body["target_signature"] = "stale"
    if change == "word": body["corrections"][0]["word_id"] = "fake"
    if change == "still_mismatch": body["corrections"][0]["text"] = "不对"
    if change == "private": canvas["owner_principal_id"] = "other"
    with pytest.raises(ValueError):
        save_candidate_speech_correction(store, ctx, canvas, **kwargs, **body)


def test_changed_command_cannot_replace_existing_user_correction(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.candidate_speech_correction import (
        read_candidate_speech_review, save_candidate_speech_correction)
    ctx, canvas, store, work_ref, observation_ref = candidate(tmp_path, monkeypatch)
    kwargs = dict(canvas_id="canvas", work_ref=work_ref, step_id="video", node_id="n",
                  observation_ref=observation_ref)
    summary = read_candidate_speech_review(store, ctx, canvas, **kwargs)["summary"]
    body = dict(target_signature=summary["target_signature"], confirmed=True, actor_kind="user",
                command_id="same", corrections=[{"word_id": summary["recognized_words"][-1]["id"],
                                                "text": "之一", "reason": "试听"}])
    save_candidate_speech_correction(store, ctx, canvas, **kwargs, **body)
    body["corrections"][0]["reason"] = "different"
    with pytest.raises(ValueError, match="command"):
        save_candidate_speech_correction(store, ctx, canvas, **kwargs, **body)


def test_detected_language_does_not_change_the_requested_source_key(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.source_speech import observe_source_speech, candidate_scope
    from novelvideo.freezone.remix.candidate_speech_correction import read_candidate_speech_review
    ctx, canvas, store, work_ref, previous = candidate(tmp_path, monkeypatch)
    import novelvideo.freezone.remix.material_execution as execution
    media = execution.canvas_material_resource(store, canvas, "n", "video")
    class Engine:
        def transcribe(self, path, language):
            assert language == "auto"
            return {"status": "completed", "language": "zh", "words": [
                {"text": "黑咖啡没有之一", "start": 0, "end": 3.56}]}
    ref = observe_source_speech(store, scope=candidate_scope(ctx, canvas, "canvas"),
        resource=media, language="auto", provider_signature="provider", aligner=Engine())
    review = read_candidate_speech_review(store, ctx, canvas, canvas_id="canvas",
        work_ref=work_ref, step_id="video", node_id="n", observation_ref=ref)
    assert review["summary"]["transcript_match"] == "normalized_match"
    assert store.read(ref["artifact_id"], 1)["payload"]["requested_language"] == "auto"


def test_candidate_status_finds_actual_observation_without_transcribing(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.candidate_speech_correction import find_candidate_speech_review
    ctx, canvas, store, work_ref, ref = candidate(tmp_path, monkeypatch)
    monkeypatch.setattr("novelvideo.freezone.remix.source_speech.configured_provider_signature", lambda: "provider")
    result = find_candidate_speech_review(store, ctx, canvas, canvas_id="canvas",
        work_ref=work_ref, step_id="video", node_id="n", language="zh")
    assert result["status"] == "ready"
    assert result["summary"]["observation_ref"] == ref
    assert result["summary"]["recognized_text"] == "黑咖啡没有知一"


def test_candidate_status_is_read_only_and_current_scope_guarded(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.candidate_speech_correction import find_candidate_speech_review
    ctx, canvas, store, work_ref, _ = candidate(tmp_path, monkeypatch)
    monkeypatch.setattr('novelvideo.freezone.remix.source_speech.configured_provider_signature', lambda: 'changed-provider')
    before = {str(p): p.read_bytes() for p in store.root.rglob('*') if p.is_file()}
    result = find_candidate_speech_review(store, ctx, canvas, canvas_id='canvas',
        work_ref=work_ref, step_id='video', node_id='n')
    assert result['status'] == 'pending'
    assert {str(p): p.read_bytes() for p in store.root.rglob('*') if p.is_file()} == before
    canvas['owner_principal_id'] = 'other'
    with pytest.raises(ValueError, match='private'):
        find_candidate_speech_review(store, ctx, canvas, canvas_id='canvas',
            work_ref=work_ref, step_id='video', node_id='n')


def test_music_without_dialogue_has_no_required_srt_editor(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.material_work import prepare_material_work
    from novelvideo.freezone.remix.candidate_speech_correction import find_candidate_speech_review
    ctx, canvas, store, old_ref, media = setup_candidate(tmp_path, monkeypatch)
    plan_ref = store.read(old_ref['artifact_id'], old_ref['revision'])['payload']['plan_ref']
    work = prepare_material_work(store, plan_ref,
        [{'id': 'music', 'description': 'background music', 'media_kind': 'audio', 'final_step_id': 'music'}],
        [{'id': 'music', 'label': 'music', 'source': 'existing', 'media_kind': 'audio', 'node_id': 'n'}], 'music-only')['work_ref']
    media['media_kind'] = 'audio'
    def no_asr_configuration():
        raise AssertionError('music status must not require ASR')
    monkeypatch.setattr('novelvideo.freezone.remix.source_speech.configured_provider_signature', no_asr_configuration)
    result = find_candidate_speech_review(store, ctx, canvas, canvas_id='canvas', work_ref=work, step_id='music', node_id='n')
    assert result['status'] == 'unavailable'
    assert '未安排台词' in result['message']
    assert 'summary' not in result
