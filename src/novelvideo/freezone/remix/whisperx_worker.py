"""JSON subprocess entry point for an isolated WhisperX Python installation."""

from __future__ import annotations

from contextlib import redirect_stdout
import json
from pathlib import Path
import sys

# The external WhisperX environment launches this file by absolute path. In that
# mode Python only places ``.../novelvideo/freezone/remix`` on ``sys.path``, so a
# sibling import loses the package context required by whisperx_adapter's
# relative imports. Add the package root explicitly and always import through
# the canonical package name; this works both from a source checkout and an
# installed site-packages tree.
package_root = Path(__file__).resolve().parents[3]
if str(package_root) not in sys.path:
    sys.path.insert(0, str(package_root))

from novelvideo.freezone.remix.whisperx_adapter import WhisperXSpeechAligner  # noqa: E402


def main() -> None:
    request = json.load(sys.stdin)
    if request["action"] == "probe":
        import whisperx

        json.dump(
            {"whisperx_version": getattr(whisperx, "__version__", "unknown")},
            sys.stdout,
        )
        return
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
