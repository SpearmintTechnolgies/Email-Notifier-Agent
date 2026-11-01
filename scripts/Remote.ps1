# Run a bash script on dm-mini safely (base64-encoded, so no quoting/escaping
# issues through PowerShell -> ssh -> bash). Returns stdout+stderr as text.
#
#   . .\scripts\Remote.ps1
#   Invoke-Remote -Script "ls -la /opt/mailops"
#   Invoke-Remote -ScriptFile .\build\something.sh

$env:Path = "C:\Program Files\Amazon\AWSCLIV2;C:\Users\Global\AppData\Local\SessionManagerPlugin\bin;" + $env:Path

function Invoke-Remote {
    param(
        [string]$Script,
        [string]$ScriptFile,
        [string]$Target = "dm-mini",
        [switch]$Sudo
    )
    if ($ScriptFile) { $Script = Get-Content $ScriptFile -Raw }
    if (-not $Script) { throw "need -Script or -ScriptFile" }

    # Normalise to LF so bash never sees CR
    $Script = $Script -replace "`r`n", "`n" -replace "`r", "`n"
    $bytes = [Text.Encoding]::UTF8.GetBytes($Script)
    $b64 = [Convert]::ToBase64String($bytes)

    $runner = if ($Sudo) { "sudo bash" } else { "bash" }
    # Single remote word: no spaces/parens/quotes reach the remote shell except ours.
    $remote = "echo $b64 | base64 -d | $runner"
    ssh -o BatchMode=yes -o ConnectTimeout=60 $Target $remote 2>&1 | Out-String
}
