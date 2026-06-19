@echo off
chcp 65001 >nul
title ansys-mechanical-zemax联合仿真程序 (诊断启动)
set "PY=D:\anaconda\envs\zemax310\python.exe"
set "ROOT=D:\260415\ansys_python_control_revised_20260619"
set "LOG=%ROOT%\launch_log.txt"

echo ==== 启动诊断 %date% %time% ==== > "%LOG%"
echo PY=%PY% >> "%LOG%"
echo ROOT=%ROOT% >> "%LOG%"

if not exist "%PY%" (
  echo [错误] 未找到 conda Python: %PY% >> "%LOG%"
  echo [错误] 未找到 conda Python: %PY%
  echo 请确认 zemax310 环境路径,或把正确的 python.exe 路径告诉我。
  pause
  exit /b 1
)

echo --- 检查 Python 与依赖 --- >> "%LOG%"
"%PY%" -c "import sys;print('python',sys.version)" >> "%LOG%" 2>&1
"%PY%" -c "import PySide6;print('PySide6 OK')" >> "%LOG%" 2>&1
"%PY%" -c "import numpy;print('numpy',numpy.__version__)" >> "%LOG%" 2>&1
"%PY%" -c "import ansys.mechanical.core;print('pymechanical OK')" >> "%LOG%" 2>&1

echo --- 启动主程序 --- >> "%LOG%"
echo 正在启动主程序... 窗口若未弹出,请等待依赖加载(可能十几秒)。
"%PY%" "%ROOT%\scripts\run_app.py" >> "%LOG%" 2>&1
echo --- 主程序退出,errorlevel=%errorlevel% --- >> "%LOG%"

echo.
echo 程序已退出。完整日志见: %LOG%
pause
