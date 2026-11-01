# One-stop operations script for Mailops + Cloudflare Tunnel.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\mailops_ops.ps1 status
#   powershell -ExecutionPolicy Bypass -File scripts\mailops_ops.ps1 restart       # Mailops only
#   powershell -ExecutionPolicy Bypass -File scripts\mailops_ops.ps1 restart-tunnel
#   powershell -ExecutionPolicy Bypass -File scripts\mailops_ops.ps1 healthz       # curl the public endpoint
#   powershell -ExecutionPolicy Bypass -File scripts\mailops_ops.ps1 tail          # tail Mailops log
#
# This script never touches .env, DPAPI, DNS, BotFather, or production
# mailboxes. It is purely a convenience over existing OS primitives.

param(
    [ValidateSet("status", "restart", "restart-tunnel", "healthz", "tail", "stop")]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $here
$pidFile = Join-Path $repoRoot "data\mailops.pid"
$appLog  = Join-Path $repoRoot "data\mailops.log"
$HOSTNAME = "mailops.altcoininformer.com"

function Mailops-Status {
    Write-Host "=== Mailops ==="
    if (Test-Path $pidFile) {
        $id = ((Get-Content $pidFile -Raw).Trim())
        try {
            $p = Get-Process -Id ([int]$id) -ErrorAction Stop
            $uptime = (Get-Date) - $p.StartTime
            Write-Host "  state:   RUNNING (pid $id)"
            Write-Host "  started: $($p.StartTime)  (up $($uptime.ToString('d\.hh\:mm\:ss')))"
        } catch {
            Write-Host "  state:   STALE pid file (pid $id)"
        }
    } else {
        Write-Host "  state:   NOT RUNNING"
    }
    Write-Host "  log:     $appLog"

    Write-Host "=== Cloudflare Tunnel ==="
    $svc = Get-Service cloudflared -ErrorAction SilentlyContinue
    if ($svc) {
        Write-Host "  service: $($svc.Status)  ($($svc.StartType))"
    } else {
        Write-Host "  service: NOT INSTALLED"
    }

    Write-Host "=== Scheduled Task ==="
    $t = Get-ScheduledTask -TaskName "Mailops Agent" -ErrorAction SilentlyContinue
    if ($t) {
        $info = Get-ScheduledTaskInfo -TaskName "Mailops Agent"
        Write-Host "  task:    $($t.State)   next=$($info.NextRunTime)   lastResult=0x$([Convert]::ToString($info.LastTaskResult,16))"
    } else {
        Write-Host "  task:    NOT INSTALLED"
    }
}

function Stop-Mailops {
    if (-not (Test-Path $pidFile)) { Write-Host "No pid file; nothing to stop."; return }
    $id = ((Get-Content $pidFile -Raw).Trim())
    try {
        Stop-Process -Id ([int]$id) -Force -ErrorAction Stop
        Write-Host "Stopped Mailops (pid $id)."
    } catch {
        Write-Host "Could not stop pid ${id}: $_"
    }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
}

function Restart-Mailops {
    Stop-Mailops
    Start-Sleep -Seconds 2
    & (Join-Path $repoRoot "start.ps1")
}

function Restart-Tunnel {
    $svc = Get-Service cloudflared -ErrorAction SilentlyContinue
    if (-not $svc) { throw "cloudflared service not installed." }
    Restart-Service cloudflared -Force
    Start-Sleep -Seconds 3
    Get-Service cloudflared | Format-List Name, Status, StartType
}

function Test-Healthz {
    Write-Host "GET https://$HOSTNAME/healthz"
    try {
        $r = Invoke-WebRequest -Uri "https://$HOSTNAME/healthz" -TimeoutSec 15 -UseBasicParsing
        Write-Host "  status: $($r.StatusCode)  body: $($r.Content)"
    } catch {
        Write-Host "  FAILED: $_"
    }
    Write-Host "GET https://127.0.0.1:8738/healthz (loopback, self-signed is expected)"
    try {
        $r = Invoke-WebRequest -Uri "https://127.0.0.1:8738/healthz" -TimeoutSec 10 -UseBasicParsing -SkipCertificateCheck
        Write-Host "  status: $($r.StatusCode)  body: $($r.Content)"
    } catch {
        Write-Host "  FAILED: $_"
    }
}

function Tail-Log {
    if (-not (Test-Path $appLog)) { Write-Host "No log yet: $appLog"; return }
    Get-Content $appLog -Tail 80 -Wait
}

switch ($Action) {
    "status"         { Mailops-Status }
    "restart"        { Restart-Mailops }
    "restart-tunnel" { Restart-Tunnel }
    "healthz"        { Test-Healthz }
    "tail"           { Tail-Log }
    "stop"           { Stop-Mailops }
}
