"""Project-local media upload for reference video remix."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from novelvideo.api.auth import get_api_user
from novelvideo.api.deps import make_static_url_for_context
from novelvideo.project_context import require_project_home_node, resolve_project_context
from novelvideo.utils.upload_safety import (
    UploadTooLargeError,
    create_staged_upload_file,
    stream_to_file_with_limit,
)


router = APIRouter(prefix="/projects/{project}/freezone/remix", tags=["freezone-remix"])
MAX_REMIX_MEDIA_BYTES = 500 * 1024 * 1024
_EXTENSIONS = {
    ".mp4": "video", ".mov": "video", ".mkv": "video", ".webm": "video",
    ".mp3": "audio", ".m4a": "audio", ".wav": "audio", ".aac": "audio",
    ".ogg": "audio", ".flac": "audio",
}


class AttachMediaBody(BaseModel):
    media_path: str = Field(min_length=1)


def _probe_media(path: Path, expected_kind: str) -> tuple[int, bool]:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        metadata = json.loads(result.stdout)
        streams = metadata.get("streams", [])
        if not any(stream.get("codec_type") == expected_kind for stream in streams):
            raise ValueError("Uploaded file has no matching media stream")
        duration = float(metadata.get("format", {}).get("duration", 0))
        if not 0 < duration < 24 * 60 * 60:
            raise ValueError("Uploaded media duration is invalid")
        has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
        return round(duration * 1_000_000), has_audio
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("Uploaded file is not valid supported media") from exc


def _save_media(stream, project_dir: Path, suffix: str, media_kind: str) -> dict:
    target_dir = project_dir / "freezone" / "remix_media"
    target_dir.mkdir(parents=True, exist_ok=True)
    staged = create_staged_upload_file(target_dir, suffix=".tmp")
    try:
        size = stream_to_file_with_limit(stream, staged, max_bytes=MAX_REMIX_MEDIA_BYTES)
        if size == 0:
            raise ValueError("Uploaded file is empty")
        duration_us, has_audio = _probe_media(staged, media_kind)
        with staged.open("rb") as media_file:
            media_hash = hashlib.file_digest(media_file, "sha256").hexdigest()
        target = target_dir / f"{uuid.uuid4().hex}{suffix}"
        staged.replace(target)
        return {
            "media_path": target.relative_to(project_dir).as_posix(),
            "media_hash": media_hash,
            "media_kind": media_kind,
            "duration_us": duration_us,
            "has_audio": has_audio,
            "size": size,
        }
    finally:
        staged.unlink(missing_ok=True)


def _inspect_existing_media(project_dir: Path, media_path: str) -> dict:
    relative = Path(media_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Media must be a project-relative file")
    path = (project_dir / relative).resolve()
    if not path.is_relative_to(project_dir) or not path.is_file():
        raise ValueError("Media must be an existing project file")
    media_kind = _EXTENSIONS.get(path.suffix.lower())
    if media_kind is None:
        raise ValueError("Unsupported remix media extension")
    size = path.stat().st_size
    if size == 0 or size > MAX_REMIX_MEDIA_BYTES:
        raise ValueError("Media size is outside the supported range")
    duration_us, has_audio = _probe_media(path, media_kind)
    with path.open("rb") as media_file:
        media_hash = hashlib.file_digest(media_file, "sha256").hexdigest()
    return {
        "media_path": path.relative_to(project_dir).as_posix(),
        "media_hash": media_hash,
        "media_kind": media_kind,
        "duration_us": duration_us,
        "has_audio": has_audio,
        "size": size,
    }


@router.post("/media/attach")
async def attach_existing_remix_media(
    project: str,
    body: AttachMediaBody,
    user: dict = Depends(get_api_user),
):
    ctx = await resolve_project_context(user=user, project_id=project, required_role="editor")
    require_project_home_node(ctx, operation="attach remix media")
    project_dir = Path(ctx.output_dir).resolve()
    try:
        result = await run_in_threadpool(_inspect_existing_media, project_dir, body.media_path)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(503, "Remix media inspection is temporarily unavailable") from exc
    result["url"] = make_static_url_for_context(ctx, result["media_path"], local_path=project_dir / result["media_path"])
    return {"ok": True, "data": result}


@router.post("/media")
async def upload_remix_media(
    project: str,
    file: Annotated[UploadFile, File()],
    user: dict = Depends(get_api_user),
):
    ctx = await resolve_project_context(user=user, project_id=project, required_role="editor")
    require_project_home_node(ctx, operation="upload remix media")
    suffix = Path(file.filename or "").suffix.lower()
    media_kind = _EXTENSIONS.get(suffix)
    if media_kind is None:
        raise HTTPException(422, "Unsupported remix media extension")
    project_dir = Path(ctx.output_dir).resolve()
    try:
        result = await run_in_threadpool(_save_media, file.file, project_dir, suffix, media_kind)
    except UploadTooLargeError as exc:
        raise HTTPException(413, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(503, "Remix media upload is temporarily unavailable") from exc
    result["url"] = make_static_url_for_context(ctx, result["media_path"], local_path=project_dir / result["media_path"])
    return {"ok": True, "data": result}
