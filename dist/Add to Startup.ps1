<#
  Make LazeR launch automatically when Windows starts (current user).

  Drops a shortcut in your Startup folder pointing at the server. Uses the
  bundled LazeR.exe if present, otherwise pythonw + the script (no console).

  Run once: right-click > Run with PowerShell.
  Remove it later:  powershell -ExecutionPolicy Bypass -File "Add to Startup.ps1" -Remove
#>
param([switch]$Remove)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$startup = [Environment]::GetFolderPath("Startup")
$lnk = Join-Path $startup "LazeR.lnk"

if ($Remove) {
    if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "Removed LazeR from startup." -ForegroundColor Green }
    else { Write-Host "LazeR was not in startup." -ForegroundColor DarkGray }
    return
}

# Prefer a bundled exe (no Python needed); fall back to pythonw + script.
$exe = Join-Path $here "LazeR.exe"
if (Test-Path $exe) {
    $target = $exe; $argline = ""; $workdir = $here
    $icon = $exe
} else {
    $py = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
    if (-not $py) {
        $p = (Get-Command python -ErrorAction SilentlyContinue).Source
        if ($p) { $pw = $p -replace 'python\.exe$', 'pythonw.exe'; if (Test-Path $pw) { $py = $pw } else { $py = $p } }
    }
    if (-not $py) { Write-Host "Python not found and no LazeR.exe. Install Python or build the exe first." -ForegroundColor Yellow; exit 1 }
    $script = Join-Path $here "server\remote_server.py"
    $target = $py; $argline = "`"$script`""; $workdir = Join-Path $here "server"
    $icon = Join-Path $here "server\LazeR.ico"
}

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath = $target
$sc.Arguments = $argline
$sc.WorkingDirectory = $workdir
if (Test-Path $icon) { $sc.IconLocation = $icon }
$sc.Description = "LazeR - LAN remote control server"
$sc.Save()

Write-Host "LazeR will now start with Windows." -ForegroundColor Green
Write-Host "Shortcut: $lnk" -ForegroundColor DarkGray
Write-Host 'Remove with:  powershell -ExecutionPolicy Bypass -File "Add to Startup.ps1" -Remove' -ForegroundColor DarkGray
