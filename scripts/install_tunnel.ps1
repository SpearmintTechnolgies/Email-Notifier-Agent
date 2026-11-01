# Install the Mailops Cloudflare Tunnel service.
#
# Preconditions:
#   1. `cloudflared tunnel login` has already been run (so ~/.cloudflared/cert.pem exists).
#   2. The named tunnel 'mailops-web' has been created
#      (`cloudflared tunnel create mailops-web`).
#   3. The hostname has been routed
#      (`cloudflared tunnel route dns mailops-web mailops.altcoininformer.com`).
#
# What this script does (in order, idempotent):
#   1. Finds the tunnel UUID + credentials JSON under $env:USERPROFILE\.cloudflared.
#   2. Copies the credentials JSON to %ProgramData%\Cloudflare\cloudflared\ so the
#      LocalSystem-run Windows service can read it. Original is left in place.
#   3. Renders deploy\cloudflared-config.template.yml with the UUID and writes
#      it to %ProgramData%\Cloudflare\cloudflared\config.yml.
#   4. Runs `cloudflared service install` (idempotent — if the service already
#      exists, this script restarts it with the new config instead).
#   5. Prints the final service status.
#
# This script does NOT create the tunnel, call Cloudflare APIs, or touch DNS.
# All of that is done by the operator with `cloudflared tunnel ...` commands
# documented in docs\REMOTE_ONBOARDING.md.

#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"
$HOSTNAME = "mailops.altcoininformer.com"
$TUNNEL_NAME = "mailops-web"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $here
$template = Join-Path $repoRoot "deploy\cloudflared-config.template.yml"
$programData = Join-Path $env:ProgramData "Cloudflare\cloudflared"
$configPath = Join-Path $programData "config.yml"

if (-not (Test-Path $template)) {
    throw "template missing: $template"
}

# 1. Locate the tunnel UUID + credentials JSON.
$cfdir = Join-Path $env:USERPROFILE ".cloudflared"
if (-not (Test-Path (Join-Path $cfdir "cert.pem"))) {
    throw "cloudflared is not authenticated. Run `cloudflared tunnel login` first."
}
$listJson = cloudflared tunnel list --output json 2>$null
if (-not $listJson) {
    throw "`cloudflared tunnel list` returned nothing. Did you create a tunnel yet?"
}
$tunnels = $listJson | ConvertFrom-Json
$match = $tunnels | Where-Object { $_.name -eq $TUNNEL_NAME }
if (-not $match) {
    throw "named tunnel '$TUNNEL_NAME' not found. Run `cloudflared tunnel create $TUNNEL_NAME` first."
}
$uuid = $match.id
Write-Host "Found tunnel '$TUNNEL_NAME' with UUID $uuid"

$credsSource = Join-Path $cfdir ("{0}.json" -f $uuid)
if (-not (Test-Path $credsSource)) {
    throw "credentials file missing: $credsSource"
}

# 2. Copy credentials to ProgramData for LocalSystem.
New-Item -ItemType Directory -Force -Path $programData | Out-Null
$credsDest = Join-Path $programData ("{0}.json" -f $uuid)
Copy-Item -Force $credsSource $credsDest
# Lock down the credentials file: SYSTEM full, Administrators read, everyone else nothing.
icacls $credsDest /inheritance:r | Out-Null
icacls $credsDest /grant:r "SYSTEM:F" "Administrators:R" | Out-Null
Write-Host "Credentials installed at $credsDest (ACL: SYSTEM+Administrators only)"

# 3. Render the template and write the config.
$rendered = (Get-Content -Raw $template).Replace("__TUNNEL_UUID__", $uuid)
Set-Content -Path $configPath -Value $rendered -Encoding utf8
Write-Host "Config written to $configPath"

# 4. Install or reinstall the service (idempotent).
$svc = Get-Service cloudflared -ErrorAction SilentlyContinue
if ($svc) {
    Write-Host "cloudflared service exists; restarting with new config..."
    Stop-Service cloudflared -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Start-Service cloudflared
} else {
    Write-Host "Installing cloudflared as a Windows service..."
    cloudflared service install
    Start-Sleep -Seconds 2
    # On Windows, the installer configures the service to auto-start.
    Set-Service cloudflared -StartupType Automatic
    Start-Service cloudflared
}

# 5. Status.
Start-Sleep -Seconds 3
Write-Host "---"
Get-Service cloudflared | Format-List Name, Status, StartType, DisplayName
Write-Host "---"
Write-Host "Public hostname: https://$HOSTNAME"
Write-Host "Verify from any network: curl.exe https://$HOSTNAME/healthz"
