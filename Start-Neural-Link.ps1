$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

$existing = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8713 -State Listen -ErrorAction SilentlyContinue
if ($existing) {
  Write-Host "Neural Link already appears to be listening on 127.0.0.1:8713."
  try {
    Invoke-RestMethod -Uri "http://127.0.0.1:8713/health" -TimeoutSec 3 | ConvertTo-Json -Depth 8
  } catch {
    Write-Host "Port is occupied, but health check failed: $($_.Exception.Message)"
  }
  exit 0
}

Write-Host "Starting X4 Neural Link bridge from $Root"
python -m bridge.server
