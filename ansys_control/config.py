from __future__ import annotations

import os
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = Path(os.environ.get("ANSYS_CONTROL_WORKSPACE", str(APP_ROOT)))

_PROJECT_FILE = os.environ.get("ANSYS_CONTROL_PROJECT", "").strip()
PROJECT_FILE = Path(_PROJECT_FILE) if _PROJECT_FILE else None

ANSYS_VERSION = os.environ.get("ANSYS_CONTROL_VERSION", "261")
ANSYS_ROOT = Path(
    os.environ.get("ANSYS_ROOT", rf"D:\Program Files\ANSYS Inc\v{ANSYS_VERSION}")
)

RUNWB2 = Path(
    os.environ.get(
        "ANSYS_RUNWB2",
        str(ANSYS_ROOT / "Framework" / "bin" / "Win64" / "RunWB2.exe"),
    )
)
MECHANICAL_EXE = Path(
    os.environ.get(
        "ANSYS_MECHANICAL_EXE",
        str(ANSYS_ROOT / "aisol" / "bin" / "winx64" / "AnsysWBU.exe"),
    )
)
OPTICSTUDIO_EXE = Path(
    os.environ.get(
        "ANSYS_OPTICSTUDIO_EXE",
        str(ANSYS_ROOT / "Zemax OpticStudio" / "OpticStudio.exe"),
    )
)

DEFAULT_OPEN_JOURNAL = Path(
    os.environ.get(
        "ANSYS_CONTROL_OPEN_JOURNAL",
        str(WORKSPACE / "open_project.wbjn"),
    )
)

REPORT_DIR = Path(os.environ.get("ANSYS_CONTROL_REPORT_DIR", str(WORKSPACE / "python_ansys_reports")))


def require_file(path: Path, label: str) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"{label} does not exist: {path}")
    return path
