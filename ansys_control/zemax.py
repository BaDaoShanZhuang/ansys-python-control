from __future__ import annotations

import subprocess

from .config import ANSYS_ROOT, OPTICSTUDIO_EXE, require_file


def open_opticstudio() -> subprocess.Popen:
    """Open Ansys Zemax OpticStudio."""
    opticstudio = require_file(OPTICSTUDIO_EXE, "OpticStudio executable")
    return subprocess.Popen([str(opticstudio)], cwd=str(ANSYS_ROOT))
