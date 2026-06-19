param(
    [string]$Python = "D:\anaconda\envs\zemax310\python.exe",
    [switch]$SkipInstaller,
    [switch]$InstallInnoSetup
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$SpecPath = Join-Path $RepoRoot "packaging\windows_app.spec"
$InnoScript = Join-Path $RepoRoot "packaging\windows_app.iss"
$AppName = "Windows" + [char]0x7aef
$DistDir = Join-Path $RepoRoot ("dist\" + $AppName)
$ReleaseDir = Join-Path $RepoRoot "release"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Find-Iscc {
    $cmd = Get-Command iscc -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }
    return $null
}

if (!(Test-Path $Python)) {
    throw "Python not found: $Python"
}

Write-Step "Check PyInstaller"
$oldErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$pyinstallerVersion = & $Python -c "import PyInstaller; print(PyInstaller.__version__)" 2>$null
$pyinstallerExitCode = $LASTEXITCODE
$ErrorActionPreference = $oldErrorActionPreference
if ($pyinstallerExitCode -ne 0) {
    Write-Host "PyInstaller is missing in the selected Python environment. Installing..." -ForegroundColor Yellow
    & $Python -m pip install --upgrade pyinstaller pyinstaller-hooks-contrib
} else {
    Write-Host "PyInstaller: $pyinstallerVersion" -ForegroundColor Green
}

Write-Step "Clean old build output"
Remove-Item -Recurse -Force (Join-Path $RepoRoot "build") -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $RepoRoot "dist") -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null

Write-Step "Build application folder"
& $Python -m PyInstaller --clean --noconfirm $SpecPath
if (!(Test-Path (Join-Path $DistDir ($AppName + ".exe")))) {
    throw "PyInstaller did not generate the expected executable: $DistDir\$AppName.exe"
}

Write-Host "Application folder created: $DistDir" -ForegroundColor Green

if ($SkipInstaller) {
    Write-Host "Installer build skipped. You can distribute the dist application folder directly." -ForegroundColor Yellow
    exit 0
}

$iscc = Find-Iscc
if (!$iscc -and $InstallInnoSetup) {
    Write-Step "Install Inno Setup"
    winget install --id JRSoftware.InnoSetup --exact --accept-package-agreements --accept-source-agreements
    $iscc = Find-Iscc
}

if (!$iscc) {
    Write-Host "Inno Setup was not found, so the .exe installer cannot be created." -ForegroundColor Yellow
    Write-Host "Install Inno Setup 6, or rerun: tools\build_windows_installer.ps1 -InstallInnoSetup" -ForegroundColor Yellow
    Write-Host "Distributable application folder: $DistDir" -ForegroundColor Yellow
    exit 0
}

Write-Step "Build installer"
& $iscc $InnoScript

$installer = Get-ChildItem $ReleaseDir -Filter "*_Setup_*.exe" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

if (!$installer) {
    throw "Installer was not generated."
}

Write-Host "Installer created: $($installer.FullName)" -ForegroundColor Green
