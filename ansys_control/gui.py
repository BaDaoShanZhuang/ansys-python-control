from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSpinBox,
    QSplitter,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .config import MECHANICAL_EXE, OPTICSTUDIO_EXE, PROJECT_FILE, RUNWB2, WORKSPACE
from .mechanical import (
    MECHANICAL_DATABASE_SUFFIXES,
    find_project_mechdb,
    launch_mechanical_project_session,
    mechanical_session_port,
    save_and_close_mechanical_session,
)
from .workbench import (
    AnalysisModule,
    ModuleOperationResult,
    export_current_solution_results,
    read_project_analysis_modules,
    read_current_mechanical_settings,
    read_current_solution_results,
    solve_current_mechanical_analysis,
    update_current_mechanical_settings,
)
from .zemax import open_opticstudio


ZEMAX_PROCESS_NAMES = ["OpticStudio.exe"]
APP_NAME = "Windows端"
APP_VERSION = "V26.5.10"
APP_TITLE = f"{APP_NAME} {APP_VERSION}"
MECHANICAL_REQUIRED_MESSAGE = (
    "请先点击“打开 Mechanical”，用当前工程的 Mechanical database（.mechdb/.mechdat）启动 Mechanical。"
    "读取设置、求解、读取结果和导出结果都只连接当前 Mechanical 会话，不再重新打开 Workbench。"
)


def package_available(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except ModuleNotFoundError:
        return False


def process_ids_by_name(process_names: list[str]) -> set[int]:
    quoted = ",".join(f"'{name}'" for name in process_names)
    command = (
        f"$names = @({quoted}); "
        "Get-Process -ErrorAction SilentlyContinue "
        "| Where-Object { $names -contains $_.ProcessName } "
        "| Select-Object -ExpandProperty Id"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            text=True,
            capture_output=True,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        return set()
    ids: set[int] = set()
    for line in result.stdout.splitlines():
        try:
            ids.add(int(line.strip()))
        except ValueError:
            continue
    return ids


class StatusRow:
    def __init__(self, name: str, value: str, ok: bool) -> None:
        self.name = name
        self.value = value
        self.ok = ok


class MechanicalOperationWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(object)

    def __init__(
        self,
        operation: str,
        project: Path,
        system_name: str,
        analysis_hint: str,
        analysis_index: int | None,
        port: int | None,
        settings_update: dict | None = None,
        conditions_update: list[dict] | None = None,
        export_dir: Path | None = None,
        export_text: bool = False,
        export_images: bool = False,
        export_result_paths: list[str] | None = None,
        export_all_sets: bool = False,
        export_time_range: dict | None = None,
    ) -> None:
        super().__init__()
        self.operation = operation
        self.project = project
        self.system_name = system_name
        self.analysis_hint = analysis_hint
        self.analysis_index = analysis_index
        self.port = port
        self.settings_update = settings_update or {}
        self.conditions_update = conditions_update or []
        self.export_dir = export_dir
        self.export_text = export_text
        self.export_images = export_images
        self.export_result_paths = export_result_paths or []
        self.export_all_sets = export_all_sets
        self.export_time_range = export_time_range or {}

    @Slot()
    def run(self) -> None:
        try:
            self.progress.emit(
                {
                    "stage": "准备操作",
                    "status": f"{self.system_name} / {self.analysis_hint}: {self.operation}",
                }
            )
            if self.port is None:
                raise RuntimeError(MECHANICAL_REQUIRED_MESSAGE)
            if self.operation == "read":
                result = read_current_mechanical_settings(
                    system_name=self.system_name,
                    analysis_hint=self.analysis_hint,
                    analysis_index=self.analysis_index,
                    port=self.port,
                    progress_callback=self.progress.emit,
                )
            elif self.operation == "read_results":
                result = read_current_solution_results(
                    system_name=self.system_name,
                    analysis_hint=self.analysis_hint,
                    analysis_index=self.analysis_index,
                    port=self.port,
                    progress_callback=self.progress.emit,
                )
            elif self.operation == "export_results":
                if self.export_dir is None:
                    raise ValueError("Missing export directory.")
                result = export_current_solution_results(
                    system_name=self.system_name,
                    analysis_hint=self.analysis_hint,
                    analysis_index=self.analysis_index,
                    export_dir=self.export_dir,
                    export_text=self.export_text,
                    export_images=self.export_images,
                    export_result_paths=self.export_result_paths,
                    export_all_sets=self.export_all_sets,
                    export_time_range=self.export_time_range,
                    port=self.port,
                    progress_callback=self.progress.emit,
                )
            elif self.operation == "update":
                result = update_current_mechanical_settings(
                    system_name=self.system_name,
                    analysis_hint=self.analysis_hint,
                    analysis_index=self.analysis_index,
                    settings_update=self.settings_update,
                    conditions_update=self.conditions_update,
                    port=self.port,
                    progress_callback=self.progress.emit,
                )
            elif self.operation == "solve":
                result = solve_current_mechanical_analysis(
                    system_name=self.system_name,
                    analysis_hint=self.analysis_hint,
                    analysis_index=self.analysis_index,
                    settings_update=self.settings_update,
                    conditions_update=self.conditions_update,
                    port=self.port,
                    progress_callback=self.progress.emit,
                )
            else:
                raise ValueError(f"Unknown operation: {self.operation}")
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.finished.emit(result)


class MechanicalLaunchWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(object)

    def __init__(self, project: Path, system_name: str, analysis_hint: str) -> None:
        super().__init__()
        self.project = project
        self.system_name = system_name
        self.analysis_hint = analysis_hint

    @Slot()
    def run(self) -> None:
        session = None
        try:
            mechdb = find_project_mechdb(self.project)
            self.progress.emit(
                {
                    "stage": "启动 Mechanical",
                    "status": f"正在用 database 打开 {mechdb.name}",
                }
            )
            session = launch_mechanical_project_session(
                self.project,
                cleanup_on_exit=False,
            )
            port = mechanical_session_port(session)
            if port is None:
                try:
                    session.exit(force=False)
                except Exception:
                    pass
                raise RuntimeError("Mechanical 已启动，但没有返回 PyMechanical/gRPC 端口，无法读取或求解当前 database。")
            self.progress.emit(
                {
                    "stage": "Mechanical 已连接",
                    "status": f"端口 {port}",
                }
            )
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.finished.emit(
                {
                    "session": session,
                    "port": port,
                    "project": self.project,
                    "database": mechdb,
                    "system_name": self.system_name,
                    "analysis_hint": self.analysis_hint,
                }
            )


class MechanicalCloseWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(object)

    def __init__(
        self,
        session,
        port: int | None,
        project: Path | None = None,
        system_name: str | None = None,
    ) -> None:
        super().__init__()
        self.session = session
        self.port = port
        self.project = project
        self.system_name = system_name or ""

    @Slot()
    def run(self) -> None:
        try:
            result = save_and_close_mechanical_session(
                self.session,
                port=self.port,
                progress_callback=self.progress.emit,
            )
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.finished.emit(result)


class AnalysisSettingsDialog(QDialog):
    save_requested = Signal(dict)

    def __init__(
        self,
        module: AnalysisModule,
        result: ModuleOperationResult,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.module = module
        self.setWindowTitle(f"{module.system_name} 分析设置")
        self.resize(980, 720)
        self.setting_infos: list[dict] = []
        self.original_values: dict[str, str] = {}
        self.original_condition_values: dict[tuple[str, str], str] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        self.summary_label = QLabel()
        self.summary_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.summary_label)

        self.controls: dict[str, QComboBox | QSpinBox | QLineEdit] = {}
        self.condition_controls: dict[tuple[str, str], QComboBox | QSpinBox | QLineEdit] = {}
        root.addWidget(QLabel("分析设置"))
        self.settings_table = QTableWidget(0, 5)
        self.settings_table.setHorizontalHeaderLabels(
            ["显示名", "Mechanical API", "当前值", "保存为", "类型"]
        )
        self.settings_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.settings_table.setAlternatingRowColors(True)
        self.settings_table.verticalHeader().setVisible(False)
        self.settings_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.settings_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.settings_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        root.addWidget(self.settings_table, stretch=2)

        root.addWidget(QLabel("已加入的分析条件"))
        self.conditions_table = QTableWidget(0, 6)
        self.conditions_table.setHorizontalHeaderLabels(
            ["条件", "类型", "状态", "属性", "当前值", "保存为"]
        )
        self.conditions_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.conditions_table.setAlternatingRowColors(True)
        self.conditions_table.verticalHeader().setVisible(False)
        self.conditions_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.conditions_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.conditions_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.conditions_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        root.addWidget(self.conditions_table, stretch=2)

        root.addWidget(QLabel("Mechanical 消息"))
        self.messages_table = QTableWidget(0, 2)
        self.messages_table.setHorizontalHeaderLabels(["级别", "消息"])
        self.messages_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.messages_table.setAlternatingRowColors(True)
        self.messages_table.verticalHeader().setVisible(False)
        self.messages_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.messages_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        root.addWidget(self.messages_table, stretch=1)

        buttons = QHBoxLayout()
        self.save_button = QPushButton("保存设置")
        self.save_button.setIcon(self.style().standardIcon(QStyle.SP_DialogSaveButton))
        self.save_button.clicked.connect(self.emit_save_requested)

        self.close_button = QPushButton("关闭")
        self.close_button.setIcon(self.style().standardIcon(QStyle.SP_DialogCloseButton))
        self.close_button.clicked.connect(self.close)

        buttons.addStretch(1)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.close_button)
        root.addLayout(buttons)

        self.apply_result(result)
        self.update_time_export_controls(False)

    def _build_control(self, setting: dict) -> QComboBox | QSpinBox | QLineEdit:
        kind = setting.get("kind", "text")
        value = str(setting.get("value", ""))
        options = [str(item) for item in setting.get("options") or []]
        editable = bool(setting.get("editable", True))

        if options:
            combo = QComboBox()
            combo.addItems(options)
            index = combo.findText(value)
            if index >= 0:
                combo.setCurrentIndex(index)
            combo.setEnabled(editable)
            return combo

        if kind == "int":
            spin = QSpinBox()
            spin.setRange(-2147483648, 2147483647)
            try:
                spin.setValue(int(float(value)))
            except ValueError:
                spin.setValue(0)
            spin.setEnabled(editable)
            return spin

        edit = QLineEdit(value)
        edit.setEnabled(editable)
        return edit

    def _control_value(self, control: QComboBox | QSpinBox | QLineEdit) -> str:
        if isinstance(control, QSpinBox):
            return str(control.value())
        if isinstance(control, QComboBox):
            return control.currentText()
        return control.text()

    def apply_result(self, result: ModuleOperationResult) -> None:
        condition_count = len(result.report.get("conditions") or [])
        self.summary_label.setText(
            "系统: {0}    模块: {1}    分析状态: {2}    Solution: {3}    分析条件: {4}".format(
                result.system_name,
                result.report.get("analysis_name", self.module.display_text),
                result.report.get("analysis_state", ""),
                result.report.get("solution_state", ""),
                condition_count,
            )
        )

        self.controls.clear()
        self.condition_controls.clear()
        self.original_values.clear()
        self.original_condition_values.clear()
        self.setting_infos = [dict(item) for item in result.report.get("settings") or []]
        self.settings_table.setRowCount(len(self.setting_infos))
        for row, setting in enumerate(self.setting_infos):
            api_name = str(setting.get("api_name", ""))
            current_value = str(setting.get("value", ""))
            self.original_values[api_name] = current_value
            control = self._build_control(setting)
            self.controls[api_name] = control

            values = [
                str(setting.get("display_name", api_name)),
                api_name,
                current_value,
                "",
                str(setting.get("type") or setting.get("kind", "")),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 1:
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.settings_table.setItem(row, column, item)
            self.settings_table.setCellWidget(row, 3, control)

        self.settings_table.resizeRowsToContents()

        condition_rows: list[tuple[str, str, str, str, str, dict | None]] = []
        for condition in result.report.get("conditions") or []:
            condition_name = str(condition.get("path") or condition.get("name") or "")
            condition_type = str(condition.get("category") or condition.get("type") or "")
            condition_state = str(condition.get("state") or "")
            properties = condition.get("properties") or []
            if not properties:
                condition_rows.append((condition_name, condition_type, condition_state, "", "", None))
                continue
            for prop in properties:
                api_name = str(prop.get("api_name") or "")
                property_name = str(prop.get("display_name") or prop.get("api_name") or "")
                property_value = str(prop.get("value", ""))
                condition_rows.append(
                    (
                        condition_name,
                        condition_type,
                        condition_state,
                        property_name,
                        property_value,
                        dict(prop, path=condition_name, api_name=api_name),
                    )
                )

        self.conditions_table.setRowCount(len(condition_rows))
        for row, values in enumerate(condition_rows):
            for column, value in enumerate(values[:5]):
                self.conditions_table.setItem(row, column, QTableWidgetItem(value))
            prop = values[5]
            if prop is None:
                continue
            path = str(prop.get("path", ""))
            api_name = str(prop.get("api_name", ""))
            key = (path, api_name)
            current_value = str(prop.get("value", ""))
            self.original_condition_values[key] = current_value
            control = self._build_control(prop)
            self.condition_controls[key] = control
            self.conditions_table.setCellWidget(row, 5, control)
        self.conditions_table.resizeRowsToContents()

        messages = result.report.get("messages") or []
        self.messages_table.setRowCount(len(messages))
        for row, message in enumerate(messages):
            values = [message.get("severity", ""), message.get("text", "")]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                self.messages_table.setItem(row, column, item)
        self.messages_table.resizeRowsToContents()

    def settings_update(self) -> dict:
        values: dict[str, object] = {}
        for key, control in self.controls.items():
            value = self._control_value(control)
            if value != self.original_values.get(key, ""):
                values[key] = value
        return values

    def conditions_update(self) -> list[dict]:
        values: list[dict] = []
        for (path, api_name), control in self.condition_controls.items():
            value = self._control_value(control)
            if value != self.original_condition_values.get((path, api_name), ""):
                values.append(
                    {
                        "path": path,
                        "api_name": api_name,
                        "value": value,
                    }
                )
        return values

    def emit_save_requested(self) -> None:
        self.save_requested.emit(
            {
                "settings": self.settings_update(),
                "conditions": self.conditions_update(),
            }
        )

    def set_busy(self, busy: bool) -> None:
        self.save_button.setEnabled(not busy)
        self.close_button.setEnabled(not busy)


class SolutionResultsDialog(QDialog):
    export_requested = Signal(dict)

    def __init__(
        self,
        module: AnalysisModule,
        result: ModuleOperationResult,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.module = module
        self.result = result
        self.setWindowTitle(f"{module.system_name} 求解结果")
        self.resize(1120, 760)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        self.summary_label = QLabel()
        self.summary_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.summary_label)

        root.addWidget(QLabel("求解设置"))
        self.settings_table = QTableWidget(0, 4)
        self.settings_table.setHorizontalHeaderLabels(["显示名", "Mechanical API", "当前值", "类型"])
        self._prepare_table(self.settings_table)
        self.settings_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        root.addWidget(self.settings_table, stretch=2)

        root.addWidget(QLabel("求解设置的条件"))
        self.conditions_table = QTableWidget(0, 5)
        self.conditions_table.setHorizontalHeaderLabels(["条件", "类型", "状态", "属性", "当前值"])
        self._prepare_table(self.conditions_table)
        self.conditions_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.conditions_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        root.addWidget(self.conditions_table, stretch=2)

        root.addWidget(QLabel("所有求解结果"))
        self.result_paths: list[str] = []
        self.result_tabs = QTabWidget()
        root.addWidget(self.result_tabs, stretch=3)

        export_options = QHBoxLayout()
        buttons = QHBoxLayout()
        self.export_all_sets_checkbox = QCheckBox("按时间/结果集导出")
        self.export_all_sets_checkbox.setToolTip("勾选后可按已有结果集导出；填写开始、结束、间隔后按指定时间点导出。")
        self.export_all_sets_checkbox.toggled.connect(self.update_time_export_controls)
        self.time_start_edit = QLineEdit()
        self.time_start_edit.setPlaceholderText("开始")
        self.time_start_edit.setFixedWidth(115)
        self.time_start_edit.setToolTip("开始时间，例如 0 或 0 [s]")
        self.time_end_edit = QLineEdit()
        self.time_end_edit.setPlaceholderText("结束")
        self.time_end_edit.setFixedWidth(115)
        self.time_end_edit.setToolTip("结束时间，例如 1 或 1 [s]")
        self.time_step_edit = QLineEdit()
        self.time_step_edit.setPlaceholderText("间隔")
        self.time_step_edit.setFixedWidth(115)
        self.time_step_edit.setToolTip("时间间隔，例如 0.1 或 0.1 [s]")
        export_options.addWidget(self.export_all_sets_checkbox)
        export_options.addWidget(QLabel("开始"))
        export_options.addWidget(self.time_start_edit)
        export_options.addWidget(QLabel("结束"))
        export_options.addWidget(self.time_end_edit)
        export_options.addWidget(QLabel("间隔"))
        export_options.addWidget(self.time_step_edit)
        export_options.addStretch(1)
        root.addLayout(export_options)

        self.export_text_button = QPushButton("导出当前 TXT")
        self.export_text_button.setIcon(self.style().standardIcon(QStyle.SP_DialogSaveButton))
        self.export_text_button.clicked.connect(lambda: self.emit_export_requested(True, False))

        self.export_images_button = QPushButton("导出当前图片")
        self.export_images_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogInfoView))
        self.export_images_button.clicked.connect(lambda: self.emit_export_requested(False, True))

        self.export_all_button = QPushButton("导出当前 TXT + 图片")
        self.export_all_button.setIcon(self.style().standardIcon(QStyle.SP_DirIcon))
        self.export_all_button.clicked.connect(lambda: self.emit_export_requested(True, True))

        self.close_button = QPushButton("关闭")
        self.close_button.setIcon(self.style().standardIcon(QStyle.SP_DialogCloseButton))
        self.close_button.clicked.connect(self.close)
        buttons.addStretch(1)
        buttons.addWidget(self.export_text_button)
        buttons.addWidget(self.export_images_button)
        buttons.addWidget(self.export_all_button)
        buttons.addWidget(self.close_button)
        root.addLayout(buttons)

        self.apply_result(result)

    def emit_export_requested(self, export_text: bool, export_images: bool) -> None:
        result_path = self.current_result_path()
        self.export_requested.emit(
            {
                "text": export_text,
                "images": export_images,
                "result_path": result_path,
                "all_sets": self.export_all_sets_checkbox.isChecked(),
                "time_range": self.export_time_range_payload(),
            }
        )

    def current_result_path(self) -> str:
        index = self.result_tabs.currentIndex()
        if 0 <= index < len(self.result_paths):
            return self.result_paths[index]
        return ""

    def set_busy(self, busy: bool) -> None:
        self.export_text_button.setEnabled(not busy)
        self.export_images_button.setEnabled(not busy)
        self.export_all_button.setEnabled(not busy)
        self.export_all_sets_checkbox.setEnabled(not busy)
        self.update_time_export_controls(self.export_all_sets_checkbox.isChecked() and not busy)
        self.close_button.setEnabled(not busy)

    def update_time_export_controls(self, enabled: bool) -> None:
        for widget in [self.time_start_edit, self.time_end_edit, self.time_step_edit]:
            widget.setEnabled(enabled)

    def export_time_range_payload(self) -> dict:
        if not self.export_all_sets_checkbox.isChecked():
            return {}
        return {
            "start": self.time_start_edit.text().strip(),
            "end": self.time_end_edit.text().strip(),
            "step": self.time_step_edit.text().strip(),
        }

    def _prepare_table(self, table: QTableWidget) -> None:
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)

    def apply_result(self, result: ModuleOperationResult) -> None:
        settings = result.report.get("settings") or []
        conditions = result.report.get("conditions") or []
        solution_results = result.report.get("solution_results") or []
        result_sets = result.report.get("result_sets") or []
        self.apply_time_range_defaults(result_sets)
        self.summary_label.setText(
            "系统: {0}    模块: {1}    Analysis: {2}    Solution: {3}    求解结果: {4}    时间点/结果集: {5}".format(
                result.system_name,
                result.report.get("analysis_name", self.module.display_text),
                result.report.get("analysis_state", ""),
                result.report.get("solution_state", ""),
                len(solution_results),
                len(result_sets),
            )
        )

        self.settings_table.setRowCount(len(settings))
        for row, setting in enumerate(settings):
            values = [
                str(setting.get("display_name") or setting.get("api_name") or ""),
                str(setting.get("api_name") or ""),
                str(setting.get("value", "")),
                str(setting.get("type") or setting.get("kind") or ""),
            ]
            self._set_row(self.settings_table, row, values)
        self.settings_table.resizeRowsToContents()

        condition_rows = self._object_property_rows(conditions, name_key="path")
        self.conditions_table.setRowCount(len(condition_rows))
        for row, values in enumerate(condition_rows):
            self._set_row(self.conditions_table, row, values)
        self.conditions_table.resizeRowsToContents()

        self.populate_result_tabs(solution_results)

    def apply_time_range_defaults(self, result_sets: list) -> None:
        time_values = [
            str(item.get("time") or "").strip()
            for item in result_sets
            if str(item.get("time") or "").strip()
        ]
        if time_values:
            self.time_start_edit.setPlaceholderText(time_values[0])
            self.time_end_edit.setPlaceholderText(time_values[-1])
        else:
            self.time_start_edit.setPlaceholderText("开始")
            self.time_end_edit.setPlaceholderText("结束")
        self.time_step_edit.setPlaceholderText("间隔")

    def populate_result_tabs(self, solution_results: list) -> None:
        self.result_tabs.clear()
        self.result_paths = []
        if not solution_results:
            empty_label = QLabel("当前 Solution 下没有可读取的结果对象。")
            self.result_tabs.addTab(empty_label, "无结果")
            return

        for result_obj in solution_results:
            path = str(result_obj.get("path") or result_obj.get("name") or "")
            name = str(result_obj.get("name") or path or "Result")
            category = str(result_obj.get("category") or result_obj.get("type") or "")
            state = str(result_obj.get("state") or "")

            tab = QWidget()
            layout = QVBoxLayout(tab)
            layout.setContentsMargins(8, 8, 8, 8)
            summary = QLabel(f"路径: {path}    类型: {category}    状态: {state}")
            summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(summary)

            table = QTableWidget(0, 4)
            table.setHorizontalHeaderLabels(["属性", "Mechanical API", "当前值", "类型"])
            self._prepare_table(table)
            table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
            properties = result_obj.get("properties") or []
            table.setRowCount(len(properties))
            for row, prop in enumerate(properties):
                self._set_row(
                    table,
                    row,
                    [
                        str(prop.get("display_name") or prop.get("api_name") or ""),
                        str(prop.get("api_name") or ""),
                        str(prop.get("value", "")),
                        str(prop.get("type") or prop.get("kind") or ""),
                    ],
                )
            table.resizeRowsToContents()
            layout.addWidget(table, stretch=1)

            self.result_paths.append(path)
            tab_title = name if len(name) <= 28 else name[:25] + "..."
            self.result_tabs.addTab(tab, tab_title)
            self.result_tabs.setTabToolTip(self.result_tabs.count() - 1, path)

    def _object_property_rows(self, objects: list, *, name_key: str) -> list[list[str]]:
        rows: list[list[str]] = []
        for obj in objects:
            object_name = str(obj.get(name_key) or obj.get("name") or "")
            object_type = str(obj.get("category") or obj.get("type") or "")
            object_state = str(obj.get("state") or "")
            properties = obj.get("properties") or []
            if not properties:
                rows.append([object_name, object_type, object_state, "", ""])
                continue
            for prop in properties:
                rows.append(
                    [
                        object_name,
                        object_type,
                        object_state,
                        str(prop.get("display_name") or prop.get("api_name") or ""),
                        str(prop.get("value", "")),
                    ]
                )
        return rows

    def _set_row(self, table: QTableWidget, row: int, values: list[str]) -> None:
        for column, value in enumerate(values):
            table.setItem(row, column, QTableWidgetItem(value))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1160, 780)

        self.selected_project: Path | None = PROJECT_FILE
        self.modules: list[AnalysisModule] = []
        self.active_thread: QThread | None = None
        self.active_worker: QObject | None = None
        self.settings_dialog: AnalysisSettingsDialog | None = None
        self.results_dialog: SolutionResultsDialog | None = None
        self.pending_settings_dialog: AnalysisSettingsDialog | None = None
        self.pending_results_dialog: SolutionResultsDialog | None = None
        self.mechanical_session = None
        self.mechanical_port: int | None = None
        self.operation_started_at: float | None = None
        self.operation_stage = "空闲"
        self.operation_status = "等待操作"
        self.operation_timer = QTimer(self)
        self.operation_timer.timeout.connect(self.update_operation_timer)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        title = QLabel(APP_TITLE)
        title.setObjectName("titleLabel")
        root.addWidget(title)

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter, stretch=1)

        splitter.addWidget(self._build_project_group())
        splitter.addWidget(self._build_action_group())
        splitter.addWidget(self._build_status_group())
        splitter.addWidget(self._build_modules_group())
        splitter.addWidget(self._build_log_group())
        splitter.setSizes([90, 90, 170, 260, 170])

        self.setCentralWidget(central)
        self._build_menu()
        self._apply_style()
        self.update_operation_progress(
            {
                "stage": self.operation_stage,
                "status": self.operation_status,
            }
        )
        self.refresh_status()
        current_project = self.current_project_path()
        if current_project is not None and current_project.exists():
            self.load_analysis_modules(show_errors=False)

    def _build_project_group(self) -> QGroupBox:
        project_group = QGroupBox("工程 / database")
        project_layout = QHBoxLayout(project_group)
        project_layout.setSpacing(10)

        self.project_path_edit = QLineEdit(str(self.selected_project or ""))
        self.project_path_edit.setPlaceholderText("选择 .wbpj 工程或 .mechdb/.mechdat database")
        self.project_path_edit.setClearButtonEnabled(True)
        self.project_path_edit.editingFinished.connect(self.project_path_edited)

        self.browse_project_button = QPushButton("选择文件")
        self.browse_project_button.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))
        self.browse_project_button.clicked.connect(self.browse_project)

        self.load_modules_button = QPushButton("读取模块")
        self.load_modules_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogDetailedView))
        self.load_modules_button.clicked.connect(self.load_analysis_modules)

        project_layout.addWidget(self.project_path_edit, stretch=1)
        project_layout.addWidget(self.browse_project_button)
        project_layout.addWidget(self.load_modules_button)
        return project_group

    def _build_action_group(self) -> QGroupBox:
        action_group = QGroupBox("启动")
        action_layout = QHBoxLayout(action_group)
        action_layout.setSpacing(10)

        self.open_mechanical_button = QPushButton("打开 Mechanical")
        self.open_mechanical_button.setIcon(self.style().standardIcon(QStyle.SP_ComputerIcon))
        self.open_mechanical_button.clicked.connect(self.open_mechanical)

        self.close_mechanical_button = QPushButton("关闭 Mechanical")
        self.close_mechanical_button.setIcon(self.style().standardIcon(QStyle.SP_DialogCloseButton))
        self.close_mechanical_button.clicked.connect(self.close_mechanical)

        self.open_zemax_button = QPushButton("打开 Zemax")
        self.open_zemax_button.setIcon(self.style().standardIcon(QStyle.SP_DriveNetIcon))
        self.open_zemax_button.clicked.connect(self.open_zemax)

        self.close_zemax_button = QPushButton("关闭 Zemax")
        self.close_zemax_button.setIcon(self.style().standardIcon(QStyle.SP_DialogCloseButton))
        self.close_zemax_button.clicked.connect(self.close_zemax)

        self.check_button = QPushButton("检查环境")
        self.check_button.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.check_button.clicked.connect(self.refresh_status)

        action_layout.addWidget(self.open_mechanical_button)
        action_layout.addWidget(self.close_mechanical_button)
        action_layout.addWidget(self.open_zemax_button)
        action_layout.addWidget(self.close_zemax_button)
        action_layout.addStretch(1)
        action_layout.addWidget(self.check_button)
        return action_group

    def _build_status_group(self) -> QWidget:
        container = QWidget()
        container_layout = QHBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(12)

        status_group = QGroupBox("环境状态")
        self.status_layout = QGridLayout(status_group)
        self.status_layout.setColumnStretch(1, 1)

        operation_group = QGroupBox("当前操作")
        operation_layout = QGridLayout(operation_group)
        operation_layout.setColumnStretch(1, 1)

        self.operation_stage_label = QLabel("空闲")
        self.operation_stage_label.setWordWrap(True)
        self.operation_stage_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.operation_status_label = QLabel("等待操作")
        self.operation_status_label.setWordWrap(True)
        self.operation_status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.operation_elapsed_label = QLabel("00:00")

        operation_layout.addWidget(QLabel("阶段"), 0, 0)
        operation_layout.addWidget(self.operation_stage_label, 0, 1)
        operation_layout.addWidget(QLabel("状态"), 1, 0)
        operation_layout.addWidget(self.operation_status_label, 1, 1)
        operation_layout.addWidget(QLabel("用时"), 2, 0)
        operation_layout.addWidget(self.operation_elapsed_label, 2, 1)
        operation_group.setMinimumWidth(360)

        container_layout.addWidget(status_group, stretch=3)
        container_layout.addWidget(operation_group, stretch=2)
        return container

    def _build_modules_group(self) -> QGroupBox:
        modules_group = QGroupBox("当前工程分析模块")
        modules_layout = QVBoxLayout(modules_group)

        self.modules_table = QTableWidget(0, 7)
        self.modules_table.setHorizontalHeaderLabels(
            ["序号", "系统名", "模块", "系统类型", "物理场", "分析类型", "求解器"]
        )
        self.modules_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.modules_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.modules_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.modules_table.setAlternatingRowColors(True)
        self.modules_table.verticalHeader().setVisible(False)
        self.modules_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.modules_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        modules_layout.addWidget(self.modules_table)

        button_layout = QHBoxLayout()
        self.read_settings_button = QPushButton("读取设置")
        self.read_settings_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogContentsView))
        self.read_settings_button.setToolTip("从当前已打开的 Mechanical 会话读取；不会启动新的 Mechanical。")
        self.read_settings_button.clicked.connect(self.read_selected_module_settings)

        self.solve_module_button = QPushButton("求解模块")
        self.solve_module_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.solve_module_button.setToolTip("通过当前 Mechanical database 会话先清除当前求解数据，再求解选中模块。")
        self.solve_module_button.clicked.connect(self.solve_selected_module)

        self.read_results_button = QPushButton("读取结果")
        self.read_results_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogInfoView))
        self.read_results_button.setToolTip("通过当前 Mechanical database 会话读取选中模块的设置、条件和所有求解结果。")
        self.read_results_button.clicked.connect(self.read_selected_module_results)

        button_layout.addWidget(self.read_settings_button)
        button_layout.addWidget(self.solve_module_button)
        button_layout.addWidget(self.read_results_button)
        button_layout.addStretch(1)
        modules_layout.addLayout(button_layout)
        return modules_group

    def _build_log_group(self) -> QGroupBox:
        log_group = QGroupBox("日志")
        log_layout = QVBoxLayout(log_group)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        log_layout.addWidget(self.log)
        return log_group

    def _build_menu(self) -> None:
        refresh_action = QAction("检查环境", self)
        refresh_action.triggered.connect(self.refresh_status)

        browse_project_action = QAction("选择文件", self)
        browse_project_action.triggered.connect(self.browse_project)

        load_modules_action = QAction("读取分析模块", self)
        load_modules_action.triggered.connect(self.load_analysis_modules)

        close_mechanical_action = QAction("关闭 Mechanical", self)
        close_mechanical_action.triggered.connect(self.close_mechanical)

        close_zemax_action = QAction("关闭 Zemax", self)
        close_zemax_action.triggered.connect(self.close_zemax)

        exit_action = QAction("退出", self)
        exit_action.triggered.connect(self.close)

        file_menu = self.menuBar().addMenu("文件")
        file_menu.addAction(browse_project_action)
        file_menu.addAction(load_modules_action)
        file_menu.addSeparator()
        file_menu.addAction(refresh_action)
        file_menu.addSeparator()
        file_menu.addAction(close_mechanical_action)
        file_menu.addAction(close_zemax_action)
        file_menu.addSeparator()
        file_menu.addAction(exit_action)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget {
                font-family: "Microsoft YaHei", "Segoe UI", Arial;
                font-size: 10.5pt;
            }
            QLabel#titleLabel {
                font-size: 18pt;
                font-weight: 600;
            }
            QPushButton {
                min-height: 34px;
                padding: 4px 12px;
            }
            QPlainTextEdit {
                font-family: Consolas, "Cascadia Mono", monospace;
                font-size: 9.5pt;
            }
            QTableWidget {
                gridline-color: #d0d5dd;
                selection-background-color: #d9e8ff;
            }
            QLabel[status="ok"] {
                color: #146c2e;
                font-weight: 600;
            }
            QLabel[status="missing"] {
                color: #b42318;
                font-weight: 600;
            }
            """
        )

    def current_project_path(self) -> Path | None:
        text = self.project_path_edit.text().strip()
        if text:
            return Path(text)
        return self.selected_project

    def project_path_edited(self) -> None:
        self.selected_project = self.current_project_path()
        self.refresh_status()

    def browse_project(self) -> None:
        current = self.current_project_path()
        start_dir = current.parent if current is not None and current.parent.exists() else WORKSPACE
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 Ansys 工程或 Mechanical database",
            str(start_dir),
            "Ansys Workbench / Mechanical Database (*.wbpj *.mechdb *.mechdat);;All Files (*)",
        )
        if not file_path:
            return

        self.selected_project = Path(file_path)
        self.project_path_edit.setText(str(self.selected_project))
        self.refresh_status()
        self.load_analysis_modules()

    def load_analysis_modules(
        self,
        checked: bool = False,
        *,
        show_errors: bool = True,
    ) -> None:
        del checked
        project = self.current_project_path()
        if project is None:
            self.modules = []
            self.modules_table.setRowCount(0)
            if show_errors:
                QMessageBox.warning(self, "未选择文件", "请先选择 Ansys Workbench 工程（.wbpj）或 Mechanical database（.mechdb/.mechdat）。")
            return
        if project.suffix.lower() in MECHANICAL_DATABASE_SUFFIXES:
            modules = [
                AnalysisModule(
                    index=1,
                    system_name=project.suffix.lower().lstrip(".").upper(),
                    display_text="Mechanical Database",
                    system_type="Mechanical",
                    physics_type="",
                    analysis_type="",
                    solver_type="",
                    directory_name=project.name,
                    visible=True,
                )
            ]
        else:
            try:
                modules = read_project_analysis_modules(project)
            except Exception as exc:
                self.modules_table.setRowCount(0)
                if show_errors:
                    self.report_error("读取分析模块失败", exc)
                else:
                    self.append_log(f"读取分析模块失败: {exc}")
                return

        self.modules = modules
        self.modules_table.setRowCount(len(modules))
        for row_index, module in enumerate(modules):
            values = [
                str(module.index),
                module.system_name,
                module.display_text,
                module.system_type,
                module.physics_type,
                module.analysis_type,
                module.solver_type,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setTextAlignment(Qt.AlignCenter)
                item.setData(Qt.UserRole, module.system_name)
                self.modules_table.setItem(row_index, column, item)

        if modules and self.modules_table.currentRow() < 0:
            self.modules_table.selectRow(0)
        self.modules_table.resizeRowsToContents()
        self.append_log(f"已读取 {len(modules)} 个分析模块: {project}")

    def selected_module(self) -> AnalysisModule | None:
        row = self.modules_table.currentRow()
        if row < 0 and self.modules:
            row = 0
            self.modules_table.selectRow(row)
        if row < 0 or row >= len(self.modules):
            return None
        return self.modules[row]

    def start_module_operation(
        self,
        operation: str,
        settings_update: dict | None = None,
        conditions_update: list[dict] | None = None,
        export_dir: Path | None = None,
        export_text: bool = False,
        export_images: bool = False,
        export_result_paths: list[str] | None = None,
        export_all_sets: bool = False,
        export_time_range: dict | None = None,
        *,
        settings_dialog: AnalysisSettingsDialog | None = None,
        results_dialog: SolutionResultsDialog | None = None,
    ) -> None:
        if self.active_thread is not None:
            QMessageBox.information(self, "操作正在执行", "当前 Mechanical 操作还没有结束。")
            return

        project = self.current_project_path()
        if project is None:
            QMessageBox.warning(self, "未选择文件", "请先选择 Ansys Workbench 工程（.wbpj）或 Mechanical database（.mechdb/.mechdat）。")
            return
        if project.suffix.lower() in MECHANICAL_DATABASE_SUFFIXES and not self.modules:
            self.load_analysis_modules(show_errors=False)
        module = self.selected_module()
        if module is None:
            QMessageBox.warning(self, "未选择模块", "请先读取工程模块并选择一个分析模块。")
            return
        if self.mechanical_port is None:
            QMessageBox.warning(self, "未打开 Mechanical", MECHANICAL_REQUIRED_MESSAGE)
            self.append_log("未打开 Mechanical database 会话，未启动读取/求解/导出操作。")
            return

        self.pending_settings_dialog = settings_dialog
        if settings_dialog is not None:
            settings_dialog.set_busy(True)
        self.pending_results_dialog = results_dialog
        if results_dialog is not None:
            results_dialog.set_busy(True)

        self.set_operation_buttons_enabled(False)
        self.start_operation_status(
            f"{module.system_name} / {module.display_text}",
            f"开始 {operation}",
        )
        self.append_log(
            f"通过当前 Mechanical database 会话执行 {module.system_name} / {module.display_text}: {operation}"
        )

        thread = QThread(self)
        worker = MechanicalOperationWorker(
            operation,
            project,
            module.system_name,
            module.display_text,
            module.index,
            self.mechanical_port,
            settings_update,
            conditions_update,
            export_dir,
            export_text,
            export_images,
            export_result_paths,
            export_all_sets,
            export_time_range,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.update_operation_progress)
        worker.finished.connect(self.module_operation_finished)
        worker.failed.connect(self.module_operation_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self.operation_thread_finished)
        self.active_thread = thread
        self.active_worker = worker
        thread.start()

    def set_operation_buttons_enabled(self, enabled: bool) -> None:
        for button in [
            self.browse_project_button,
            self.load_modules_button,
            self.read_settings_button,
            self.solve_module_button,
            self.read_results_button,
            self.open_mechanical_button,
            self.close_mechanical_button,
            self.open_zemax_button,
            self.close_zemax_button,
            self.check_button,
        ]:
            button.setEnabled(enabled)

    def start_operation_status(self, stage: str, status: str) -> None:
        self.operation_started_at = time.monotonic()
        self.operation_timer.start(1000)
        self.update_operation_progress({"stage": stage, "status": status})
        self.update_operation_timer()

    @Slot(object)
    def update_operation_progress(self, payload: object) -> None:
        if isinstance(payload, dict):
            stage = str(payload.get("stage") or self.operation_stage)
            status = str(payload.get("status") or "")
            analysis_state = str(payload.get("analysis_state") or "")
            solution_state = str(payload.get("solution_state") or "")
        else:
            stage = str(payload)
            status = ""
            analysis_state = ""
            solution_state = ""

        self.operation_stage = stage
        detail_parts = [part for part in [status, analysis_state, solution_state] if part]
        self.operation_status = " | ".join(detail_parts) if detail_parts else "运行中"
        self.operation_stage_label.setText(self.operation_stage)
        self.operation_status_label.setText(self.operation_status)

    def update_operation_timer(self) -> None:
        if self.operation_started_at is None:
            elapsed_seconds = 0
        else:
            elapsed_seconds = int(time.monotonic() - self.operation_started_at)
        minutes, seconds = divmod(elapsed_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            elapsed_text = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        else:
            elapsed_text = f"{minutes:02d}:{seconds:02d}"
        self.operation_elapsed_label.setText(elapsed_text)

    def stop_operation_timer(self) -> None:
        self.update_operation_timer()
        self.operation_timer.stop()

    def read_selected_module_settings(self) -> None:
        self.start_module_operation("read")

    def solve_selected_module(self) -> None:
        self.append_log("求解前将先清除选中模块当前求解数据。")
        self.start_module_operation("solve")

    def read_selected_module_results(self) -> None:
        self.start_module_operation("read_results")

    def export_results_from_dialog(
        self,
        dialog: SolutionResultsDialog,
        payload: dict,
    ) -> None:
        module = self.selected_module()
        if module is None:
            QMessageBox.warning(dialog, "未选择模块", "请先选择一个分析模块。")
            return
        default_dir = (
            Path(__file__).resolve().parents[1]
            / "exports"
            / f"{module.system_name}_{module.display_text}".replace(" ", "_")
        )
        default_dir.mkdir(parents=True, exist_ok=True)
        selected_dir = QFileDialog.getExistingDirectory(
            dialog,
            "选择导出目录",
            str(default_dir),
        )
        if not selected_dir:
            return
        result_path = str(payload.get("result_path") or "")
        if not result_path:
            QMessageBox.warning(dialog, "未选择结果", "请先在结果窗口中选择一个求解结果选项卡。")
            return
        self.start_module_operation(
            "export_results",
            export_dir=Path(selected_dir),
            export_text=bool(payload.get("text")),
            export_images=bool(payload.get("images")),
            export_result_paths=[result_path],
            export_all_sets=bool(payload.get("all_sets")),
            export_time_range=payload.get("time_range") or {},
            results_dialog=dialog,
        )

    def save_settings_from_dialog(
        self,
        dialog: AnalysisSettingsDialog,
        update_payload: dict,
    ) -> None:
        settings_update = update_payload.get("settings") or {}
        conditions_update = update_payload.get("conditions") or []
        if not settings_update and not conditions_update:
            QMessageBox.information(dialog, "没有设置变更", "当前设置没有修改。")
            return
        self.start_module_operation(
            "update",
            settings_update,
            conditions_update,
            settings_dialog=dialog,
        )

    @Slot(object)
    def module_operation_finished(self, result: ModuleOperationResult) -> None:
        for item in result.operation_log:
            self.append_log(item)

        if result.operation == "read_settings":
            self.update_operation_progress({"stage": "读取完成", "status": result.system_name})
            self.open_settings_dialog(result)
        elif result.operation == "read_results":
            self.update_operation_progress({"stage": "结果读取完成", "status": result.system_name})
            self.open_results_dialog(result)
        elif result.operation == "export_results":
            if self.pending_results_dialog is not None:
                self.pending_results_dialog.apply_result(result)
            exported = result.report.get("exported_files") or []
            ok_files = [item for item in exported if item.get("status") == "ok"]
            self.append_log(f"求解结果导出完成: {len(ok_files)} 个文件")
            self.show_export_result(result)
        elif result.operation == "update_settings":
            if self.pending_settings_dialog is not None:
                self.pending_settings_dialog.apply_result(result)
            self.append_log(f"设置和分析条件已保存: {result.system_name}")
            self.update_operation_progress({"stage": "保存完成", "status": result.system_name})
        elif result.report.get("solved"):
            self.append_log(f"求解完成: {result.system_name} ({result.report.get('solve_method')})")
            self.update_operation_progress(
                {
                    "stage": "求解完成",
                    "status": str(result.report.get("solve_method") or ""),
                    "analysis_state": str(result.report.get("analysis_state") or ""),
                    "solution_state": str(result.report.get("solution_state") or ""),
                }
            )
        else:
            self.append_log(f"操作完成: {result.system_name} / {result.operation}")
            self.update_operation_progress({"stage": "操作完成", "status": result.operation})

    def open_settings_dialog(self, result: ModuleOperationResult) -> None:
        module = self.selected_module()
        if module is None:
            return
        if self.settings_dialog is not None:
            self.settings_dialog.close()

        dialog = AnalysisSettingsDialog(module, result, self)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.save_requested.connect(
            lambda settings, current_dialog=dialog: self.save_settings_from_dialog(
                current_dialog,
                settings,
            )
        )
        dialog.destroyed.connect(self.clear_settings_dialog)
        self.settings_dialog = dialog
        dialog.show()
        self.append_log(f"已打开设置窗口: {result.system_name}")

    def open_results_dialog(self, result: ModuleOperationResult) -> None:
        module = self.selected_module()
        if module is None:
            return
        if self.results_dialog is not None:
            self.results_dialog.close()

        dialog = SolutionResultsDialog(module, result, self)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.export_requested.connect(
            lambda payload, current_dialog=dialog: self.export_results_from_dialog(
                current_dialog,
                payload,
            )
        )
        dialog.destroyed.connect(self.clear_results_dialog)
        self.results_dialog = dialog
        dialog.show()
        self.append_log(f"已打开求解结果窗口: {result.system_name}")

    def show_export_result(self, result: ModuleOperationResult) -> None:
        exported = result.report.get("exported_files") or []
        ok_paths = [str(item.get("path")) for item in exported if item.get("status") == "ok"]
        failed = [item for item in exported if item.get("status") != "ok"]
        if ok_paths:
            export_dir = str(Path(ok_paths[0]).parent)
            subprocess.Popen(["explorer", export_dir])
            message = f"已导出 {len(ok_paths)} 个文件到:\n{export_dir}"
            if failed:
                message += f"\n有 {len(failed)} 个项目导出失败，详情见日志。"
            QMessageBox.information(self, "导出完成", message)
        else:
            QMessageBox.warning(self, "导出失败", "没有成功导出文件，请查看日志。")
        for item in failed:
            self.append_log(
                "导出失败: {0} {1} {2}".format(
                    item.get("kind", ""),
                    item.get("path", ""),
                    item.get("message", ""),
                )
            )

    @Slot(str)
    def module_operation_failed(self, message: str) -> None:
        self.append_log(f"当前 Mechanical 操作失败: {message}")
        self.update_operation_progress({"stage": "操作失败", "status": message})
        if self.pending_settings_dialog is not None:
            self.pending_settings_dialog.set_busy(False)
        if self.pending_results_dialog is not None:
            self.pending_results_dialog.set_busy(False)
        QMessageBox.critical(self, "当前 Mechanical 操作失败", message)

    @Slot()
    def operation_thread_finished(self) -> None:
        self.stop_operation_timer()
        if self.pending_settings_dialog is not None:
            self.pending_settings_dialog.set_busy(False)
        if self.pending_results_dialog is not None:
            self.pending_results_dialog.set_busy(False)
        self.pending_settings_dialog = None
        self.pending_results_dialog = None
        self.active_thread = None
        self.active_worker = None
        self.set_operation_buttons_enabled(True)

    @Slot()
    def clear_settings_dialog(self) -> None:
        self.settings_dialog = None

    @Slot()
    def clear_results_dialog(self) -> None:
        self.results_dialog = None

    def refresh_status(self) -> None:
        rows = self.collect_status()
        while self.status_layout.count():
            item = self.status_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        headers = ["项目", "路径/模块", "状态"]
        for column, header in enumerate(headers):
            label = QLabel(header)
            label.setStyleSheet("font-weight: 600;")
            self.status_layout.addWidget(label, 0, column)

        for row_index, row in enumerate(rows, start=1):
            name_label = QLabel(row.name)
            value_label = QLabel(row.value)
            value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            status_label = QLabel("正常" if row.ok else "缺失")
            status_label.setProperty("status", "ok" if row.ok else "missing")
            status_label.style().unpolish(status_label)
            status_label.style().polish(status_label)

            self.status_layout.addWidget(name_label, row_index, 0)
            self.status_layout.addWidget(value_label, row_index, 1)
            self.status_layout.addWidget(status_label, row_index, 2)

        self.append_log("环境状态已刷新")

    def collect_status(self) -> list[StatusRow]:
        project = self.current_project_path()
        project_value = str(project) if project is not None else "未选择"
        project_ok = bool(project is not None and project.exists())
        checks = [
            ("工程/database", project_value, project_ok),
            ("RunWB2", RUNWB2, RUNWB2.exists()),
            ("Mechanical", MECHANICAL_EXE, MECHANICAL_EXE.exists()),
            ("Zemax OpticStudio", OPTICSTUDIO_EXE, OPTICSTUDIO_EXE.exists()),
            ("PyMechanical", "ansys.mechanical.core", package_available("ansys.mechanical.core")),
            ("pythonnet", "clr", package_available("clr")),
            ("PySide6", "PySide6", package_available("PySide6")),
        ]
        return [StatusRow(name, str(value), ok) for name, value, ok in checks]

    def open_mechanical(self) -> None:
        if self.active_thread is not None:
            QMessageBox.information(self, "操作正在执行", "当前操作还没有结束。")
            return
        project = self.current_project_path()
        if project is None:
            QMessageBox.warning(self, "未选择文件", "请先选择 Ansys Workbench 工程（.wbpj）或 Mechanical database（.mechdb/.mechdat）。")
            return
        if project.suffix.lower() in MECHANICAL_DATABASE_SUFFIXES and not self.modules:
            self.load_analysis_modules(show_errors=False)
        module = self.selected_module()
        if module is None:
            QMessageBox.warning(self, "未选择模块", "请先读取工程模块并选择一个分析模块。")
            return
        self.set_operation_buttons_enabled(False)
        self.start_operation_status(
            "启动 Mechanical",
            f"正在用 database 打开 {module.system_name} / {module.display_text}",
        )
        self.append_log(
            "正在后台用 Mechanical database 打开 Mechanical，"
            f"工程={project}，模块={module.system_name} / {module.display_text}"
        )

        thread = QThread(self)
        worker = MechanicalLaunchWorker(project, module.system_name, module.display_text)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.update_operation_progress)
        worker.finished.connect(self.mechanical_launch_finished)
        worker.failed.connect(self.mechanical_launch_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self.operation_thread_finished)
        self.active_thread = thread
        self.active_worker = worker
        thread.start()

    @Slot(object)
    def mechanical_launch_finished(self, result: dict) -> None:
        self.mechanical_session = result.get("session")
        self.mechanical_port = result.get("port")
        port_text = f"端口={self.mechanical_port}" if self.mechanical_port else "未读取到端口"
        self.update_operation_progress(
            {
                "stage": "Mechanical 已启动",
                "status": f"{result.get('system_name')} / {result.get('analysis_hint')}，{port_text}",
            }
        )
        self.append_log(
            "已用 Mechanical database 启动 Mechanical，"
            f"工程={result.get('project')}，database={result.get('database')}，"
            f"模块={result.get('system_name')}，{port_text}"
        )

    @Slot(str)
    def mechanical_launch_failed(self, message: str) -> None:
        self.update_operation_progress({"stage": "打开 Mechanical 失败", "status": message})
        self.append_log(f"打开 Mechanical 失败: {message}")
        QMessageBox.critical(self, "打开 Mechanical 失败", message)

    def open_zemax(self) -> None:
        try:
            process = open_opticstudio()
        except Exception as exc:
            self.report_error("打开 Zemax 失败", exc)
            return
        self.append_log(f"已启动 Zemax OpticStudio，PID={process.pid}")

    def close_mechanical(self) -> None:
        if self.active_thread is not None:
            QMessageBox.information(self, "操作正在执行", "当前操作还没有结束。")
            return

        answer = QMessageBox.question(
            self,
            "关闭 Mechanical",
            "将先保存当前 Mechanical database 工程，然后正常关闭 Mechanical。不会强制终止进程。继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        self.set_operation_buttons_enabled(False)
        self.start_operation_status("关闭 Mechanical", "正在保存当前 Mechanical database 工程")
        self.append_log("正在后台保存当前 Mechanical database 工程并正常关闭 Mechanical（非强制）")

        thread = QThread(self)
        worker = MechanicalCloseWorker(
            self.mechanical_session,
            self.mechanical_port,
            self.current_project_path(),
            "",
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.update_operation_progress)
        worker.finished.connect(self.mechanical_close_finished)
        worker.failed.connect(self.mechanical_close_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self.operation_thread_finished)
        self.active_thread = thread
        self.active_worker = worker
        thread.start()

    @Slot(object)
    def mechanical_close_finished(self, result: dict) -> None:
        self.mechanical_session = None
        self.mechanical_port = None
        save_output = str(result.get("save_output") or "").strip()
        if save_output:
            for line in save_output.splitlines():
                self.append_log(f"Mechanical 保存: {line}")
        status = str(result.get("status") or "已保存并发送正常关闭请求")
        self.update_operation_progress({"stage": "关闭 Mechanical", "status": status})
        self.append_log(status)
        if not result.get("closed"):
            QMessageBox.information(
                self,
                "关闭 Mechanical",
                "已保存当前 Mechanical database 工程，并发送正常关闭请求。如果 Mechanical 弹出确认窗口，请在 Mechanical 中确认关闭。程序不会强制终止进程。",
            )

    @Slot(str)
    def mechanical_close_failed(self, message: str) -> None:
        self.update_operation_progress({"stage": "关闭 Mechanical 失败", "status": message})
        self.append_log(f"关闭 Mechanical 失败: {message}")
        QMessageBox.critical(self, "关闭 Mechanical 失败", message)

    def close_zemax(self) -> None:
        self.close_process_group("Zemax OpticStudio", ZEMAX_PROCESS_NAMES)

    def close_process_group(self, label: str, process_names: list[str]) -> None:
        answer = QMessageBox.question(
            self,
            f"关闭 {label}",
            f"将请求关闭 {label} 相关进程。正常关闭失败时会强制终止，未保存的更改可能丢失。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        for process_name in process_names:
            result, output = self.run_taskkill(process_name, force=False)
            if result.returncode == 0:
                self.append_log(f"已请求关闭 {process_name}: {output}")
            elif "not found" in output.lower() or "没有找到" in output:
                self.append_log(f"未发现正在运行的 {process_name}")
            else:
                self.append_log(
                    f"正常关闭 {process_name} 失败，改用强制关闭: {output}"
                )
                force_result, force_output = self.run_taskkill(process_name, force=True)
                if force_result.returncode == 0:
                    self.append_log(f"已强制关闭 {process_name}: {force_output}")
                elif "not found" in force_output.lower() or "没有找到" in force_output:
                    self.append_log(f"强制关闭时未发现正在运行的 {process_name}")
                else:
                    self.append_log(
                        f"强制关闭 {process_name} 返回 {force_result.returncode}: {force_output}"
                    )

    def run_taskkill(self, process_name: str, *, force: bool) -> tuple[subprocess.CompletedProcess[str], str]:
        command = ["taskkill"]
        if force:
            command.append("/F")
        command.extend(["/IM", process_name, "/T"])
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
        )
        output = (result.stdout or result.stderr or "").strip()
        return result, output

    def report_error(self, title: str, exc: Exception) -> None:
        message = f"{title}: {exc}"
        self.append_log(message)
        QMessageBox.critical(self, title, str(exc))

    def append_log(self, text: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log.appendPlainText(f"[{timestamp}] {text}")


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
