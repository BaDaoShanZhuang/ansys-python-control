from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ansys_control.config import MECHANICAL_EXE, OPTICSTUDIO_EXE, PROJECT_FILE, RUNWB2


def status(name: str, ok: bool, detail: str) -> None:
    label = "OK" if ok else "MISSING"
    print(f"{label}: {name}: {detail}")


def main() -> int:
    print(f"Python executable: {sys.executable}")
    print(f"Python version: {sys.version.split()[0]}")

    status("Workbench project", PROJECT_FILE.exists(), str(PROJECT_FILE))
    status("RunWB2", RUNWB2.exists(), str(RUNWB2))
    status("Mechanical executable", MECHANICAL_EXE.exists(), str(MECHANICAL_EXE))
    status("OpticStudio executable", OPTICSTUDIO_EXE.exists(), str(OPTICSTUDIO_EXE))

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

    if not has_pymechanical or not has_pyside6 or not has_pythonnet:
        print("")
        print("Install command:")
        print(f'"{sys.executable}" -m pip install -e "{PROJECT_ROOT}"')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
