# Quick developer launcher. Activates the .venv (if present), loads .env,
# and starts the poller + dashboard together.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (Test-Path ".venv\Scripts\Activate.ps1") {
    . ".venv\Scripts\Activate.ps1"
}

if (-not $env:MAXIMO_BASE_URL -and (Test-Path ".env")) {
    Get-Content .env | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z0-9_]+)\s*=\s*(.+?)\s*$') {
            [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
        }
    }
}

python -m duplicate_monitor both
