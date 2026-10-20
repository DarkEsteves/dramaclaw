from novelvideo.freezone.remix.reference_parameter_receipts import (
    generation_requirements_digest,
    submitted_reference_parameter_receipt,
    reusable_reference_parameters,
    merged_reference_parameter_answers,
    material_parameter_values,
)
from types import SimpleNamespace

import pytest


SCOPE = {
    "project_id": "p", "canvas_id": "c", "agent_id": "main",
    "requester_user_id": "u", "commission_id": "commission",
}


@pytest.mark.anyio
async def test_verified_parameter_receipt_is_saved_before_agent_bridge_wake(monkeypatch, tmp_path):
    from novelvideo.api.routes import chat

    events = []
    payload = chat.ClarificationToolResultIn(
        bridge_key="bridge", project_id="p", canvas_id="c", agent_id="main",
        turn_id="turn", answers={"video_model": "model-a"},
    )
    monkeypatch.setattr(chat, "_prepare_clarification_tool_result_payload",
                        lambda *_args, **_kwargs: {"ok": True, "status": "clarification_frontend_result"})
    async def persist(**_kwargs):
        events.append("persist")
    monkeypatch.setattr(chat, "_persist_reference_parameter_submission", persist)
    def deliver(*_args, **_kwargs):
        events.append("wake")
        return {"ok": True}
    monkeypatch.setattr(chat, "_deliver_clarification_tool_result_payload", deliver)
    assert await chat._resolve_clarification_with_reference_persistence(
        payload=payload, user={"username": "actor"}, username="actor",
        project_state_dir=tmp_path,
    ) == {"ok": True}
    assert events == ["persist", "wake"]
    async def failed_persist(**_kwargs):
        events.append("persist_failed")
        raise OSError("database unavailable")
    monkeypatch.setattr(chat, "_persist_reference_parameter_submission", failed_persist)
    with pytest.raises(OSError, match="database unavailable"):
        await chat._resolve_clarification_with_reference_persistence(
            payload=payload, user={"username": "actor"}, username="actor",
            project_state_dir=tmp_path,
        )
    assert events == ["persist", "wake", "persist_failed"]


@pytest.mark.anyio
async def test_generation_card_binds_to_pending_commission_without_native_plan_claim(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from novelvideo.api.routes import chat
    from novelvideo.freezone import workflow_drafts

    ctx = SimpleNamespace(project_id="p", state_dir=tmp_path, output_dir=tmp_path,
                          requester_user_id="u")
    commission = {"commission_ref": {"artifact_id": "commission", "revision": 1},
                  "direction": {"remix_direction": "保留结构"},
                  "direction_revision": 1, "production_scope": "可编辑合成"}
    run = {"run_id": "run-1", "project_id": "p", "canvas_id": "c", "agent_id": "main",
           "requester_user_id": "u", "commission_ref": commission["commission_ref"],
               "commission_phase": "preparing_materials",
           "inputs": {"video_model": "seedance-2.0-mini"},
           "generation_requirements_digest": "digest",
           "pending_interaction": {"bridge_key": "card-1", "clarification_id": "clarification-1",
                                   "event": {"clarification_id": "clarification-1",
                                             "questions": [{"id": "video_model"}]}}}
    payload = chat.ClarificationToolResultIn(
        bridge_key="card-1", project_id="p", canvas_id="c", agent_id="main",
        turn_id="turn-1", answers={"video_model": "seedance-2.0-mini"},
    )
    event = {"clarification_id": "clarification-1",
             "questions": [{"id": "video_model"}]}
    async def resolve_context(*_args, **_kwargs):
        return ctx

    monkeypatch.setattr(chat, "_project_context_for_interaction_result", resolve_context)
    async def current_owner(*_args, **_kwargs):
        return commission, run

    monkeypatch.setattr(chat, "_current_reference_parameter_owner", current_owner)
    monkeypatch.setattr(chat, "read_bridge_message",
                        lambda *_args, **_kwargs: {"event": event})
    monkeypatch.setattr(workflow_drafts, "read_native_plan_claim",
                        lambda **_kwargs: None)
    persisted = []
    original = workflow_drafts.record_reference_parameter_submission
    monkeypatch.setattr(workflow_drafts, "record_reference_parameter_submission",
                        lambda **kwargs: persisted.append(kwargs))

    await chat._persist_reference_parameter_submission(
        user={"username": "u"}, payload=payload,
        result={"ok": True, "status": "clarification_frontend_result",
                "answers": payload.answers},
        project_state_dir=tmp_path,
    )

    assert persisted
    assert persisted[0]["scope"]["commission_id"] == "commission"
    assert persisted[0]["requirements_digest"] == "digest"
    assert persisted[0]["interaction"]["result"]["answers"] == payload.answers
    assert workflow_drafts.record_reference_parameter_submission is not original


def test_parameter_receipt_reuses_only_matching_submitted_scope_and_requirements():
    receipt = {
        "status": "submitted", "scope": SCOPE, "requirements_digest": "req-1",
        "message_id": "bridge-1", "interaction_id": "question-1",
        "answers": {"video_generate_audio": True},
    }
    assert reusable_reference_parameters(receipt, SCOPE, "req-1") == {"video_generate_audio": True}
    assert reusable_reference_parameters(receipt, SCOPE, "req-2") is None
    assert reusable_reference_parameters(receipt, {**SCOPE, "commission_id": "other"}, "req-1") is None
    assert reusable_reference_parameters({**receipt, "status": "pending"}, SCOPE, "req-1") is None
    assert reusable_reference_parameters({**receipt, "message_id": ""}, SCOPE, "req-1") is None
    assert reusable_reference_parameters({**receipt, "answers": {}}, SCOPE, "req-1") is None


def test_parameter_receipt_returns_copy():
    receipt = {
        "status": "submitted", "scope": SCOPE, "requirements_digest": "req-1",
        "message_id": "bridge-1", "interaction_id": "question-1",
        "answers": {"video_generate_audio": True},
    }
    answers = reusable_reference_parameters(receipt, SCOPE, "req-1")
    assert answers is not receipt["answers"]


def test_only_a_real_generation_card_becomes_a_scoped_receipt():
    interaction = {
        "bridge_key": "bridge-1", "clarification_id": "question-1",
        "scope": SCOPE, "generation_requirements_digest": "req-1",
        "event": {"questions": [{"id": key} for key in (
            "video_model", "video_aspect_ratio", "video_resolution",
            "video_duration_seconds", "video_generate_audio", "video_variants_per_node",
        )]},
        "result": {"ok": True, "status": "clarification_frontend_result",
                   "answers": {
                       "video_model": {"option_ids": ["seedance"]},
                       "video_aspect_ratio": {"option_ids": ["9:16"]},
                       "video_resolution": {"option_ids": ["480p"]},
                       "video_duration_seconds": {"option_ids": ["4"]},
                       "video_generate_audio": {"option_ids": ["true"]},
                       "video_variants_per_node": {"option_ids": ["1"]},
                   }},
    }
    receipt = submitted_reference_parameter_receipt(interaction)
    assert reusable_reference_parameters(receipt, SCOPE, "req-1") == interaction["result"]["answers"]
    assert submitted_reference_parameter_receipt({**interaction, "bridge_key": ""}) is None
    assert submitted_reference_parameter_receipt({**interaction, "event": {"questions": [{"id": "direction"}]}}) is None
    assert submitted_reference_parameter_receipt({**interaction, "event": {"questions": [{"id": "video_model"}]}}) is None
    assert submitted_reference_parameter_receipt({**interaction, "result": {**interaction["result"], "status": "pending"}}) is None


def test_video_parameter_card_does_not_require_global_clip_duration():
    keys = ("video_model", "video_aspect_ratio", "video_resolution", "video_variants_per_node")
    interaction = {
        "bridge_key": "bridge", "clarification_id": "card", "scope": SCOPE,
        "generation_requirements_digest": "current",
        "event": {"questions": [{"id": key} for key in keys]},
        "result": {"ok": True, "status": "clarification_frontend_result", "answers": {
            "video_model": "model", "video_aspect_ratio": "9:16",
            "video_resolution": "720P", "video_variants_per_node": 1,
        }},
    }
    assert submitted_reference_parameter_receipt(interaction)["answers"] == interaction["result"]["answers"]


def test_material_choices_unwrap_one_submitted_option_without_inventing_defaults():
    values = material_parameter_values({
        "video_model": {"option_ids": ["model-a"]},
        "video_aspect_ratio": {"option_ids": ["9:16"]},
        "video_resolution": "720P",
        "video_generate_audio": {"option_ids": ["true"]},
        "video_duration_seconds": {"option_ids": ["5"]},
    })
    assert values == {"video_model": "model-a", "video_aspect_ratio": "9:16",
                      "video_resolution": "720P", "video_generate_audio": True}
    assert material_parameter_values({"video_model": {"option_ids": ["a", "b"]}}) == {}


def test_requirements_digest_tracks_generation_scope_and_confirmed_direction():
    commission = {"direction": {"remix_direction": "对话反转"}, "production_scope": "可编辑合成"}
    inputs = {"video_model": "seedance", "video_generate_audio": True, "user_goal": "未使用的叙述"}
    expected = generation_requirements_digest(commission, inputs)
    assert expected == generation_requirements_digest(commission, {**inputs, "user_goal": "另一句叙述"})
    assert expected != generation_requirements_digest(commission, {**inputs, "video_generate_audio": False})
    assert expected != generation_requirements_digest({**commission, "direction": {"remix_direction": "换内容"}}, inputs)
    assert expected != generation_requirements_digest({**commission, "direction_revision": 2}, inputs)


def test_separate_verified_cards_preserve_prior_answers_and_ignore_other_scope():
    def card(key, field, value, *, scope=SCOPE, digest="req-1"):
        return {"bridge_key": key, "clarification_id": key, "scope": scope,
                "generation_requirements_digest": digest,
                "event": {"questions": [{"id": field}]},
                "result": {"ok": True, "status": "clarification_frontend_result",
                           "answers": {field: value}}}

    receipts = [card("model", "video_model", "seedance"),
                card("old", "video_duration_seconds", 4),
                card("other", "video_resolution", "720p", digest="old-requirement"),
                card("new", "video_duration_seconds", 10),
                card("foreign", "video_generate_audio", True,
                     scope={**SCOPE, "requester_user_id": "someone-else"})]
    assert merged_reference_parameter_answers(receipts, SCOPE, "req-1") == {
        "video_model": "seedance", "video_duration_seconds": 10,
    }


def test_changed_requirement_keeps_independent_approved_fields():
    from novelvideo.freezone.remix.reference_parameter_receipts import generation_requirements_snapshot

    commission = {"direction": {"remix_direction": "retain"}, "production_scope": "editable"}
    before = generation_requirements_snapshot(commission, {
        "video_model": "model-a", "video_generate_audio": True,
    })
    after = generation_requirements_snapshot(commission, {
        "video_model": "model-a", "video_generate_audio": False,
    })
    interaction = {
        "bridge_key": "card", "clarification_id": "fields", "scope": SCOPE,
        "generation_requirements_digest": "old-digest", "requirements_snapshot": before,
        "event": {"questions": [{"id": key} for key in (
            "video_model", "video_resolution", "video_duration_seconds", "video_generate_audio",
            "image_quality",
        )]},
        "result": {"ok": True, "status": "clarification_frontend_result", "answers": {
            "video_model": "model-a", "video_resolution": "480p",
            "video_duration_seconds": 15, "video_generate_audio": True,
            "image_quality": "standard",
        }},
    }
    assert merged_reference_parameter_answers([interaction], SCOPE, "new-digest",
        current_requirements=after) == {
        "video_model": "model-a", "video_resolution": "480p",
        "video_duration_seconds": 15, "image_quality": "standard",
    }
    changed_model = generation_requirements_snapshot(commission, {
        "video_model": "model-b", "video_generate_audio": True,
    })
    assert merged_reference_parameter_answers([interaction], SCOPE, "new-digest",
        current_requirements=changed_model) == {"image_quality": "standard"}


def test_same_direction_text_with_new_direction_revision_rejects_old_card():
    from novelvideo.freezone.remix.reference_parameter_receipts import generation_requirements_snapshot

    direction = {"remix_direction": "retain the original rhythm"}
    inputs = {"video_model": "model-a", "video_resolution": "480p"}
    previous = {"direction": direction, "direction_revision": 1,
                "production_scope": "editable"}
    current = {**previous, "direction_revision": 2}
    interaction = {
        "bridge_key": "old-card", "clarification_id": "video-choice", "scope": SCOPE,
        "generation_requirements_digest": generation_requirements_digest(previous, inputs),
        "requirements_snapshot": generation_requirements_snapshot(previous, inputs),
        "event": {"questions": [{"id": "video_resolution"}]},
        "result": {"ok": True, "status": "clarification_frontend_result",
                   "answers": {"video_resolution": "480p"}},
    }
    assert merged_reference_parameter_answers(
        [interaction], SCOPE, generation_requirements_digest(current, inputs),
        current_requirements=generation_requirements_snapshot(current, inputs),
    ) is None


def test_later_model_card_invalidates_earlier_capability_choices():
    def card(identifier, values):
        return {"bridge_key": identifier, "clarification_id": identifier, "scope": SCOPE,
                "generation_requirements_digest": "same-requirements",
                "event": {"questions": [{"id": key} for key in values]},
                "result": {"ok": True, "status": "clarification_frontend_result",
                           "answers": values}}

    first = card("first", {"video_model": "model-a", "video_resolution": "480p",
                           "video_duration_seconds": 15, "video_generate_audio": True,
                           "image_quality": "standard"})
    changed_model = card("second", {"video_model": "model-b"})
    assert merged_reference_parameter_answers([first, changed_model], SCOPE,
        "same-requirements") == {"video_model": "model-b", "image_quality": "standard"}


def test_submitted_parameters_are_shared_across_continuation_runs(tmp_path):
    from novelvideo.freezone.workflow_drafts import (
        record_reference_parameter_submission, read_reference_parameter_submissions,
    )
    interaction = {"bridge_key": "ordinary-turn-card", "clarification_id": "choice-1",
                   "scope": SCOPE, "generation_requirements_digest": "req-1",
                   "event": {"questions": [{"id": "video_model"},
                                           {"id": "video_duration_seconds"}]},
                   "result": {"ok": True, "status": "clarification_frontend_result",
                              "answers": {"video_model": "seedance", "video_duration_seconds": 15}}}
    for _ in range(2):
        record_reference_parameter_submission(project_dir=tmp_path, scope=SCOPE,
            requirements_digest="req-1", interaction=interaction)
    stored = read_reference_parameter_submissions(project_dir=tmp_path, scope=SCOPE,
        requirements_digest="req-1")
    assert merged_reference_parameter_answers(stored, SCOPE, "req-1") == {
        "video_model": "seedance", "video_duration_seconds": 15,
    }
    assert len(stored) == 1
    assert read_reference_parameter_submissions(project_dir=tmp_path,
        scope={**SCOPE, "requester_user_id": "other"}, requirements_digest="req-1") == []
    assert read_reference_parameter_submissions(project_dir=tmp_path, scope=SCOPE,
        requirements_digest="req-2") == []


def test_persisted_parameter_fields_survive_an_unrelated_requirement_change(tmp_path):
    from novelvideo.freezone.workflow_drafts import (
        record_reference_parameter_submission, read_reference_parameter_submissions,
    )
    from novelvideo.freezone.remix.reference_parameter_receipts import generation_requirements_snapshot

    commission = {"direction": {"remix_direction": "retain"}, "production_scope": "editable"}
    before_inputs = {"video_model": "model-a", "video_generate_audio": True}
    after_inputs = {"video_model": "model-a", "video_generate_audio": False}
    digest = generation_requirements_digest(commission, before_inputs)
    interaction = {
        "bridge_key": "persisted-card", "clarification_id": "choices", "scope": SCOPE,
        "generation_requirements_digest": digest,
        "requirements_snapshot": generation_requirements_snapshot(commission, before_inputs),
        "event": {"questions": [{"id": "video_resolution"}, {"id": "video_generate_audio"}]},
        "result": {"ok": True, "status": "clarification_frontend_result",
                   "answers": {"video_resolution": "480p", "video_generate_audio": True}},
    }
    record_reference_parameter_submission(project_dir=tmp_path, scope=SCOPE,
        requirements_digest=digest, interaction=interaction)
    current_digest = generation_requirements_digest(commission, after_inputs)
    stored = read_reference_parameter_submissions(project_dir=tmp_path, scope=SCOPE,
        requirements_digest=current_digest, include_prior_requirements=True)
    assert merged_reference_parameter_answers(stored, SCOPE, current_digest,
        current_requirements=generation_requirements_snapshot(commission, after_inputs)) == {
        "video_resolution": "480p",
    }


@pytest.mark.anyio
async def test_ordinary_chat_parameter_answer_is_available_to_background_continuation(
    tmp_path, monkeypatch,
):
    from novelvideo.api.routes import chat
    from novelvideo.chat import reference_remix_continuation as continuation
    from novelvideo.freezone.remix import reference_commission
    from novelvideo.freezone import workflow_drafts
    from novelvideo.freezone.workflow_drafts import read_reference_parameter_submissions

    ctx = SimpleNamespace(state_dir=tmp_path, output_dir=tmp_path, project_id="p",
                          requester_user_id="u")
    async def project_context(*_args):
        return ctx
    monkeypatch.setattr(chat, "_project_context_for_interaction_result", project_context)
    run = {"project_id": "p", "canvas_id": "c", "agent_id": "main",
           "requester_user_id": "u", "commission_ref": {"artifact_id": "commission", "revision": 1},
           "status": "completed", "inputs": {}}
    monkeypatch.setattr(continuation.coordinator.store, "list_for_actor", lambda *_args: [run])
    commission = {"commission_ref": run["commission_ref"], "phase": "drafting_plan",
                  "direction": {"remix_direction": "keep the edit"},
                  "production_scope": "可编辑合成", "direction_revision": 1}
    monkeypatch.setattr(reference_commission, "read_reference_commission", lambda *_args, **_kwargs: commission)
    monkeypatch.setattr(workflow_drafts, "read_native_plan_claim", lambda **_kwargs: None)
    monkeypatch.setattr(chat, "read_bridge_message", lambda *_args, **_kwargs: {
        "key": "ordinary-turn-card", "event": {"clarification_id": "choice-1",
          "workflow_draft_id": "reference-draft", "workflow_expected_revision": 2,
          "questions": [{"id": "video_model"}, {"id": "video_duration_seconds"}]}})
    payload = chat.ClarificationToolResultIn(
        bridge_key="ordinary-turn-card", project_id="p", canvas_id="c", agent_id="main",
        turn_id="ordinary-turn", answers={"video_model": "seedance", "video_duration_seconds": 15},
    )
    await chat._persist_reference_parameter_submission(
        user={"username": "alice", "user_id": "u"}, payload=payload,
        result={"ok": True, "status": "clarification_frontend_result", "answers": payload.answers},
        project_state_dir=tmp_path,
    )
    scope = {"project_id": "p", "canvas_id": "c", "agent_id": "main",
             "requester_user_id": "u", "commission_id": "commission"}
    digest = generation_requirements_digest(commission, run["inputs"])
    stored = read_reference_parameter_submissions(project_dir=tmp_path, scope=scope,
        requirements_digest=digest)
    assert merged_reference_parameter_answers(stored, scope, digest) == payload.answers
    assert await chat._current_reference_parameter_answers(
        ctx, canvas_id="c", agent_id="main",
    ) == ("commission", payload.answers)
    old_run = {**run, "commission_ref": {"artifact_id": "old-commission", "revision": 1}}
    old_commission = {"commission_ref": old_run["commission_ref"], "phase": "completed"}
    monkeypatch.setattr(continuation.coordinator.store, "list_for_actor",
                        lambda *_args: [old_run, run])
    monkeypatch.setattr(reference_commission, "read_reference_commission",
                        lambda _store, ref, **_kwargs: old_commission if ref["artifact_id"] == "old-commission" else commission)
    assert await chat._current_reference_parameter_answers(
        ctx, canvas_id="c", agent_id="main",
    ) == ("commission", payload.answers)
    commission["commission_ref"] = {"artifact_id": "commission", "revision": 2}
    assert await chat._current_reference_parameter_answers(
        ctx, canvas_id="c", agent_id="main",
    ) == ("commission", payload.answers)
    commission["commission_ref"] = run["commission_ref"]
    monkeypatch.setattr(reference_commission, "read_reference_commission", lambda *_args, **_kwargs: commission)
    monkeypatch.setattr(continuation.coordinator.store, "list_for_actor", lambda *_args: [run])
    conflicting_run = {**run, "inputs": {"video_generate_audio": False}}
    monkeypatch.setattr(continuation.coordinator.store, "list_for_actor",
                        lambda *_args: [run, conflicting_run])
    assert await chat._current_reference_parameter_answers(
        ctx, canvas_id="c", agent_id="main",
    ) is None
    monkeypatch.setattr(continuation.coordinator.store, "list_for_actor", lambda *_args: [run])
    monkeypatch.setattr(chat, "read_bridge_message", lambda *_args, **_kwargs: {
        "event": {"clarification_id": "unrelated-card", "questions": [{"id": "video_model"}]},
    })
    payload.bridge_key = "another-workflow-card"
    await chat._persist_reference_parameter_submission(
        user={"username": "alice", "user_id": "u"}, payload=payload,
        result={"ok": True, "status": "clarification_frontend_result", "answers": payload.answers},
        project_state_dir=tmp_path,
    )
    assert len(read_reference_parameter_submissions(project_dir=tmp_path, scope=scope,
        requirements_digest=digest)) == 1


@pytest.mark.anyio
async def test_http_clarification_result_records_verified_parameter_submission(tmp_path, monkeypatch):
    from novelvideo.api.routes import chat

    payload = chat.ClarificationToolResultIn(
        bridge_key="card", project_id="p", canvas_id="c", agent_id="main",
        turn_id="ordinary-turn", answers={"video_model": "seedance"},
    )
    monkeypatch.setattr(chat, "_bridge_project_state_dir", lambda *_args: _state_dir(tmp_path))
    seen = []
    monkeypatch.setattr(chat, "_resolve_clarification_tool_result_payload",
                        lambda *_args, **_kwargs: {"ok": True, "status": "clarification_frontend_result",
                                                  "answers": payload.answers})
    async def persist_parameters(**kwargs):
        seen.append(kwargs)
    async def persist_ui(**_kwargs):
        return None
    monkeypatch.setattr(chat, "_persist_reference_parameter_submission", persist_parameters)
    monkeypatch.setattr(chat, "_persist_clarification_result_ui_event", persist_ui)
    await chat.resolve_clarification_tool_result(payload, user={"username": "alice"})
    assert len(seen) == 1
    assert seen[0]["project_state_dir"] == tmp_path
    assert seen[0]["result"]["answers"] == payload.answers


async def _state_dir(path):
    return path
