# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata


spec_dir = Path(SPECPATH).resolve()
app_root = spec_dir.parent
python_prefix = Path(sys.prefix)

datas = [
    (str(app_root / "README.md"), "."),
]

manual = app_root / "docs" / "Windows端软件说明.docx"
if manual.exists():
    datas.append((str(manual), "docs"))

user_manual = app_root / "docs" / "Windows端使用说明书_V26.5.34.docx"
if user_manual.exists():
    datas.append((str(user_manual), "docs"))

formula_doc = app_root / "docs" / "节点位移计算整体位移和旋转_理论公式.docx"
if formula_doc.exists():
    datas.append((str(formula_doc), "docs"))

for package_name in [
    "ansys",
    "ansys.api.mechanical",
    "ansys.mechanical.core",
    "ansys.mechanical.stubs",
    "ansys.tools.common",
    "grpc",
    "google",
    "google.protobuf",
    "pythonnet",
    "clr_loader",
]:
    try:
        datas += collect_data_files(package_name)
    except Exception:
        pass

for distribution_name in [
    "ansys-api-mechanical",
    "ansys-mechanical-core",
    "ansys-mechanical-stubs",
    "ansys-tools-common",
    "grpcio",
    "protobuf",
    "pythonnet",
    "clr-loader",
    "psutil",
    "requests",
]:
    try:
        datas += copy_metadata(distribution_name)
    except Exception:
        pass

hiddenimports = []
for package_name in [
    "ansys",
    "ansys.mechanical",
    "ansys.mechanical.core",
    "ansys.api",
    "ansys.api.mechanical",
    "ansys.mechanical.stubs",
    "ansys.tools",
    "grpc",
    "google.protobuf",
    "pythonnet",
    "clr",
    "clr_loader",
    "psutil",
    "requests",
]:
    try:
        hiddenimports += collect_submodules(package_name)
    except Exception:
        hiddenimports.append(package_name)

conda_binaries = []
for dll_name in [
    "libbz2.dll",
    "LIBBZ2.dll",
    "libssl-3-x64.dll",
    "libcrypto-3-x64.dll",
    "ffi.dll",
]:
    dll_path = python_prefix / "Library" / "bin" / dll_name
    if dll_path.exists():
        conda_binaries.append((str(dll_path), "."))


a = Analysis(
    [str(app_root / "scripts" / "run_app.py")],
    pathex=[str(app_root)],
    binaries=conda_binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "notebook",
        "pytest",
        "tkinter",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ansys-mechanical-zemax",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ansys-mechanical-zemax",
)
