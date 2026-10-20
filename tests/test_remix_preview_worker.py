"""Preview worker process budgets are applied before FFmpeg starts."""

import io
import json
import resource
import sys

import pytest

from novelvideo.freezone.remix import preview_worker, render


@pytest.mark.parametrize("previous_soft", [resource.RLIM_INFINITY, 2 * 1024**3])
def test_linux_preview_worker_caps_address_space(monkeypatch, previous_soft, tmp_path):
    hard = resource.RLIM_INFINITY
    limits = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(resource, "getrlimit", lambda _: (previous_soft, hard))
    monkeypatch.setattr(resource, "setrlimit", lambda kind, values: limits.append((kind, values)))
    monkeypatch.setattr(render, "render_preview_frame", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "manifest": {}, "media_paths": {}, "frame": 0,
        "output_path": str(tmp_path / "preview.png"),
    })))

    preview_worker.main()

    assert limits == [(
        resource.RLIMIT_AS,
        (4 * 1024**3 if previous_soft == resource.RLIM_INFINITY else previous_soft, hard),
    )]
