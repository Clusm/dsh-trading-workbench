param(
  [string]$DataDir = '',
  [switch]$Force
)

# Stops the local workbench-service process. It does NOT close positions, does
# not cancel exchange protection orders and does not change the service control
# state: stopping automation is the top-bar action in the workbench UI
# (关调度 + POST /api/control pause), which is the persisted stop. Use this
# script only to take the service process down for maintenance.
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($DataDir)) { $DataDir = Join-Path $root 'data' }
$serviceDir = (Resolve-Path -LiteralPath ([System.IO.Path]::GetFullPath($DataDir))).Path
$lockPath = Join-Path $serviceDir 'service.lock'

if (-not (Test-Path -LiteralPath $lockPath)) {
  Write-Host "stop-workbench: no service.lock in $serviceDir (service already stopped?)"
  exit 0
}

$lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
$pidValue = [int]$lock.pid
$process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
if (-not $process) {
  Write-Host "stop-workbench: pid $pidValue from service.lock is not running"
  exit 0
}

Write-Host "stop-workbench: pid $pidValue ($($process.ProcessName)) data-dir $serviceDir"
Write-Host 'stop-workbench: the service control state is unchanged; open the workbench and press 停止自动交易 to stop automation.'

if (-not $Force) {
  try { Stop-Process -Id $pidValue -ErrorAction Stop } catch { throw }
} else {
  Stop-Process -Id $pidValue -Force -ErrorAction Stop
}
Write-Host 'stop-workbench: service process stopped. Restart with scripts/start-service.ps1 -Background'
