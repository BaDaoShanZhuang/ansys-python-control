# ansys-mechanical-zemax联合仿真程序

A local desktop controller for **Ansys Mechanical ↔ Zemax OpticStudio** co-simulation (STOP: Structural–Thermal–Optical Performance). Version **V26.5.34**.

It drives both solvers from one GUI: runs a background Mechanical session, exports node displacements, converts them to optical element rigid-body displacement/rotation (decenter / despace / tilt), writes them into a non-sequential Zemax model, runs ray traces, and reads detector results. It does **not** replace the solvers — FEM is still solved by Mechanical and ray tracing by Zemax.

The current main workflow uses Mechanical **database files** (`.mechdb` / `.mechdat`). After a database file is selected, the APP opens it in a background Mechanical session, connects through PyMechanical, reads analyses from that session, and performs settings / solve / result-read / export against it.

## Documentation

| Doc (`docs/`) | Audience |
|---------------|----------|
| `Windows端软件说明` | 用户向：软件总体说明 |
| `Windows端使用说明书_V26.5.34` | 用户向：操作手册 |
| `节点位移计算整体位移和旋转_理论公式` | 刚体配准 / 随机振动采样的数学推导 |
| `软件开发文档` | 开发/维护者：架构、模块、数据格式、构建 |

MATLAB helpers for the detector time-series `.dts` files live in `tools/matlab/`.

## Local paths

- Mechanical executable: `D:\Program Files\ANSYS Inc\v261\aisol\bin\winx64\AnsysWBU.exe`
- Zemax OpticStudio executable: `D:\Program Files\ANSYS Inc\v261\Zemax OpticStudio\OpticStudio.exe`
- Python interpreter (source runs): `D:\anaconda\envs\zemax310\python.exe` (`3.10 ≤ ver < 3.12`)

These are auto-detected on first run; override via `文件 → 路径设置` or the env vars below.

## Install dependencies (source)

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\install_dependencies.ps1
```

Dependencies: `ansys-mechanical-core`, `PySide6`, `pythonnet`, `numpy`, `scipy` (see `requirements.txt`).

## Run (source)

```powershell
& 'D:\anaconda\envs\zemax310\python.exe' scripts\check_environment.py   # verify interpreter + packages + exe paths
& 'D:\anaconda\envs\zemax310\python.exe' scripts\run_app.py             # start the desktop APP
```

Inside PyCharm, point the interpreter at the same conda env, then:

```powershell
python -m pip install -e .
python scripts\check_environment.py
python scripts\run_app.py
```

## Build installer

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_windows_installer.ps1
# first time, to also install Inno Setup 6:
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_windows_installer.ps1 -InstallInnoSetup
```

The installer is written to `release\ansys-mechanical-zemax_Setup_V26.5.34.exe`. It bundles the Python runtime, PySide6, PyMechanical/ZOS-API dependencies, numpy/scipy, and the `docs/` documentation. It does **not** include Ansys Mechanical, Zemax OpticStudio, or their licenses — those must already be installed and licensed on the target machine.

On first startup the APP scans common install locations (`C:\` / `D:\Program Files\ANSYS Inc\v*` and common OpticStudio folders) and saves detected paths to `%APPDATA%\WindowsDuan\settings.json`. Runtime logs roll daily into `%APPDATA%\WindowsDuan\logs\`.

Path priority is **environment variables > user config > auto-detection**. Env vars: `ANSYS_ROOT`, `ANSYS_RUNWB2`, `ANSYS_MECHANICAL_EXE`, `ANSYS_OPTICSTUDIO_EXE`.

## Desktop APP — current functions

**Mechanical**

- Select a database (`.mechdb` / `.mechdat`); open it in a persistent background session without blocking the UI.
- Read analysis modules from the live session (not guessed from filenames).
- Read editable settings dynamically from `AnalysisSettings.VisibleProperties`; read/import Tabular Data; read analysis conditions (loads, supports, force, temperature, gravity, thermal BCs).
- Save changes back to the session (written to disk only when you choose "save" on close).
- Clear the module's old solution before solving; show stage / status / elapsed in the "current operation" panel.
- Read Solution result objects; export one result to TXT (with time/frequency-set range export) or PNG.
- Close with a save / discard / cancel dialog.

**Zemax (background ZOS-API, no GUI window)**

- Select a `.zmx` project.
- Read Mechanical node-displacement exports and compute rigid-body displacement/rotation per element via SVD/Kabsch (with reflection protection), plus a surface-figure residual RMS diagnostic.
- Import poses as `original baseline + current Mechanical delta` (repeated imports do not accumulate). Three modes: **steady-state**, **transient** and **random vibration**; transient/random use a two-step *import folder → trace & save DTS* flow.
- Random vibration samples N(0, Σ=D·C·D) from the per-DOF 1σ (optional `pose_correlation.csv`).
- Ray trace: clear non-sequential detectors, set NSC `NumberOfCores` to all logical cores, run one trace with the project's default settings.
- Detector window: read detectors, full-resolution quick view with mouse-wheel zoom, export to **MATLAB `.mat`** + pseudo-color PNG, and play detector time-series `.dts` (per-frame centroid shown during playback).

## Code layout

| File | Responsibility |
|------|----------------|
| `ansys_control/gui.py` | PySide6 desktop APP: `MainWindow`, dialogs, `*Worker` threads, light-theme QSS |
| `ansys_control/mechanical.py` | Mechanical launch / connection / database discovery / close-save / session port |
| `ansys_control/mechanical_ops.py` | Mechanical journal-script generation, settings & result collection, exports |
| `ansys_control/zemax.py` | Background ZOS-API, pose calculation, baseline handling, ray trace, detector & DTS export |
| `ansys_control/config.py` | Path auto-detection and user settings (`settings.json`) |
| `ansys_control/logging_setup.py` | File logging + global excepthook + Qt message handler |

## Legacy Workbench tools

Command-line scripts for old `.wbpj` / `.wbpz` workflows remain as compatibility helpers only; the desktop workflow should use `.mechdb` / `.mechdat`:

```powershell
python scripts\list_analysis_modules.py D:\path\to\project.wbpj
python scripts\open_mechanical.py D:\path\to\project.wbpj --system SYS
python scripts\module_operation.py read --project D:\path\to\project.wbpj --system SYS
python scripts\run_journal.py --project D:\path\to\project.wbpj D:\path\to\journal.wbjn
```
