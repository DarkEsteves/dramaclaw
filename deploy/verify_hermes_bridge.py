"""Check portable canvas tools in the interpreter that runs the Hermes worker."""

from __future__ import annotations

import argparse
import importlib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]


def check_dependencies() -> list[str]:
    from packaging.requirements import Requirement

    failures: list[str] = []
    for line in (ROOT / "deploy" / "hermes-bridge-requirements.txt").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        requirement = Requirement(line)
        try:
            installed = version(requirement.name)
        except PackageNotFoundError:
            failures.append(f"missing {requirement.name}")
        else:
            if installed not in requirement.specifier:
                failures.append(f"{requirement.name} must satisfy {requirement.specifier}")
    return failures


def check_portable_tools() -> None:
    sys.path.insert(0, str(ROOT / "src"))
    schema = importlib.import_module("novelvideo.freezone.workflow_schema")
    graph = importlib.import_module("novelvideo.freezone.agent_workflows.graph")
    preflight = importlib.import_module("novelvideo.freezone.workflow_preflight")
    commands = importlib.import_module("novelvideo.freezone.remix.production_commands")
    schema.workflow_plan_json_schema()
    assert callable(graph.build_workflow_graph_commands)
    preflight.evaluate_workflow_preflight(
        {"plan": {"nodes": []}}, model_responses={}, limits={}, runtime_available=False,
    )
    commands.validate_production_command({
        "command_id": "bridge-probe", "plan_ref": {"artifact_id": "plan-probe", "revision": 1},
        "kind": "edit_turn", "target_id": "turn-probe", "payload": {"text": "probe"},
    })
    if "novelvideo.api" in sys.modules or "novelvideo.freezone.recipe_runtime" in sys.modules:
        raise RuntimeError("canvas helpers imported the application runtime")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dependencies-only", action="store_true")
    arguments = parser.parse_args()
    try:
        failures = check_dependencies()
        if failures:
            print("Hermes canvas bridge dependencies: " + "; ".join(failures), file=sys.stderr)
            return 1
        if not arguments.dependencies_only:
            check_portable_tools()
    except Exception as exc:
        detail = exc.name if isinstance(exc, ModuleNotFoundError) else type(exc).__name__
        print(f"Hermes canvas bridge check failed: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
