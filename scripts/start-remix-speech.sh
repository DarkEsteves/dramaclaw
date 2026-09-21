#!/usr/bin/env bash
# Start the CE API with the repository's isolated, download-disabled WhisperX setup.
set -euo pipefail

root_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root_dir"

api_host="${NOVELVIDEO_API_HOST:-127.0.0.1}"
api_port="${NOVELVIDEO_API_PORT:-8780}"
ready_timeout="${NOVELVIDEO_API_READY_TIMEOUT:-60}"
whisperx_python="${DRAMACLAW_REMIX_WHISPERX_PYTHON:-$HOME/.local/share/dramaclaw/whisperx-venv/bin/python}"
model_cache="${DRAMACLAW_REMIX_MODEL_CACHE:-$HOME/.cache/dramaclaw/whisperx-models}"
api_pid=""
response_file="$(mktemp -t dramaclaw-remix-speech.XXXXXX)"

cleanup() {
  trap - INT TERM EXIT
  rm -f "$response_file"
  if [ -n "$api_pid" ] && kill -0 "$api_pid" >/dev/null 2>&1; then
    kill "$api_pid" >/dev/null 2>&1 || true
    wait "$api_pid" 2>/dev/null || true
  fi
}
trap cleanup INT TERM EXIT

command -v uv >/dev/null 2>&1 || {
  echo "uv is required: https://docs.astral.sh/uv/" >&2
  exit 2
}
command -v curl >/dev/null 2>&1 || {
  echo "curl is required for the speech readiness check." >&2
  exit 2
}
if [ ! -x "$root_dir/.venv/bin/python" ]; then
  echo "The project environment is missing. Run: uv sync --group dev" >&2
  exit 2
fi
if [ ! -x "$whisperx_python" ]; then
  echo "WhisperX Python is missing or not executable: $whisperx_python" >&2
  exit 2
fi
if [ ! -d "$model_cache" ]; then
  echo "WhisperX model cache directory is missing: $model_cache" >&2
  exit 2
fi
if ! find "$model_cache" -mindepth 1 -print -quit | grep -q .; then
  echo "WhisperX model cache directory is empty: $model_cache" >&2
  exit 2
fi

# Local development is explicitly single tenant CE. Empty values override any
# inherited distributed or control-plane configuration.
export ST_EDITION=ce
export ST_CONTROL_PLANE_DSN=
export ST_REDIS_URL=
export ST_CELERY_BROKER_URL=
export ST_CELERY_RESULT_BACKEND=
export DRAMACLAW_REMIX_SPEECH_ENGINE=whisperx
export DRAMACLAW_REMIX_WHISPERX_PYTHON="$whisperx_python"
export DRAMACLAW_REMIX_MODEL_CACHE="$model_cache"
export DRAMACLAW_REMIX_WHISPER_MODEL="${DRAMACLAW_REMIX_WHISPER_MODEL:-small}"
export DRAMACLAW_REMIX_DEVICE="${DRAMACLAW_REMIX_DEVICE:-cpu}"
export DRAMACLAW_REMIX_COMPUTE_TYPE="${DRAMACLAW_REMIX_COMPUTE_TYPE:-int8}"
export DRAMACLAW_REMIX_BATCH_SIZE="${DRAMACLAW_REMIX_BATCH_SIZE:-4}"

echo "Starting CE remix speech API at http://${api_host}:${api_port}"
echo "WhisperX Python: $whisperx_python"
echo "Model cache: $model_cache"
uv run --no-sync novelvideo api --host "$api_host" --port "$api_port" &
api_pid="$!"

ready_url="http://127.0.0.1:${api_port}/api/v1/projects/local/freezone/remix/speech-readiness"
deadline=$((SECONDS + ready_timeout))
while true; do
  if ! kill -0 "$api_pid" >/dev/null 2>&1; then
    echo "API process exited before speech became ready." >&2
    exit 1
  fi
  if curl -fsS --max-time 2 "$ready_url" >"$response_file" 2>/dev/null; then
    if "$root_dir/.venv/bin/python" - "$response_file" <<'PY'
import json
import sys

payload = json.loads(open(sys.argv[1], encoding="utf-8").read())
data = payload.get("data", {})
if not data.get("ready"):
    print(data.get("error") or "speech engine is not ready", file=sys.stderr)
    raise SystemExit(1)
PY
    then
      echo "Speech engine is ready: $ready_url"
      break
    fi
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "Speech readiness check failed after ${ready_timeout}s: $ready_url" >&2
    [ ! -s "$response_file" ] || cat "$response_file" >&2
    exit 1
  fi
  sleep 1
done

echo "Press Ctrl+C to stop."
wait "$api_pid"
