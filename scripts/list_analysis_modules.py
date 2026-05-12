from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ansys_control.config import PROJECT_FILE
from ansys_control.workbench import read_project_analysis_modules


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List existing analysis modules in a Workbench .wbpj/.wbpz project."
    )
    parser.add_argument(
        "project",
        nargs="?",
        type=Path,
        default=PROJECT_FILE,
        help="Path to a Workbench .wbpj or .wbpz project.",
    )
    args = parser.parse_args()

    modules = read_project_analysis_modules(args.project)
    if not modules:
        print(f"No analysis modules found: {args.project}")
        return 0

    print(f"Project: {args.project}")
    print("Index\tSystem\tModule\tSystem type\tPhysics\tAnalysis\tSolver")
    for module in modules:
        print(
            "\t".join(
                [
                    str(module.index),
                    module.system_name,
                    module.display_text,
                    module.system_type,
                    module.physics_type,
                    module.analysis_type,
                    module.solver_type,
                ]
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
