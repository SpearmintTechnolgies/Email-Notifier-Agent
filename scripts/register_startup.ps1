# Register/unregister Mailops Scheduled Tasks on Windows.
#
# Creates ONE scheduled task:
#   "Mailops Agent"  — runs start.ps1 at logon of desktop-t5sb54m\global.
#                     Preserves DPAPI CurrentUser.
#
# NOTE: The Cloudflare Tunnel runs as a separate LocalSystem Windows Service
# (installed by scripts\install_tunnel.ps1) — NOT a scheduled task. That is
# the correct split: the tunnel is user-independent, Mailops is per-user.
#
# Idempotent: safe to re-run. Reads/writes nothing outside Task Scheduler.
# Does NOT change .env, DPAPI state, or project files.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\register_startup.ps1 install
#   powershell -ExecutionPolicy Bypass -File scripts\register_startup.ps1 uninstall
#   powershell -ExecutionPolicy Bypass -File scripts\register_startup.ps1 status

param(
    [ValidateSet("install", "uninstall", "status")]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $here
$startScript = Join-Path $repoRoot "start.ps1"
$taskName = "Mailops Agent"
# Target the specific Windows account that owns the DPAPI master key.
# If you ever change the account, update this and re-run `install`.
$targetUser = "$env:USERDOMAIN\$env:USERNAME"

function Install-MailopsTask {
    if (-not (Test-Path $startScript)) {
        throw "start.ps1 not found at $startScript"
    }
    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startScript`"" `
        -WorkingDirectory $repoRoot

    # Trigger: at logon of the specific user. This preserves DPAPI CurrentUser
    # (which requires the user profile to be loaded) and avoids running
    # Mailops twice if a second account logs in to this machine.
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $targetUser
    # Add a 15s delay so the network/sync stack is up before Mailops starts
    # (reduces first-poll Telegram connection failures after a cold boot).
    $trigger.Delay = "PT15S"

    # Settings:
    #   - restart up to 3 times on failure (bounded; prevents log spam loops)
    #   - don't run when on battery? No — this is a hosting laptop, let it run.
    #   - allow start if on batteries, do not stop if going on batteries
    #   - execution time limit 0 (unbounded; it's a daemon)
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 2) `
        -MultipleInstances IgnoreNew

    # Principal: run as the interactive user (NOT elevated).
    # IMPORTANT: do NOT use `-RunLevel Highest`; Mailops must run with the
    # same token the user has on the console so DPAPI CurrentUser unseals.
    $principal = New-ScheduledTaskPrincipal `
        -UserId $targetUser `
        -LogonType Interactive `
        -RunLevel Limited

    $task = New-ScheduledTask `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "Mailops email agent (@ourmailagent_bot) + loopback onboarding server. Starts at logon of $targetUser; preserves DPAPI."

    # Register (overwrite). No credential prompt needed for interactive logon tasks.
    Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null
    Write-Host "Scheduled task '$taskName' installed (at logon of $targetUser)."
}

function Uninstall-MailopsTask {
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Host "No scheduled task named '$taskName' is registered."
        return
    }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Scheduled task '$taskName' removed."
}

function Show-Status {
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Host "Mailops scheduled task: NOT INSTALLED"
    } else {
        $info = Get-ScheduledTaskInfo -TaskName $taskName
        Write-Host "Mailops scheduled task:"
        $existing | Format-List TaskName, State, Author, Description
        Write-Host "Last run:    $($info.LastRunTime)   (result 0x$([Convert]::ToString($info.LastTaskResult, 16)))"
        Write-Host "Next run:    $($info.NextRunTime)"
        Write-Host "Num missed:  $($info.NumberOfMissedRuns)"
    }
    Write-Host "---"
    $svc = Get-Service cloudflared -ErrorAction SilentlyContinue
    if ($svc) {
        Write-Host "cloudflared service: $($svc.Status) ($($svc.StartType))"
    } else {
        Write-Host "cloudflared service: NOT INSTALLED"
    }
    Write-Host "---"
    $pidFile = Join-Path $repoRoot "data\mailops.pid"
    if (Test-Path $pidFile) {
        $maybe = (Get-Content $pidFile -Raw).Trim()
        if ($maybe) {
            try {
                $proc = Get-Process -Id ([int]$maybe) -ErrorAction Stop
                Write-Host "Mailops process:   RUNNING (pid $maybe, started $($proc.StartTime))"
            } catch {
                Write-Host "Mailops process:   STALE pid file (pid $maybe not running)"
            }
        }
    } else {
        Write-Host "Mailops process:   not started (no pid file)"
    }
}

switch ($Action) {
    "install"   { Install-MailopsTask;   Show-Status }
    "uninstall" { Uninstall-MailopsTask; Show-Status }
    "status"    { Show-Status }
}
