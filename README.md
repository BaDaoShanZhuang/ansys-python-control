# Ansys Python Control

This folder is the Python automation workspace for the local SYS-5 Ansys model.

## Local paths

- Workbench project: `D:\260415\SYS-5_Mechanical.wbpj`
- Workbench launcher: `D:\Program Files\ANSYS Inc\v261\Framework\bin\Win64\RunWB2.exe`
- Mechanical executable: `D:\Program Files\ANSYS Inc\v261\aisol\bin\winx64\AnsysWBU.exe`
- Zemax OpticStudio executable: `D:\Program Files\ANSYS Inc\v261\Zemax OpticStudio\OpticStudio.exe`
- PyCharm interpreter: `D:\anaconda\envs\zemax310\python.exe`

## Install dependencies into the PyCharm interpreter

Run this from PowerShell or from the PyCharm terminal:

```powershell
& 'D:\260415\ansys_python_control\tools\install_dependencies.ps1'
```

## Run the first checks

Check the configured interpreter and required packages:

```powershell
& 'D:\anaconda\envs\zemax310\python.exe' 'D:\260415\ansys_python_control\scripts\check_environment.py'
```

Start the desktop APP:

```powershell
& 'D:\anaconda\envs\zemax310\python.exe' 'D:\260415\ansys_python_control\scripts\run_app.py'
```

Open Mechanical through Workbench for the first analysis module:

```powershell
& 'D:\anaconda\envs\zemax310\python.exe' 'D:\260415\ansys_python_control\scripts\open_mechanical.py'
```

Open Mechanical through Workbench for a selected project and system:

```powershell
& 'D:\anaconda\envs\zemax310\python.exe' 'D:\260415\ansys_python_control\scripts\open_mechanical.py' 'D:\260415\20260505\SYS_20260424_Mechanical.wbpj' --system 'SYS 4'
```

List the analysis modules already added to a Workbench project:

```powershell
& 'D:\anaconda\envs\zemax310\python.exe' 'D:\260415\ansys_python_control\scripts\list_analysis_modules.py' 'D:\260415\20260505\SYS_20260424_Mechanical.wbpj'
```

Read the selected module's Mechanical analysis settings through Workbench:

```powershell
& 'D:\anaconda\envs\zemax310\python.exe' 'D:\260415\ansys_python_control\scripts\module_operation.py' read --system 'SYS'
```

Change settings on the selected module through Workbench:

```powershell
& 'D:\anaconda\envs\zemax310\python.exe' 'D:\260415\ansys_python_control\scripts\module_operation.py' update --system 'SYS' --set WeakSprings=Off --set SolverType=Direct --set SolverPivotChecking=Error
```

Solve the selected module through Workbench:

```powershell
& 'D:\anaconda\envs\zemax310\python.exe' 'D:\260415\ansys_python_control\scripts\module_operation.py' solve --system 'SYS'
```

`module_operation.py` and the APP operations execute through the selected Workbench system. Settings, solving, result reading, and result export all send commands through the Workbench `Model` container so Mechanical keeps the Workbench project context.

Run an existing Workbench journal against the project:

```powershell
& 'D:\anaconda\envs\zemax310\python.exe' 'D:\260415\ansys_python_control\scripts\run_journal.py' 'D:\260415\create_cavity_outer_surface_named_selection.wbjn'
```

Run a Workbench journal against a selected project:

```powershell
& 'D:\anaconda\envs\zemax310\python.exe' 'D:\260415\ansys_python_control\scripts\run_journal.py' --project 'D:\260415\20260505\SYS_20260424_Mechanical.wbpj' 'D:\260415\20260505\name_cavity_outer_surfaces_for_heating.wbjn'
```

Test direct PyMechanical launch:

```powershell
& 'D:\anaconda\envs\zemax310\python.exe' 'D:\260415\ansys_python_control\scripts\pymechanical_smoke_test.py'
```

Inside PyCharm, use the same scripts directly because the project interpreter is already configured:

```powershell
python -m pip install -e .
python scripts\check_environment.py
python scripts\run_app.py
python scripts\open_mechanical.py
python scripts\list_analysis_modules.py D:\260415\20260505\SYS_20260424_Mechanical.wbpj
python scripts\module_operation.py read --system SYS
python scripts\run_journal.py D:\260415\create_cavity_outer_surface_named_selection.wbjn
python scripts\pymechanical_smoke_test.py
```

## Automation routes

Use `scripts\run_journal.py` for the existing `.wbpj` workflow because Workbench owns the SYS-5 project file.

Use `scripts\pymechanical_smoke_test.py` and `ansys_control\mechanical.py` when building direct PyMechanical automation. PyMechanical can launch Mechanical as a Python-controlled service and run Python inside Mechanical by calling `run_python_script`.

The active workflow uses the PyCharm interpreter above.

## Desktop APP

The first PySide6 desktop APP entry point is:

```powershell
python scripts\run_app.py
```

Current functions:

- Select a `.wbpj` Workbench project path or a Mechanical database file (`.mechdb` / `.mechdat`).
- Read and display the analysis modules already added to the selected project.
- Open the selected module's current Mechanical analysis settings in a separate settings window, but only after the user has already opened the Mechanical project.
- Read settings dynamically from the current Mechanical session's `AnalysisSettings.VisibleProperties`, so different analysis modules show different editable settings.
- Read the selected analysis module's currently added analysis conditions, such as loads, supports, gravity, force, temperature, and thermal boundary conditions, from the current Mechanical tree.
- Change and save the displayed Mechanical settings and editable analysis-condition properties from the settings window.
- Clear the selected module's current generated solution data before solving it in the current Mechanical session.
- Read the selected module's solve setup, analysis conditions, and all existing Solution result objects in a separate results window.
- Export Solution result objects such as deformation and stress to TXT files and export the corresponding Mechanical result view images to PNG files.
- Show the current operation stage, Mechanical status, and elapsed time in a right-side box next to the environment status while reading, saving, opening Mechanical, or solving.
- Open Mechanical directly from the selected Mechanical database in a connectable Mechanical UI without blocking the APP while Mechanical starts.
- Close the current Mechanical session after saving, without force-killing Workbench.
- Open Ansys Zemax OpticStudio.
- Close Zemax OpticStudio from the APP.
- Check local executable paths and required Python packages.
