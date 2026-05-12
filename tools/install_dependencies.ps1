$ErrorActionPreference = "Stop"

$project = "D:\260415\ansys_python_control"
$python = "D:\anaconda\envs\zemax310\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "PyCharm interpreter not found: $python"
}

& $python -m pip install -e $project
& $python (Join-Path $project "scripts\check_environment.py")
