from pathlib import Path
import shutil
import subprocess

import pytest
from pydantic import ValidationError

from novelvideo.api.schemas import FreezoneAudioSpeechRequest, FreezoneAudioVoiceRef
from novelvideo.freezone import audio_node
from novelvideo.task_backend.projection import ProjectProjection


def test_dialogue_contract_requires_a_voice_and_plain_spoken_text() -> None:
    voice = FreezoneAudioVoiceRef(scope="user_custom", voice_id="voice-a")
    request = FreezoneAudioSpeechRequest(
        text="你好。\n再见。",
        dialogue_turns=[
            {"speaker": "男", "text": "你好。", "voice_ref": voice.model_dump()},
            {"speaker": "女", "text": "再见。", "voice_ref": voice.model_dump()},
        ],
    )
    assert [turn.speaker for turn in request.dialogue_turns] == ["男", "女"]
    with pytest.raises(ValidationError):
        FreezoneAudioSpeechRequest(text="你好。", dialogue_turns=[{"speaker": "男", "text": "男：你好。"}])


@pytest.mark.asyncio
async def test_dialogue_generation_sends_only_spoken_lines_in_order(monkeypatch, tmp_path: Path) -> None:
    calls = []

    async def fake_single(**kwargs):
        calls.append((kwargs["text"], kwargs["voice_ref"]["voice_id"]))
        path = audio_node.freezone_audio_speech_output_path(tmp_path, kwargs["job_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"part")
        return audio_node.FreezoneAudioSpeechResult(path, 100, "audio/mpeg", "model", "voice", "hash")

    def fake_concat(parts, output):
        output.write_bytes(b"joined")

    monkeypatch.setattr(audio_node, "generate_freezone_audio_speech", fake_single)
    monkeypatch.setattr(audio_node, "_concat_dialogue_audio", fake_concat, raising=False)
    monkeypatch.setattr(audio_node, "_duration_ms", lambda _: 200)
    result = await audio_node.generate_freezone_dialogue_speech(
        store=None, username="u", project="p", project_dir=tmp_path, job_id="job",
        emotion_prompt="", projection=None,
        turns=[
            {"speaker": "男", "text": "你好。", "voice_ref": {"scope": "user_custom", "voice_id": "m"}},
            {"speaker": "女", "text": "再见。", "voice_ref": {"scope": "user_custom", "voice_id": "f"}},
        ],
    )
    assert calls == [("你好。", "m"), ("再见。", "f")]
    assert result.audio_path.read_bytes() == b"joined"


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg tools unavailable")
def test_dialogue_concat_produces_one_playable_track(tmp_path: Path) -> None:
    parts = []
    for index in range(2):
        part = tmp_path / f"turn-{index}.mp3"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency={440 + index * 220}:duration=0.2",
            "-c:a", "libmp3lame", str(part),
        ], check=True, capture_output=True)
        parts.append(part)
    output = tmp_path / "dialogue.mp3"
    audio_node._concat_dialogue_audio(parts, output)
    assert output.is_file()
    assert 350 <= audio_node._duration_ms(output) <= 500


@pytest.mark.asyncio
async def test_dialogue_generation_selects_the_projected_character_for_each_turn(monkeypatch, tmp_path: Path) -> None:
    names = []

    async def fake_single(**kwargs):
        names.append(kwargs["projection"].require("voice_character")["name"])
        path = audio_node.freezone_audio_speech_output_path(tmp_path, kwargs["job_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"part")
        return audio_node.FreezoneAudioSpeechResult(path, 100, "audio/mpeg", "model", "voice", "hash")

    monkeypatch.setattr(audio_node, "generate_freezone_audio_speech", fake_single)
    monkeypatch.setattr(audio_node, "_concat_dialogue_audio", lambda parts, output: output.write_bytes(b"joined"))
    monkeypatch.setattr(audio_node, "_duration_ms", lambda _: 200)
    projection = ProjectProjection(task_type="freezone_audio_speech", projection_version=1, fields={
        "voice_character": None,
        "voice_characters": {"张三": {"name": "张三"}, "李四": {"name": "李四"}},
    })
    await audio_node.generate_freezone_dialogue_speech(
        store=None, username="u", project="p", project_dir=tmp_path, job_id="job",
        projection=projection, turns=[
            {"speaker": "甲", "text": "你好", "voice_ref": {"scope": "character_default", "character_name": "张三"}},
            {"speaker": "乙", "text": "再见", "voice_ref": {"scope": "character_default", "character_name": "李四"}},
        ],
    )
    assert names == ["张三", "李四"]
