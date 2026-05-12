from __future__ import annotations

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
    timeout_seconds: int = 45,
    progress_callback=None,
) -> dict:
    """Save the current Mechanical project, then request a normal non-forced exit."""
    session = mechanical
    session_port = port or mechanical_session_port(session)

    if session is None:
        from ansys.mechanical.core import connect_to_mechanical

        from .workbench import _resolve_mechanical_port

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

    _emit_close_progress(progress_callback, "保存 Mechanical", "正在保存当前 Mechanical 工程")
    save_output = session.run_python_script(_SAVE_CURRENT_PROJECT_SCRIPT)

    _emit_close_progress(progress_callback, "关闭 Mechanical", "正在正常关闭 Mechanical（非强制）")
    session.exit(force=False)

    closed = _wait_for_mechanical_port_to_close(session_port, timeout_seconds)
    if closed:
        status = "已保存并正常关闭 Mechanical"
    else:
        status = "已保存并发送正常关闭请求；Mechanical 可能仍在等待关闭确认"
    _emit_close_progress(progress_callback, "关闭 Mechanical", status)
    return {
        "saved": True,
        "closed": closed,
        "port": session_port,
        "save_output": save_output,
        "status": status,
    }


def _emit_close_progress(progress_callback, stage: str, status: str) -> None:
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
