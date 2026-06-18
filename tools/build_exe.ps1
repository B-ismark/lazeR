<#
  Build a standalone LazeR.exe - bundles Python + every dependency so other
  Windows users can run the server with NO Python installed.

  Output: dist\LazeR.exe  (single file, ~15-25 MB)

  Run from anywhere (paths resolve relative to this script):
    powershell -ExecutionPolicy Bypass -File tools\build_exe.ps1

  Then ship dist\LazeR.exe with LazeR.apk and START_HERE.md. Double-click to run.
#>
# NOT 'Stop': the build shells out to uv/pip/PyInstaller, which all log progress to
# stderr. Under 'Stop', PowerShell 5.1 turns that normal stderr into a terminating
# NativeCommandError and aborts the build. We use 'Continue' and gate every native
# step on $LASTEXITCODE (and a final exe-exists check) instead.
$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
Set-Location $root

function Find-Python {
    # Prefer the project's local virtualenv (incl. uv-created .venv) so the build
    # reuses the deps already installed there; fall back to a global interpreter.
    $venv = Join-Path $root "server\.venv\Scripts\python.exe"
    if (Test-Path $venv) { return $venv }
    foreach ($c in @("python", "py")) {
        try { & $c --version *>$null; if ($LASTEXITCODE -eq 0) { return $c } } catch {}
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host "Python needed to BUILD the exe (end users won't need it)." -ForegroundColor Yellow
    Write-Host "  winget install -e --id Python.Python.3.12   (or: uv venv in server\)"
    exit 1
}
Write-Host "Building with $(& $py --version)" -ForegroundColor Cyan

# Build-time deps: the app's runtime deps + PyInstaller. uv-created venvs ship
# without pip, so fall back to `uv pip` (targeting this interpreter) when pip is
# absent. The install is idempotent - already-present packages are skipped.
$pkgs = @("pyinstaller", "pynput", "cryptography", "pycaw", "comtypes",
          "zeroconf", "qrcode", "pillow", "pystray")
# uv-created venvs ship without pip, so fall back to `uv pip` (targeting this
# interpreter) when pip is absent. find_spec is silent + exits 0/1, so it's a clean
# probe; every install is gated on $LASTEXITCODE (stderr flows to the console).
& $py -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('pip') else 1)" *>$null
$hasPip = ($LASTEXITCODE -eq 0)
if ($hasPip) {
    & $py -m pip install --quiet --disable-pip-version-check @pkgs
    $ok = ($LASTEXITCODE -eq 0)
} elseif (Get-Command uv -ErrorAction SilentlyContinue) {
    & uv pip install --python $py @pkgs
    $ok = ($LASTEXITCODE -eq 0)
} else {
    Write-Host "No pip in the interpreter and uv not found - can't install build deps." -ForegroundColor Red
    exit 1
}
if (-not $ok) { Write-Host "dependency install failed." -ForegroundColor Red; exit 1 }

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
# Keep PyInstaller's iterative work/spec dirs OUT of the source tree: when the repo
# lives under OneDrive, sync locks those folders mid-build and the build dies with
# "Access is denied". The final exe still lands in dist/ (a single write).
$work = Join-Path $env:TEMP "lazeR-pyi-build"
$pyiArgs += @(
    "--distpath", (Join-Path $root "dist"),
    "--workpath", $work,
    "--specpath", $work,
    $script
)

# PyInstaller logs progress to stderr; with 'Continue' that just prints, and we
# judge success by the exit code (and the exe-exists check below).
& $py -m PyInstaller @pyiArgs
if ($LASTEXITCODE -ne 0) { Write-Host "PyInstaller build failed." -ForegroundColor Red; exit 1 }

$exe = Join-Path $root "dist\LazeR.exe"
if (Test-Path $exe) {
    Write-Host ""
    Write-Host "Built: $exe" -ForegroundColor Green
    Write-Host "Ship dist\LazeR.exe + LazeR.apk + START_HERE.md. No Python needed to run it." -ForegroundColor Cyan
} else {
    Write-Host "Build finished but LazeR.exe not found - check the output above." -ForegroundColor Yellow
}
