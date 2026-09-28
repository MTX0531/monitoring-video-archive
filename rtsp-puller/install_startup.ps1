# install_startup.ps1
#
# Register the NVR surveillance archiver so that it starts automatically.
# Two modes are supported:
#   (default)          -> create a shortcut in the user's Startup folder
#   -ScheduledTask     -> register a logon-triggered scheduled task (recommended)
#
# This file is kept ASCII-only on purpose: the project folder contains
# non-ASCII characters, and a script that scans its own location
# ($PSScriptRoot) must not embed any literal non-ASCII path.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File install_startup.ps1
#   powershell -ExecutionPolicy Bypass -File install_startup.ps1 -ScheduledTask
#   powershell -ExecutionPolicy Bypass -File install_startup.ps1 -PythonExe "C:\Python312\pythonw.exe"

[CmdletBinding()]
param(
    [string]$PythonExe = "",
    [switch]$ScheduledTask
)

$ErrorActionPreference = "Stop"

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Root       = $PSScriptRoot
$Script     = Join-Path $Root "nvr_puller.py"
$Shortcut   = "NVR-Puller.lnk"
$TaskName   = "NVR-Puller"

Write-Host "============================================================"
Write-Host " NVR archiver - install autostart"
Write-Host "============================================================"
Write-Host " Project dir : $Root"

if (-not (Test-Path -LiteralPath $Script)) {
    Write-Host "[ERROR] nvr_puller.py not found in $Root"
    exit 1
}

# ---------------------------------------------------------------- python
function Resolve-Pythonw {
    param([string]$Explicit)

    if ($Explicit -ne "") {
        if (Test-Path -LiteralPath $Explicit) { return (Resolve-Path -LiteralPath $Explicit).Path }
        Write-Host "[WARN] PythonExe not found: $Explicit"
    }

    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd -ne $null) { return $cmd.Source }

    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd -ne $null) {
        $cand = Join-Path (Split-Path $cmd.Source) "pythonw.exe"
        if (Test-Path -LiteralPath $cand) { return $cand }
        return $cmd.Source
    }
    return ""
}

$pythonw = Resolve-Pythonw -Explicit $PythonExe
if ($pythonw -eq "") {
    Write-Host "[ERROR] No python interpreter found."
    Write-Host "        Install Python 3.9+ and make sure it is in PATH,"
    Write-Host "        or re-run with -PythonExe <full path to pythonw.exe>."
    exit 1
}
Write-Host " Interpreter : $pythonw"

# ---------------------------------------------------------------- install
if ($ScheduledTask) {
    $action = New-ScheduledTaskAction -Execute $pythonw `
                                      -Argument ('"' + $Script + '"') `
                                      -WorkingDirectory $Root

    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                                             -DontStopIfGoingOnBatteries `
                                             -StartWhenAvailable `
                                             -RestartCount 3 `
                                             -RestartInterval (New-TimeSpan -Minutes 1) `
                                             -ExecutionTimeLimit ([TimeSpan]::Zero)

    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
                                            -LogonType Interactive `
                                            -RunLevel Limited

    Register-ScheduledTask -TaskName $TaskName `
                           -Action $action `
                           -Trigger $trigger `
                           -Settings $settings `
                           -Principal $principal `
                           -Description "Archive store surveillance video from the NVR over RTSP" `
                           -Force | Out-Null

    Write-Host "[OK] Scheduled task '$TaskName' registered (runs at logon)."
    Write-Host "     Start it now :  Start-ScheduledTask -TaskName '$TaskName'"
    Write-Host "     Check status :  Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
} else {
    $startupDir = [Environment]::GetFolderPath("Startup")
    $lnkPath = Join-Path $startupDir $Shortcut

    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($lnkPath)
    $sc.TargetPath       = $pythonw
    $sc.Arguments        = '"' + $Script + '"'
    $sc.WorkingDirectory = $Root
    $sc.WindowStyle      = 7
    $sc.Description      = "NVR surveillance video archiver"
    $sc.Save()

    Write-Host "[OK] Startup shortcut created:"
    Write-Host "     $lnkPath"
}

Write-Host ""
Write-Host " Done. The archiver will run automatically after the next logon."
Write-Host " To run it right now, simply double-click run_forever.bat"
Write-Host "============================================================"
