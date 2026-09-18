from __future__ import annotations

import json
from pathlib import Path

import pytest

from novelvideo.freezone.remix.store import RemixArtifactConflict, RemixArtifactStore
from novelvideo.freezone.remix.timing import compile_timing


def evidence_payload() -> dict:
    return {
        "media_hash": "sha256:reference",
        "time_base": "1/90000",
        "duration_us": 1_000_000,
        "cuts": [{"start_us": 0, "end_us": 1_000_000}],
        "representative_frames": [{"pts": 0, "time_us": 0}],
        "transcript_ref": {"artifact_id": "transcript", "revision": 1},
    }


def test_create_persists_a_validated_versioned_artifact(tmp_path: Path) -> None:
    store = RemixArtifactStore(tmp_path, "project-a", created_by="editor")

    saved = store.create(
        schema="reference_evidence.v1", payload=evidence_payload(), upstream_refs=[]
    )

    assert saved["schema"] == "reference_evidence.v1"
    assert saved["revision"] == 1
    assert saved["project_id"] == "project-a"
    assert saved["created_by"] == "editor"
    assert saved["content_hash"].startswith("sha256:")
    assert saved["payload"] == evidence_payload()
    assert (
        tmp_path / "freezone" / "_remix_artifacts" / saved["id"] / "1.json"
    ).is_file()
    assert not Path(saved["payload_path"]).is_absolute()


def test_reference_evidence_can_be_published_before_transcription(
    tmp_path: Path,
) -> None:
    payload = evidence_payload()
    payload.pop("transcript_ref")

    saved = RemixArtifactStore(tmp_path, "project-a").create(
        schema="reference_evidence.v1", payload=payload, upstream_refs=[]
    )

    assert "transcript_ref" not in saved["payload"]


def test_original_video_word_timeline_does_not_require_a_script(tmp_path: Path) -> None:
    payload = {
        "media_ref": {"artifact_id": "evidence", "revision": 1},
        "language": "zh",
        "words": [
            {
                "text": "你好",
                "start_char": 0,
                "end_char": 2,
                "start_us": 0,
                "end_us": 1,
                "status": "matched",
            }
        ],
    }

    saved = RemixArtifactStore(tmp_path, "project-a").create(
        schema="word_timeline.v1", payload=payload, upstream_refs=[]
    )

    assert "script_ref" not in saved["payload"]


def test_timing_compiler_output_can_be_published(tmp_path: Path) -> None:
    payload = compile_timing(
        script={
            "revision": 7,
            "media_hash": "voice-a",
            "selections": [
                {
                    "selection_id": "line",
                    "start_char": 0,
                    "end_char": 2,
                    "script_revision": 7,
                }
            ],
            "events": [
                {"event_id": "product", "selection_id": "line", "required": True}
            ],
        },
        words={
            "script_revision": 7,
            "media_hash": "voice-a",
            "words": [
                {"start_char": 0, "end_char": 2, "start_us": 0, "end_us": 100_000}
            ],
        },
        edit_map=[
            {"source_start_us": 0, "source_end_us": 100_000, "timeline_start_us": 0}
        ],
        overrides=[],
        fps=30,
    )

    saved = RemixArtifactStore(tmp_path, "project-a").create(
        schema="performance_timing.v1", payload=payload, upstream_refs=[]
    )

    assert saved["payload"] == payload


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        ("reference_evidence.v1", evidence_payload()),
        (
            "word_timeline.v1",
            {
                "media_ref": {"artifact_id": "evidence", "revision": 1},
                "script_ref": {"artifact_id": "script", "revision": 1},
                "language": "zh",
                "status": "completed",
                "words": [
                    {
                        "text": "你好",
                        "start_char": 0,
                        "end_char": 2,
                        "start_us": 0,
                        "end_us": 1,
                        "status": "matched",
                    }
                ],
            },
        ),
        (
            "reference_blueprint.v1",
            {
                "narrative_role": "hook",
                "events": [
                    {
                        "id": "event-1",
                        "evidence_refs": [{"artifact_id": "evidence", "revision": 1}],
                    }
                ],
                "preserve_replace_rules": [],
                "observations": [],
                "inferences": [],
                "user_choices": [],
            },
        ),
        (
            "remix_script.v1",
            {
                "dialogue": "hello",
                "segments": [{"id": "segment-1", "text": "hello"}],
                "selections": [{"id": "selection-1", "start_char": 0, "end_char": 5}],
                "blueprint_mappings": [],
                "media_requirements": [],
            },
        ),
        (
            "performance_timing.v1",
            {
                "schema": "performance_timing.v1",
                "script_revision": 1,
                "media_hash": "voice-a",
                "fps": 30,
                "events": [{"event_id": "event-1", "start_frame": 0, "end_frame": 1}],
                "unresolved_required_events": [],
                "override_conflicts": [],
            },
        ),
        (
            "composition_manifest.v1",
            {
                "output_spec": {
                    "width": 1080,
                    "height": 1920,
                    "fps": 30,
                    "duration_frames": 30,
                },
                "media_bindings": [
                    {"media_id": "background", "kind": "image", "media_hash": "sha256:" + "0" * 64},
                    {"media_id": "voice", "kind": "audio", "media_hash": "sha256:" + "0" * 64},
                ],
                "layers": [
                    {
                        "media_id": "background",
                        "start_frame": 0,
                        "end_frame": 30,
                        "z_order": 0,
                    }
                ],
                "subtitle_events": [],
                "audio_events": [
                    {
                        "media_id": "voice",
                        "role": "narration",
                        "start_frame": 0,
                        "end_frame": 30,
                        "volume": 1.0,
                    }
                ],
                "manual_overrides": [],
                "source_versions": [],
            },
        ),
    ],
)
def test_each_v1_schema_has_a_valid_structured_payload(
    tmp_path: Path, schema: str, payload: dict
) -> None:
    saved = RemixArtifactStore(tmp_path, "project-a").create(
        schema=schema, payload=payload, upstream_refs=[]
    )

    assert saved["payload"] == payload


def test_invalid_schema_or_invalid_payload_never_publishes(tmp_path: Path) -> None:
    store = RemixArtifactStore(tmp_path, "project-a")

    with pytest.raises(ValueError, match="Unsupported remix artifact schema"):
        store.create(schema="not-a-remix-schema", payload={}, upstream_refs=[])
    with pytest.raises(ValueError, match="reference_evidence.v1"):
        store.create(
            schema="reference_evidence.v1", payload={"duration_us": 1}, upstream_refs=[]
        )

    assert not (tmp_path / "freezone" / "_remix_artifacts").exists()


def test_composition_manifest_with_missing_narration_cannot_be_published(
    tmp_path: Path,
) -> None:
    store = RemixArtifactStore(tmp_path, "project-a")
    invalid = {
        "output_spec": {"width": 180, "height": 320, "fps": 30, "duration_frames": 60},
        "media_bindings": [{"media_id": "background", "kind": "image", "media_hash": "sha256:" + "0" * 64}],
        "layers": [
            {"media_id": "background", "start_frame": 0, "end_frame": 60, "z_order": 0}
        ],
        "subtitle_events": [],
        "audio_events": [],
        "manual_overrides": [],
        "source_versions": [],
    }
    with pytest.raises(ValueError, match="narration"):
        store.create("composition_manifest.v1", invalid, [])


def test_normalized_original_video_transcript_is_a_valid_word_timeline(
    tmp_path: Path,
) -> None:
    from novelvideo.freezone.remix.alignment import transcribe_media

    class LocalAligner:
        def transcribe(self, media_path: Path, language: str) -> dict:
            return {
                "status": "completed",
                "words": [{"text": "你好", "start_us": 0, "end_us": 400_000}],
            }

    media = tmp_path / "narration.wav"
    media.write_bytes(b"voice")
    normalized = transcribe_media(media, "zh", aligner=LocalAligner())

    saved = RemixArtifactStore(tmp_path, "project-a").create(
        "word_timeline.v1", normalized, []
    )
    assert saved["payload"]["media_hash"] == normalized["media_hash"]
    assert saved["payload"]["words"][0]["end_char"] == 2


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (
            "word_timeline.v1",
            {
                "media_ref": {"artifact_id": "evidence", "revision": 1},
                "language": "en",
                "words": [
                    {
                        "text": "bad",
                        "start_char": 2,
                        "end_char": 2,
                        "start_us": 0,
                        "end_us": 1,
                    }
                ],
            },
        ),
        (
            "performance_timing.v1",
            {
                "schema": "performance_timing.v1",
                "script_revision": 1,
                "media_hash": "voice-a",
                "fps": 30,
                "events": [{"event_id": "event-1", "start_frame": 4, "end_frame": 4}],
                "unresolved_required_events": [],
                "override_conflicts": [],
            },
        ),
    ],
)
def test_malformed_timing_ranges_are_rejected(
    tmp_path: Path, schema: str, payload: dict
) -> None:
    with pytest.raises(ValueError, match="payload is invalid"):
        RemixArtifactStore(tmp_path, "project-a").create(
            schema=schema, payload=payload, upstream_refs=[]
        )


def test_revisions_are_immutable_and_compare_and_swap(tmp_path: Path) -> None:
    store = RemixArtifactStore(tmp_path, "project-a")
    first = store.create(
        schema="reference_evidence.v1", payload=evidence_payload(), upstream_refs=[]
    )
    second_payload = {**evidence_payload(), "duration_us": 2_000_000}

    second = store.revise(
        first["id"], expected_revision=1, payload=second_payload, upstream_refs=[]
    )

    assert store.read(first["id"], 1)["payload"]["duration_us"] == 1_000_000
    assert store.read(first["id"], 2) == second
    with pytest.raises(RemixArtifactConflict):
        store.revise(
            first["id"],
            expected_revision=1,
            payload=evidence_payload(),
            upstream_refs=[],
        )


def test_cross_project_references_and_storage_are_rejected(tmp_path: Path) -> None:
    owner = RemixArtifactStore(tmp_path, "project-a")
    artifact = owner.create(
        schema="reference_evidence.v1", payload=evidence_payload(), upstream_refs=[]
    )
    other = RemixArtifactStore(tmp_path, "project-b")

    with pytest.raises(ValueError, match="project"):
        other.create(
            schema="reference_evidence.v1",
            payload=evidence_payload(),
            upstream_refs=[
                {
                    "artifact_id": artifact["id"],
                    "revision": 1,
                    "project_id": "project-a",
                }
            ],
        )
    with pytest.raises(FileNotFoundError):
        other.create(
            schema="reference_evidence.v1",
            payload=evidence_payload(),
            upstream_refs=[{"artifact_id": artifact["id"], "revision": 1}],
        )
    with pytest.raises(FileNotFoundError):
        other.read(artifact["id"], 1)


def test_missing_payload_is_not_served_from_a_cache(tmp_path: Path) -> None:
    store = RemixArtifactStore(tmp_path, "project-a")
    artifact = store.create(
        schema="reference_evidence.v1", payload=evidence_payload(), upstream_refs=[]
    )
    envelope_path = (
        tmp_path / "freezone" / "_remix_artifacts" / artifact["id"] / "1.json"
    )
    payload_path = (
        envelope_path.parent / json.loads(envelope_path.read_text())["payload_path"]
    )
    payload_path.unlink()

    with pytest.raises(FileNotFoundError):
        store.read(artifact["id"], 1)


def test_symlinked_payload_is_rejected(tmp_path: Path) -> None:
    store = RemixArtifactStore(tmp_path, "project-a")
    artifact = store.create(
        schema="reference_evidence.v1", payload=evidence_payload(), upstream_refs=[]
    )
    envelope_path = (
        tmp_path / "freezone" / "_remix_artifacts" / artifact["id"] / "1.json"
    )
    payload_path = (
        envelope_path.parent / json.loads(envelope_path.read_text())["payload_path"]
    )
    replacement = tmp_path / "replacement.json"
    replacement.write_text("{}")
    payload_path.unlink()
    payload_path.symlink_to(replacement)

    with pytest.raises(FileNotFoundError):
        store.read(artifact["id"], 1)


def test_interrupted_publish_never_exposes_an_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RemixArtifactStore(tmp_path, "project-a")
    import novelvideo.freezone.remix.store as store_module

    original_replace = store_module.os.replace

    def fail_envelope_publish(source: Path, target: Path) -> None:
        if target.name == "1.json":
            raise OSError("simulated interruption")
        original_replace(source, target)

    monkeypatch.setattr(store_module.os, "replace", fail_envelope_publish)

    with pytest.raises(OSError, match="simulated interruption"):
        store.create(
            schema="reference_evidence.v1", payload=evidence_payload(), upstream_refs=[]
        )

    artifact_directories = list((tmp_path / "freezone" / "_remix_artifacts").iterdir())
    assert len(artifact_directories) == 1
    assert not (artifact_directories[0] / "1.json").exists()
