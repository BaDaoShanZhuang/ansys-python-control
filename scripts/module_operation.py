from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ansys_control.config import PROJECT_FILE
from ansys_control.mechanical_ops import (
    read_analysis_module_settings,
    solve_analysis_module,
    update_analysis_module_settings,
)


def parse_setting(values: list[str]) -> dict:
    settings: dict[str, object] = {}
    for value in values:
        if "=" not in value:
            raise argparse.ArgumentTypeError(f"Expected NAME=VALUE, got: {value}")
        name, raw = value.split("=", 1)
        name = name.strip()
        raw = raw.strip()
        if not name:
            raise argparse.ArgumentTypeError(f"Empty setting name in: {value}")
        if name == "NumberOfSteps":
            settings[name] = int(raw)
        else:
            settings[name] = raw
    return settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read, update, or solve one analysis module through Workbench so "
            "Mechanical keeps the Workbench project context."
        )
    )
    parser.add_argument(
        "operation",
        choices=["read", "update", "solve"],
        help="Operation to run on the selected Workbench system.",
    )
    parser.add_argument(
        "--project",
        type=Path,
        default=PROJECT_FILE,
        help="Path to a Workbench .wbpj or .wbpz project.",
    )
    parser.add_argument(
        "--system",
        required=True,
        help='Workbench system name, for example "SYS", "SYS 1", or "SYS 2".',
    )
    parser.add_argument(
        "--analysis-hint",
        default="",
        help='Optional Mechanical analysis name/type hint, for example "Modal".',
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Analysis setting to change. Can be repeated.",
    )
    args = parser.parse_args()

    settings_update = parse_setting(args.set)
    try:
        if args.operation == "read":
            result = read_analysis_module_settings(
                project_file=args.project,
                system_name=args.system,
                analysis_hint=args.analysis_hint,
            )
        elif args.operation == "update":
            result = update_analysis_module_settings(
                project_file=args.project,
                system_name=args.system,
                analysis_hint=args.analysis_hint,
                settings_update=settings_update,
            )
        else:
            result = solve_analysis_module(
                project_file=args.project,
                system_name=args.system,
                analysis_hint=args.analysis_hint,
                settings_update=settings_update,
            )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(result.report, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
