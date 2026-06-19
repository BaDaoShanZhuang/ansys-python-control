from __future__ import annotations

import csv
import io
import json
import math
import os
import random
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import config as app_config
from .config import require_file

try:  # numpy 用于 SVD/Kabsch 严格刚体配准;不可用时回退纯 Python 小角度解
    import numpy as _np
except Exception:  # pragma: no cover - 仅在缺少 numpy 的环境触发
    _np = None


_ZOSAPI_SESSIONS: list[dict[str, object]] = []
DETECTOR_PREVIEW_MAX_DIMENSION = 384


def open_opticstudio(project_file: Path | None = None) -> dict[str, object]:
    """Open the selected Zemax project in a managed background ZOS-API session, without GUI."""
    if project_file is None:
        raise ValueError("后台打开 Zemax 需要先选择工程文件。")
    project_file = require_file(project_file, "Zemax project file")
    application, system = _open_or_reuse_zosapi_project(project_file)
    return _opticstudio_session_info(application, system, project_file, "standalone_background")


@dataclass
class LensPoseRecord:
    """单个光学元件的刚体运动:质心平移(偏心 decenter / 离焦 despace)+ 绕质心旋转(倾斜 tilt)。

    x/y/z 为质心平移,单位 m;其中横向分量即光学偏心(decenter),沿光轴分量即离焦/despace。
    rx/ry/rz 为绕质心的倾斜(tilt),以旋转向量分量表示,单位 deg。
    rms_residual 为去除最佳刚体平移+旋转后各节点的剩余 RMS(单位 m),
    表征该元件的非刚体面形误差;为 None 表示未计算(缺少节点坐标或 numpy 不可用)。
    """

    name: str
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    rx: float = 0.0
    ry: float = 0.0
    rz: float = 0.0
    source: str = ""
    displacement_unit: str = "m"
    rotation_unit: str = "deg"
    rms_residual: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "rx": self.rx,
            "ry": self.ry,
            "rz": self.rz,
            "source": self.source,
            "displacement_unit": self.displacement_unit,
            "rotation_unit": self.rotation_unit,
            "rms_residual": self.rms_residual,
        }


@dataclass
class LensPoseFrame:
    frame_index: int
    time_value: float | None
    time_label: str
    set_index: int | None
    records: list[LensPoseRecord]
    source_files: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "frame_index": self.frame_index,
            "time_value": self.time_value,
            "time_label": self.time_label,
            "set_index": self.set_index,
            "records": [record.as_dict() for record in self.records],
            "source_files": list(self.source_files),
        }


def default_zemax_pose_output_path(zemax_file: Path) -> Path:
    zemax_file = Path(zemax_file)
    return zemax_file.with_name(f"{zemax_file.stem}_mechanical_pose{zemax_file.suffix}")


def read_lens_pose_records(folder: Path) -> list[LensPoseRecord]:
    folder = require_file(Path(folder), "Mechanical export folder")
    if not folder.is_dir():
        raise NotADirectoryError(f"Mechanical export path is not a folder: {folder}")

    if (folder / "solution_results_summary.txt").exists():
        computed = calculate_pose_records_from_mechanical_exports(folder, save=True)
        computed_records = [
            record
            for record in computed.get("latest_records", [])
            if isinstance(record, LensPoseRecord)
        ]
        if computed_records:
            return computed_records

    records: list[LensPoseRecord] = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".csv", ".txt", ".json"}:
            continue
        if path.name.lower() in {"solution_results_summary.txt", "lens_pose_calculation.log"}:
            continue
        try:
            if path.suffix.lower() == ".json":
                records.extend(_records_from_json(path))
            else:
                records.extend(_records_from_text_table(path))
        except Exception:
            continue

    merged: dict[str, LensPoseRecord] = {}
    for record in records:
        if not record.name.strip():
            continue
        merged[_normalize_name(record.name)] = record
    if not merged:
        computed = calculate_pose_records_from_mechanical_exports(folder, save=True)
        for item in computed.get("latest_records", []):
            if isinstance(item, LensPoseRecord):
                merged[_normalize_name(item.name)] = item
    return list(merged.values())


def calculate_pose_records_from_mechanical_exports(
    folder: Path,
    save_dir: Path | None = None,
    *,
    save: bool = True,
) -> dict[str, object]:
    folder = require_file(Path(folder), "Mechanical export folder")
    if not folder.is_dir():
        raise NotADirectoryError(f"Mechanical export path is not a folder: {folder}")

    logs: list[str] = []
    summary = _read_solution_summary(folder / "solution_results_summary.txt")
    component_tables: dict[tuple[str, str], dict[str, object]] = {}
    skipped_files: list[str] = []

    for path in sorted(folder.glob("*.txt")):
        if path.name.lower() == "solution_results_summary.txt" or path.name.endswith("_properties.txt"):
            continue
        table = _read_mechanical_export_table(path)
        if table is None:
            skipped_files.append(path.name)
            continue
        result_name = _result_name_from_export_file(path)
        section = summary.get(result_name, {})
        component = _component_from_table(table, section, path)
        lens_name = _lens_name_from_result(result_name, section, component)
        if not component:
            logs.append(f"跳过 {path.name}: 不是 UX/UY/UZ 方向位移结果，不能用于位移/旋转计算。")
            continue
        payload = {
            "path": path,
            "result_name": result_name,
            "section": section,
            "table": table,
        }
        if component == "vector":
            for axis in ["ux", "uy", "uz"]:
                component_tables[(lens_name, axis)] = payload
        else:
            component_tables[(lens_name, component)] = payload

    by_lens: dict[str, dict[str, dict[str, object]]] = {}
    for (lens_name, component), payload in component_tables.items():
        by_lens.setdefault(lens_name, {})[component] = payload

    all_records: list[dict[str, object]] = []
    latest_records: list[LensPoseRecord] = []
    for lens_name, components in sorted(by_lens.items()):
        pose = _calculate_lens_pose_from_components(lens_name, components, logs)
        if pose is None:
            continue
        record, all_record = pose
        latest_records.append(record)
        all_records.append(all_record)

    if skipped_files:
        logs.append(f"跳过无法识别的 TXT 文件 {len(skipped_files)} 个。")
    if not all_records:
        logs.append(
            "没有算出可导入 Zemax 的镜片位移/旋转。当前导出文件通常只有总形变或单方向 Directional Deformation。"
        )

    saved_paths: dict[str, str] = {}
    if save:
        output_dir = Path(save_dir) if save_dir is not None else folder / "zemax_pose_calculation"
        output_dir.mkdir(parents=True, exist_ok=True)
        saved_paths = _save_pose_calculation(output_dir, all_records, logs, folder)
        logs.append("位移/旋转计算结果已保存: " + ", ".join(saved_paths.values()))

    return {
        "latest_records": latest_records,
        "all_records": all_records,
        "logs": logs,
        "saved_paths": saved_paths,
        "source_folder": str(folder),
    }


def import_lens_poses_to_zmx(
    zemax_file: Path,
    export_folder: Path,
    output_file: Path | None = None,
    *,
    add_delta: bool = True,
    displacement_scale: float = 1.0,
    rotation_unit: str = "deg",
) -> dict[str, object]:
    zemax_file = require_file(Path(zemax_file), "Zemax project file")
    if zemax_file.suffix.lower() != ".zmx":
        raise ValueError("当前版本先支持 .zmx 文本工程。请选择 .zmx，或先在 Zemax 中另存为 .zmx 后再导入。")

    records = read_lens_pose_records(export_folder)
    if not records:
        raise ValueError(
            "Mechanical 导出文件夹中没有读取到镜片位移/旋转数据。"
            "支持 CSV/TXT/JSON，至少需要 name/comment/named_selection 和 x/y/z/rx/ry/rz 或 dx/dy/dz/tx/ty/tz 字段。"
        )

    output_file = Path(output_file) if output_file is not None else default_zemax_pose_output_path(zemax_file)
    text, encoding = _read_zmx_text(zemax_file)
    lines = text.splitlines(keepends=True)
    object_blocks = _find_zmx_nsc_object_blocks(lines)
    if not object_blocks:
        raise ValueError("没有在 Zemax 文件中找到 NSC 对象块，无法按 Comment 匹配导入。")
    baseline_rows = []
    for block in object_blocks:
        current = _parse_nsop_line(lines[int(block["nsop_index"])])
        if current is None:
            continue
        baseline_rows.append(
            {
                "object_index": int(block.get("object_index") or 0),
                "comment": str(block.get("comment") or ""),
                "values": [float(value) for value in current["values"]],
            }
        )
    baseline = _load_or_create_pose_baseline(zemax_file, baseline_rows)

    matched: list[dict[str, object]] = []
    unmatched: list[dict[str, object]] = []
    updated_lines = list(lines)
    rotation_factor = 180.0 / math.pi if rotation_unit.lower().startswith("rad") else 1.0

    for record in records:
        matches = _match_object_blocks(record.name, object_blocks)
        if not matches:
            unmatched.append(record.as_dict())
            continue
        for block in matches:
            current = _parse_nsop_line(lines[block["nsop_index"]])
            if current is None:
                unmatched.append({**record.as_dict(), "message": "匹配到 Comment，但没有有效 NSOP 位置行"})
                continue

            delta = [
                record.x * displacement_scale,
                record.y * displacement_scale,
                record.z * displacement_scale,
                record.rx * rotation_factor,
                record.ry * rotation_factor,
                record.rz * rotation_factor,
            ]
            if add_delta:
                baseline_entry = _pose_baseline_entry_for_object(baseline, block)
                baseline_values = list(baseline_entry.get("values") or current["values"])
                if len(baseline_values) != 6:
                    baseline_values = current["values"]
                new_values = [float(baseline_values[i]) + delta[i] for i in range(6)]
            else:
                baseline_entry = {}
                baseline_values = current["values"]
                new_values = delta
            updated_lines[block["nsop_index"]] = _format_nsop_line(
                lines[block["nsop_index"]],
                new_values,
                current["tail"],
            )
            matched.append(
                {
                    **record.as_dict(),
                    "object_index": block["object_index"],
                    "comment": block["comment"],
                    "old_values": current["values"],
                    "baseline_values": baseline_values,
                    "baseline_source": baseline_entry.get("source") or baseline.get("source") or "",
                    "new_values": new_values,
                }
            )

    if not matched:
        raise ValueError("没有任何 Mechanical 命名选择名称匹配到 Zemax NSC 对象 Comment。")

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text("".join(updated_lines), encoding=encoding)
    return {
        "input_file": str(zemax_file),
        "output_file": str(output_file),
        "export_folder": str(export_folder),
        "record_count": len(records),
        "matched_count": len(matched),
        "unmatched_count": len(unmatched),
        "matched": matched,
        "unmatched": unmatched,
        "mode": "delta" if add_delta else "absolute",
        "displacement_scale": displacement_scale,
        "rotation_unit": rotation_unit,
        "baseline_file": str(baseline.get("path") or ""),
        "baseline_source": str(baseline.get("source") or ""),
        "baseline_created": bool(baseline.get("created")),
    }


def import_lens_poses_to_current_opticstudio(
    export_folder: Path,
    project_file: Path | None = None,
    *,
    save: bool = True,
) -> dict[str, object]:
    """Import calculated lens pose deltas into the selected, currently open OpticStudio NSC system."""
    records = read_lens_pose_records(export_folder)
    if not records:
        raise ValueError(
            "Mechanical 导出文件夹中没有读取到镜片位移/旋转数据。"
            "需要先读取位移/旋转，并确保导出中包含每个镜片的 UX/UY/UZ 和节点坐标。"
        )

    expected_project = require_file(project_file, "Zemax project file") if project_file is not None else None
    application, system = _connect_or_open_selected_opticstudio_system(expected_project)
    system_file = str(getattr(system, "SystemFile", "") or "")
    if expected_project is not None and not _same_existing_file(system_file, expected_project):
        current_text = system_file or "未保存/空工程"
        raise RuntimeError(
            "当前连接的 OpticStudio 工程不是所选 Zemax 工程，已停止导入，避免写入错误工程。\n"
            f"所选工程: {expected_project}\n"
            f"当前工程: {current_text}\n"
            "请关闭本程序之外占用该工程的 OpticStudio 会话，或重新选择正确的 Zemax 工程。"
        )
    nce = getattr(system, "NCE", None)
    if nce is None:
        raise RuntimeError("当前 OpticStudio 工程没有非序列组件编辑器 NCE；当前版本只处理非序列模式。")

    object_rows = _current_nce_object_rows(nce)
    if not object_rows:
        raise RuntimeError("当前 OpticStudio 非序列工程没有可匹配的 NCE 对象。")

    zemax_length_unit = _current_zemax_length_unit(system)
    baseline = _load_or_create_pose_baseline(expected_project, object_rows)
    matched, unmatched = _apply_pose_records_to_nce(records, object_rows, baseline, zemax_length_unit)

    if not matched:
        raise ValueError("没有任何 Mechanical 命名选择名称匹配到当前 OpticStudio NCE 对象 Comment。")

    saved = False
    save_error = ""
    if save:
        try:
            system.Save()
            saved = True
        except Exception as exc:
            save_error = str(exc)
    else:
        _mark_zosapi_session_dirty(application, system, True)

    return {
        "system_file": system_file,
        "expected_project_file": str(expected_project or ""),
        "system_name": str(getattr(system, "SystemName", "") or ""),
        "application_mode": str(getattr(application, "Mode", "") or ""),
        "zemax_length_unit": zemax_length_unit,
        "zemax_rotation_unit": "deg",
        "matched": matched,
        "unmatched": unmatched,
        "matched_count": len(matched),
        "unmatched_count": len(unmatched),
        "saved": saved,
        "save_error": save_error,
        "mode": "baseline_plus_delta",
        "baseline_file": str(baseline.get("path") or ""),
        "baseline_source": str(baseline.get("source") or ""),
        "baseline_created": bool(baseline.get("created")),
        "baseline_write_error": str(baseline.get("write_error") or ""),
    }


def close_managed_opticstudio_project(
    project_file: Path | None = None,
    *,
    save: bool,
) -> dict[str, object]:
    """Save/discard and close OpticStudio sessions created by this program."""
    expected_project = require_file(project_file, "Zemax project file") if project_file is not None else None
    matched_sessions = _matching_zosapi_sessions(expected_project)
    saved_count = 0
    closed_count = 0
    errors: list[str] = []
    session_infos: list[dict[str, object]] = []

    for session in matched_sessions:
        application = session.get("application")
        system = _valid_current_zos_application(application)
        if application is None or system is None:
            _forget_zosapi_session(session)
            continue
        system_file = str(getattr(system, "SystemFile", "") or session.get("project_file") or "")
        session_info = {
            "system_file": system_file,
            "dirty": bool(session.get("dirty")),
            "saved": False,
            "closed": False,
        }
        try:
            if save:
                system.Save()
                session["dirty"] = False
                session_info["saved"] = True
                saved_count += 1
            try:
                system.Close(bool(save))
            except Exception:
                pass
            application.CloseApplication()
            session_info["closed"] = True
            closed_count += 1
        except Exception as exc:
            errors.append(f"{system_file or '未命名工程'}: {exc}")
        finally:
            session_infos.append(session_info)
            _forget_zosapi_session(session)

    return {
        "matched_sessions": len(matched_sessions),
        "saved_count": saved_count,
        "closed_count": closed_count,
        "errors": errors,
        "sessions": session_infos,
        "save_requested": save,
    }


def _same_existing_file(left: str | Path, right: str | Path) -> bool:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text or not right_text:
        return False
    left_path = Path(left_text)
    right_path = Path(right_text)
    try:
        return left_path.samefile(right_path)
    except Exception:
        pass
    try:
        left_norm = os.path.normcase(os.path.abspath(str(left_path)))
        right_norm = os.path.normcase(os.path.abspath(str(right_path)))
    except Exception:
        return False
    return left_norm == right_norm


def _current_zemax_length_unit(system: object) -> str:
    try:
        units = system.SystemData.Units
        return str(units.LensUnits)
    except Exception:
        return "Millimeters"


def _length_unit_scale(from_unit: str, to_unit: str) -> float:
    from_meters = _length_unit_to_meters(from_unit)
    to_meters = _length_unit_to_meters(to_unit)
    if to_meters == 0:
        raise ValueError(f"无法识别 Zemax 长度单位: {to_unit}")
    return from_meters / to_meters


def _length_unit_to_meters(unit: str) -> float:
    normalized = _normalize_unit(unit)
    aliases = {
        "m": 1.0,
        "meter": 1.0,
        "meters": 1.0,
        "metre": 1.0,
        "metres": 1.0,
        "mm": 1.0e-3,
        "millimeter": 1.0e-3,
        "millimeters": 1.0e-3,
        "millimetre": 1.0e-3,
        "millimetres": 1.0e-3,
        "cm": 1.0e-2,
        "centimeter": 1.0e-2,
        "centimeters": 1.0e-2,
        "centimetre": 1.0e-2,
        "centimetres": 1.0e-2,
        "um": 1.0e-6,
        "micrometer": 1.0e-6,
        "micrometers": 1.0e-6,
        "micrometre": 1.0e-6,
        "micrometres": 1.0e-6,
        "micron": 1.0e-6,
        "microns": 1.0e-6,
        "nm": 1.0e-9,
        "nanometer": 1.0e-9,
        "nanometers": 1.0e-9,
        "nanometre": 1.0e-9,
        "nanometres": 1.0e-9,
        "in": 0.0254,
        "inch": 0.0254,
        "inches": 0.0254,
    }
    if normalized in aliases:
        return aliases[normalized]
    raise ValueError(f"无法识别长度单位: {unit}")


def _rotation_unit_scale(from_unit: str, to_unit: str) -> float:
    from_radians = _rotation_unit_to_radians(from_unit)
    to_radians = _rotation_unit_to_radians(to_unit)
    if to_radians == 0:
        raise ValueError(f"无法识别目标角度单位: {to_unit}")
    return from_radians / to_radians


def _rotation_unit_to_radians(unit: str) -> float:
    normalized = _normalize_unit(unit)
    if normalized in {"deg", "degree", "degrees"}:
        return math.pi / 180.0
    if normalized in {"rad", "radian", "radians"}:
        return 1.0
    raise ValueError(f"无法识别角度单位: {unit}")


def _normalize_unit(unit: str) -> str:
    normalized = str(unit or "").strip().lower()
    normalized = normalized.replace("µ", "u").replace("μ", "u")
    normalized = re.sub(r"[^a-z0-9]+", "", normalized)
    return normalized


def current_opticstudio_project_info(project_file: Path | None = None) -> dict[str, object]:
    """Read the managed background OpticStudio project without opening a GUI."""
    expected_project = require_file(project_file, "Zemax project file") if project_file is not None else None
    application, system = _connect_or_open_selected_opticstudio_system(expected_project, open_if_missing=False)
    system_file = str(getattr(system, "SystemFile", "") or "")
    if expected_project is not None and not _same_existing_file(system_file, expected_project):
        raise RuntimeError(
            "当前后台 ZOS-API 会话中的 OpticStudio 工程不是所选 Zemax 工程。\n"
            f"所选工程: {expected_project}\n"
            f"当前工程: {system_file or '未保存/空工程'}"
        )
    nce = getattr(system, "NCE", None)
    object_count = int(getattr(nce, "NumberOfObjects", 0) or 0) if nce is not None else 0
    return {
        "system_file": system_file,
        "system_name": str(getattr(system, "SystemName", "") or ""),
        "mode": str(getattr(system, "Mode", "") or ""),
        "application_mode": str(getattr(application, "Mode", "") or ""),
        "object_count": object_count,
        "opticstudio_instance": int(getattr(application, "OpticStudioInstance", 0) or 0),
        "launch_method": "standalone_background",
    }


def calculate_pose_time_series_from_mechanical_exports(
    folder: Path,
    save_dir: Path | None = None,
    *,
    save: bool = True,
) -> dict[str, object]:
    folder = require_file(Path(folder), "Mechanical export folder")
    if not folder.is_dir():
        raise NotADirectoryError(f"Mechanical export path is not a folder: {folder}")

    logs: list[str] = []
    summary = _read_solution_summary(folder / "solution_results_summary.txt")
    component_tables: dict[tuple[str, tuple[object, ...], str], dict[str, object]] = {}
    frame_infos: dict[tuple[object, ...], dict[str, object]] = {}
    skipped_files: list[str] = []

    for path in sorted(folder.glob("*.txt")):
        if path.name.lower() == "solution_results_summary.txt" or path.name.endswith("_properties.txt"):
            continue
        table = _read_mechanical_export_table(path)
        if table is None:
            skipped_files.append(path.name)
            continue
        result_name = _result_name_from_export_file(path)
        section = summary.get(result_name, {})
        component = _component_from_table(table, section, path)
        lens_name = _lens_name_from_result(result_name, section, component)
        if not component:
            logs.append(f"跳过 {path.name}: 不是 UX/UY/UZ 方向位移结果，不能用于位移/旋转计算。")
            continue
        frame_info = _time_frame_info_from_export_file(path)
        frame_key = tuple(frame_info["key"])
        frame_infos.setdefault(frame_key, frame_info)
        payload = {
            "path": path,
            "result_name": result_name,
            "section": section,
            "table": table,
            "frame_info": frame_info,
        }
        if component == "vector":
            for axis in ["ux", "uy", "uz"]:
                component_tables[(lens_name, frame_key, axis)] = payload
        else:
            component_tables[(lens_name, frame_key, component)] = payload

    by_frame: dict[tuple[object, ...], dict[str, dict[str, dict[str, object]]]] = {}
    for (lens_name, frame_key, component), payload in component_tables.items():
        by_frame.setdefault(frame_key, {}).setdefault(lens_name, {})[component] = payload

    frames: list[LensPoseFrame] = []
    all_frame_records: list[dict[str, object]] = []
    sorted_frame_keys = sorted(
        by_frame,
        key=lambda key: (
            frame_infos.get(key, {}).get("time_sort") is None,
            frame_infos.get(key, {}).get("time_sort") if frame_infos.get(key, {}).get("time_sort") is not None else 0.0,
            frame_infos.get(key, {}).get("set_index") or 0,
            str(key),
        ),
    )
    for frame_position, frame_key in enumerate(sorted_frame_keys, start=1):
        frame_info = frame_infos.get(frame_key, {})
        records: list[LensPoseRecord] = []
        frame_source_files: list[str] = []
        for lens_name, components in sorted(by_frame[frame_key].items()):
            pose = _calculate_lens_pose_from_components(
                lens_name,
                components,
                logs,
                context=f"时间 {frame_info.get('time_label') or frame_position}",
            )
            if pose is None:
                continue
            record, all_record = pose
            records.append(record)
            frame_source_files.extend(str(path) for path in str(record.source).split(", ") if path)
            all_frame_record = {
                **all_record,
                "frame_index": frame_position,
                "time_value": frame_info.get("time_value"),
                "time_label": str(frame_info.get("time_label") or ""),
                "set_index": frame_info.get("set_index"),
            }
            all_frame_records.append(all_frame_record)
        if records:
            frames.append(
                LensPoseFrame(
                    frame_index=frame_position,
                    time_value=frame_info.get("time_value") if isinstance(frame_info.get("time_value"), float) else None,
                    time_label=str(frame_info.get("time_label") or frame_position),
                    set_index=frame_info.get("set_index") if isinstance(frame_info.get("set_index"), int) else None,
                    records=records,
                    source_files=list(dict.fromkeys(frame_source_files)),
                )
            )

    if skipped_files:
        logs.append(f"跳过无法识别的 TXT 文件 {len(skipped_files)} 个。")
    if not frames:
        logs.append("没有算出可用于 Zemax 时间序列追迹的镜片位移/旋转帧。")

    saved_paths: dict[str, str] = {}
    if save:
        output_dir = Path(save_dir) if save_dir is not None else folder / "zemax_pose_calculation"
        output_dir.mkdir(parents=True, exist_ok=True)
        saved_paths = _save_pose_time_series_calculation(output_dir, frames, all_frame_records, logs, folder)
        logs.append("时间序列位移/旋转计算结果已保存: " + ", ".join(saved_paths.values()))

    return {
        "frames": frames,
        "frame_count": len(frames),
        "all_records": all_frame_records,
        "logs": logs,
        "saved_paths": saved_paths,
        "source_folder": str(folder),
    }


def _format_float_for_log(value: object) -> str:
    try:
        return f"{float(value):.6g}"
    except (TypeError, ValueError):
        return str(value)


def _matlab_raw_dts_path(playback_file: Path) -> Path:
    playback_file = Path(playback_file)
    suffix = playback_file.suffix or ".dts"
    stem = playback_file.stem
    if stem.lower().endswith("_playback"):
        stem = stem[:-9]
    return playback_file.with_name(f"{stem}_matlab_raw{suffix}")


def _format_pose_record_for_log(record: LensPoseRecord) -> str:
    residual_text = (
        f"; 面形残差RMS={record.rms_residual:.3g} {record.displacement_unit}"
        if record.rms_residual is not None
        else ""
    )
    return (
        f"{record.name}: "
        f"平移(偏心/离焦) X={record.x:.6g}, Y={record.y:.6g}, Z={record.z:.6g} {record.displacement_unit}; "
        f"倾斜 Rx={record.rx:.6g}, Ry={record.ry:.6g}, Rz={record.rz:.6g} {record.rotation_unit}"
        f"{residual_text}"
    )


def _time_series_pose_calculation_log(series: dict[str, object], frames: list[LensPoseFrame]) -> str:
    lines = [
        "Zemax 时间序列位移/旋转计算完成，先列出全部时间帧的镜片位移/旋转；下面这些值已全部算完，然后才会开始逐帧导入和追迹。",
        f"Mechanical 导出文件夹: {series.get('source_folder') or ''}",
        f"时间帧数: {len(frames)}",
    ]
    saved_paths = series.get("saved_paths") if isinstance(series.get("saved_paths"), dict) else {}
    if saved_paths:
        lines.append("位移/旋转计算文件:")
        for key, value in saved_paths.items():
            lines.append(f"  {key}: {value}")
    for frame in frames:
        lines.append(f"时间帧 {frame.frame_index}/{len(frames)}，时刻={frame.time_label}:")
        for record in frame.records:
            lines.append("  " + _format_pose_record_for_log(record))
    extra_logs = [str(item) for item in list(series.get("logs") or []) if str(item).strip()]
    if extra_logs:
        lines.append("计算诊断:")
        lines.extend("  " + item for item in extra_logs)
    return "\n".join(lines)


def _time_series_frame_trace_log(
    frame: LensPoseFrame,
    frame_count: int,
    matched: list[dict[str, object]],
    detector_number: int,
    zemax_length_unit: str,
) -> str:
    lines = [
        f"Zemax 时间序列追迹: 当前导入时间帧 {frame.frame_index}/{frame_count}，时刻={frame.time_label}，"
        f"已写入以下镜片位移/旋转，随后清空 Detector 并追迹 Detector {detector_number}。"
    ]
    for item in matched:
        try:
            object_index = int(item.get("object_index") or 0)
        except (TypeError, ValueError):
            object_index = 0
        comment = str(item.get("comment") or "").strip()
        source_name = str(item.get("name") or "").strip()
        display_name = comment or source_name or "(无 Comment)"
        if source_name and comment and source_name != comment:
            display_name = f"{display_name}（Mechanical={source_name}）"
        converted = list(item.get("converted_delta_values") or [])
        if len(converted) >= 6:
            lines.append(
                f"  {object_index:03d} {display_name}: "
                f"导入位移=({_format_float_for_log(converted[0])}, {_format_float_for_log(converted[1])}, "
                f"{_format_float_for_log(converted[2])}) {zemax_length_unit}; "
                f"导入旋转=({_format_float_for_log(converted[3])}, {_format_float_for_log(converted[4])}, "
                f"{_format_float_for_log(converted[5])}) deg"
            )
        else:
            lines.append(f"  {object_index:03d} {display_name}")
    return "\n".join(lines)


def list_zemax_detectors(project_file: Path) -> dict[str, object]:
    """Open/reuse the selected Zemax project in the background and list NSC detector objects."""
    project_file = require_file(Path(project_file), "Zemax project file")
    _application, system = _connect_or_open_selected_opticstudio_system(project_file)
    nce = getattr(system, "NCE", None)
    if nce is None:
        raise RuntimeError("当前 OpticStudio 工程没有非序列组件编辑器 NCE；无法读取探测器。")
    detectors = _current_detector_rows(nce)
    return {
        "system_file": str(getattr(system, "SystemFile", "") or ""),
        "system_name": str(getattr(system, "SystemName", "") or ""),
        "object_count": int(getattr(nce, "NumberOfObjects", 0) or 0),
        "detector_count": len(detectors),
        "detectors": detectors,
    }


def run_zemax_ray_trace_and_read_detector(
    project_file: Path,
    detector_number: int,
    *,
    output_dir: Path | None = None,
    clear_detectors: bool = True,
    log_scale: bool = True,
    cpu_core_count: int | None = None,
) -> dict[str, object]:
    """Run a background NSC ray trace and read the selected detector result."""
    project_file = require_file(Path(project_file), "Zemax project file")
    detector_number = int(detector_number)
    if detector_number <= 0:
        raise ValueError(f"无效探测器编号: {detector_number}")

    _application, system = _connect_or_open_selected_opticstudio_system(project_file)
    nce = getattr(system, "NCE", None)
    if nce is None:
        raise RuntimeError("当前 OpticStudio 工程没有非序列组件编辑器 NCE；无法光线追迹。")

    detectors = _current_detector_rows(nce)
    detector = next((item for item in detectors if int(item.get("object_index") or 0) == detector_number), None)
    if detector is None:
        raise ValueError(f"所选对象 {detector_number} 不是可读取的 Detector。")

    ray_trace = system.Tools.OpenNSCRayTrace()
    cpu_core_info = _configure_ray_trace_cpu_cores(ray_trace, cpu_core_count)
    try:
        if clear_detectors:
            ray_trace.ClearDetectors(0)
        ray_trace.RunAndWaitForCompletion()
    finally:
        try:
            ray_trace.Close()
        except Exception:
            pass

    detector_result = _read_detector_result_grid(nce, detector_number)
    if output_dir is None:
        output_dir = project_file.parent / "zemax_raytrace_results"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    base_name = _detector_result_base_name(project_file, detector, detector_number)
    mat_path = output_dir / f"{base_name}.mat"
    png_path = output_dir / f"{base_name}.png"
    _write_detector_grid_mat(mat_path, detector_result, detector)
    _write_false_color_png(
        png_path,
        detector_result["grid"],
        float(detector_result["min_value"]),
        float(detector_result["max_value"]),
        log_scale=log_scale,
    )

    return {
        "system_file": str(getattr(system, "SystemFile", "") or ""),
        "system_name": str(getattr(system, "SystemName", "") or ""),
        "detector": detector,
        "ray_trace": {
            "clear_detectors": bool(clear_detectors),
            "settings_source": "zemax_current_defaults",
        },
        "cpu_cores": cpu_core_info,
        "summary": {key: value for key, value in detector_result.items() if key != "grid"},
        "mat_path": str(mat_path),
        "image_path": str(png_path),
        "log_scale": bool(log_scale),
    }


def run_zemax_ray_trace_clear_detectors(
    project_file: Path,
    *,
    cpu_core_count: int | None = None,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    """Clear all NSC detectors and ray trace once without reading detector grids."""
    project_file = require_file(Path(project_file), "Zemax project file")
    _application, system = _connect_or_open_selected_opticstudio_system(project_file)
    nce = getattr(system, "NCE", None)
    if nce is None:
        raise RuntimeError("当前 OpticStudio 工程没有非序列组件编辑器 NCE；无法光线追迹。")

    detectors = _current_detector_rows(nce)
    if not detectors:
        raise RuntimeError("当前 Zemax 非序列工程没有可读取的 Detector。")

    ray_trace = system.Tools.OpenNSCRayTrace()
    cpu_core_info = _configure_ray_trace_cpu_cores(ray_trace, cpu_core_count)
    try:
        if progress_callback is not None:
            progress_callback(
                {
                    "stage": "Zemax 光线追迹",
                    "status": f"清空 {len(detectors)} 个探测器，并按 Zemax 当前默认设置开始追迹",
                }
            )
        ray_trace.ClearDetectors(0)
        ray_trace.RunAndWaitForCompletion()
    finally:
        try:
            ray_trace.Close()
        except Exception:
            pass

    detectors = _current_detector_rows(nce)
    return {
        "system_file": str(getattr(system, "SystemFile", "") or ""),
        "system_name": str(getattr(system, "SystemName", "") or ""),
        "object_count": int(getattr(nce, "NumberOfObjects", 0) or 0),
        "detector_count": len(detectors),
        "detectors": detectors,
        "ray_trace": {
            "clear_detectors": True,
            "settings_source": "zemax_current_defaults",
        },
        "cpu_cores": cpu_core_info,
    }


def read_zemax_detector_result(
    project_file: Path,
    detector_number: int,
    *,
    output_dir: Path | None = None,
    log_scale: bool = True,
    export_matlab: bool = False,
) -> dict[str, object]:
    """Read/export one selected detector from the current background OpticStudio project.

    两条路径都读取全分辨率探测器矩阵:
    - ``export_matlab=False``(快速查看):只读全分辨率网格并生成伪彩色 PNG 预览,不落盘数据文件;
    - ``export_matlab=True``(完整导出):额外把全分辨率矩阵写成 MATLAB ``.mat`` 文件。
    """
    project_file = require_file(Path(project_file), "Zemax project file")
    detector_number = int(detector_number)
    if detector_number <= 0:
        raise ValueError(f"无效探测器编号: {detector_number}")

    connect_started = time.monotonic()
    _application, system = _connect_or_open_selected_opticstudio_system(project_file)
    nce = getattr(system, "NCE", None)
    if nce is None:
        raise RuntimeError("当前 OpticStudio 工程没有非序列组件编辑器 NCE；无法读取探测器。")

    detectors = _current_detector_rows(nce)
    detector = next((item for item in detectors if int(item.get("object_index") or 0) == detector_number), None)
    if detector is None:
        raise ValueError(f"所选对象 {detector_number} 不是可读取的 Detector。")

    connect_and_list_seconds = time.monotonic() - connect_started
    if output_dir is None:
        output_dir = project_file.parent / "zemax_raytrace_results"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result = _export_detector_result(
        nce,
        project_file,
        detector,
        output_dir,
        log_scale=log_scale,
        export_matlab=export_matlab,
    )
    timings = result.setdefault("timings", {})
    if isinstance(timings, dict):
        timings["connect_and_list_seconds"] = round(connect_and_list_seconds, 3)
    return {
        "system_file": str(getattr(system, "SystemFile", "") or ""),
        "system_name": str(getattr(system, "SystemName", "") or ""),
        "object_count": int(getattr(nce, "NumberOfObjects", 0) or 0),
        "detector_count": len(detectors),
        "detectors": detectors,
        "result": result,
        "output_dir": str(output_dir),
        "log_scale": bool(log_scale),
    }


def run_zemax_time_series_ray_trace(
    export_folder: Path,
    project_file: Path,
    detector_number: int | None = None,
    *,
    output_file: Path | None = None,
    cpu_core_count: int | None = None,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    """Apply transient Mechanical pose frames, trace each frame, and save raw detector data to .dts."""
    export_folder = require_file(Path(export_folder), "Mechanical export folder")
    if not export_folder.is_dir():
        raise NotADirectoryError(f"Mechanical export path is not a folder: {export_folder}")
    project_file = require_file(Path(project_file), "Zemax project file")

    if progress_callback is not None:
        progress_callback({"stage": "Zemax 时间序列追迹", "status": "正在解析 Mechanical 时间序列位移/旋转"})
    series = calculate_pose_time_series_from_mechanical_exports(export_folder, save=True)
    frames = [frame for frame in series.get("frames") or [] if isinstance(frame, LensPoseFrame)]
    if not frames:
        raise ValueError(
            "所选 Mechanical 导出文件夹没有可用于时间序列追迹的位移/旋转帧。"
            "需要导出瞬态结果集，并包含镜片 UX/UY/UZ 或 UVECTORS 节点数据。"
        )

    if progress_callback is not None:
        progress_callback(
            {
                "stage": "Zemax 时间序列位移/旋转计算完成",
                "status": f"已先计算 {len(frames)} 个时间帧的全部镜片位移/旋转，随后才开始追迹",
                "log": _time_series_pose_calculation_log(series, frames),
            }
        )
        progress_callback({"stage": "Zemax 时间序列追迹", "status": f"正在打开 Zemax 工程，时间帧 {len(frames)} 个"})
    application, system = _connect_or_open_selected_opticstudio_system(project_file)
    nce = getattr(system, "NCE", None)
    if nce is None:
        raise RuntimeError("当前 OpticStudio 工程没有非序列组件编辑器 NCE；无法进行时间序列追迹。")
    object_rows = _current_nce_object_rows(nce)
    if not object_rows:
        raise RuntimeError("当前 OpticStudio 非序列工程没有可匹配的 NCE 对象。")
    detectors = _current_detector_rows(nce)
    if not detectors:
        raise RuntimeError("当前 Zemax 非序列工程没有可读取的 Detector。")
    if detector_number is None:
        detector_number = int(detectors[0].get("object_index") or 0)
    detector_number = int(detector_number)
    detector = next((item for item in detectors if int(item.get("object_index") or 0) == detector_number), None)
    if detector is None:
        raise ValueError(f"所选对象 {detector_number} 不是可读取的 Detector。")

    if output_file is None:
        output_dir = project_file.parent / "zemax_time_series_results"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = output_dir / f"{project_file.stem}_transient_detector_{detector_number:03d}_{timestamp}.dts"
    output_file = Path(output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    raw_output_file = _matlab_raw_dts_path(output_file)
    raw_output_file.parent.mkdir(parents=True, exist_ok=True)
    temp_output_file = output_file.with_name(f"{output_file.name}.tmp")
    temp_raw_output_file = raw_output_file.with_name(f"{raw_output_file.name}.tmp")
    if temp_output_file.exists():
        temp_output_file.unlink()
    if temp_raw_output_file.exists():
        temp_raw_output_file.unlink()

    zemax_length_unit = _current_zemax_length_unit(system)
    if progress_callback is not None:
        progress_callback({"stage": "Zemax 时间序列追迹", "status": "正在读取/创建 Zemax 原始基准位置"})
    baseline = _load_or_create_pose_baseline(project_file, object_rows)
    if progress_callback is not None:
        progress_callback({"stage": "Zemax 时间序列追迹", "status": "正在打开非序列光线追迹工具"})
    ray_trace = system.Tools.OpenNSCRayTrace()
    cpu_core_info = _configure_ray_trace_cpu_cores(ray_trace, cpu_core_count)
    frame_entries: list[dict[str, object]] = []
    unmatched_by_frame: list[dict[str, object]] = []
    started = time.monotonic()

    try:
        with zipfile.ZipFile(temp_output_file, "w", compression=zipfile.ZIP_DEFLATED) as playback_archive, zipfile.ZipFile(
            temp_raw_output_file,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as raw_archive:
            for frame in frames:
                if progress_callback is not None:
                    progress_callback(
                        {
                            "stage": "Zemax 时间序列追迹",
                            "status": f"帧 {frame.frame_index}/{len(frames)}，时间 {frame.time_label}: 写入镜片位移/旋转",
                        }
                    )
                matched, unmatched = _apply_pose_records_to_nce(
                    frame.records,
                    object_rows,
                    baseline,
                    zemax_length_unit,
                )
                if not matched:
                    raise ValueError(f"时间帧 {frame.time_label} 没有任何镜片名称匹配到 Zemax NCE Object Comment。")
                if unmatched:
                    unmatched_by_frame.append(
                        {
                            "frame_index": frame.frame_index,
                            "time_label": frame.time_label,
                            "unmatched": unmatched,
                        }
                    )

                if progress_callback is not None:
                    progress_callback(
                        {
                            "stage": "Zemax 时间序列追迹",
                            "status": f"帧 {frame.frame_index}/{len(frames)}，时间 {frame.time_label}: 已导入镜片位移/旋转，准备追迹",
                            "log": _time_series_frame_trace_log(
                                frame,
                                len(frames),
                                matched,
                                detector_number,
                                zemax_length_unit,
                            ),
                        }
                    )
                    progress_callback(
                        {
                            "stage": "Zemax 时间序列追迹",
                            "status": f"帧 {frame.frame_index}/{len(frames)}，时间 {frame.time_label}: 清空 Detector 并追迹",
                        }
                    )
                ray_trace.ClearDetectors(0)
                ray_trace.RunAndWaitForCompletion()
                if progress_callback is not None:
                    progress_callback(
                        {
                            "stage": "Zemax 时间序列追迹",
                            "status": f"帧 {frame.frame_index}/{len(frames)}，时间 {frame.time_label}: 读取 Detector 原始矩阵",
                        }
                    )
                detector_result = _read_detector_result_grid(nce, detector_number, max_dimension=None)
                frame_dir = f"frames/{frame.frame_index:04d}"
                detector_csv_path = f"{frame_dir}/detector.csv"
                detector_png_path = f"{frame_dir}/detector.png"
                pose_json_path = f"{frame_dir}/pose.json"
                if progress_callback is not None:
                    progress_callback(
                        {
                            "stage": "Zemax 时间序列追迹",
                            "status": f"帧 {frame.frame_index}/{len(frames)}，时间 {frame.time_label}: 分别写入快速播放PNG和MATLAB原始CSV",
                        }
                    )
                raw_archive.writestr(detector_csv_path, _grid_to_csv_text(detector_result["grid"]))
                playback_archive.writestr(
                    detector_png_path,
                    _false_color_png_bytes(
                        detector_result["grid"],
                        float(detector_result.get("min_value") or 0.0),
                        float(detector_result.get("max_value") or 0.0),
                        log_scale=True,
                    ),
                )
                pose_json_text = json.dumps(
                    {
                        "frame": frame.as_dict(),
                        "matched": matched,
                        "unmatched": unmatched,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                playback_archive.writestr(pose_json_path, pose_json_text)
                raw_archive.writestr(
                    pose_json_path,
                    pose_json_text,
                )
                summary = {key: value for key, value in detector_result.items() if key != "grid"}
                frame_entries.append(
                    {
                        "frame_index": frame.frame_index,
                        "time_value": frame.time_value,
                        "time_label": frame.time_label,
                        "set_index": frame.set_index,
                        "detector_png": detector_png_path,
                        "detector_png_full_resolution": True,
                        "raw_detector_csv": detector_csv_path,
                        "raw_dts_file": str(raw_output_file),
                        "pose_json": pose_json_path,
                        "matched_count": len(matched),
                        "unmatched_count": len(unmatched),
                        "imported_objects": _matched_import_object_summaries(matched),
                        "summary": summary,
                    }
                )

            manifest = {
                "schema": "ansys_zemax_transient_detector_playback_dts_v1",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "file_role": "fast_playback_full_resolution_png",
                "matlab_raw_file": str(raw_output_file),
                "project_file": str(project_file),
                "source_export_folder": str(export_folder),
                "system_file": str(getattr(system, "SystemFile", "") or ""),
                "system_name": str(getattr(system, "SystemName", "") or ""),
                "zemax_length_unit": zemax_length_unit,
                "detector": detector,
                "frame_count": len(frame_entries),
                "frames": frame_entries,
                "cpu_cores": cpu_core_info,
                "pose_calculation": {
                    "saved_paths": series.get("saved_paths") or {},
                    "logs": series.get("logs") or [],
                },
                "unmatched_by_frame": unmatched_by_frame,
                "playback": {
                    "description": "Fast playback file. Frames contain full-resolution detector PNG images. MATLAB raw detector CSV data is saved in matlab_raw_file.",
                    "full_resolution_png": True,
                },
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
            raw_frame_entries = []
            for frame_entry in frame_entries:
                raw_entry = dict(frame_entry)
                raw_entry.pop("detector_png", None)
                raw_entry.pop("detector_png_full_resolution", None)
                raw_entry.pop("raw_dts_file", None)
                raw_entry["detector_csv"] = raw_entry.pop("raw_detector_csv", "")
                raw_frame_entries.append(raw_entry)
            raw_manifest = {
                **manifest,
                "schema": "ansys_zemax_transient_detector_matlab_raw_dts_v1",
                "file_role": "matlab_raw_detector_csv",
                "playback_file": str(output_file),
                "frames": raw_frame_entries,
                "matlab": {
                    "description": "This .dts file is a ZIP container. In MATLAB: unzip('file.dts','out'); manifest=jsondecode(fileread(fullfile('out','manifest.json'))); data=readmatrix(fullfile('out',manifest.frames(1).detector_csv));",
                    "loader": "matlab/load_dts_detector_series.m",
                },
            }
            raw_manifest.pop("playback", None)
            playback_archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            raw_archive.writestr("manifest.json", json.dumps(raw_manifest, ensure_ascii=False, indent=2))
            raw_archive.writestr("matlab/load_dts_detector_series.m", _matlab_dts_loader_text())
        if output_file.exists():
            output_file.unlink()
        if raw_output_file.exists():
            raw_output_file.unlink()
        temp_output_file.replace(output_file)
        temp_raw_output_file.replace(raw_output_file)
    finally:
        try:
            ray_trace.Close()
        except Exception:
            pass
        try:
            _restore_nce_pose_baseline(object_rows, baseline)
        except Exception:
            pass
        if temp_output_file.exists():
            try:
                temp_output_file.unlink()
            except Exception:
                pass
        if temp_raw_output_file.exists():
            try:
                temp_raw_output_file.unlink()
            except Exception:
                pass

    _mark_zosapi_session_dirty(application, system, False)
    return {
        "system_file": str(getattr(system, "SystemFile", "") or ""),
        "system_name": str(getattr(system, "SystemName", "") or ""),
        "project_file": str(project_file),
        "source_export_folder": str(export_folder),
        "output_file": str(output_file),
        "playback_file": str(output_file),
        "raw_output_file": str(raw_output_file),
        "detector": detector,
        "frame_count": len(frame_entries),
        "frames": frame_entries,
        "cpu_cores": cpu_core_info,
        "zemax_length_unit": zemax_length_unit,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def _sample_multivariate_pose(sigma_list, correlation, sample_count: int, seed: int | None):
    """从多元正态 N(0, Σ) 抽样位移/旋转向量,Σ = D·C·D。

    sigma_list : 各自由度 1σ(长度 6N,顺序为每个镜片的 x,y,z,rx,ry,rz);
    correlation: 相关矩阵 C(6N×6N);None 表示单位阵(各自由度独立,等价于逐项独立高斯);
    返回 (sample_count, 6N) 数组;numpy 不可用时返回 None(由调用方回退)。
    """
    if _np is None:
        return None
    sigma = _np.asarray(sigma_list, dtype=float)
    n = sigma.size
    if correlation is None:
        C = _np.eye(n)
    else:
        C = _np.asarray(correlation, dtype=float)
        if C.shape != (n, n):
            raise ValueError(f"相关矩阵维度应为 {n}x{n},实际为 {tuple(C.shape)};自由度顺序见计算日志。")
        C = 0.5 * (C + C.T)  # 对称化
    cov = (sigma[:, None] * sigma[None, :]) * C  # Σ = D C D
    cov = 0.5 * (cov + cov.T)
    # 投影到最近半正定矩阵(防止用户相关阵非 PSD 或数值误差)
    eigvals, eigvecs = _np.linalg.eigh(cov)
    eigvals = _np.clip(eigvals, 0.0, None)
    cov = (eigvecs * eigvals) @ eigvecs.T
    rng = _np.random.default_rng(seed)
    return rng.multivariate_normal(_np.zeros(n), cov, size=int(sample_count))


def calculate_random_vibration_pose_frames_from_mechanical_exports(
    folder: Path,
    sample_count: int,
    save_dir: Path | None = None,
    *,
    seed: int | None = None,
    save: bool = True,
    correlation=None,
) -> dict[str, object]:
    """以 Mechanical 1σ 位移/旋转为标准差,按多元正态 N(0, Σ) 生成随机振动位移/旋转样本。

    Σ = D·C·D:D 为各自由度 1σ 的对角阵,C 为相关矩阵。
    - 默认 C = 单位阵 → 各镜片/各自由度独立(与逐项独立高斯等价);
    - 若导出文件夹内有 ``pose_correlation.csv``(6N×6N 数值矩阵,DOF 顺序见日志)
      或调用方传入 ``correlation``,则启用相关性(典型来自模态分析)。
    """
    folder = require_file(Path(folder), "Mechanical export folder")
    if not folder.is_dir():
        raise NotADirectoryError(f"Mechanical export path is not a folder: {folder}")
    sample_count = max(1, int(sample_count))
    source_records = read_lens_pose_records(folder)
    if not source_records:
        raise ValueError(
            "Mechanical 导出文件夹中没有读取到可作为随机振动标准差的镜片位移/旋转。"
            "需要包含镜片 UX/UY/UZ 或可导入的位移/旋转数据。"
        )

    dof_names = ["x", "y", "z", "rx", "ry", "rz"]
    sigma_list: list[float] = []
    for source in source_records:
        sigma_list += [abs(float(getattr(source, d))) for d in dof_names]
    dof_count = len(sigma_list)

    # 相关矩阵:调用方传入 > 文件 pose_correlation.csv > 单位阵(独立)
    correlation_source = "单位阵（各镜片/各自由度独立）"
    corr_warnings: list[str] = []
    if correlation is not None:
        correlation_source = "调用方提供的相关矩阵"
    elif _np is not None:
        corr_path = folder / "pose_correlation.csv"
        if corr_path.exists():
            try:
                loaded = _np.loadtxt(corr_path, delimiter=",")
                if loaded.shape == (dof_count, dof_count):
                    correlation = loaded
                    correlation_source = f"相关矩阵文件 {corr_path.name}"
                else:
                    corr_warnings.append(
                        f"发现 {corr_path.name} 但维度 {tuple(loaded.shape)} ≠ {dof_count}×{dof_count}，已忽略，按独立处理。"
                    )
            except Exception as exc:
                corr_warnings.append(f"读取 {corr_path.name} 失败({exc})，已忽略，按独立处理。")

    logs = [
        f"随机振动样本数: {sample_count}",
        f"随机种子: {seed if seed is not None else '系统默认'}",
        "采样分布: 多元正态 N(0, Σ)，Σ = D·C·D（D=各自由度1σ对角阵，C=相关矩阵），均值为 0。",
        f"相关性来源: {correlation_source}",
        "自由度顺序（构造相关矩阵用，行/列一一对应）: "
        + ", ".join(f"{src.name}.{d}" for src in source_records for d in dof_names),
    ]
    logs.extend(corr_warnings)

    samples = _sample_multivariate_pose(sigma_list, correlation, sample_count, seed)
    fallback_rng = random.Random(seed) if samples is None else None
    if samples is None:
        logs.append("注意: numpy 不可用，已回退为逐自由度独立高斯抽样（忽略相关矩阵）。")

    frames: list[LensPoseFrame] = []
    all_records: list[dict[str, object]] = []
    for frame_index in range(1, sample_count + 1):
        records: list[LensPoseRecord] = []
        for lens_index, source in enumerate(source_records):
            if samples is not None:
                base = lens_index * 6
                values = [float(samples[frame_index - 1][base + k]) for k in range(6)]
            else:
                values = [fallback_rng.gauss(0.0, abs(float(getattr(source, d)))) for d in dof_names]
            sampled = LensPoseRecord(
                name=source.name,
                x=values[0],
                y=values[1],
                z=values[2],
                rx=values[3],
                ry=values[4],
                rz=values[5],
                source=f"random sample {frame_index}; std source: {source.source}",
                displacement_unit=source.displacement_unit,
                rotation_unit=source.rotation_unit,
            )
            records.append(sampled)
            all_records.append(
                {
                    **sampled.as_dict(),
                    "frame_index": frame_index,
                    "time_value": None,
                    "time_label": f"random_{frame_index}",
                    "set_index": frame_index,
                    "std_x": abs(float(source.x)),
                    "std_y": abs(float(source.y)),
                    "std_z": abs(float(source.z)),
                    "std_rx": abs(float(source.rx)),
                    "std_ry": abs(float(source.ry)),
                    "std_rz": abs(float(source.rz)),
                    "std_source": source.source,
                }
            )
        frames.append(
            LensPoseFrame(
                frame_index=frame_index,
                time_value=None,
                time_label=f"random_{frame_index}",
                set_index=frame_index,
                records=records,
                source_files=[source.source for source in source_records],
            )
        )

    logs.append(
        "标准差摘要: "
        + "; ".join(
            f"{record.name}: sigma=({abs(record.x):.6g}, {abs(record.y):.6g}, {abs(record.z):.6g}, "
            f"{abs(record.rx):.6g}, {abs(record.ry):.6g}, {abs(record.rz):.6g})"
            for record in source_records[:20]
        )
    )
    saved_paths: dict[str, str] = {}
    if save:
        output_dir = Path(save_dir) if save_dir is not None else folder / "zemax_pose_calculation"
        output_dir.mkdir(parents=True, exist_ok=True)
        saved_paths = _save_pose_time_series_calculation(output_dir, frames, all_records, logs, folder)
        logs.append("随机振动位移/旋转样本已保存: " + ", ".join(saved_paths.values()))
    return {
        "frames": frames,
        "frame_count": len(frames),
        "source_records": source_records,
        "all_records": all_records,
        "logs": logs,
        "saved_paths": saved_paths,
        "source_folder": str(folder),
        "sample_count": sample_count,
        "seed": seed,
    }


def run_zemax_random_vibration_ray_trace(
    export_folder: Path,
    project_file: Path,
    detector_number: int | None = None,
    *,
    sample_count: int = 20,
    output_file: Path | None = None,
    seed: int | None = None,
    cpu_core_count: int | None = None,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    """Generate random vibration pose samples, trace each sample, and save raw detector data to .dts."""
    export_folder = require_file(Path(export_folder), "Mechanical export folder")
    if not export_folder.is_dir():
        raise NotADirectoryError(f"Mechanical export path is not a folder: {export_folder}")
    project_file = require_file(Path(project_file), "Zemax project file")
    sample_count = max(1, int(sample_count))

    if progress_callback is not None:
        progress_callback({"stage": "Zemax 随机振动追迹", "status": f"正在生成 {sample_count} 个随机振动位移/旋转样本"})
    series = calculate_random_vibration_pose_frames_from_mechanical_exports(
        export_folder,
        sample_count,
        seed=seed,
        save=True,
    )
    frames = [frame for frame in series.get("frames") or [] if isinstance(frame, LensPoseFrame)]
    if not frames:
        raise ValueError("没有生成可用于随机振动追迹的位移/旋转样本。")

    if progress_callback is not None:
        progress_callback({"stage": "Zemax 随机振动追迹", "status": f"正在打开 Zemax 工程，随机样本 {len(frames)} 个"})
    application, system = _connect_or_open_selected_opticstudio_system(project_file)
    nce = getattr(system, "NCE", None)
    if nce is None:
        raise RuntimeError("当前 OpticStudio 工程没有非序列组件编辑器 NCE；无法进行随机振动追迹。")
    object_rows = _current_nce_object_rows(nce)
    if not object_rows:
        raise RuntimeError("当前 OpticStudio 非序列工程没有可匹配的 NCE 对象。")
    detectors = _current_detector_rows(nce)
    if not detectors:
        raise RuntimeError("当前 Zemax 非序列工程没有可读取的 Detector。")
    if detector_number is None:
        detector_number = int(detectors[0].get("object_index") or 0)
    detector_number = int(detector_number)
    detector = next((item for item in detectors if int(item.get("object_index") or 0) == detector_number), None)
    if detector is None:
        raise ValueError(f"所选对象 {detector_number} 不是可读取的 Detector。")

    if output_file is None:
        output_dir = project_file.parent / "zemax_random_vibration_results"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = output_dir / f"{project_file.stem}_random_detector_{detector_number:03d}_{timestamp}.dts"
    output_file = Path(output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    raw_output_file = _matlab_raw_dts_path(output_file)
    raw_output_file.parent.mkdir(parents=True, exist_ok=True)
    temp_output_file = output_file.with_name(f"{output_file.name}.tmp")
    temp_raw_output_file = raw_output_file.with_name(f"{raw_output_file.name}.tmp")
    if temp_output_file.exists():
        temp_output_file.unlink()
    if temp_raw_output_file.exists():
        temp_raw_output_file.unlink()

    zemax_length_unit = _current_zemax_length_unit(system)
    if progress_callback is not None:
        progress_callback({"stage": "Zemax 随机振动追迹", "status": "正在读取/创建 Zemax 原始基准位置"})
    baseline = _load_or_create_pose_baseline(project_file, object_rows)
    if progress_callback is not None:
        progress_callback({"stage": "Zemax 随机振动追迹", "status": "正在打开非序列光线追迹工具"})
    ray_trace = system.Tools.OpenNSCRayTrace()
    cpu_core_info = _configure_ray_trace_cpu_cores(ray_trace, cpu_core_count)
    frame_entries: list[dict[str, object]] = []
    unmatched_by_frame: list[dict[str, object]] = []
    started = time.monotonic()

    try:
        with zipfile.ZipFile(temp_output_file, "w", compression=zipfile.ZIP_DEFLATED) as playback_archive, zipfile.ZipFile(
            temp_raw_output_file,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as raw_archive:
            for frame in frames:
                if progress_callback is not None:
                    progress_callback(
                        {
                            "stage": "Zemax 随机振动追迹",
                            "status": f"样本 {frame.frame_index}/{len(frames)}: 写入随机位移/旋转",
                        }
                    )
                matched, unmatched = _apply_pose_records_to_nce(
                    frame.records,
                    object_rows,
                    baseline,
                    zemax_length_unit,
                )
                if not matched:
                    raise ValueError(f"随机样本 {frame.frame_index} 没有任何镜片名称匹配到 Zemax NCE Object Comment。")
                if unmatched:
                    unmatched_by_frame.append(
                        {
                            "frame_index": frame.frame_index,
                            "time_label": frame.time_label,
                            "unmatched": unmatched,
                        }
                    )

                if progress_callback is not None:
                    progress_callback(
                        {
                            "stage": "Zemax 随机振动追迹",
                            "status": f"样本 {frame.frame_index}/{len(frames)}: 清空 Detector 并追迹",
                        }
                    )
                ray_trace.ClearDetectors(0)
                ray_trace.RunAndWaitForCompletion()
                if progress_callback is not None:
                    progress_callback(
                        {
                            "stage": "Zemax 随机振动追迹",
                            "status": f"样本 {frame.frame_index}/{len(frames)}: 读取 Detector 原始矩阵",
                        }
                    )
                detector_result = _read_detector_result_grid(nce, detector_number, max_dimension=None)
                frame_dir = f"frames/{frame.frame_index:04d}"
                detector_csv_path = f"{frame_dir}/detector.csv"
                detector_png_path = f"{frame_dir}/detector.png"
                pose_json_path = f"{frame_dir}/pose.json"
                if progress_callback is not None:
                    progress_callback(
                        {
                            "stage": "Zemax 随机振动追迹",
                            "status": f"样本 {frame.frame_index}/{len(frames)}: 分别写入快速播放PNG和MATLAB原始CSV",
                        }
                    )
                raw_archive.writestr(detector_csv_path, _grid_to_csv_text(detector_result["grid"]))
                playback_archive.writestr(
                    detector_png_path,
                    _false_color_png_bytes(
                        detector_result["grid"],
                        float(detector_result.get("min_value") or 0.0),
                        float(detector_result.get("max_value") or 0.0),
                        log_scale=True,
                    ),
                )
                pose_json_text = json.dumps(
                    {
                        "frame": frame.as_dict(),
                        "matched": matched,
                        "unmatched": unmatched,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                playback_archive.writestr(pose_json_path, pose_json_text)
                raw_archive.writestr(
                    pose_json_path,
                    pose_json_text,
                )
                summary = {key: value for key, value in detector_result.items() if key != "grid"}
                frame_entries.append(
                    {
                        "frame_index": frame.frame_index,
                        "time_value": frame.time_value,
                        "time_label": frame.time_label,
                        "set_index": frame.set_index,
                        "detector_png": detector_png_path,
                        "detector_png_full_resolution": True,
                        "raw_detector_csv": detector_csv_path,
                        "raw_dts_file": str(raw_output_file),
                        "pose_json": pose_json_path,
                        "matched_count": len(matched),
                        "unmatched_count": len(unmatched),
                        "imported_objects": _matched_import_object_summaries(matched),
                        "summary": summary,
                    }
                )

            manifest = {
                "schema": "ansys_zemax_random_vibration_detector_playback_dts_v1",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "file_role": "fast_playback_full_resolution_png",
                "matlab_raw_file": str(raw_output_file),
                "project_file": str(project_file),
                "source_export_folder": str(export_folder),
                "system_file": str(getattr(system, "SystemFile", "") or ""),
                "system_name": str(getattr(system, "SystemName", "") or ""),
                "zemax_length_unit": zemax_length_unit,
                "detector": detector,
                "frame_count": len(frame_entries),
                "frames": frame_entries,
                "cpu_cores": cpu_core_info,
                "random_vibration": {
                    "sample_count": sample_count,
                    "seed": seed,
                    "sigma_source": "calculated Mechanical lens displacement/rotation absolute values",
                },
                "pose_calculation": {
                    "saved_paths": series.get("saved_paths") or {},
                    "logs": series.get("logs") or [],
                },
                "unmatched_by_frame": unmatched_by_frame,
                "playback": {
                    "description": "Fast playback file. Frames contain full-resolution detector PNG images. MATLAB raw detector CSV data is saved in matlab_raw_file.",
                    "full_resolution_png": True,
                },
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
            raw_frame_entries = []
            for frame_entry in frame_entries:
                raw_entry = dict(frame_entry)
                raw_entry.pop("detector_png", None)
                raw_entry.pop("detector_png_full_resolution", None)
                raw_entry.pop("raw_dts_file", None)
                raw_entry["detector_csv"] = raw_entry.pop("raw_detector_csv", "")
                raw_frame_entries.append(raw_entry)
            raw_manifest = {
                **manifest,
                "schema": "ansys_zemax_random_vibration_detector_matlab_raw_dts_v1",
                "file_role": "matlab_raw_detector_csv",
                "playback_file": str(output_file),
                "frames": raw_frame_entries,
                "matlab": {
                    "description": "This .dts file is a ZIP container. In MATLAB: unzip('file.dts','out'); manifest=jsondecode(fileread(fullfile('out','manifest.json'))); data=readmatrix(fullfile('out',manifest.frames(1).detector_csv));",
                    "loader": "matlab/load_dts_detector_series.m",
                },
            }
            raw_manifest.pop("playback", None)
            playback_archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            raw_archive.writestr("manifest.json", json.dumps(raw_manifest, ensure_ascii=False, indent=2))
            raw_archive.writestr("matlab/load_dts_detector_series.m", _matlab_dts_loader_text())
        if output_file.exists():
            output_file.unlink()
        if raw_output_file.exists():
            raw_output_file.unlink()
        temp_output_file.replace(output_file)
        temp_raw_output_file.replace(raw_output_file)
    finally:
        try:
            ray_trace.Close()
        except Exception:
            pass
        try:
            _restore_nce_pose_baseline(object_rows, baseline)
        except Exception:
            pass
        if temp_output_file.exists():
            try:
                temp_output_file.unlink()
            except Exception:
                pass
        if temp_raw_output_file.exists():
            try:
                temp_raw_output_file.unlink()
            except Exception:
                pass

    _mark_zosapi_session_dirty(application, system, False)
    return {
        "system_file": str(getattr(system, "SystemFile", "") or ""),
        "system_name": str(getattr(system, "SystemName", "") or ""),
        "project_file": str(project_file),
        "source_export_folder": str(export_folder),
        "output_file": str(output_file),
        "playback_file": str(output_file),
        "raw_output_file": str(raw_output_file),
        "detector": detector,
        "frame_count": len(frame_entries),
        "frames": frame_entries,
        "cpu_cores": cpu_core_info,
        "zemax_length_unit": zemax_length_unit,
        "sample_count": sample_count,
        "seed": seed,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def _read_solution_summary(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    sections: dict[str, dict[str, str]] = {}
    current_name = ""
    for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current_name = line[1:-1].strip()
            sections[current_name] = {}
            continue
        if current_name and ":" in line:
            key, value = line.split(":", 1)
            sections[current_name][_normalize_key(key)] = value.strip()
    return sections


def _read_mechanical_export_table(path: Path) -> dict[str, object] | None:
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        if line.strip()
    ]
    if len(lines) < 2:
        return None
    delimiter = _detect_delimiter("\n".join(lines[:5]))
    if delimiter == "whitespace":
        rows = [re.split(r"\s+", line) for line in lines]
    else:
        rows = list(csv.reader(lines, delimiter=delimiter))
    if len(rows) < 2:
        return None

    header = [str(value).strip() for value in rows[0]]
    normalized = [_normalize_key(value) for value in header]
    if not any("node" in key for key in normalized):
        return None

    node_key = _first_matching_key(normalized, ["node_number", "node", "node_id", "nodeid"])
    if node_key is None:
        return None
    node_index = normalized.index(node_key)

    data_rows: list[dict[str, float]] = []
    vector_column_index = -1
    for vector_key in ["vector_deformation", "uvectors", "u_vectors", "deformation_vector"]:
        if vector_key in normalized:
            vector_column_index = normalized.index(vector_key)
            break
    for row in rows[1:]:
        if len(row) <= node_index:
            continue
        node_text = str(row[node_index]).strip()
        if not node_text:
            continue
        try:
            node_id = int(float(node_text))
        except ValueError:
            continue
        item: dict[str, float] = {"node": float(node_id)}
        for column_index, key in enumerate(normalized):
            if column_index >= len(row) or column_index == node_index:
                continue
            if not key:
                continue
            item[key] = _number(row[column_index])
        if vector_column_index >= 0 and len(row) > vector_column_index + 2:
            item["ux"] = _number(row[vector_column_index])
            item["uy"] = _number(row[vector_column_index + 1])
            item["uz"] = _number(row[vector_column_index + 2])
        data_rows.append(item)
    if not data_rows:
        return None
    has_vector_components = vector_column_index >= 0 and all(
        "ux" in row and "uy" in row and "uz" in row
        for row in data_rows
    )
    return {
        "path": path,
        "header": header,
        "normalized_header": normalized,
        "rows": data_rows,
        "has_vector_components": has_vector_components,
    }


def _result_name_from_export_file(path: Path) -> str:
    name = path.stem
    name = re.sub(r"^\d+_", "", name)
    name = re.sub(r"_set_\d+.*$", "", name, flags=re.IGNORECASE)
    return name.strip()


def _component_from_table(table: dict[str, object], section: dict[str, str], path: Path) -> str:
    expression = str(section.get("expression") or "").strip().lower()
    if expression in {"ux", "uy", "uz"}:
        return expression
    if bool(table.get("has_vector_components")) or expression in {"uvectors", "u_vector", "u_vectors", "vector"}:
        return "vector"

    keys = list(table.get("normalized_header") or [])
    for component in ["ux", "uy", "uz"]:
        if component in keys:
            return component
    joined = " ".join(keys + [_normalize_key(path.stem)])
    if "directional_deformation" in joined or "directional" in joined:
        clean_name = _normalize_key(path.stem)
        spaced_name = " " + clean_name.replace("_", " ") + " "
        for token, component in [("ux", "ux"), ("uy", "uy"), ("uz", "uz"), (" x ", "ux"), (" y ", "uy"), (" z ", "uz")]:
            if clean_name.endswith("_" + token.strip()) or token in spaced_name:
                return component
    return ""


def _lens_name_from_result(result_name: str, section: dict[str, str], component: str) -> str:
    name = str(section.get("identifier") or "").strip() or result_name
    name = re.sub(r"(?i)(?:^|[_\-\s])u?[xyz]$", "", name).strip("_- ")
    if component == "vector":
        stripped = re.sub(r"(?i)(?:^|[_\-\s])u?_?vectors?$", "", name).strip("_- ")
        name = stripped or name
    elif component:
        name = re.sub(rf"(?i)(?:^|[_\-\s]){component}$", "", name).strip("_- ")
        name = re.sub(rf"(?i)(?:^|[_\-\s]){component[1]}$", "", name).strip("_- ")
    return name or result_name


def _time_frame_info_from_export_file(path: Path) -> dict[str, object]:
    stem = re.sub(r"^\d+_", "", path.stem)
    match = re.search(r"_set_(\d+)(?:_(.*))?$", stem, flags=re.IGNORECASE)
    if not match:
        return {
            "key": ["single"],
            "set_index": None,
            "time_value": None,
            "time_sort": None,
            "time_label": "single",
        }
    set_index = int(match.group(1))
    raw_time = str(match.group(2) or "").strip("_ ")
    time_label = raw_time.replace("_", " ").strip() or f"set {set_index}"
    time_value: float | None = None
    time_match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", time_label)
    if time_match:
        time_value = float(time_match.group(0))
    return {
        "key": ["set", set_index, time_label],
        "set_index": set_index,
        "time_value": time_value,
        "time_sort": time_value,
        "time_label": time_label,
    }


def _calculate_lens_pose_from_components(
    lens_name: str,
    components: dict[str, dict[str, object]],
    logs: list[str],
    *,
    context: str = "",
) -> tuple[LensPoseRecord, dict[str, object]] | None:
    label = f"{lens_name} ({context})" if context else lens_name
    missing = [component for component in ["ux", "uy", "uz"] if component not in components]
    if missing:
        logs.append(
            f"{label}: 缺少 {', '.join(m.upper() for m in missing)}，"
            "只能读取到部分方向位移，无法计算完整位移/旋转。"
        )
        return None

    merged_nodes = _merge_component_tables(components)
    if not merged_nodes:
        logs.append(f"{label}: 三个方向位移文件没有共同节点，无法计算。")
        return None

    has_coords = all("x" in node and "y" in node and "z" in node for node in merged_nodes)
    translation = _average_translation(merged_nodes)
    rotation_rad = (0.0, 0.0, 0.0)
    rms_residual: float | None = None
    rotation_status = "not_computed"
    if has_coords:
        rotation_rad, rms_residual, rotation_status = _fit_rigid_body_rotation(
            merged_nodes, translation
        )
    else:
        logs.append(
            f"{label}: 已计算平均位移，但导出 TXT 没有节点原始坐标，"
            "无法拟合旋转。需要导出含节点坐标 X/Y/Z 和 UX/UY/UZ 的原始数据。"
        )

    source_files = list(dict.fromkeys(str(components[key]["path"].name) for key in ["ux", "uy", "uz"]))
    record = LensPoseRecord(
        name=lens_name,
        x=translation[0],
        y=translation[1],
        z=translation[2],
        rx=math.degrees(rotation_rad[0]),
        ry=math.degrees(rotation_rad[1]),
        rz=math.degrees(rotation_rad[2]),
        source=", ".join(source_files),
        displacement_unit="m",
        rotation_unit="deg",
        rms_residual=rms_residual,
    )
    all_record = {
        **record.as_dict(),
        "node_count": len(merged_nodes),
        "rotation_status": rotation_status,
    }
    residual_text = (
        f"; 面形残差RMS={rms_residual:.3g} m" if rms_residual is not None else ""
    )
    logs.append(
        f"{label}: 节点 {len(merged_nodes)} 个，质心平移(偏心/离焦) "
        f"X={record.x:.6g}, Y={record.y:.6g}, Z={record.z:.6g} m; "
        f"倾斜 Rx={record.rx:.6g}, Ry={record.ry:.6g}, Rz={record.rz:.6g} deg "
        f"({rotation_status}){residual_text}"
    )
    return record, all_record


def _merge_component_tables(components: dict[str, dict[str, object]]) -> list[dict[str, float]]:
    indexed: dict[str, dict[int, dict[str, float]]] = {}
    for component, payload in components.items():
        table = payload["table"]
        rows = table.get("rows") or []
        indexed[component] = {int(row["node"]): row for row in rows if isinstance(row, dict) and "node" in row}
    common_nodes = set(indexed["ux"]).intersection(indexed["uy"], indexed["uz"])
    merged: list[dict[str, float]] = []
    for node_id in sorted(common_nodes):
        ux_row = indexed["ux"][node_id]
        uy_row = indexed["uy"][node_id]
        uz_row = indexed["uz"][node_id]
        item = {
            "node": float(node_id),
            "ux": _component_value(ux_row, "ux"),
            "uy": _component_value(uy_row, "uy"),
            "uz": _component_value(uz_row, "uz"),
        }
        coords = _node_coordinates(ux_row) or _node_coordinates(uy_row) or _node_coordinates(uz_row)
        if coords is not None:
            item.update({"x": coords[0], "y": coords[1], "z": coords[2]})
        merged.append(item)
    return merged


def _component_value(row: dict[str, float], component: str) -> float:
    if component in row:
        return float(row[component])
    candidates = [
        component,
        component[-1],
        f"directional_deformation_{component[-1]}",
        "directional_deformation",
        "deformation",
        "value",
    ]
    for key in candidates:
        if key in row:
            return float(row[key])
    # Mechanical exported two-column Directional Deformation headers normalize
    # to a long key; after node id, the first numeric non-coordinate value is the result.
    for key, value in row.items():
        if key in {"node", "x", "y", "z", "x_location", "y_location", "z_location", "x_coordinate", "y_coordinate", "z_coordinate"}:
            continue
        return float(value)
    return 0.0


def _node_coordinates(row: dict[str, float]) -> tuple[float, float, float] | None:
    triples = [
        ("x", "y", "z"),
        ("x_location", "y_location", "z_location"),
        ("x_coordinate", "y_coordinate", "z_coordinate"),
        ("coordinate_x", "coordinate_y", "coordinate_z"),
    ]
    for keys in triples:
        if all(key in row for key in keys):
            return (float(row[keys[0]]), float(row[keys[1]]), float(row[keys[2]]))
    return None


def _average_translation(nodes: list[dict[str, float]]) -> tuple[float, float, float]:
    count = max(len(nodes), 1)
    return (
        sum(node["ux"] for node in nodes) / count,
        sum(node["uy"] for node in nodes) / count,
        sum(node["uz"] for node in nodes) / count,
    )


def _fit_rigid_body_rotation(
    nodes: list[dict[str, float]],
    translation: tuple[float, float, float],
) -> tuple[tuple[float, float, float], float | None, str]:
    """用 SVD/Kabsch 严格刚体配准拟合绕质心的旋转。

    返回 ``(旋转向量[rad], 面形残差RMS[m] 或 None, 状态)``:

    - 平移沿用质心位移(节点位移均值,见 :func:`_average_translation`),即光学偏心/离焦;
      旋转以质心为中心,用旋转矩阵的对数映射(log map)表示为旋转向量,小角度时与各轴小转角一致。
    - 残差RMS 为去除最佳刚体平移+旋转后各节点的剩余位移 RMS,表征非刚体面形误差。
    - Kabsch 解带反射保护(det 修正),保证得到正交旋转(det=+1)而非镜像。
    - ``numpy`` 不可用或求解失败时回退到线性化小角度最小二乘解,状态记为 ``computed_small_angle``。
    """

    if _np is None:
        return _fit_small_rotation(nodes, translation), None, "computed_small_angle"
    try:
        p = _np.asarray([[n["x"], n["y"], n["z"]] for n in nodes], dtype=float)
        u = _np.asarray([[n["ux"], n["uy"], n["uz"]] for n in nodes], dtype=float)
        if p.shape[0] < 3:
            # 少于 3 个非共线点无法唯一确定旋转,回退小角度近似
            return _fit_small_rotation(nodes, translation), None, "computed_small_angle"
        q = p + u
        centroid_p = p.mean(axis=0)
        centroid_q = q.mean(axis=0)
        p_centered = p - centroid_p
        q_centered = q - centroid_q
        covariance = p_centered.T @ q_centered
        u_svd, _s, vt = _np.linalg.svd(covariance)
        reflection = 1.0 if _np.linalg.det(vt.T @ u_svd.T) >= 0.0 else -1.0
        rotation = vt.T @ _np.diag([1.0, 1.0, reflection]) @ u_svd.T
        rotvec = _rotation_matrix_to_rotation_vector(rotation)
        residual = q_centered - p_centered @ rotation.T
        rms = float(_np.sqrt((residual ** 2).sum(axis=1).mean()))
        return (
            (float(rotvec[0]), float(rotvec[1]), float(rotvec[2])),
            rms,
            "computed_kabsch",
        )
    except Exception:
        # 任何数值异常都安全回退到纯 Python 小角度解
        return _fit_small_rotation(nodes, translation), None, "computed_small_angle"


def _rotation_matrix_to_rotation_vector(rotation: "Any") -> "Any":
    """旋转矩阵 -> 旋转向量(轴 * 角,rad)。小角度时等于反对称部分。"""

    cos_theta = max(-1.0, min(1.0, (float(_np.trace(rotation)) - 1.0) / 2.0))
    theta = math.acos(cos_theta)
    antisymmetric = _np.array([
        rotation[2, 1] - rotation[1, 2],
        rotation[0, 2] - rotation[2, 0],
        rotation[1, 0] - rotation[0, 1],
    ])
    if theta < 1e-9:
        # 极小转角:log map 退化为反对称部分的一半
        return antisymmetric / 2.0
    axis_norm = float(_np.linalg.norm(antisymmetric))
    if axis_norm < 1e-12:
        # theta 接近 pi 的退化情形,从 (R + I)/2 的主对角提取轴
        diagonal = _np.clip((_np.diag(rotation) + 1.0) / 2.0, 0.0, 1.0)
        axis = _np.sqrt(diagonal)
        return axis * theta
    return antisymmetric / axis_norm * theta


def _fit_small_rotation(
    nodes: list[dict[str, float]],
    translation: tuple[float, float, float],
) -> tuple[float, float, float]:
    cx = sum(node["x"] for node in nodes) / len(nodes)
    cy = sum(node["y"] for node in nodes) / len(nodes)
    cz = sum(node["z"] for node in nodes) / len(nodes)
    ata = [[0.0, 0.0, 0.0] for _ in range(3)]
    atb = [0.0, 0.0, 0.0]
    for node in nodes:
        x = node["x"] - cx
        y = node["y"] - cy
        z = node["z"] - cz
        residual = [
            node["ux"] - translation[0],
            node["uy"] - translation[1],
            node["uz"] - translation[2],
        ]
        rows = [
            [0.0, z, -y],
            [-z, 0.0, x],
            [y, -x, 0.0],
        ]
        for row, value in zip(rows, residual):
            for i in range(3):
                atb[i] += row[i] * value
                for j in range(3):
                    ata[i][j] += row[i] * row[j]
    return tuple(_solve_3x3(ata, atb))


def _solve_3x3(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    augmented = [matrix[row][:] + [rhs[row]] for row in range(3)]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-30:
            return [0.0, 0.0, 0.0]
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        pivot_value = augmented[column][column]
        for item in range(column, 4):
            augmented[column][item] /= pivot_value
        for row in range(3):
            if row == column:
                continue
            factor = augmented[row][column]
            for item in range(column, 4):
                augmented[row][item] -= factor * augmented[column][item]
    return [augmented[row][3] for row in range(3)]


def _first_matching_key(keys: list[str], candidates: list[str]) -> str | None:
    for candidate in candidates:
        if candidate in keys:
            return candidate
    for key in keys:
        if "node" in key:
            return key
    return None


def _save_pose_calculation(
    output_dir: Path,
    records: list[dict[str, object]],
    logs: list[str],
    source_folder: Path,
) -> dict[str, str]:
    csv_path = output_dir / "lens_pose_latest.csv"
    json_path = output_dir / "lens_pose_calculation.json"
    log_path = output_dir / "lens_pose_calculation.log"

    fieldnames = [
        "name",
        "x",
        "y",
        "z",
        "rx",
        "ry",
        "rz",
        "rms_residual",
        "displacement_unit",
        "rotation_unit",
        "node_count",
        "rotation_status",
        "source",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow({field: record.get(field, "") for field in fieldnames})

    payload = {
        "source_folder": str(source_folder),
        "records": records,
        "logs": logs,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log_path.write_text("\n".join(logs) + "\n", encoding="utf-8")
    return {"csv": str(csv_path), "json": str(json_path), "log": str(log_path)}


def _save_pose_time_series_calculation(
    output_dir: Path,
    frames: list[LensPoseFrame],
    records: list[dict[str, object]],
    logs: list[str],
    source_folder: Path,
) -> dict[str, str]:
    csv_path = output_dir / "lens_pose_time_series.csv"
    json_path = output_dir / "lens_pose_time_series.json"
    log_path = output_dir / "lens_pose_time_series.log"

    fieldnames = [
        "frame_index",
        "time_value",
        "time_label",
        "set_index",
        "name",
        "x",
        "y",
        "z",
        "rx",
        "ry",
        "rz",
        "rms_residual",
        "displacement_unit",
        "rotation_unit",
        "node_count",
        "rotation_status",
        "source",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow({field: record.get(field, "") for field in fieldnames})

    payload = {
        "schema": "zemax_pose_time_series_v1",
        "source_folder": str(source_folder),
        "frame_count": len(frames),
        "frames": [frame.as_dict() for frame in frames],
        "records": records,
        "logs": logs,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log_path.write_text("\n".join(logs) + "\n", encoding="utf-8")
    return {"csv": str(csv_path), "json": str(json_path), "log": str(log_path)}


def _records_from_json(path: Path) -> list[LensPoseRecord]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        if isinstance(payload.get("records"), list):
            rows = payload["records"]
        elif isinstance(payload.get("poses"), list):
            rows = payload["poses"]
        else:
            rows = [
                {"name": name, **values}
                for name, values in payload.items()
                if isinstance(values, dict)
            ]
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    return [
        record
        for index, row in enumerate(rows, start=1)
        if isinstance(row, dict)
        for record in [_record_from_mapping(row, f"{path.name}:{index}")]
        if record is not None
    ]


def _records_from_text_table(path: Path) -> list[LensPoseRecord]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    meaningful = [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "//", ";"))
    ]
    if not meaningful:
        return []

    sample = "\n".join(meaningful[:10])
    delimiter = _detect_delimiter(sample)
    if delimiter == "whitespace":
        rows = [re.split(r"\s+", line.strip()) for line in meaningful]
    else:
        rows = list(csv.reader(meaningful, delimiter=delimiter))
    if not rows:
        return []

    header = [_normalize_key(value) for value in rows[0]]
    if "node_number" in header or "node" in header and any("location" in key or "deformation" in key for key in header):
        return []
    has_header = any(key in _HEADER_ALIASES for key in header)
    records: list[LensPoseRecord] = []
    if has_header:
        for index, row in enumerate(rows[1:], start=2):
            mapping = {header[i]: row[i] for i in range(min(len(header), len(row)))}
            record = _record_from_mapping(mapping, f"{path.name}:{index}")
            if record is not None:
                records.append(record)
    else:
        for index, row in enumerate(rows, start=1):
            if len(row) >= 7:
                name = str(row[0]).strip()
                values = row[1:7]
            elif len(row) == 6:
                name = path.stem
                values = row[:6]
            else:
                continue
            record = LensPoseRecord(
                name=name,
                x=_number(values[0]),
                y=_number(values[1]),
                z=_number(values[2]),
                rx=_number(values[3]),
                ry=_number(values[4]),
                rz=_number(values[5]),
                source=f"{path.name}:{index}",
            )
            records.append(record)
    return records


def _read_zmx_text(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        encoding = "utf-16"
    elif data.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    else:
        encoding = "utf-8"
    return data.decode(encoding, errors="replace"), encoding


_HEADER_ALIASES = {
    "name",
    "comment",
    "named_selection",
    "namedselection",
    "selection",
    "object",
    "lens",
    "x",
    "y",
    "z",
    "dx",
    "dy",
    "dz",
    "ux",
    "uy",
    "uz",
    "rx",
    "ry",
    "rz",
    "tx",
    "ty",
    "tz",
    "translation_x",
    "translation_y",
    "translation_z",
    "position_x",
    "position_y",
    "position_z",
    "x_position",
    "y_position",
    "z_position",
    "delta_x",
    "delta_y",
    "delta_z",
    "displacement_x",
    "displacement_y",
    "displacement_z",
    "tilt_x",
    "tilt_y",
    "tilt_z",
    "rotation_x",
    "rotation_y",
    "rotation_z",
    "theta_x",
    "theta_y",
    "theta_z",
    "dtheta_x",
    "dtheta_y",
    "dtheta_z",
    "rot_x",
    "rot_y",
    "rot_z",
}


def _record_from_mapping(row: dict[str, Any], source: str) -> LensPoseRecord | None:
    normalized = {_normalize_key(str(key)): value for key, value in row.items()}
    name = _first_text(
        normalized,
        ["name", "comment", "named_selection", "namedselection", "selection", "object", "lens"],
    )
    if not name:
        return None
    return LensPoseRecord(
        name=name,
        x=_first_number(
            normalized,
            ["dx", "x", "ux", "translation_x", "position_x", "x_position", "delta_x", "displacement_x"],
        ),
        y=_first_number(
            normalized,
            ["dy", "y", "uy", "translation_y", "position_y", "y_position", "delta_y", "displacement_y"],
        ),
        z=_first_number(
            normalized,
            ["dz", "z", "uz", "translation_z", "position_z", "z_position", "delta_z", "displacement_z"],
        ),
        rx=_first_number(
            normalized,
            ["rx", "tx", "tilt_x", "rotation_x", "theta_x", "dtheta_x", "rot_x"],
        ),
        ry=_first_number(
            normalized,
            ["ry", "ty", "tilt_y", "rotation_y", "theta_y", "dtheta_y", "rot_y"],
        ),
        rz=_first_number(
            normalized,
            ["rz", "tz", "tilt_z", "rotation_z", "theta_z", "dtheta_z", "rot_z"],
        ),
        displacement_unit=_first_text(
            normalized,
            ["displacement_unit", "length_unit", "position_unit", "linear_unit", "unit", "units"],
        )
        or "m",
        rotation_unit=_first_text(
            normalized,
            ["rotation_unit", "angle_unit", "tilt_unit", "angular_unit"],
        )
        or "deg",
        source=source,
    )


def _detect_delimiter(sample: str) -> str:
    counts = {delimiter: sample.count(delimiter) for delimiter in [",", "\t", ";"]}
    delimiter, count = max(counts.items(), key=lambda item: item[1])
    return delimiter if count > 0 else "whitespace"


def _normalize_key(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[\[\(（].*?[\]\)）]", "", value)
    value = value.replace("命名选择", "named_selection").replace("镜片", "lens")
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "_", value)
    return value.strip("_")


def _normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).strip()).lower()


def _first_text(row: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip().strip('"')
    return ""


def _first_number(row: dict[str, Any], keys: list[str]) -> float:
    for key in keys:
        if key in row:
            return _number(row[key])
    return 0.0


def _number(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", text)
    return float(match.group(0)) if match else 0.0


def _find_zmx_nsc_object_blocks(lines: list[str]) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    object_index = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("NSOH "):
            if current is not None:
                blocks.append(current)
            object_index += 1
            parts = stripped.split(maxsplit=4)
            current = {
                "object_index": object_index,
                "nsoh_index": index,
                "comment": parts[4].strip().strip('"') if len(parts) >= 5 else "",
                "nsop_index": None,
            }
        elif current is not None and stripped.startswith("NSOP ") and current.get("nsop_index") is None:
            current["nsop_index"] = index
    if current is not None:
        blocks.append(current)
    return [block for block in blocks if block.get("comment") and block.get("nsop_index") is not None]


def _match_object_blocks(name: str, blocks: list[dict[str, object]]) -> list[dict[str, object]]:
    target = _normalize_name(name)
    return [block for block in blocks if _normalize_name(str(block.get("comment") or "")) == target]


def _pose_baseline_path(project_file: Path) -> Path:
    return project_file.with_name(f"{project_file.stem}_mechanical_pose_baseline.json")


def _load_or_create_pose_baseline(
    project_file: Path | None,
    object_rows: list[dict[str, object]],
) -> dict[str, object]:
    if project_file is None:
        return _pose_baseline_from_nce_rows(object_rows, source="current_background_session", path="")

    project_file = Path(project_file)
    baseline_path = _pose_baseline_path(project_file)
    if baseline_path.exists():
        loaded = _read_pose_baseline_file(baseline_path)
        if loaded is not None:
            loaded["created"] = False
            return loaded

    candidate = _find_zmx_baseline_candidate(project_file)
    if candidate is not None:
        baseline = _pose_baseline_from_zmx(candidate, path=str(baseline_path))
        baseline["source"] = str(candidate)
    else:
        baseline = _pose_baseline_from_nce_rows(object_rows, source=str(project_file), path=str(baseline_path))

    baseline["project_file"] = str(project_file)
    baseline["created"] = True
    try:
        baseline_path.write_text(
            json.dumps(_serializable_pose_baseline(baseline), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        baseline["write_error"] = str(exc)
    return baseline


def _read_pose_baseline_file(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    entries = data.get("entries")
    if not isinstance(entries, list):
        return None
    normalized_entries = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        values = entry.get("values")
        if not isinstance(values, list) or len(values) != 6:
            continue
        try:
            normalized_entries.append(
                {
                    "object_index": int(entry.get("object_index") or 0),
                    "comment": str(entry.get("comment") or ""),
                    "normalized_comment": _normalize_name(str(entry.get("comment") or "")),
                    "values": [float(value) for value in values],
                    "source": str(entry.get("source") or data.get("source") or path),
                }
            )
        except Exception:
            continue
    if not normalized_entries:
        return None
    data["entries"] = normalized_entries
    data["path"] = str(path)
    data.setdefault("source", str(path))
    return data


def _serializable_pose_baseline(baseline: dict[str, object]) -> dict[str, object]:
    return {
        "schema": 1,
        "project_file": str(baseline.get("project_file") or ""),
        "source": str(baseline.get("source") or ""),
        "entries": [
            {
                "object_index": int(entry.get("object_index") or 0),
                "comment": str(entry.get("comment") or ""),
                "values": [float(value) for value in entry.get("values") or []],
                "source": str(entry.get("source") or baseline.get("source") or ""),
            }
            for entry in baseline.get("entries") or []
            if isinstance(entry, dict)
        ],
    }


def _find_zmx_baseline_candidate(project_file: Path) -> Path | None:
    if project_file.suffix.lower() != ".zmx" or not project_file.parent.exists():
        return None
    stem = project_file.stem.lower()
    tokens = ("baseline", "before", "backup", "bak", "origin", "original")
    candidates: list[Path] = []
    for candidate in project_file.parent.glob(f"{project_file.stem}*.zmx"):
        if _same_existing_file(candidate, project_file):
            continue
        lower_name = candidate.stem.lower()
        if lower_name.startswith(stem) and any(token in lower_name for token in tokens):
            candidates.append(candidate)
    if not candidates:
        return None
    candidates.sort(key=lambda path: path.stat().st_mtime)
    return candidates[0]


def _pose_baseline_from_zmx(source_path: Path, *, path: str = "") -> dict[str, object]:
    text, _encoding = _read_zmx_text(source_path)
    lines = text.splitlines()
    entries: list[dict[str, object]] = []
    for block in _find_zmx_nsc_object_blocks(lines):
        current = _parse_nsop_line(lines[int(block["nsop_index"])])
        if current is None:
            continue
        comment = str(block.get("comment") or "")
        entries.append(
            {
                "object_index": int(block.get("object_index") or 0),
                "comment": comment,
                "normalized_comment": _normalize_name(comment),
                "values": [float(value) for value in current["values"]],
                "source": str(source_path),
            }
        )
    return {
        "schema": 1,
        "path": path,
        "source": str(source_path),
        "entries": entries,
    }


def _pose_baseline_from_nce_rows(
    rows: list[dict[str, object]],
    *,
    source: str,
    path: str,
) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    for row in rows:
        values = row.get("values")
        if not isinstance(values, list) or len(values) != 6:
            obj = row.get("object")
            if obj is None:
                continue
            values = _nce_object_pose_values(obj)
        comment = str(row.get("comment") or "")
        entries.append(
            {
                "object_index": int(row.get("object_index") or 0),
                "comment": comment,
                "normalized_comment": _normalize_name(comment),
                "values": [float(value) for value in values],
                "source": source,
            }
        )
    return {
        "schema": 1,
        "path": path,
        "source": source,
        "entries": entries,
    }


def _pose_baseline_entry_for_object(
    baseline: dict[str, object],
    item: dict[str, object],
) -> dict[str, object]:
    entries = [entry for entry in baseline.get("entries") or [] if isinstance(entry, dict)]
    object_index = int(item.get("object_index") or 0)
    comment = str(item.get("comment") or "")
    normalized_comment = _normalize_name(comment)

    for entry in entries:
        if int(entry.get("object_index") or 0) == object_index:
            return entry

    comment_matches = [
        entry
        for entry in entries
        if str(entry.get("normalized_comment") or _normalize_name(str(entry.get("comment") or ""))) == normalized_comment
    ]
    if len(comment_matches) == 1:
        return comment_matches[0]

    return {
        "object_index": object_index,
        "comment": comment,
        "values": item.get("values") or [],
        "source": baseline.get("source") or "",
        "missing": True,
    }


def _connect_or_open_selected_opticstudio_system(
    project_file: Path | None,
    *,
    open_if_missing: bool = True,
) -> tuple[object, object]:
    managed = _find_managed_zosapi_project(project_file)
    if managed is not None:
        return managed

    connection_errors: list[str] = []

    if project_file is not None and open_if_missing:
        try:
            return _open_or_reuse_zosapi_project(project_file)
        except Exception as exc:
            connection_errors.append(str(exc))

    detail = "\n".join(item for item in connection_errors if item)
    raise RuntimeError(
        "当前没有可连接的 Zemax 后台 ZOS-API 工程。\n"
        "请先在 Zemax 选项卡选择工程文件，然后执行导入操作；程序会在后台打开所选工程，不打开 GUI。\n\n"
        f"连接信息:\n{detail}"
    )


def _open_or_reuse_zosapi_project(project_file: Path) -> tuple[object, object]:
    project_file = require_file(project_file, "Zemax project file")
    managed = _find_managed_zosapi_project(project_file)
    if managed is not None:
        return managed

    ZOSAPI = _load_zosapi()
    connection = ZOSAPI.ZOSAPI_Connection()
    application = connection.CreateNewApplication()
    if application is None:
        raise RuntimeError("CreateNewApplication 返回空对象，无法启动可控 OpticStudio。")
    try:
        if not bool(getattr(application, "IsValidLicenseForAPI", False)):
            raise RuntimeError(f"当前 OpticStudio 许可不能使用 ZOS-API: {getattr(application, 'LicenseStatus', '')}")
        _show_changes_in_ui(application, True)
        system = getattr(application, "PrimarySystem", None)
        if system is None:
            raise RuntimeError("无法获取 OpticStudio PrimarySystem。")
        system.LoadFile(str(project_file), False)
        _show_changes_in_ui(application, True)
    except Exception:
        try:
            application.CloseApplication()
        except Exception:
            pass
        raise

    _remember_zosapi_session(connection, application, system, project_file)
    return application, system


def _find_managed_zosapi_project(project_file: Path | None) -> tuple[object, object] | None:
    for session in list(_ZOSAPI_SESSIONS):
        application = session.get("application")
        system = _valid_current_zos_application(application)
        if system is None:
            try:
                _ZOSAPI_SESSIONS.remove(session)
            except ValueError:
                pass
            continue
        if project_file is None:
            session["system"] = system
            return application, system
        system_file = str(getattr(system, "SystemFile", "") or "")
        remembered_file = session.get("project_file")
        if _same_existing_file(system_file, project_file) or (
            remembered_file is not None and _same_existing_file(str(remembered_file), project_file)
        ):
            session["system"] = system
            return application, system
    return None


def _matching_zosapi_sessions(project_file: Path | None) -> list[dict[str, object]]:
    matches: list[dict[str, object]] = []
    for session in list(_ZOSAPI_SESSIONS):
        application = session.get("application")
        system = _valid_current_zos_application(application)
        if system is None:
            _forget_zosapi_session(session)
            continue
        if project_file is None:
            matches.append(session)
            continue
        system_file = str(getattr(system, "SystemFile", "") or "")
        remembered_file = session.get("project_file")
        if _same_existing_file(system_file, project_file) or (
            remembered_file is not None and _same_existing_file(str(remembered_file), project_file)
        ):
            matches.append(session)
    return matches


def _remember_zosapi_session(
    connection: object | None,
    application: object,
    system: object,
    project_file: Path | None,
) -> None:
    for session in list(_ZOSAPI_SESSIONS):
        if session.get("application") is application:
            session.update({"connection": connection, "system": system, "project_file": project_file})
            session.setdefault("dirty", False)
            return
    _ZOSAPI_SESSIONS.append(
        {
            "connection": connection,
            "application": application,
            "system": system,
            "project_file": project_file,
            "dirty": False,
        }
    )


def _mark_zosapi_session_dirty(application: object, system: object, dirty: bool) -> None:
    for session in list(_ZOSAPI_SESSIONS):
        if session.get("application") is application:
            session["system"] = system
            session["dirty"] = bool(dirty)
            return
    system_file = str(getattr(system, "SystemFile", "") or "")
    _remember_zosapi_session(None, application, system, Path(system_file) if system_file else None)
    if _ZOSAPI_SESSIONS:
        _ZOSAPI_SESSIONS[-1]["dirty"] = bool(dirty)


def _forget_zosapi_session(session: dict[str, object]) -> None:
    try:
        _ZOSAPI_SESSIONS.remove(session)
    except ValueError:
        pass


def _opticstudio_session_info(
    application: object,
    system: object,
    project_file: Path | None,
    launch_method: str,
) -> dict[str, object]:
    nce = getattr(system, "NCE", None)
    object_count = int(getattr(nce, "NumberOfObjects", 0) or 0) if nce is not None else 0
    return {
        "pid": int(getattr(application, "ClientProcess", 0) or 0),
        "opticstudio_instance": int(getattr(application, "OpticStudioInstance", 0) or 0),
        "project_file": str(project_file or ""),
        "system_file": str(getattr(system, "SystemFile", "") or ""),
        "system_name": str(getattr(system, "SystemName", "") or ""),
        "application_mode": str(getattr(application, "Mode", "") or ""),
        "object_count": object_count,
        "api_connected": True,
        "launch_method": launch_method,
    }


def _show_changes_in_ui(application: object, enabled: bool) -> None:
    try:
        setattr(application, "ShowChangesInUI", bool(enabled))
    except Exception:
        pass


def _load_zosapi() -> object:
    try:
        import clr  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "当前 Python 环境缺少 pythonnet（import clr 失败），不能连接当前 OpticStudio。"
            "请用包含 pythonnet 的环境启动软件，例如 D:\\anaconda\\envs\\zemax310\\python.exe。"
        ) from exc

    opticstudio_dir = require_file(app_config.OPTICSTUDIO_EXE, "OpticStudio executable").parent
    net_helper_candidates = [
        opticstudio_dir / "ZOSAPI_NetHelper.dll",
        opticstudio_dir / "ZemaxData" / "ZOS-API" / "Libraries" / "ZOSAPI_NetHelper.dll",
    ]
    net_helper = next((path for path in net_helper_candidates if path.exists()), None)
    if net_helper is None:
        raise FileNotFoundError("没有找到 ZOSAPI_NetHelper.dll，无法加载 ZOS-API。")

    clr.AddReference(str(net_helper))
    import ZOSAPI_NetHelper  # type: ignore

    initialized = ZOSAPI_NetHelper.ZOSAPI_Initializer.Initialize(str(opticstudio_dir))
    if not initialized:
        initialized = ZOSAPI_NetHelper.ZOSAPI_Initializer.Initialize()
    if not initialized:
        raise RuntimeError(f"ZOS-API 初始化失败，OpticStudio 路径: {opticstudio_dir}")

    zemax_dir = Path(str(ZOSAPI_NetHelper.ZOSAPI_Initializer.GetZemaxDirectory()))
    clr.AddReference(str(zemax_dir / "ZOSAPI.dll"))
    clr.AddReference(str(zemax_dir / "ZOSAPI_Interfaces.dll"))
    import ZOSAPI  # type: ignore

    return ZOSAPI


def _valid_current_zos_application(application: object | None) -> object | None:
    if application is None:
        return None
    try:
        if not bool(getattr(application, "IsValidLicenseForAPI", False)):
            return None
        system = getattr(application, "PrimarySystem", None)
        if system is None:
            return None
        return system
    except Exception:
        return None


def _current_nce_object_rows(nce: object) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    count = int(getattr(nce, "NumberOfObjects", 0) or 0)
    for object_index in range(1, count + 1):
        try:
            obj = nce.GetObjectAt(object_index)
        except Exception:
            continue
        comment = str(getattr(obj, "Comment", "") or "").strip()
        if not comment:
            continue
        rows.append(
            {
                "object_index": object_index,
                "comment": comment,
                "object": obj,
                "values": _nce_object_pose_values(obj),
            }
        )
    return rows


def _nce_object_pose_values(obj: object) -> list[float]:
    return [
        float(obj.XPosition),
        float(obj.YPosition),
        float(obj.ZPosition),
        float(obj.TiltAboutX),
        float(obj.TiltAboutY),
        float(obj.TiltAboutZ),
    ]


def _set_nce_object_pose_values(obj: object, values: list[float]) -> None:
    if len(values) < 6:
        raise ValueError("NCE object pose requires six values.")
    obj.XPosition = float(values[0])
    obj.YPosition = float(values[1])
    obj.ZPosition = float(values[2])
    obj.TiltAboutX = float(values[3])
    obj.TiltAboutY = float(values[4])
    obj.TiltAboutZ = float(values[5])


def _apply_pose_records_to_nce(
    records: list[LensPoseRecord],
    object_rows: list[dict[str, object]],
    baseline: dict[str, object],
    zemax_length_unit: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    matched: list[dict[str, object]] = []
    unmatched: list[dict[str, object]] = []
    for record in records:
        matches = _match_nce_objects(record.name, object_rows)
        if not matches:
            unmatched.append(record.as_dict())
            continue
        for item in matches:
            obj = item["object"]
            current_values = _nce_object_pose_values(obj)
            baseline_entry = _pose_baseline_entry_for_object(baseline, item)
            baseline_values = list(baseline_entry.get("values") or current_values)
            if len(baseline_values) != 6:
                baseline_values = current_values
            length_scale = _length_unit_scale(record.displacement_unit, zemax_length_unit)
            rotation_scale = _rotation_unit_scale(record.rotation_unit, "deg")
            raw_delta = [record.x, record.y, record.z, record.rx, record.ry, record.rz]
            delta = [
                record.x * length_scale,
                record.y * length_scale,
                record.z * length_scale,
                record.rx * rotation_scale,
                record.ry * rotation_scale,
                record.rz * rotation_scale,
            ]
            new_values = [float(baseline_values[index]) + delta[index] for index in range(6)]
            _set_nce_object_pose_values(obj, new_values)
            matched.append(
                {
                    **record.as_dict(),
                    "object_index": item["object_index"],
                    "comment": item["comment"],
                    "old_values": current_values,
                    "current_values": current_values,
                    "baseline_values": baseline_values,
                    "baseline_source": baseline_entry.get("source") or baseline.get("source") or "",
                    "baseline_missing": bool(baseline_entry.get("missing")),
                    "raw_delta_values": raw_delta,
                    "converted_delta_values": delta,
                    "new_values": new_values,
                    "mechanical_displacement_unit": record.displacement_unit,
                    "zemax_length_unit": zemax_length_unit,
                    "length_scale": length_scale,
                    "mechanical_rotation_unit": record.rotation_unit,
                    "zemax_rotation_unit": "deg",
                    "rotation_scale": rotation_scale,
                }
            )
    return matched, unmatched


def _matched_import_object_summaries(matched: list[dict[str, object]]) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    seen: set[tuple[int, str, str]] = set()
    for item in matched:
        try:
            object_index = int(item.get("object_index") or 0)
        except (TypeError, ValueError):
            object_index = 0
        comment = str(item.get("comment") or "").strip()
        source_name = str(item.get("name") or "").strip()
        display_name = comment or source_name
        key = (object_index, display_name, source_name)
        if key in seen:
            continue
        seen.add(key)
        summaries.append(
            {
                "object_index": object_index,
                "name": display_name,
                "comment": comment,
                "source_name": source_name,
            }
        )
    return summaries


def _restore_nce_pose_baseline(object_rows: list[dict[str, object]], baseline: dict[str, object]) -> None:
    for item in object_rows:
        obj = item.get("object")
        if obj is None:
            continue
        baseline_entry = _pose_baseline_entry_for_object(baseline, item)
        values = baseline_entry.get("values")
        if isinstance(values, list) and len(values) == 6:
            _set_nce_object_pose_values(obj, [float(value) for value in values])


def _current_detector_rows(nce: object) -> list[dict[str, object]]:
    detectors: list[dict[str, object]] = []
    count = int(getattr(nce, "NumberOfObjects", 0) or 0)
    for object_index in range(1, count + 1):
        try:
            obj = nce.GetObjectAt(object_index)
        except Exception:
            continue
        type_name = _safe_string(getattr(obj, "TypeName", ""))
        type_value = _safe_string(getattr(obj, "Type", ""))
        if "detector" not in f"{type_name} {type_value}".lower():
            continue
        comment = str(getattr(obj, "Comment", "") or "").strip()
        detector = {
            "object_index": object_index,
            "comment": comment,
            "type_name": type_name or type_value,
            "x_pixels": None,
            "y_pixels": None,
            "x_half_width": _safe_object_cell_number(obj, "Par1"),
            "y_half_width": _safe_object_cell_number(obj, "Par2"),
        }
        try:
            success, x_pixels, y_pixels = nce.GetDetectorDimensions(object_index, 0, 0)
            if success:
                detector["x_pixels"] = int(x_pixels)
                detector["y_pixels"] = int(y_pixels)
        except Exception:
            detector["x_pixels"] = _safe_object_cell_integer(obj, "Par3")
            detector["y_pixels"] = _safe_object_cell_integer(obj, "Par4")
        detectors.append(detector)
    return detectors


def _safe_string(value: object) -> str:
    try:
        return str(value or "")
    except Exception:
        return ""


def _safe_object_cell_number(obj: object, column_name: str) -> float | None:
    try:
        ZOSAPI = _load_zosapi()
        cell = obj.GetObjectCell(getattr(ZOSAPI.Editors.NCE.ObjectColumn, column_name))
        try:
            return float(cell.DoubleValue)
        except Exception:
            return float(cell.IntegerValue)
    except Exception:
        return None


def _safe_object_cell_integer(obj: object, column_name: str) -> int | None:
    try:
        ZOSAPI = _load_zosapi()
        cell = obj.GetObjectCell(getattr(ZOSAPI.Editors.NCE.ObjectColumn, column_name))
        try:
            return int(cell.IntegerValue)
        except Exception:
            return int(round(float(cell.DoubleValue)))
    except Exception:
        return None


def _detected_cpu_core_count() -> int:
    return max(1, int(os.cpu_count() or 1))


def _configure_ray_trace_cpu_cores(ray_trace: object, requested_core_count: int | None) -> dict[str, object]:
    detected_cores = _detected_cpu_core_count()
    if requested_core_count is None:
        requested_cores = detected_cores
        mode = "auto_all_logical_cores"
    else:
        requested_cores = max(1, int(requested_core_count))
        mode = "manual"

    result: dict[str, object] = {
        "mode": mode,
        "detected_cpu_cores": detected_cores,
        "requested_cpu_cores": requested_cores,
        "supported": False,
        "configured_cpu_cores": None,
        "message": "",
    }
    try:
        current_value = getattr(ray_trace, "NumberOfCores")
        del current_value
        setattr(ray_trace, "NumberOfCores", requested_cores)
        result["configured_cpu_cores"] = int(getattr(ray_trace, "NumberOfCores"))
        result["supported"] = True
        result["message"] = "已设置 NSC Ray Trace NumberOfCores。"
    except Exception as exc:
        result["message"] = f"当前 ZOS-API 无法设置 NSC Ray Trace NumberOfCores: {exc}"
    return result


def _export_detector_result(
    nce: object,
    project_file: Path,
    detector: dict[str, object],
    output_dir: Path,
    *,
    log_scale: bool,
    export_matlab: bool,
) -> dict[str, object]:
    detector_number = int(detector.get("object_index") or 0)
    if detector_number <= 0:
        raise ValueError(f"无效探测器编号: {detector_number}")

    read_started = time.monotonic()
    # 快速查看与完整导出现在都读全分辨率矩阵;两者区别仅在于是否落盘 .mat 文件。
    detector_result = _read_detector_result_grid(nce, detector_number, max_dimension=None)
    read_seconds = time.monotonic() - read_started
    base_name = _detector_result_base_name(project_file, detector, detector_number)
    png_path = output_dir / f"{base_name}.png"
    write_started = time.monotonic()
    mat_path: Path | None = None
    if export_matlab:
        mat_path = output_dir / f"{base_name}.mat"
        _write_detector_grid_mat(mat_path, detector_result, detector)
    image_grid = _downsample_grid_for_preview(
        detector_result["grid"],
        DETECTOR_PREVIEW_MAX_DIMENSION,
    )
    _write_false_color_png(
        png_path,
        image_grid,
        float(detector_result["min_value"]),
        float(detector_result["max_value"]),
        log_scale=log_scale,
    )
    write_seconds = time.monotonic() - write_started
    return {
        "detector": detector,
        "status": "ok",
        "summary": {key: value for key, value in detector_result.items() if key != "grid"},
        "mat_path": str(mat_path) if mat_path is not None else "",
        "image_path": str(png_path),
        "read_mode": "full_matlab_export" if export_matlab else "full_read",
        "preview_image_max_dimension": DETECTOR_PREVIEW_MAX_DIMENSION,
        "timings": {
            "read_seconds": round(read_seconds, 3),
            "write_seconds": round(write_seconds, 3),
        },
    }


def _read_detector_result_grid(
    nce: object,
    detector_number: int,
    *,
    max_dimension: int | None = None,
) -> dict[str, object]:
    detector_number = int(detector_number)
    success_dims, x_pixels, y_pixels = nce.GetDetectorDimensions(detector_number, 0, 0)
    if not success_dims:
        raise RuntimeError(f"无法读取探测器 {detector_number} 的像素尺寸。")
    x_pixels = int(x_pixels)
    y_pixels = int(y_pixels)
    if x_pixels <= 0 or y_pixels <= 0:
        raise RuntimeError(f"探测器 {detector_number} 的像素尺寸无效: {x_pixels} x {y_pixels}")

    grid_result = _detector_flux_grid(
        nce,
        detector_number,
        x_pixels,
        y_pixels,
        max_dimension=max_dimension,
    )
    grid = grid_result["grid"]
    x_indices = grid_result["x_indices"]
    y_indices = grid_result["y_indices"]
    total_flux_success, total_flux = nce.GetDetectorData(detector_number, 0, 0, 0)
    total_hits_success, total_hits = nce.GetDetectorData(detector_number, -3, 0, 0)
    std_success, std_dev = nce.GetDetectorData(detector_number, -4, 0, 0)

    minimum = math.inf
    maximum = -math.inf
    summed = 0.0
    weighted_x = 0.0
    weighted_y = 0.0
    nonzero_pixels = 0
    for y_index, row in enumerate(grid):
        original_y = int(y_indices[y_index]) if y_index < len(y_indices) else y_index
        for x_index, value in enumerate(row):
            original_x = int(x_indices[x_index]) if x_index < len(x_indices) else x_index
            value = float(value)
            if value < minimum:
                minimum = value
            if value > maximum:
                maximum = value
            summed += value
            if value != 0:
                nonzero_pixels += 1
                weighted_x += original_x * value
                weighted_y += original_y * value
    if math.isinf(minimum):
        minimum = 0.0
    if maximum == -math.inf:
        maximum = 0.0
    centroid_x = weighted_x / summed if summed else None
    centroid_y = weighted_y / summed if summed else None

    return {
        "x_pixels": x_pixels,
        "y_pixels": y_pixels,
        "preview_x_pixels": len(grid[0]) if grid else 0,
        "preview_y_pixels": len(grid),
        "sampled": bool(grid_result["sampled"]),
        "read_method": str(grid_result["read_method"]),
        "total_flux": float(total_flux) if total_flux_success else summed,
        "total_hits": float(total_hits) if total_hits_success else None,
        "std_dev": float(std_dev) if std_success else None,
        "grid_sum": summed,
        "min_value": float(minimum),
        "max_value": float(maximum),
        "nonzero_pixels": nonzero_pixels,
        "centroid_x_pixel": centroid_x,
        "centroid_y_pixel": centroid_y,
        "grid": grid,
    }


def _detector_flux_grid(
    nce: object,
    detector_number: int,
    x_pixels: int,
    y_pixels: int,
    *,
    max_dimension: int | None = None,
) -> dict[str, object]:
    x_indices = _sample_indices(x_pixels, max_dimension)
    y_indices = _sample_indices(y_pixels, max_dimension)
    sampled = len(x_indices) != x_pixels or len(y_indices) != y_pixels
    try:
        data = nce.GetAllDetectorDataSafe(detector_number, 0)
        if int(getattr(data, "Rank", 0) or 0) == 2:
            width = int(data.GetLength(0))
            height = int(data.GetLength(1))
            x_indices = _sample_indices(width, max_dimension)
            y_indices = _sample_indices(height, max_dimension)
            data_sampled = len(x_indices) != width or len(y_indices) != height
            return {
                "grid": [
                    [float(data.GetValue(x_index, y_index)) for x_index in x_indices]
                    for y_index in y_indices
                ],
                "x_indices": x_indices,
                "y_indices": y_indices,
                "sampled": data_sampled,
                "read_method": "GetAllDetectorDataSafe_sampled" if data_sampled else "GetAllDetectorDataSafe",
            }
    except Exception:
        pass

    grid: list[list[float]] = []
    for y_index in y_indices:
        row: list[float] = []
        for x_index in x_indices:
            pixel = int(y_index) * x_pixels + int(x_index) + 1
            success, value = nce.GetDetectorData(detector_number, pixel, 0, 0)
            row.append(float(value) if success else 0.0)
        grid.append(row)
    return {
        "grid": grid,
        "x_indices": x_indices,
        "y_indices": y_indices,
        "sampled": sampled,
        "read_method": "GetDetectorData_sampled" if sampled else "GetDetectorData",
    }


def _sample_indices(length: int, max_dimension: int | None) -> list[int]:
    length = int(length)
    if length <= 0:
        return []
    if max_dimension is None or int(max_dimension) <= 0 or length <= int(max_dimension):
        return list(range(length))
    sample_count = max(1, min(length, int(max_dimension)))
    if sample_count == 1:
        return [length // 2]
    return [
        min(length - 1, max(0, int(round(index * (length - 1) / (sample_count - 1)))))
        for index in range(sample_count)
    ]


def _downsample_grid_for_preview(grid: list[list[float]], max_dimension: int) -> list[list[float]]:
    if not grid or not grid[0]:
        return grid
    height = len(grid)
    width = len(grid[0])
    x_indices = _sample_indices(width, max_dimension)
    y_indices = _sample_indices(height, max_dimension)
    if len(x_indices) == width and len(y_indices) == height:
        return grid
    return [[float(grid[y_index][x_index]) for x_index in x_indices] for y_index in y_indices]


def _detector_result_base_name(project_file: Path, detector: dict[str, object], detector_number: int) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    comment = re.sub(r"[^A-Za-z0-9_-]+", "_", str(detector.get("comment") or "detector")).strip("_")
    if not comment:
        comment = "detector"
    return f"{project_file.stem}_detector_{int(detector_number):03d}_{comment}_{timestamp}"


def _write_detector_grid_csv(path: Path, grid: list[list[float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        for row in grid:
            writer.writerow([f"{float(value):.12g}" for value in row])


def _write_detector_grid_mat(
    path: Path,
    detector_result: dict[str, object],
    detector: dict[str, object],
) -> None:
    """把全分辨率探测器矩阵 + 摘要写成 MATLAB .mat 二进制文件。

    MATLAB 中 ``load('xxx.mat')`` 即得到:
    - ``detector``      : y×x 的双精度通量矩阵(行=Y 像素,列=X 像素,与 CSV 行序一致);
    - ``detector_info`` : 含探测器编号、注释、像素尺寸、总通量、质心等的结构体。
    """
    if _np is None:
        raise RuntimeError("导出 MATLAB .mat 需要 numpy,但当前环境未安装 numpy(pip install numpy)。")
    try:
        from scipy.io import savemat
    except Exception as exc:  # pragma: no cover - 仅在缺少 scipy 的环境触发
        raise RuntimeError(
            "导出 MATLAB .mat 需要 scipy,但当前环境未安装 scipy(pip install scipy)。"
        ) from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    grid = detector_result.get("grid") or []
    array = _np.asarray(grid, dtype=float)
    if array.ndim != 2:
        array = array.reshape((len(grid), -1)) if grid else _np.zeros((0, 0), dtype=float)

    def _num(value: object, default: float = 0.0) -> float:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    centroid_x = detector_result.get("centroid_x_pixel")
    centroid_y = detector_result.get("centroid_y_pixel")
    info = {
        "object_index": int(_num(detector.get("object_index"))),
        "comment": str(detector.get("comment") or ""),
        "type_name": str(detector.get("type_name") or ""),
        "x_pixels": int(_num(detector_result.get("x_pixels"), array.shape[1] if array.ndim == 2 else 0)),
        "y_pixels": int(_num(detector_result.get("y_pixels"), array.shape[0] if array.ndim == 2 else 0)),
        "total_flux": _num(detector_result.get("total_flux")),
        "total_hits": _num(detector_result.get("total_hits")),
        "std_dev": _num(detector_result.get("std_dev")),
        "grid_sum": _num(detector_result.get("grid_sum")),
        "min_value": _num(detector_result.get("min_value")),
        "max_value": _num(detector_result.get("max_value")),
        "nonzero_pixels": int(_num(detector_result.get("nonzero_pixels"))),
        "centroid_x_pixel": _num(centroid_x, float("nan")) if centroid_x is not None else float("nan"),
        "centroid_y_pixel": _num(centroid_y, float("nan")) if centroid_y is not None else float("nan"),
    }
    savemat(str(path), {"detector": array, "detector_info": info}, do_compression=True)


def _grid_to_csv_text(grid: list[list[float]]) -> str:
    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    for row in grid:
        writer.writerow([f"{float(value):.12g}" for value in row])
    return stream.getvalue()


def _false_color_png_bytes(
    grid: list[list[float]],
    min_value: float,
    max_value: float,
    *,
    log_scale: bool,
) -> bytes:
    if not grid or not grid[0]:
        raise RuntimeError("探测器像素矩阵为空，无法生成伪彩色图。")
    from PySide6.QtCore import QByteArray, QBuffer, QIODevice
    from PySide6.QtGui import QImage, qRgb

    height = len(grid)
    width = len(grid[0])
    image = QImage(width, height, QImage.Format_RGB32)
    if max_value <= min_value:
        scale_denominator = 1.0
    elif log_scale:
        scale_denominator = math.log1p(max_value - min_value)
    else:
        scale_denominator = max_value - min_value

    for y_index, row in enumerate(grid):
        image_y = height - 1 - y_index
        for x_index, value in enumerate(row):
            value = float(value)
            if max_value <= min_value:
                normalized = 0.0
            elif log_scale:
                normalized = math.log1p(max(0.0, value - min_value)) / scale_denominator
            else:
                normalized = (value - min_value) / scale_denominator
            red, green, blue = _false_color_rgb(normalized)
            image.setPixel(x_index, image_y, qRgb(red, green, blue))

    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    try:
        if not image.save(buffer, "PNG"):
            raise RuntimeError("伪彩色 PNG 编码失败。")
        return bytes(data)
    finally:
        buffer.close()


def _matlab_dts_loader_text() -> str:
    return """function series = load_dts_detector_series(dtsFile, outputFolder)
%LOAD_DTS_DETECTOR_SERIES Read Ansys-Zemax STOP transient detector .dts files.
%   series = load_dts_detector_series('result.dts') unzips the custom .dts
%   container, reads manifest.json, and loads each detector CSV matrix.
%
%   The .dts file is a ZIP container with:
%     manifest.json
%     frames/0001/detector.csv
%     frames/0001/pose.json
%
%   MATLAB R2016b+ jsondecode is required.

if nargin < 2 || isempty(outputFolder)
    [parentFolder, baseName, ~] = fileparts(dtsFile);
    if isempty(parentFolder)
        parentFolder = pwd;
    end
    outputFolder = fullfile(parentFolder, [baseName '_unzipped']);
end
if ~exist(outputFolder, 'dir')
    mkdir(outputFolder);
end
unzip(dtsFile, outputFolder);
manifestText = fileread(fullfile(outputFolder, 'manifest.json'));
manifest = jsondecode(manifestText);
frameCount = numel(manifest.frames);
frames = struct([]);
for k = 1:frameCount
    frame = manifest.frames(k);
    detectorPath = fullfile(outputFolder, strrep(frame.detector_csv, '/', filesep));
    frames(k).frame_index = frame.frame_index;
    if isfield(frame, 'time_value')
        frames(k).time_value = frame.time_value;
    else
        frames(k).time_value = [];
    end
    if isfield(frame, 'time_label')
        frames(k).time_label = frame.time_label;
    else
        frames(k).time_label = '';
    end
    frames(k).detector = readmatrix(detectorPath);
end
series = struct();
series.manifest = manifest;
series.frames = frames;
series.outputFolder = outputFolder;
end
"""


def _write_false_color_png(
    path: Path,
    grid: list[list[float]],
    min_value: float,
    max_value: float,
    *,
    log_scale: bool,
) -> None:
    if not grid or not grid[0]:
        raise RuntimeError("探测器像素矩阵为空，无法生成伪彩色图。")
    from PySide6.QtGui import QImage, qRgb

    height = len(grid)
    width = len(grid[0])
    image = QImage(width, height, QImage.Format_RGB32)
    if max_value <= min_value:
        scale_denominator = 1.0
    elif log_scale:
        scale_denominator = math.log1p(max_value - min_value)
    else:
        scale_denominator = max_value - min_value

    for y_index, row in enumerate(grid):
        image_y = height - 1 - y_index
        for x_index, value in enumerate(row):
            value = float(value)
            if max_value <= min_value:
                normalized = 0.0
            elif log_scale:
                normalized = math.log1p(max(0.0, value - min_value)) / scale_denominator
            else:
                normalized = (value - min_value) / scale_denominator
            red, green, blue = _false_color_rgb(normalized)
            image.setPixel(x_index, image_y, qRgb(red, green, blue))
    path.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(path), "PNG"):
        raise RuntimeError(f"伪彩色图保存失败: {path}")


def _false_color_rgb(value: float) -> tuple[int, int, int]:
    value = min(1.0, max(0.0, float(value)))
    stops = [
        (0.0, (0, 0, 80)),
        (0.25, (0, 128, 255)),
        (0.5, (0, 220, 80)),
        (0.75, (255, 220, 0)),
        (1.0, (255, 0, 0)),
    ]
    for index in range(1, len(stops)):
        left_pos, left_color = stops[index - 1]
        right_pos, right_color = stops[index]
        if value <= right_pos:
            if right_pos == left_pos:
                fraction = 0.0
            else:
                fraction = (value - left_pos) / (right_pos - left_pos)
            return tuple(
                int(round(left_color[channel] + (right_color[channel] - left_color[channel]) * fraction))
                for channel in range(3)
            )
    return stops[-1][1]


def _match_nce_objects(name: str, rows: list[dict[str, object]]) -> list[dict[str, object]]:
    target = _normalize_name(name)
    return [row for row in rows if _normalize_name(str(row.get("comment") or "")) == target]


def _parse_nsop_line(line: str) -> dict[str, object] | None:
    stripped = line.strip()
    if not stripped.startswith("NSOP "):
        return None
    parts = stripped.split()
    if len(parts) < 7:
        return None
    try:
        values = [float(parts[index]) for index in range(1, 7)]
    except ValueError:
        return None
    return {"values": values, "tail": parts[7:]}


def _format_nsop_line(original_line: str, values: list[float], tail: list[str]) -> str:
    prefix = original_line[: len(original_line) - len(original_line.lstrip())]
    newline = "\r\n" if original_line.endswith("\r\n") else "\n" if original_line.endswith("\n") else ""
    fields = ["NSOP"] + [f"{value:.12E}" for value in values] + list(tail)
    return prefix + " ".join(fields) + newline
