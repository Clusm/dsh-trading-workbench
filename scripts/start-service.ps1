param(
  [int]$Port = 8791,
  [string]$DataDir = '',
  [string]$HostBaseUrl = '',
  [switch]$Background,
  [switch]$SkipVerify
)

# F1 (5.1 / 8-F): one start entry point that states the absolute data directory
# and the port up front, and verifies the resulting Host + Service identity.
# It stays a thin wrapper over run_service.py - no deployment platform.
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$workspace = Split-Path -Parent $root

# Absolute paths only: the service and the Host must agree on ONE data directory.
# data-dir owns credentials + venv so a packaged install (inside node_modules)
# keeps them across upgrades; the reference project is only a fallback.
if ([string]::IsNullOrWhiteSpace($DataDir)) { $DataDir = Join-Path $root 'data' }
$serviceDir = [System.IO.Path]::GetFullPath($DataDir)
if (-not (Test-Path -LiteralPath $serviceDir)) { New-Item -ItemType Directory -Path $serviceDir | Out-Null }
$serviceDir = (Resolve-Path -LiteralPath $serviceDir).Path
$serviceUrl = "http://127.0.0.1:$Port"

# python: explicit env -> data-dir venv -> package venv -> reference venv -> PATH.
$pythonCandidates = @(
  (Join-Path $serviceDir 'service-venv\Scripts\python.exe'),
  (Join-Path $root '.venv\Scripts\python.exe'),
  (Join-Path $workspace 'multi-agent-trader\.venv\Scripts\python.exe')
)
$python = $env:WORKBENCH_SERVICE_PYTHON
if ([string]::IsNullOrWhiteSpace($python)) {
  $python = ($pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1)
}
if ([string]::IsNullOrWhiteSpace($python)) {
  $onPath = Get-Command python -ErrorAction SilentlyContinue
  if ($onPath) { $python = $onPath.Source }
}
if ([string]::IsNullOrWhiteSpace($python)) {
  throw "no Python interpreter found. Set WORKBENCH_SERVICE_PYTHON or create $serviceDir\service-venv. Looked at: $($pythonCandidates -join ', ')"
}

# dotenv: explicit env -> data-dir demo.env -> reference project .env.
# credentials.py only reads the Demo key/secret from this file; nothing else is injected.
$dotenvCandidates = @(
  (Join-Path $serviceDir 'credentials\demo.env'),
  (Join-Path $workspace 'multi-agent-trader\.env')
)
$dotenv = $env:WORKBENCH_DOTENV_FILE
if ([string]::IsNullOrWhiteSpace($dotenv)) {
  $dotenv = ($dotenvCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1)
}
if (-not [string]::IsNullOrWhiteSpace($dotenv)) {
  $env:WORKBENCH_DOTENV_FILE = (Resolve-Path -LiteralPath $dotenv).Path
} else {
  Write-Warning "no demo credential file yet: the service will run read-only. Enter the Demo key in Settings -> Connection & account."
}

Write-Host "start-service: python   $python"
Write-Host "start-service: data-dir $serviceDir"
Write-Host "start-service: port     $Port ($serviceUrl)"

# Source release identity: the manifest is what the RUNNING Host reports back.
$releaseOut = & node (Join-Path $PSScriptRoot 'build-release.mjs') 2>&1
$releaseLine = ($releaseOut | Select-String -Pattern '^release_id') | Select-Object -First 1
if ($releaseLine) { Write-Host "start-service: $releaseLine" }

$runScript = Join-Path $root 'service\run_service.py'

if ($Background) {
  $logDir = Join-Path $serviceDir 'logs'
  if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
  $stdout = Join-Path $logDir 'service.out.log'
  $stderr = Join-Path $logDir 'service.err.log'
  # PowerShell 5.1 的 Start-Process 在枚举环境块时，遇到 NO_PROXY / no_proxy 这种
  # 仅大小写不同的重复键会直接抛 ArgumentException（本机真实发生过：代理软件会同时
  # 写入大小写两份）。此时退回 cmd start —— 它不枚举环境，能带着原样环境块启动。
  $started = $false
  try {
    $process = Start-Process -FilePath $python -ArgumentList @($runScript, '--port', "$Port", '--data-dir', $serviceDir) -WorkingDirectory (Join-Path $root 'service') -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    Write-Host "start-service: pid      $($process.Id) (logs: $logDir)"
    $started = $true
  } catch [System.ArgumentException] {
    Write-Warning "Start-Process refused the duplicated proxy env keys; falling back to cmd start"
  }
  if (-not $started) {
    $cmdLine = 'set "WORKBENCH_DOTENV_FILE=' + $env:WORKBENCH_DOTENV_FILE + '" && start "" /b "' + $python + '" "' + $runScript + '" --port ' + $Port + ' --data-dir "' + $serviceDir + '" >> "' + $stdout + '" 2>> "' + $stderr + '"'
    cmd.exe /c $cmdLine | Out-Null
    Write-Host "start-service: started via cmd fallback (logs: $logDir)"
  }

  $deadline = (Get-Date).AddSeconds(60)
  $ready = $false
  while ((Get-Date) -lt $deadline) {
    try {
      $health = Invoke-RestMethod -Uri "$serviceUrl/health" -TimeoutSec 3
      if ($health.ok -eq $true) { $ready = $true; break }
    } catch { Start-Sleep -Milliseconds 750 }
  }
  if (-not $ready) {
    Write-Warning "service did not answer /health within 60s; check $stderr"
    exit 1
  }
  Write-Host "start-service: service  ready=$($health.ready) venue=$($health.venue) ledger=$($health.db.ledger_id) $($health.service_version)"
} else {
  Write-Host 'start-service: foreground (Ctrl+C stops it). Verify in another shell with:'
  Write-Host "  node scripts/verify-runtime.mjs --service $serviceUrl --data-dir \"$serviceDir\""
}

if ($SkipVerify) { exit 0 }
if (-not $Background) {
  Write-Host 'start-service: identity verification needs a running service; use -Background to verify from here.'
  exit 0
}
if ([string]::IsNullOrWhiteSpace($HostBaseUrl)) { $HostBaseUrl = $env:WORKBENCH_HOST_BASE_URL }
if ([string]::IsNullOrWhiteSpace($HostBaseUrl)) {
  Write-Warning 'no -HostBaseUrl / WORKBENCH_HOST_BASE_URL: the Host half cannot be verified from here.'
  Write-Warning "  find the DSH web URL (e.g. http://127.0.0.1:58707) and run:"
  Write-Warning "  node scripts/verify-runtime.mjs --host <url> --service $serviceUrl --data-dir \"$serviceDir\""
  exit 0
}
& node (Join-Path $PSScriptRoot 'verify-runtime.mjs') --host $HostBaseUrl --service $serviceUrl --data-dir $serviceDir
exit $LASTEXITCODE
