# Ansys–Zemax STOP 联动控制台(原 Windows端)

`Windows端 V26.5.34` is a local desktop controller for Ansys Mechanical and Zemax OpticStudio.

The current main workflow uses Mechanical database files (`.mechdb` / `.mechdat`). After a database file is selected, the APP opens the current project in a background Mechanical session, connects through PyMechanical, reads the analyses from that Mechanical session, and performs settings, solve, result-read, and export operations against the current background Mechanical session.

## Local paths

- Mechanical executable: `D:\Program Files\ANSYS Inc\v261\aisol\bin\winx64\AnsysWBU.exe`
- Zemax OpticStudio executable: `D:\Program Files\ANSYS Inc\v261\Zemax OpticStudio\OpticStudio.exe`
- Python interpreter: `D:\anaconda\envs\zemax310\python.exe`

## Install dependencies

Run this from PowerShell or from the PyCharm terminal:

```powershell
& 'D:\260415\ansys_python_control\tools\install_dependencies.ps1'
```

## Run

Check the configured interpreter and required packages:

```powershell
& 'D:\anaconda\envs\zemax310\python.exe' 'D:\260415\ansys_python_control\scripts\check_environment.py'
```

Start the desktop APP:

```powershell
& 'D:\anaconda\envs\zemax310\python.exe' 'D:\260415\ansys_python_control\scripts\run_app.py'
```

Inside PyCharm, use the same interpreter:

```powershell
python -m pip install -e .
python scripts\check_environment.py
python scripts\run_app.py
```

## Build installer

Build a distributable Windows installer:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_windows_installer.ps1
```

If Inno Setup 6 is not installed yet, run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build_windows_installer.ps1 -InstallInnoSetup
```

The generated installer is written to `release\Windows端_Setup_V26.5.34.exe`.
The installer includes the Python runtime, PySide6, PyMechanical/ZOS-API Python dependencies, and bundled documentation. It does not include Ansys Mechanical, Zemax OpticStudio, or their licenses; those must already be installed and licensed on the target computer.

On first startup after installation, the APP automatically scans common Ansys and Zemax install locations, including `C:\Program Files\ANSYS Inc\v*`, `D:\Program Files\ANSYS Inc\v*`, and common OpticStudio folders. Detected paths are saved to `%APPDATA%\WindowsDuan\settings.json`.

The user can open `文件 -> 路径设置` or click `路径设置` in the Mechanical console to review or change:

- ANSYS root directory
- `RunWB2.exe`
- `AnsysWBU.exe`
- `OpticStudio.exe`

Environment variables still have the highest priority when they are set: `ANSYS_ROOT`, `ANSYS_RUNWB2`, `ANSYS_MECHANICAL_EXE`, and `ANSYS_OPTICSTUDIO_EXE`.

## Desktop APP

Current functions:

- Select a Mechanical database file (`.mechdb` / `.mechdat`).
- Open the selected database in a background Mechanical session without blocking the APP.
- Read analysis modules from the currently opened Mechanical session.
- Open a settings window for the selected analysis module only after Mechanical is open.
- Read editable Mechanical analysis settings dynamically from `AnalysisSettings.VisibleProperties`.
- Read the selected module's analysis conditions, including loads, supports, force, temperature, gravity, and thermal boundary conditions.
- Save changed settings and editable analysis-condition properties back to the current Mechanical session.
- Clear the selected module's current generated solution data before solving.
- Show solve stage, status, and elapsed time in the right-side operation-status panel.
- Read solve setup, analysis conditions, and Solution result objects from the current module.
- Export one selected Solution result to TXT, including time-range export when the result supports time/frequency sets.
- Export the selected Mechanical result view image to PNG.
- Close the current Mechanical session with a dialog that lets the user save, discard changes, or cancel.
- Select a Zemax `.zmx` project without opening the OpticStudio GUI.
- Read Mechanical exported mirror/lens node results and calculate rigid-body position and rotation changes.
- Import calculated lens pose changes into the selected non-sequential Zemax project through a background ZOS-API session.
- Write Zemax lens poses as `original baseline + current Mechanical delta`, so repeated imports do not accumulate the same change.
- Open a popup Zemax ray-trace/detector tab, clear all non-sequential Detector objects, run one background NSC ray trace with the current Zemax project's default ray-trace settings, then let the user select one Detector and export only that detector grid to CSV and pseudo-color PNG.
- Detect the local logical CPU core count and set Zemax NSC Ray Trace `NumberOfCores` to all cores by default.
- Save or discard the program-managed background Zemax API session from the APP.
- Check local executable paths and required Python packages.

## Code layout

- `ansys_control\gui.py`: PySide6 desktop APP.
- `ansys_control\mechanical.py`: Mechanical launch, connection, database discovery, and close/save helpers.
- `ansys_control\mechanical_ops.py`: Mechanical operations, result export, and legacy Workbench-compatible operations.
- `ansys_control\zemax.py`: Zemax background ZOS-API import, Mechanical pose calculation, baseline handling, ray trace, and detector image export.

## Legacy Workbench tools

Some command-line scripts still exist for old `.wbpj` / `.wbpz` workflows:

```powershell
python scripts\list_analysis_modules.py D:\path\to\project.wbpj
python scripts\open_mechanical.py D:\path\to\project.wbpj --system SYS
python scripts\module_operation.py read --project D:\path\to\project.wbpj --system SYS
python scripts\run_journal.py --project D:\path\to\project.wbpj D:\path\to\journal.wbjn
```

These are compatibility helpers only. The desktop APP workflow should use `.mechdb` / `.mechdat`.

