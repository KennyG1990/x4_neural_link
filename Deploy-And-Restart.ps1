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
  # 2026-07-02 crash post-mortem: 500ms was not enough — the new python raced the old port teardown
  # (WinError 10048) and DIED, leaving the bridge down. Wait until the port is actually free (up to 8s).
  for ($w = 0; $w -lt 16; $w++) {
    $still = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $still) { break }
    Start-Sleep -Milliseconds 500
  }
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
  # 2026-07-02: up to 2 start attempts — if health never appears (startup crash), try once more before
  # giving up, and say so loudly either way.
  $started = $false
  foreach ($try in 1, 2) {
    Start-Process -FilePath "python" -ArgumentList "-m", "bridge.server" -WorkingDirectory $Root -WindowStyle Hidden
    Start-Sleep -Seconds 2
    for ($h = 0; $h -lt 12; $h++) {
      try { Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2 | Out-Null; $started = $true; break }
      catch { Start-Sleep -Seconds 1 }
    }
    if ($started) { break }
    Write-Host ("  start attempt {0} FAILED - retrying..." -f $try) -ForegroundColor Yellow
    Stop-Bridge
  }
  if (-not $started) {
    Write-Host "  BRIDGE DOWN - both start attempts failed. Manual attention needed." -ForegroundColor Red
    return
  }
  for ($i = 0; $i -lt 1; $i++) {
    try {
      Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2 | Out-Null
      Write-Host ("  reloaded - BRIDGE UP @ {0}" -f (Get-Date -Format HH:mm:ss)) -ForegroundColor Green
      # ---- CI GATE (workflow v2, 2026-07-01): fast selftest smoke on EVERY reload; RED = the change is NOT done.
      # Fast deterministic suites only (no LLM, temp stores). Result also appended to runtime\logs\ci_gate.log so
      # agents can verify the gate without the transcript.
      $gate = @("actions_selftest", "relation_move_validator_selftest", "decision_record_selftest",
                "job_escalation_selftest", "route_decision_selftest")
      $fails = @()
      foreach ($g in $gate) {
        try {
          $r = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/ops/$g" -TimeoutSec 25
          if (-not $r.ok) { $fails += ("{0} ({1}/{2})" -f $g, $r.passed, $r.total) }
        } catch { $fails += "$g (unreachable)" }
      }
      $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
      $logDir = Join-Path $Root "runtime\logs"
      if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
      if ($fails.Count -eq 0) {
        Write-Host ("  CI GATE PASS ({0} suites)" -f $gate.Count) -ForegroundColor Green
        "PASS $stamp [$($gate.Count) suites]" | Out-File -Append -Encoding utf8 (Join-Path $logDir "ci_gate.log")
      } else {
        Write-Host ("  CI GATE **RED**: {0}" -f ($fails -join "; ")) -ForegroundColor Red
        Write-Host "  The change is NOT done until this gate is green (workflow v2)." -ForegroundColor Red
        "RED  $stamp $($fails -join '; ')" | Out-File -Append -Encoding utf8 (Join-Path $logDir "ci_gate.log")
      }
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
    # 2026-07-02: STABLE-SIGNATURE debounce — agents edit several files seconds apart; reload only once the
    # tree has been quiet for 2 consecutive checks (was a fixed 400ms, which raced multi-file edit bursts).
    $stable = $new
    for ($d = 0; $d -lt 20; $d++) {
      Start-Sleep -Milliseconds 700
      $probe = Get-Sig
      if ($probe -eq $stable) { break }
      $stable = $probe
    }
    Invoke-Reload "change detected"
    $sig = Get-Sig
  }
}
