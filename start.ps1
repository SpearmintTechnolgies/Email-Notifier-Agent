# Mailops launcher (Windows) - starts the Telegram bot in the background
# using the project's own .venv. Safe to double-click via "Start Mailops.bat".
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# Prefer the project virtual environment; fall back to system python.
$py = Join-Path $here ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$dataDir = Join-Path $here "data"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$pidFile = Join-Path $dataDir "mailops.pid"
$appLog  = Join-Path $dataDir "mailops.log"
$outLog  = Join-Path $dataDir "bot.stdout.log"
$errLog  = Join-Path $dataDir "bot.stderr.log"

function Test-PidAlive([int]$ProcessId) {
  if ($ProcessId -le 0) { return $false }
  try {
    $null = Get-Process -Id $ProcessId -ErrorAction Stop
    return $true
  } catch {
    return $false
  }
}

function Show-Balloon([string]$Title, [string]$Text) {
  try {
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue | Out-Null
    Add-Type -AssemblyName System.Drawing -ErrorAction SilentlyContinue | Out-Null
    $ni = New-Object System.Windows.Forms.NotifyIcon
    $ni.Icon = [System.Drawing.SystemIcons]::Information
    $ni.Visible = $true
    $ni.BalloonTipTitle = $Title
    $ni.BalloonTipText = $Text
    $ni.ShowBalloonTip(4000)
    Start-Sleep -Seconds 1
    $ni.Dispose()
  } catch {}
}

if (Test-Path $pidFile) {
  $existing = 0
  try { $existing = [int]((Get-Content $pidFile -Raw).Trim()) } catch { $existing = 0 }
  if (Test-PidAlive $existing) {
    Write-Host "Mailops is already running (pid $existing). Open your Telegram bot."
    Show-Balloon "Mailops" "Already running - open your Telegram bot"
    exit 0
  }
}

if (-not (Test-Path (Join-Path $here ".env"))) {
  Write-Host "WARNING: .env missing. Copy .env.example to .env and fill it in."
}

Write-Host "Installing mailops deps (if needed)..."
& $py -m pip install -q -r "$here\requirements.txt"

Add-Content -Path $appLog -Value ("`n=== launcher restart {0} ===`n" -f (Get-Date -Format o)) -Encoding utf8

# Use cmd redirection so PowerShell does not treat log lines as errors.
$batRunner = Join-Path $dataDir "run-once.cmd"
@"
@echo off
set PYTHONPATH=$here
cd /d "$here"
"$py" -m mailops.main run > "$outLog" 2> "$errLog"
"@ | Set-Content -Path $batRunner -Encoding ASCII

Write-Host "Starting Mailops in background..."
$proc = Start-Process -FilePath $batRunner -WorkingDirectory $here -WindowStyle Hidden -PassThru

$ok = $false
for ($i = 0; $i -lt 45; $i++) {
  Start-Sleep -Seconds 1
  if (Test-Path $appLog) {
    $joined = ((Get-Content $appLog -Tail 60 -ErrorAction SilentlyContinue) -join "`n")
    if ($joined -match "Application started") { $ok = $true; break }
    if ($joined -match "mailops already running") { $ok = $true; break }
  }
  if (-not (Test-PidAlive $proc.Id) -and $i -gt 5) { break }
}

if ($ok) {
  Write-Host "Mailops started."
  Write-Host "Open your Telegram bot and send /start."
  Write-Host "Log: $appLog"
  Show-Balloon "Mailops" "Agent started - open your Telegram bot"
  Start-Sleep -Seconds 2
  exit 0
}

Write-Host "Mailops failed to start. Last log lines:"
Write-Host "--- mailops.log ---"
Get-Content $appLog -Tail 40 -ErrorAction SilentlyContinue
Write-Host "--- bot.stderr.log ---"
Get-Content $errLog -Tail 40 -ErrorAction SilentlyContinue
pause
exit 1
