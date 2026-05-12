from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ansys_control.config import PROJECT_FILE
from ansys_control.workbench import open_workbench_mechanical, read_project_analysis_modules


def main() -> int:
    parser = argparse.ArgumentParser(description="Open Mechanical from Workbench for a selected analysis module.")
    parser.add_argument(
        "project",
        nargs="?",
        type=Path,
        default=PROJECT_FILE,
        help="Optional path to a .wbpj or .wbpz project.",
    )
    parser.add_argument(
        "--system",
        default="",
        help='Workbench system name, for example "SYS", "SYS 1", or "SYS 4".',
    )
    args = parser.parse_args()

    system_name = args.system
    if not system_name:
        modules = read_project_analysis_modules(args.project)
        if not modules:
            print("No analysis module was found in the Workbench project.", file=sys.stderr)
            return 1
        system_name = modules[0].system_name

    launch_info = open_workbench_mechanical(project_file=args.project, system_name=system_name)
    process = launch_info["process"]
    print(f"Opened Workbench and requested Mechanical for system: {system_name}")
    print(f"Project: {launch_info['project']}")
    print(f"RunWB2 PID: {process.pid}")
    print(f"Journal: {launch_info['journal_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
