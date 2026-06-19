@echo off
chcp 65001 >nul
title ansys-mechanical-zemax联合仿真程序 (修订版/源码启动)
set "PY=D:\anaconda\envs\zemax310\python.exe"
set "APP=D:\260415\ansys_python_control_revised_20260619\scripts\run_app.py"
if not exist "%PY%" (
  echo [错误] 未找到 conda Python: %PY%
  echo 请在路径设置或环境变量中确认 zemax310 环境位置。
  pause
  exit /b 1
)
echo 正在启动修订版(源码)...
"%PY%" "%APP%"
echo.
echo 程序已退出。若上方有报错,请截图反馈。
pause
