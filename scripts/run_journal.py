from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ansys_control.workbench import run_workbench_journal


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a Workbench journal on an Ansys project.")
    parser.add_argument("journal", type=Path, help="Path to a .wbjn journal file.")
    parser.add_argument(
        "--project",
        type=Path,
        help="Optional path to a .wbpj or .wbpz project.",
    )
    parser.add_argument("--ui", action="store_true", help="Run Workbench in UI mode.")
    args = parser.parse_args()

    result = run_workbench_journal(
        args.journal,
        project_file=args.project,
        batch=not args.ui,
        check=False,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
