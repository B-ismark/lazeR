<#
  Publish a MAJOR LazeR release to GitHub: build LazeR.exe + the hardened release
  APK, then create (or refresh) a GitHub Release with both assets attached.

  POLICY: only run this for MAJOR releases (headline features, protocol changes, an
  explicit version bump). Routine commits just get pushed — see CLAUDE.md. The
  script asks for confirmation unless you pass -Yes.

  Usage (from anywhere):
    powershell -ExecutionPolicy Bypass -File tools\publish_release.ps1 -Tag v1.2.0 `
        -Title "LazeR v1.2 — <headline>" [-Notes "..."] [-Yes]

  Prereqs: gh CLI authenticated (`gh auth login`), the uv venv at server\.venv.
#>
param(
    [Parameter(Mandatory = $true)][string]$Tag,
    [string]$Title = "",
    [string]$Notes = "",
    [switch]$Yes
)

# Native build tools log to stderr; 'Stop' + 2>&1 would turn that into terminating
# errors on PowerShell 5.1. Stay on 'Continue' and gate on $LASTEXITCODE / file checks.
$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here
$repo = "B-ismark/lazeR"

if (-not $Title) { $Title = "LazeR $Tag" }

# --- gate: major releases only ------------------------------------------------
if (-not $Yes) {
    Write-Host "Per policy, publish a GitHub Release ONLY for MAJOR milestones." -ForegroundColor Yellow
    $ans = Read-Host "Publish release '$Tag' now? (y/N)"
    if ($ans -notmatch '^[Yy]') { Write-Host "Aborted."; exit 0 }
}

# --- gh must be authenticated -------------------------------------------------
$gh = (Get-Command gh -ErrorAction SilentlyContinue).Source
if (-not $gh) { Write-Host "gh CLI not installed (winget install GitHub.cli)." -ForegroundColor Red; exit 1 }
& gh auth status *>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "gh is not authenticated. Run:  gh auth login" -ForegroundColor Red
    exit 1
}

# --- pause OneDrive (it locks build/ mid-compile under this repo) --------------
$odWasRunning = [bool](Get-Process OneDrive -ErrorAction SilentlyContinue)
Get-Process OneDrive -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
# free any running server/build that holds the exe or build dirs
Get-Process LazeR, java -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Remove-Item "$root\build" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "$root\android\app\build\kotlin", "$root\android\app\build\tmp" -Recurse -Force -ErrorAction SilentlyContinue

function Resume-OneDrive {
    if ($odWasRunning) {
        $od = "C:\Program Files\Microsoft OneDrive\OneDrive.exe"
        if (-not (Test-Path $od)) { $od = "$env:LOCALAPPDATA\Microsoft\OneDrive\OneDrive.exe" }
        if (Test-Path $od) { Start-Process $od }
    }
}

# --- build the Windows server exe ---------------------------------------------
Write-Host "Building LazeR.exe ..." -ForegroundColor Cyan
& powershell -ExecutionPolicy Bypass -File "$here\build_exe.ps1"
$exe = Join-Path $root "dist\LazeR.exe"
if (-not (Test-Path $exe)) { Write-Host "exe build failed." -ForegroundColor Red; Resume-OneDrive; exit 1 }

# --- build the hardened release APK -------------------------------------------
Write-Host "Building release APK ..." -ForegroundColor Cyan
$env:JAVA_HOME = [Environment]::GetEnvironmentVariable("JAVA_HOME", "Machine")
Push-Location "$root\android"
& "$root\android\gradlew.bat" assembleRelease --no-daemon
$apkCode = $LASTEXITCODE
Pop-Location
$apk = Join-Path $root "android\app\build\outputs\apk\release\app-release.apk"
if ($apkCode -ne 0 -or -not (Test-Path $apk)) { Write-Host "APK build failed." -ForegroundColor Red; Resume-OneDrive; exit 1 }
Copy-Item $apk (Join-Path $root "dist\LazeR.apk") -Force
$apkOut = Join-Path $root "dist\LazeR.apk"

Resume-OneDrive

# --- create or refresh the release --------------------------------------------
& gh release view $Tag --repo $repo *>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Release $Tag exists — uploading assets (--clobber)." -ForegroundColor Cyan
    & gh release upload $Tag $exe $apkOut --repo $repo --clobber
} else {
    Write-Host "Creating release $Tag." -ForegroundColor Cyan
    if ($Notes) {
        & gh release create $Tag $exe $apkOut --repo $repo --title $Title --notes $Notes
    } else {
        & gh release create $Tag $exe $apkOut --repo $repo --title $Title --generate-notes
    }
}
if ($LASTEXITCODE -ne 0) { Write-Host "Release publish failed." -ForegroundColor Red; exit 1 }
Write-Host "Published $Tag with LazeR.exe + LazeR.apk." -ForegroundColor Green
& gh release view $Tag --repo $repo --web *>$null
