from __future__ import annotations

import ast
import json
import re
import socket
import subprocess
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from .config import DEFAULT_OPEN_JOURNAL, PROJECT_FILE, RUNWB2, WORKSPACE, require_file


_WORKBENCH_REF_RE = re.compile(r"\$\$[0-9a-fA-F-]+")
_WORKBENCH_LOCK_RE = re.compile(
    r"The project was locked by (?P<owner>.+?) at (?P<time>[0-9/:\s]+)\.",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AnalysisModule:
    """Analysis system stored in a Workbench Project Schematic."""

    index: int
    system_name: str
    display_text: str
    system_type: str
    physics_type: str
    analysis_type: str
    solver_type: str
    directory_name: str
    visible: bool

    @property
    def status_text(self) -> str:
        return "Visible" if self.visible else "Hidden"


@dataclass(frozen=True)
class ModuleOperationResult:
    project: Path
    system_name: str
    operation: str
    report: dict
    journal_path: Path

    @property
    def settings_before(self) -> dict:
        return dict(self.report.get("settings_before") or {})

    @property
    def settings_after(self) -> dict:
        return dict(self.report.get("settings_after") or {})

    @property
    def operation_log(self) -> list[str]:
        return [str(item) for item in self.report.get("operation_log") or []]


def _project_or_default(project_file: str | Path | None) -> Path:
    if project_file:
        return Path(project_file)
    if PROJECT_FILE is None:
        raise ValueError("请先选择 Ansys Workbench 工程文件（.wbpj 或 .wbpz）。")
    return PROJECT_FILE


def _project_cwd(project: Path) -> Path:
    if project.parent.exists():
        return project.parent
    return WORKSPACE


def _read_workbench_project_xml(project: Path) -> str:
    suffix = project.suffix.lower()
    if suffix == ".wbpz":
        with zipfile.ZipFile(project) as archive:
            project_members = [
                name for name in archive.namelist() if name.lower().endswith(".wbpj")
            ]
            if not project_members:
                raise ValueError(f"No .wbpj file was found inside archive: {project}")
            project_member = sorted(project_members, key=lambda name: (name.count("/"), name))[0]
            return archive.read(project_member).decode("utf-8-sig")

    return project.read_text(encoding="utf-8-sig")


def _element_text(element: ElementTree.Element | None) -> str:
    if element is None or element.text is None:
        return ""
    return element.text.strip()


def _parse_member_data(text: str) -> dict:
    if not text:
        return {}

    normalized = _WORKBENCH_REF_RE.sub('"<workbench-reference>"', text)
    try:
        parsed = ast.literal_eval(normalized)
    except (SyntaxError, ValueError):
        return {}

    if not isinstance(parsed, dict):
        return {}
    return parsed


def _workbench_string(value: str | Path) -> str:
    return repr(str(value))


def _mechanical_task_script(task: dict) -> str:
    task_json = json.dumps(task, ensure_ascii=True)
    task_literal = repr(task_json)
    return f"""
import json
import os
import traceback
import codecs

task = json.loads({task_literal})
report_path = task["report_path"]
progress_path = task.get("progress_path") or ""
export_dir = task.get("export_dir") or ""
export_text = bool(task.get("export_text"))
export_images = bool(task.get("export_images"))
export_result_paths = task.get("export_result_paths") or []
export_all_sets = bool(task.get("export_all_sets"))
export_time_range = task.get("export_time_range") or {{}}
settings_update = task.get("settings_update") or {{}}
conditions_update = task.get("conditions_update") or []
perform_solve = bool(task.get("perform_solve"))
include_results = bool(task.get("include_results"))
analysis_index = int(task.get("analysis_index") or 0)

result = {{
    "project": task.get("project"),
    "system_name": task.get("system_name"),
    "operation": task.get("operation"),
    "analysis_name": "",
    "analysis_index": analysis_index,
    "selected_analysis_index": 0,
    "analysis_type": "",
    "analysis_state": "",
    "solution_state": "",
    "analysis_candidates": [],
    "settings": [],
    "conditions": [],
    "solution_results": [],
    "result_sets": [],
    "exported_files": [],
    "export_all_sets": export_all_sets,
    "export_time_range": export_time_range,
    "settings_before": {{}},
    "settings_after": {{}},
    "operation_log": [],
    "messages": [],
    "solved": False,
    "solve_method": "",
}}

def text(value):
    try:
        unicode_type = unicode
    except NameError:
        unicode_type = str
    try:
        if isinstance(value, unicode_type):
            return value
    except Exception:
        pass
    try:
        return str(value)
    except Exception:
        try:
            return repr(value)
        except Exception:
            return "<unprintable>"

def write_report():
    folder = os.path.dirname(report_path)
    if folder and not os.path.exists(folder):
        os.makedirs(folder)
    with open(report_path, "w") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)

def write_progress(stage, status="", analysis=None, solution=None):
    if not progress_path:
        return
    payload = {{
        "stage": text(stage),
        "status": text(status),
        "analysis_state": "",
        "solution_state": "",
    }}
    if analysis is not None:
        payload["analysis_state"] = text(safe_get(analysis, "State"))
    if solution is not None:
        payload["solution_state"] = text(safe_get(solution, "State"))
    try:
        folder = os.path.dirname(progress_path)
        if folder and not os.path.exists(folder):
            os.makedirs(folder)
        with open(progress_path, "w") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
    except Exception:
        pass

def record(message):
    result["operation_log"].append(text(message))

def safe_get(obj, attr):
    try:
        return getattr(obj, attr)
    except Exception as ex:
        return "ERR:" + text(ex)

def dotnet_type_name(value):
    try:
        return text(value.GetType().FullName)
    except Exception:
        return type(value).__name__

def enum_options(value):
    try:
        import System
        value_type = value.GetType()
        if not value_type.IsEnum:
            return []
        return [text(name) for name in System.Enum.GetNames(value_type)]
    except Exception:
        return []

def setting_kind(value, options):
    if options:
        return "enum"
    full_name = dotnet_type_name(value)
    value_text = text(value)
    if "Quantity" in full_name or looks_quantity_text(value_text):
        return "quantity"
    if isinstance(value, bool) or full_name == "System.Boolean":
        return "bool"
    if isinstance(value, int) or full_name in (
        "System.Int16",
        "System.Int32",
        "System.Int64",
        "System.UInt16",
        "System.UInt32",
        "System.UInt64",
    ):
        return "int"
    if isinstance(value, float) or full_name in ("System.Single", "System.Double", "System.Decimal"):
        return "float"
    if full_name == "System.String":
        return "text"
    return "text"

def looks_quantity_text(value):
    value_text = text(value)
    return "[" in value_text and "]" in value_text

def simple_editable(value, kind, value_text):
    if value_text.startswith("ERR:"):
        return False
    full_name = dotnet_type_name(value)
    if kind in ("enum", "bool", "int", "float", "quantity"):
        return True
    return full_name == "System.String"

def setting_record(api_name, display_name, value):
    options = enum_options(value)
    kind = setting_kind(value, options)
    if kind == "bool":
        options = ["False", "True"]
    value_text = text(value)
    return {{
        "api_name": text(api_name),
        "display_name": text(display_name or api_name),
        "value": value_text,
        "kind": kind,
        "type": dotnet_type_name(value),
        "options": options,
        "editable": simple_editable(value, kind, value_text),
    }}

def collect_settings(settings):
    values = {{}}
    records = []
    try:
        visible_properties = list(settings.VisibleProperties)
    except Exception as ex:
        record("VisibleProperties unavailable: " + text(ex))
        visible_properties = []

    for prop in visible_properties:
        api_name = text(safe_get(prop, "APIName"))
        display_name = text(safe_get(prop, "Name"))
        if not api_name or api_name == "None" or api_name.startswith("ERR:"):
            continue
        value = safe_get(settings, api_name)
        item = setting_record(api_name, display_name, value)
        values[api_name] = item["value"]
        records.append(item)

    return values, records

def collect_visible_properties(obj):
    records = []
    try:
        visible_properties = list(obj.VisibleProperties)
    except Exception:
        visible_properties = []

    for prop in visible_properties:
        api_name = text(safe_get(prop, "APIName"))
        display_name = text(safe_get(prop, "Name"))
        if not api_name or api_name == "None" or api_name.startswith("ERR:"):
            continue
        value = safe_get(obj, api_name)
        item = setting_record(api_name, display_name, value)
        records.append(item)
    return records

def object_category(obj):
    value = safe_get(obj, "DataModelObjectCategory")
    if text(value).startswith("ERR:"):
        value = safe_get(obj, "ObjectCategory")
    return text(value)

def object_children(obj):
    try:
        return list(obj.Children)
    except Exception:
        return []

def object_label(obj):
    name = text(safe_get(obj, "Name"))
    if not name or name == "None" or name.startswith("ERR:"):
        name = text(obj)
    return name

def callable_attr(obj, attr):
    try:
        return callable(getattr(obj, attr))
    except Exception:
        return False

def is_skipped_condition_branch(obj):
    label = object_label(obj).lower()
    category = object_category(obj).lower()
    type_name = dotnet_type_name(obj).lower()
    combined = " ".join([label, category, type_name])
    skipped_tokens = [
        "analysissettings",
        "analysis settings",
        "solution",
        "result",
        "probe",
        "chart",
        "worksheet",
    ]
    return any(token in combined for token in skipped_tokens)

def condition_state(obj):
    suppressed = safe_get(obj, "Suppressed")
    suppressed_text = text(suppressed)
    if suppressed_text == "True":
        return "Suppressed"
    if suppressed_text == "False":
        return "Active"
    state = text(safe_get(obj, "ObjectState"))
    if not state.startswith("ERR:") and state != "None":
        return state
    return ""

def condition_objects(analysis):
    objects = []

    def visit(obj, path, depth):
        if depth > 8:
            return
        for child in object_children(obj):
            if is_skipped_condition_branch(child):
                continue
            child_name = object_label(child)
            child_path = path + " / " + child_name if path else child_name
            properties = collect_visible_properties(child)
            children = object_children(child)

            if properties:
                objects.append((child_path, child, properties))

            if children:
                visit(child, child_path, depth + 1)

    visit(analysis, "", 0)
    return objects

def collect_conditions(analysis):
    conditions = []
    for child_path, child, properties in condition_objects(analysis):
        conditions.append({{
            "name": object_label(child),
            "path": child_path,
            "category": object_category(child),
            "type": dotnet_type_name(child),
            "state": condition_state(child),
            "properties": properties,
        }})
    return conditions

def collect_result_summary_properties(obj, existing_properties):
    existing_api_names = set([text(item.get("api_name", "")) for item in existing_properties])
    records = []
    common_names = [
        "Minimum",
        "Maximum",
        "Average",
        "MinimumOccursOn",
        "MaximumOccursOn",
        "DisplayTime",
        "SetNumber",
        "ResultNumber",
        "Unit",
        "By",
        "Location",
        "ScopingMethod",
        "Suppressed",
        "ObjectState",
    ]
    for api_name in common_names:
        if api_name in existing_api_names:
            continue
        value = safe_get(obj, api_name)
        value_text = text(value)
        if value_text.startswith("ERR:") or value_text in ("None", ""):
            continue
        item = setting_record(api_name, api_name, value)
        item["editable"] = False
        records.append(item)
    return records

def is_solution_result_object(obj, properties=None):
    label = object_label(obj).lower()
    category = object_category(obj).lower()
    type_name = dotnet_type_name(obj).lower()
    combined = " ".join([label, category, type_name])
    result_tokens = [
        "result",
        "deformation",
        "stress",
        "strain",
        "temperature",
        "thermal",
        "heat",
        "flux",
        "probe",
        "userdefined",
        "user defined",
    ]
    if any(token in combined for token in result_tokens):
        return True
    if callable_attr(obj, "Evaluate") or callable_attr(obj, "ExportToTextFile"):
        return True
    props = properties or []
    prop_names = set([text(item.get("api_name", "")).lower() for item in props])
    return any(name in prop_names for name in ["minimum", "maximum", "average"])

def collect_solution_results(solution):
    solution_results = []

    def visit(obj, path, depth):
        if depth > 10:
            return
        for child in object_children(obj):
            child_name = object_label(child)
            child_path = path + " / " + child_name if path else child_name
            properties = collect_visible_properties(child)
            properties.extend(collect_result_summary_properties(child, properties))
            children = object_children(child)

            if properties and is_solution_result_object(child, properties):
                solution_results.append({{
                    "name": child_name,
                    "path": child_path,
                    "category": object_category(child),
                    "type": dotnet_type_name(child),
                    "state": condition_state(child),
                    "properties": properties,
                }})

            if children:
                visit(child, child_path, depth + 1)

    visit(solution, "", 0)
    return solution_results

def as_list(value):
    value_text = text(value)
    if value_text.startswith("ERR:") or value_text in ("None", ""):
        return []
    try:
        return list(value)
    except Exception:
        pass
    try:
        count = int(value.Count)
        return [value[index] for index in range(count)]
    except Exception:
        return []

def int_or_zero(value):
    try:
        return int(value)
    except Exception:
        pass
    try:
        return int(float(text(value)))
    except Exception:
        return 0

def safe_get_or_call(obj, attr):
    value = safe_get(obj, attr)
    try:
        if callable(value):
            return value()
    except Exception:
        pass
    return value

def collect_result_sets(analysis, solution=None):
    records = []
    seen = set()

    def add_record(set_number, time_value):
        set_number = int_or_zero(set_number) or 1
        time_text = text(time_value).strip()
        key = (set_number, time_text)
        if key in seen:
            return
        seen.add(key)
        records.append({{
            "set_number": set_number,
            "time": time_text,
            "label": result_set_label(set_number, time_text),
        }})

    def visit_result_objects(obj, depth):
        if depth > 6:
            return
        for child in object_children(obj):
            set_number = safe_get(child, "SetNumber")
            if text(set_number).startswith("ERR:") or text(set_number) in ("None", ""):
                set_number = safe_get(child, "ResultNumber")
            display_time = safe_get(child, "DisplayTime")
            if not text(set_number).startswith("ERR:") or not text(display_time).startswith("ERR:"):
                if text(set_number) not in ("", "None") or text(display_time) not in ("", "None"):
                    add_record(set_number, display_time)
            children = object_children(child)
            if children:
                visit_result_objects(child, depth + 1)

    if solution is not None:
        try:
            visit_result_objects(solution, 0)
        except Exception as ex:
            record("Lightweight result-set collection failed: " + text(ex))

    record("Skipped analysis.GetResultsData during read to avoid blocking Mechanical on database result loading.")

    if not records:
        records.append({{"set_number": 1, "time": "", "label": "Set 1"}})
    return sorted(records, key=lambda item: int_or_zero(item.get("set_number")))

def result_set_label(set_number, time_value):
    time_text = text(time_value).strip()
    if time_text and time_text not in ("None", ""):
        return "Set " + text(set_number) + " / " + time_text
    return "Set " + text(set_number)

def time_range_requested(time_range):
    return any(text(time_range.get(key, "")).strip() for key in ("start", "end", "step"))

def split_number_unit(raw_value):
    raw = text(raw_value).strip()
    if not raw:
        raise ValueError("时间值不能为空")
    allowed = "+-.0123456789eE"
    end = 0
    while end < len(raw) and raw[end] in allowed:
        end += 1
    if end == 0:
        raise ValueError("无法解析时间数值: " + raw)
    return float(raw[:end]), raw[end:].strip()

def format_time_value(value, unit):
    number_text = ("%.12g" % value)
    unit_text = text(unit).strip()
    if unit_text:
        return number_text + " " + unit_text
    return number_text

def build_time_range_sets(time_range):
    start_raw = text(time_range.get("start", "")).strip()
    end_raw = text(time_range.get("end", "")).strip()
    step_raw = text(time_range.get("step", "")).strip()
    if not start_raw or not end_raw or not step_raw:
        raise RuntimeError("按时间范围导出时，请填写开始时间、结束时间和时间间隔；或者全部留空按已有结果集导出。")

    start_value, start_unit = split_number_unit(start_raw)
    end_value, end_unit = split_number_unit(end_raw)
    step_value, step_unit = split_number_unit(step_raw)
    unit = start_unit or end_unit or step_unit
    if step_value <= 0:
        raise RuntimeError("时间间隔必须大于 0。")
    if end_value < start_value:
        raise RuntimeError("结束时间不能小于开始时间。")

    records = []
    value = start_value
    index = 1
    tolerance = abs(step_value) * 1e-9 + 1e-12
    while value <= end_value + tolerance:
        if index > 10000:
            raise RuntimeError("时间点数量超过 10000，请增大时间间隔或缩小导出范围。")
        time_text = format_time_value(value, unit)
        records.append({{
            "set_number": index,
            "time": time_text,
            "label": "Time " + time_text,
            "source": "time_range",
        }})
        value = start_value + step_value * index
        index += 1
    return records

def build_export_result_sets(existing_sets, time_range):
    if time_range_requested(time_range):
        return build_time_range_sets(time_range)
    return existing_sets

def safe_filename(value):
    raw = text(value).strip()
    if not raw:
        raw = "result"
    invalid = '<>:"/\\\\|?*'
    cleaned = []
    for char in raw:
        if char in invalid or ord(char) < 32 or ord(char) > 127:
            cleaned.append("_")
        else:
            cleaned.append(char)
    return "".join(cleaned)[:120].strip(" .") or "result"

def result_set_suffix(set_info):
    set_number = int_or_zero(set_info.get("set_number"))
    suffix = "set_" + str(set_number).zfill(3)
    time_text = text(set_info.get("time", "")).strip()
    if time_text:
        suffix += "_" + safe_filename(time_text)
    return suffix

def result_object_has_data(child):
    state_text = text(safe_get(child, "ObjectState")).lower()
    if state_text and not state_text.startswith("err:"):
        no_data_tokens = ["not solved", "unsolved", "not evaluated", "underdefined", "solve required", "invalid"]
        if any(token in state_text for token in no_data_tokens):
            return False
        return True
    for api_name in ("Minimum", "Maximum", "Average"):
        value_text = text(safe_get(child, api_name))
        if value_text and value_text not in ("None", "") and not value_text.startswith("ERR:"):
            return True
    return False

def is_invalid_result_expression_message(message):
    lower_message = text(message).lower()
    tokens = [
        "not a recognized result",
        "unable to create user defined result",
        "attempted to load a specific result",
    ]
    return any(token in lower_message for token in tokens)

def normalize_evaluate_failure(message):
    message_text = text(message)
    if is_invalid_result_expression_message(message_text):
        return "自定义结果表达式无效，Mechanical 无法识别该结果表达式: " + message_text
    return message_text

def refresh_result_object(child, solution, force=False):
    errors = []
    for name, call in [
        ("solution.EvaluateAllResults", lambda: solution.EvaluateAllResults()),
        ("child.EvaluateAllResults", lambda: child.EvaluateAllResults()),
        ("child.Evaluate", lambda: child.Evaluate()),
    ]:
        try:
            call()
        except Exception as ex:
            error_text = normalize_evaluate_failure(ex)
            errors.append(name + ": " + error_text)
            record(name + " skipped/failed: " + error_text)
            continue
        if result_object_has_data(child):
            return True, name
        errors.append(name + ": 结果评估命令已返回，但当前结果对象仍没有可用求解数据")
    if not force and not result_object_has_data(child):
        errors.append("结果评估后仍没有可用求解数据")
    if errors:
        return False, "; ".join(errors)
    return False, "当前结果对象没有可用的评估方法或评估失败"

def set_display_time(child, time_text):
    if not time_text:
        return False, "DisplayTime has no value"
    current_value = safe_get(child, "DisplayTime")
    if text(current_value).startswith("ERR:"):
        return False, text(current_value)
    attempts = [time_text]
    if "[" not in time_text and "]" not in time_text:
        attempts.append(time_text + " [sec]")
    last_error = ""
    for raw in attempts:
        try:
            setattr(child, "DisplayTime", convert_setting_value(current_value, raw))
            return True, "DisplayTime=" + raw
        except Exception as ex:
            last_error = text(ex)
    return False, last_error

def apply_result_set(child, solution, set_info):
    set_number = int_or_zero(set_info.get("set_number"))
    errors = []
    if text(set_info.get("source", "")) == "time_range":
        ok, message = set_display_time(child, text(set_info.get("time", "")))
        if ok:
            evaluated, refresh_method = refresh_result_object(child, solution, force=True)
            if evaluated:
                message += "; " + refresh_method
                return True, message
            else:
                errors.append("evaluate failed: " + refresh_method)
        errors.append("DisplayTime: " + message)

    for api_name in ("SetNumber", "ResultNumber"):
        current_value = safe_get(child, api_name)
        if text(current_value).startswith("ERR:"):
            continue
        try:
            setattr(child, api_name, convert_setting_value(current_value, set_number))
            method = api_name + "=" + text(set_number)
            evaluated, refresh_method = refresh_result_object(child, solution, force=True)
            if evaluated:
                method += "; " + refresh_method
                return True, method
            else:
                errors.append(api_name + " evaluate failed: " + refresh_method)
        except Exception as ex:
            errors.append(api_name + ": " + text(ex))

    ok, message = set_display_time(child, text(set_info.get("time", "")))
    if ok:
        evaluated, refresh_method = refresh_result_object(child, solution, force=True)
        if evaluated:
            message += "; " + refresh_method
            return True, message
        else:
            errors.append("DisplayTime evaluate failed: " + refresh_method)
    errors.append("DisplayTime: " + message)
    return False, "; ".join(errors)

def export_one_result(folder, solution, child_path, child, properties, base_name, write_text, write_images, exported):
    evaluated, evaluate_message = refresh_result_object(child, solution)
    exported.append({{
        "kind": "evaluate",
        "path": child_path,
        "status": "ok" if evaluated else "failed",
        "message": evaluate_message,
    }})
    if not evaluated:
        return
    try:
        if hasattr(child, "Activate"):
            child.Activate()
    except Exception as ex:
        exported.append({{"kind": "activate", "path": child_path, "status": "failed", "message": text(ex)}})

    if write_text:
        txt_path = os.path.join(folder, base_name + ".txt")
        if hasattr(child, "ExportToTextFile"):
            try:
                child.ExportToTextFile(txt_path)
                exported.append({{"kind": "txt", "path": txt_path, "status": "ok"}})
            except Exception as ex:
                exported.append({{"kind": "txt", "path": txt_path, "status": "failed", "message": text(ex)}})
        else:
            fallback_path = os.path.join(folder, base_name + "_properties.txt")
            try:
                with codecs.open(fallback_path, "w", "utf-8") as stream:
                    stream.write(child_path + "\\n")
                    for prop in properties:
                        stream.write(text(prop.get("display_name") or prop.get("api_name")) + ": " + text(prop.get("value")) + "\\n")
                exported.append({{"kind": "txt", "path": fallback_path, "status": "ok"}})
            except Exception as ex:
                exported.append({{"kind": "txt", "path": fallback_path, "status": "failed", "message": text(ex)}})

    if write_images:
        image_path = os.path.join(folder, base_name + ".png")
        try:
            ExtAPI.Graphics.ExportScreenToImage(image_path)
            exported.append({{"kind": "image", "path": image_path, "status": "ok"}})
        except Exception as ex:
            exported.append({{"kind": "image", "path": image_path, "status": "failed", "message": text(ex)}})

def export_solution_outputs(analysis, solution, folder, write_text, write_images, selected_paths, export_all_sets, export_time_range):
    if not folder or (not write_text and not write_images):
        return
    if not os.path.exists(folder):
        os.makedirs(folder)
    exported = []
    existing_result_sets = result.get("result_sets") or collect_result_sets(analysis, solution)
    result_sets = (
        build_export_result_sets(existing_result_sets, export_time_range)
        if export_all_sets
        else existing_result_sets
    )
    result["export_result_sets"] = result_sets
    summary_path = os.path.join(folder, "solution_results_summary.txt")
    with codecs.open(summary_path, "w", "utf-8") as stream:
        stream.write("Analysis: " + text(result.get("analysis_name")) + "\\n")
        stream.write("Analysis state: " + text(result.get("analysis_state")) + "\\n")
        stream.write("Solution state: " + text(result.get("solution_state")) + "\\n\\n")
        if export_all_sets:
            stream.write("Export mode: time range / result sets\\n")
            for set_info in result_sets:
                stream.write("  - " + text(set_info.get("label")) + "\\n")
            stream.write("\\n")
        for item in result.get("solution_results", []):
            stream.write("[" + text(item.get("path")) + "]\\n")
            stream.write("Category: " + text(item.get("category")) + "\\n")
            stream.write("State: " + text(item.get("state")) + "\\n")
            for prop in item.get("properties", []):
                stream.write(text(prop.get("display_name") or prop.get("api_name")) + ": " + text(prop.get("value")) + "\\n")
            stream.write("\\n")
    exported.append({{"kind": "summary", "path": summary_path, "status": "ok"}})

    selected = set([text(item) for item in selected_paths if text(item)])
    candidates = solution_result_objects(solution)
    if selected:
        candidates = [item for item in candidates if item[0] in selected]
    if selected and not candidates:
        exported.append({{"kind": "selection", "path": ", ".join(selected), "status": "failed", "message": "没有找到选中的求解结果"}})

    explicit_time_range = time_range_requested(export_time_range)
    multi_set_export = bool(export_all_sets and (explicit_time_range or len(result_sets) > 1))
    if export_all_sets and not explicit_time_range and len(result_sets) <= 1:
        exported.append({{"kind": "result_sets", "path": "", "status": "ok", "message": "没有枚举到多个时间点/结果集，已按当前结果状态导出"}})

    index = 1
    for child_path, child, properties in candidates:
        name = str(index).zfill(2) + "_" + safe_filename(child_path)
        index += 1
        if multi_set_export:
            total_sets = len(result_sets)
            for set_index, set_info in enumerate(result_sets, start=1):
                write_progress(
                    "导出求解结果",
                    "正在导出 " + child_path + " - " + text(set_info.get("label")) + " (" + text(set_index) + "/" + text(total_sets) + ")",
                    analysis,
                    solution,
                )
                applied, message = apply_result_set(child, solution, set_info)
                if not applied:
                    exported.append({{
                        "kind": "result_set",
                        "path": child_path,
                        "status": "failed",
                        "message": text(set_info.get("label")) + ": " + normalize_evaluate_failure(message),
                    }})
                    continue
                exported.append({{
                    "kind": "result_set",
                    "path": child_path,
                    "status": "ok",
                    "message": text(set_info.get("label")) + ": " + message,
                }})
                export_one_result(
                    folder,
                    solution,
                    child_path,
                    child,
                    properties,
                    name + "_" + result_set_suffix(set_info),
                    write_text,
                    write_images,
                    exported,
                )
        else:
            export_one_result(folder, solution, child_path, child, properties, name, write_text, write_images, exported)

    result["exported_files"] = exported

def solution_result_objects(solution):
    objects = []
    def visit(obj, path, depth):
        if depth > 10:
            return
        for child in object_children(obj):
            child_name = object_label(child)
            child_path = path + " / " + child_name if path else child_name
            properties = collect_visible_properties(child)
            properties.extend(collect_result_summary_properties(child, properties))
            children = object_children(child)
            if properties and is_solution_result_object(child, properties):
                objects.append((child_path, child, properties))
            if children:
                visit(child, child_path, depth + 1)
    visit(solution, "", 0)
    return objects

def enum_value(enum_type, value):
    if value in (None, "", "NoChange"):
        return None
    return getattr(enum_type, text(value))

def bool_value(value):
    if isinstance(value, bool):
        return value
    normalized = text(value).strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ValueError("Invalid boolean value: " + text(value))

def convert_setting_value(current_value, raw_value):
    options = enum_options(current_value)
    if options:
        import System
        return System.Enum.Parse(current_value.GetType(), text(raw_value))

    full_name = dotnet_type_name(current_value)
    if "Quantity" in full_name or looks_quantity_text(current_value):
        try:
            return Quantity(text(raw_value))
        except Exception:
            return text(raw_value)
    if isinstance(current_value, bool) or full_name == "System.Boolean":
        return bool_value(raw_value)
    if isinstance(current_value, int) or full_name in (
        "System.Int16",
        "System.Int32",
        "System.Int64",
        "System.UInt16",
        "System.UInt32",
        "System.UInt64",
    ):
        return int(raw_value)
    if isinstance(current_value, float) or full_name in ("System.Single", "System.Double", "System.Decimal"):
        return float(raw_value)
    return text(raw_value)

def apply_setting(settings, name, value):
    if value in (None, "", "NoChange"):
        return
    current_value = getattr(settings, name)
    converted = convert_setting_value(current_value, value)
    setattr(settings, name, converted)
    record("SET " + name + " = " + text(value))

def apply_object_property(obj, name, value):
    if value in (None, "", "NoChange"):
        return
    current_value = getattr(obj, name)
    converted = convert_setting_value(current_value, value)
    setattr(obj, name, converted)

def apply_condition_updates(analysis, updates):
    if not updates:
        return
    objects_by_path = {{}}
    for child_path, child, properties in condition_objects(analysis):
        objects_by_path[child_path] = child

    for update in updates:
        child_path = text(update.get("path", ""))
        api_name = text(update.get("api_name", ""))
        value = update.get("value")
        if not child_path or not api_name:
            continue
        child = objects_by_path.get(child_path)
        if child is None:
            raise RuntimeError("没有找到分析条件: " + child_path)
        try:
            apply_object_property(child, api_name, value)
            record("SET CONDITION " + child_path + "." + api_name + " = " + text(value))
        except Exception as ex:
            raise RuntimeError(
                "设置分析条件失败: " + child_path + "." + api_name + " = " + text(value) + "; " + text(ex)
            )

def analysis_text(analysis):
    parts = []
    for attr in ["Name", "AnalysisType"]:
        value = safe_get(analysis, attr)
        parts.append(text(value))
    parts.append(text(analysis))
    return " ".join(parts)

def choose_analysis(analyses):
    hint = text(task.get("analysis_hint") or "").strip().lower()
    result["analysis_candidates"] = []
    for index, candidate in enumerate(analyses):
        label = analysis_text(candidate)
        result["analysis_candidates"].append({{
            "index": index,
            "display_index": index + 1,
            "text": label,
        }})

    if len(analyses) == 1:
        result["selected_analysis_index"] = 1
        record("Only one Mechanical analysis exists; using it.")
        return analyses[0]

    if analysis_index > 0 and analysis_index <= len(analyses):
        candidate = analyses[analysis_index - 1]
        result["selected_analysis_index"] = analysis_index
        record("Matched analysis by Workbench module index: " + text(analysis_index))
        return candidate

    for index, candidate in enumerate(analyses):
        label = analysis_text(candidate)
        if hint and hint in label.lower():
            record("Matched analysis by hint: " + hint)
            result["selected_analysis_index"] = index + 1
            return candidate

    hint_words = [
        word for word in re_split_words(hint) if len(word) >= 4 and word not in ("ansys",)
    ]
    for index, candidate in enumerate(analyses):
        label_lower = analysis_text(candidate).lower()
        if hint_words and all(word in label_lower for word in hint_words):
            record("Matched analysis by hint words: " + ", ".join(hint_words))
            result["selected_analysis_index"] = index + 1
            return candidate

    if hint:
        record("No analysis matched hint '" + hint + "'. Using first Mechanical analysis.")
    result["selected_analysis_index"] = 1
    return analyses[0]

def re_split_words(value):
    words = []
    current = []
    for char in value:
        if char.isalnum():
            current.append(char)
        else:
            if current:
                words.append("".join(current))
                current = []
    if current:
        words.append("".join(current))
    return words

def clear_current_solution(analysis, solution):
    write_progress("清除旧求解数据", "正在清除当前分析模块已有求解数据", analysis, solution)
    clear_calls = [
        ("solution.ClearGeneratedData", lambda: solution.ClearGeneratedData()),
        ("analysis.ClearGeneratedData", lambda: analysis.ClearGeneratedData()),
    ]
    for name, call in clear_calls:
        try:
            record("Clearing current solve data with " + name)
            call()
            record(name + " completed")
            return
        except Exception as ex:
            record(name + " failed: " + text(ex))
    record("No current solve data clear method completed.")

def solve_analysis(analysis):
    clear_current_solution(analysis, analysis.Solution)
    write_progress("调用 Mechanical 求解", "Mechanical 正在求解，界面计时继续更新", analysis, analysis.Solution)
    attempts = [
        ("analysis.Solve", lambda: analysis.Solve()),
        ("analysis.Solution.Solve", lambda: analysis.Solution.Solve()),
        ("analysis.Solution.Solve(True)", lambda: analysis.Solution.Solve(True)),
    ]
    last_error = None
    for name, call in attempts:
        try:
            record("Calling " + name)
            call()
            result["solved"] = True
            result["solve_method"] = name
            record(name + " completed")
            write_progress("求解完成", name + " completed", analysis, analysis.Solution)
            return
        except Exception as ex:
            last_error = ex
            record(name + " failed: " + text(ex))
    raise last_error

try:
    write_progress("读取 Mechanical 工程", "正在读取当前 Mechanical 模型")
    model = ExtAPI.DataModel.Project.Model
    analyses = list(model.Analyses)
    if not analyses:
        raise RuntimeError("当前 Mechanical 工程没有可读取的分析模块。请先在 Mechanical 中打开包含分析的工程。")

    analysis = choose_analysis(analyses)
    settings = analysis.AnalysisSettings
    solution = analysis.Solution
    write_progress("已选择分析模块", object_label(analysis), analysis, solution)

    result["analysis_name"] = text(safe_get(analysis, "Name"))
    result["analysis_type"] = text(safe_get(analysis, "AnalysisType"))
    result["analysis_state"] = text(safe_get(analysis, "State"))
    result["solution_state"] = text(safe_get(solution, "State"))
    result["settings_before"], settings_records_before = collect_settings(settings)
    result["settings"] = settings_records_before

    for key in sorted(settings_update.keys()):
        write_progress("保存分析设置", "正在写入 AnalysisSettings." + text(key), analysis, solution)
        apply_setting(settings, key, settings_update[key])
    if conditions_update:
        write_progress("保存分析条件", "正在写入载荷/约束/热边界条件", analysis, solution)
    apply_condition_updates(analysis, conditions_update)

    write_progress("刷新设置和条件", "正在读取写入后的当前状态", analysis, solution)
    result["settings_after"], settings_records_after = collect_settings(settings)
    result["settings"] = settings_records_after
    result["conditions"] = collect_conditions(analysis)
    if include_results:
        result["result_sets"] = collect_result_sets(analysis, solution)
        write_progress("读取求解结果", "正在读取 Solution 下的所有结果对象", analysis, solution)
        result["solution_results"] = collect_solution_results(solution)
    if export_text or export_images:
        if not result["solution_results"]:
            result["result_sets"] = collect_result_sets(analysis, solution)
            write_progress("读取求解结果", "正在读取 Solution 下的所有结果对象", analysis, solution)
            result["solution_results"] = collect_solution_results(solution)
        write_progress("导出求解结果", "正在导出 TXT/图片文件", analysis, solution)
        export_solution_outputs(analysis, solution, export_dir, export_text, export_images, export_result_paths, export_all_sets, export_time_range)

    if perform_solve:
        solve_analysis(analysis)
        result["analysis_state"] = text(safe_get(analysis, "State"))
        result["solution_state"] = text(safe_get(solution, "State"))
        result["conditions"] = collect_conditions(analysis)
        result["result_sets"] = collect_result_sets(analysis, solution)
        write_progress("读取求解结果", "正在读取求解完成后的结果对象", analysis, solution)
        result["solution_results"] = collect_solution_results(solution)
        if export_text or export_images:
            write_progress("导出求解结果", "正在导出求解完成后的 TXT/图片文件", analysis, solution)
            export_solution_outputs(analysis, solution, export_dir, export_text, export_images, export_result_paths, export_all_sets, export_time_range)

    try:
        messages = list(ExtAPI.Application.Messages.AllVisibleMessages)
        for message in messages[-50:]:
            result["messages"].append({{
                "severity": text(safe_get(message, "Severity")),
                "text": text(safe_get(message, "DisplayString")),
            }})
    except Exception as ex:
        record("Message collection skipped: " + text(ex))

except Exception:
    write_progress("操作失败", traceback.format_exc())
    result["error"] = traceback.format_exc()
    write_report()
    raise
else:
    if not perform_solve:
        write_progress("操作完成", "当前 Mechanical 状态读取/保存完成")
    write_report()
"""


def _workbench_module_journal(
    *,
    project: Path,
    system_name: str,
    component_name: str,
    edit_before_send: bool,
    mechanical_script: str,
    save_project: bool,
) -> str:
    save_line = (
        f"Save(FilePath={_workbench_string(project)}, Overwrite=True)"
        if save_project
        else ""
    )
    if component_name == "Model":
        prepare_lines = """try:
    container1.Refresh()
except Exception:
    pass"""
    elif edit_before_send:
        prepare_lines = "container1.Edit()"
    else:
        prepare_lines = ""
    return f"""# encoding: utf-8
SetScriptVersion(Version="12.1")

system1 = GetSystem(Name={_workbench_string(system_name)})
container1 = system1.GetContainer(ComponentName={_workbench_string(component_name)})
{prepare_lines}

mechanical_script = r'''{mechanical_script}'''

container1.SendCommand(Language="Python", Command=mechanical_script)
container1.Exit()
{save_line}
"""


def _run_workbench_journal_with_progress(
    *,
    journal_path: Path,
    project: Path,
    progress_path: Path,
    progress_callback=None,
) -> subprocess.CompletedProcess[str]:
    runwb2 = require_file(RUNWB2, "RunWB2")
    cmd = [
        str(runwb2),
        "-B",
        "-F",
        str(project),
        "-R",
        str(journal_path),
    ]
    _emit_progress(progress_callback, "启动 Workbench", "正在通过 Workbench 执行当前模块操作")
    process = subprocess.Popen(
        cmd,
        cwd=str(_project_cwd(project)),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    last_text = ""
    while process.poll() is None:
        last_text = _poll_progress_file(progress_path, progress_callback, last_text)
        time.sleep(0.5)
    stdout, stderr = process.communicate()
    _poll_progress_file(progress_path, progress_callback, last_text)
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=int(process.returncode or 0),
        stdout=stdout or "",
        stderr=stderr or "",
    )


def read_project_analysis_modules(
    project_file: str | Path | None = None,
) -> list[AnalysisModule]:
    """Read existing analysis systems from a Workbench .wbpj/.wbpz project file."""
    project = require_file(_project_or_default(project_file), "Workbench project")
    if project.suffix.lower() not in {".wbpj", ".wbpz"}:
        raise ValueError(f"Expected a .wbpj or .wbpz Workbench project: {project}")

    xml_text = _read_workbench_project_xml(project)
    root = ElementTree.fromstring(xml_text)

    modules: list[AnalysisModule] = []
    for obj in root.findall(".//Object"):
        if _element_text(obj.find("class-type")) != "System":
            continue

        system_name = _element_text(obj.find("object-name"))
        member_data = _parse_member_data(_element_text(obj.find("member-data")))
        attributes = member_data.get("Attributes", {})
        if not isinstance(attributes, dict):
            attributes = {}

        display_text = str(
            member_data.get("DisplayText")
            or member_data.get("HeaderText")
            or system_name
            or "<unnamed>"
        )
        system_type = str(
            member_data.get("SystemType")
            or attributes.get("SystemName")
            or display_text
        )
        physics_type = str(
            attributes.get("PhysicsTypeDisplayString")
            or attributes.get("PhysicsType")
            or ""
        )
        analysis_type = str(
            attributes.get("AnalysisTypeDisplayString")
            or attributes.get("AnalysisType")
            or display_text
        )
        solver_type = str(
            attributes.get("SolverTypeDisplayString")
            or attributes.get("SolverType")
            or ""
        )
        directory_name = str(member_data.get("UniqueSystemDirectoryName") or system_name)
        visible = bool(member_data.get("SystemVisible", True))

        modules.append(
            AnalysisModule(
                index=len(modules) + 1,
                system_name=system_name or f"System {len(modules) + 1}",
                display_text=display_text,
                system_type=system_type,
                physics_type=physics_type,
                analysis_type=analysis_type,
                solver_type=solver_type,
                directory_name=directory_name,
                visible=visible,
            )
        )

    return modules


def run_analysis_module_operation(
    *,
    project_file: str | Path | None = None,
    system_name: str,
    analysis_hint: str | None = None,
    analysis_index: int | None = None,
    settings_update: dict | None = None,
    conditions_update: list[dict] | None = None,
    solve: bool = False,
    include_results: bool = False,
    export_dir: str | Path | None = None,
    export_text: bool = False,
    export_images: bool = False,
    export_result_paths: list[str] | None = None,
    export_all_sets: bool = False,
    export_time_range: dict | None = None,
    save_project: bool = True,
    progress_callback=None,
) -> ModuleOperationResult:
    """Read, update, or solve one Mechanical analysis system through Workbench."""
    project = require_file(_project_or_default(project_file), "Workbench project")
    if project.suffix.lower() not in {".wbpj", ".wbpz"}:
        raise ValueError(f"Expected a .wbpj or .wbpz Workbench project: {project}")
    if not system_name:
        raise ValueError("A Workbench system name is required.")

    settings_update = dict(settings_update or {})
    conditions_update = list(conditions_update or [])
    export_result_paths = list(export_result_paths or [])
    export_time_range = dict(export_time_range or {})
    analysis_index = int(analysis_index or 0)
    operation = "solve" if solve else (
        "update_settings" if settings_update or conditions_update else "read_settings"
    )
    if include_results and operation == "read_settings":
        operation = "read_results"
    if export_text or export_images:
        operation = "export_results"
        include_results = True
    component_name = "Model"

    with tempfile.TemporaryDirectory(prefix="ansys_control_") as temp_dir:
        started_at = time.time()
        temp_path = Path(temp_dir)
        report_path = temp_path / "module_operation_report.json"
        progress_path = temp_path / "module_operation_progress.json"
        journal_path = temp_path / "module_operation.wbjn"
        task = {
            "project": str(project),
            "system_name": system_name,
            "analysis_hint": analysis_hint or "",
            "analysis_index": analysis_index,
            "operation": operation,
            "settings_update": settings_update,
            "conditions_update": conditions_update,
            "perform_solve": solve,
            "include_results": include_results,
            "export_dir": str(export_dir or ""),
            "export_text": export_text,
            "export_images": export_images,
            "export_result_paths": export_result_paths,
            "export_all_sets": export_all_sets,
            "export_time_range": export_time_range,
            "report_path": str(report_path),
            "progress_path": str(progress_path),
        }

        journal_text = _workbench_module_journal(
            project=project,
            system_name=system_name,
            component_name=component_name,
            edit_before_send=component_name == "Results",
            mechanical_script=_mechanical_task_script(task),
            save_project=save_project and (solve or bool(settings_update) or bool(conditions_update)),
        )
        journal_path.write_text(journal_text, encoding="utf-8")

        completed = _run_workbench_journal_with_progress(
            journal_path=journal_path,
            project=project,
            progress_path=progress_path,
            progress_callback=progress_callback,
        )
        if completed.returncode != 0:
            workbench_error = _recent_workbench_log_excerpt(started_at)
            raise RuntimeError(
                _format_workbench_failure(
                    f"RunWB2 failed with exit code {completed.returncode}",
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    workbench_error=workbench_error,
                )
            )
        if not report_path.exists():
            workbench_error = _recent_workbench_log_excerpt(started_at)
            raise RuntimeError(
                _format_workbench_failure(
                    "Mechanical did not create an operation report.",
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    workbench_error=workbench_error,
                )
            )

        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("error"):
            raise RuntimeError(str(report["error"]))

        return ModuleOperationResult(
            project=project,
            system_name=system_name,
            operation=operation,
            report=report,
            journal_path=journal_path,
        )


def run_current_mechanical_operation(
    *,
    system_name: str,
    analysis_hint: str | None = None,
    analysis_index: int | None = None,
    settings_update: dict | None = None,
    conditions_update: list[dict] | None = None,
    solve: bool = False,
    include_results: bool = False,
    export_dir: str | Path | None = None,
    export_text: bool = False,
    export_images: bool = False,
    export_result_paths: list[str] | None = None,
    export_all_sets: bool = False,
    export_time_range: dict | None = None,
    port: int | None = None,
    progress_callback=None,
) -> ModuleOperationResult:
    """Read, update, or solve by connecting to the currently open Mechanical session.

    This function never launches Workbench or Mechanical. The target Mechanical
    process must already be running with its PyMechanical gRPC server enabled.
    """
    if not system_name:
        raise ValueError("A Workbench system name is required.")

    settings_update = dict(settings_update or {})
    conditions_update = list(conditions_update or [])
    export_result_paths = list(export_result_paths or [])
    export_time_range = dict(export_time_range or {})
    analysis_index = int(analysis_index or 0)
    operation = "solve" if solve else (
        "update_settings" if settings_update or conditions_update else "read_settings"
    )
    if include_results and operation == "read_settings":
        operation = "read_results"
    if export_text or export_images:
        operation = "export_results"
    _emit_progress(progress_callback, "查找当前 Mechanical", "正在查找可连接的 Mechanical 会话")
    port = _resolve_mechanical_port(port)
    if port is None:
        raise RuntimeError(
            "当前没有可连接的 Mechanical 会话。请先用本程序“打开 Mechanical”打开工程，"
            "或先打开带 PyMechanical/gRPC 服务的 Mechanical。读取设置时程序不会自动打开新的 Mechanical。"
        )

    port_owner = _local_port_owner(port)
    if port_owner and not _port_owner_looks_mechanical(port_owner):
        raise RuntimeError(
            f"端口 {port} 当前被 {port_owner} 占用，不是 Mechanical gRPC 服务。"
            "请先打开可连接的当前 Mechanical，或换一个 Mechanical gRPC 端口。"
        )
    if not port_owner and not _is_local_port_open(port):
        raise RuntimeError(
            "当前没有可连接的 Mechanical 会话。请先打开带 PyMechanical/gRPC "
            f"服务的 Mechanical（默认端口 {port}），再读取设置。程序不会自动打开新的 Mechanical。"
        )

    with tempfile.TemporaryDirectory(prefix="ansys_control_current_") as temp_dir:
        temp_path = Path(temp_dir)
        report_path = temp_path / "current_mechanical_report.json"
        progress_path = temp_path / "current_mechanical_progress.json"
        task = {
            "project": "<current Mechanical>",
            "system_name": system_name,
            "analysis_hint": analysis_hint or "",
            "analysis_index": analysis_index,
            "operation": operation,
            "settings_update": settings_update,
            "conditions_update": conditions_update,
            "perform_solve": solve,
            "include_results": include_results,
            "export_dir": str(export_dir or ""),
            "export_text": export_text,
            "export_images": export_images,
            "export_result_paths": export_result_paths,
            "export_all_sets": export_all_sets,
            "export_time_range": export_time_range,
            "report_path": str(report_path),
            "progress_path": str(progress_path),
        }
        script = _mechanical_task_script(task)

        try:
            from ansys.mechanical.core import connect_to_mechanical

            _emit_progress(progress_callback, "连接 Mechanical", f"正在连接当前 Mechanical 端口 {port}")
            mechanical = connect_to_mechanical(
                port=port,
                connect_timeout=8,
                clear_on_connect=False,
                cleanup_on_exit=False,
            )
        except Exception as exc:
            raise RuntimeError(
                "当前没有可连接的 Mechanical 会话。请先打开带 PyMechanical/gRPC "
                f"服务的 Mechanical（默认端口 {port}），再读取设置。程序不会自动打开新的 Mechanical。"
            ) from exc

        try:
            _run_python_script_with_progress(
                mechanical,
                script,
                progress_path,
                progress_callback,
            )
        except Exception as exc:
            raise RuntimeError(
                "已连接到当前 Mechanical，但执行读取/保存脚本失败。"
            ) from exc

        if not report_path.exists():
            raise RuntimeError("当前 Mechanical 没有返回设置报告。")

        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("error"):
            raise RuntimeError(str(report["error"]))

        return ModuleOperationResult(
            project=Path("<current Mechanical>"),
            system_name=system_name,
            operation=operation,
            report=report,
            journal_path=report_path,
        )


def _emit_progress(progress_callback, stage: str, status: str = "", **extra) -> None:
    if progress_callback is None:
        return
    payload = {"stage": stage, "status": status}
    payload.update(extra)
    try:
        progress_callback(payload)
    except Exception:
        pass


def _run_python_script_with_progress(
    mechanical,
    script: str,
    progress_path: Path,
    progress_callback,
) -> None:
    result: dict[str, object] = {"error": None}

    def target() -> None:
        try:
            mechanical.run_python_script(script)
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()

    last_text = ""
    while thread.is_alive():
        last_text = _poll_progress_file(progress_path, progress_callback, last_text)
        time.sleep(0.5)

    thread.join()
    _poll_progress_file(progress_path, progress_callback, last_text)
    if result["error"] is not None:
        raise result["error"]


def _poll_progress_file(
    progress_path: Path,
    progress_callback,
    last_text: str,
) -> str:
    if progress_callback is None or not progress_path.exists():
        return last_text
    try:
        current_text = progress_path.read_text(encoding="utf-8")
    except OSError:
        return last_text
    if current_text == last_text:
        return last_text
    try:
        payload = json.loads(current_text)
    except json.JSONDecodeError:
        return last_text
    _emit_progress(
        progress_callback,
        str(payload.get("stage", "")),
        str(payload.get("status", "")),
        analysis_state=str(payload.get("analysis_state", "")),
        solution_state=str(payload.get("solution_state", "")),
    )
    return current_text


def _recent_workbench_log_excerpt(started_at: float) -> str:
    log_dir = Path(tempfile.gettempdir()) / "WorkbenchLogs"
    if not log_dir.exists():
        return ""

    candidates = []
    for log_path in log_dir.glob("CoreEvents*.log"):
        try:
            if log_path.stat().st_mtime >= started_at - 2:
                candidates.append(log_path)
        except OSError:
            continue
    if not candidates:
        return ""

    latest = max(candidates, key=lambda path: path.stat().st_mtime)
    try:
        lines = latest.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ""

    interesting = [
        line
        for line in lines
        if any(
            token in line
            for token in (
                "MechanicalError",
                "Unable to",
                "Unexpected error",
                "locked by",
                "lost communication",
                "强迫关闭",
                "拒绝",
            )
        )
    ]
    excerpt_lines = interesting[-8:] or lines[-8:]
    if not excerpt_lines:
        return ""
    return "\nWorkbench log:\n" + "\n".join(excerpt_lines)


def _format_workbench_failure(
    title: str,
    *,
    stdout: str = "",
    stderr: str = "",
    workbench_error: str = "",
) -> str:
    details = f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}{workbench_error}"
    match = _WORKBENCH_LOCK_RE.search(details)
    if match:
        owner = match.group("owner").strip()
        locked_at = match.group("time").strip()
        return (
            "Workbench 工程当前被锁定，无法执行读取、求解或导出。\n"
            f"锁定会话: {owner}\n"
            f"锁定时间: {locked_at}\n"
            "请先在已经打开的 Workbench/Mechanical 中保存并关闭该工程；"
            "如果确认没有 Ansys 进程占用，再重新运行当前操作。"
            "程序不会自动删除锁信息，避免破坏工程数据。\n\n"
            f"原始信息:\n{details}"
        )
    return f"{title}\n{details}"


def _is_local_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.5):
            return True
    except OSError:
        return False


def _resolve_mechanical_port(port: int | None) -> int | None:
    if port is not None:
        return port

    for candidate in _list_mechanical_listener_ports():
        if _can_connect_pymechanical_port(candidate):
            return candidate

    default_port = 10000
    owner = _local_port_owner(default_port)
    if owner and _port_owner_looks_mechanical(owner) and _can_connect_pymechanical_port(default_port):
        return default_port
    if not owner and _is_local_port_open(default_port) and _can_connect_pymechanical_port(default_port):
        return default_port
    return None


def _can_connect_pymechanical_port(port: int) -> bool:
    try:
        from ansys.mechanical.core import connect_to_mechanical

        connect_to_mechanical(
            port=port,
            connect_timeout=2,
            clear_on_connect=False,
            cleanup_on_exit=False,
        )
        return True
    except Exception:
        return False


def _list_mechanical_listener_ports() -> list[int]:
    try:
        import psutil
    except Exception:
        return []

    try:
        connections = psutil.net_connections(kind="tcp")
    except Exception:
        return []

    ports: list[int] = []
    for connection in connections:
        if connection.status != psutil.CONN_LISTEN:
            continue
        local_address = getattr(connection, "laddr", None)
        port = getattr(local_address, "port", None)
        if not port:
            continue
        owner = _process_owner_text(connection.pid)
        if owner and _port_owner_looks_mechanical(owner):
            ports.append(int(port))
    return sorted(set(ports))


def _local_port_owner(port: int) -> str | None:
    try:
        import psutil
    except Exception:
        return None

    try:
        connections = psutil.net_connections(kind="tcp")
    except Exception:
        return None

    for connection in connections:
        local_address = getattr(connection, "laddr", None)
        local_port = getattr(local_address, "port", None)
        if local_port != port or connection.status != psutil.CONN_LISTEN:
            continue
        pid = connection.pid
        return _process_owner_text(pid) or "unknown process"
    return None


def _process_owner_text(pid: int | None) -> str | None:
    if pid is None:
        return None
    try:
        import psutil
    except Exception:
        return f"PID {pid}"

    try:
        process = psutil.Process(pid)
        return f"{process.name()} (PID {pid})"
    except Exception:
        return f"PID {pid}"


def _port_owner_looks_mechanical(owner: str) -> bool:
    owner_lower = owner.lower()
    return any(
        name in owner_lower
        for name in ("ansyswbu", "mechanical", "runwb2", "wbapp", "ansys.inc")
    )


def read_current_mechanical_settings(
    *,
    system_name: str,
    analysis_hint: str | None = None,
    analysis_index: int | None = None,
    port: int | None = None,
    progress_callback=None,
) -> ModuleOperationResult:
    return run_current_mechanical_operation(
        system_name=system_name,
        analysis_hint=analysis_hint,
        analysis_index=analysis_index,
        settings_update=None,
        solve=False,
        port=port,
        progress_callback=progress_callback,
    )


def update_current_mechanical_settings(
    *,
    system_name: str,
    analysis_hint: str | None = None,
    analysis_index: int | None = None,
    settings_update: dict,
    conditions_update: list[dict] | None = None,
    port: int | None = None,
    progress_callback=None,
) -> ModuleOperationResult:
    return run_current_mechanical_operation(
        system_name=system_name,
        analysis_hint=analysis_hint,
        analysis_index=analysis_index,
        settings_update=settings_update,
        conditions_update=conditions_update,
        solve=False,
        port=port,
        progress_callback=progress_callback,
    )


def read_current_solution_results(
    *,
    system_name: str,
    analysis_hint: str | None = None,
    analysis_index: int | None = None,
    port: int | None = None,
    progress_callback=None,
) -> ModuleOperationResult:
    return run_current_mechanical_operation(
        system_name=system_name,
        analysis_hint=analysis_hint,
        analysis_index=analysis_index,
        settings_update=None,
        conditions_update=None,
        solve=False,
        include_results=True,
        port=port,
        progress_callback=progress_callback,
    )


def export_current_solution_results(
    *,
    system_name: str,
    analysis_hint: str | None = None,
    analysis_index: int | None = None,
    export_dir: str | Path,
    export_text: bool = True,
    export_images: bool = False,
    export_result_paths: list[str] | None = None,
    export_all_sets: bool = False,
    export_time_range: dict | None = None,
    port: int | None = None,
    progress_callback=None,
) -> ModuleOperationResult:
    return run_current_mechanical_operation(
        system_name=system_name,
        analysis_hint=analysis_hint,
        analysis_index=analysis_index,
        settings_update=None,
        conditions_update=None,
        solve=False,
        include_results=True,
        export_dir=export_dir,
        export_text=export_text,
        export_images=export_images,
        export_result_paths=export_result_paths,
        export_all_sets=export_all_sets,
        export_time_range=export_time_range,
        port=port,
        progress_callback=progress_callback,
    )


def solve_current_mechanical_analysis(
    *,
    system_name: str,
    analysis_hint: str | None = None,
    analysis_index: int | None = None,
    settings_update: dict | None = None,
    conditions_update: list[dict] | None = None,
    port: int | None = None,
    progress_callback=None,
) -> ModuleOperationResult:
    return run_current_mechanical_operation(
        system_name=system_name,
        analysis_hint=analysis_hint,
        analysis_index=analysis_index,
        settings_update=settings_update,
        conditions_update=conditions_update,
        solve=True,
        include_results=True,
        port=port,
        progress_callback=progress_callback,
    )


def read_analysis_module_settings(
    project_file: str | Path | None = None,
    *,
    system_name: str,
    analysis_hint: str | None = None,
    analysis_index: int | None = None,
    progress_callback=None,
) -> ModuleOperationResult:
    return run_analysis_module_operation(
        project_file=project_file,
        system_name=system_name,
        analysis_hint=analysis_hint,
        analysis_index=analysis_index,
        settings_update=None,
        solve=False,
        save_project=False,
        progress_callback=progress_callback,
    )


def update_analysis_module_settings(
    project_file: str | Path | None = None,
    *,
    system_name: str,
    analysis_hint: str | None = None,
    analysis_index: int | None = None,
    settings_update: dict,
    conditions_update: list[dict] | None = None,
    progress_callback=None,
) -> ModuleOperationResult:
    return run_analysis_module_operation(
        project_file=project_file,
        system_name=system_name,
        analysis_hint=analysis_hint,
        analysis_index=analysis_index,
        settings_update=settings_update,
        conditions_update=conditions_update,
        solve=False,
        save_project=True,
        progress_callback=progress_callback,
    )


def read_analysis_module_results(
    project_file: str | Path | None = None,
    *,
    system_name: str,
    analysis_hint: str | None = None,
    analysis_index: int | None = None,
    progress_callback=None,
) -> ModuleOperationResult:
    return run_analysis_module_operation(
        project_file=project_file,
        system_name=system_name,
        analysis_hint=analysis_hint,
        analysis_index=analysis_index,
        settings_update=None,
        solve=False,
        include_results=True,
        save_project=False,
        progress_callback=progress_callback,
    )


def export_analysis_module_results(
    project_file: str | Path | None = None,
    *,
    system_name: str,
    analysis_hint: str | None = None,
    analysis_index: int | None = None,
    export_dir: str | Path,
    export_text: bool = True,
    export_images: bool = False,
    export_result_paths: list[str] | None = None,
    export_all_sets: bool = False,
    export_time_range: dict | None = None,
    progress_callback=None,
) -> ModuleOperationResult:
    return run_analysis_module_operation(
        project_file=project_file,
        system_name=system_name,
        analysis_hint=analysis_hint,
        analysis_index=analysis_index,
        settings_update=None,
        conditions_update=None,
        solve=False,
        include_results=True,
        export_dir=export_dir,
        export_text=export_text,
        export_images=export_images,
        export_result_paths=export_result_paths,
        export_all_sets=export_all_sets,
        export_time_range=export_time_range,
        save_project=False,
        progress_callback=progress_callback,
    )


def solve_analysis_module(
    project_file: str | Path | None = None,
    *,
    system_name: str,
    analysis_hint: str | None = None,
    analysis_index: int | None = None,
    settings_update: dict | None = None,
    conditions_update: list[dict] | None = None,
    progress_callback=None,
) -> ModuleOperationResult:
    return run_analysis_module_operation(
        project_file=project_file,
        system_name=system_name,
        analysis_hint=analysis_hint,
        analysis_index=analysis_index,
        settings_update=settings_update,
        conditions_update=conditions_update,
        solve=True,
        save_project=True,
        progress_callback=progress_callback,
    )


def run_workbench_journal(
    journal_path: str | Path,
    *,
    project_file: str | Path | None = None,
    batch: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a Workbench journal against a Workbench project."""
    runwb2 = require_file(RUNWB2, "RunWB2")
    project = require_file(_project_or_default(project_file), "Workbench project")
    journal = require_file(Path(journal_path), "Workbench journal")

    cmd = [
        str(runwb2),
        "-B" if batch else "-I",
        "-F",
        str(project),
        "-R",
        str(journal),
    ]
    result = subprocess.run(
        cmd,
        cwd=str(_project_cwd(project)),
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        workbench_error = _recent_workbench_log_excerpt(time.time() - 30)
        raise RuntimeError(
            _format_workbench_failure(
                f"RunWB2 failed with exit code {result.returncode}",
                stdout=result.stdout,
                stderr=result.stderr,
                workbench_error=workbench_error,
            )
        )
    return result


def open_mechanical_project(
    journal_path: str | Path | None = None,
    *,
    project_file: str | Path | None = None,
) -> subprocess.Popen:
    """Open a Workbench project in the Mechanical UI."""
    runwb2 = require_file(RUNWB2, "RunWB2")
    project = require_file(_project_or_default(project_file), "Workbench project")
    journal = Path(journal_path) if journal_path else DEFAULT_OPEN_JOURNAL

    cmd = [str(runwb2), "-I", "-F", str(project)]
    if journal.exists():
        cmd.extend(["-R", str(journal)])

    return subprocess.Popen(cmd, cwd=str(_project_cwd(project)))


def open_workbench_mechanical(
    *,
    project_file: str | Path | None = None,
    system_name: str,
) -> dict:
    """Open Mechanical from the selected Workbench system.

    This intentionally opens Workbench first and then calls the selected
    system's Model.Edit(), so Mechanical keeps the Workbench project context.
    """
    runwb2 = require_file(RUNWB2, "RunWB2")
    project = require_file(_project_or_default(project_file), "Workbench project")
    if not system_name:
        raise ValueError("A Workbench system name is required.")

    tmp_dir = WORKSPACE / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    safe_system = re.sub(r"[^0-9A-Za-z_.-]+", "_", system_name).strip("_") or "system"
    journal_path = tmp_dir / f"open_workbench_mechanical_{safe_system}_{int(time.time())}.wbjn"
    journal_text = f"""# encoding: utf-8
SetScriptVersion(Version="12.1")

system1 = GetSystem(Name={_workbench_string(system_name)})
model1 = system1.GetContainer(ComponentName="Model")
model1.Edit()
"""
    journal_path.write_text(journal_text, encoding="utf-8")

    cmd = [
        str(runwb2),
        "-I",
        "-F",
        str(project),
        "-R",
        str(journal_path),
    ]
    process = subprocess.Popen(cmd, cwd=str(_project_cwd(project)))
    return {
        "process": process,
        "project": project,
        "system_name": system_name,
        "journal_path": journal_path,
        "cmd": cmd,
    }


def save_and_close_workbench_mechanical(
    *,
    project_file: str | Path | None = None,
    system_name: str,
) -> dict:
    """Request a normal save and close for a Workbench-opened Mechanical model."""
    project = require_file(_project_or_default(project_file), "Workbench project")
    if not system_name:
        raise ValueError("A Workbench system name is required.")

    with tempfile.TemporaryDirectory(prefix="ansys_control_close_") as temp_dir:
        journal_path = Path(temp_dir) / "close_workbench_mechanical.wbjn"
        journal_text = f"""# encoding: utf-8
SetScriptVersion(Version="12.1")

system1 = GetSystem(Name={_workbench_string(system_name)})
model1 = system1.GetContainer(ComponentName="Model")
try:
    model1.Exit()
except Exception:
    pass
Save(FilePath={_workbench_string(project)}, Overwrite=True)
"""
        journal_path.write_text(journal_text, encoding="utf-8")
        completed = run_workbench_journal(
            journal_path,
            project_file=project,
            batch=False,
            check=False,
        )
        if completed.returncode != 0:
            return {
                "saved": False,
                "closed": False,
                "status": (
                    "Workbench 保存/关闭脚本未成功执行。请在已打开的 Workbench/Mechanical "
                    "中手动保存并关闭；程序没有强制终止进程。"
                ),
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "returncode": completed.returncode,
            }
        return {
            "saved": True,
            "closed": True,
            "status": "已请求 Workbench 保存工程并正常关闭 Mechanical",
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "returncode": completed.returncode,
        }
