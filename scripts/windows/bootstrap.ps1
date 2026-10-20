# windows/bootstrap.ps1 -- the one paste on a new Windows PC, in PowerShell opened with "Run as administrator", that lets
# fcvm-arm set it up (scripts/windows-pc-setup.sh --bootstrap-port PORT). It is printed with its values filled in by
# scripts/windows-pc-bootstrap.sh; the @@...@@ placeholders below are those values. Safe to run again.
#   * OpenSSH Server (and the client, if it was removed), closed to the network before it first starts: its firewall
#     rule off, listening on 127.0.0.1 only, keys only; Windows PowerShell as its shell
#   * the setting-up account's key (fcvm-arm's ~/.ssh/to-<alias>.pub) for this administrator, in
#     C:\ProgramData\ssh\administrators_authorized_keys (the only file sshd reads for an administrator) and ~\.ssh
#   * a key of this PC's own and a logon task, 'devbox tunnel', that keeps a reverse SSH tunnel open to fcvm-arm
#     (127.0.0.1:PORT there -> this PC's 22), with fcvm-arm's host key pinned
#   * last, the PC's public key and the user name, on the clipboard: fcvm-arm authorizes that key for the tunnel only
#     (scripts/windows-pc-bootstrap.sh --allow)
# Once windows-pc-setup.sh has put the PC on its Cloudflare route, the tunnel is no longer used: remove it with
# Unregister-ScheduledTask 'devbox tunnel'.
& {
$ErrorActionPreference = 'Stop'

$DevBox     = '@@BOX@@'
$DevBoxHost = '@@BOX_HOST_KEY_LINE@@'
$ClaudeKey  = '@@CLIENT_KEY@@'
$Port       = @@PORT@@

function Step($m) { Write-Host "`n== $m" -ForegroundColor Cyan }

$me = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$me).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw 'Run this in PowerShell opened with "Run as administrator".'
}

Step 'OpenSSH server'
# Windows' own capability only, which installs into System32\OpenSSH, where everything below looks for it.
$help = 'install "OpenSSH Server" (and "OpenSSH Client") in Settings > System > Optional features, then run this again'
if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) {
  try { Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null } catch { throw "OpenSSH Server did not install ($($_.Exception.Message)): $help" }
}
if (-not (Test-Path "$env:WINDIR\System32\OpenSSH\sshd.exe")) { throw "OpenSSH Server is not Windows' own (System32\OpenSSH): $help" }
# The client (ssh, ssh-keygen) is its own capability: usually present, but it can have been removed.
if (-not (Test-Path "$env:WINDIR\System32\OpenSSH\ssh.exe")) {
  Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0 | Out-Null
  if (-not (Test-Path "$env:WINDIR\System32\OpenSSH\ssh.exe")) { throw "OpenSSH Client did not install: $help" }
}
# The install opened TCP 22 in the firewall: closed before sshd ever starts.
Get-NetFirewallRule -Name OpenSSH-Server-In-TCP -ErrorAction SilentlyContinue | Disable-NetFirewallRule
Set-Service sshd -StartupType Automatic
Start-Service sshd
# Sessions open in Windows PowerShell rather than cmd.
New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -PropertyType String -Force `
  -Value "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" | Out-Null
# Reached only through the reverse tunnel, which arrives on loopback: listen there only, keys only.
$cfg = "$env:ProgramData\ssh\sshd_config"
$keep = Get-Content $cfg | Where-Object { $_ -notmatch '^\s*(ListenAddress|PasswordAuthentication|KbdInteractiveAuthentication|ChallengeResponseAuthentication)\s' }
Copy-Item $cfg "$cfg.before-devbox" -Force
Set-Content -Path $cfg -Encoding ascii -Value (@('ListenAddress 127.0.0.1', 'PasswordAuthentication no', 'ChallengeResponseAuthentication no') + $keep)
& "$env:WINDIR\System32\OpenSSH\sshd.exe" -t
if ($LASTEXITCODE -ne 0) { Copy-Item "$cfg.before-devbox" $cfg -Force; throw 'sshd_config did not pass sshd -t; restored it' }
Restart-Service sshd -Force
Write-Host "sshd: $((Get-Service sshd).Status), on 127.0.0.1 only, keys only"

Step "Claude's key"
# An administrator's key is read only from the shared file, which must be Administrators and SYSTEM only.
$admins = "$env:ProgramData\ssh\administrators_authorized_keys"
New-Item -ItemType Directory -Force (Split-Path $admins) | Out-Null
if (-not ((Test-Path $admins) -and (Select-String -Path $admins -SimpleMatch $ClaudeKey -Quiet))) {
  Add-Content -Path $admins -Value $ClaudeKey -Encoding ascii
}
icacls $admins /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
$own = "$env:USERPROFILE\.ssh\authorized_keys"
New-Item -ItemType Directory -Force "$env:USERPROFILE\.ssh" | Out-Null
if (-not ((Test-Path $own) -and (Select-String -Path $own -SimpleMatch $ClaudeKey -Quiet))) {
  Add-Content -Path $own -Value $ClaudeKey -Encoding ascii
}
Write-Host 'added'

Step "This PC's key for the tunnel"
$dir = "$env:USERPROFILE\.devbox-tunnel"
New-Item -ItemType Directory -Force $dir | Out-Null
$key = "$dir\id_ed25519"
if (-not (Test-Path $key)) {
  # One command line string, so the empty passphrase reaches ssh-keygen in every PowerShell version.
  $kg = Start-Process -FilePath "$env:WINDIR\System32\OpenSSH\ssh-keygen.exe" -Wait -PassThru -NoNewWindow `
    -ArgumentList "-q -t ed25519 -N `"`" -C windows-pc-$env:COMPUTERNAME -f `"$key`""
  if ($kg.ExitCode -ne 0) { throw "ssh-keygen failed ($($kg.ExitCode))" }
}
if (-not (Test-Path "$key.pub")) { throw "ssh-keygen did not create $key" }
Set-Content -Path "$dir\known_hosts" -Value $DevBoxHost -Encoding ascii

Step 'Tunnel, kept up at every logon'
$tunnel = "$dir\tunnel.ps1"
@"
`$log = '$dir\tunnel.log'
while (`$true) {
  if ((Test-Path `$log) -and (Get-Item `$log).Length -gt 1MB) { Move-Item -Force `$log "`$log.1" }
  Add-Content `$log "`$(Get-Date -Format s) connecting"
  & "`$env:WINDIR\System32\OpenSSH\ssh.exe" -i '$key' -N -R 127.0.0.1:${Port}:127.0.0.1:22 ``
    -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 ``
    -o UserKnownHostsFile='$dir\known_hosts' -o StrictHostKeyChecking=yes $DevBox 2>> `$log
  Add-Content `$log "`$(Get-Date -Format s) exited `$LASTEXITCODE"
  Start-Sleep -Seconds 15
}
"@ | Set-Content -Path $tunnel -Encoding utf8

$name = 'devbox tunnel'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$tunnel`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $me.Name
$principal = New-ScheduledTaskPrincipal -UserId $me.Name -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
Start-ScheduledTask -TaskName $name
Write-Host "task '$name' started; log: $dir\tunnel.log"

Step 'Send this to Claude (it is on your clipboard)'
$line = "$((Get-Content "$key.pub" -Raw).Trim()) user=$env:USERNAME"
try { Set-Clipboard -Value $line } catch {}
Write-Host $line -ForegroundColor Yellow
Write-Host "`nThe tunnel connects by itself within a few seconds of Claude adding that key."
}
