#!/usr/bin/env bash
#
# claw-master-windows-setup -- put a Windows PC on the shared Claude subscriptions through the Cloudflare
# tunnel (claw-master-tunnel.tf). Run on fcvm-arm, which reaches the PC over an ssh alias: the PC keeps a
# reverse SSH tunnel open to fcvm-arm and runs OpenSSH Server with Windows PowerShell as its shell.
#
#   scripts/claw-master-windows-setup.sh windows-pc     on fcvm-arm (its role reads the PC's token)
#
# Unlike the Mac setup this is not opt-in per command: `claw-master agent install` points every Claude
# Code on the PC (CLI, desktop app, IDE extensions) at the pool, failing closed. Undo on the PC with
# `claw-master agent uninstall` and Unregister-ScheduledTask 'claw-master tunnel'.
#   * claw-master and cloudflared in %USERPROFILE%\.local\bin, each pinned by sha256 and verified ON the PC
#   * %USERPROFILE%\.config\claw-master\cloudflare.env (owner-only ACL, checked before it is filled): the
#     PC's own Access service token (claude-master/windows-pc-access), through ssh STDIN only
#   * %USERPROFILE%\.config\claw-master\ca.pem: the server's PUBLIC CA certificate (fcvm-arm's copy)
#   * the logon task 'claw-master tunnel': `cloudflared access tcp` on 127.0.0.1:8444, token read from
#     the file into its own environment, never its arguments
#   * a request through that tunnel to Anthropic must succeed before `claw-master agent install` runs
# Each PowerShell step travels as -EncodedCommand (no quoting through ssh) and data on stdin; a step
# that fails exits non-zero, which stops this script.
set -euo pipefail

REGION=${AWS_REGION:-us-west-1}
CA_FILE=${CLAW_MASTER_CA:-$HOME/.config/claude-master/ca.pem}

# Pins. Keep CM_TAG equal to claw_master_tag in claw-master-server.tf and CFD_VERSION to
# cloudflared_version in claw-master-tunnel.tf (tests enforce both); bump the sha256 values with them.
CM_TAG=claw-master-dc78270
CM_SHA256_WINDOWS_AMD64=8cae4162f7913d378f5e378d0cd0304d48e664fe8cade321f4dbab7012296ddc
CFD_VERSION=2026.9.3
CFD_SHA256_WINDOWS_AMD64=f096265ec2fcbe9bb6e2d64268db167ced3fcbb83d894bdb9e2fcdb26f2ea7e2
AGENT_LISTEN=127.0.0.1:8445

ALIAS=${1:-}
[[ $ALIAS =~ ^[a-z][a-z0-9-]{0,30}$ ]] || { echo "usage: claw-master-windows-setup.sh ALIAS   (the PC's ssh alias here)" >&2; exit 2; }

# Run a PowerShell script on the PC; stdin is passed through. Progress records would arrive as CLIXML.
on_pc() {
  local enc
  enc=$(printf '$ProgressPreference = %s\n%s' "'SilentlyContinue'" "$1" | iconv -f utf-8 -t utf-16le | base64 -w0)
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$ALIAS" "powershell -NoProfile -NonInteractive -EncodedCommand $enc" | tr -d '\r'
}
# PowerShell assignments for values the scripts below need ('' quoted; the values hold no quote).
ps_vars() { while [ $# -gt 0 ]; do printf "\$%s='%s'\n" "$1" "$2"; shift 2; done; }

echo "1/7 reaching $ALIAS"
grep -q 'BEGIN CERTIFICATE' "$CA_FILE" || { echo "no server CA certificate at $CA_FILE" >&2; exit 1; }
ARCH=$(on_pc '$env:PROCESSOR_ARCHITECTURE') || { echo "$ALIAS is not reachable: the PC must be on, with its devbox tunnel task running" >&2; exit 1; }
[ "$ARCH" = AMD64 ] || { echo "this script installs the Windows x64 builds; $ALIAS is $ARCH" >&2; exit 1; }

echo "2/7 installing claw-master $CM_TAG and cloudflared $CFD_VERSION (verified on the PC)"
on_pc "$(ps_vars Tag "$CM_TAG" CmSha "$CM_SHA256_WINDOWS_AMD64" CfdVersion "$CFD_VERSION" CfdSha "$CFD_SHA256_WINDOWS_AMD64")
$(cat <<'PS'
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = 'Tls12'
$bin = "$env:USERPROFILE\.local\bin"
New-Item -ItemType Directory -Force $bin | Out-Null
function Install-Pinned($url, $dest, $sha, $what) {
  # Judged by the hash of the installed executable, never by the version it reports.
  if ((Test-Path $dest) -and (Get-FileHash $dest).Hash -eq $sha) { return }
  Invoke-WebRequest -UseBasicParsing $url -OutFile "$dest.new"
  if ((Get-FileHash "$dest.new").Hash -ne $sha) { Remove-Item "$dest.new"; throw "$what did not match its pinned sha256" }
  Move-Item -Force "$dest.new" $dest
}
Install-Pinned "https://github.com/ejc3/CLIProxyAPI/releases/download/$Tag/claw-master-windows-amd64.exe" "$bin\claw-master.exe" $CmSha 'claw-master'
Install-Pinned "https://github.com/cloudflare/cloudflared/releases/download/$CfdVersion/cloudflared-windows-amd64.exe" "$bin\cloudflared.exe" $CfdSha 'cloudflared'
'claw-master ' + (Get-FileHash "$bin\claw-master.exe").Hash.Substring(0, 16) + ', cloudflared ' + (Get-FileHash "$bin\cloudflared.exe").Hash.Substring(0, 16)
PS
)"

echo "3/7 the PC's Access service token (from Secrets Manager, through ssh stdin only)"
{ set +x; } 2>/dev/null
SECRET=$(aws secretsmanager get-secret-value --region "$REGION" --secret-id claude-master/windows-pc-access --query SecretString --output text)
PORT=$(printf '%s' "$SECRET" | python3 -c 'import json,sys; print(json.load(sys.stdin)["local_port"])')
printf '%s' "$SECRET" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("TUNNEL_SERVICE_TOKEN_ID=" + d["client_id"])
print("TUNNEL_SERVICE_TOKEN_SECRET=" + d["client_secret"])
print("CLAUDE_MASTER_TUNNEL_HOSTNAME=" + d["hostname"])
print("CLAUDE_MASTER_TUNNEL_PORT=%s" % d["local_port"])' |
  on_pc "$(cat <<'PS'
$ErrorActionPreference = 'Stop'
$d = "$env:USERPROFILE\.config\claw-master"
New-Item -ItemType Directory -Force $d | Out-Null
$f = "$d\cloudflare.env"
# Created empty, made owner-only and CHECKED, and only then filled.
Set-Content -Path "$f.new" -Value '' -NoNewline
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = New-Object Security.AccessControl.FileSecurity
$acl.SetAccessRuleProtection($true, $false)
$acl.SetOwner($sid)
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'Allow')))
Set-Acl -Path "$f.new" -AclObject $acl
$rules = @((Get-Acl "$f.new").Access)
if ($rules.Count -ne 1 -or $rules[0].IdentityReference.Translate([Security.Principal.SecurityIdentifier]) -ne $sid) {
  Remove-Item "$f.new"; throw 'cloudflare.env could not be made owner-only; nothing written'
}
[IO.File]::WriteAllText("$f.new", [Console]::In.ReadToEnd())
Move-Item -Force "$f.new" $f
'installed cloudflare.env, readable by ' + $rules[0].IdentityReference.Value + ' only'
PS
)"
unset SECRET

echo "4/7 the server's public CA certificate"
on_pc "$(cat <<'PS'
$ErrorActionPreference = 'Stop'
[IO.File]::WriteAllText("$env:USERPROFILE\.config\claw-master\ca.pem", [Console]::In.ReadToEnd())
'installed ca.pem'
PS
)" < "$CA_FILE"

echo "5/7 the 'claw-master tunnel' logon task, and the tunnel up on 127.0.0.1:$PORT"
on_pc "$(ps_vars Port "$PORT")
$(cat <<'PS'
$ErrorActionPreference = 'Stop'
$d = "$env:USERPROFILE\.config\claw-master"
New-Item -ItemType Directory -Force "$env:LOCALAPPDATA\claw-master" | Out-Null
# Runs the local end of the Cloudflare tunnel, restarted if it exits. The token is read from the
# owner-only file into this process's environment, never its arguments. The log rotates at 5 MB.
@'
$d = "$env:USERPROFILE\.config\claw-master"
$log = "$env:LOCALAPPDATA\claw-master\tunnel.log"
while ($true) {
  if ((Test-Path $log) -and (Get-Item $log).Length -gt 5242880) { Move-Item -Force $log "$log.1" }
  foreach ($line in Get-Content "$d\cloudflare.env") {
    if ($line -match '^([A-Z_]+)=(.*)$') { [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process') }
  }
  & "$env:USERPROFILE\.local\bin\cloudflared.exe" --logfile $log access tcp --hostname $env:CLAUDE_MASTER_TUNNEL_HOSTNAME --url "127.0.0.1:$env:CLAUDE_MASTER_TUNNEL_PORT"
  Start-Sleep -Seconds 15
}
'@ | Set-Content -Path "$d\tunnel.ps1" -Encoding utf8
$me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$name = 'claw-master tunnel'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$d\tunnel.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $me
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name = 'cloudflared.exe'" | Where-Object { $_.CommandLine -like '*access tcp*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
# A logon trigger does not fire for the session already open: start it now.
Start-ScheduledTask -TaskName $name
for ($i = 0; $i -lt 30; $i++) {
  if (Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { "tunnel listening on 127.0.0.1:$Port"; exit 0 }
  Start-Sleep -Seconds 2
}
throw "the tunnel did not start listening on 127.0.0.1:$Port; see $env:LOCALAPPDATA\claw-master\tunnel.log"
PS
)"

# A listening port proves only that cloudflared started: it accepts before reaching anything. The pool
# must answer through it (the token valid, the connector and the server up) before the PC is switched over.
# The proxy answers CONNECT 200 before it reaches Anthropic and a failed upstream request as 5xx, so the
# HTTP status must be Anthropic's own: 2xx or 4xx.
echo "6/7 a request to Anthropic through the tunnel and the server, before switching the PC over"
on_pc "$(ps_vars Port "$PORT")
$(cat <<'PS'
$d = "$env:USERPROFILE\.config\claw-master"
for ($i = 0; $i -lt 10; $i++) {
  $out = & curl.exe -s -m 30 -o NUL -w '%{http_connect} %{http_code}' -x "http://127.0.0.1:$Port" --cacert "$d\ca.pem" https://api.anthropic.com/v1/models
  if ($LASTEXITCODE -eq 0 -and $out -match '^200 [24]\d\d$') { "through the pool: CONNECT and HTTP status $out"; exit 0 }
  Start-Sleep -Seconds 3
}
"the pool did not answer through the tunnel (curl exit $LASTEXITCODE, status $out); the PC was not switched over"
exit 1
PS
)"

echo "7/7 claw-master agent install (every Claude Code on the PC uses the pool from here on), and a check"
on_pc "$(ps_vars Port "$PORT" Listen "$AGENT_LISTEN")
$(cat <<'PS'
$d = "$env:USERPROFILE\.config\claw-master"
# Not 'Stop' here: in Windows PowerShell 5.1 a native program's redirected stderr would end the script.
& "$env:USERPROFILE\.local\bin\claw-master.exe" agent install --listen $Listen --open "127.0.0.1:$Port" --ca "$d\ca.pem" 2>&1 | ForEach-Object { "$_" }
if ($LASTEXITCODE -ne 0) { "agent install failed ($LASTEXITCODE)"; exit 1 }
# The proxy URL carries the agent's token: curl reads it from the environment, never its arguments,
# and it is never printed.
$env:HTTPS_PROXY = (Get-Content "$env:USERPROFILE\.claude\settings.json" -Raw | ConvertFrom-Json).env.HTTPS_PROXY
$out = & curl.exe -s -m 30 -o NUL -w '%{http_connect} %{http_code}' --cacert "$d\ca.pem" https://api.anthropic.com/v1/models
if ($LASTEXITCODE -ne 0 -or $out -notmatch '^200 [24]\d\d$') { "the agent did not reach the pool (curl exit $LASTEXITCODE, status $out)"; exit 1 }
"through the agent: CONNECT and HTTP status $out"
'tasks: ' + ((Get-ScheduledTask | Where-Object { $_.TaskName -like 'claw-master*' } | ForEach-Object { $_.TaskName + '=' + $_.State }) -join ', ')
PS
)"
echo "done. Restart the Claude desktop app and any open Claude sessions on the PC."
