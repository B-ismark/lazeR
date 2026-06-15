<#
  Creates a clickable "LazeR" shortcut on your Desktop (and Start Menu) that
  launches the server window — no terminal, just the app.

  Run once: right-click > Run with PowerShell.
#>
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# Find pythonw (no console) — fall back to python.
function Find-Pythonw {
    foreach ($c in @("pythonw", "python")) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) {
            $p = $cmd.Source
            $pw = $p -replace 'python\.exe$', 'pythonw.exe'
            if (Test-Path $pw) { return $pw }
            return $p
        }
    }
    return $null
}

$pyw = Find-Pythonw
if (-not $pyw) {
    Write-Host "Python not found. Install Python 3.8+ first, then re-run." -ForegroundColor Yellow
    exit 1
}

$script = Join-Path $here "server\remote_server.py"
$icon   = Join-Path $here "server\LazeR.ico"
$workdir = Join-Path $here "server"

function New-LazeRShortcut($path) {
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($path)
    $sc.TargetPath = $pyw
    $sc.Arguments = "`"$script`""
    $sc.WorkingDirectory = $workdir
    if (Test-Path $icon) { $sc.IconLocation = $icon }
    $sc.Description = "LazeR — LAN remote control server"
    $sc.Save()
}

$desktop = [Environment]::GetFolderPath("Desktop")
New-LazeRShortcut (Join-Path $desktop "LazeR.lnk")
Write-Host "Created: $desktop\LazeR.lnk" -ForegroundColor Green

$startMenu = [Environment]::GetFolderPath("Programs")
New-LazeRShortcut (Join-Path $startMenu "LazeR.lnk")
Write-Host "Created: $startMenu\LazeR.lnk" -ForegroundColor Green

Write-Host "`nDone. Click the LazeR icon on your Desktop to start." -ForegroundColor Cyan
