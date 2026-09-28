# uninstall_startup.ps1
#
# Remove the autostart entries created by install_startup.ps1.
# Archived video files are NOT touched.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File uninstall_startup.ps1

[CmdletBinding()]
param()

$ErrorActionPreference = "Continue"

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$Shortcut = "NVR-Puller.lnk"
$TaskName = "NVR-Puller"

Write-Host "============================================================"
Write-Host " NVR archiver - remove autostart"
Write-Host "============================================================"

$startupDir = [Environment]::GetFolderPath("Startup")
$lnkPath = Join-Path $startupDir $Shortcut
if (Test-Path -LiteralPath $lnkPath) {
    Remove-Item -LiteralPath $lnkPath -Force
    Write-Host "[OK] Removed startup shortcut: $lnkPath"
} else {
    Write-Host "[SKIP] No startup shortcut found."
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task -ne $null) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[OK] Unregistered scheduled task: $TaskName"
} else {
    Write-Host "[SKIP] No scheduled task found."
}

Write-Host ""
Write-Host " Note: any running python process must be stopped manually."
Write-Host "       Archived video files were left untouched."
Write-Host "============================================================"
