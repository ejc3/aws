#!/usr/bin/env bash
#
# claw-master-windows-setup -- put a Windows PC on the shared Claude subscriptions through the Cloudflare
# tunnel (claw-master-tunnel.tf), reaching the PC from fcvm-metal-arm over an ssh alias there (the PC
# keeps a reverse SSH tunnel to fcvm-arm open; its alias, e.g. `windows-pc`, is in ~/.ssh/config).
#
#   scripts/claw-master-windows-setup.sh windows-pc     run on a jumpbox as an AWS administrator
#
# Unlike the Mac setup this is not opt-in per command: `claw-master agent install` points every Claude
# Code on the PC (CLI, desktop app, IDE extensions) at the pool, failing closed. Undo on the PC with
# `claw-master agent uninstall`, and Unregister-ScheduledTask 'claw-master tunnel'.
#   * claw-master and cloudflared in %USERPROFILE%\.local\bin, each pinned by sha256 and verified ON the PC
#   * %USERPROFILE%\.config\claw-master\cloudflare.env (owner-only ACL): the PC's own Access service
#     token (claude-master/windows-pc-access). It travels from Secrets Manager through ssh STDIN only:
#     never an argument, never written on fcvm or here.
#   * %USERPROFILE%\.config\claw-master\ca.pem: the server's PUBLIC CA certificate
#   * the logon task 'claw-master tunnel', which keeps `cloudflared access tcp` running on
#     127.0.0.1:8444 (token read from the file into its own environment, never its arguments)
#   * `claw-master agent install` (its own logon task and the env of %USERPROFILE%\.claude\settings.json)
# Each PowerShell step travels as -EncodedCommand (no quoting through two ssh hops); data on stdin.
# Re-running is safe: it re-verifies the binaries, rewrites the files and restarts the tunnel task.
set -euo pipefail

REGION=${AWS_REGION:-us-west-1}
FCVM=${FCVM_HOST:-184.72.40.255}                       # fcvm-metal-arm's Elastic IP
KEY=${FCVM_KEY:-$HOME/.ssh/fcvm-ec2}

# Pins. Keep CM_TAG equal to claw_master_tag in claw-master-server.tf and CFD_VERSION to
# cloudflared_version in claw-master-tunnel.tf (tests enforce both); bump the sha256 values with them.
CM_TAG=claw-master-dc78270
CM_SHA256_WINDOWS_AMD64=8cae4162f7913d378f5e378d0cd0304d48e664fe8cade321f4dbab7012296ddc
CFD_VERSION=2026.9.3
CFD_SHA256_WINDOWS_AMD64=f096265ec2fcbe9bb6e2d64268db167ced3fcbb83d894bdb9e2fcdb26f2ea7e2
AGENT_LISTEN=127.0.0.1:8445

ALIAS=${1:-}
[[ $ALIAS =~ ^[a-z][a-z0-9-]{0,30}$ ]] || { echo "usage: claw-master-windows-setup.sh ALIAS   (the PC's ssh alias on fcvm-arm)" >&2; exit 2; }

# Run a PowerShell script on the PC: jumpbox -> fcvm -> (reverse tunnel) -> PC. stdin is passed through.
on_pc() {
  local enc
  enc=$(printf '$ProgressPreference = %s\n%s' "'SilentlyContinue'" "$1" | iconv -f utf-8 -t utf-16le | base64 -w0)
  ssh -o BatchMode=yes -o ConnectTimeout=10 -i "$KEY" "ubuntu@$FCVM" \
    "ssh -o BatchMode=yes -o ConnectTimeout=10 $ALIAS powershell -NoProfile -NonInteractive -EncodedCommand $enc"
}
# PowerShell assignments for values the scripts below need ('' quoted; the values hold no quote).
ps_vars() { while [ $# -gt 0 ]; do printf "\$%s='%s'\n" "$1" "$2"; shift 2; done; }

echo "0/7 the server must be serving the open listener (the tunnel leads nowhere otherwise)"
SERVER_ID=$(aws ec2 describe-instances --region "$REGION" --filters Name=tag:Name,Values=claude-master-server Name=instance-state-name,Values=running --query 'Reservations[0].Instances[0].InstanceId' --output text)
[ -n "$SERVER_ID" ] && [ "$SERVER_ID" != None ] || { echo "the claude-master server is not running" >&2; exit 1; }
CMD_ID=$(aws ssm send-command --region "$REGION" --instance-ids "$SERVER_ID" --document-name AWS-RunShellScript --parameters 'commands=["cat /var/lib/claude-master/state/ca.pem 2>/dev/null; echo ===STATUS===; claw-master-status 2>&1"]' --query Command.CommandId --output text)
aws ssm wait command-executed --region "$REGION" --command-id "$CMD_ID" --instance-id "$SERVER_ID"
READY=$(aws ssm get-command-invocation --region "$REGION" --command-id "$CMD_ID" --instance-id "$SERVER_ID" --query StandardOutputContent --output text)
CA=${READY%%===STATUS===*}
printf '%s\n' "$READY" | grep -q '^open listener: listening' || {
  echo "the server is not listening on its open port; check claw-master-status, then re-run this." >&2
  exit 1
}
printf '%s\n' "$CA" | grep -q 'BEGIN CERTIFICATE' || { echo "the server has no CA certificate yet" >&2; exit 1; }

echo "1/7 reaching $ALIAS through fcvm"
ARCH=$(on_pc '$env:PROCESSOR_ARCHITECTURE' | tr -d '\r') || { echo "$ALIAS is not reachable: the PC must be on, logged in, with its devbox tunnel task running" >&2; exit 1; }
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
)" | tr -d '\r'

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
# Owner-only before anything is written: the file is created, locked down, then filled.
Set-Content -Path "$f.new" -Value '' -NoNewline
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
icacls "$f.new" /inheritance:r /grant:r "*${sid}:F" | Out-Null
[IO.File]::WriteAllText("$f.new", [Console]::In.ReadToEnd())
Move-Item -Force "$f.new" $f
'installed cloudflare.env, readable by: ' + (((Get-Acl $f).Access | ForEach-Object { $_.IdentityReference.Value }) -join ', ')
PS
)" | tr -d '\r'
unset SECRET

echo "4/7 the server's public CA certificate (fetched by the readiness check above)"
printf '%s\n' "$CA" | on_pc "$(cat <<'PS'
$ErrorActionPreference = 'Stop'
$d = "$env:USERPROFILE\.config\claw-master"
[IO.File]::WriteAllText("$d\ca.pem", [Console]::In.ReadToEnd())
'installed ca.pem'
PS
)" | tr -d '\r'

echo "5/7 the 'claw-master tunnel' logon task, and the tunnel up on 127.0.0.1:$PORT"
on_pc "$(ps_vars Port "$PORT")
$(cat <<'PS'
$ErrorActionPreference = 'Stop'
$d = "$env:USERPROFILE\.config\claw-master"
$logs = "$env:LOCALAPPDATA\claw-master"
New-Item -ItemType Directory -Force $logs | Out-Null
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
Start-ScheduledTask -TaskName $name
for ($i = 0; $i -lt 30; $i++) {
  if (Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { "tunnel listening on 127.0.0.1:$Port"; exit 0 }
  Start-Sleep -Seconds 2
}
throw "the tunnel did not start listening on 127.0.0.1:$Port; see $logs\tunnel.log"
PS
)" | tr -d '\r'

echo "6/7 claw-master agent install (every Claude Code on the PC uses the pool from here on)"
on_pc "$(ps_vars Port "$PORT" Listen "$AGENT_LISTEN")
$(cat <<'PS'
$ErrorActionPreference = 'Stop'
$d = "$env:USERPROFILE\.config\claw-master"
& "$env:USERPROFILE\.local\bin\claw-master.exe" agent install --listen $Listen --open "127.0.0.1:$Port" --ca "$d\ca.pem" 2>&1 | ForEach-Object { "$_" }
if ($LASTEXITCODE -ne 0) { throw "agent install failed ($LASTEXITCODE)" }
PS
)" | tr -d '\r'

echo "7/7 checking: a request to Anthropic through the agent, the tunnel and the server"
on_pc "$(cat <<'PS'
$d = "$env:USERPROFILE\.config\claw-master"
# The proxy URL carries the agent's token: read from the settings, never printed.
$proxy = (Get-Content "$env:USERPROFILE\.claude\settings.json" -Raw | ConvertFrom-Json).env.HTTPS_PROXY
$code = & curl.exe -s -o NUL -w '%{http_connect} %{http_code}' -x $proxy --cacert "$d\ca.pem" https://api.anthropic.com/v1/models
"CONNECT and HTTP status: $code (200 for CONNECT; a 502 means the tunnel or the server is down)"
'tasks: ' + ((Get-ScheduledTask | Where-Object { $_.TaskName -like 'claw-master*' } | ForEach-Object { $_.TaskName + '=' + $_.State }) -join ', ')
PS
)" | tr -d '\r'
echo "done. Restart the Claude desktop app and any open Claude sessions on the PC."
