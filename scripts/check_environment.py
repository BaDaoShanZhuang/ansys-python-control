from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ansys_control import config as app_config
from ansys_control.config import PROJECT_FILE


def status(name: str, ok: bool, detail: str) -> None:
    label = "OK" if ok else "MISSING"
    print(f"{label}: {name}: {detail}")


def main() -> int:
    print(f"Python executable: {sys.executable}")
    print(f"Python version: {sys.version.split()[0]}")

    project_ok = bool(PROJECT_FILE is not None and PROJECT_FILE.exists())
    status("Workbench project", project_ok, str(PROJECT_FILE or "not configured"))
    status("RunWB2", app_config.RUNWB2.exists(), str(app_config.RUNWB2))
    status("Mechanical executable", app_config.MECHANICAL_EXE.exists(), str(app_config.MECHANICAL_EXE))
    status("OpticStudio executable", app_config.OPTICSTUDIO_EXE.exists(), str(app_config.OPTICSTUDIO_EXE))

    try:
        has_pymechanical = importlib.util.find_spec("ansys.mechanical.core") is not None
    except ModuleNotFoundError:
        has_pymechanical = False
    status("ansys-mechanical-core", has_pymechanical, "Python package")

    has_pyside6 = importlib.util.find_spec("PySide6") is not None
    status("PySide6", has_pyside6, "Python package")

    try:
        has_pythonnet = importlib.util.find_spec("clr") is not None
    except ModuleNotFoundError:
        has_pythonnet = False
    status("pythonnet", has_pythonnet, "Python package")

    has_numpy = importlib.util.find_spec("numpy") is not None
    status("numpy", has_numpy, "Python package")

    has_scipy = importlib.util.find_spec("scipy") is not None
    status("scipy", has_scipy, "Python package (MATLAB .mat 导出)")

    if not has_pymechanical or not has_pyside6 or not has_pythonnet or not has_numpy or not has_scipy:
        print("")
        print("Install command:")
        print(f'"{sys.executable}" -m pip install -e "{PROJECT_ROOT}"')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
