from __future__ import annotations

from types import SimpleNamespace
import hashlib

import pytest


VOICE_HASH = hashlib.sha256(b"voice").hexdigest()


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


def test_transcribe_runner_fails_task_when_engine_is_unavailable(tmp_path):
    from novelvideo.task_backend.runners.remix import run_remix_transcribe_media

    media = tmp_path / "media" / "voice.wav"
    media.parent.mkdir()
    media.write_bytes(b"voice")

    with pytest.raises(RuntimeError, match="no speech aligner"):
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
    operations.claim_cancellation(operation["operation_id"], reason="user_cancelled")
    def timing_out(_envelope, _ctx):
        raise TimeoutError("unknown")
    guarded = _with_operation_failure_transition(timing_out)
    with pytest.raises(TimeoutError):
        guarded({"task_id": "task-1", "payload": {"operation_id": operation["operation_id"]}}, SimpleNamespace(output_dir=str(tmp_path), project_id="demo"))
    assert operations.get(operation["operation_id"])["status"] == "pending_reconciliation"
