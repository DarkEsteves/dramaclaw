"""Real Seatbelt regression: SQLite works inside the existing worker boundary."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from novelvideo.security.sandbox_wrap import SandboxSpec, build_macos_profile


def test_ancestor_metadata_grants_are_exact_and_cover_resolved_symlink_paths(tmp_path, monkeypatch):
    state = tmp_path / "real-state"
    state.mkdir()
    alias = tmp_path / "state-link"
    alias.symlink_to(state, target_is_directory=True)
    monkeypatch.setenv("NOVELVIDEO_STATE_DIR", str(alias))
    profile = build_macos_profile(SandboxSpec(user="alice"))
    block = profile.split(";; READ metadata: exact ancestors of authorized paths only\n")[1]
    block = block.split(";; READ deny: host secrets")[0]

    assert "(allow file-read-metadata" in block
    assert f"(literal {json.dumps(str(alias))})" in block
    assert f"(literal {json.dumps(str(state.resolve()))})" in block
    assert "subpath" not in block
    assert "file-read-data" not in block
    assert "file-read*" not in block
    assert "file-write" not in block
    assert "network" not in block
    assert str(state / "bob") not in block


@pytest.mark.skipif(
    platform.system() != "Darwin" or not shutil.which("sandbox-exec"),
    reason="requires the real macOS Seatbelt sandbox",
)
@pytest.mark.parametrize("secret_roots", ["none", "state", "all"])
def test_host_secret_ancestor_metadata_stays_denied(tmp_path, monkeypatch, secret_roots):
    host = (tmp_path / "host").resolve()
    secret = host / ".ssh"
    home = secret / ".hermes"
    home.mkdir(parents=True)
    canary = secret / "private.txt"
    canary.write_text("HOST-SECRET-CANARY")
    (secret / "bob").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: host))
    if secret_roots == "state":
        monkeypatch.setenv("NOVELVIDEO_STATE_DIR", str(secret))
    elif secret_roots == "all":
        for kind in ("STATE", "OUTPUT", "RUNTIME"):
            monkeypatch.setenv(f"NOVELVIDEO_{kind}_DIR", str(secret))
    profile = build_macos_profile(SandboxSpec(user="alice", hermes_home=home))
    if secret_roots == "all":
        metadata = profile.split(";; READ metadata: exact ancestors of authorized paths only\n")[1]
        metadata = metadata.split(";; READ deny: host secrets")[0]
        assert "(allow" not in metadata
    script = """
import json, pathlib, sys
secret = pathlib.Path(sys.argv[1]); results = {}
for name, operation in {"stat": secret.stat, "peer_stat": (secret / "bob").stat,
                        "read": lambda: (secret / "private.txt").read_text()}.items():
    try:
        operation(); results[name] = "allowed"
    except PermissionError:
        results[name] = "denied"
print(json.dumps(results))
"""
    completed = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "--", sys.executable,
         "-c", script, str(secret)],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"stat": "denied", "peer_stat": "denied", "read": "denied"}


@pytest.mark.skipif(
    platform.system() != "Darwin" or not shutil.which("sandbox-exec"),
    reason="requires the real macOS Seatbelt sandbox",
)
def test_peer_metadata_stays_denied_when_home_symlink_targets_peer(tmp_path, monkeypatch):
    state = (tmp_path / "state").resolve()
    alice = state / "alice"
    peer = state / "bob"
    target = peer / "hidden" / "hermes"
    alice.mkdir(parents=True)
    target.mkdir(parents=True)
    (peer / "private.txt").write_text("PEER-CANARY")
    home = alice / ".hermes"
    home.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("NOVELVIDEO_STATE_DIR", str(state))
    profile = build_macos_profile(SandboxSpec(user="alice", hermes_home=home))
    script = """
import json, pathlib, sys
peer = pathlib.Path(sys.argv[1]); results = {}
for name, operation in {"stat": peer.stat, "read": lambda: (peer / "private.txt").read_text()}.items():
    try:
        operation(); results[name] = "allowed"
    except PermissionError:
        results[name] = "denied"
print(json.dumps(results))
"""
    completed = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "--", sys.executable,
         "-c", script, str(peer)],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"stat": "denied", "read": "denied"}


@pytest.mark.skipif(
    platform.system() != "Darwin" or not shutil.which("sandbox-exec"),
    reason="requires the real macOS Seatbelt sandbox",
)
def test_own_sqlite_keeps_peer_data_listing_and_writes_denied(tmp_path, monkeypatch):
    roots = {kind: (tmp_path / kind).resolve() for kind in ("state", "output", "runtime")}
    for kind, root in roots.items():
        root.mkdir()
        monkeypatch.setenv(f"NOVELVIDEO_{kind.upper()}_DIR", str(root))
    home = roots["state"] / "alice" / ".hermes"
    home.mkdir(parents=True)
    peer = roots["state"] / "bob"
    peer.mkdir()
    canary = peer / "private.txt"
    canary.write_text("PEER-CONTENT-MUST-STAY-PRIVATE")
    (home / "peer-link").symlink_to(canary)
    profile = build_macos_profile(SandboxSpec(user="alice", hermes_home=home))
    script = """
import json, pathlib, sqlite3, sys
home, peer, root = map(pathlib.Path, sys.argv[1:])
results = {}
try:
    conn = sqlite3.connect(home / "bridge.sqlite3")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE proof (value INTEGER)")
    conn.execute("INSERT INTO proof VALUES (42)")
    conn.commit()
    results["sqlite"] = conn.execute("SELECT value FROM proof").fetchone()[0]
    conn.close()
except Exception as exc:
    results["sqlite"] = type(exc).__name__
for name, operation in {
    "peer_read": lambda: (peer / "private.txt").read_text(),
    "peer_symlink_read": lambda: (home / "peer-link").read_text(),
    "peer_list": lambda: list(peer.iterdir()),
    "root_list": lambda: list(root.iterdir()),
    "peer_write": lambda: (peer / "created.txt").write_text("bad"),
}.items():
    try:
        operation()
        results[name] = "allowed"
    except PermissionError:
        results[name] = "denied"
print(json.dumps(results))
"""
    completed = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "--", sys.executable, "-c", script,
         str(home), str(peer), str(roots["state"])],
        cwd=home, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                       "HOME": str(home), "TMPDIR": str(home)},
        capture_output=True, text=True, timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    results = json.loads(completed.stdout)
    assert results == {
        "sqlite": 42, "peer_read": "denied", "peer_symlink_read": "denied",
        "peer_list": "denied", "root_list": "denied", "peer_write": "denied",
    }
    assert not (peer / "created.txt").exists()
