from __future__ import annotations

import csv
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import OPTICSTUDIO_EXE, require_file


_ZOSAPI_SESSIONS: list[dict[str, object]] = []


def open_opticstudio(project_file: Path | None = None) -> dict[str, object]:
    """Open the selected Zemax project in a managed background ZOS-API session, without GUI."""
    if project_file is None:
        raise ValueError("后台打开 Zemax 需要先选择工程文件。")
    project_file = require_file(project_file, "Zemax project file")
    application, system = _open_or_reuse_zosapi_project(project_file)
    return _opticstudio_session_info(application, system, project_file, "standalone_background")


@dataclass
class LensPoseRecord:
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
            logs.append(f"跳过 {path.name}: 不是 UX/UY/UZ 方向位移结果，不能用于位姿计算。")
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
        missing = [component for component in ["ux", "uy", "uz"] if component not in components]
        if missing:
            logs.append(
                f"{lens_name}: 缺少 {', '.join(m.upper() for m in missing)}，"
                "只能读取到部分方向位移，无法计算完整位移/旋转。"
            )
            continue

        merged_nodes = _merge_component_tables(components)
        if not merged_nodes:
            logs.append(f"{lens_name}: 三个方向位移文件没有共同节点，无法计算。")
            continue

        has_coords = all("x" in node and "y" in node and "z" in node for node in merged_nodes)
        translation = _average_translation(merged_nodes)
        rotation_rad = (0.0, 0.0, 0.0)
        rotation_status = "not_computed"
        if has_coords:
            rotation_rad = _fit_small_rotation(merged_nodes, translation)
            rotation_status = "computed"
        else:
            logs.append(
                f"{lens_name}: 已计算平均位移，但导出 TXT 没有节点原始坐标，"
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
        )
        latest_records.append(record)
        all_records.append(
            {
                **record.as_dict(),
                "node_count": len(merged_nodes),
                "rotation_status": rotation_status,
            }
        )
        logs.append(
            f"{lens_name}: 节点 {len(merged_nodes)} 个，平均位移 "
            f"X={record.x:.6g}, Y={record.y:.6g}, Z={record.z:.6g}; "
            f"旋转 Rx={record.rx:.6g} deg, Ry={record.ry:.6g} deg, Rz={record.rz:.6g} deg "
            f"({rotation_status}); 位移单位={record.displacement_unit}"
        )

    if skipped_files:
        logs.append(f"跳过无法识别的 TXT 文件 {len(skipped_files)} 个。")
    if not all_records:
        logs.append(
            "没有算出可导入 Zemax 的镜片位姿。当前导出文件通常只有总形变或单方向 Directional Deformation。"
        )

    saved_paths: dict[str, str] = {}
    if save:
        output_dir = Path(save_dir) if save_dir is not None else folder / "zemax_pose_calculation"
        output_dir.mkdir(parents=True, exist_ok=True)
        saved_paths = _save_pose_calculation(output_dir, all_records, logs, folder)
        logs.append("位姿计算结果已保存: " + ", ".join(saved_paths.values()))

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
            "需要先读取位姿，并确保导出中包含每个镜片的 UX/UY/UZ 和节点坐标。"
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
    matched: list[dict[str, object]] = []
    unmatched: list[dict[str, object]] = []
    for record in records:
        matches = _match_nce_objects(record.name, object_rows)
        if not matches:
            unmatched.append(record.as_dict())
            continue
        for item in matches:
            obj = item["object"]
            current_values = [
                float(obj.XPosition),
                float(obj.YPosition),
                float(obj.ZPosition),
                float(obj.TiltAboutX),
                float(obj.TiltAboutY),
                float(obj.TiltAboutZ),
            ]
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
            obj.XPosition = new_values[0]
            obj.YPosition = new_values[1]
            obj.ZPosition = new_values[2]
            obj.TiltAboutX = new_values[3]
            obj.TiltAboutY = new_values[4]
            obj.TiltAboutZ = new_values[5]
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
    exact = [block for block in blocks if _normalize_name(str(block.get("comment") or "")) == target]
    if exact:
        return exact
    suffix = [block for block in blocks if _normalize_name(str(block.get("comment") or "")).endswith(target)]
    return suffix


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

    opticstudio_dir = require_file(OPTICSTUDIO_EXE, "OpticStudio executable").parent
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


def _match_nce_objects(name: str, rows: list[dict[str, object]]) -> list[dict[str, object]]:
    target = _normalize_name(name)
    exact = [row for row in rows if _normalize_name(str(row.get("comment") or "")) == target]
    if exact:
        return exact
    return [row for row in rows if _normalize_name(str(row.get("comment") or "")).endswith(target)]


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
