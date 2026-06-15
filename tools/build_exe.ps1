<#
  Build a standalone LazeR.exe - bundles Python + every dependency so other
  Windows users can run the server with NO Python installed.

  Output: dist\LazeR.exe  (single file, ~15-25 MB)

  Run from anywhere (paths resolve relative to this script):
    powershell -ExecutionPolicy Bypass -File tools\build_exe.ps1

  Then ship dist\LazeR.exe with LazeR.apk and START_HERE.md. Double-click to run.
#>
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
Set-Location $root

function Find-Python {
    foreach ($c in @("python", "py")) {
        try { $v = & $c --version 2>$null; if ($?) { return $c } } catch {}
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host "Python needed to BUILD the exe (end users won't need it)." -ForegroundColor Yellow
    Write-Host "  winget install -e --id Python.Python.3.12"
    exit 1
}
Write-Host "Building with $(& $py --version)" -ForegroundColor Cyan

# Build-time deps: the app's runtime deps + PyInstaller.
& $py -m pip install --quiet --disable-pip-version-check pyinstaller pynput cryptography pycaw comtypes zeroconf qrcode pillow pystray
if (-not $?) { Write-Host "pip install failed." -ForegroundColor Red; exit 1 }

$script = Join-Path $root "server\remote_server.py"
$icon   = Join-Path $root "server\LazeR.ico"

# --collect-all pulls data/binaries/hidden submodules for libs PyInstaller's
# static analysis misses (zeroconf, comtypes, pycaw, pynput backends, pystray).
# Excludes trim dead weight with zero feature loss:
#  - comtypes.test: a large unit-test suite dragged in by --collect-all comtypes.
#  - non-Windows pynput backends (X11/macOS/uinput): never used on Windows.
#  - gi (GTK): pystray imports it conditionally; the win32 tray backend is used here.
$exclude = @(
    "comtypes.test",
    "pynput.keyboard._xorg", "pynput.keyboard._darwin", "pynput.keyboard._uinput",
    "pynput.mouse._xorg", "pynput.mouse._darwin",
    "pynput._util.xorg", "pynput._util.darwin", "pynput._util.darwin_vks",
    "pynput._util.uinput",
    "gi"
)
$pyiArgs = @(
    "--noconfirm", "--clean", "--onefile", "--windowed",
    "--name", "LazeR",
    "--icon", $icon,
    "--add-data", "$icon;.",
    "--collect-all", "zeroconf",
    "--collect-all", "comtypes",
    "--collect-all", "pycaw",
    "--collect-submodules", "pynput",
    "--collect-submodules", "cryptography",
    "--collect-all", "pystray",
    "--hidden-import", "PIL._tkinter_finder"
)
foreach ($m in $exclude) { $pyiArgs += @("--exclude-module", $m) }
# UPX (optional): if upx.exe sits in tools\upx\, PyInstaller compresses the bundled
# DLLs/pyds with it - lossless, ~tiny startup cost. We EXCLUDE the crypto + C-runtime
# binaries from UPX: a corrupted crypto binding would silently disable encryption
# (the import is guarded), so those stay byte-identical to an uncompressed build.
$upxSkip = @(
    "vcruntime140.dll", "vcruntime140_1.dll",
    "_rust.pyd", "_ssl.pyd", "_hashlib.pyd",
    "libcrypto-3.dll", "libcrypto-3-x64.dll", "libssl-3.dll", "libssl-3-x64.dll"
)
$upxDir = Join-Path $here "upx"
if (Test-Path (Join-Path $upxDir "upx.exe")) {
    Write-Host "UPX found - compressing binaries (crypto/runtime DLLs excluded)." -ForegroundColor Green
    $pyiArgs += @("--upx-dir", $upxDir)
    foreach ($f in $upxSkip) { $pyiArgs += @("--upx-exclude", $f) }
} else {
    Write-Host "UPX not found (tools\upx\upx.exe) - skipping binary compression." -ForegroundColor DarkGray
}
$pyiArgs += @(
    "--distpath", (Join-Path $root "dist"),
    "--workpath", (Join-Path $root "build"),
    "--specpath", (Join-Path $root "build"),
    $script
)

& $py -m PyInstaller @pyiArgs
if (-not $?) { Write-Host "PyInstaller build failed." -ForegroundColor Red; exit 1 }

$exe = Join-Path $root "dist\LazeR.exe"
if (Test-Path $exe) {
    Write-Host ""
    Write-Host "Built: $exe" -ForegroundColor Green
    Write-Host "Ship dist\LazeR.exe + LazeR.apk + START_HERE.md. No Python needed to run it." -ForegroundColor Cyan
} else {
    Write-Host "Build finished but LazeR.exe not found - check the output above." -ForegroundColor Yellow
}
