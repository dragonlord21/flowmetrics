# Snapshot the warehouse, retain the 14 newest archives, delete the
# rest. PowerShell wrapper for Windows Task Scheduler.
#
# Required env vars (set by the scheduler task):
#   FLOWMETRICS_HOME   install root (holds contracts and data folders)
#   FLOWMETRICS_VENV   venv folder to run flow.exe from

$ErrorActionPreference = "Stop"

$Home = $env:FLOWMETRICS_HOME
$Venv = $env:FLOWMETRICS_VENV
if (-not $Home -or -not $Venv) {
    throw "Set FLOWMETRICS_HOME and FLOWMETRICS_VENV."
}

Set-Location $Home

& "$Venv\Scripts\flow.exe" backup --data-dir data

Get-ChildItem "$Home\data\_backups\flowmetrics-*.tar.gz" `
    -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 14 |
    Remove-Item -Force
