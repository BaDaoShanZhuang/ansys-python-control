from __future__ import annotations

import json
import socket
import time
from pathlib import Path

from .config import MECHANICAL_EXE, PROJECT_FILE, require_file


MECHANICAL_DATABASE_SUFFIXES = {".mechdb", ".mechdat"}


_SAVE_CURRENT_PROJECT_SCRIPT = r'''
messages = []
try:
    project = ExtAPI.DataModel.Project
    project.Save()
    messages.append("ExtAPI.DataModel.Project.Save: ok")
except Exception as exc:
    messages.append("ExtAPI.DataModel.Project.Save: failed: " + str(exc))
    raise
try:
    messages.append("ProjectDirectory: " + str(project.ProjectDirectory))
except Exception:
    pass
"\n".join(messages)
'''


_READ_ANALYSES_SCRIPT = r'''
import json

def text(value):
    try:
        if value is None:
            return ""
        return str(value)
    except Exception:
        return ""

def safe_get(obj, name):
    try:
        return getattr(obj, name)
    except Exception:
        return ""

def safe_type(obj):
    try:
        return obj.GetType().FullName
    except Exception:
        return type(obj).__name__

def analysis_record(index, analysis):
    name = text(safe_get(analysis, "Name")).strip()
    analysis_type = text(safe_get(analysis, "AnalysisType")).strip()
    physics_type = text(safe_get(analysis, "PhysicsType")).strip()
    solver_type = text(safe_get(analysis, "SolverType")).strip()
    state = text(safe_get(analysis, "State")).strip()
    type_name = safe_type(analysis)
    display = name or analysis_type or ("Analysis " + str(index))
    return {
        "index": index,
        "system_name": "MECH-" + str(index),
        "display_text": display,
        "system_type": type_name,
        "physics_type": physics_type,
        "analysis_type": analysis_type,
        "solver_type": solver_type or state,
        "directory_name": display,
        "visible": True,
    }

model = ExtAPI.DataModel.Project.Model
analyses = list(model.Analyses)
records = [analysis_record(index + 1, analysis) for index, analysis in enumerate(analyses)]
json.dumps(records)
'''


def find_project_mechdb(project_file: str | Path | None = None) -> Path:
    """Find the Mechanical database inside an unpacked Workbench project."""
    if project_file:
        project = Path(project_file)
    elif PROJECT_FILE is not None:
        project = PROJECT_FILE
    else:
        raise ValueError("请先选择 Ansys Workbench 工程文件（.wbpj）或 Mechanical database（.mechdb/.mechdat）。")
    project = require_file(project, "Workbench project or Mechanical database")
    if project.suffix.lower() in MECHANICAL_DATABASE_SUFFIXES:
        return project
    if project.suffix.lower() != ".wbpj":
        raise ValueError(
            "请先选择已解包的 .wbpj 工程或 Mechanical database（.mechdb/.mechdat）；"
            f"当前文件不能用于 database 方式启动 Mechanical: {project}"
        )

    files_dir = project.with_name(f"{project.stem}_files")
    if not files_dir.exists():
        raise FileNotFoundError(f"没有找到 Workbench 工程文件目录: {files_dir}")

    mechdb_files: list[Path] = []
    for suffix in sorted(MECHANICAL_DATABASE_SUFFIXES):
        mechdb_files.extend(files_dir.rglob(f"*{suffix}"))
    if not mechdb_files:
        raise FileNotFoundError(f"没有在工程文件目录中找到 Mechanical database（.mechdb/.mechdat）: {files_dir}")

    return max(mechdb_files, key=lambda path: (path.stat().st_mtime, path.stat().st_size))


def read_mechanical_database_analysis_modules(
    project_file: str | Path,
    *,
    progress_callback=None,
) -> list:
    """Read actual analysis modules from a Mechanical database file."""
    from ansys.mechanical.core import launch_mechanical

    from .mechanical_ops import AnalysisModule

    mechanical_exe = require_file(MECHANICAL_EXE, "Mechanical executable")
    mechdb = find_project_mechdb(project_file)
    _emit_progress(progress_callback, "启动 Mechanical", f"后台读取 database 模块: {mechdb.name}")
    session = launch_mechanical(
        exec_file=str(mechanical_exe),
        batch=True,
        start_instance=True,
        cleanup_on_exit=True,
        clear_on_connect=False,
        additional_switches=["-file", str(mechdb)],
    )
    try:
        _emit_progress(progress_callback, "读取分析模块", "正在读取 Mechanical Model.Analyses")
        raw = session.run_python_script(_READ_ANALYSES_SCRIPT)
    finally:
        try:
            session.exit(force=False)
        except Exception:
            pass

    try:
        records = json.loads(str(raw or "[]"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Mechanical 返回的分析模块数据不是有效 JSON: {raw}") from exc

    modules = _analysis_records_to_modules(records)
    if not modules:
        raise RuntimeError("当前 Mechanical database 中没有读取到分析模块。")
    _emit_progress(progress_callback, "读取完成", f"已读取 {len(modules)} 个 Mechanical 分析模块")
    return modules


def read_current_mechanical_analysis_modules(
    *,
    port: int,
    progress_callback=None,
) -> list:
    """Read actual analysis modules from the currently open Mechanical session."""
    from ansys.mechanical.core import connect_to_mechanical

    _emit_progress(progress_callback, "连接 Mechanical", f"正在连接当前 Mechanical 端口 {port}")
    session = connect_to_mechanical(
        port=port,
        connect_timeout=8,
        clear_on_connect=False,
        cleanup_on_exit=False,
    )
    _emit_progress(progress_callback, "读取分析模块", "正在从当前 Mechanical 读取 Model.Analyses")
    raw = session.run_python_script(_READ_ANALYSES_SCRIPT)
    try:
        records = json.loads(str(raw or "[]"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Mechanical 返回的分析模块数据不是有效 JSON: {raw}") from exc

    modules = _analysis_records_to_modules(records)
    if not modules:
        raise RuntimeError("当前 Mechanical 会话中没有读取到分析模块。")
    _emit_progress(progress_callback, "读取完成", f"已读取 {len(modules)} 个 Mechanical 分析模块")
    return modules


def _analysis_records_to_modules(records: list[dict]) -> list:
    from .mechanical_ops import AnalysisModule

    return [
        AnalysisModule(
            index=int(record.get("index") or position),
            system_name=str(record.get("system_name") or f"MECH-{position}"),
            display_text=str(record.get("display_text") or f"Analysis {position}"),
            system_type=str(record.get("system_type") or "Mechanical"),
            physics_type=str(record.get("physics_type") or ""),
            analysis_type=str(record.get("analysis_type") or ""),
            solver_type=str(record.get("solver_type") or ""),
            directory_name=str(record.get("directory_name") or ""),
            visible=bool(record.get("visible", True)),
        )
        for position, record in enumerate(records, start=1)
    ]


def launch_mechanical_session(*, batch: bool = True, cleanup_on_exit: bool = False):
    """Launch Mechanical through PyMechanical and return the session object."""
    from ansys.mechanical.core import launch_mechanical

    mechanical_exe = require_file(MECHANICAL_EXE, "Mechanical executable")
    return launch_mechanical(
        exec_file=str(mechanical_exe),
        batch=batch,
        cleanup_on_exit=cleanup_on_exit,
    )


def launch_mechanical_project_session(
    project_file: str | Path | None = None,
    *,
    cleanup_on_exit: bool = False,
):
    """Launch a visible, PyMechanical-connectable Mechanical session for a project."""
    from ansys.mechanical.core import launch_mechanical

    mechanical_exe = require_file(MECHANICAL_EXE, "Mechanical executable")
    mechdb = find_project_mechdb(project_file)
    return launch_mechanical(
        exec_file=str(mechanical_exe),
        batch=False,
        start_instance=True,
        cleanup_on_exit=cleanup_on_exit,
        clear_on_connect=False,
        additional_switches=["-file", str(mechdb)],
    )


def mechanical_session_port(mechanical) -> int | None:
    """Return the PyMechanical port for a launched or connected Mechanical session."""
    for attribute in ("_port", "port"):
        try:
            value = getattr(mechanical, attribute)
        except Exception:
            continue
        try:
            return int(value)
        except Exception:
            continue
    return None


def save_and_close_mechanical_session(
    mechanical=None,
    *,
    port: int | None = None,
    save_project: bool = True,
    timeout_seconds: int = 45,
    progress_callback=None,
) -> dict:
    """Save or discard the current Mechanical project, then close Mechanical."""
    session = mechanical
    session_port = port or mechanical_session_port(session)

    if session is None:
        from ansys.mechanical.core import connect_to_mechanical

        from .mechanical_ops import _resolve_mechanical_port

        session_port = _resolve_mechanical_port(port)
        if session_port is None:
            raise RuntimeError(
                "没有可连接的 Mechanical 会话，无法安全保存并关闭。"
                "请先用本程序打开 Mechanical，或在 Mechanical 中手动保存后关闭。"
            )
        _emit_close_progress(
            progress_callback,
            "连接 Mechanical",
            f"正在连接当前 Mechanical 端口 {session_port}",
        )
        session = connect_to_mechanical(
            port=session_port,
            connect_timeout=8,
            clear_on_connect=False,
            cleanup_on_exit=False,
        )

    save_output = ""
    if save_project:
        _emit_close_progress(progress_callback, "保存 Mechanical", "正在保存当前 Mechanical 工程")
        save_output = session.run_python_script(_SAVE_CURRENT_PROJECT_SCRIPT)
    else:
        _emit_close_progress(progress_callback, "关闭 Mechanical", "不保存当前 Mechanical 工程，正在关闭")

    _emit_close_progress(progress_callback, "关闭 Mechanical", "正在正常关闭 Mechanical（非强制）")
    session.exit(force=not save_project)

    closed = _wait_for_mechanical_port_to_close(session_port, timeout_seconds)
    if closed and save_project:
        status = "已保存并正常关闭 Mechanical"
    elif closed:
        status = "未保存并已关闭 Mechanical"
    elif save_project:
        status = "已保存并发送正常关闭请求；Mechanical 可能仍在等待关闭确认"
    else:
        status = "已发送不保存关闭请求；Mechanical 可能仍在关闭中"
    _emit_close_progress(progress_callback, "关闭 Mechanical", status)
    return {
        "saved": save_project,
        "closed": closed,
        "port": session_port,
        "save_output": save_output,
        "status": status,
    }


def _emit_close_progress(progress_callback, stage: str, status: str) -> None:
    if progress_callback is None:
        return
    progress_callback({"stage": stage, "status": status})


def _emit_progress(progress_callback, stage: str, status: str) -> None:
    if progress_callback is None:
        return
    progress_callback({"stage": stage, "status": status})


def _wait_for_mechanical_port_to_close(port: int | None, timeout_seconds: int) -> bool:
    if port is None:
        return True
    deadline = time.monotonic() + max(1, timeout_seconds)
    while time.monotonic() < deadline:
        if not _is_port_open(port):
            return True
        time.sleep(0.5)
    return not _is_port_open(port)


def _is_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def run_mechanical_python(script_text: str, *, batch: bool = True):
    """Launch Mechanical, run Python inside Mechanical, and close the session."""
    mechanical = launch_mechanical_session(batch=batch, cleanup_on_exit=True)
    try:
        return mechanical.run_python_script(script_text)
    finally:
        mechanical.exit()
