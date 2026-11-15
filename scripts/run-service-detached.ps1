param([int]$Port = 8791, [string]$DataDir = "")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$workspace = Split-Path -Parent $root
$python = Join-Path $workspace "multi-agent-trader\.venv\Scripts\python.exe"
$dotenv = Join-Path $workspace "multi-agent-trader\.env"
if ([string]::IsNullOrWhiteSpace($DataDir)) { $DataDir = Join-Path $root "data" }
$env:WORKBENCH_DOTENV_FILE = (Resolve-Path -LiteralPath $dotenv).Path
$logDir = Join-Path $DataDir "logs"
if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$out = Join-Path $logDir "detached.out.log"
$err = Join-Path $logDir "detached.err.log"
$proc = Start-Process -FilePath $python -ArgumentList @((Join-Path $root "service\run_service.py"), "--port", "$Port", "--data-dir", (Resolve-Path -LiteralPath $DataDir).Path) -WorkingDirectory (Join-Path $root "service") -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
Write-Output ("detached pid=" + $proc.Id)
# 关键：不要 WaitForExit —— 让子进程成为独立进程，父 shell 退出不影响它。