"""The local preview frame limit and shared EE capacity error contract."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from contextlib import asynccontextmanager


class PreviewBusy(Exception):
    """All preview rendering slots are in use."""


class PreviewUnavailable(Exception):
    """A preview slot cannot be safely held."""


class _LocalLease:
    async def ensure_current(self) -> None:
        return None


class LocalPreviewCapacity:
    def __init__(
        self, limit: int = 2, *, max_render_seconds: float = 300.0,
        worker_command: tuple[str, ...] | None = None,
    ) -> None:
        self._semaphore = asyncio.Semaphore(limit)
        self.max_render_seconds = max_render_seconds
        self.worker_command = worker_command or (
            sys.executable, "-m", "novelvideo.freezone.remix.preview_worker",
        )

    @asynccontextmanager
    async def slot(self, *, wait_seconds: float = 5.0):
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=wait_seconds)
        except TimeoutError as exc:
            raise PreviewBusy("preview renderer is busy") from exc
        try:
            yield _LocalLease()
        finally:
            self._semaphore.release()

    async def render_frame(
        self, lease: _LocalLease, manifest: dict, *, media_paths: dict,
        frame: int, output_path,
    ) -> None:
        payload = json.dumps({
            "manifest": manifest,
            "media_paths": {key: str(path) for key, path in media_paths.items()},
            "frame": frame,
            "output_path": str(output_path),
        }).encode("utf-8")
        try:
            process = await asyncio.create_subprocess_exec(
                *self.worker_command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise PreviewUnavailable("preview renderer could not start") from exc
        try:
            await asyncio.wait_for(
                process.communicate(payload), timeout=self.max_render_seconds,
            )
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, self.worker_command)
        except BaseException as exc:
            # Cancellation must stop the worker and every FFmpeg descendant
            # before the semaphore can be released by slot().
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process.returncode is None:
                await process.wait()
            if isinstance(exc, TimeoutError):
                raise PreviewUnavailable("preview render exceeded its time budget") from exc
            raise
