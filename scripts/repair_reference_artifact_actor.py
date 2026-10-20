#!/usr/bin/env python3
"""Inspect or repair one legacy blank native artifact owner.

Default is dry-run. The saved operation, task result and canvas must agree.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from novelvideo.freezone.remix.artifact_actor_repair import repair_blank_artifact_actor


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("output-dir", "state-dir", "project-id", "canvas-id",
                 "artifact-id", "operation-id"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--revision", type=int, required=True)
    parser.add_argument("--apply", action="store_true", help="write the verified metadata repair")
    args = parser.parse_args()
    result = repair_blank_artifact_actor(
        output_dir=Path(args.output_dir), state_dir=Path(args.state_dir),
        project_id=args.project_id, canvas_id=args.canvas_id,
        artifact_id=args.artifact_id, expected_revision=args.revision,
        operation_id=args.operation_id, apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
