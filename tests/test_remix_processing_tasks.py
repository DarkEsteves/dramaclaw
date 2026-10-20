from __future__ import annotations

from types import SimpleNamespace
import hashlib
import json
import logging
from copy import deepcopy

import pytest


VOICE_HASH = hashlib.sha256(b"voice").hexdigest()


def test_plan_runner_logs_correlated_persist_and_operation_publication(tmp_path, monkeypatch, caplog):
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.task_backend.runners import remix as runner

    operations = RemixOperationStore(tmp_path, "demo")
    operation = operations.start(
        idempotency_key="diagnostic-plan", input_refs=[], producer_version="remix-process.v1",
        normalized_parameters={"action": "generate_production_plan_draft", "payload": {"topic": "咖啡"}},
        node_revision=1, canvas_id="canvas-1", node_id="plan-1",
    )
    stages: list[tuple[str, float]] = []

    class Manager:
        def update_progress_for_project(self, _ctx, _task_type, _episode, **kwargs):
            stages.append((kwargs["current_task"], kwargs["progress"]))
            assert kwargs["expected_task_id"] == "task-1"
            assert kwargs["scope"] == "job-1"

    async def generate(**_kwargs):
        from novelvideo.freezone.remix.production_plan_draft_stages import DraftStageEvent

        callback = _kwargs["on_stage"]
        for stage, payload in (("outline", {"beats": []}),):
            await callback(DraftStageEvent(stage, "started"))
            await callback(DraftStageEvent(stage, "completed", payload))
        return {
            "schema_version": 2, "goal": "咖啡", "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
            "beats": [{"id": "hook", "purpose": "提问", "duration_frames": 90,
                       "turns": [{"id": "line", "text": "你好世界"}]}],
            "resources": [], "placements": [], "provenance": {},
        }

    monkeypatch.setattr(runner, "generate_production_plan_draft", generate)
    monkeypatch.setattr("novelvideo.task_state.get_task_manager", lambda: Manager())
    monkeypatch.setattr(runner, "raise_if_envelope_cancel_requested", lambda _envelope: None)
    with caplog.at_level(logging.INFO, logger="novelvideo.llm_instrumentation"):
        result = runner.run_remix_generate_production_plan_draft(
            {"__run_task_id": "task-1", "scope": "job-1", "episode": 0,
             "payload": {"topic": "咖啡", "operation_id": operation["operation_id"],
                         "node_revision": 1, "workflow_run_id": "run-1", "workflow_node_id": "plan-1"}},
            SimpleNamespace(output_dir=str(tmp_path), project_id="demo", requester_username="alice", requester_user_id="user-alice"),
        )
    assert result['artifact_ref']['revision'] == 1
    assert result['speech_estimates'][0]['entity_id'] == 'line'
    assert result['plan_diagnostics'] == []
    events = [json.loads(record.getMessage()) for record in caplog.records
              if record.name == "novelvideo.llm_instrumentation" and record.getMessage().startswith("{")]
    assert [item["stage"] for item in events if item["event"].startswith("production_plan.runner_")] == [
        "requesting", "persisting", "persisting", "publishing", "publishing",
    ]
    assert all(item["project_id"] == "demo" and item["canvas_id"] == "canvas-1"
               and item["node_id"] == "plan-1" and item["workflow_run_id"] == "run-1"
               and item["operation_id"] == operation["operation_id"] and item["task_id"] == "task-1"
               for item in events)
    assert stages == [
        ("正在起草作品方案", 0.02),
        ("正在整理结构与台词", 0.05),
        ("正在保存作品方案", 0.95),
        ("正在发布作品方案", 0.98),
    ]
    assert operations.read_draft_stage(operation["operation_id"], "task-1", "outline") == {"beats": []}
    with pytest.raises(FileNotFoundError):
        operations.read_draft_stage(operation["operation_id"], "task-1", "arrangement")


def test_failed_second_draft_stage_never_saves_or_publishes_a_plan(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.remix.production_plan_draft_stages import DraftStageEvent
    from novelvideo.task_backend.runners import remix as runner

    operations = RemixOperationStore(tmp_path, "demo")
    operation = operations.start(
        idempotency_key="failed-second-stage", input_refs=[], producer_version="remix-process.v1",
        normalized_parameters={"action": "generate_production_plan_draft", "payload": {"topic": "咖啡"}},
        node_revision=1, canvas_id="canvas-1", node_id="plan-1",
    )

    async def generate(**kwargs):
        await kwargs["on_stage"](DraftStageEvent("outline", "completed", {"beats": []}))
        await kwargs["on_stage"](DraftStageEvent("arrangement", "started"))
        raise TimeoutError("arrangement timed out")

    monkeypatch.setattr(runner, "generate_production_plan_draft", generate)
    monkeypatch.setattr(runner, "_publish_operation_result", lambda *_args: pytest.fail("published"))
    ctx = SimpleNamespace(output_dir=str(tmp_path), project_id="demo",
                          requester_username="alice", requester_user_id="user-alice")
    store = runner.production_store(ctx)
    monkeypatch.setattr(store, "create", lambda *_args: pytest.fail("saved"))
    monkeypatch.setattr(runner, "production_store", lambda _ctx: store)
    with pytest.raises(TimeoutError):
        runner.run_remix_generate_production_plan_draft(
            {"__run_task_id": "task-1", "payload": {"topic": "咖啡",
                "operation_id": operation["operation_id"], "node_revision": 1}},
            ctx,
        )
    assert operations.read_draft_stage(operation["operation_id"], "task-1", "outline") == {"beats": []}
    with pytest.raises(FileNotFoundError):
        operations.read_draft_stage(operation["operation_id"], "task-1", "arrangement")
    assert operations.get(operation["operation_id"])["output_refs"] == []


def test_plan_runner_passes_output_preferences_without_polluting_topic(tmp_path, monkeypatch):
    from novelvideo.task_backend.runners import remix as runner

    captured: dict[str, object] = {}

    async def generate(**kwargs):
        captured.update(kwargs)
        return {
            "schema_version": 2, "goal": kwargs["topic"],
            "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
            "beats": [{"id": "hook", "purpose": "提问", "duration_frames": 90, "turns": []}],
            "resources": [], "placements": [], "provenance": {},
        }

    monkeypatch.setattr(runner, "generate_production_plan_draft", generate)
    runner.run_remix_generate_production_plan_draft(
        {"payload": {"topic": "做一条黑咖啡短片", "output_preferences": {
            "aspect_ratio": "9:16", "source_clip_duration_seconds": 5,
        }}},
        SimpleNamespace(output_dir=str(tmp_path), project_id="demo", requester_username="alice", requester_user_id="user-alice"),
    )
    assert captured["topic"] == "做一条黑咖啡短片"
    assert captured["output_preferences"] == {"aspect_ratio": "9:16", "source_clip_duration_seconds": 5}


def test_production_speech_alignment_uses_exact_plan_turn_and_media(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.task_backend.runners import remix as runner

    media = tmp_path / "media" / "voice.wav"
    media.parent.mkdir()
    media.write_bytes(b"voice")
    store = RemixArtifactStore(tmp_path, "demo")
    plan = store.create("production_plan.v2", {
        "schema_version": 2, "goal": "问答", "output": {"width": 1920, "height": 1080, "fps_num": 25, "fps_den": 1},
        "beats": [{"id": "beat", "purpose": "提问", "duration_frames": 100,
                   "turns": [{"id": "turn", "speaker": "甲", "text": "为什么？"}]}],
        "resources": [{"id": "voice", "node_id": "node-voice", "media_kind": "audio",
                       "media_path": "media/voice.wav", "media_hash": VOICE_HASH,
                       "duration_us": 4_000_000, "has_audio": True}],
        "placements": [{"id": "speech", "kind": "audio", "role": "speech", "track_id": "audio:speech",
                        "resource_id": "voice", "start": {"kind": "program_frame", "frame": 0},
                        "end": {"kind": "program_frame", "frame": 100}}], "provenance": {},
    }, [])
    monkeypatch.setattr(runner, "align_script", lambda path, script: {
        "media_hash": "sha256:" + VOICE_HASH, "language": "zh", "status": "completed",
        "transcript": "为什么？", "words": [{"text": "为什么", "start_char": 0, "end_char": 3,
                                        "start_us": 500_000, "end_us": 900_000}],
    })
    ref = {"artifact_id": plan["id"], "revision": plan["revision"]}
    result = runner.run_remix_align_production_speech({"payload": {
        "media_path": "media/voice.wav", "media_sha256": VOICE_HASH,
        "plan_ref": ref, "turn_id": "turn", "placement_id": "speech",
    }}, SimpleNamespace(output_dir=str(tmp_path), project_id="demo"))
    artifact = store.read(result["artifact_ref"]["artifact_id"], result["artifact_ref"]["revision"])
    assert artifact["schema"] == "word_timeline.v1"
    assert artifact["upstream_refs"][0]["artifact_id"] == plan["id"]
    assert artifact["payload"]["turn_id"] == "turn"
    assert artifact["payload"]["placement_id"] == "speech"
    assert artifact["payload"]["text_hash"] == hashlib.sha256("为什么？".encode()).hexdigest()
    assert artifact["payload"]["words"][0]["start_us"] == 500_000


@pytest.mark.parametrize("use_take", [False, True])
def test_production_speech_alignment_times_two_turns_from_one_video_once(tmp_path, monkeypatch, use_take):
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.task_backend.runners import remix as runner

    media = tmp_path / "media" / "interview.mp4"
    media.parent.mkdir()
    media.write_bytes(b"two speakers in one take")
    digest = hashlib.sha256(media.read_bytes()).hexdigest()
    store = RemixArtifactStore(tmp_path, "demo")
    first, second = "我一天三杯。", "行，下一题。"
    plan = store.create("production_plan.v2", {
        "schema_version": 2, "goal": "访谈", "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
        "beats": [{"id": "beat", "purpose": "收尾", "duration_frames": 180,
                   "turns": [{"id": "guest", "speaker": "嘉宾", "text": first, "speech_placement_id": "picture"},
                             {"id": "host", "speaker": "主持人", "text": second, "speech_placement_id": "picture"}]}],
        "resources": [{"id": "video", "node_id": "video-node", "media_kind": "video",
                       "media_path": "media/interview.mp4", "media_hash": digest,
                       "duration_us": 6_000_000, "has_audio": True}],
        "placements": [{"id": "picture", "kind": "visual", "role": "primary", "track_id": "visual:primary",
                        "resource_id": "video", "start": {"kind": "beat", "ref_id": "beat", "edge": "start"},
                        "end": {"kind": "beat", "ref_id": "beat", "edge": "end"}}], "provenance": {},
    }, [])
    if use_take:
        updated = deepcopy(plan["payload"])
        updated["speech_takes"] = [{"id": "take", "beat_id": "beat", "resource_id": "video",
                                   "turn_ids": ["guest", "host"], "origin_placement_id": "picture",
                                   "source_in_us": 0, "source_out_us": None,
                                   "speed_num": 1, "speed_den": 1, "lead_in_frames": 0}]
        updated["placements"][0]["speech_take_id"] = "take"
        plan = store.revise(plan["id"], 1, updated, [])
    recognized = []

    def recognize(path, script):
        recognized.append((path, script["text"]))
        offset = len(first) + 1
        return {"media_hash": "sha256:" + digest, "language": "zh", "status": "completed",
                "transcript": script["text"], "words": [
                    {"text": "我一天三杯", "start_char": 0, "end_char": 5,
                     "start_us": 500_000, "end_us": 2_400_000},
                    {"text": "行，下一题", "start_char": offset, "end_char": offset + 5,
                     "start_us": 3_000_000, "end_us": 4_500_000},
                ]}

    monkeypatch.setattr(runner, "align_script", recognize)
    result = runner.run_remix_align_production_speech({"payload": {
        "media_path": "media/interview.mp4", "media_sha256": digest,
        "plan_ref": {"artifact_id": plan["id"], "revision": plan["revision"]},
        "turn_id": "guest", **({"take_id": "take"} if use_take else {"placement_id": "picture"}),
    }}, SimpleNamespace(output_dir=str(tmp_path), project_id="demo"))
    assert recognized == [(media, first + "\n" + second)]
    assert result["status"] == "completed"
    primary = store.read(result["artifact_ref"]["artifact_id"], 1)
    assert primary["payload"]["turn_id"] == "guest"
    assert primary["payload"]["words"][0]["start_char"] == 0
    assert len(primary["payload"]["group_refs"]) == 1
    other_ref = primary["payload"]["group_refs"][0]
    assert other_ref["turn_id"] == "host"
    other = store.read(other_ref["artifact_id"], other_ref["revision"])
    assert other["payload"]["words"] == [{
        "text": "行，下一题", "start_char": 0, "end_char": 5,
        "start_us": 3_000_000, "end_us": 4_500_000,
    }]
    assert other["payload"]["text_hash"] == hashlib.sha256(second.encode()).hexdigest()
    revised = deepcopy(plan["payload"])
    for candidate, evidence in zip(revised["beats"][0]["turns"], (primary, other), strict=True):
        evidence_payload = evidence["payload"]
        candidate["speech_timing"] = {
            "placement_id": "picture", "media_hash": digest,
            "text": candidate["text"], "text_hash": evidence_payload["text_hash"],
            "source_in_us": 0, "source_out_us": 6_000_000,
            "evidence_ref": {"artifact_id": evidence["id"], "revision": 1},
            "words": [{"id": f"{candidate['id']}:word:{index}", **word}
                      for index, word in enumerate(evidence_payload["words"])],
        }
    if use_take:
        revised["speech_cues"] = [
            {"id": f"cue:{candidate['id']}", "turn_id": candidate["id"], "take_id": "take",
             "text": candidate["text"], "source_start_us": candidate["speech_timing"]["words"][0]["start_us"],
             "source_end_us": candidate["speech_timing"]["words"][-1]["end_us"],
             "timing_source": "asr", "timing_status": "ready",
             "word_ids": [word["id"] for word in candidate["speech_timing"]["words"]],
             "evidence_ref": candidate["speech_timing"]["evidence_ref"]}
            for candidate in revised["beats"][0]["turns"]
        ]
    store.revise(plan["id"], plan["revision"], revised, [
        {"artifact_id": primary["id"], "revision": 1},
        {"artifact_id": other["id"], "revision": 1},
    ])
    from novelvideo.freezone.remix.production_plan_v2 import compile_production_plan_v2
    manifest = compile_production_plan_v2(revised)
    assert len(manifest["layers"]) == 1
    assert len(manifest["audio_events"]) == 1
    assert manifest["audio_events"][0]["media_id"] == "video"


def test_production_speech_alignment_recognizes_full_media_even_with_an_edit_range(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.task_backend.runners import remix as runner

    media = tmp_path / "media" / "talk.mp4"
    media.parent.mkdir()
    media.write_bytes(b"talk")
    digest = hashlib.sha256(b"talk").hexdigest()
    store = RemixArtifactStore(tmp_path, "demo")
    plan = store.create("production_plan.v2", {
        "schema_version": 2, "goal": "问答", "output": {"width": 1280, "height": 720, "fps_num": 25, "fps_den": 1},
        "beats": [{"id": "b", "purpose": "提问", "duration_frames": 50,
                   "turns": [{"id": "t", "text": "你好", "speaker": "甲"}]}],
        "resources": [{"id": "v", "node_id": "n", "media_kind": "video", "media_path": "media/talk.mp4",
                       "media_hash": digest, "duration_us": 5_000_000, "has_audio": True}],
        "placements": [{"id": "p", "kind": "visual", "role": "primary", "track_id": "v", "resource_id": "v",
                        "start": {"kind": "program_frame", "frame": 0}, "end": {"kind": "program_frame", "frame": 25},
                        "source_in_us": 500_000, "source_out_us": 1_500_000}], "provenance": {},
    }, [])
    recognized = []
    def recognize(path, script):
        recognized.append(path)
        return {
        "media_hash": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        "language": "zh", "status": "completed", "transcript": "你好",
        "words": [{"text": "你好", "start_char": 0, "end_char": 2, "start_us": 600_000, "end_us": 900_000}],
    }
    monkeypatch.setattr(runner, "align_script", recognize)
    result = runner.run_remix_align_production_speech({"payload": {
        "media_path": "media/talk.mp4", "media_sha256": digest,
        "plan_ref": {"artifact_id": plan["id"], "revision": 1}, "turn_id": "t", "placement_id": "p",
    }}, SimpleNamespace(output_dir=str(tmp_path), project_id="demo"))
    artifact = store.read(result["artifact_ref"]["artifact_id"], 1)
    assert recognized == [media]
    assert artifact["payload"]["source_in_us"] == 0
    assert artifact["payload"]["source_out_us"] == 5_000_000
    assert artifact["payload"]["media_hash"] == "sha256:" + digest
    assert artifact["payload"]["words"][0]["start_us"] == 600_000


def test_production_speech_alignment_preserves_full_word_timing_beyond_estimated_duration(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.task_backend.runners import remix as runner

    media = tmp_path / "media" / "answer.mp4"
    media.parent.mkdir()
    media.write_bytes(b"full answer")
    digest = hashlib.sha256(b"full answer").hexdigest()
    store = RemixArtifactStore(tmp_path, "demo")
    plan = store.create("production_plan.v2", {
        "schema_version": 2, "goal": "问答", "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
        "beats": [{"id": "b", "purpose": "回答", "duration_frames": 90,
                   "turns": [{"id": "t", "text": "黑咖啡，没有之一。", "speaker": "嘉宾"}]}],
        "resources": [{"id": "v", "node_id": "n", "media_kind": "video", "media_path": "media/answer.mp4",
                       "media_hash": digest, "duration_us": 4_096_000, "has_audio": True}],
        "placements": [{"id": "p", "kind": "visual", "role": "primary", "track_id": "v", "resource_id": "v",
                        "start": {"kind": "program_frame", "frame": 0},
                        "end": {"kind": "program_frame", "frame": 90}}], "provenance": {},
    }, [])
    recognized = []
    def recognize(path, script):
        recognized.append(path)
        return {"media_hash": "sha256:" + digest, "language": "zh", "status": "completed",
                "transcript": script["text"], "words": [
                    {"text": "黑咖啡", "start_char": 0, "end_char": 3, "start_us": 1_340_000, "end_us": 2_340_000},
                    {"text": "没有之一", "start_char": 4, "end_char": 8, "start_us": 3_120_000, "end_us": 3_560_000},
                ]}
    monkeypatch.setattr(runner, "align_script", recognize)
    result = runner.run_remix_align_production_speech({"payload": {
        "media_path": "media/answer.mp4", "media_sha256": digest,
        "plan_ref": {"artifact_id": plan["id"], "revision": 1}, "turn_id": "t", "placement_id": "p",
    }}, SimpleNamespace(output_dir=str(tmp_path), project_id="demo"))
    artifact = store.read(result["artifact_ref"]["artifact_id"], 1)
    assert recognized == [media]
    assert result["status"] == "completed"
    assert artifact["payload"]["status"] == "completed"
    assert artifact["payload"]["words"][-1]["end_us"] == 3_560_000
    assert artifact["payload"]["source_out_us"] == 4_096_000


def test_production_speech_alignment_still_recognizes_when_generated_media_is_shorter_than_plan(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.task_backend.runners import remix as runner

    media = tmp_path / "media" / "short.wav"
    media.parent.mkdir()
    media.write_bytes(b"short")
    digest = hashlib.sha256(b"short").hexdigest()
    store = RemixArtifactStore(tmp_path, "demo")
    plan = store.create("production_plan.v2", {
        "schema_version": 2, "goal": "问答", "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
        "beats": [{"id": "b", "purpose": "回答", "duration_frames": 120,
                   "turns": [{"id": "t", "text": "你好", "speaker": "嘉宾"}]}],
        "resources": [{"id": "v", "node_id": "n", "media_kind": "audio", "media_path": "media/short.wav",
                       "media_hash": digest, "duration_us": 2_000_000, "has_audio": True}],
        "placements": [{"id": "p", "kind": "audio", "role": "speech", "track_id": "a", "resource_id": "v",
                        "start": {"kind": "program_frame", "frame": 0},
                        "end": {"kind": "program_frame", "frame": 120}}], "provenance": {},
    }, [])
    monkeypatch.setattr(runner, "align_script", lambda path, script: {
        "media_hash": "sha256:" + digest, "language": "zh", "status": "completed",
        "transcript": "你好", "words": [{"text": "你好", "start_char": 0, "end_char": 2,
                                     "start_us": 200_000, "end_us": 800_000}],
    })
    result = runner.run_remix_align_production_speech({"payload": {
        "media_path": "media/short.wav", "media_sha256": digest,
        "plan_ref": {"artifact_id": plan["id"], "revision": 1}, "turn_id": "t", "placement_id": "p",
    }}, SimpleNamespace(output_dir=str(tmp_path), project_id="demo"))
    artifact = store.read(result["artifact_ref"]["artifact_id"], 1)
    assert artifact["payload"]["status"] == "completed"
    assert artifact["payload"]["source_out_us"] == 2_000_000


def test_reference_evidence_runner_publishes_full_video_evidence(tmp_path, monkeypatch):
    from novelvideo.task_backend.runners import remix as runner

    media = tmp_path / "media" / "reference.mp4"
    media.parent.mkdir()
    media.write_bytes(b"video")
    evidence = {
        "media_hash": hashlib.sha256(b"video").hexdigest(),
        "time_base": "1/1000", "duration_us": 1_000_000,
        "cuts": [], "representative_frames": [{"pts": 0, "time_us": 0}],
    }
    monkeypatch.setattr(runner, "build_reference_evidence", lambda path: evidence)
    result = runner.run_remix_analyze_reference_video(
        {"payload": {"media_path": "media/reference.mp4", "media_sha256": hashlib.sha256(b"video").hexdigest()}},
        SimpleNamespace(output_dir=str(tmp_path), project_id="demo"),
    )
    assert result["artifact_ref"]["revision"] == 1
    assert result["artifact_ref"]["artifact_id"]


def test_broll_plan_runner_publishes_plan_for_exact_script_revision(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.task_backend.runners import remix as runner

    store = RemixArtifactStore(tmp_path, "demo")
    script = store.create("remix_script.v1", {
        "dialogue": "新品登场", "segments": [], "selections": [],
        "events": [{"event_id": "product"}], "blueprint_mappings": [],
        "media_requirements": [],
    }, [])
    script_ref = {"artifact_id": script["id"], "revision": script["revision"]}
    operations = RemixOperationStore(tmp_path, "demo")
    operation = operations.start(
        idempotency_key="broll-plan-1",
        input_refs=[script_ref],
        producer_version="remix-process.v1",
        normalized_parameters={"action": "plan_broll", "payload": {"script_ref": script_ref}},
        node_revision=2,
    )

    async def generate(*, script, username):
        assert script["dialogue"] == "新品登场"
        assert username == "alice"
        return {
            **script,
            "media_requirements": [{
                "event_id": "product", "required": True,
                "purpose": "展示产品", "suggested_query": "产品特写",
            }],
        }

    monkeypatch.setattr(runner, "generate_broll_plan", generate)
    result = runner.run_remix_plan_broll(
        {"payload": {"script_ref": script_ref, "operation_id": operation["operation_id"]}},
        SimpleNamespace(output_dir=str(tmp_path), project_id="demo", requester_username="alice"),
    )

    artifact = store.read(result["artifact_ref"]["artifact_id"], result["artifact_ref"]["revision"])
    assert artifact["schema"] == "remix_script.v1"
    assert artifact["upstream_refs"] == [{**script_ref, "project_id": "demo"}]
    assert artifact["payload"]["media_requirements"][0]["required"] is True
    assert operations.get(operation["operation_id"])["output_refs"] == [result["artifact_ref"]]


def test_script_draft_runner_publishes_editable_draft_for_exact_blueprint(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.task_backend.runners import remix as runner

    store = RemixArtifactStore(tmp_path, "demo")
    blueprint = store.create("reference_blueprint.v1", {
        "narrative_role": "提问后反转", "events": [], "preserve_replace_rules": [],
        "observations": [], "inferences": [], "user_choices": [],
    }, [])
    blueprint_ref = {"artifact_id": blueprint["id"], "revision": blueprint["revision"]}
    operations = RemixOperationStore(tmp_path, "demo")
    operation = operations.start(
        idempotency_key="script-draft-1",
        input_refs=[blueprint_ref],
        producer_version="remix-process.v1",
        normalized_parameters={"action": "generate_script_draft", "payload": {
            "blueprint_ref": blueprint_ref, "new_topic": "薯条",
        }},
        node_revision=2,
    )

    monkeypatch.setattr(runner, "reference_blueprint_readiness_errors", lambda payload, refs: [])

    async def generate(*, blueprint, topic, username):
        assert blueprint["narrative_role"] == "提问后反转"
        assert topic == "薯条"
        assert username == "alice"
        return {"dialogue": "男：你吃什么？\n女：薯条。", "segments": [], "selections": [{
            "id": "generated-1", "label": "产品亮相", "start": 10, "end": 12,
        }]}

    monkeypatch.setattr(runner, "generate_script_draft", generate)
    result = runner.run_remix_generate_script_draft(
        {"payload": {
            "blueprint_ref": blueprint_ref, "new_topic": "薯条",
            "operation_id": operation["operation_id"],
        }},
        SimpleNamespace(output_dir=str(tmp_path), project_id="demo", requester_username="alice"),
    )

    artifact = store.read(result["artifact_ref"]["artifact_id"], result["artifact_ref"]["revision"])
    assert artifact["schema"] == "remix_script.v1"
    assert artifact["upstream_refs"] == [{**blueprint_ref, "project_id": "demo"}]
    assert artifact["payload"]["dialogue"] == "男：你吃什么？\n女：薯条。"
    assert artifact["payload"]["segments"] == []
    assert artifact["payload"]["selections"][0]["label"] == "产品亮相"
    assert operations.get(operation["operation_id"])["output_refs"] == [result["artifact_ref"]]


@pytest.mark.parametrize('requirements', [None, {'primary_shots': {'min': 3, 'max': 6}}])
def test_production_plan_runner_publishes_draft_from_analysis_context(tmp_path, monkeypatch, requirements):
    from novelvideo.api.routes.remix import prepare_registered_action
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.task_backend.runners import remix as runner

    store = RemixArtifactStore(tmp_path, "demo", created_by="user-alice")
    prepared = prepare_registered_action(
        store, "generate_production_plan_draft",
        {"topic": "黑咖啡", "analysis_context": {"summary": "先提问"},
         **({"requirements": requirements} if requirements is not None else {})},
        idempotency_key="production-plan-1", node_revision=1,
    )
    operations = RemixOperationStore(tmp_path, "demo")
    operation = operations.start(
        idempotency_key="production-plan-1", input_refs=prepared["input_refs"],
        producer_version=prepared["producer_version"],
        normalized_parameters=prepared["normalized_parameters"],
        node_revision=1,
    )
    async def generate(*, topic, blueprint, username, locked_turns, **kwargs):
        assert topic == "黑咖啡"
        assert blueprint == {"summary": "先提问"}
        assert username == "alice"
        assert locked_turns is None
        assert kwargs.get('requirements') == requirements
        return {
            "schema_version": 2, "goal": topic, "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
            "beats": [{"id": "hook", "purpose": "引出问题", "duration_frames": 90,
                       "turns": []}], "resources": [], "placements": [], "provenance": {},
        }
    monkeypatch.setattr(runner, "generate_production_plan_draft", generate)
    result = runner.run_remix_generate_production_plan_draft(
        {"payload": {**prepared["task_payload"], "operation_id": operation["operation_id"]}},
        SimpleNamespace(output_dir=str(tmp_path), project_id="demo", requester_username="alice", requester_user_id="user-alice"),
    )
    artifact = RemixArtifactStore(tmp_path, "demo").read(result["artifact_ref"]["artifact_id"], 1)
    assert artifact["schema"] == "production_plan.v2"
    assert artifact["created_by"] == "user-alice"
    assert artifact["payload"]["goal"] == "黑咖啡"
    if requirements is not None:
        assert artifact['payload']['provenance']['requirements'] == requirements
        assert artifact['payload']['provenance']['requirements_fingerprint'] == prepared['task_payload']['requirements_fingerprint']
    assert artifact["upstream_refs"] == []
    assert operations.get(operation["operation_id"])["output_refs"] == [result["artifact_ref"]]


def test_production_plan_runner_uses_exact_blueprint_revision(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.task_backend.runners import remix as runner

    store = RemixArtifactStore(tmp_path, "demo")
    blueprint = store.create("reference_blueprint.v1", {
        "narrative_role": "提问后反转", "events": [], "preserve_replace_rules": [],
        "observations": [], "inferences": [], "user_choices": [],
    }, [])
    ref = {"artifact_id": blueprint["id"], "revision": blueprint["revision"]}
    async def generate(*, topic, blueprint, username, locked_turns, on_stage):
        assert blueprint["narrative_role"] == "提问后反转"
        return {
            "schema_version": 2, "goal": topic, "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
            "beats": [{"id": "hook", "purpose": "提问", "duration_frames": 90, "turns": []}],
            "resources": [], "placements": [], "provenance": {},
        }
    monkeypatch.setattr(runner, "generate_production_plan_draft", generate)
    result = runner.run_remix_generate_production_plan_draft(
        {"payload": {"topic": "黑咖啡", "blueprint_ref": ref}},
        SimpleNamespace(output_dir=str(tmp_path), project_id="demo", requester_username="alice", requester_user_id="user-alice"),
    )
    artifact = store.read(result["artifact_ref"]["artifact_id"], 1)
    assert artifact["upstream_refs"] == [{**ref, "project_id": "demo"}]


def test_production_plan_runner_passes_saved_source_clock(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.task_backend.runners import remix as runner

    store = RemixArtifactStore(tmp_path, 'demo')
    evidence = store.create('reference_evidence.v1', {
        'media_hash': 'sha256:' + 'a' * 64, 'time_base': '1/30000',
        'duration_us': 18_100_000, 'cuts': [], 'representative_frames': [],
    }, [])
    evidence_ref = {'artifact_id': evidence['id'], 'revision': 1}
    blueprint = store.create('reference_blueprint.v1', {
        'narrative_role': '短片', 'events': [], 'preserve_replace_rules': [],
        'observations': [], 'inferences': [], 'user_choices': [],
        'evidence_ref': evidence_ref,
    }, [evidence_ref])
    seen = {}

    async def generate(**kwargs):
        seen.update(kwargs)
        return {'schema_version': 2, 'goal': kwargs['topic'],
                'output': {'width': 1080, 'height': 1920, 'fps_num': 30, 'fps_den': 1},
                'beats': [{'id': 'hook', 'purpose': '开场', 'duration_frames': 90, 'turns': []}],
                'resources': [], 'placements': [], 'provenance': {}}

    monkeypatch.setattr(runner, 'generate_production_plan_draft', generate)
    runner.run_remix_generate_production_plan_draft(
        {'payload': {'topic': '保留原片节奏', 'blueprint_ref': {
            'artifact_id': blueprint['id'], 'revision': 1}}},
        SimpleNamespace(output_dir=str(tmp_path), project_id='demo',
                        requester_username='alice', requester_user_id='user-alice'))
    assert seen['source_timing']['duration_us'] == 18_100_000
    assert seen['source_timing']['evidence_ref'] == evidence_ref


def test_workflow_production_draft_publishes_one_editable_plan(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.task_backend.runners import remix as runner

    async def generate(*, topic, blueprint, username, locked_turns, on_stage):
        return {
            "schema_version": 2, "goal": topic, "output": {"width": 1920, "height": 1080, "fps_num": 24, "fps_den": 1},
            "beats": [{"id": "opening", "purpose": "开场", "duration_frames": 48,
                       "turns": []}], "resources": [], "placements": [], "provenance": {},
        }
    monkeypatch.setattr(runner, "generate_production_plan_draft", generate)
    result = runner.run_remix_generate_production_plan_draft(
        {"payload": {"topic": "横屏故事"}},
        SimpleNamespace(output_dir=str(tmp_path), project_id="demo", requester_username="alice", requester_user_id="user-alice"),
    )
    store = RemixArtifactStore(tmp_path, "demo")
    v2 = store.read(result["artifact_ref"]["artifact_id"], 1)
    assert v2["schema"] == "production_plan.v2"
    assert v2["payload"]["output"] == {"width": 1920, "height": 1080,
                                       "fps_num": 24, "fps_den": 1, "background": "#000000"}
    assert "source_plan_ref" not in v2["payload"]["provenance"]
    assert "source_plan_ref" not in result


def test_compile_production_plan_runner_saves_exact_v2_manifest(tmp_path):
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.task_backend.runners import remix as runner
    from test_production_plan_v2 import _anchor, _plan

    store = RemixArtifactStore(tmp_path, "demo", created_by="user-alice")
    plan = _plan()
    plan["resources"] = []
    plan["placements"] = [{"id": "card", "kind": "graphic", "role": "effect", "track_id": "graphic",
                           "graphic": {"template": "question_card", "text": "为什么？"},
                           "start": _anchor(0), "end": _anchor(48)}]
    saved = store.create("production_plan.v2", plan, [])
    ref = {"artifact_id": saved["id"], "revision": 1}
    result = runner.run_remix_compile_production_plan(
        {"payload": {"plan_ref": ref}},
        SimpleNamespace(output_dir=str(tmp_path), project_id="demo", requester_username="alice", requester_user_id="user-alice"),
    )
    manifest = store.read(result["artifact_ref"]["artifact_id"], 1)
    assert manifest["schema"] == "composition_manifest.v2"
    assert manifest["created_by"] == "user-alice"
    assert manifest["payload"]["source_versions"] == [ref]
    assert manifest["upstream_refs"] == [{**ref, "project_id": "demo"}]


def test_compile_blocks_program_duration_changed_after_speech_or_edit(tmp_path):
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.task_backend.runners import remix as runner
    from test_production_plan_v2 import _anchor, _plan

    store = RemixArtifactStore(tmp_path, 'demo', created_by='user-alice')
    plan = _plan()
    plan['resources'] = []
    plan['placements'] = [{
        'id': 'card', 'kind': 'graphic', 'role': 'effect', 'track_id': 'graphic',
        'graphic': {'template': 'question_card', 'text': 'Why?'},
        'start': _anchor(0), 'end': _anchor(48),
    }]
    plan['provenance']['requirements'] = {'duration_us': {'max': 1_000_000}}
    saved = store.create('production_plan.v2', plan, [])
    with pytest.raises(ValueError, match='duration_mismatch'):
        runner.run_remix_compile_production_plan(
            {'payload': {'plan_ref': {'artifact_id': saved['id'], 'revision': 1}}},
            SimpleNamespace(output_dir=str(tmp_path), project_id='demo',
                            requester_username='alice', requester_user_id='user-alice'))


def test_compile_rechecks_current_authenticated_duration(tmp_path):
    from novelvideo.freezone.remix.production_requirements import resolve_production_requirements
    from novelvideo.task_backend.runners import remix as runner
    from test_production_requirements import commission_fixture
    from test_production_plan_v2 import _anchor, _plan

    store, commission, _ = commission_fixture(tmp_path, {'duration_us': {'max': 1_000_000}})
    resolved = resolve_production_requirements(store, commission=commission)
    plan = _plan()
    plan['resources'] = []
    plan['placements'] = [{
        'id': 'card', 'kind': 'graphic', 'role': 'effect', 'track_id': 'graphic',
        'graphic': {'template': 'question_card', 'text': 'Why?'},
        'start': _anchor(0), 'end': _anchor(48),
    }]
    plan['provenance'] = {
        'requirements': {'duration_us': {'max': 10_000_000}},
        'requirements_fingerprint': resolved['requirements_fingerprint'],
        'requirements_provenance': resolved['requirements_provenance'],
    }
    saved = store.create('production_plan.v2', plan, [])
    with pytest.raises(ValueError, match='authenticated duration|requirements_fingerprint'):
        runner.run_remix_compile_production_plan(
            {'payload': {'plan_ref': {'artifact_id': saved['id'], 'revision': 1}}},
            SimpleNamespace(output_dir=str(tmp_path), project_id='p',
                            requester_username='u', requester_user_id='u'))


def test_take_timeline_runner_aligns_each_selected_video_to_its_own_text(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.task_backend.runners import remix as runner

    store = RemixArtifactStore(tmp_path, "demo")
    script = store.create("remix_script.v1", {
        "dialogue": "男：你好。\n女：再见。", "segments": [
            {"segment_id": "one", "start_char": 2, "end_char": 5, "speaker": "男", "source": "speaking_video"},
            {"segment_id": "two", "start_char": 8, "end_char": 11, "speaker": "女", "source": "speaking_video"},
        ], "selections": [], "events": [], "blueprint_mappings": [], "media_requirements": [],
    }, [])
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / "one.mp4").write_bytes(b"video one")
    (media_dir / "two.mp4").write_bytes(b"video two")
    seen = []

    def align(path, source):
        seen.append((path.name, source["dialogue"]))
        text = source["dialogue"]
        return {"schema": "word_timeline.v1", "status": "completed", "script_revision": source["revision"],
                "transcript": text, "media_hash": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                "language": "zh", "words": [{"text": text, "start_char": 0, "end_char": len(text), "start_us": 0, "end_us": 500_000}]}

    monkeypatch.setattr(runner, "align_script", align)
    monkeypatch.setattr(runner, "_media_duration_us", lambda path: 1_000_000)
    result = runner.run_remix_compile_take_timeline({"payload": {
        "script_ref": {"artifact_id": script["id"], "revision": 1}, "fps": 30,
        "take_bindings": [
            {"segment_id": "one", "media_path": "media/one.mp4", "media_sha256": hashlib.sha256(b"video one").hexdigest()},
            {"segment_id": "two", "media_path": "media/two.mp4", "media_sha256": hashlib.sha256(b"video two").hexdigest()},
        ],
    }}, SimpleNamespace(output_dir=str(tmp_path), project_id="demo"))
    artifact = store.read(result["artifact_ref"]["artifact_id"], result["artifact_ref"]["revision"])
    assert seen == [("one.mp4", "你好。"), ("two.mp4", "再见。")]
    assert artifact["schema"] == "performance_timing.v2"
    assert [(row["start_frame"], row["end_frame"]) for row in artifact["payload"]["takes"]] == [(0, 30), (30, 60)]
    assert len(artifact["upstream_refs"]) == 3  # exact script and two word timelines


def test_transcribe_runner_resolves_project_relative_media_and_publishes_timeline(tmp_path, monkeypatch):
    from novelvideo.task_backend.runners import remix as runner

    media = tmp_path / "media" / "voice.wav"
    media.parent.mkdir()
    media.write_bytes(b"voice")
    monkeypatch.setattr(
        runner,
        "transcribe_media",
        lambda path, language: {
            "schema": "word_timeline.v1", "media_hash": "sha256:voice", "language": language,
            "status": "completed", "transcript": "hi", "confidence_available": False,
            "words": [{"text": "hi", "start_char": 0, "end_char": 2, "start_us": 0, "end_us": 100_000}],
        },
    )
    result = runner.run_remix_transcribe_media(
        {"payload": {"media_path": "media/voice.wav", "language": "en", "media_sha256": VOICE_HASH}},
        SimpleNamespace(output_dir=str(tmp_path), project_id="demo"),
    )
    assert result["artifact_ref"]["revision"] == 1
    assert result["artifact_ref"]["artifact_id"]


def test_transcribe_runner_records_output_without_claiming_current_node(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.task_backend.runners import remix as runner

    media = tmp_path / "media" / "voice.wav"
    media.parent.mkdir()
    media.write_bytes(b"voice")
    operations = RemixOperationStore(tmp_path, "demo")
    operation = operations.start(
        idempotency_key="transcribe-1", input_refs=[], producer_version="remix-process.v1",
        normalized_parameters={"action": "transcribe_media"}, node_revision=7,
    )
    monkeypatch.setattr(runner, "transcribe_media", lambda path, language: {
        "schema": "word_timeline.v1", "media_hash": "sha256:voice", "language": language,
        "status": "completed", "transcript": "hi", "confidence_available": False,
        "words": [{"text": "hi", "start_char": 0, "end_char": 2, "start_us": 0, "end_us": 100_000}],
    })

    result = runner.run_remix_transcribe_media(
        {"payload": {"media_path": "media/voice.wav", "language": "en", "media_sha256": VOICE_HASH,
                     "operation_id": operation["operation_id"]}},
        SimpleNamespace(output_dir=str(tmp_path), project_id="demo"),
    )
    again = operations.start(
        idempotency_key="transcribe-1", input_refs=[], producer_version="remix-process.v1",
        normalized_parameters={"action": "transcribe_media"}, node_revision=7,
    )

    assert again["status"] == "needs_confirmation"
    assert again["output_refs"] == [result["artifact_ref"]]


def test_transcribe_runner_fails_task_when_newapi_is_unavailable(tmp_path, monkeypatch):
    from novelvideo.task_backend.runners.remix import run_remix_transcribe_media
    from novelvideo import config

    media = tmp_path / "media" / "voice.wav"
    media.parent.mkdir()
    media.write_bytes(b"voice")
    monkeypatch.setattr(config, "get_newapi_runtime_credentials", lambda: ("", ""))

    with pytest.raises(RuntimeError, match="configured gateway and API key"):
        run_remix_transcribe_media(
            {"payload": {"media_path": "media/voice.wav", "language": "en", "media_sha256": VOICE_HASH}},
            SimpleNamespace(output_dir=str(tmp_path), project_id="demo"),
        )


def test_align_runner_reads_exact_script_revision_and_publishes_timeline(tmp_path, monkeypatch):
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.task_backend.runners import remix as runner
    media = tmp_path / "media/voice.wav"
    media.parent.mkdir()
    media.write_bytes(b"voice")
    script = RemixArtifactStore(tmp_path, "demo").create("remix_script.v1", {"dialogue": "hi", "segments": [], "selections": [], "blueprint_mappings": [], "media_requirements": []}, [])
    monkeypatch.setattr(runner, "align_script", lambda path, value: {"schema": "word_timeline.v1", "media_hash": "sha256:voice", "script_revision": value["revision"], "language": "en", "status": "completed", "transcript": "hi", "confidence_available": False, "words": [{"text": "hi", "start_char": 0, "end_char": 2, "start_us": 0, "end_us": 1}]})
    result = runner.run_remix_align_script({"payload": {"media_path": "media/voice.wav", "media_sha256": VOICE_HASH, "script_ref": {"artifact_id": script["id"], "revision": 1}}}, SimpleNamespace(output_dir=str(tmp_path), project_id="demo"))
    assert result["artifact_ref"]["revision"] == 1


def test_transcribe_runner_rejects_media_changed_since_submission(tmp_path):
    from novelvideo.task_backend.runners.remix import run_remix_transcribe_media

    media = tmp_path / "media" / "voice.wav"
    media.parent.mkdir()
    media.write_bytes(b"replacement")

    with pytest.raises(ValueError, match="changed since submission"):
        run_remix_transcribe_media(
            {"payload": {"media_path": "media/voice.wav", "language": "en", "media_sha256": VOICE_HASH}},
            SimpleNamespace(output_dir=str(tmp_path), project_id="demo"),
        )


def test_compile_runner_uses_aligned_media_hash_and_exact_script_link(tmp_path):
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.task_backend.runners.remix import run_remix_compile_semantic_timeline

    store = RemixArtifactStore(tmp_path, "demo")
    media = tmp_path / "media" / "voice.wav"
    media.parent.mkdir()
    media.write_bytes(b"voice")
    media_hash = "sha256:" + hashlib.sha256(b"voice").hexdigest()
    blueprint = store.create("reference_blueprint.v1", {
        "narrative_role": "hook", "events": [], "preserve_replace_rules": [],
        "observations": [], "inferences": [], "user_choices": [],
    }, [])
    script = store.create("remix_script.v1", {
        "dialogue": "咖啡", "segments": [],
        "selections": [{"selection_id": "product", "start_char": 0, "end_char": 2, "script_revision": 1}],
        "events": [{"event_id": "product", "selection_id": "product", "required": True}],
        "blueprint_mappings": [], "media_requirements": [],
    }, [])
    script_ref = {"artifact_id": script["id"], "revision": 1}
    words = store.create("word_timeline.v1", {
        "media_hash": media_hash, "script_revision": 1, "language": "zh", "status": "completed",
        "words": [{"text": "咖啡", "start_char": 0, "end_char": 2, "start_us": 100_000, "end_us": 500_000}],
    }, [script_ref])

    result = run_remix_compile_semantic_timeline({"payload": {
        "script_ref": script_ref, "word_timeline_ref": {"artifact_id": words["id"], "revision": 1},
        "blueprint_ref": {"artifact_id": blueprint["id"], "revision": 1},
        "media_path": "media/voice.wav", "media_sha256": hashlib.sha256(b"voice").hexdigest(),
        "edit_map": [{"source_start_us": 0, "source_end_us": 1_000_000, "timeline_start_us": 0}],
        "overrides": [], "fps": 30,
    }}, SimpleNamespace(output_dir=str(tmp_path), project_id="demo"))

    assert result["status"] == "completed"
    compiled = store.read(**result["artifact_ref"])
    assert compiled["payload"]["media_hash"] == media_hash
    assert compiled["payload"]["script_revision"] == script["revision"]
    assert compiled["payload"]["alignment_revision"] == words["revision"]
    assert compiled["payload"]["events"] == [{"event_id": "product", "start_frame": 3, "end_frame": 15}]
    assert {ref["artifact_id"] for ref in compiled["upstream_refs"]} == {
        script["id"], words["id"], blueprint["id"],
    }


def test_compile_runner_rejects_unlinked_word_timeline(tmp_path):
    from novelvideo.freezone.remix.store import RemixArtifactStore
    from novelvideo.task_backend.runners.remix import run_remix_compile_semantic_timeline

    store = RemixArtifactStore(tmp_path, "demo")
    media = tmp_path / "media" / "voice.wav"
    media.parent.mkdir()
    media.write_bytes(b"voice")
    blueprint = store.create("reference_blueprint.v1", {
        "narrative_role": "hook", "events": [], "preserve_replace_rules": [],
        "observations": [], "inferences": [], "user_choices": [],
    }, [])
    script = store.create("remix_script.v1", {
        "dialogue": "hello", "segments": [], "selections": [],
        "blueprint_mappings": [], "media_requirements": [],
    }, [])
    words = store.create("word_timeline.v1", {
        "media_hash": "sha256:other", "script_revision": 1, "language": "en", "status": "completed",
        "words": [{"text": "hello", "start_char": 0, "end_char": 5, "start_us": 0, "end_us": 100_000}],
    }, [])

    with pytest.raises(ValueError, match="does not belong"):
        run_remix_compile_semantic_timeline({"payload": {
            "script_ref": {"artifact_id": script["id"], "revision": 1},
            "word_timeline_ref": {"artifact_id": words["id"], "revision": 1},
            "blueprint_ref": {"artifact_id": blueprint["id"], "revision": 1},
            "media_path": "media/voice.wav", "media_sha256": hashlib.sha256(b"voice").hexdigest(),
            "edit_map": [], "overrides": [], "fps": 30,
        }}, SimpleNamespace(output_dir=str(tmp_path), project_id="demo"))


@pytest.mark.parametrize("error,status", [(RuntimeError("provider failed"), "failed"), (TimeoutError("unknown"), "pending_reconciliation")])
def test_runner_failure_transitions_bound_operation(tmp_path, error, status):
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.task_backend.runners.remix import _with_operation_failure_transition
    operations = RemixOperationStore(tmp_path, "demo")
    operation = operations.start(idempotency_key="failure", input_refs=[], producer_version="remix-process.v1", normalized_parameters={}, node_revision=1)
    def failing(_envelope, _ctx):
        raise error
    guarded = _with_operation_failure_transition(failing)
    with pytest.raises(type(error)):
        guarded({"task_id": "task-1", "payload": {"operation_id": operation["operation_id"]}}, SimpleNamespace(output_dir=str(tmp_path), project_id="demo"))
    assert operations.get(operation["operation_id"])["status"] == status


def test_runner_timeout_during_cancel_preserves_unknown_cancellation_for_reconciliation(tmp_path):
    from novelvideo.freezone.remix.operations import RemixOperationStore
    from novelvideo.task_backend.runners.remix import _with_operation_failure_transition
    operations = RemixOperationStore(tmp_path, "demo")
    operation = operations.start(idempotency_key="cancel-timeout", input_refs=[], producer_version="remix-process.v1", normalized_parameters={}, node_revision=1)
    operations.claim_cancellation(operation["operation_id"], reason="user_cancelled", owner="test-request")
    def timing_out(_envelope, _ctx):
        raise TimeoutError("unknown")
    guarded = _with_operation_failure_transition(timing_out)
    with pytest.raises(TimeoutError):
        guarded({"task_id": "task-1", "payload": {"operation_id": operation["operation_id"]}}, SimpleNamespace(output_dir=str(tmp_path), project_id="demo"))
    assert operations.get(operation["operation_id"])["status"] == "pending_reconciliation"
