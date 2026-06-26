# Deploy-And-Restart.ps1  (run + watch — location-independent, no hard-coded paths)
# Run once and leave the window open. Compile-checks the bridge, runs it FROM THIS
# FOLDER, then watches bridge/ + config/ and restarts in place on every edit.
# A compile error keeps the previous bridge alive (fix + save again). Ctrl+C to stop.
# Works from any path: the bridge runs wherever this script lives.

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Port = 8713
$Watch = @((Join-Path $Root "bridge"), (Join-Path $Root "config"))
$Compile = @(
  "bridge\contracts.py", "bridge\telemetry.py", "bridge\memory.py", "bridge\scoring.py",
  "bridge\events.py", "bridge\player2_client.py", "bridge\router.py", "bridge\server.py",
  "bridge\retrieval.py", "bridge\catdat.py", "bridge\lore.py"
)

try { Start-Transcript -Path (Join-Path $Root "deploy.log") -Force | Out-Null } catch {}

function Get-Sig {
  $files = Get-ChildItem -Path $Watch -Recurse -File -Include *.py, *.json -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notmatch '__pycache__' }
  ($files | Sort-Object FullName | ForEach-Object { "$($_.FullName)=$($_.LastWriteTimeUtc.Ticks)" }) -join "|"
}

function Stop-Bridge {
  $conns = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
  foreach ($c in $conns) { try { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue } catch {} }
  Start-Sleep -Milliseconds 500
}

function Invoke-Reload([string]$why) {
  Set-Location -LiteralPath $Root
  Write-Host ("[{0}] {1} - compiling..." -f (Get-Date -Format HH:mm:ss), $why) -ForegroundColor Cyan
  python -m py_compile @Compile
  if ($LASTEXITCODE -ne 0) {
    Write-Host "  COMPILE FAILED - previous bridge left running. Fix and save again." -ForegroundColor Red
    return
  }
  Stop-Bridge
  # Run the bridge directly from this folder (no deploy/copy). __file__-relative root.
  Start-Process -FilePath "python" -ArgumentList "-m", "bridge.server" -WorkingDirectory $Root -WindowStyle Hidden
  Start-Sleep -Seconds 2
  for ($i = 0; $i -lt 12; $i++) {
    try {
      Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2 | Out-Null
      Write-Host ("  reloaded - BRIDGE UP @ {0}" -f (Get-Date -Format HH:mm:ss)) -ForegroundColor Green
      return
    } catch { Start-Sleep -Milliseconds 700 }
  }
  Write-Host "  WARNING: bridge did not answer /health after restart." -ForegroundColor Yellow
}

Clear-Host
Write-Host "== Neural Link - RUN + WATCH ==" -ForegroundColor Yellow
Write-Host "Runs from this folder; editing bridge/ or config/ auto-reloads. Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ("Root: {0}" -f $Root) -ForegroundColor DarkGray
Write-Host ""
Invoke-Reload "initial start"
$sig = Get-Sig
while ($true) {
  Start-Sleep -Seconds 1
  $new = Get-Sig
  if ($new -ne $sig) {
    Start-Sleep -Milliseconds 400   # debounce: let the editor finish writing
    Invoke-Reload "change detected"
    $sig = Get-Sig
  }
}
