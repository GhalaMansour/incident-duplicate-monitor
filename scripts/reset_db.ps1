# Reset the local SQLite database and the scan pickle. Use with care.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$dbPath = Join-Path $root "src\duplicate_monitor\monitor.db"
$pklPath = Join-Path $root "src\duplicate_monitor\live_scan.pkl"

foreach ($p in @($dbPath, $pklPath)) {
    if (Test-Path $p) {
        Remove-Item -Force $p
        Write-Host "removed: $p"
    }
}
