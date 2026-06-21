"""用真实 PySide6 界面 + 模拟数据,截取全流程软件截图(中文需真实 Windows 平台字体)。

注意:不要设 QT_QPA_PLATFORM=offscreen(无字体会出豆腐块)。用 WA_DontShowOnScreen
让窗口完成布局但不真正显示到屏幕,避免打扰用户。
"""
from __future__ import annotations
import sys
import zipfile
import io
from pathlib import Path

ROOT = r"D:/260415/ansys_python_control_revised_20260619"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PySide6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem, QTabWidget
from PySide6.QtCore import Qt
from ansys_control import gui
from ansys_control.mechanical_ops import AnalysisModule, ModuleOperationResult

OUT = Path(ROOT) / "docs" / "images"
OUT.mkdir(parents=True, exist_ok=True)

import logging
logging.basicConfig(level=logging.WARNING)
_logger = logging.getLogger("screenshots")

app = QApplication.instance() or QApplication(sys.argv)
gui._apply_light_palette(app)
gui._apply_app_font(app, _logger)


def realize(w):
    w.setAttribute(Qt.WA_DontShowOnScreen, True)
    w.show()
    app.processEvents()
    app.processEvents()


def grab(w, name):
    realize(w)
    pix = w.grab()
    path = OUT / name
    pix.save(str(path))
    print(f"  saved {name}  {pix.width()}x{pix.height()}")


def fill(table: QTableWidget, rows):
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            table.setItem(r, c, QTableWidgetItem(str(val)))
    table.resizeRowsToContents()


def mock_module():
    return AnalysisModule(1, "MECH-1", "静力结构", "Static Structural",
                          "Structural", "Static Structural", "Mechanical APDL", "MECH-1", True)


def mock_result():
    return ModuleOperationResult(Path("S_optical_model.mechdb"), "MECH-1", "read", {}, Path("journal.py"))


# ============ 1. Mechanical 主界面 ============
def shot_main():
    win = gui.MainWindow()
    win.resize(1300, 720)
    fill(win.modules_table, [
        ("1", "MECH-1", "静力结构", "Static Structural", "Structural", "Static Structural", "Mechanical APDL"),
        ("2", "MECH-5", "瞬态结构", "Transient Structural", "Structural", "Transient", "Mechanical APDL"),
        ("3", "MECH-12", "随机振动", "Random Vibration", "Structural", "Spectrum", "Mechanical APDL"),
    ])
    win.modules_table.selectRow(0)
    for line in ["环境状态已刷新",
                 "选择 Mechanical database 后已启动并保持后台会话",
                 "已从会话读取 3 个分析模块",
                 "已选中 MECH-1 / 静力结构"]:
        win.append_log(line)
    grab(win, "software_main_mechanical.png")
    return win


# ============ 2. 求解中 ============
def shot_solving():
    win = gui.MainWindow()
    win.resize(1300, 720)
    fill(win.modules_table, [
        ("1", "MECH-1", "静力结构", "Static Structural", "Structural", "Static Structural", "Mechanical APDL"),
        ("2", "MECH-5", "瞬态结构", "Transient Structural", "Structural", "Transient", "Mechanical APDL"),
        ("3", "MECH-12", "随机振动", "Random Vibration", "Structural", "Spectrum", "Mechanical APDL"),
    ])
    win.modules_table.selectRow(0)
    try:
        win._set_operation_state("running")
        win.operation_stage_label.setText("求解")
        win.operation_status_label.setText("正在求解 MECH-1 / 静力结构 …")
        win.operation_elapsed_label.setText("00:42")
    except Exception as e:
        print("  [warn] set_operation_state:", e)
    for line in ["开始求解 MECH-1 / 静力结构",
                 "已清除该模块旧结果",
                 "求解资源:尽量使用全部 CPU 核心",
                 "正在调用 Mechanical 求解 …"]:
        win.append_log(line)
    grab(win, "mechanical_solve_mock.png")


# ============ 3. Zemax 页 + 位移/旋转计算结果 ============
def shot_zemax_pose():
    win = gui.MainWindow()
    win.resize(1300, 720)
    try:
        win.main_stack.setCurrentIndex(1)
    except Exception as e:
        print("  [warn] switch zemax page:", e)
    records = [
        {"name": "M1", "x": 1.82e-7, "y": -2.41e-7, "z": 5.06e-8, "rx": 3.1e-6, "ry": -1.4e-6, "rz": 2.0e-7, "rms_residual": 4.7e-9, "source": "01_M1.txt"},
        {"name": "M2", "x": -9.3e-8, "y": 1.55e-7, "z": -3.2e-8, "rx": -2.2e-6, "ry": 8.0e-7, "rz": -1.1e-7, "rms_residual": 3.1e-9, "source": "02_M2.txt"},
        {"name": "L1", "x": 4.0e-8, "y": -1.1e-8, "z": 2.7e-7, "rx": 5.0e-7, "ry": 6.2e-7, "rz": -9.0e-8, "rms_residual": 8.8e-9, "source": "03_L1.txt"},
        {"name": "L2", "x": -2.6e-8, "y": 7.7e-8, "z": -1.9e-7, "rx": -4.1e-7, "ry": -3.3e-7, "rz": 1.2e-7, "rms_residual": 6.0e-9, "source": "04_L2.txt"},
    ]
    win.populate_zemax_pose_table(records)
    try:
        win.set_zemax_pose_log([
            "选择文件夹并稳态导入:S_optical_model/exports/MECH-1",
            "匹配对象:M1, M2, L1, L2(共 4 个);未匹配名称:无",
            "已按 基准+增量 写入 Zemax 非序列对象",
            "面形残差 RMS 均为纳米级,变形接近纯刚体",
        ])
    except Exception as e:
        print("  [warn] set_zemax_pose_log:", e)
    grab(win, "zemax_pose_results.png")


# ============ 4. 随机振动导入 tab ============
def shot_random_vibration():
    win = gui.MainWindow()
    win.resize(1300, 720)
    try:
        win.main_stack.setCurrentIndex(1)
    except Exception:
        pass
    # 找到"随机振动导入"那个 QTabWidget 并切到该 tab
    for tw in win.findChildren(QTabWidget):
        for i in range(tw.count()):
            if "随机振动" in tw.tabText(i):
                tw.setCurrentIndex(i)
    win.populate_zemax_pose_table([
        {"name": "M1", "x": 8.2e-8, "y": -1.1e-7, "z": 3.0e-8, "rx": 1.6e-6, "ry": -9.0e-7, "rz": 1.2e-7, "rms_residual": 2.4e-9, "source": "M1_1sigma.txt"},
        {"name": "M2", "x": -5.1e-8, "y": 7.7e-8, "z": -1.8e-8, "rx": -1.1e-6, "ry": 4.0e-7, "rz": -6.0e-8, "rms_residual": 1.9e-9, "source": "M2_1sigma.txt"},
    ])
    try:
        win.set_zemax_pose_log([
            "选择文件夹并导入随机振动:已读取各镜片 1σ 位移/旋转",
            "Σ = D·C·D(默认 C=I,独立);如需相关性放入 pose_correlation.csv",
            "将按多元正态生成 20 个样本,逐样本写入并追迹保存为 DTS",
        ])
    except Exception:
        pass
    grab(win, "zemax_random_vibration.png")


# ============ 5. 分析设置对话框 ============
def shot_settings_dialog():
    try:
        dlg = gui.AnalysisSettingsDialog(mock_module(), mock_result())
    except Exception as e:
        print("  [warn] AnalysisSettingsDialog 构造失败:", e)
        return
    dlg.resize(1180, 1000)
    if hasattr(dlg, "summary_label"):
        dlg.summary_label.setText("MECH-12 / 随机振动 — 分析设置(来自当前 Mechanical 会话的真实属性)")
    fill(dlg.settings_table, [
        ("Options / Number Of Modes To Use", "NumberOfModesToUse", "Program Controlled", "All", "枚举"),
        ("Output Controls / Stress", "Stress", "Yes", "Yes", "布尔"),
        ("Output Controls / Strain", "Strain", "No", "Yes", "布尔"),
        ("Output Controls / Velocity", "Velocity", "No", "Yes", "布尔"),
        ("Output Controls / Acceleration", "Acceleration", "No", "Yes", "布尔"),
        ("Damping Controls / Damping Ratio", "DampingRatio", "0.02", "0.02", "数字"),
        ("Analysis Data Management / Save MAPDL db", "SaveMAPDLDb", "No", "Yes", "枚举"),
    ])
    if hasattr(dlg, "conditions_table"):
        fill(dlg.conditions_table, [
            ("PSD G Acceleration", "PSD 加速度", "已定义(5 行)", "Tabular", "—", "Active"),
            ("Fixed Support", "固定约束", "镜筒法兰面", "—", "—", "Active"),
        ])
    # 填 Tabular Data 预览(PSD 谱),让预览区不空
    if hasattr(dlg, "tabular_summary_table"):
        fill(dlg.tabular_summary_table, [
            ("PSD G Acceleration", "Magnitude", "Mechanical", "2", "5", "已读取"),
        ])
    if hasattr(dlg, "tabular_data_table"):
        t = dlg.tabular_data_table
        t.setColumnCount(2)
        t.setHorizontalHeaderLabels(["Frequency [Hz]", "PSD [G²/Hz]"])
        psd = [("20", "0.010"), ("50", "0.040"), ("200", "0.040"), ("500", "0.020"), ("2000", "0.005")]
        t.setRowCount(len(psd))
        for r, row in enumerate(psd):
            for c, v in enumerate(row):
                t.setItem(r, c, QTableWidgetItem(v))
    if hasattr(dlg, "tabular_import_hint_label"):
        dlg.tabular_import_hint_label.setText("已选中 PSD G Acceleration:5 行频率/PSD 数据。")
    if hasattr(dlg, "messages_table"):
        fill(dlg.messages_table, [
            ("Info", "Solve completed"),
            ("Warning", "Participation factor summary: 92% mass captured"),
        ])
    grab(dlg, "mechanical_settings_dialog.png")


# ============ 6. 求解结果对话框 ============
def shot_results_dialog():
    try:
        dlg = gui.SolutionResultsDialog(mock_module(), mock_result())
    except Exception as e:
        print("  [warn] SolutionResultsDialog 构造失败:", e)
        return
    dlg.resize(1120, 720)
    if hasattr(dlg, "summary_label"):
        dlg.summary_label.setText("MECH-1 / 静力结构 — Solution 结果对象(选中一个导出为 TXT,用于刚体位移/旋转换算)")
    if hasattr(dlg, "settings_table"):
        fill(dlg.settings_table, [
            ("Total Deformation", "TotalDeformation", "Max 3.42e-6 m", "结果"),
            ("Directional Deformation (X)", "DirectionalDeformation", "Max 1.8e-6 m", "结果"),
            ("Directional Deformation (Y)", "DirectionalDeformation", "Max 2.4e-6 m", "结果"),
            ("Directional Deformation (Z)", "DirectionalDeformation", "Max 5.1e-7 m", "结果"),
            ("Equivalent Stress", "EquivalentStress", "Max 12.6 MPa", "结果"),
        ])
    # 所有求解结果(标签页)
    if hasattr(dlg, "populate_result_tabs"):
        dlg.populate_result_tabs([
            {"name": "M1 Directional Deformation", "path": "Solution/M1_Deformation",
             "category": "Directional Deformation", "state": "Solved",
             "properties": [
                 {"display_name": "Orientation", "api_name": "NormalOrientation", "value": "Z Axis", "type": "枚举"},
                 {"display_name": "Maximum", "api_name": "Maximum", "value": "5.06e-7 m", "type": "结果"},
                 {"display_name": "Minimum", "api_name": "Minimum", "value": "-3.1e-7 m", "type": "结果"},
                 {"display_name": "Scoping", "api_name": "Location", "value": "Named Selection: M1", "type": "文本"},
             ]},
            {"name": "M2 Total Deformation", "path": "Solution/M2_Total",
             "category": "Total Deformation", "state": "Solved",
             "properties": [
                 {"display_name": "Maximum", "api_name": "Maximum", "value": "3.42e-6 m", "type": "结果"},
                 {"display_name": "Scoping", "api_name": "Location", "value": "Named Selection: M2", "type": "文本"},
             ]},
        ])
    grab(dlg, "mechanical_results_dialog.png")


# ============ 7. 探测器伪彩光斑(真实数据渲染)============
def _load_real_detector_grid():
    base = Path(r"D:/260415/20260505/S_optical_model_SstageV8(1)/S_optical_model_SstageV8/zemax_time_series_results")
    raws = list(base.glob("*_matlab_raw.dts"))
    if not raws:
        return None
    z = zipfile.ZipFile(raws[0])
    csv = z.read("frames/0001/detector.csv").decode()
    grid = [[float(v) for v in line.split(",")] for line in csv.strip().splitlines()]
    return grid


def shot_detector_window():
    grid = _load_real_detector_grid()
    win = gui.MainWindow()
    win.resize(1300, 720)
    try:
        win.main_stack.setCurrentIndex(1)
    except Exception:
        pass
    # 构建并取得"Zemax 追迹 / 探测器"窗口(惰性构建,内含预览控件)
    dlg = win.ensure_zemax_raytrace_dialog()
    dlg.resize(1180, 720)
    # 探测器下拉框填模拟项
    try:
        combo = win.zemax_detector_combo
        combo.clear()
        combo.addItem("254 | 焦面探测器 | Detector Rectangle | 1000 x 1000", 254)
        combo.addItem("249 | M2 | Detector Rectangle | 512 x 512", 249)
        combo.setCurrentIndex(0)
    except Exception as e:
        print("  [warn] detector combo:", e)
    # 真实数据渲染伪彩光斑
    if grid is not None:
        try:
            win.set_zemax_detector_preview_grid(grid)
        except Exception as e:
            print("  [warn] preview grid:", e)
    # 追迹/结果摘要日志
    for attr, lines in (
        ("zemax_raytrace_result_log", [
            "光线追迹完成(NSC,NumberOfCores=全部逻辑核心)",
            "Detector 254:总通量 31.98,峰值 0.1396,非零像素 10224",
            "当前质心:X=495.930 px, Y=499.002 px",
        ]),
        ("zemax_detector_result_log", [
            "已读取全分辨率网格 1000 x 1000",
            "可“完整导出MATLAB”为 .mat,或滚轮缩放查看伪彩光斑。",
        ]),
    ):
        w = getattr(win, attr, None)
        if w is not None:
            try:
                w.setPlainText("\n".join(lines))
            except Exception:
                try:
                    w.setText("\n".join(lines))
                except Exception:
                    pass
    grab(dlg, "zemax_detector_window.png")


if __name__ == "__main__":
    for fn in [shot_main, shot_solving, shot_zemax_pose, shot_random_vibration,
               shot_settings_dialog, shot_results_dialog, shot_detector_window]:
        print(f"== {fn.__name__} ==")
        try:
            fn()
        except Exception as e:
            import traceback
            print(f"  [ERROR] {fn.__name__}: {e}")
            traceback.print_exc()
    print("done")
