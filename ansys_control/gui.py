from __future__ import annotations

import csv
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QColor, QImage, QPainter, QPen, QPixmap, qRgb
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
    QScrollArea,
    QSizePolicy,
    QPlainTextEdit,
    QSlider,
    QSpinBox,
    QSplitter,
    QStackedWidget,
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
    read_current_mechanical_analysis_modules,
    read_mechanical_database_analysis_modules,
    save_and_close_mechanical_session,
)
from .mechanical_ops import (
    AnalysisModule,
    ModuleOperationResult,
    export_current_solution_results,
    read_project_analysis_modules,
    read_current_mechanical_settings,
    read_current_solution_results,
    solve_current_mechanical_analysis,
    update_current_mechanical_settings,
)
from .zemax import (
    DETECTOR_PREVIEW_MAX_DIMENSION,
    calculate_pose_records_from_mechanical_exports,
    close_managed_opticstudio_project,
    import_lens_poses_to_current_opticstudio,
    list_zemax_detectors,
    read_zemax_detector_result,
    read_lens_pose_records,
    run_zemax_ray_trace_clear_detectors,
    run_zemax_random_vibration_ray_trace,
    run_zemax_time_series_ray_trace,
)


ZEMAX_PROCESS_NAMES = ["OpticStudio.exe"]
APP_NAME = "Windows端"
APP_VERSION = "V26.5.29"
APP_TITLE = "对话框控制程序"
MECHANICAL_REQUIRED_MESSAGE = (
    "请先选择 Mechanical database（.mechdb/.mechdat），程序会自动启动后台 Mechanical。"
    "读取设置、求解、读取结果和导出结果都只连接当前后台 Mechanical 会话，不再重新打开 Workbench。"
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
        "| Where-Object { $names -contains $_.ProcessName -or $names -contains ($_.ProcessName + '.exe') } "
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


def compact_status_value(value: str, max_chars: int = 52) -> str:
    value = str(value).strip()
    if len(value) <= max_chars:
        return value
    left = max_chars // 2 - 2
    right = max_chars - left - 5
    return f"{value[:left]} ... {value[-right:]}"


class ModuleLoadWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(object)

    def __init__(self, project: Path, port: int | None = None) -> None:
        super().__init__()
        self.project = project
        self.port = port

    @Slot()
    def run(self) -> None:
        try:
            suffix = self.project.suffix.lower()
            self.progress.emit(
                {
                    "stage": "读取分析模块",
                    "status": f"正在读取 {self.project.name}",
                }
            )
            if self.port is not None:
                modules = read_current_mechanical_analysis_modules(
                    port=self.port,
                    progress_callback=self.progress.emit,
                )
            elif suffix in MECHANICAL_DATABASE_SUFFIXES:
                modules = read_mechanical_database_analysis_modules(
                    self.project,
                    progress_callback=self.progress.emit,
                )
            else:
                modules = read_project_analysis_modules(self.project)
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.finished.emit({"project": self.project, "modules": modules})


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
        save_project: bool,
        project: Path | None = None,
        system_name: str | None = None,
    ) -> None:
        super().__init__()
        self.session = session
        self.port = port
        self.save_project = save_project
        self.project = project
        self.system_name = system_name or ""

    @Slot()
    def run(self) -> None:
        try:
            result = save_and_close_mechanical_session(
                self.session,
                port=self.port,
                project_file=self.project,
                save_project=self.save_project,
                progress_callback=self.progress.emit,
            )
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.finished.emit(result)


class ZemaxPoseImportWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(object)

    def __init__(
        self,
        export_folder: Path,
        zemax_project: Path,
    ) -> None:
        super().__init__()
        self.export_folder = export_folder
        self.zemax_project = zemax_project

    @Slot()
    def run(self) -> None:
        try:
            self.progress.emit(
                {
                    "stage": "Zemax 导入",
                    "status": "正在后台打开所选 Zemax 工程并匹配 NSC Object Comment；导入后暂不保存",
                }
            )
            result = import_lens_poses_to_current_opticstudio(
                self.export_folder,
                self.zemax_project,
                save=False,
            )
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.finished.emit(result)


class ZemaxCloseWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(object)

    def __init__(
        self,
        project: Path | None,
        save_project: bool,
    ) -> None:
        super().__init__()
        self.project = project
        self.save_project = save_project

    @Slot()
    def run(self) -> None:
        try:
            self.progress.emit(
                {
                    "stage": "关闭 Zemax",
                    "status": "正在保存并关闭后台 Zemax API 会话" if self.save_project else "正在放弃未保存导入并关闭后台 Zemax API 会话",
                }
            )
            managed_result = close_managed_opticstudio_project(self.project, save=self.save_project)
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.finished.emit(
                {
                    "managed": managed_result,
                    "processes": [],
                    "save_project": self.save_project,
                }
            )


class ZemaxDetectorWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(object)

    def __init__(
        self,
        operation: str,
        zemax_project: Path,
        detector_number: int | None = None,
        *,
        output_dir: Path | None = None,
        log_scale: bool = True,
        cpu_core_count: int | None = None,
        write_csv: bool = False,
        preview_max_dimension: int | None = DETECTOR_PREVIEW_MAX_DIMENSION,
        export_folder: Path | None = None,
        output_file: Path | None = None,
        sample_count: int = 20,
        random_seed: int | None = None,
    ) -> None:
        super().__init__()
        self.operation = operation
        self.zemax_project = zemax_project
        self.detector_number = detector_number
        self.output_dir = output_dir
        self.log_scale = log_scale
        self.cpu_core_count = cpu_core_count
        self.write_csv = write_csv
        self.preview_max_dimension = preview_max_dimension
        self.export_folder = export_folder
        self.output_file = output_file
        self.sample_count = max(1, int(sample_count))
        self.random_seed = random_seed

    @Slot()
    def run(self) -> None:
        try:
            if self.operation == "list_detectors":
                self.progress.emit({"stage": "Zemax 探测器", "status": "正在后台读取探测器列表"})
                result = list_zemax_detectors(self.zemax_project)
                result["operation"] = self.operation
            elif self.operation == "ray_trace":
                self.progress.emit({"stage": "Zemax 光线追迹", "status": "正在后台清空探测器并执行非序列光线追迹"})
                result = run_zemax_ray_trace_clear_detectors(
                    self.zemax_project,
                    cpu_core_count=self.cpu_core_count,
                    progress_callback=self.progress.emit,
                )
                result["operation"] = self.operation
            elif self.operation == "time_series_trace":
                if self.detector_number is None:
                    raise ValueError("未选择探测器。")
                if self.export_folder is None:
                    raise ValueError("未选择 Mechanical 时间序列导出文件夹。")
                self.progress.emit(
                    {
                        "stage": "Zemax 时间序列追迹",
                        "status": f"正在按时间序列写入形变并追迹 Detector {self.detector_number}",
                    }
                )
                result = run_zemax_time_series_ray_trace(
                    self.export_folder,
                    self.zemax_project,
                    self.detector_number,
                    output_file=self.output_file,
                    cpu_core_count=self.cpu_core_count,
                    progress_callback=self.progress.emit,
                )
                result["operation"] = self.operation
            elif self.operation == "random_vibration_trace":
                if self.detector_number is None:
                    raise ValueError("未选择探测器。")
                if self.export_folder is None:
                    raise ValueError("未选择 Mechanical 随机振动标准差导出文件夹。")
                self.progress.emit(
                    {
                        "stage": "Zemax 随机振动追迹",
                        "status": f"正在生成 {self.sample_count} 个随机振动样本并追迹 Detector {self.detector_number}",
                    }
                )
                result = run_zemax_random_vibration_ray_trace(
                    self.export_folder,
                    self.zemax_project,
                    self.detector_number,
                    sample_count=self.sample_count,
                    output_file=self.output_file,
                    seed=self.random_seed,
                    cpu_core_count=self.cpu_core_count,
                    progress_callback=self.progress.emit,
                )
                result["operation"] = self.operation
            elif self.operation in {"read_detector_result", "export_detector_result"}:
                if self.detector_number is None:
                    raise ValueError("未选择探测器。")
                write_csv = bool(self.write_csv or self.operation == "export_detector_result")
                self.progress.emit(
                    {
                        "stage": "Zemax 探测器结果",
                        "status": (
                            f"正在完整导出 Detector {self.detector_number}"
                            if write_csv
                            else f"正在快速读取 Detector {self.detector_number} 预览"
                        ),
                    }
                )
                result = read_zemax_detector_result(
                    self.zemax_project,
                    self.detector_number,
                    output_dir=self.output_dir,
                    log_scale=self.log_scale,
                    write_csv=write_csv,
                    preview_max_dimension=None if write_csv else self.preview_max_dimension,
                )
                result["operation"] = self.operation
            else:
                raise ValueError(f"未知 Zemax 探测器操作: {self.operation}")
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.finished.emit(result)


class DetectorImageDialog(QDialog):
    def __init__(self, image_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"探测器伪彩色图 - {image_path.name}")
        self.resize(900, 700)

        layout = QVBoxLayout(self)
        pixmap = QPixmap(str(image_path))
        if pixmap.isNull():
            layout.addWidget(QLabel(f"无法打开图像:\n{image_path}"))
        else:
            label = QLabel()
            label.setPixmap(pixmap)
            label.setAlignment(Qt.AlignCenter)

            scroll = QScrollArea()
            scroll.setWidget(label)
            scroll.setWidgetResizable(False)
            layout.addWidget(scroll, stretch=1)

        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        button_layout.addWidget(close_button)
        layout.addLayout(button_layout)


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
        self.resize(1120, 820)
        self.setting_infos: list[dict] = []
        self.original_values: dict[str, str] = {}
        self.original_condition_values: dict[tuple[str, str], str] = {}
        self.tabular_records: list[dict] = []
        self.current_tabular_index: int | None = None

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

        tabular_header = QHBoxLayout()
        tabular_header.addWidget(QLabel("Tabular Data"))
        self.tabular_open_excel_button = QPushButton("打开当前 Excel")
        self.tabular_open_excel_button.setIcon(self.style().standardIcon(QStyle.SP_FileIcon))
        self.tabular_open_excel_button.setToolTip("打开软件自动创建的当前 Tabular Data Excel 文件。")
        self.tabular_open_excel_button.clicked.connect(self.open_current_tabular_excel)
        self.tabular_reload_excel_button = QPushButton("导入当前 Excel")
        self.tabular_reload_excel_button.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.tabular_reload_excel_button.setToolTip("从软件自动创建的当前 Excel 文件导入修改后的数据。")
        self.tabular_reload_excel_button.clicked.connect(self.import_current_generated_excel)
        self.tabular_import_button = QPushButton("从 Excel/CSV/TXT 导入")
        self.tabular_import_button.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))
        self.tabular_import_button.setToolTip("先在外部表格中修改数据，再导入当前选中的 Tabular Data。导入后点击保存设置才写入 Mechanical。")
        self.tabular_import_button.clicked.connect(self.import_current_tabular_data)
        self.tabular_import_hint_label = QLabel("选择一个 Tabular Data 后显示表格和曲线。")
        self.tabular_import_hint_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        tabular_header.addWidget(self.tabular_open_excel_button)
        tabular_header.addWidget(self.tabular_reload_excel_button)
        tabular_header.addWidget(self.tabular_import_button)
        tabular_header.addWidget(self.tabular_import_hint_label, stretch=1)
        root.addLayout(tabular_header)

        tabular_splitter = QSplitter(Qt.Vertical)
        self.tabular_summary_table = QTableWidget(0, 6)
        self.tabular_summary_table.setHorizontalHeaderLabels(
            ["对象", "属性", "来源", "列数", "行数", "状态"]
        )
        self.tabular_summary_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tabular_summary_table.setAlternatingRowColors(True)
        self.tabular_summary_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tabular_summary_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tabular_summary_table.verticalHeader().setVisible(False)
        self.tabular_summary_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tabular_summary_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tabular_summary_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tabular_summary_table.itemSelectionChanged.connect(self.update_tabular_preview)

        tabular_preview_splitter = QSplitter(Qt.Horizontal)
        self.tabular_data_table = QTableWidget(0, 0)
        self.tabular_data_table.setAlternatingRowColors(True)
        self.tabular_data_table.verticalHeader().setVisible(True)
        self.tabular_data_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tabular_data_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tabular_curve_label = QLabel("暂无曲线")
        self.tabular_curve_label.setAlignment(Qt.AlignCenter)
        self.tabular_curve_label.setMinimumSize(420, 260)
        self.tabular_curve_label.setStyleSheet("border: 1px solid #d0d5dd; background: #ffffff;")

        tabular_preview_splitter.addWidget(self.tabular_data_table)
        tabular_preview_splitter.addWidget(self.tabular_curve_label)
        tabular_preview_splitter.setSizes([540, 460])
        tabular_splitter.addWidget(self.tabular_summary_table)
        tabular_splitter.addWidget(tabular_preview_splitter)
        tabular_splitter.setSizes([150, 260])
        root.addWidget(tabular_splitter, stretch=3)

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

    def _setting_display_value(self, setting: dict) -> str:
        tabular_data = setting.get("tabular_data") or []
        if not tabular_data:
            return str(setting.get("value", ""))
        if len(tabular_data) == 1:
            table = tabular_data[0]
            row_count = int(table.get("row_count") or len(table.get("rows") or []))
            column_count = len(table.get("columns") or [])
            return f"Tabular Data ({row_count} 行 x {column_count} 列)"
        return f"Tabular Data ({len(tabular_data)} 个表)"

    def _extend_tabular_records(self, scope: str, owner: str, property_name: str, prop: dict) -> None:
        for table in prop.get("tabular_data") or []:
            rows = [list(row) for row in table.get("rows") or []]
            record = dict(table)
            record["scope"] = scope
            record["owner"] = owner
            record["property"] = property_name
            record["path"] = str(prop.get("path") or "")
            record["api_name"] = str(prop.get("api_name") or "")
            record["original_rows"] = [list(row) for row in rows]
            record["edited_rows"] = [list(row) for row in rows]
            self.tabular_records.append(record)

    def _tabular_headers(self, table: dict) -> list[str]:
        headers: list[str] = []
        for index, column in enumerate(table.get("columns") or []):
            if isinstance(column, dict):
                name = str(column.get("name") or f"Column {index + 1}")
                unit = str(column.get("unit") or "").strip()
                role = str(column.get("role") or "").strip()
                header = name
                if unit:
                    header = f"{header} [{unit}]"
                if role in {"input", "output"}:
                    header = f"{header} ({role})"
                headers.append(header)
            else:
                headers.append(str(column) or f"Column {index + 1}")
        return headers

    def _safe_file_part(self, value: object, fallback: str) -> str:
        text_value = str(value or "").strip()
        text_value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text_value)
        text_value = re.sub(r"\s+", "_", text_value).strip("._ ")
        if not text_value:
            text_value = fallback
        return text_value[:80]

    def _excel_column_name(self, index: int) -> str:
        index += 1
        name = ""
        while index > 0:
            index, remainder = divmod(index - 1, 26)
            name = chr(ord("A") + remainder) + name
        return name

    def _xml_escape_text(self, value: object) -> str:
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    def _xlsx_text_cell(self, row_index: int, column_index: int, value: object) -> str:
        cell_ref = f"{self._excel_column_name(column_index)}{row_index + 1}"
        escaped = self._xml_escape_text(value)
        return f'<c r="{cell_ref}" t="inlineStr"><is><t>{escaped}</t></is></c>'

    def _xlsx_sheet_xml(self, headers: list[str], rows: list[list[str]]) -> str:
        sheet_rows: list[str] = []
        all_rows = [headers] + rows
        for row_index, row_values in enumerate(all_rows):
            cells = [
                self._xlsx_text_cell(row_index, column_index, row_values[column_index] if column_index < len(row_values) else "")
                for column_index in range(len(headers))
            ]
            sheet_rows.append(f'<row r="{row_index + 1}">{"".join(cells)}</row>')
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
            f'<sheetData>{"".join(sheet_rows)}</sheetData>'
            "</worksheet>"
        )

    def _write_tabular_xlsx(self, path: Path, headers: list[str], rows: list[list[str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        sheet_xml = self._xlsx_sheet_xml(headers, rows)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                "</Types>",
            )
            archive.writestr(
                "_rels/.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                "</Relationships>",
            )
            archive.writestr(
                "xl/workbook.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="TabularData" sheetId="1" r:id="rId1"/></sheets>'
                "</workbook>",
            )
            archive.writestr(
                "xl/_rels/workbook.xml.rels",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                "</Relationships>",
            )
            archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)

    def create_tabular_excel_files(self) -> None:
        if not self.tabular_records:
            return
        folder_name = "_".join(
            [
                self._safe_file_part(self.module.system_name, "module"),
                self._safe_file_part(self.module.display_text, "analysis"),
                datetime.now().strftime("%Y%m%d_%H%M%S"),
            ]
        )
        output_dir = WORKSPACE / "exports" / "tabular_data" / folder_name
        used_names: set[str] = set()
        for index, table in enumerate(self.tabular_records, start=1):
            headers = self._tabular_headers(table)
            rows = [list(row) for row in table.get("edited_rows") or []]
            base_name = "_".join(
                [
                    f"{index:02d}",
                    self._safe_file_part(table.get("owner"), "owner"),
                    self._safe_file_part(table.get("property") or table.get("title"), "tabular"),
                ]
            )
            candidate = base_name
            suffix = 2
            while candidate.lower() in used_names:
                candidate = f"{base_name}_{suffix}"
                suffix += 1
            used_names.add(candidate.lower())
            excel_path = output_dir / f"{candidate}.xlsx"
            try:
                self._write_tabular_xlsx(excel_path, headers, rows)
                table["excel_path"] = str(excel_path)
                table["excel_error"] = ""
            except Exception as exc:
                table["excel_path"] = ""
                table["excel_error"] = str(exc)

    def _column_name_for_import(self, column: object, index: int) -> str:
        if isinstance(column, dict):
            return str(column.get("name") or f"Column {index + 1}").strip().lower()
        return str(column or f"Column {index + 1}").strip().lower()

    def _numeric_cell_value(self, value: object) -> float | None:
        text_value = str(value).strip().replace(",", "")
        if not text_value:
            return None
        match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", text_value)
        if match is None:
            return None
        try:
            return float(match.group(0))
        except ValueError:
            return None

    def _xlsx_column_index(self, cell_ref: str) -> int:
        match = re.match(r"([A-Za-z]+)", cell_ref or "")
        if match is None:
            return 0
        index = 0
        for char in match.group(1).upper():
            index = index * 26 + (ord(char) - ord("A") + 1)
        return max(0, index - 1)

    def _xml_local_name(self, tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def _read_xlsx_first_sheet_rows(self, path: Path) -> list[list[str]]:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            shared_strings: list[str] = []
            if "xl/sharedStrings.xml" in names:
                shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                for item in shared_root.iter():
                    if self._xml_local_name(item.tag) != "si":
                        continue
                    parts = [
                        node.text or ""
                        for node in item.iter()
                        if self._xml_local_name(node.tag) == "t"
                    ]
                    shared_strings.append("".join(parts))

            sheet_names = sorted(
                name
                for name in names
                if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
            )
            if not sheet_names:
                raise ValueError("Excel 文件中没有找到工作表。")

            sheet_root = ET.fromstring(archive.read(sheet_names[0]))
            rows: list[list[str]] = []
            for row_node in sheet_root.iter():
                if self._xml_local_name(row_node.tag) != "row":
                    continue
                row_values: list[str] = []
                for cell_node in row_node:
                    if self._xml_local_name(cell_node.tag) != "c":
                        continue
                    column_index = self._xlsx_column_index(cell_node.attrib.get("r", ""))
                    while len(row_values) <= column_index:
                        row_values.append("")
                    cell_type = cell_node.attrib.get("t", "")
                    value = ""
                    if cell_type == "inlineStr":
                        value = "".join(
                            node.text or ""
                            for node in cell_node.iter()
                            if self._xml_local_name(node.tag) == "t"
                        )
                    else:
                        value_node = next(
                            (
                                node
                                for node in cell_node
                                if self._xml_local_name(node.tag) == "v"
                            ),
                            None,
                        )
                        raw_value = value_node.text if value_node is not None else ""
                        if cell_type == "s" and raw_value != "":
                            try:
                                value = shared_strings[int(raw_value)]
                            except Exception:
                                value = raw_value
                        else:
                            value = raw_value or ""
                    row_values[column_index] = str(value).strip()
                if any(cell.strip() for cell in row_values):
                    rows.append(row_values)
            return rows

    def _read_delimited_tabular_rows(self, path: Path) -> list[list[str]]:
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            return []
        sample = "\n".join(lines[:20])
        delimiter = ","
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            delimiter = dialect.delimiter
        except Exception:
            delimiter = "\t" if "\t" in sample else ("," if "," in sample else "")
        if delimiter:
            return [
                [cell.strip() for cell in row]
                for row in csv.reader(lines, delimiter=delimiter)
                if any(cell.strip() for cell in row)
            ]
        return [
            [cell.strip() for cell in re.split(r"\s+", line.strip())]
            for line in lines
        ]

    def _read_tabular_import_file(self, path: Path) -> list[list[str]]:
        suffix = path.suffix.lower()
        if suffix in {".xlsx", ".xlsm"}:
            return self._read_xlsx_first_sheet_rows(path)
        if suffix in {".csv", ".txt", ".tsv"}:
            return self._read_delimited_tabular_rows(path)
        if suffix == ".xls":
            raise ValueError("暂不支持老式 .xls 二进制格式，请另存为 .xlsx、.csv 或 .txt 后导入。")
        raise ValueError(f"不支持的表格文件格式: {suffix}")

    def _normalise_import_rows(self, raw_rows: list[list[str]], table: dict) -> list[list[str]]:
        rows = [
            [str(cell).strip() for cell in row]
            for row in raw_rows
            if any(str(cell).strip() for cell in row)
        ]
        if not rows:
            raise ValueError("导入文件中没有有效数据行。")

        columns = table.get("columns") or []
        column_count = len(columns)
        if column_count <= 0:
            column_count = max(len(row) for row in rows)

        first_row = rows[0]
        expected_names = [
            self._column_name_for_import(column, index)
            for index, column in enumerate(columns)
        ]
        first_row_names = [str(cell).strip().lower() for cell in first_row[:column_count]]
        header_match = any(
            cell and expected and (cell == expected or cell in expected or expected in cell)
            for cell, expected in zip(first_row_names, expected_names)
        )
        header_like = header_match or any(
            cell and self._numeric_cell_value(cell) is None
            for cell in first_row[:column_count]
        )
        if header_like and len(rows) > 1:
            rows = rows[1:]

        normalised: list[list[str]] = []
        for row in rows:
            values = list(row[:column_count])
            while len(values) < column_count:
                values.append("")
            if any(value.strip() for value in values):
                normalised.append(values)
        if not normalised:
            raise ValueError("导入文件去掉表头后没有有效数据。")
        return normalised

    def _store_current_tabular_edits(self) -> None:
        return

    def update_tabular_preview(self) -> None:
        selected = self.tabular_summary_table.selectionModel().selectedRows()
        if not selected:
            return
        index = selected[0].row()
        if self.current_tabular_index != index:
            self._store_current_tabular_edits()
        self.show_tabular_preview(index)

    def show_tabular_preview(self, index: int) -> None:
        self.current_tabular_index = index
        if not (0 <= index < len(self.tabular_records)):
            self.tabular_data_table.clear()
            self.tabular_data_table.setRowCount(0)
            self.tabular_data_table.setColumnCount(0)
            self.tabular_curve_label.setText("暂无曲线")
            self.tabular_curve_label.setPixmap(QPixmap())
            self.tabular_open_excel_button.setEnabled(False)
            self.tabular_reload_excel_button.setEnabled(False)
            self.tabular_import_button.setEnabled(False)
            return
        table = self.tabular_records[index]
        headers = self._tabular_headers(table)
        rows = table.get("edited_rows") or []
        editable = bool(table.get("editable", False))

        self.tabular_data_table.blockSignals(True)
        self.tabular_data_table.clear()
        self.tabular_data_table.setColumnCount(len(headers))
        self.tabular_data_table.setHorizontalHeaderLabels(headers)
        self.tabular_data_table.setRowCount(len(rows))
        for row_index, row_values in enumerate(rows):
            for column_index in range(len(headers)):
                value = str(row_values[column_index]) if column_index < len(row_values) else ""
                item = QTableWidgetItem(value)
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.tabular_data_table.setItem(row_index, column_index, item)
        self.tabular_data_table.blockSignals(False)
        self.tabular_data_table.resizeRowsToContents()
        self.tabular_data_table.resizeColumnsToContents()
        excel_path = str(table.get("excel_path") or "")
        has_excel = bool(excel_path and Path(excel_path).is_file())
        self.tabular_open_excel_button.setEnabled(has_excel)
        self.tabular_reload_excel_button.setEnabled(editable and has_excel)
        self.tabular_import_button.setEnabled(editable)
        if editable:
            imported_file = str(table.get("imported_file") or "")
            if imported_file:
                self.tabular_import_hint_label.setText(f"已导入: {imported_file}")
            elif excel_path:
                self.tabular_import_hint_label.setText(f"已创建当前数据 Excel: {excel_path}")
            else:
                self.tabular_import_hint_label.setText("修改数据请先编辑外部 Excel/CSV/TXT，再导入当前 Tabular Data。")
        else:
            if excel_path:
                self.tabular_import_hint_label.setText(f"已创建只读预览 Excel: {excel_path}")
            else:
                self.tabular_import_hint_label.setText("当前 Tabular Data 暂按只读显示，不能写回。")
        self.update_tabular_curve(index)

    def update_tabular_curve(self, index: int) -> None:
        if not (0 <= index < len(self.tabular_records)):
            self.tabular_curve_label.setText("暂无曲线")
            self.tabular_curve_label.setPixmap(QPixmap())
            return
        pixmap = self.render_tabular_curve(self.tabular_records[index])
        if pixmap is None:
            self.tabular_curve_label.setPixmap(QPixmap())
            self.tabular_curve_label.setText("没有可绘制的数值曲线")
        else:
            self.tabular_curve_label.setText("")
            self.tabular_curve_label.setPixmap(pixmap)

    def render_tabular_curve(self, table: dict) -> QPixmap | None:
        rows = table.get("edited_rows") or []
        columns = table.get("columns") or []
        if len(rows) < 1 or len(columns) < 2:
            return None

        x_index = 0
        for index, column in enumerate(columns):
            if isinstance(column, dict) and str(column.get("role") or "") == "input":
                x_index = index
                break
        y_indices = [
            index
            for index, column in enumerate(columns)
            if index != x_index and isinstance(column, dict) and str(column.get("role") or "") == "output"
        ]
        if not y_indices:
            y_indices = [index for index in range(len(columns)) if index != x_index]

        series: list[tuple[int, list[tuple[float, float]]]] = []
        all_x: list[float] = []
        all_y: list[float] = []
        for y_index in y_indices:
            points: list[tuple[float, float]] = []
            for row in rows:
                if x_index >= len(row) or y_index >= len(row):
                    continue
                x_value = self._numeric_cell_value(row[x_index])
                y_value = self._numeric_cell_value(row[y_index])
                if x_value is None or y_value is None:
                    continue
                points.append((x_value, y_value))
                all_x.append(x_value)
                all_y.append(y_value)
            if points:
                series.append((y_index, points))
        if not series or not all_x or not all_y:
            return None

        width = 640
        height = 320
        left = 68
        top = 32
        right = 28
        bottom = 54
        plot_width = width - left - right
        plot_height = height - top - bottom
        x_min, x_max = min(all_x), max(all_x)
        y_min, y_max = min(all_y), max(all_y)
        if x_min == x_max:
            x_min -= 1.0
            x_max += 1.0
        if y_min == y_max:
            y_min -= 1.0
            y_max += 1.0
        y_pad = (y_max - y_min) * 0.08
        y_min -= y_pad
        y_max += y_pad

        pixmap = QPixmap(width, height)
        pixmap.fill(QColor("#ffffff"))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#1f2937"), 1))
        painter.drawRect(left, top, plot_width, plot_height)

        painter.setPen(QPen(QColor("#e5e7eb"), 1))
        for step in range(1, 5):
            x = left + int(plot_width * step / 5)
            y = top + int(plot_height * step / 5)
            painter.drawLine(x, top, x, top + plot_height)
            painter.drawLine(left, y, left + plot_width, y)

        painter.setPen(QPen(QColor("#374151"), 1))
        title = f"{table.get('owner', '')} / {table.get('property', table.get('title', ''))}"
        painter.drawText(left, 20, str(title)[:90])
        painter.drawText(left, height - 18, f"X: {self._tabular_headers(table)[x_index]}")
        painter.drawText(8, top + 12, f"{y_max:.4g}")
        painter.drawText(8, top + plot_height, f"{y_min:.4g}")
        painter.drawText(left, top + plot_height + 18, f"{x_min:.4g}")
        painter.drawText(left + plot_width - 60, top + plot_height + 18, f"{x_max:.4g}")

        colors = [
            QColor("#2563eb"),
            QColor("#dc2626"),
            QColor("#16a34a"),
            QColor("#9333ea"),
            QColor("#ea580c"),
            QColor("#0891b2"),
        ]

        def map_point(point: tuple[float, float]) -> tuple[int, int]:
            x_value, y_value = point
            x = left + int((x_value - x_min) / (x_max - x_min) * plot_width)
            y = top + plot_height - int((y_value - y_min) / (y_max - y_min) * plot_height)
            return x, y

        headers = self._tabular_headers(table)
        for series_index, (y_index, points) in enumerate(series):
            color = colors[series_index % len(colors)]
            painter.setPen(QPen(color, 2))
            previous = None
            for point in points:
                mapped = map_point(point)
                if previous is not None:
                    painter.drawLine(previous[0], previous[1], mapped[0], mapped[1])
                painter.drawEllipse(mapped[0] - 2, mapped[1] - 2, 4, 4)
                previous = mapped
            painter.setPen(QPen(color, 1))
            legend_y = 38 + series_index * 18
            painter.drawLine(width - 170, legend_y - 5, width - 145, legend_y - 5)
            painter.drawText(width - 140, legend_y, headers[y_index][:28])

        painter.end()
        return pixmap

    def current_tabular_record(self) -> dict | None:
        if self.current_tabular_index is None or not (0 <= self.current_tabular_index < len(self.tabular_records)):
            return None
        return self.tabular_records[self.current_tabular_index]

    def open_path_with_default_app(self, path: Path) -> None:
        try:
            os.startfile(str(path))
        except Exception:
            subprocess.Popen(["explorer", str(path)])

    def open_current_tabular_excel(self) -> None:
        table = self.current_tabular_record()
        if table is None:
            QMessageBox.information(self, "未选择 Tabular Data", "请先选择一个 Tabular Data。")
            return
        excel_path = Path(str(table.get("excel_path") or ""))
        if not excel_path.is_file():
            error_text = str(table.get("excel_error") or "")
            QMessageBox.warning(self, "Excel 未创建", error_text or "当前 Tabular Data 没有可打开的 Excel 文件。")
            return
        self.open_path_with_default_app(excel_path)

    def import_tabular_rows_from_path(self, table: dict, path: Path) -> None:
        raw_rows = self._read_tabular_import_file(path)
        rows = self._normalise_import_rows(raw_rows, table)
        table["edited_rows"] = rows
        table["row_count"] = len(rows)
        table["truncated"] = False
        table["imported_file"] = str(path)

    def refresh_current_tabular_preview_after_import(self, selected_index: int) -> None:
        self.populate_tabular_data()
        self.tabular_summary_table.selectRow(selected_index)
        self.show_tabular_preview(selected_index)

    def import_current_generated_excel(self) -> None:
        table = self.current_tabular_record()
        if table is None:
            QMessageBox.information(self, "未选择 Tabular Data", "请先选择一个 Tabular Data。")
            return
        if not table.get("editable"):
            QMessageBox.warning(self, "不能导入", "当前 Tabular Data 暂按只读显示，不能写回 Mechanical。")
            return
        excel_path = Path(str(table.get("excel_path") or ""))
        if not excel_path.is_file():
            QMessageBox.warning(self, "Excel 未创建", "当前 Tabular Data 没有可导入的 Excel 文件。")
            return
        selected_index = self.current_tabular_index or 0
        try:
            self.import_tabular_rows_from_path(table, excel_path)
        except Exception as exc:
            QMessageBox.critical(self, "导入失败", str(exc))
            return
        self.refresh_current_tabular_preview_after_import(selected_index)

    def import_current_tabular_data(self) -> None:
        table = self.current_tabular_record()
        if table is None:
            QMessageBox.information(self, "未选择 Tabular Data", "请先选择一个 Tabular Data。")
            return
        if not table.get("editable"):
            QMessageBox.warning(self, "不能导入", "当前 Tabular Data 暂按只读显示，不能写回 Mechanical。")
            return
        excel_path = Path(str(table.get("excel_path") or ""))
        start_dir = str(excel_path.parent) if excel_path.is_file() else ""
        file_name, _ = QFileDialog.getOpenFileName(
            self,
            "导入 Tabular Data",
            start_dir,
            "Excel/CSV/TXT (*.xlsx *.xlsm *.csv *.txt *.tsv);;All Files (*)",
        )
        if not file_name:
            return
        path = Path(file_name)
        selected_index = self.current_tabular_index or 0
        try:
            self.import_tabular_rows_from_path(table, path)
        except Exception as exc:
            QMessageBox.critical(self, "导入失败", str(exc))
            return
        self.refresh_current_tabular_preview_after_import(selected_index)

    def import_generated_excels_before_save(self) -> bool:
        errors: list[str] = []
        selected_index = self.current_tabular_index or 0
        for table in self.tabular_records:
            if not table.get("editable"):
                continue
            excel_path = Path(str(table.get("excel_path") or ""))
            if not excel_path.is_file():
                continue
            imported_file = str(table.get("imported_file") or "")
            if imported_file and Path(imported_file) != excel_path:
                continue
            try:
                self.import_tabular_rows_from_path(table, excel_path)
            except Exception as exc:
                label = f"{table.get('owner', '')} / {table.get('property', table.get('title', ''))}"
                errors.append(f"{label}: {exc}")
        if errors:
            QMessageBox.critical(
                self,
                "导入 Excel 失败",
                "保存设置前自动读取当前 Excel 失败，请先关闭 Excel 或检查表格格式。\n\n"
                + "\n".join(errors[:10]),
            )
            return False
        if self.tabular_records:
            self.refresh_current_tabular_preview_after_import(
                min(selected_index, len(self.tabular_records) - 1)
            )
        return True

    def populate_tabular_data(self) -> None:
        self.current_tabular_index = None
        self.tabular_summary_table.setRowCount(len(self.tabular_records))
        for row, table in enumerate(self.tabular_records):
            column_count = len(table.get("columns") or [])
            row_count = int(table.get("row_count") or len(table.get("rows") or []))
            status_parts = []
            if table.get("editable"):
                status_parts.append("可编辑")
            else:
                status_parts.append("只读")
            if table.get("truncated"):
                status_parts.append("已截断预览")
            if table.get("imported_file"):
                status_parts.append("已导入")
            if table.get("excel_path"):
                status_parts.append("已创建Excel")
            if table.get("excel_error"):
                status_parts.append("Excel创建失败")
            values = [
                str(table.get("owner") or ""),
                str(table.get("property") or table.get("title") or ""),
                str(table.get("source") or ""),
                str(column_count),
                str(row_count),
                " / ".join(status_parts),
            ]
            for column, value in enumerate(values):
                self.tabular_summary_table.setItem(row, column, QTableWidgetItem(value))
        self.tabular_summary_table.resizeRowsToContents()
        if self.tabular_records:
            self.tabular_summary_table.selectRow(0)
            self.show_tabular_preview(0)
        else:
            self.tabular_data_table.clear()
            self.tabular_data_table.setRowCount(0)
            self.tabular_data_table.setColumnCount(0)
            self.tabular_curve_label.setText("暂无曲线")
            self.tabular_curve_label.setPixmap(QPixmap())
            self.tabular_open_excel_button.setEnabled(False)
            self.tabular_reload_excel_button.setEnabled(False)
            self.tabular_import_button.setEnabled(False)

    def tabular_updates(self) -> tuple[dict[str, object], list[dict]]:
        self._store_current_tabular_edits()
        settings_updates: dict[str, object] = {}
        condition_updates: list[dict] = []
        for table in self.tabular_records:
            if not table.get("editable"):
                continue
            edited_rows = table.get("edited_rows") or []
            original_rows = table.get("original_rows") or []
            if edited_rows == original_rows:
                continue
            payload = {
                "kind": "tabular",
                "path": str(table.get("path") or ""),
                "api_name": str(table.get("api_name") or ""),
                "columns": table.get("columns") or [],
                "rows": edited_rows,
            }
            if table.get("scope") == "settings":
                settings_updates[payload["api_name"]] = payload
            else:
                condition_updates.append(payload)
        return settings_updates, condition_updates

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
        self.tabular_records.clear()
        self.setting_infos = [dict(item) for item in result.report.get("settings") or []]
        self.settings_table.setRowCount(len(self.setting_infos))
        for row, setting in enumerate(self.setting_infos):
            api_name = str(setting.get("api_name", ""))
            control_value = str(setting.get("value", ""))
            current_value = self._setting_display_value(setting)
            self.original_values[api_name] = control_value
            self._extend_tabular_records(
                "settings",
                "Analysis Settings",
                str(setting.get("display_name") or api_name),
                setting,
            )
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
                prop_payload = dict(prop, path=condition_name, api_name=api_name)
                self._extend_tabular_records("condition", condition_name, property_name, prop_payload)
                property_value = self._setting_display_value(prop)
                condition_rows.append(
                    (
                        condition_name,
                        condition_type,
                        condition_state,
                        property_name,
                        property_value,
                        prop_payload,
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
            control_value = str(prop.get("value", ""))
            self.original_condition_values[key] = control_value
            control = self._build_control(prop)
            self.condition_controls[key] = control
            self.conditions_table.setCellWidget(row, 5, control)
        self.conditions_table.resizeRowsToContents()
        self.create_tabular_excel_files()
        self.populate_tabular_data()

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
        if not self.import_generated_excels_before_save():
            return
        settings_update = self.settings_update()
        conditions_update = self.conditions_update()
        tabular_settings_update, tabular_conditions_update = self.tabular_updates()
        settings_update.update(tabular_settings_update)
        conditions_update.extend(tabular_conditions_update)
        self.save_requested.emit(
            {
                "settings": settings_update,
                "conditions": conditions_update,
            }
        )

    def set_busy(self, busy: bool) -> None:
        self.save_button.setEnabled(not busy)
        self.close_button.setEnabled(not busy)
        can_import = False
        has_excel = False
        if self.current_tabular_index is not None and 0 <= self.current_tabular_index < len(self.tabular_records):
            current = self.tabular_records[self.current_tabular_index]
            can_import = bool(current.get("editable"))
            excel_path = Path(str(current.get("excel_path") or ""))
            has_excel = excel_path.is_file()
        self.tabular_open_excel_button.setEnabled(not busy and has_excel)
        self.tabular_reload_excel_button.setEnabled(not busy and can_import and has_excel)
        self.tabular_import_button.setEnabled(not busy and can_import)


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
        self.resize(1260, 840)
        self.setMinimumSize(1100, 720)

        self.selected_project: Path | None = PROJECT_FILE
        self.selected_mechanical_export_folder: Path | None = None
        self.selected_zemax_project: Path | None = None
        self.current_zemax_info: dict[str, object] = {}
        self.zemax_pose_records: list[dict[str, object]] = []
        self.zemax_detectors: list[dict[str, object]] = []
        self.zemax_detector_results: dict[int, dict[str, object]] = {}
        self.zemax_detector_image_path: Path | None = None
        self.zemax_detector_preview_pixmap: QPixmap | None = None
        self.zemax_detector_preview_zoom_factor = 1.0
        self.zemax_detector_preview_fit_to_window = True
        self.zemax_time_series_path: Path | None = None
        self.zemax_time_series_manifest: dict[str, object] = {}
        self.zemax_time_series_frames: list[dict[str, object]] = []
        self.zemax_time_series_current_index = 0
        self.zemax_time_series_timer = QTimer(self)
        self.zemax_time_series_timer.timeout.connect(self.advance_zemax_time_series_frame)
        self.zemax_raytrace_dialog: QDialog | None = None
        self.modules: list[AnalysisModule] = []
        self.active_thread: QThread | None = None
        self.active_worker: QObject | None = None
        self.settings_dialog: AnalysisSettingsDialog | None = None
        self.results_dialog: SolutionResultsDialog | None = None
        self.pending_settings_dialog: AnalysisSettingsDialog | None = None
        self.pending_results_dialog: SolutionResultsDialog | None = None
        self.mechanical_session = None
        self.mechanical_port: int | None = None
        self.module_load_show_errors = True
        self.load_modules_after_mechanical_launch = False
        self.operation_started_at: float | None = None
        self.operation_stage = "空闲"
        self.operation_status = "等待操作"
        self.operation_timer = QTimer(self)
        self.operation_timer.timeout.connect(self.update_operation_timer)

        central = QWidget()
        central.setObjectName("appShell")
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 14)
        root.setSpacing(10)

        body_splitter = QSplitter(Qt.Vertical)
        body_splitter.setObjectName("bodySplitter")
        body_splitter.setChildrenCollapsible(False)
        root.addWidget(body_splitter, stretch=1)

        main_area = QWidget()
        main_area.setObjectName("mainGlass")
        main_area_layout = QHBoxLayout(main_area)
        main_area_layout.setContentsMargins(8, 8, 8, 8)
        main_area_layout.setSpacing(10)

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(9, 12, 9, 12)
        sidebar_layout.setSpacing(8)

        sidebar_title = QLabel("控制中心")
        sidebar_title.setObjectName("sidebarTitle")
        sidebar_layout.addWidget(sidebar_title)

        self.mechanical_nav_button = QPushButton("Mechanical")
        self.mechanical_nav_button.setObjectName("navButton")
        self.mechanical_nav_button.setCheckable(True)
        self.mechanical_nav_button.clicked.connect(lambda: self._set_main_view(0))
        sidebar_layout.addWidget(self.mechanical_nav_button)

        self.zemax_nav_button = QPushButton("Zemax")
        self.zemax_nav_button.setObjectName("navButton")
        self.zemax_nav_button.setCheckable(True)
        self.zemax_nav_button.clicked.connect(lambda: self._set_main_view(1))
        sidebar_layout.addWidget(self.zemax_nav_button)
        sidebar_layout.addStretch(1)

        sidebar_hint = QLabel("Windows 软件")
        sidebar_hint.setObjectName("sidebarHint")
        sidebar_layout.addWidget(sidebar_hint)

        self.main_stack = QStackedWidget()
        self.main_stack.setObjectName("mainStack")
        self.main_stack.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.main_stack.addWidget(self._build_mechanical_tab())
        self.main_stack.addWidget(self._build_zemax_tab())

        main_area_layout.addWidget(sidebar)
        main_area_layout.addWidget(self.main_stack, stretch=1)
        body_splitter.addWidget(main_area)

        bottom_splitter = QSplitter(Qt.Horizontal)
        bottom_splitter.setObjectName("bottomSplitter")
        bottom_splitter.setChildrenCollapsible(False)
        bottom_splitter.addWidget(self._build_operation_group())
        bottom_splitter.addWidget(self._build_log_group())
        bottom_splitter.setStretchFactor(0, 1)
        bottom_splitter.setStretchFactor(1, 3)
        bottom_splitter.setSizes([360, 860])
        body_splitter.addWidget(bottom_splitter)
        body_splitter.setSizes([640, 180])

        self.setCentralWidget(central)
        self._build_menu()
        self._apply_style()
        self._set_main_view(0)
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

    def _set_main_view(self, index: int) -> None:
        self.main_stack.setCurrentIndex(index)
        self.mechanical_nav_button.setChecked(index == 0)
        self.zemax_nav_button.setChecked(index == 1)

    def _build_mechanical_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        top_row = QWidget()
        top_row_layout = QHBoxLayout(top_row)
        top_row_layout.setContentsMargins(0, 0, 0, 0)
        top_row_layout.setSpacing(12)
        top_row_layout.addWidget(self._build_mechanical_console_panel(), stretch=1)
        top_row_layout.addWidget(self._build_status_group())
        layout.addWidget(top_row)
        layout.addWidget(self._build_modules_group(), stretch=1)
        return tab

    def _build_mechanical_console_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(0)
        panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        title = QLabel("Mechanical 控制台")
        title.setObjectName("toolbarTitle")
        caption = QLabel("后台 Mechanical 会话 / database")
        caption.setObjectName("toolbarCaption")
        layout.addWidget(title)
        layout.addWidget(caption)
        layout.addWidget(self._build_project_toolbar())
        return panel

    def _build_project_toolbar(self) -> QWidget:
        toolbar = QWidget()
        toolbar.setObjectName("projectToolbar")
        toolbar.setMinimumWidth(0)
        toolbar.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        toolbar.setMaximumWidth(620)
        toolbar_layout = QVBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(9, 6, 9, 6)
        toolbar_layout.setSpacing(5)

        self.project_path_edit = QLineEdit(str(self.selected_project or ""))
        self.project_path_edit.setObjectName("pathField")
        self.project_path_edit.setPlaceholderText("选择 .mechdb/.mechdat database")
        self.project_path_edit.setClearButtonEnabled(True)
        self.project_path_edit.editingFinished.connect(self.project_path_edited)
        self.project_path_edit.setMinimumWidth(0)
        self.project_path_edit.setMaximumWidth(520)
        self.project_path_edit.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)

        self.browse_project_button = QPushButton("选择文件")
        self.browse_project_button.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))
        self.browse_project_button.clicked.connect(self.browse_project)

        self.load_modules_button = QPushButton("读取模块")
        self.load_modules_button.setObjectName("primaryAction")
        self.load_modules_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogDetailedView))
        self.load_modules_button.clicked.connect(self.load_analysis_modules)

        self.check_button = QPushButton("检查环境")
        self.check_button.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.check_button.clicked.connect(self.refresh_status)

        self.close_mechanical_button = QPushButton("关闭后台")
        self.close_mechanical_button.setObjectName("dangerButton")
        self.close_mechanical_button.setIcon(self.style().standardIcon(QStyle.SP_DialogCloseButton))
        self.close_mechanical_button.clicked.connect(self.close_mechanical)

        file_row = QHBoxLayout()
        file_row.setContentsMargins(0, 0, 0, 0)
        file_row.setSpacing(6)
        file_row.addWidget(self.project_path_edit, stretch=1)
        file_row.addWidget(self.browse_project_button)
        file_row.addWidget(self.load_modules_button)
        toolbar_layout.addLayout(file_row)

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(6)
        action_row.addStretch(1)
        action_row.addWidget(self.check_button)
        action_row.addWidget(self.close_mechanical_button)
        toolbar_layout.addLayout(action_row)
        return toolbar

    def _build_zemax_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter)

        top_splitter = QSplitter(Qt.Horizontal)
        top_splitter.setChildrenCollapsible(False)
        top_splitter.addWidget(self._build_zemax_project_action_group())
        top_splitter.addWidget(self._build_zemax_status_group())
        top_splitter.setStretchFactor(0, 2)
        top_splitter.setStretchFactor(1, 1)
        top_splitter.setSizes([760, 420])

        splitter.addWidget(top_splitter)
        splitter.addWidget(self._build_zemax_pose_import_group())
        splitter.addWidget(self._build_zemax_raytrace_launcher_group())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 4)
        splitter.setStretchFactor(2, 1)
        splitter.setSizes([120, 430, 90])
        return tab

    def _build_project_group(self) -> QGroupBox:
        project_group = QGroupBox("Mechanical database")
        project_layout = QVBoxLayout(project_group)
        project_layout.setContentsMargins(8, 8, 8, 8)
        project_layout.setSpacing(6)

        file_layout = QHBoxLayout()
        file_layout.setSpacing(6)

        self.project_path_edit = QLineEdit(str(self.selected_project or ""))
        self.project_path_edit.setPlaceholderText("选择 .mechdb/.mechdat database")
        self.project_path_edit.setClearButtonEnabled(True)
        self.project_path_edit.editingFinished.connect(self.project_path_edited)
        self.project_path_edit.setMinimumWidth(180)

        self.browse_project_button = QPushButton("选择文件")
        self.browse_project_button.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))
        self.browse_project_button.clicked.connect(self.browse_project)

        self.load_modules_button = QPushButton("读取模块")
        self.load_modules_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogDetailedView))
        self.load_modules_button.clicked.connect(self.load_analysis_modules)

        file_layout.addWidget(self.project_path_edit, stretch=1)
        file_layout.addWidget(self.browse_project_button)
        file_layout.addWidget(self.load_modules_button)
        project_layout.addLayout(file_layout)

        action_layout = QHBoxLayout()
        action_layout.setSpacing(6)

        self.close_mechanical_button = QPushButton("关闭后台 Mechanical")
        self.close_mechanical_button.setIcon(self.style().standardIcon(QStyle.SP_DialogCloseButton))
        self.close_mechanical_button.clicked.connect(self.close_mechanical)

        self.check_button = QPushButton("检查环境")
        self.check_button.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.check_button.clicked.connect(self.refresh_status)

        action_layout.addWidget(self.close_mechanical_button)
        action_layout.addStretch(1)
        action_layout.addWidget(self.check_button)
        project_layout.addLayout(action_layout)
        return project_group

    def _build_zemax_project_action_group(self) -> QGroupBox:
        project_group = QGroupBox("Zemax 工程文件 / 操作")
        project_layout = QVBoxLayout(project_group)
        project_layout.setContentsMargins(8, 8, 8, 8)
        project_layout.setSpacing(6)

        file_layout = QHBoxLayout()
        file_layout.setSpacing(6)

        self.zemax_project_path_edit = QLineEdit(str(self.selected_zemax_project or ""))
        self.zemax_project_path_edit.setPlaceholderText("选择 .zmx/.zos/.zar/.zprj Zemax 工程文件")
        self.zemax_project_path_edit.setClearButtonEnabled(True)
        self.zemax_project_path_edit.editingFinished.connect(self.zemax_project_path_edited)

        self.browse_zemax_project_button = QPushButton("选择文件")
        self.browse_zemax_project_button.setIcon(self.style().standardIcon(QStyle.SP_DialogOpenButton))
        self.browse_zemax_project_button.clicked.connect(self.browse_zemax_project)

        file_layout.addWidget(self.zemax_project_path_edit, stretch=1)
        file_layout.addWidget(self.browse_zemax_project_button)
        project_layout.addLayout(file_layout)

        action_layout = QHBoxLayout()
        action_layout.setSpacing(6)
        self.close_zemax_button = QPushButton("关闭 Zemax")
        self.close_zemax_button.setIcon(self.style().standardIcon(QStyle.SP_DialogCloseButton))
        self.close_zemax_button.clicked.connect(self.close_zemax)

        self.refresh_zemax_button = QPushButton("刷新状态")
        self.refresh_zemax_button.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.refresh_zemax_button.clicked.connect(self.update_zemax_status)

        action_layout.addWidget(self.close_zemax_button)
        action_layout.addStretch(1)
        action_layout.addWidget(self.refresh_zemax_button)
        project_layout.addLayout(action_layout)
        return project_group

    def _build_zemax_pose_import_group(self) -> QGroupBox:
        import_group = QGroupBox("非序列镜片位移/旋转导入")
        layout = QVBoxLayout(import_group)

        import_tabs = QTabWidget()
        import_tabs.addTab(self._build_zemax_steady_import_tab(), "稳态导入")
        import_tabs.addTab(self._build_zemax_transient_import_tab(), "瞬态导入")
        import_tabs.addTab(self._build_zemax_random_import_tab(), "随机振动导入")
        layout.addWidget(import_tabs)

        self.zemax_pose_table = QTableWidget(0, 8)
        self.zemax_pose_table.setHorizontalHeaderLabels(["名称/Comment", "X", "Y", "Z", "Rx", "Ry", "Rz", "来源"])
        self.zemax_pose_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.zemax_pose_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.zemax_pose_table.setAlternatingRowColors(True)
        self.zemax_pose_table.verticalHeader().setVisible(False)
        self.zemax_pose_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.zemax_pose_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        layout.addWidget(self.zemax_pose_table, stretch=1)
        return import_group

    def _build_zemax_steady_import_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        label = QLabel("稳态导入：选择一个 Mechanical 结果导出文件夹，按 Comment 匹配并把单帧位移/旋转导入所选 Zemax 工程。")
        label.setWordWrap(True)
        layout.addWidget(label)

        actions = QHBoxLayout()
        self.import_pose_button = QPushButton("选择文件夹并稳态导入")
        self.import_pose_button.setIcon(self.style().standardIcon(QStyle.SP_DialogApplyButton))
        self.import_pose_button.setToolTip("选择 Mechanical 结果导出文件夹，并导入镜片位移/旋转到当前所选 Zemax 工程。")
        self.import_pose_button.clicked.connect(self.import_mechanical_pose_to_zemax)
        self.steady_raytrace_button = QPushButton("稳态清空并追迹")
        self.steady_raytrace_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.steady_raytrace_button.clicked.connect(lambda: self.run_zemax_ray_trace(mode_label="稳态"))
        actions.addWidget(self.import_pose_button)
        actions.addWidget(self.steady_raytrace_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addStretch(1)
        return tab

    def _build_zemax_transient_import_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        label = QLabel("瞬态导入：选择按时间导出的 Mechanical 结果文件夹，逐时间帧写入 Zemax、清空追迹并保存 .zzz。")
        label.setWordWrap(True)
        layout.addWidget(label)

        detector_layout = QHBoxLayout()
        detector_layout.addWidget(QLabel("保存到ZZZ的探测器"))
        self.zemax_time_series_detector_combo = QComboBox()
        self.zemax_time_series_detector_combo.setMinimumContentsLength(36)
        self.zemax_time_series_detector_combo.currentIndexChanged.connect(self.zemax_detector_selection_changed)
        self._reset_zemax_detector_combo(self.zemax_time_series_detector_combo)
        self.load_zemax_time_series_detectors_button = QPushButton("读取探测器")
        self.load_zemax_time_series_detectors_button.setIcon(
            self.style().standardIcon(QStyle.SP_FileDialogDetailedView)
        )
        self.load_zemax_time_series_detectors_button.clicked.connect(self.load_zemax_detectors)
        detector_layout.addWidget(self.zemax_time_series_detector_combo, stretch=1)
        detector_layout.addWidget(self.load_zemax_time_series_detectors_button)
        layout.addLayout(detector_layout)

        actions = QHBoxLayout()
        self.transient_time_series_button = QPushButton("瞬态时间序列追迹并保存ZZZ")
        self.transient_time_series_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.transient_time_series_button.clicked.connect(self.run_zemax_time_series_trace)
        self.transient_raytrace_button = QPushButton("瞬态清空并追迹")
        self.transient_raytrace_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.transient_raytrace_button.clicked.connect(lambda: self.run_zemax_ray_trace(mode_label="瞬态"))
        actions.addWidget(self.transient_time_series_button)
        actions.addWidget(self.transient_raytrace_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addStretch(1)
        return tab

    def _build_zemax_random_import_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        label = QLabel("随机振动导入：把当前计算得到的镜片位移/转角绝对值作为标准差，按用户设定数量生成随机样本并逐样本追迹。")
        label.setWordWrap(True)
        layout.addWidget(label)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("生成数量"))
        self.random_vibration_count_spin = QSpinBox()
        self.random_vibration_count_spin.setRange(1, 10000)
        self.random_vibration_count_spin.setValue(20)
        controls.addWidget(self.random_vibration_count_spin)
        self.random_vibration_trace_button = QPushButton("随机振动追迹并保存ZZZ")
        self.random_vibration_trace_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.random_vibration_trace_button.clicked.connect(self.run_zemax_random_vibration_trace)
        self.random_raytrace_button = QPushButton("随机振动清空并追迹")
        self.random_raytrace_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.random_raytrace_button.clicked.connect(lambda: self.run_zemax_ray_trace(mode_label="随机振动"))
        controls.addWidget(self.random_vibration_trace_button)
        controls.addWidget(self.random_raytrace_button)
        controls.addStretch(1)
        layout.addLayout(controls)
        layout.addStretch(1)
        return tab

    def _build_zemax_raytrace_launcher_group(self) -> QGroupBox:
        launcher_group = QGroupBox("非序列光线追迹 / 探测器")
        layout = QHBoxLayout(launcher_group)
        label = QLabel("探测器读取、结果摘要、ZZZ 播放和伪彩色图在弹出窗口中操作。")
        label.setWordWrap(True)
        self.open_zemax_raytrace_tab_button = QPushButton("打开探测器/ZZZ窗口")
        self.open_zemax_raytrace_tab_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogDetailedView))
        self.open_zemax_raytrace_tab_button.clicked.connect(self.open_zemax_raytrace_dialog)
        layout.addWidget(label, stretch=1)
        layout.addWidget(self.open_zemax_raytrace_tab_button)
        return launcher_group

    def ensure_zemax_raytrace_dialog(self) -> QDialog:
        if self.zemax_raytrace_dialog is not None:
            return self.zemax_raytrace_dialog

        dialog = QDialog(self)
        dialog.setWindowTitle("Zemax 追迹 / 探测器")
        dialog.resize(1180, 720)
        layout = QVBoxLayout(dialog)

        layout.addWidget(self._build_zemax_raytrace_detector_panel(), stretch=1)

        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(dialog.close)
        button_layout.addWidget(close_button)
        layout.addLayout(button_layout)

        self.zemax_raytrace_dialog = dialog
        return dialog

    def open_zemax_raytrace_dialog(self) -> None:
        dialog = self.ensure_zemax_raytrace_dialog()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        self.set_operation_buttons_enabled(self.active_thread is None)

    def _build_zemax_raytrace_detector_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)

        detector_layout = QHBoxLayout()
        detector_layout.addWidget(QLabel("探测器"))
        self.zemax_detector_combo = QComboBox()
        self.zemax_detector_combo.setMinimumContentsLength(36)
        self.zemax_detector_combo.currentIndexChanged.connect(self.zemax_detector_selection_changed)
        self._reset_zemax_detector_combo(self.zemax_detector_combo)
        self.load_zemax_detectors_button = QPushButton("读取探测器")
        self.load_zemax_detectors_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogDetailedView))
        self.load_zemax_detectors_button.clicked.connect(self.load_zemax_detectors)
        self.read_selected_detector_button = QPushButton("快速查看选中探测器")
        self.read_selected_detector_button.setIcon(self.style().standardIcon(QStyle.SP_DialogApplyButton))
        self.read_selected_detector_button.clicked.connect(self.read_selected_zemax_detector_result)
        self.export_selected_detector_button = QPushButton("完整导出CSV")
        self.export_selected_detector_button.setIcon(self.style().standardIcon(QStyle.SP_DialogSaveButton))
        self.export_selected_detector_button.clicked.connect(self.export_selected_zemax_detector_result)
        detector_layout.addWidget(self.zemax_detector_combo, stretch=1)
        detector_layout.addWidget(self.load_zemax_detectors_button)
        detector_layout.addWidget(self.read_selected_detector_button)
        detector_layout.addWidget(self.export_selected_detector_button)
        layout.addLayout(detector_layout)

        content_splitter = QSplitter(Qt.Horizontal)

        detector_panel = QWidget()
        detector_panel_layout = QVBoxLayout(detector_panel)
        detector_panel_layout.setContentsMargins(0, 0, 0, 0)
        detector_panel_layout.addWidget(QLabel("探测器列表 / 当前结果"))
        self.zemax_detector_result_log = QPlainTextEdit()
        self.zemax_detector_result_log.setReadOnly(True)
        self.zemax_detector_result_log.setMaximumBlockCount(1000)
        self.zemax_detector_result_log.setMinimumHeight(460)
        self.zemax_detector_result_log.setPlaceholderText("读取探测器后显示当前工程的 Detector 列表；读取结果后显示当前 Detector 摘要。")
        detector_panel_layout.addWidget(self.zemax_detector_result_log, stretch=1)
        content_splitter.addWidget(detector_panel)

        result_panel = QWidget()
        result_panel_layout = QVBoxLayout(result_panel)
        result_panel_layout.setContentsMargins(0, 0, 0, 0)
        result_panel_layout.addWidget(QLabel("光线追迹 / 结果摘要"))
        self.zemax_raytrace_result_log = QPlainTextEdit()
        self.zemax_raytrace_result_log.setReadOnly(True)
        self.zemax_raytrace_result_log.setMaximumBlockCount(1000)
        self.zemax_raytrace_result_log.setMinimumHeight(210)
        self.zemax_raytrace_result_log.setPlaceholderText("完成光线追迹或读取 Detector 后显示操作摘要。")
        result_panel_layout.addWidget(self.zemax_raytrace_result_log, stretch=1)

        preview_header = QHBoxLayout()
        preview_header.addWidget(QLabel("伪彩色预览"))
        preview_header.addStretch(1)
        self.open_zemax_zzz_button = QPushButton("打开ZZZ")
        self.open_zemax_zzz_button.clicked.connect(self.open_zemax_time_series_zzz)
        self.play_zemax_zzz_button = QPushButton("播放")
        self.play_zemax_zzz_button.clicked.connect(self.toggle_zemax_time_series_playback)
        self.zemax_zzz_frame_slider = QSlider(Qt.Horizontal)
        self.zemax_zzz_frame_slider.setRange(0, 0)
        self.zemax_zzz_frame_slider.setFixedWidth(170)
        self.zemax_zzz_frame_slider.valueChanged.connect(self.zemax_time_series_slider_changed)
        self.zemax_zzz_frame_label = QLabel("0/0")
        self.zemax_detector_zoom_out_button = QPushButton("缩小")
        self.zemax_detector_zoom_out_button.clicked.connect(self.zoom_out_zemax_detector_preview)
        self.zemax_detector_zoom_fit_button = QPushButton("适应窗口")
        self.zemax_detector_zoom_fit_button.clicked.connect(self.fit_zemax_detector_preview)
        self.zemax_detector_zoom_in_button = QPushButton("放大")
        self.zemax_detector_zoom_in_button.clicked.connect(self.zoom_in_zemax_detector_preview)
        self.zemax_detector_zoom_label = QLabel("无图像")
        preview_header.addWidget(self.open_zemax_zzz_button)
        preview_header.addWidget(self.play_zemax_zzz_button)
        preview_header.addWidget(self.zemax_zzz_frame_slider)
        preview_header.addWidget(self.zemax_zzz_frame_label)
        preview_header.addWidget(self.zemax_detector_zoom_out_button)
        preview_header.addWidget(self.zemax_detector_zoom_fit_button)
        preview_header.addWidget(self.zemax_detector_zoom_in_button)
        preview_header.addWidget(self.zemax_detector_zoom_label)
        result_panel_layout.addLayout(preview_header)

        self.zemax_detector_preview_label = QLabel("暂无伪彩色图")
        self.zemax_detector_preview_label.setAlignment(Qt.AlignCenter)
        self.zemax_detector_preview_label.setMinimumSize(360, 300)
        self.zemax_detector_preview_label.setStyleSheet("border: 1px solid #d0d5dd; background: #ffffff;")
        self.zemax_detector_preview_scroll = QScrollArea()
        self.zemax_detector_preview_scroll.setWidget(self.zemax_detector_preview_label)
        self.zemax_detector_preview_scroll.setWidgetResizable(False)
        self.zemax_detector_preview_scroll.setAlignment(Qt.AlignCenter)
        self.zemax_detector_preview_scroll.setMinimumSize(380, 320)
        result_panel_layout.addWidget(self.zemax_detector_preview_scroll, stretch=2)
        self.set_zemax_detector_zoom_buttons_enabled(False)
        self.update_zemax_time_series_controls()
        content_splitter.addWidget(result_panel)
        content_splitter.setSizes([560, 560])
        layout.addWidget(content_splitter, stretch=1)
        return panel

    def _build_zemax_status_group(self) -> QGroupBox:
        status_group = QGroupBox("Zemax 状态")
        layout = QGridLayout(status_group)
        layout.setColumnStretch(1, 1)

        self.zemax_exe_status_label = QLabel("")
        self.zemax_exe_status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.zemax_selected_project_label = QLabel("")
        self.zemax_selected_project_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.zemax_project_status_label = QLabel("")
        self.zemax_project_status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.zemax_process_status_label = QLabel("")
        self.zemax_process_status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        layout.addWidget(QLabel("OpticStudio"), 0, 0)
        layout.addWidget(self.zemax_exe_status_label, 0, 1)
        layout.addWidget(QLabel("选择工程"), 1, 0)
        layout.addWidget(self.zemax_selected_project_label, 1, 1)
        layout.addWidget(QLabel("当前工程"), 2, 0)
        layout.addWidget(self.zemax_project_status_label, 2, 1)
        layout.addWidget(QLabel("运行进程"), 3, 0)
        layout.addWidget(self.zemax_process_status_label, 3, 1)
        return status_group

    def _build_status_group(self) -> QGroupBox:
        status_group = QGroupBox("环境状态")
        status_group.setMinimumWidth(620)
        status_group.setMaximumWidth(820)
        status_group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.status_layout = QGridLayout(status_group)
        self.status_layout.setContentsMargins(8, 7, 8, 7)
        self.status_layout.setHorizontalSpacing(6)
        self.status_layout.setVerticalSpacing(1)
        self.status_layout.setColumnStretch(0, 0)
        self.status_layout.setColumnStretch(1, 1)
        self.status_layout.setColumnStretch(2, 0)
        return status_group

    def _build_operation_group(self) -> QGroupBox:
        operation_group = QGroupBox("当前操作")
        operation_layout = QGridLayout(operation_group)
        operation_layout.setContentsMargins(8, 8, 8, 8)
        operation_layout.setHorizontalSpacing(6)
        operation_layout.setVerticalSpacing(4)
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
        operation_group.setMinimumWidth(300)
        return operation_group

    def _build_modules_group(self) -> QGroupBox:
        modules_group = QGroupBox("当前工程分析模块")
        modules_group.setMinimumWidth(0)
        modules_group.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        modules_layout = QVBoxLayout(modules_group)

        self.modules_table = QTableWidget(0, 7)
        self.modules_table.setMinimumWidth(0)
        self.modules_table.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.modules_table.setHorizontalHeaderLabels(
            ["序号", "系统名", "模块", "系统类型", "物理场", "分析类型", "求解器"]
        )
        self.modules_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.modules_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.modules_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.modules_table.setAlternatingRowColors(True)
        self.modules_table.verticalHeader().setVisible(False)
        self.modules_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        module_header = self.modules_table.horizontalHeader()
        for column, width in enumerate([54, 96, 170, 150, 96, 120, 150]):
            module_header.setSectionResizeMode(column, QHeaderView.Interactive)
            self.modules_table.setColumnWidth(column, width)
        module_header.setSectionResizeMode(3, QHeaderView.Stretch)
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
        log_layout.setContentsMargins(8, 8, 8, 8)
        log_layout.setSpacing(6)
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

        browse_zemax_project_action = QAction("选择 Zemax 工程", self)
        browse_zemax_project_action.triggered.connect(self.browse_zemax_project)

        load_modules_action = QAction("读取分析模块", self)
        load_modules_action.triggered.connect(self.load_analysis_modules)

        close_mechanical_action = QAction("关闭后台 Mechanical", self)
        close_mechanical_action.triggered.connect(self.close_mechanical)

        close_zemax_action = QAction("关闭 Zemax", self)
        close_zemax_action.triggered.connect(self.close_zemax)

        exit_action = QAction("退出", self)
        exit_action.triggered.connect(self.close)

        file_menu = self.menuBar().addMenu("文件")
        file_menu.addAction(browse_project_action)
        file_menu.addAction(load_modules_action)
        file_menu.addSeparator()
        file_menu.addAction(browse_zemax_project_action)
        file_menu.addSeparator()
        file_menu.addAction(refresh_action)
        file_menu.addSeparator()
        file_menu.addAction(close_mechanical_action)
        file_menu.addAction(close_zemax_action)
        file_menu.addSeparator()
        file_menu.addAction(exit_action)

    def _apply_style(self) -> None:
        style = """
            QWidget {
                color: #172033;
                selection-background-color: #0a84ff;
                selection-color: #ffffff;
            }
            QMainWindow,
            QDialog,
            QWidget#appShell {
                background: #eef3f8;
            }
            QLabel#titleLabel {
                color: #142033;
                padding: 4px 0 4px 2px;
            }
            QLabel#appDot {
                background: #0a84ff;
                border-radius: 8px;
                min-width: 16px;
                max-width: 16px;
                min-height: 16px;
                max-height: 16px;
                margin-left: 2px;
            }
            QLabel#themePill {
                color: #0a65d8;
                background: rgba(230, 242, 255, 210);
                border: 1px solid rgba(183, 217, 255, 220);
                border-radius: 14px;
                padding: 5px 12px;
            }
            QWidget#mainGlass {
                background: rgba(255, 255, 255, 110);
                border: 1px solid rgba(207, 217, 230, 210);
                border-radius: 18px;
            }
            QWidget#sidebar {
                background: rgba(247, 250, 253, 190);
                border: 1px solid rgba(213, 224, 237, 210);
                border-radius: 16px;
                min-width: 150px;
                max-width: 168px;
            }
            QLabel#sidebarTitle {
                color: #142033;
                padding: 4px 8px 8px 8px;
            }
            QLabel#sidebarHint {
                color: #65758a;
                background: rgba(255, 255, 255, 130);
                border: 1px solid rgba(213, 224, 237, 180);
                border-radius: 12px;
                padding: 8px;
            }
            QStackedWidget#mainStack {
                background: transparent;
                border: 0;
            }
            QPushButton#navButton {
                background: transparent;
                border: 1px solid transparent;
                border-radius: 12px;
                color: #53637a;
                min-height: 30px;
                padding: 6px 10px;
                text-align: left;
            }
            QPushButton#navButton:hover {
                background: rgba(255, 255, 255, 150);
                border-color: rgba(213, 224, 237, 180);
                color: #0a65d8;
            }
            QPushButton#navButton:checked {
                background: rgba(255, 255, 255, 230);
                border-color: #b7d9ff;
                color: #0a65d8;
            }
            QWidget#projectToolbar {
                background: rgba(255, 255, 255, 210);
                border: 1px solid rgba(207, 217, 230, 230);
                border-radius: 18px;
            }
            QLabel#toolbarTitle {
                color: #142033;
            }
            QLabel#toolbarCaption {
                color: #53637a;
            }
            QMenuBar {
                background: rgba(255, 255, 255, 165);
                border-bottom: 1px solid rgba(205, 216, 229, 180);
                padding: 3px 8px;
            }
            QMenuBar::item {
                background: transparent;
                border-radius: 8px;
                padding: 5px 10px;
            }
            QMenuBar::item:selected {
                background: rgba(10, 132, 255, 30);
                color: #0a65d8;
            }
            QMenu {
                background: rgba(250, 252, 255, 245);
                border: 1px solid #cfd9e6;
                border-radius: 12px;
                padding: 6px;
            }
            QMenu::item {
                border-radius: 8px;
                padding: 6px 24px;
            }
            QMenu::item:selected {
                background: #e6f2ff;
                color: #0a65d8;
            }
            QTabWidget::pane {
                background: rgba(255, 255, 255, 150);
                border: 1px solid rgba(207, 217, 230, 210);
                border-radius: 18px;
                top: -1px;
            }
            QTabBar::tab {
                background: rgba(255, 255, 255, 120);
                border: 1px solid rgba(207, 217, 230, 190);
                border-radius: 14px;
                color: #53637a;
                min-height: 28px;
                min-width: 104px;
                margin: 0 4px 8px 0;
                padding: 5px 16px;
            }
            QTabBar::tab:selected {
                background: #ffffff;
                border-color: #9dccff;
                color: #0a65d8;
            }
            QTabBar::tab:hover {
                background: #f7fbff;
                color: #0a65d8;
            }
            QGroupBox {
                background: rgba(255, 255, 255, 185);
                border: 1px solid rgba(207, 217, 230, 230);
                border-radius: 18px;
                margin-top: 18px;
                padding: 16px 12px 12px 12px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                color: #142033;
                left: 14px;
                padding: 0 8px;
            }
            QPushButton {
                background: rgba(255, 255, 255, 210);
                border: 1px solid #c9d5e3;
                border-radius: 12px;
                color: #172033;
                min-height: 32px;
                padding: 5px 14px;
            }
            QPushButton:hover {
                background: #f7fbff;
                border-color: #9dccff;
                color: #0a65d8;
            }
            QPushButton:pressed {
                background: #d9ecff;
                border-color: #0a84ff;
            }
            QPushButton:disabled {
                background: rgba(236, 242, 248, 180);
                color: #9aa8b8;
                border-color: #d8e2ef;
            }
            QPushButton#primaryAction {
                background: #0a84ff;
                border-color: #0a78ea;
                color: #ffffff;
            }
            QPushButton#primaryAction:hover {
                background: #1b8fff;
                border-color: #006fdd;
                color: #ffffff;
            }
            QPushButton#primaryAction:pressed {
                background: #006fdd;
                border-color: #0062c7;
            }
            QPushButton#dangerButton {
                background: rgba(255, 244, 242, 230);
                border-color: #f2b8b5;
                color: #c7342e;
            }
            QPushButton#dangerButton:hover {
                background: #ffecea;
                border-color: #ee928d;
                color: #b62520;
            }
            QLineEdit,
            QComboBox,
            QSpinBox {
                background: rgba(255, 255, 255, 230);
                border: 1px solid #cbd8e6;
                border-radius: 11px;
                color: #172033;
                min-height: 30px;
                padding: 4px 10px;
            }
            QLineEdit:focus,
            QComboBox:focus,
            QSpinBox:focus {
                border: 1px solid #0a84ff;
                background: #ffffff;
            }
            QComboBox::drop-down {
                border: 0;
                width: 24px;
            }
            QCheckBox {
                spacing: 8px;
                color: #253246;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border-radius: 5px;
                border: 1px solid #b9c7d8;
                background: #ffffff;
            }
            QCheckBox::indicator:checked {
                background: #0a84ff;
                border-color: #0a84ff;
            }
            QTableWidget {
                background: rgba(255, 255, 255, 220);
                alternate-background-color: #f7fafe;
                border: 1px solid #d4deea;
                border-radius: 12px;
                color: #172033;
                gridline-color: #e1e8f0;
                selection-background-color: #d9ecff;
                selection-color: #142033;
            }
            QTableWidget::item {
                padding: 5px 7px;
            }
            QTableWidget::item:selected {
                background: #d9ecff;
                color: #142033;
            }
            QHeaderView::section {
                background: #f1f6fb;
                border: 0;
                border-right: 1px solid #dbe4ee;
                border-bottom: 1px solid #dbe4ee;
                color: #53637a;
                padding: 6px 8px;
            }
            QPlainTextEdit {
                background: #111a2a;
                border: 1px solid #26364a;
                border-radius: 14px;
                color: #e8eef7;
                padding: 9px;
            }
            QScrollArea {
                background: transparent;
                border: 1px solid #d4deea;
                border-radius: 12px;
            }
            QSplitter::handle {
                background: transparent;
            }
            QSplitter::handle:horizontal {
                width: 10px;
            }
            QSplitter::handle:vertical {
                height: 10px;
            }
            QScrollBar:vertical,
            QScrollBar:horizontal {
                background: transparent;
                border: 0;
                margin: 2px;
            }
            QScrollBar:vertical {
                width: 10px;
            }
            QScrollBar:horizontal {
                height: 10px;
            }
            QScrollBar::handle {
                background: rgba(117, 133, 153, 120);
                border-radius: 5px;
            }
            QScrollBar::handle:hover {
                background: rgba(10, 132, 255, 150);
            }
            QScrollBar::add-line,
            QScrollBar::sub-line {
                width: 0;
                height: 0;
            }
            QLabel[status="ok"] {
                color: #168a4d;
            }
            QLabel[status="missing"] {
                color: #c7342e;
            }
        """
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(style)
        else:
            self.setStyleSheet(style)

    def current_project_path(self) -> Path | None:
        text = self.project_path_edit.text().strip()
        if text:
            return Path(text)
        return self.selected_project

    def project_path_edited(self) -> None:
        self.selected_project = self.current_project_path()
        self.refresh_status()

    def current_mechanical_export_folder(self) -> Path | None:
        candidates = self.mechanical_export_folder_candidates()
        if candidates:
            self.selected_mechanical_export_folder = candidates[0]
            return candidates[0]
        if self.selected_mechanical_export_folder is not None and self.selected_mechanical_export_folder.exists():
            return self.selected_mechanical_export_folder
        return None

    def mechanical_export_folder_edited(self) -> None:
        self.selected_mechanical_export_folder = self.current_mechanical_export_folder()

    def mechanical_export_folder_candidates(self) -> list[Path]:
        exports_root = WORKSPACE / "exports"
        if not exports_root.exists():
            return []
        candidates: list[tuple[float, Path]] = []
        for summary_path in exports_root.rglob("solution_results_summary.txt"):
            folder = summary_path.parent
            if not folder.is_dir():
                continue
            if "tabular_data" in [part.lower() for part in folder.parts]:
                continue
            relevant_files = [
                path
                for path in folder.glob("*.txt")
                if path.name.lower() != "solution_results_summary.txt"
                and not path.name.lower().endswith("_properties.txt")
            ]
            if not relevant_files:
                continue
            newest = max([summary_path, *relevant_files], key=lambda path: path.stat().st_mtime)
            candidates.append((newest.stat().st_mtime, folder))
        candidates.sort(key=lambda item: item[0], reverse=True)
        unique: list[Path] = []
        seen: set[str] = set()
        for _, folder in candidates:
            key = str(folder.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(folder)
        return unique

    def current_zemax_project_path(self) -> Path | None:
        text = self.zemax_project_path_edit.text().strip()
        if text:
            return Path(text)
        return self.selected_zemax_project

    def zemax_project_path_edited(self) -> None:
        self.selected_zemax_project = self.current_zemax_project_path()
        self.current_zemax_info = {}
        self.clear_zemax_detector_state()
        self.update_zemax_status()

    def browse_project(self) -> None:
        current = self.current_project_path()
        start_dir = current.parent if current is not None and current.parent.exists() else WORKSPACE
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 Mechanical database",
            str(start_dir),
            "Mechanical Database (*.mechdb *.mechdat);;All Files (*)",
        )
        if not file_path:
            return

        self.selected_project = Path(file_path)
        self.project_path_edit.setText(str(self.selected_project))
        self.refresh_status()
        self.modules = []
        self.modules_table.setRowCount(0)
        self.open_mechanical(auto_load_modules=True)

    def browse_zemax_project(self) -> None:
        current = self.current_zemax_project_path()
        default_zemax_dir = WORKSPACE / "S_optical_model_SstageV8"
        if current is not None and current.parent.exists():
            start_dir = current.parent
        elif default_zemax_dir.exists():
            start_dir = default_zemax_dir
        else:
            start_dir = WORKSPACE
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 Zemax 工程文件",
            str(start_dir),
            "Zemax Project (*.zmx *.zos *.zar *.zprj);;All Files (*)",
        )
        if not file_path:
            return

        self.selected_zemax_project = Path(file_path)
        self.current_zemax_info = {}
        self.clear_zemax_detector_state()
        self.zemax_project_path_edit.setText(str(self.selected_zemax_project))
        self.update_zemax_status()
        self.append_log(f"已选择 Zemax 工程: {self.selected_zemax_project}。不会打开 GUI；导入时通过后台 ZOS-API 打开。")

    def _read_zemax_pose_records_from_folder(self, folder: Path) -> tuple[dict[str, object] | None, list[dict[str, object]], list[str]]:
        calculation: dict[str, object] | None = None
        errors: list[str] = []
        records: list[dict[str, object]] = []
        try:
            calculation = calculate_pose_records_from_mechanical_exports(folder)
        except Exception as exc:
            errors.append(f"{folder}: {exc}")
        if calculation is not None:
            records = [
                record.as_dict()
                for record in calculation.get("latest_records", [])
                if hasattr(record, "as_dict")
            ]
        if not records:
            try:
                records = [record.as_dict() for record in read_lens_pose_records(folder)]
            except Exception as exc:
                errors.append(f"{folder}: {exc}")
                records = []
        return calculation, records, errors

    def apply_zemax_pose_records_from_folder(self, folder: Path, *, source_label: str) -> bool:
        calculation, records, errors = self._read_zemax_pose_records_from_folder(folder)
        if not records:
            details = "\n".join(errors[:6])
            QMessageBox.warning(
                self,
                "未读取到位姿",
                f"所选 Mechanical 导出文件夹没有找到可计算镜片位移/旋转的数据:\n{folder}\n\n"
                + details,
            )
            return False
        self.selected_mechanical_export_folder = folder

        self.zemax_pose_records = records
        self.populate_zemax_pose_table(self.zemax_pose_records)

        log_lines = [f"{source_label}: {folder}"]
        if calculation is not None:
            log_lines.extend(str(line) for line in calculation.get("logs") or [])
        saved_paths = calculation.get("saved_paths") if calculation is not None else {}
        if isinstance(saved_paths, dict) and saved_paths:
            log_lines.append("输出文件:")
            for label, path in saved_paths.items():
                log_lines.append(f"  {label}: {path}")
        self.set_zemax_pose_log(log_lines)
        self.append_log(f"已从 Mechanical 原始导出文件计算 {len(records)} 条镜片位移/旋转记录: {folder}")
        return True

    def load_zemax_pose_records(self) -> None:
        candidate_folders = self.mechanical_export_folder_candidates()
        if not candidate_folders and self.selected_mechanical_export_folder is not None and self.selected_mechanical_export_folder.exists():
            candidate_folders = [self.selected_mechanical_export_folder]
        if not candidate_folders:
            QMessageBox.warning(
                self,
                "未找到 Mechanical 导出",
                "没有在软件 exports 目录中找到可用于 Zemax 位姿计算的 Mechanical 结果导出文件夹。"
                "请先在求解结果窗口导出包含镜片 UX/UY/UZ 的 TXT 结果。",
            )
            return

        errors: list[str] = []
        for candidate in candidate_folders:
            calculation, records, candidate_errors = self._read_zemax_pose_records_from_folder(candidate)
            if records:
                self.selected_mechanical_export_folder = candidate
                self.zemax_pose_records = records
                self.populate_zemax_pose_table(self.zemax_pose_records)
                log_lines = [f"自动识别 Mechanical 导出文件夹: {candidate}"]
                if calculation is not None:
                    log_lines.extend(str(line) for line in calculation.get("logs") or [])
                    saved_paths = calculation.get("saved_paths") or {}
                else:
                    saved_paths = {}
                if isinstance(saved_paths, dict) and saved_paths:
                    log_lines.append("输出文件:")
                    for label, path in saved_paths.items():
                        log_lines.append(f"  {label}: {path}")
                self.set_zemax_pose_log(log_lines)
                self.append_log(f"已从 Mechanical 原始导出文件计算 {len(records)} 条镜片位移/旋转记录: {candidate}")
                return
            errors.extend(candidate_errors)

        details = "\n".join(errors[:6])
        QMessageBox.warning(
            self,
            "未读取到位姿",
            "已自动检查最近的 Mechanical 导出文件夹，但没有找到可计算镜片位移/旋转的数据。\n\n"
            + details,
        )

    def choose_zemax_mechanical_export_folder(self) -> Path | None:
        start_dir = WORKSPACE / "exports"
        if self.selected_mechanical_export_folder is not None and self.selected_mechanical_export_folder.exists():
            start_dir = self.selected_mechanical_export_folder
        else:
            candidates = self.mechanical_export_folder_candidates()
            if candidates:
                start_dir = candidates[0]
        folder = QFileDialog.getExistingDirectory(
            self,
            "选择 Mechanical 结果导出文件夹",
            str(start_dir if start_dir.exists() else WORKSPACE),
            QFileDialog.ShowDirsOnly,
        )
        if not folder:
            return None
        return Path(folder)

    def set_zemax_pose_log(self, lines: list[str]) -> None:
        if lines:
            self.append_log("Zemax 位姿计算:\n" + "\n".join(lines))

    def populate_zemax_pose_table(self, records: list[dict[str, object]]) -> None:
        self.zemax_pose_table.setRowCount(len(records))
        for row_index, record in enumerate(records):
            values = [
                str(record.get("name") or ""),
                f"{float(record.get('x') or 0):.9g}",
                f"{float(record.get('y') or 0):.9g}",
                f"{float(record.get('z') or 0):.9g}",
                f"{float(record.get('rx') or 0):.9g}",
                f"{float(record.get('ry') or 0):.9g}",
                f"{float(record.get('rz') or 0):.9g}",
                str(record.get("source") or ""),
            ]
            for column, value in enumerate(values):
                self.zemax_pose_table.setItem(row_index, column, QTableWidgetItem(value))
        self.zemax_pose_table.resizeRowsToContents()

    def clear_zemax_detector_state(self) -> None:
        self.zemax_detectors = []
        self.zemax_detector_results = {}
        self.zemax_detector_image_path = None
        for combo in self.zemax_detector_combos():
            self._reset_zemax_detector_combo(combo)
        if hasattr(self, "zemax_detector_result_log"):
            self.zemax_detector_result_log.clear()
        if hasattr(self, "zemax_raytrace_result_log"):
            self.zemax_raytrace_result_log.clear()
        self.set_zemax_detector_preview(None)

    def zemax_detector_combos(self) -> list[QComboBox]:
        combos: list[QComboBox] = []
        for name in ("zemax_time_series_detector_combo", "zemax_detector_combo"):
            combo = getattr(self, name, None)
            if isinstance(combo, QComboBox):
                combos.append(combo)
        return combos

    def _reset_zemax_detector_combo(self, combo: QComboBox) -> None:
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("请先读取探测器", None)
        combo.blockSignals(False)

    def zemax_detector_combo_label(self, detector: dict[str, object]) -> str:
        object_index = int(detector.get("object_index") or 0)
        comment = str(detector.get("comment") or "").strip()
        type_name = str(detector.get("type_name") or "Detector").strip()
        x_pixels = detector.get("x_pixels")
        y_pixels = detector.get("y_pixels")
        pixel_text = f"{x_pixels} x {y_pixels}" if x_pixels and y_pixels else "像素未知"
        return f"{object_index:03d} | {comment or '(无 Comment)'} | {type_name} | {pixel_text}"

    def selected_zemax_detector_number(self) -> int | None:
        for combo in self.zemax_detector_combos():
            value = combo.currentData()
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None

    def populate_zemax_detector_combo(self, detectors: list[dict[str, object]]) -> None:
        self.ensure_zemax_raytrace_dialog()
        previous_detector_number = self.selected_zemax_detector_number()
        valid_detectors = [
            detector
            for detector in detectors
            if int(detector.get("object_index") or 0) > 0
        ]
        for combo in self.zemax_detector_combos():
            combo.blockSignals(True)
            combo.clear()
            if valid_detectors:
                for detector in valid_detectors:
                    combo.addItem(
                        self.zemax_detector_combo_label(detector),
                        int(detector.get("object_index") or 0),
                    )
                if previous_detector_number is not None:
                    index = combo.findData(previous_detector_number)
                    combo.setCurrentIndex(index if index >= 0 else 0)
                else:
                    combo.setCurrentIndex(0)
            else:
                combo.addItem("当前工程没有探测器", None)
            combo.blockSignals(False)
        self.zemax_detector_selection_changed()

    @Slot(int)
    def zemax_detector_selection_changed(self, _index: int = -1) -> None:
        sender = self.sender()
        detector_number: int | None = None
        if isinstance(sender, QComboBox):
            value = sender.currentData()
            if value is not None:
                try:
                    detector_number = int(value)
                except (TypeError, ValueError):
                    detector_number = None
        if detector_number is None:
            detector_number = self.selected_zemax_detector_number()
        if detector_number is None:
            self.zemax_detector_image_path = None
            self.set_zemax_detector_preview(None)
            return
        for combo in self.zemax_detector_combos():
            if combo is sender:
                continue
            index = combo.findData(detector_number)
            if index >= 0 and combo.currentIndex() != index:
                combo.blockSignals(True)
                combo.setCurrentIndex(index)
                combo.blockSignals(False)
        result = self.zemax_detector_results.get(detector_number)
        if not result or result.get("status") != "ok":
            self.zemax_detector_image_path = None
            self.set_zemax_detector_preview(None)
            return
        image_path = Path(str(result.get("image_path") or ""))
        self.zemax_detector_image_path = image_path if image_path.exists() else None
        self.set_zemax_detector_preview(self.zemax_detector_image_path)

    def load_zemax_detectors(self) -> None:
        self.ensure_zemax_raytrace_dialog()
        if self.active_thread is not None:
            QMessageBox.information(self, "操作正在执行", "当前操作还没有结束。")
            return
        zemax_project = self.current_zemax_project_path()
        if zemax_project is None:
            QMessageBox.warning(self, "未选择 Zemax 工程", "请先选择 Zemax 工程文件。")
            return
        if not zemax_project.exists():
            QMessageBox.warning(self, "Zemax 工程不存在", f"所选 Zemax 工程文件不存在:\n{zemax_project}")
            return

        self.clear_zemax_detector_state()
        self.set_operation_buttons_enabled(False)
        self.start_operation_status("Zemax 探测器", "后台读取非序列探测器列表")
        self.append_log(f"正在后台读取 Zemax 探测器: {zemax_project}")

        thread = QThread(self)
        worker = ZemaxDetectorWorker("list_detectors", zemax_project)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.update_operation_progress)
        worker.finished.connect(self.zemax_detector_operation_finished)
        worker.failed.connect(self.zemax_detector_operation_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self.operation_thread_finished)
        self.active_thread = thread
        self.active_worker = worker
        thread.start()

    def run_zemax_ray_trace(self, *, mode_label: str = "非序列") -> None:
        if self.active_thread is not None:
            QMessageBox.information(self, "操作正在执行", "当前操作还没有结束。")
            return
        zemax_project = self.current_zemax_project_path()
        if zemax_project is None:
            QMessageBox.warning(self, "未选择 Zemax 工程", "请先选择 Zemax 工程文件。")
            return
        if not zemax_project.exists():
            QMessageBox.warning(self, "Zemax 工程不存在", f"所选 Zemax 工程文件不存在:\n{zemax_project}")
            return

        self.zemax_detector_results = {}
        self.zemax_detector_image_path = None
        self.set_zemax_detector_preview(None)
        self.set_operation_buttons_enabled(False)
        self.start_operation_status(
            f"Zemax {mode_label}光线追迹",
            f"{mode_label}: 清空全部探测器，并使用当前 Zemax 工程默认追迹设置运行",
        )
        self.append_log(
            f"开始后台清空所有 Zemax 探测器并执行一次{mode_label}光线追迹，"
            f"工程={zemax_project}。追迹参数使用当前 Zemax 工程默认设置。"
        )

        thread = QThread(self)
        worker = ZemaxDetectorWorker(
            "ray_trace",
            zemax_project,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.update_operation_progress)
        worker.finished.connect(self.zemax_detector_operation_finished)
        worker.failed.connect(self.zemax_detector_operation_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self.operation_thread_finished)
        self.active_thread = thread
        self.active_worker = worker
        thread.start()

    def run_zemax_time_series_trace(self) -> None:
        self.ensure_zemax_raytrace_dialog()
        if self.active_thread is not None:
            QMessageBox.information(self, "操作正在执行", "当前操作还没有结束。")
            return
        zemax_project = self.current_zemax_project_path()
        if zemax_project is None:
            QMessageBox.warning(self, "未选择 Zemax 工程", "请先选择 Zemax 工程文件。")
            return
        if not zemax_project.exists():
            QMessageBox.warning(self, "Zemax 工程不存在", f"所选 Zemax 工程文件不存在:\n{zemax_project}")
            return
        detector_number = self.selected_zemax_detector_number()
        if detector_number is None:
            QMessageBox.warning(self, "未选择探测器", "请先读取探测器列表，并选择用于形成光斑的 Detector。")
            return
        export_folder = self.choose_zemax_mechanical_export_folder()
        if export_folder is None:
            return
        if not export_folder.exists() or not export_folder.is_dir():
            QMessageBox.warning(self, "导出文件夹不存在", f"所选 Mechanical 导出文件夹不存在:\n{export_folder}")
            return

        default_dir = zemax_project.parent / "zemax_time_series_results"
        default_dir.mkdir(parents=True, exist_ok=True)
        default_path = default_dir / f"{zemax_project.stem}_detector_{detector_number:03d}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zzz"
        output_file_text, _ = QFileDialog.getSaveFileName(
            self,
            "保存 Zemax 时间序列光斑数据",
            str(default_path),
            "Zemax Time Series Data (*.zzz);;All Files (*)",
        )
        if not output_file_text:
            return
        output_file = Path(output_file_text)
        if output_file.suffix.lower() != ".zzz":
            output_file = output_file.with_suffix(".zzz")

        self.zemax_time_series_timer.stop()
        self.set_operation_buttons_enabled(False)
        self.start_operation_status("Zemax 时间序列追迹", "逐帧写入瞬态形变、追迹并保存 .zzz")
        self.append_log(
            "开始 Zemax 时间序列追迹: "
            f"工程={zemax_project}，Detector={detector_number}，"
            f"Mechanical导出={export_folder}，输出={output_file}"
        )

        thread = QThread(self)
        worker = ZemaxDetectorWorker(
            "time_series_trace",
            zemax_project,
            detector_number,
            export_folder=export_folder,
            output_file=output_file,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.update_operation_progress)
        worker.finished.connect(self.zemax_detector_operation_finished)
        worker.failed.connect(self.zemax_detector_operation_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self.operation_thread_finished)
        self.active_thread = thread
        self.active_worker = worker
        thread.start()

    def run_zemax_random_vibration_trace(self) -> None:
        self.ensure_zemax_raytrace_dialog()
        if self.active_thread is not None:
            QMessageBox.information(self, "操作正在执行", "当前操作还没有结束。")
            return
        zemax_project = self.current_zemax_project_path()
        if zemax_project is None:
            QMessageBox.warning(self, "未选择 Zemax 工程", "请先选择 Zemax 工程文件。")
            return
        if not zemax_project.exists():
            QMessageBox.warning(self, "Zemax 工程不存在", f"所选 Zemax 工程文件不存在:\n{zemax_project}")
            return
        detector_number = self.selected_zemax_detector_number()
        if detector_number is None:
            QMessageBox.warning(self, "未选择探测器", "请先读取探测器列表，并选择用于形成光斑的 Detector。")
            return
        export_folder = self.choose_zemax_mechanical_export_folder()
        if export_folder is None:
            return
        if not export_folder.exists() or not export_folder.is_dir():
            QMessageBox.warning(self, "导出文件夹不存在", f"所选 Mechanical 导出文件夹不存在:\n{export_folder}")
            return

        sample_count = 20
        if hasattr(self, "random_vibration_count_spin"):
            sample_count = int(self.random_vibration_count_spin.value())
        default_dir = zemax_project.parent / "zemax_random_vibration_results"
        default_dir.mkdir(parents=True, exist_ok=True)
        default_path = default_dir / f"{zemax_project.stem}_random_detector_{detector_number:03d}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zzz"
        output_file_text, _ = QFileDialog.getSaveFileName(
            self,
            "保存 Zemax 随机振动光斑数据",
            str(default_path),
            "Zemax Random Vibration Data (*.zzz);;All Files (*)",
        )
        if not output_file_text:
            return
        output_file = Path(output_file_text)
        if output_file.suffix.lower() != ".zzz":
            output_file = output_file.with_suffix(".zzz")

        self.zemax_time_series_timer.stop()
        self.set_operation_buttons_enabled(False)
        self.start_operation_status("Zemax 随机振动追迹", f"生成 {sample_count} 个随机样本、逐样本追迹并保存 .zzz")
        self.append_log(
            "开始 Zemax 随机振动追迹: "
            f"工程={zemax_project}，Detector={detector_number}，样本数={sample_count}，"
            f"标准差来源={export_folder}，输出={output_file}"
        )

        thread = QThread(self)
        worker = ZemaxDetectorWorker(
            "random_vibration_trace",
            zemax_project,
            detector_number,
            export_folder=export_folder,
            output_file=output_file,
            sample_count=sample_count,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.update_operation_progress)
        worker.finished.connect(self.zemax_detector_operation_finished)
        worker.failed.connect(self.zemax_detector_operation_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self.operation_thread_finished)
        self.active_thread = thread
        self.active_worker = worker
        thread.start()

    def read_selected_zemax_detector_result(self) -> None:
        self._start_zemax_detector_result_operation(export_csv=False)

    def export_selected_zemax_detector_result(self) -> None:
        self._start_zemax_detector_result_operation(export_csv=True)

    def _start_zemax_detector_result_operation(self, *, export_csv: bool) -> None:
        self.ensure_zemax_raytrace_dialog()
        if self.active_thread is not None:
            QMessageBox.information(self, "操作正在执行", "当前操作还没有结束。")
            return
        zemax_project = self.current_zemax_project_path()
        if zemax_project is None:
            QMessageBox.warning(self, "未选择 Zemax 工程", "请先选择 Zemax 工程文件。")
            return
        if not zemax_project.exists():
            QMessageBox.warning(self, "Zemax 工程不存在", f"所选 Zemax 工程文件不存在:\n{zemax_project}")
            return
        detector_number = self.selected_zemax_detector_number()
        if detector_number is None:
            QMessageBox.warning(self, "未选择探测器", "请先读取探测器列表，并在下拉框选择一个 Detector。")
            return

        if not export_csv:
            cached = self.zemax_detector_results.get(detector_number)
            cached_image_text = str(cached.get("image_path") or "") if isinstance(cached, dict) else ""
            cached_image = Path(cached_image_text) if cached_image_text else None
            if isinstance(cached, dict) and cached.get("status") == "ok" and cached_image is not None and cached_image.exists():
                self.zemax_detector_image_path = cached_image
                self.set_zemax_detector_preview(cached_image)
                lines = [
                    f"工程: {self.current_zemax_info.get('system_file') or zemax_project}",
                    f"读取方式: 使用缓存的 Detector {detector_number} 快速预览",
                    "",
                ]
                lines.extend(self.zemax_detector_result_lines(cached))
                self.zemax_raytrace_result_log.setPlainText("\n".join(lines))
                self.update_operation_progress({"stage": "Zemax 探测器结果", "status": f"Detector {detector_number} 使用缓存预览"})
                self.append_log(f"使用缓存打开 Zemax Detector {detector_number} 伪彩色预览: {cached_image}")
                return

        if not export_csv:
            self.zemax_detector_image_path = None
            self.set_zemax_detector_preview(None)
        self.set_operation_buttons_enabled(False)
        self.start_operation_status(
            "Zemax 探测器结果",
            f"完整导出 Detector {detector_number}" if export_csv else f"快速读取 Detector {detector_number} 预览",
        )
        self.append_log(
            (
                "开始完整导出 Zemax Detector 结果: "
                if export_csv
                else "开始快速读取 Zemax Detector 预览: "
            )
            + f"工程={zemax_project}，Detector={detector_number}"
        )

        thread = QThread(self)
        worker = ZemaxDetectorWorker(
            "export_detector_result" if export_csv else "read_detector_result",
            zemax_project,
            detector_number,
            write_csv=export_csv,
            preview_max_dimension=None if export_csv else DETECTOR_PREVIEW_MAX_DIMENSION,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.update_operation_progress)
        worker.finished.connect(self.zemax_detector_operation_finished)
        worker.failed.connect(self.zemax_detector_operation_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self.operation_thread_finished)
        self.active_thread = thread
        self.active_worker = worker
        thread.start()

    @Slot(object)
    def zemax_detector_operation_finished(self, result: object) -> None:
        self.ensure_zemax_raytrace_dialog()
        if not isinstance(result, dict):
            result = {}
        operation = str(result.get("operation") or "")
        system_file = str(result.get("system_file") or "")
        self.current_zemax_info = {
            "system_file": system_file,
            "system_name": str(result.get("system_name") or ""),
            "object_count": result.get("object_count"),
            "launch_method": "standalone_background",
        }

        if operation == "list_detectors":
            detectors = list(result.get("detectors") or [])
            self.zemax_detectors = detectors
            self.populate_zemax_detector_combo(detectors)
            lines = [
                f"工程: {system_file or '未命名工程'}",
                f"非序列对象数: {result.get('object_count')}",
                f"探测器数量: {len(detectors)}",
            ]
            for detector in detectors[:80]:
                lines.append(self.zemax_detector_summary_line(detector))
            if len(detectors) > 80:
                lines.append(f"... 其余 {len(detectors) - 80} 个探测器未在摘要中展开")
            self.zemax_detector_result_log.setPlainText("\n".join(lines))
            self.zemax_raytrace_result_log.clear()
            self.update_operation_progress({"stage": "Zemax 探测器", "status": f"读取到 {len(detectors)} 个探测器"})
            self.append_log(f"已读取 Zemax 探测器 {len(detectors)} 个: {system_file or self.current_zemax_project_path()}")
        elif operation == "ray_trace":
            cpu_cores = result.get("cpu_cores") if isinstance(result.get("cpu_cores"), dict) else {}
            self.zemax_detector_results = {}
            self.zemax_detector_image_path = None
            self.set_zemax_detector_preview(None)
            detectors = [item for item in result.get("detectors") or [] if isinstance(item, dict)]
            self.zemax_detectors = [dict(item) for item in detectors]
            self.populate_zemax_detector_combo(self.zemax_detectors)
            detector_lines = [
                f"工程: {system_file or '未命名工程'}",
                f"追迹完成，可选择并读取 {len(self.zemax_detectors)} 个探测器",
            ]
            for detector in self.zemax_detectors[:120]:
                detector_lines.append(self.zemax_detector_summary_line(detector))
            if len(self.zemax_detectors) > 120:
                detector_lines.append(f"... 其余 {len(self.zemax_detectors) - 120} 个探测器未在摘要中展开")
            self.zemax_detector_result_log.setPlainText("\n".join(detector_lines))

            lines = [
                f"工程: {system_file or '未命名工程'}",
                "追迹方式: 清空全部探测器后追迹一次，结果等待用户选择 Detector 后读取",
                f"探测器数量: {result.get('detector_count')}",
                f"CPU逻辑核心检测: {cpu_cores.get('detected_cpu_cores')}",
                f"CPU核心请求/实际: {cpu_cores.get('requested_cpu_cores')} / {cpu_cores.get('configured_cpu_cores')}",
                f"CPU核心设置状态: {'已启用' if cpu_cores.get('supported') else '未启用'}，{cpu_cores.get('message') or ''}",
                "下一步: 在下拉框选择 Detector，然后点击“快速查看选中探测器”；需要全量矩阵时再点“完整导出CSV”。",
            ]
            self.zemax_raytrace_result_log.setPlainText("\n".join(lines))
            self.update_operation_progress(
                {
                    "stage": "Zemax 光线追迹",
                    "status": f"追迹完成，探测器 {result.get('detector_count')} 个，等待选择读取",
                }
            )
            self.append_log(
                "Zemax 光线追迹完成: "
                f"探测器 {result.get('detector_count')} 个，"
                f"CPU核心={cpu_cores.get('configured_cpu_cores') or cpu_cores.get('requested_cpu_cores')}，"
                "请选择 Detector 后读取结果"
            )
        elif operation in {"time_series_trace", "random_vibration_trace"}:
            cpu_cores = result.get("cpu_cores") if isinstance(result.get("cpu_cores"), dict) else {}
            output_file = Path(str(result.get("output_file") or ""))
            frame_count = int(result.get("frame_count") or 0)
            detector = result.get("detector") if isinstance(result.get("detector"), dict) else {}
            is_random = operation == "random_vibration_trace"
            lines = [
                f"工程: {system_file or result.get('project_file') or '未命名工程'}",
                f"Detector: {self.zemax_detector_summary_line(detector) if detector else '未知'}",
                f"{'随机样本数' if is_random else '时间帧数'}: {frame_count}",
                f"输出 ZZZ: {output_file}",
                f"CPU逻辑核心检测: {cpu_cores.get('detected_cpu_cores')}",
                f"CPU核心请求/实际: {cpu_cores.get('requested_cpu_cores')} / {cpu_cores.get('configured_cpu_cores')}",
                f"耗时: {self._format_float(result.get('elapsed_seconds'))} s",
                "保存格式: .zzz ZIP 容器，包含 manifest.json、每帧 detector.csv 和 MATLAB 读取脚本。",
            ]
            if is_random:
                lines.insert(3, f"随机振动样本数: {result.get('sample_count')}")
            self.zemax_raytrace_result_log.setPlainText("\n".join(lines))
            if output_file.exists():
                try:
                    self.load_zemax_time_series_zzz_file(output_file)
                except Exception as exc:
                    self.append_log(f"ZZZ 已生成，但自动加载播放失败: {exc}")
            self.update_operation_progress(
                {
                    "stage": "Zemax 随机振动追迹完成" if is_random else "Zemax 时间序列追迹完成",
                    "status": f"已保存 {frame_count} 帧到 {output_file}",
                }
            )
            self.append_log(
                f"{'Zemax 随机振动追迹完成' if is_random else 'Zemax 时间序列追迹完成'}: "
                f"帧数={frame_count}，ZZZ={output_file}"
            )
        elif operation in {"read_detector_result", "export_detector_result"}:
            detector_result = result.get("result") if isinstance(result.get("result"), dict) else {}
            detector = detector_result.get("detector") if isinstance(detector_result.get("detector"), dict) else {}
            detector_number = int(detector.get("object_index") or 0)
            if detector_number > 0:
                self.zemax_detector_results[detector_number] = detector_result
            if not self.zemax_detectors:
                detectors = [item for item in result.get("detectors") or [] if isinstance(item, dict)]
                self.zemax_detectors = [dict(item) for item in detectors]
                self.populate_zemax_detector_combo(self.zemax_detectors)

            lines = [
                f"工程: {system_file or '未命名工程'}",
                (
                    f"读取方式: 完整导出当前选中的 Detector {detector_number}"
                    if operation == "export_detector_result"
                    else f"读取方式: 快速预览当前选中的 Detector {detector_number}"
                ),
                f"输出文件夹: {result.get('output_dir')}",
                f"伪彩色比例: {'对数' if result.get('log_scale') else '线性'}",
                "",
            ]
            lines.extend(self.zemax_detector_result_lines(detector_result))
            self.zemax_raytrace_result_log.setPlainText("\n".join(lines))
            self.zemax_detector_selection_changed()
            self.update_operation_progress({"stage": "Zemax 探测器结果", "status": f"Detector {detector_number} 已读取"})
            csv_text = detector_result.get("csv_path") or "预览模式未导出"
            self.append_log(
                f"Zemax Detector {detector_number} 结果读取完成: "
                f"CSV={csv_text}，PNG={detector_result.get('image_path')}"
            )
        else:
            self.update_operation_progress({"stage": "Zemax 操作完成", "status": operation or "完成"})
        self.update_zemax_status()

    @Slot(str)
    def zemax_detector_operation_failed(self, message: str) -> None:
        self.update_operation_progress({"stage": "Zemax 探测器/追迹失败", "status": message})
        self.append_log(f"Zemax 探测器/追迹失败: {message}")
        QMessageBox.critical(self, "Zemax 探测器/追迹失败", message)
        self.update_zemax_status()

    def zemax_detector_summary_line(self, detector: dict[str, object]) -> str:
        object_index = detector.get("object_index") or ""
        comment = str(detector.get("comment") or "").strip() or "(无 Comment)"
        type_name = str(detector.get("type_name") or "Detector").strip()
        x_pixels = detector.get("x_pixels")
        y_pixels = detector.get("y_pixels")
        x_half_width = detector.get("x_half_width")
        y_half_width = detector.get("y_half_width")
        return (
            f"{object_index} | {comment} | {type_name} | "
            f"{x_pixels or '?'} x {y_pixels or '?'} px | "
            f"半宽 X/Y={self._format_float(x_half_width)} / {self._format_float(y_half_width)}"
        )

    def zemax_detector_result_lines(self, result: dict[str, object]) -> list[str]:
        detector = result.get("detector") if isinstance(result.get("detector"), dict) else {}
        lines = [f"- {self.zemax_detector_summary_line(detector)}"]
        if result.get("status") != "ok":
            lines.append(f"  状态: 失败，{result.get('message') or '未知错误'}")
            return lines

        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        sampled = bool(summary.get("sampled"))
        csv_path = str(result.get("csv_path") or "").strip()
        timings = result.get("timings") if isinstance(result.get("timings"), dict) else {}
        lines.extend(
            [
                f"  原始像素: {summary.get('x_pixels')} x {summary.get('y_pixels')}",
                f"  预览像素: {summary.get('preview_x_pixels')} x {summary.get('preview_y_pixels')}"
                + ("（已抽样）" if sampled else ""),
                f"  读取方式: {result.get('read_mode') or summary.get('read_method') or 'unknown'}",
                f"  总能量/通量: {self._format_float(summary.get('total_flux'))}",
                f"  总命中数: {self._format_float(summary.get('total_hits'))}",
                f"  {'预览矩阵和' if sampled else '矩阵和'}: {self._format_float(summary.get('grid_sum'))}",
                f"  最小/最大: {self._format_float(summary.get('min_value'))} / {self._format_float(summary.get('max_value'))}",
                f"  非零像素: {summary.get('nonzero_pixels')}",
                f"  质心像素 X/Y: {self._format_float(summary.get('centroid_x_pixel'))} / {self._format_float(summary.get('centroid_y_pixel'))}",
                f"  用时: 读取 {self._format_float(timings.get('read_seconds'))} s，写图/CSV {self._format_float(timings.get('write_seconds'))} s",
                f"  CSV: {csv_path or '预览模式未导出；需要完整矩阵时点击“完整导出CSV”'}",
                f"  伪彩色图: {result.get('image_path')}",
            ]
        )
        return lines

    def open_zemax_time_series_zzz(self) -> None:
        start_dir = WORKSPACE
        if self.zemax_time_series_path is not None and self.zemax_time_series_path.parent.exists():
            start_dir = self.zemax_time_series_path.parent
        else:
            zemax_project = self.current_zemax_project_path()
            if zemax_project is not None and zemax_project.parent.exists():
                candidate = zemax_project.parent / "zemax_time_series_results"
                start_dir = candidate if candidate.exists() else zemax_project.parent
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "打开 Zemax 时间序列 ZZZ",
            str(start_dir),
            "Zemax Time Series Data (*.zzz);;All Files (*)",
        )
        if not file_path:
            return
        try:
            self.load_zemax_time_series_zzz_file(Path(file_path))
        except Exception as exc:
            QMessageBox.critical(self, "打开 ZZZ 失败", str(exc))

    def load_zemax_time_series_zzz_file(self, path: Path) -> None:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(path)
        frames: list[dict[str, object]] = []
        with zipfile.ZipFile(path, "r") as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            for frame_meta in manifest.get("frames") or []:
                if not isinstance(frame_meta, dict):
                    continue
                detector_csv = str(frame_meta.get("detector_csv") or "")
                if not detector_csv:
                    continue
                with archive.open(detector_csv, "r") as stream:
                    text = stream.read().decode("utf-8-sig", errors="replace")
                grid = self._detector_grid_from_csv_text(text)
                frames.append({"meta": frame_meta, "grid": grid})
        if not frames:
            raise ValueError("ZZZ 文件中没有可播放的 detector 帧。")
        self.zemax_time_series_path = path
        self.zemax_time_series_manifest = manifest
        self.zemax_time_series_frames = frames
        self.zemax_time_series_current_index = 0
        self.zemax_time_series_timer.stop()
        self.update_zemax_time_series_controls()
        self.display_zemax_time_series_frame(0)
        self.append_log(f"已加载 Zemax 时间序列 ZZZ: {path}，帧数={len(frames)}")

    def _detector_grid_from_csv_text(self, text: str) -> list[list[float]]:
        rows: list[list[float]] = []
        for row in csv.reader(text.splitlines()):
            if not row:
                continue
            values: list[float] = []
            for value in row:
                try:
                    values.append(float(value))
                except ValueError:
                    values.append(0.0)
            if values:
                rows.append(values)
        return rows

    def update_zemax_time_series_controls(self) -> None:
        frame_count = len(self.zemax_time_series_frames)
        if hasattr(self, "zemax_zzz_frame_slider"):
            self.zemax_zzz_frame_slider.blockSignals(True)
            self.zemax_zzz_frame_slider.setRange(0, max(0, frame_count - 1))
            self.zemax_zzz_frame_slider.setValue(min(self.zemax_time_series_current_index, max(0, frame_count - 1)))
            self.zemax_zzz_frame_slider.setEnabled(frame_count > 0)
            self.zemax_zzz_frame_slider.blockSignals(False)
        if hasattr(self, "play_zemax_zzz_button"):
            self.play_zemax_zzz_button.setEnabled(frame_count > 0)
            self.play_zemax_zzz_button.setText("暂停" if self.zemax_time_series_timer.isActive() else "播放")
        if hasattr(self, "zemax_zzz_frame_label"):
            if frame_count:
                self.zemax_zzz_frame_label.setText(f"{self.zemax_time_series_current_index + 1}/{frame_count}")
            else:
                self.zemax_zzz_frame_label.setText("0/0")

    def display_zemax_time_series_frame(self, index: int) -> None:
        if not self.zemax_time_series_frames:
            return
        index = max(0, min(int(index), len(self.zemax_time_series_frames) - 1))
        self.zemax_time_series_current_index = index
        frame = self.zemax_time_series_frames[index]
        grid = frame.get("grid") if isinstance(frame, dict) else None
        if not isinstance(grid, list):
            return
        self.set_zemax_detector_preview_grid(grid)
        meta = frame.get("meta") if isinstance(frame.get("meta"), dict) else {}
        time_label = str(meta.get("time_label") or meta.get("time_value") or index + 1)
        lines = [
            f"ZZZ: {self.zemax_time_series_path}",
            f"当前帧: {index + 1}/{len(self.zemax_time_series_frames)}",
            f"时间: {time_label}",
            f"Detector CSV: {meta.get('detector_csv')}",
        ]
        manifest = self.zemax_time_series_manifest
        detector = manifest.get("detector") if isinstance(manifest.get("detector"), dict) else {}
        if detector:
            lines.insert(1, f"Detector: {self.zemax_detector_summary_line(detector)}")
        self.zemax_raytrace_result_log.setPlainText("\n".join(lines))
        self.update_zemax_time_series_controls()

    @Slot(int)
    def zemax_time_series_slider_changed(self, value: int) -> None:
        self.display_zemax_time_series_frame(value)

    @Slot()
    def toggle_zemax_time_series_playback(self) -> None:
        if not self.zemax_time_series_frames:
            return
        if self.zemax_time_series_timer.isActive():
            self.zemax_time_series_timer.stop()
        else:
            self.zemax_time_series_timer.start(500)
        self.update_zemax_time_series_controls()

    @Slot()
    def advance_zemax_time_series_frame(self) -> None:
        if not self.zemax_time_series_frames:
            self.zemax_time_series_timer.stop()
            self.update_zemax_time_series_controls()
            return
        next_index = (self.zemax_time_series_current_index + 1) % len(self.zemax_time_series_frames)
        self.display_zemax_time_series_frame(next_index)

    def set_zemax_detector_preview_grid(self, grid: list[list[float]]) -> None:
        pixmap = self.zemax_detector_grid_pixmap(grid)
        if pixmap.isNull():
            self.set_zemax_detector_preview(None)
            return
        self.zemax_detector_preview_pixmap = pixmap
        self.zemax_detector_preview_fit_to_window = True
        self.zemax_detector_preview_zoom_factor = self.zemax_detector_fit_preview_factor()
        self.set_zemax_detector_zoom_buttons_enabled(True)
        self.update_zemax_detector_preview_pixmap()

    def zemax_detector_grid_pixmap(self, grid: list[list[float]]) -> QPixmap:
        if not grid or not grid[0]:
            return QPixmap()
        height = len(grid)
        width = len(grid[0])
        minimum = min(float(value) for row in grid for value in row)
        maximum = max(float(value) for row in grid for value in row)
        image = QImage(width, height, QImage.Format_RGB32)
        denominator = math.log1p(maximum - minimum) if maximum > minimum else 1.0
        for y_index, row in enumerate(grid):
            image_y = height - 1 - y_index
            for x_index, value in enumerate(row):
                if maximum <= minimum:
                    normalized = 0.0
                else:
                    normalized = math.log1p(max(0.0, float(value) - minimum)) / denominator
                red, green, blue = self.false_color_rgb(normalized)
                image.setPixel(x_index, image_y, qRgb(red, green, blue))
        return QPixmap.fromImage(image)

    def false_color_rgb(self, value: float) -> tuple[int, int, int]:
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
                fraction = 0.0 if right_pos == left_pos else (value - left_pos) / (right_pos - left_pos)
                return tuple(
                    int(round(left_color[channel] + (right_color[channel] - left_color[channel]) * fraction))
                    for channel in range(3)
                )
        return stops[-1][1]

    def set_zemax_detector_preview(self, image_path: Path | None) -> None:
        if not hasattr(self, "zemax_detector_preview_label"):
            return
        self.zemax_detector_preview_pixmap = None
        if image_path is None or not Path(image_path).exists():
            self.zemax_detector_preview_label.setPixmap(QPixmap())
            self.zemax_detector_preview_label.setText("暂无伪彩色图")
            self.zemax_detector_preview_label.resize(360, 300)
            self.set_zemax_detector_zoom_buttons_enabled(False)
            return
        pixmap = QPixmap(str(image_path))
        if pixmap.isNull():
            self.zemax_detector_preview_label.setPixmap(QPixmap())
            self.zemax_detector_preview_label.setText("伪彩色图打开失败")
            self.zemax_detector_preview_label.resize(360, 300)
            self.set_zemax_detector_zoom_buttons_enabled(False)
            return
        self.zemax_detector_preview_pixmap = pixmap
        self.zemax_detector_preview_fit_to_window = True
        self.zemax_detector_preview_zoom_factor = self.zemax_detector_fit_preview_factor()
        self.set_zemax_detector_zoom_buttons_enabled(True)
        self.update_zemax_detector_preview_pixmap()

    def set_zemax_detector_zoom_buttons_enabled(self, enabled: bool) -> None:
        for button_name in [
            "zemax_detector_zoom_out_button",
            "zemax_detector_zoom_fit_button",
            "zemax_detector_zoom_in_button",
        ]:
            button = getattr(self, button_name, None)
            if button is not None:
                button.setEnabled(enabled)
        label = getattr(self, "zemax_detector_zoom_label", None)
        if label is not None:
            label.setText("无图像" if not enabled else self.zemax_detector_zoom_text())

    def zemax_detector_fit_preview_factor(self) -> float:
        pixmap = self.zemax_detector_preview_pixmap
        if pixmap is None or pixmap.isNull():
            return 1.0
        scroll = getattr(self, "zemax_detector_preview_scroll", None)
        if scroll is None:
            return 1.0
        viewport_size = scroll.viewport().size()
        viewport_width = max(1, viewport_size.width() - 8)
        viewport_height = max(1, viewport_size.height() - 8)
        width_factor = viewport_width / max(1, pixmap.width())
        height_factor = viewport_height / max(1, pixmap.height())
        return max(0.05, min(width_factor, height_factor))

    def zemax_detector_zoom_text(self) -> str:
        if self.zemax_detector_preview_pixmap is None:
            return "无图像"
        if self.zemax_detector_preview_fit_to_window:
            return f"适应 {self.zemax_detector_fit_preview_factor() * 100:.0f}%"
        return f"{self.zemax_detector_preview_zoom_factor * 100:.0f}%"

    def update_zemax_detector_preview_pixmap(self) -> None:
        if not hasattr(self, "zemax_detector_preview_label"):
            return
        pixmap = self.zemax_detector_preview_pixmap
        if pixmap is None or pixmap.isNull():
            return
        if self.zemax_detector_preview_fit_to_window:
            factor = self.zemax_detector_fit_preview_factor()
        else:
            factor = self.zemax_detector_preview_zoom_factor
        factor = max(0.05, min(16.0, float(factor)))
        target_width = max(1, int(round(pixmap.width() * factor)))
        target_height = max(1, int(round(pixmap.height() * factor)))
        scaled = pixmap.scaled(target_width, target_height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.zemax_detector_preview_label.setText("")
        self.zemax_detector_preview_label.setPixmap(scaled)
        self.zemax_detector_preview_label.resize(scaled.size())
        zoom_label = getattr(self, "zemax_detector_zoom_label", None)
        if zoom_label is not None:
            zoom_label.setText(self.zemax_detector_zoom_text())

    @Slot()
    def zoom_in_zemax_detector_preview(self) -> None:
        self.zoom_zemax_detector_preview(1.25)

    @Slot()
    def zoom_out_zemax_detector_preview(self) -> None:
        self.zoom_zemax_detector_preview(0.8)

    def zoom_zemax_detector_preview(self, factor: float) -> None:
        if self.zemax_detector_preview_pixmap is None:
            return
        if self.zemax_detector_preview_fit_to_window:
            self.zemax_detector_preview_zoom_factor = self.zemax_detector_fit_preview_factor()
        self.zemax_detector_preview_fit_to_window = False
        self.zemax_detector_preview_zoom_factor = max(
            0.05,
            min(16.0, self.zemax_detector_preview_zoom_factor * float(factor)),
        )
        self.update_zemax_detector_preview_pixmap()

    @Slot()
    def fit_zemax_detector_preview(self) -> None:
        if self.zemax_detector_preview_pixmap is None:
            return
        self.zemax_detector_preview_fit_to_window = True
        self.zemax_detector_preview_zoom_factor = self.zemax_detector_fit_preview_factor()
        self.update_zemax_detector_preview_pixmap()

    def _format_float(self, value: object) -> str:
        if value is None:
            return "无"
        try:
            return f"{float(value):.12g}"
        except (TypeError, ValueError):
            return str(value)

    def load_analysis_modules(
        self,
        checked: bool = False,
        *,
        show_errors: bool = True,
    ) -> None:
        del checked
        if self.active_thread is not None:
            if show_errors:
                QMessageBox.information(self, "操作正在执行", "当前操作还没有结束。")
            return
        project = self.current_project_path()
        if project is None:
            self.modules = []
            self.modules_table.setRowCount(0)
            if show_errors:
                QMessageBox.warning(self, "未选择文件", "请先选择 Mechanical database（.mechdb/.mechdat）。")
            return

        self.module_load_show_errors = show_errors
        self.modules = []
        self.modules_table.setRowCount(0)
        self.set_operation_buttons_enabled(False)
        self.start_operation_status("读取分析模块", f"正在读取 {project}")
        self.append_log(f"正在后台读取分析模块: {project}")

        thread = QThread(self)
        worker = ModuleLoadWorker(project, self.mechanical_port)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.update_operation_progress)
        worker.finished.connect(self.module_load_finished)
        worker.failed.connect(self.module_load_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self.operation_thread_finished)
        self.active_thread = thread
        self.active_worker = worker
        thread.start()

    @Slot(object)
    def module_load_finished(self, payload: object) -> None:
        if isinstance(payload, dict):
            modules = list(payload.get("modules") or [])
            project = Path(str(payload.get("project") or self.current_project_path() or ""))
        else:
            modules = []
            project = self.current_project_path() or Path("")

        self.apply_analysis_modules(modules, project)
        self.update_operation_progress({"stage": "模块读取完成", "status": f"{len(modules)} 个分析模块"})

    @Slot(str)
    def module_load_failed(self, message: str) -> None:
        self.modules = []
        self.modules_table.setRowCount(0)
        self.append_log(f"读取分析模块失败: {message}")
        self.update_operation_progress({"stage": "模块读取失败", "status": message})
        if self.module_load_show_errors:
            QMessageBox.critical(self, "读取分析模块失败", message)

    def apply_analysis_modules(self, modules: list[AnalysisModule], project: Path) -> None:
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
            QMessageBox.warning(self, "未选择文件", "请先选择 Mechanical database（.mechdb/.mechdat）。")
            return
        module = self.selected_module()
        if module is None:
            QMessageBox.warning(self, "未选择模块", "请先读取工程模块并选择一个分析模块。")
            return
        if self.mechanical_port is None:
            QMessageBox.warning(self, "未启动后台 Mechanical", MECHANICAL_REQUIRED_MESSAGE)
            self.append_log("未启动后台 Mechanical database 会话，未启动读取/求解/导出操作。")
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
        for button_name in [
            "browse_project_button",
            "load_modules_button",
            "read_settings_button",
            "solve_module_button",
            "read_results_button",
            "close_mechanical_button",
            "browse_zemax_project_button",
            "close_zemax_button",
            "refresh_zemax_button",
            "import_pose_button",
            "steady_raytrace_button",
            "zemax_time_series_detector_combo",
            "load_zemax_time_series_detectors_button",
            "transient_time_series_button",
            "transient_raytrace_button",
            "random_vibration_trace_button",
            "random_raytrace_button",
            "random_vibration_count_spin",
            "open_zemax_raytrace_tab_button",
            "load_zemax_detectors_button",
            "open_zemax_zzz_button",
            "read_selected_detector_button",
            "export_selected_detector_button",
            "check_button",
        ]:
            button = getattr(self, button_name, None)
            if button is not None:
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
            self.append_log("已写入当前 Mechanical 会话；工程文件只会在关闭 Mechanical 时按你的选择保存。")
            self.update_operation_progress({"stage": "保存完成", "status": result.system_name})
        elif result.report.get("solved"):
            self.append_log(f"求解完成: {result.system_name} ({result.report.get('solve_method')})")
            solve_resources = result.report.get("solve_resources") if isinstance(result.report.get("solve_resources"), dict) else {}
            if solve_resources:
                self.append_log(
                    "本次默认求解资源: "
                    f"CPU最大逻辑核心={solve_resources.get('requested_cpu_cores')}，"
                    f"GPU={solve_resources.get('requested_gpu_device')} x {solve_resources.get('requested_gpu_devices')}，"
                    f"配置={'已写入' if solve_resources.get('configured') else '未写入'}，"
                    f"Solve配置={solve_resources.get('solve_configuration') or '未知'}"
                )
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
        if self.load_modules_after_mechanical_launch:
            self.load_modules_after_mechanical_launch = False
            QTimer.singleShot(0, lambda: self.load_analysis_modules(show_errors=True))

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

        for row_index, row in enumerate(rows):
            state_text = "正常" if row.ok else "缺失"
            value_text = compact_status_value(row.value)
            name_label = QLabel(f"{row.name}:")
            name_label.setWordWrap(False)
            name_label.setMinimumWidth(0)
            name_label.setMaximumWidth(118)
            name_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

            value_label = QLabel(value_text)
            value_label.setWordWrap(False)
            value_label.setToolTip(row.value)
            value_label.setMinimumWidth(0)
            value_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

            status_label = QLabel(state_text)
            status_label.setWordWrap(False)
            status_label.setProperty("status", "ok" if row.ok else "missing")
            status_label.style().unpolish(status_label)
            status_label.style().polish(status_label)

            self.status_layout.addWidget(name_label, row_index, 0)
            self.status_layout.addWidget(value_label, row_index, 1)
            self.status_layout.addWidget(status_label, row_index, 2)

        self.update_zemax_status()
        self.append_log("环境状态已刷新")

    def update_zemax_status(self) -> None:
        process_ids = sorted(process_ids_by_name(ZEMAX_PROCESS_NAMES))
        process_text = "未运行" if not process_ids else ", ".join(str(pid) for pid in process_ids)

        exe_status = str(OPTICSTUDIO_EXE)
        if not OPTICSTUDIO_EXE.exists():
            exe_status = f"{OPTICSTUDIO_EXE}（缺失）"

        project_text = "未运行"
        selected_project = self.current_zemax_project_path()
        if selected_project is None:
            selected_text = "未选择"
        elif selected_project.exists():
            selected_text = str(selected_project)
        else:
            selected_text = f"{selected_project}（不存在）"
        if process_ids:
            info = self.current_zemax_info if isinstance(self.current_zemax_info, dict) else {}
            system_file = str(info.get("system_file") or "")
            if selected_project is not None and system_file and not self._same_path(system_file, selected_project):
                info = {}
                self.current_zemax_info = {}
            if info:
                system_name = str(info.get("system_name") or "")
                object_count = info.get("object_count")
                instance = info.get("opticstudio_instance")
                project_text = system_file or system_name or "已连接当前工程"
                if object_count is not None:
                    project_text = f"{project_text}（NSC 对象 {object_count} 个）"
                if instance:
                    project_text = f"{project_text}，后台 Instance={instance}"
            else:
                project_text = "检测到 OpticStudio 后台/外部进程，当前软件未连接工程"
        else:
            self.current_zemax_info = {}

        self.zemax_exe_status_label.setText(exe_status)
        self.zemax_selected_project_label.setText(selected_text)
        self.zemax_project_status_label.setText(project_text)
        self.zemax_process_status_label.setText(process_text)

    def _same_path(self, left: str | Path, right: str | Path) -> bool:
        try:
            return Path(left).samefile(Path(right))
        except Exception:
            try:
                return str(Path(left).resolve()).lower() == str(Path(right).resolve()).lower()
            except Exception:
                return str(left).strip().lower() == str(right).strip().lower()

    def collect_status(self) -> list[StatusRow]:
        project = self.current_project_path()
        project_value = str(project) if project is not None else "未选择"
        project_ok = bool(project is not None and project.exists())
        zemax_project = self.current_zemax_project_path()
        zemax_project_value = str(zemax_project) if zemax_project is not None else "未选择"
        zemax_project_ok = bool(zemax_project is not None and zemax_project.exists())
        checks = [
            ("工程/database", project_value, project_ok),
            ("Zemax 工程", zemax_project_value, zemax_project_ok),
            ("RunWB2", RUNWB2, RUNWB2.exists()),
            ("Mechanical", MECHANICAL_EXE, MECHANICAL_EXE.exists()),
            ("Zemax OpticStudio", OPTICSTUDIO_EXE, OPTICSTUDIO_EXE.exists()),
            ("PyMechanical", "ansys.mechanical.core", package_available("ansys.mechanical.core")),
            ("pythonnet", "clr", package_available("clr")),
            ("PySide6", "PySide6", package_available("PySide6")),
        ]
        return [StatusRow(name, str(value), ok) for name, value, ok in checks]

    def open_mechanical(self, auto_load_modules: bool = False) -> None:
        if self.active_thread is not None:
            QMessageBox.information(self, "操作正在执行", "当前操作还没有结束。")
            return
        project = self.current_project_path()
        if project is None:
            QMessageBox.warning(self, "未选择文件", "请先选择 Mechanical database（.mechdb/.mechdat）。")
            return
        if self.mechanical_port is not None:
            self.append_log("当前已经有可连接的 Mechanical 会话。")
            if auto_load_modules:
                QTimer.singleShot(0, lambda: self.load_analysis_modules(show_errors=True))
            return
        module = self.selected_module()
        system_name = module.system_name if module is not None else "MECHANICAL"
        display_text = module.display_text if module is not None else project.name
        self.load_modules_after_mechanical_launch = auto_load_modules
        self.set_operation_buttons_enabled(False)
        self.start_operation_status(
            "启动 Mechanical",
            f"正在后台打开 {display_text}",
        )
        self.append_log(
            "选择工程后正在后台启动 Mechanical database 会话，"
            f"工程={project}，模块={system_name} / {display_text}"
        )

        thread = QThread(self)
        worker = MechanicalLaunchWorker(project, system_name, display_text)
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
                "status": f"后台会话 {result.get('system_name')} / {result.get('analysis_hint')}，{port_text}",
            }
        )
        self.append_log(
            "已用 Mechanical database 启动后台 Mechanical，"
            f"工程={result.get('project')}，database={result.get('database')}，"
            f"模块={result.get('system_name')}，{port_text}"
        )

    @Slot(str)
    def mechanical_launch_failed(self, message: str) -> None:
        self.load_modules_after_mechanical_launch = False
        self.update_operation_progress({"stage": "启动后台 Mechanical 失败", "status": message})
        self.append_log(f"启动后台 Mechanical 失败: {message}")
        QMessageBox.critical(self, "启动后台 Mechanical 失败", message)

    def import_mechanical_pose_to_zemax(self) -> None:
        if self.active_thread is not None:
            QMessageBox.information(self, "操作正在执行", "当前操作还没有结束。")
            return
        zemax_project = self.current_zemax_project_path()
        if zemax_project is None:
            QMessageBox.warning(self, "未选择 Zemax 工程", "请先选择 Zemax 工程文件。")
            return
        if not zemax_project.exists():
            QMessageBox.warning(self, "Zemax 工程不存在", f"所选 Zemax 工程文件不存在:\n{zemax_project}")
            return
        export_folder = self.choose_zemax_mechanical_export_folder()
        if export_folder is None:
            return
        if not export_folder.exists() or not export_folder.is_dir():
            QMessageBox.warning(self, "导出文件夹不存在", f"所选 Mechanical 导出文件夹不存在:\n{export_folder}")
            return
        if not self.apply_zemax_pose_records_from_folder(export_folder, source_label="用户选择 Mechanical 导出文件夹"):
            return

        self.set_operation_buttons_enabled(False)
        self.start_operation_status("Zemax 导入", "后台打开所选 Zemax 工程并写入位移/旋转")
        self.append_log(
            "开始通过后台 ZOS-API 导入 Mechanical 位移/旋转到所选 Zemax 非序列对象，"
            f"Zemax 工程={zemax_project}，用户选择导出文件夹={export_folder}"
        )

        thread = QThread(self)
        worker = ZemaxPoseImportWorker(
            export_folder,
            zemax_project,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.update_operation_progress)
        worker.finished.connect(self.zemax_pose_import_finished)
        worker.failed.connect(self.zemax_pose_import_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self.operation_thread_finished)
        self.active_thread = thread
        self.active_worker = worker
        thread.start()

    @Slot(object)
    def zemax_pose_import_finished(self, result: object) -> None:
        if not isinstance(result, dict):
            result = {}
        matched_count = int(result.get("matched_count") or 0)
        unmatched_count = int(result.get("unmatched_count") or 0)
        system_file = str(result.get("system_file") or "")
        zemax_length_unit = str(result.get("zemax_length_unit") or "")
        saved = bool(result.get("saved"))
        save_error = str(result.get("save_error") or "")
        baseline_file = str(result.get("baseline_file") or "")
        baseline_source = str(result.get("baseline_source") or "")
        baseline_created = bool(result.get("baseline_created"))
        baseline_write_error = str(result.get("baseline_write_error") or "")
        self.current_zemax_info = {
            "system_file": system_file,
            "launch_method": "standalone_background",
        }
        self.update_operation_progress(
            {
                "stage": "Zemax 导入完成",
                "status": f"匹配 {matched_count} 个对象，未匹配 {unmatched_count} 条记录",
            }
        )
        self.append_log(
            "Zemax 非序列对象导入完成: "
            f"匹配 {matched_count}，未匹配 {unmatched_count}，当前工程={system_file or '未命名工程'}，"
            f"Zemax 长度单位={zemax_length_unit or '未知'}，"
            f"保存={'成功' if saved else '未保存，关闭 Zemax 时再选择是否保存'}"
        )
        if baseline_file:
            created_text = "新建" if baseline_created else "沿用"
            self.append_log(f"Zemax 原始基准: {created_text} {baseline_file}，来源={baseline_source or '当前工程'}")
        if baseline_write_error:
            self.append_log(f"Zemax 原始基准保存失败: {baseline_write_error}")
        for item in list(result.get("matched") or [])[:5]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("comment") or item.get("name") or "")
            old_values = item.get("old_values") or []
            baseline_values = item.get("baseline_values") or old_values
            raw_delta = item.get("raw_delta_values") or []
            converted_delta = item.get("converted_delta_values") or []
            new_values = item.get("new_values") or []
            length_scale = item.get("length_scale")
            if (
                len(old_values) >= 6
                and len(baseline_values) >= 6
                and len(raw_delta) >= 6
                and len(converted_delta) >= 6
                and len(new_values) >= 6
            ):
                self.append_log(
                    f"Zemax 导入明细 {name}: "
                    f"位移换算 {item.get('mechanical_displacement_unit')} -> {item.get('zemax_length_unit')} "
                    f"(x{float(length_scale or 1):.6g}); "
                    f"原始位移=({float(raw_delta[0]):.6g}, {float(raw_delta[1]):.6g}, {float(raw_delta[2]):.6g})，"
                    f"导入位移=({float(converted_delta[0]):.6g}, {float(converted_delta[1]):.6g}, {float(converted_delta[2]):.6g})，"
                    f"基准位置=({float(baseline_values[0]):.9g}, {float(baseline_values[1]):.9g}, {float(baseline_values[2]):.9g})，"
                    f"导入前位置=({float(old_values[0]):.9g}, {float(old_values[1]):.9g}, {float(old_values[2]):.9g})，"
                    f"新位置=({float(new_values[0]):.9g}, {float(new_values[1]):.9g}, {float(new_values[2]):.9g})"
                )
        if save_error:
            self.append_log(f"Zemax 当前工程保存失败: {save_error}")
        if unmatched_count:
            unmatched_names = [
                str(item.get("name") or "")
                for item in list(result.get("unmatched") or [])[:10]
                if isinstance(item, dict)
            ]
            if unmatched_names:
                self.append_log("未匹配名称示例: " + ", ".join(unmatched_names))
        QMessageBox.information(
            self,
            "Zemax 导入完成",
            "已导入镜片位移/旋转，但还没有保存 Zemax 工程。\n"
            f"匹配: {matched_count}\n未匹配: {unmatched_count}\n"
            f"当前工程: {system_file or '未命名工程'}\n"
            f"Zemax 长度单位: {zemax_length_unit or '未知'}\n"
            "保存: 未保存，点击“关闭 Zemax”时再选择保存或不保存。",
        )
        self.update_zemax_status()

    @Slot(str)
    def zemax_pose_import_failed(self, message: str) -> None:
        self.update_operation_progress({"stage": "Zemax 导入失败", "status": message})
        self.append_log(f"Zemax 非序列对象导入失败: {message}")
        QMessageBox.critical(self, "Zemax 导入失败", message)

    def close_mechanical(self) -> None:
        if self.active_thread is not None:
            QMessageBox.information(self, "操作正在执行", "当前操作还没有结束。")
            return

        dialog = QMessageBox(self)
        dialog.setWindowTitle("关闭后台 Mechanical")
        dialog.setText("关闭当前后台 Mechanical database 会话前请选择是否保存。")
        dialog.setInformativeText("选择“不保存关闭”会丢弃后台 Mechanical 中尚未保存的更改。")
        save_button = dialog.addButton("保存后关闭", QMessageBox.AcceptRole)
        discard_button = dialog.addButton("不保存关闭", QMessageBox.DestructiveRole)
        cancel_button = dialog.addButton("取消", QMessageBox.RejectRole)
        dialog.setDefaultButton(save_button)
        dialog.exec()
        clicked_button = dialog.clickedButton()
        if clicked_button == cancel_button or clicked_button is None:
            return
        save_project = clicked_button == save_button

        self.set_operation_buttons_enabled(False)
        status_text = (
            "正在保存当前后台 Mechanical database 工程"
            if save_project
            else "正在不保存关闭当前后台 Mechanical database 工程"
        )
        self.start_operation_status("关闭 Mechanical", status_text)
        self.append_log(status_text)

        thread = QThread(self)
        worker = MechanicalCloseWorker(
            self.mechanical_session,
            self.mechanical_port,
            save_project,
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
        status = str(result.get("status") or "已发送关闭请求")
        self.update_operation_progress({"stage": "关闭 Mechanical", "status": status})
        self.append_log(status)
        if not result.get("closed"):
            saved_text = "已保存当前后台 Mechanical database 工程" if result.get("saved") else "已请求不保存关闭当前后台 Mechanical database 工程"
            QMessageBox.information(
                self,
                "关闭后台 Mechanical",
                f"{saved_text}，并发送关闭请求。",
            )

    @Slot(str)
    def mechanical_close_failed(self, message: str) -> None:
        self.update_operation_progress({"stage": "关闭 Mechanical 失败", "status": message})
        self.append_log(f"关闭 Mechanical 失败: {message}")
        QMessageBox.critical(self, "关闭 Mechanical 失败", message)

    def close_zemax(self) -> None:
        if self.active_thread is not None:
            QMessageBox.information(self, "操作正在执行", "当前操作还没有结束。")
            return

        dialog = QMessageBox(self)
        dialog.setWindowTitle("关闭 Zemax")
        dialog.setText("关闭当前后台 Zemax API 工程前请选择是否保存。")
        dialog.setInformativeText(
            "选择“保存后关闭”会保存本程序后台 ZOS-API 会话中的镜片位置/旋转修改；"
            "选择“不保存关闭”会放弃这些未保存导入。此操作不会打开或关闭 Zemax GUI。"
        )
        save_button = dialog.addButton("保存后关闭", QMessageBox.AcceptRole)
        discard_button = dialog.addButton("不保存关闭", QMessageBox.DestructiveRole)
        cancel_button = dialog.addButton("取消", QMessageBox.RejectRole)
        dialog.setDefaultButton(save_button)
        dialog.exec()
        clicked_button = dialog.clickedButton()
        if clicked_button == cancel_button or clicked_button is None:
            return
        save_project = clicked_button == save_button

        self.set_operation_buttons_enabled(False)
        status_text = "正在保存并关闭后台 Zemax API 会话" if save_project else "正在不保存关闭后台 Zemax API 会话"
        self.start_operation_status("关闭 Zemax", status_text)
        self.append_log(status_text)

        thread = QThread(self)
        worker = ZemaxCloseWorker(
            self.current_zemax_project_path(),
            save_project,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.update_operation_progress)
        worker.finished.connect(self.zemax_close_finished)
        worker.failed.connect(self.zemax_close_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self.operation_thread_finished)
        self.active_thread = thread
        self.active_worker = worker
        thread.start()

    @Slot(object)
    def zemax_close_finished(self, result: object) -> None:
        if not isinstance(result, dict):
            result = {}
        managed = result.get("managed") if isinstance(result.get("managed"), dict) else {}
        save_project = bool(result.get("save_project"))
        saved_count = int(managed.get("saved_count") or 0)
        closed_count = int(managed.get("closed_count") or 0)
        matched_sessions = int(managed.get("matched_sessions") or 0)
        errors = [str(item) for item in managed.get("errors") or []]
        if save_project:
            self.append_log(f"Zemax 后台 API 保存会话: 找到 {matched_sessions} 个程序控制会话，保存 {saved_count} 个，关闭 {closed_count} 个")
        else:
            self.append_log(f"Zemax 后台 API 放弃未保存导入: 找到 {matched_sessions} 个程序控制会话，关闭 {closed_count} 个")
        if save_project and matched_sessions == 0:
            self.append_log("未找到本程序创建的后台 ZOS-API 会话，当前没有可保存的 Zemax 后台工程。")
        for error in errors:
            self.append_log(f"Zemax 保存/关闭会话失败: {error}")

        status = "Zemax 后台 API 会话已保存并关闭" if save_project else "Zemax 后台 API 会话已放弃未保存导入并关闭"
        self.update_operation_progress({"stage": "关闭 Zemax", "status": status})
        self.append_log(status)
        self.update_zemax_status()

    @Slot(str)
    def zemax_close_failed(self, message: str) -> None:
        self.update_operation_progress({"stage": "关闭 Zemax 失败", "status": message})
        self.append_log(f"关闭 Zemax 失败: {message}")
        QMessageBox.critical(self, "关闭 Zemax 失败", message)
        self.update_zemax_status()

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
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
