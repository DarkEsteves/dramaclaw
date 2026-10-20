"""Run one exact preview in an isolated process owned by the EE capacity port."""

from __future__ import annotations

import json
import sys
from pathlib import Path

def main() -> None:
    # On Linux, FFmpeg inherits this address-space limit. This bounds malformed
    # source images and filter graphs even when output dimensions are modest.
    if sys.platform.startswith("linux"):
        import resource

        memory_limit = 4 * 1024 * 1024 * 1024
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = min(memory_limit, hard) if hard != resource.RLIM_INFINITY else memory_limit
        soft_limit = limit if soft == resource.RLIM_INFINITY else min(soft, limit)
        resource.setrlimit(resource.RLIMIT_AS, (soft_limit, hard))
    from novelvideo.freezone.remix.render import render_preview_frame

    request = json.load(sys.stdin)
    render_preview_frame(
        request["manifest"],
        media_paths={key: Path(value) for key, value in request["media_paths"].items()},
        frame=request["frame"],
        output_path=Path(request["output_path"]),
    )


if __name__ == "__main__":
    main()
