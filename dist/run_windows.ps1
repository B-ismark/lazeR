<#
  LazeR — laptop server one-shot setup + run (Windows).

  Does everything the server side needs:
    1. Finds Python (or tells you how to get it).
    2. Installs the Python dependencies (pynput + pycaw + comtypes).
    3. Opens the firewall for UDP 50505 (asks for admin once).
    4. Optionally installs the phone app over USB if adb + phone are present.
    5. Starts the server and prints the IP + token.

  Usage (right-click > Run with PowerShell, or):
    powershell -ExecutionPolicy Bypass -File run_windows.ps1
    powershell -ExecutionPolicy Bypass -File run_windows.ps1 -InstallApk   # also push APK via adb
#>
param([switch]$InstallApk)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

function Find-Python {
    foreach ($c in @("python", "py")) {
        try { $v = & $c --version 2>$null; if ($?) { return $c } } catch {}
    }
    return $null
}

Write-Host "=== LazeR server setup ===" -ForegroundColor Cyan

# 1. Python
$py = Find-Python
if (-not $py) {
    Write-Host "Python not found. Install it first:" -ForegroundColor Yellow
    Write-Host "  winget install -e --id Python.Python.3.12"
    Write-Host "  (or download from https://www.python.org/downloads/ and tick 'Add to PATH')"
    exit 1
}
Write-Host "Python: $(& $py --version)" -ForegroundColor Green

# 2. Dependencies
Write-Host "Installing dependencies..." -ForegroundColor Cyan
& $py -m pip install --user --quiet --disable-pip-version-check pynput cryptography pycaw comtypes zeroconf qrcode pillow pystray
Write-Host "Dependencies ready." -ForegroundColor Green

# 3. Firewall (UDP 50505) — needs admin, self-elevates just this step.
# Profile = Private ONLY (home/work networks). We deliberately do NOT open the
# port on Public networks (open/cafe Wi-Fi) — LazeR is unsafe to expose there.
$ruleName = "LazeR UDP 50505"
$have = $false
try { if (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue) { $have = $true } } catch {}
if (-not $have) {
    Write-Host "Opening firewall for UDP 50505 on PRIVATE networks (accept the admin prompt)..." -ForegroundColor Cyan
    $fw = "New-NetFirewallRule -DisplayName '$ruleName' -Direction Inbound -Protocol UDP -LocalPort 50505 -Action Allow -Profile Private | Out-Null"
    try {
        Start-Process powershell.exe -Verb RunAs -ArgumentList "-NoProfile", "-Command", $fw -Wait
        Write-Host "Firewall rule added." -ForegroundColor Green
    } catch {
        Write-Host "Could not add firewall rule (declined). If the phone can't connect," -ForegroundColor Yellow
        Write-Host "allow Python through Windows Firewall, or re-run and accept the prompt." -ForegroundColor Yellow
    }
} else {
    Write-Host "Firewall rule already present." -ForegroundColor Green
}

# 4. Optional: push the APK to a USB-connected phone
$apk = Join-Path $here "LazeR.apk"
if ($InstallApk) {
    $adb = (Get-Command adb -ErrorAction SilentlyContinue).Source
    if ($adb -and (Test-Path $apk)) {
        Write-Host "Installing LazeR.apk on the connected phone..." -ForegroundColor Cyan
        & $adb install -r $apk
    } else {
        Write-Host "adb or LazeR.apk not found — copy LazeR.apk to your phone and tap to install." -ForegroundColor Yellow
    }
} else {
    Write-Host "Phone app: copy LazeR.apk to your phone and tap to install (allow 'unknown apps')." -ForegroundColor Cyan
    Write-Host "  Or re-run with -InstallApk if your phone is on USB with debugging on." -ForegroundColor DarkGray
}

# 5. Desktop shortcut (clickable icon) — created once
$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "LazeR.lnk"
if (-not (Test-Path $lnk)) {
    try {
        $pyw = ($py -replace 'python\.exe$', 'pythonw.exe')
        if (-not (Test-Path $pyw)) { $pyw = (Get-Command $py).Source }
        $icon = Join-Path $here "server\LazeR.ico"
        $ws = New-Object -ComObject WScript.Shell
        $sc = $ws.CreateShortcut($lnk)
        $sc.TargetPath = $pyw
        $sc.Arguments = "`"$(Join-Path $here 'server\remote_server.py')`""
        $sc.WorkingDirectory = Join-Path $here "server"
        if (Test-Path $icon) { $sc.IconLocation = $icon }
        $sc.Description = "LazeR - LAN remote control server"
        $sc.Save()
        Write-Host "Created clickable LazeR icon on your Desktop." -ForegroundColor Green
    } catch {
        Write-Host "Could not create Desktop shortcut (run 'Create LazeR Shortcut.ps1' later)." -ForegroundColor DarkGray
    }
}

# 6. Run the server (a window opens; close it to stop)
Write-Host "`nStarting server...`n" -ForegroundColor Cyan
$env:PYTHONUNBUFFERED = "1"
& $py (Join-Path $here "server\remote_server.py")
