"""Regenerate the isolated Hermes production edit command contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from novelvideo.freezone.remix.production_command_schema import production_command_json_schema


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / ".hermes/plugins/freezone/production_command_schema.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if the generated contract is stale")
    args = parser.parse_args()
    expected = json.dumps(production_command_json_schema(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.check:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != expected:
            print(f"stale production command schema: {TARGET}")
            return 1
        print("production command schema is current")
        return 0
    TARGET.write_text(expected, encoding="utf-8")
    print(TARGET)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
