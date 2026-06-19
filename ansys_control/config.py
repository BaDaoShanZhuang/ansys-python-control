from __future__ import annotations

import json
import os
import sys
from pathlib import Path


# 用户设置目录键。沿用历史名称 "WindowsDuan" 仅为向后兼容(%APPDATA%\WindowsDuan),
# 避免老用户升级后丢失已保存的路径配置;产品显示名见 gui.APP_NAME。
APP_CONFIG_DIR_NAME = "WindowsDuan"
SETTINGS_VERSION = 1


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def user_config_dir() -> Path:
    base = os.environ.get("ANSYS_CONTROL_CONFIG_DIR", "").strip()
    if base:
        return Path(base)
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        return Path(appdata) / APP_CONFIG_DIR_NAME
    return Path.home() / f".{APP_CONFIG_DIR_NAME}"


APP_ROOT = app_root()
CONFIG_DIR = user_config_dir()
SETTINGS_FILE = CONFIG_DIR / "settings.json"
WORKSPACE = Path(os.environ.get("ANSYS_CONTROL_WORKSPACE", str(APP_ROOT)))

_PROJECT_FILE = os.environ.get("ANSYS_CONTROL_PROJECT", "").strip()
PROJECT_FILE = Path(_PROJECT_FILE) if _PROJECT_FILE else None

DEFAULT_ANSYS_VERSION = os.environ.get("ANSYS_CONTROL_VERSION", "261")
DEFAULT_ANSYS_ROOT = Path(rf"D:\Program Files\ANSYS Inc\v{DEFAULT_ANSYS_VERSION}")

ENV_PATH_KEYS = {
    "ansys_root": "ANSYS_ROOT",
    "runwb2": "ANSYS_RUNWB2",
    "mechanical_exe": "ANSYS_MECHANICAL_EXE",
    "opticstudio_exe": "ANSYS_OPTICSTUDIO_EXE",
}


def _load_user_settings() -> dict:
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def _write_user_settings(settings: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = dict(settings)
    payload["version"] = SETTINGS_VERSION
    SETTINGS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _path_text(value: object) -> str:
    return str(value or "").strip().strip('"')


def _configured_path(settings: dict, key: str) -> str:
    env_name = ENV_PATH_KEYS.get(key, "")
    env_value = _path_text(os.environ.get(env_name, ""))
    if env_value:
        return env_value
    paths = settings.get("paths")
    if isinstance(paths, dict):
        return _path_text(paths.get(key, ""))
    return ""


def _common_program_roots() -> list[Path]:
    candidates: list[Path] = []
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        value = _path_text(os.environ.get(env_name, ""))
        if value:
            candidates.append(Path(value))
    for drive in ("C:", "D:", "E:", "F:"):
        candidates.append(Path(drive) / "Program Files")
        candidates.append(Path(drive) / "Program Files (x86)")
    return _unique_paths(candidates)


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        text = str(path).lower()
        if text in seen:
            continue
        seen.add(text)
        unique.append(path)
    return unique


def _version_number(path: Path) -> int:
    text = path.name.lower().lstrip("v")
    try:
        return int(text)
    except ValueError:
        return -1


def _candidate_ansys_roots() -> list[Path]:
    candidates: list[Path] = []
    env_root = _path_text(os.environ.get("ANSYS_ROOT", ""))
    if env_root:
        candidates.append(Path(env_root))
    candidates.append(DEFAULT_ANSYS_ROOT)

    for base in _common_program_roots():
        ansys_inc = base / "ANSYS Inc"
        if not ansys_inc.exists():
            continue
        try:
            roots = [path for path in ansys_inc.glob("v*") if path.is_dir()]
        except OSError:
            roots = []
        candidates.extend(sorted(roots, key=_version_number, reverse=True))
    return _unique_paths(candidates)


def _find_first_existing(paths: list[Path]) -> Path | None:
    for path in paths:
        try:
            if path.exists():
                return path
        except OSError:
            continue
    return None


def _candidate_opticstudio_paths(ansys_roots: list[Path]) -> list[Path]:
    candidates: list[Path] = []
    for root in ansys_roots:
        candidates.append(root / "Zemax OpticStudio" / "OpticStudio.exe")

    for base in _common_program_roots():
        candidates.extend(
            [
                base / "Zemax OpticStudio" / "OpticStudio.exe",
                base / "Ansys Zemax OpticStudio" / "OpticStudio.exe",
                base / "OpticStudio" / "OpticStudio.exe",
            ]
        )
        for folder_name in ("Zemax OpticStudio*", "Ansys Zemax OpticStudio*", "OpticStudio*"):
            try:
                candidates.extend(path / "OpticStudio.exe" for path in base.glob(folder_name) if path.is_dir())
            except OSError:
                pass
    return _unique_paths(candidates)


def detect_installation_paths() -> dict[str, str]:
    """Detect local Ansys Mechanical, Workbench, and OpticStudio paths."""
    ansys_roots = _candidate_ansys_roots()
    detected: dict[str, str] = {}

    root = next(
        (
            candidate
            for candidate in ansys_roots
            if (candidate / "aisol" / "bin" / "winx64" / "AnsysWBU.exe").exists()
            or (candidate / "Framework" / "bin" / "Win64" / "RunWB2.exe").exists()
        ),
        None,
    )
    if root is not None:
        detected["ansys_root"] = str(root)
        runwb2 = root / "Framework" / "bin" / "Win64" / "RunWB2.exe"
        mechanical = root / "aisol" / "bin" / "winx64" / "AnsysWBU.exe"
        if runwb2.exists():
            detected["runwb2"] = str(runwb2)
        if mechanical.exists():
            detected["mechanical_exe"] = str(mechanical)

    opticstudio = _find_first_existing(_candidate_opticstudio_paths(ansys_roots))
    if opticstudio is not None:
        detected["opticstudio_exe"] = str(opticstudio)

    return detected


def _resolved_paths(settings: dict) -> dict[str, Path]:
    ansys_root_text = _configured_path(settings, "ansys_root") or str(DEFAULT_ANSYS_ROOT)
    ansys_root = Path(ansys_root_text)
    return {
        "ansys_root": ansys_root,
        "runwb2": Path(
            _configured_path(settings, "runwb2")
            or str(ansys_root / "Framework" / "bin" / "Win64" / "RunWB2.exe")
        ),
        "mechanical_exe": Path(
            _configured_path(settings, "mechanical_exe")
            or str(ansys_root / "aisol" / "bin" / "winx64" / "AnsysWBU.exe")
        ),
        "opticstudio_exe": Path(
            _configured_path(settings, "opticstudio_exe")
            or str(ansys_root / "Zemax OpticStudio" / "OpticStudio.exe")
        ),
    }


def _paths_need_detection(paths: dict[str, Path], settings: dict) -> bool:
    configured_paths = settings.get("paths")
    if not isinstance(configured_paths, dict):
        return True
    for key in ("runwb2", "mechanical_exe", "opticstudio_exe"):
        if not paths[key].exists():
            return True
    return False


def _merge_detected_paths(settings: dict, detected: dict[str, str]) -> dict:
    merged = dict(settings)
    paths = dict(merged.get("paths") if isinstance(merged.get("paths"), dict) else {})
    for key, value in detected.items():
        if os.environ.get(ENV_PATH_KEYS.get(key, ""), "").strip():
            continue
        current = Path(_path_text(paths.get(key, ""))) if _path_text(paths.get(key, "")) else None
        if current is None or not current.exists():
            paths[key] = value
    merged["paths"] = paths
    return merged


def ensure_detected_path_settings() -> dict[str, str]:
    settings = _load_user_settings()
    current = _resolved_paths(settings)
    if not _paths_need_detection(current, settings):
        return {}

    detected = detect_installation_paths()
    if detected:
        merged = _merge_detected_paths(settings, detected)
        _write_user_settings(merged)
    return detected


def apply_path_settings() -> None:
    global ANSYS_ROOT, RUNWB2, MECHANICAL_EXE, OPTICSTUDIO_EXE
    paths = _resolved_paths(_load_user_settings())
    ANSYS_ROOT = paths["ansys_root"]
    RUNWB2 = paths["runwb2"]
    MECHANICAL_EXE = paths["mechanical_exe"]
    OPTICSTUDIO_EXE = paths["opticstudio_exe"]


def get_path_settings() -> dict[str, str]:
    paths = _resolved_paths(_load_user_settings())
    return {key: str(value) for key, value in paths.items()}


def save_path_settings(paths: dict[str, str]) -> None:
    settings = _load_user_settings()
    existing = dict(settings.get("paths") if isinstance(settings.get("paths"), dict) else {})
    for key in ("ansys_root", "runwb2", "mechanical_exe", "opticstudio_exe"):
        value = _path_text(paths.get(key, ""))
        if value:
            existing[key] = value
    settings["paths"] = existing
    _write_user_settings(settings)
    apply_path_settings()


STARTUP_DETECTED_PATHS = ensure_detected_path_settings()
apply_path_settings()

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
