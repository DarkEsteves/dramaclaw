"""JSON subprocess entry point for an isolated WhisperX Python installation."""

from __future__ import annotations

from contextlib import redirect_stdout
import json
from pathlib import Path
import sys

from whisperx_adapter import WhisperXSpeechAligner


def main() -> None:
    request = json.load(sys.stdin)
    aligner = WhisperXSpeechAligner(
        model_cache=Path(request["model_cache"]),
        model_name=request["model_name"],
        device=request["device"],
        compute_type=request["compute_type"],
        batch_size=request["batch_size"],
    )
    with redirect_stdout(sys.stderr):
        if request["action"] == "transcribe":
            result = aligner.transcribe(Path(request["media_path"]), request["language"])
        elif request["action"] == "align":
            result = aligner.align(Path(request["media_path"]), request["script"], request["language"])
        else:
            raise ValueError("unknown WhisperX worker action")
    json.dump(result, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
