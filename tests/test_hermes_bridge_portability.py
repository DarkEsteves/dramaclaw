"""Canvas helpers must work in the Hermes environment without the API runtime."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("module_name", [
    "novelvideo.freezone.workflow_preflight",
    "novelvideo.freezone.remix.production_commands",
])
def test_portable_helpers_do_not_import_the_api_or_recipe_runtime(module_name):
    script = """
import importlib
import importlib.abc
import sys
sys.path.insert(0, sys.argv[1])
class BackendImportGuard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'novelvideo.api', 'novelvideo.freezone.recipe_runtime', 'ulid', 'pydantic_ai'}:
            raise RuntimeError('backend-only import: ' + fullname)
sys.meta_path.insert(0, BackendImportGuard())
module = importlib.import_module(sys.argv[2])
if sys.argv[2].endswith('production_commands'):
    result = module.validate_production_command({
        'command_id': 'command-1', 'plan_ref': {'artifact_id': 'plan-1', 'revision': 1},
        'kind': 'edit_turn', 'target_id': 'turn-1', 'payload': {'text': '校对台词'},
    })
    assert result['payload']['text'] == '校对台词'
else:
    assert module.FREEZONE_DEFAULT_IMAGE_MODEL == 'newapi_gpt_image2'
    module.evaluate_workflow_preflight({'plan': {'nodes': []}}, model_responses={}, limits={}, runtime_available=False)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(ROOT / "src"), module_name],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def _installed_fork_with_missing_bridge(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    setup = scripts / "setup-hermes.sh"
    setup.write_text((ROOT / "scripts" / "setup-hermes.sh").read_text())
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {
        "worker-python": '''
case "$1" in
  */verify_hermes_bridge.py) [ -f "$BRIDGE_READY" ]; exit $? ;;
  -) cat >/dev/null; exit 0 ;;
esac
exit 17
''',
        "hermes": "echo 'Hermes test fork'",
        "uv": 'printf "%s\\n" "$*" > "$INSTALL_ARGS"; touch "$BRIDGE_READY"',
    }.items():
        binary = bin_dir / name
        binary.write_text(f"#!/bin/sh\n{body}\n")
        binary.chmod(0o755)
    return setup, {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "HERMES_PYTHON": str(bin_dir / "worker-python"),
        "HERMES_CLI_PATH": str(bin_dir / "hermes"),
        "BRIDGE_READY": str(tmp_path / "ready"),
        "INSTALL_ARGS": str(tmp_path / "install-args"),
    }


def test_check_refuses_a_valid_fork_with_missing_canvas_dependencies(tmp_path):
    setup, env = _installed_fork_with_missing_bridge(tmp_path)
    result = subprocess.run(
        ["/bin/bash", str(setup), "--check"], env=env,
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode != 0
    assert "bridge" in result.stderr.lower()
    assert not Path(env["INSTALL_ARGS"]).exists()


def test_setup_repairs_canvas_dependencies_before_an_installed_fork_early_return(tmp_path):
    setup, env = _installed_fork_with_missing_bridge(tmp_path)
    result = subprocess.run(
        ["/bin/bash", str(setup)], env=env,
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert Path(env["BRIDGE_READY"]).exists()
    args = Path(env["INSTALL_ARGS"]).read_text()
    assert f"--python {env['HERMES_PYTHON']}" in args
    assert "hermes-bridge-requirements.txt" in args
    assert "pydantic-ai" not in args
    assert "ulid" not in args
