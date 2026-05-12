from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ansys_control.mechanical import launch_mechanical_session


def main() -> int:
    mechanical = launch_mechanical_session(batch=True, cleanup_on_exit=True)
    try:
        print(mechanical)
        print(mechanical.run_python_script("ExtAPI.DataModel.Project.ProductVersion"))
        print(mechanical.run_python_script("ExtAPI.DataModel.Project.ProjectDirectory"))
    finally:
        mechanical.exit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
