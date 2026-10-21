#!/usr/bin/env bash
#
# windows-pc-setup -- put a Windows PC on its Cloudflare route (colin-pc-ssh.tf for `colin-pc`), the way
# mac-ssh-connector-setup.sh does a Mac, and give its clients Windows-MCP (screenshots, clicks, typing in the user's own
# session). Run on a jumpbox as an AWS administrator.
#
#   scripts/windows-pc-setup.sh colin-pc                        over the PC's Cloudflare route (a re-run)
#   scripts/windows-pc-setup.sh colin-pc --bootstrap-port 21800 over the temporary reverse tunnel the PC keeps to
#                                                               fcvm-arm's 127.0.0.1:21800 (the first run; see
#                                                               scripts/windows/bootstrap.ps1)
#
# It reaches the PC from fcvm-arm as `ssh <alias>` (mac-ssh-client.tf: fcvm-arm's own key and host-key pin);
# --bootstrap-port only points that alias at 127.0.0.1:PORT with no proxy command. The account it logs in as must be an
# administrator on the PC (OpenSSH then gives it an elevated session). Each PowerShell step travels as -EncodedCommand
# (no quoting through two ssh hops) and data on stdin; a step that fails exits non-zero, which stops the script.
#   1. the PC is Windows x64
#   2. OpenSSH Server takes keys only and listens on 127.0.0.1 only (the connector is its one way in), its inbound
#      firewall rule is off, and its shell is Windows PowerShell
#   3. the clients' public keys (~/.ssh/to-<alias>.pub, made by mac-ssh-client.tf) in a managed block of
#      C:\ProgramData\ssh\administrators_authorized_keys, the only file sshd reads for an administrator
#   4. cloudflared, pinned by version and sha256 (the pins in scripts/claw-master-windows-setup.sh; a test keeps them
#      equal) and verified ON the PC, in C:\ProgramData\cloudflared-<alias> (SYSTEM and Administrators only)
#   5. the connector token, from its administration-only secret, through ssh STDIN only, into a file there
#   6. Windows-MCP in the user's session: uv and windows-mcp pinned, anonymous telemetry off, a bearer key of its own
#      (WINDOWS_MCP_AUTH_KEY, made on the PC and never printed), serving streamable HTTP on the PC's 127.0.0.1:8000 from
#      its per-user logon task, checked to refuse a request without the key. Clients reach it with
#      scripts/windows/windows-mcp-bridge.
#   7. last, the SYSTEM task 'cloudflared <alias>', at boot and restarted when it exits: `cloudflared tunnel run
#      --token-file` (a restart ends the sessions through it, so nothing follows it)
# Re-running is safe: what is already right is left alone, and the connector restarts only when its task or token
# changed or it is not running. A restart ends every session through the connector, this one too when it came that
# way, so it runs from a one-off SYSTEM task that outlives the session.
set -euo pipefail

REGION=${AWS_REGION:-us-west-1}
FCVM=${FCVM_HOST:-184.72.40.255} # fcvm-metal-arm's Elastic IP
KEY=${FCVM_KEY:-$HOME/.ssh/fcvm-ec2}

# Pins: keep CFD_* equal to scripts/claw-master-windows-setup.sh (scripts/test-windows-pc-setup.py enforces it).
CFD_VERSION=2026.9.3
CFD_SHA256_WINDOWS_AMD64=f096265ec2fcbe9bb6e2d64268db167ced3fcbb83d894bdb9e2fcdb26f2ea7e2
UV_VERSION=0.13.0
UV_SHA256_WINDOWS_AMD64_ZIP=088962f9e7b7bd9ea740c04c650b2a21c8928c345bd99ac24350dc924dba656c
# Windows-MCP from an exact upstream commit, not PyPI 0.8.8: 0.8.8's control gate latches in USER_CONTROL when the
# hook misses a key-up (Win+L, an elevated window) and refuses every tool until a restart (upstream #456, fixed in #460),
# and gates background tools too (#457; main's default "relaxed" policy gates only direct input). Main still calls
# itself 0.8.8, so the commit, not the version, says what is installed. The source zip is checked on the PC.
WINDOWS_MCP_COMMIT=5b24a8ab5c453e691feb6402cf455d83cf10998e
WINDOWS_MCP_ZIP_SHA256=b077f424a19d87ee6e71063263e080bdd7baa2bd27bf93c1355882b0cd75697c

usage() { echo "usage: windows-pc-setup.sh ALIAS [--bootstrap-port PORT]   (ALIAS: colin-pc)" >&2; exit 2; }
ALIAS=${1:-}
shift || true
BOOTSTRAP_PORT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --bootstrap-port) [ $# -ge 2 ] || usage; BOOTSTRAP_PORT=$2; shift 2 ;;
    *) usage ;;
  esac
done
[ -z "$BOOTSTRAP_PORT" ] || [[ $BOOTSTRAP_PORT =~ ^[0-9]{4,5}$ ]] || usage

# Per PC: its connector-token secret and its clients (box:account), matching colin-pc-ssh.tf and mac-ssh-client.tf.
case "$ALIAS" in
  colin-pc)
    TUNNEL_SECRET=colin-pc-ssh-tunnel-token
    CLIENTS="fcvm:ubuntu nextjs:colin"
    ;;
  *) usage ;;
esac

PC_SSH="$ALIAS"
if [ -n "$BOOTSTRAP_PORT" ]; then
  PC_SSH="-o ProxyCommand=none -o HostName=127.0.0.1 -o Port=$BOOTSTRAP_PORT $ALIAS"
fi
on_fcvm() { ssh -o BatchMode=yes -o ConnectTimeout=10 -i "$KEY" "ubuntu@$FCVM" "$@"; }
# Run a PowerShell script on the PC: jumpbox -> fcvm-arm -> the PC. stdin is passed through. Progress records would
# arrive as CLIXML on stderr.
on_pc() {
  local enc
  enc=$(printf '$ProgressPreference = %s\n%s' "'SilentlyContinue'" "$1" | iconv -f utf-8 -t utf-16le | base64 -w0)
  on_fcvm "ssh -o BatchMode=yes -o ConnectTimeout=10 $PC_SSH powershell -NoProfile -NonInteractive -EncodedCommand $enc" | tr -d '\r'
}
# PowerShell assignments for values the scripts below need ('' quoted; the values hold no quote).
ps_vars() { while [ $# -gt 0 ]; do printf "\$%s='%s'\n" "$1" "$2"; shift 2; done; }

# A client's public key as exactly `ssh-ed25519 <blob>`, or nothing (as in mac-ssh-connector-setup.sh).
clean_key() {
  local raw=$1 key
  case "$raw" in *$'\n'* | *$'\r'*) return 1 ;; esac
  key=$(printf '%s' "$raw" | cut -d' ' -f1-2)
  [[ $key =~ ^ssh-ed25519\ AAAAC3NzaC1lZDI1NTE5[A-Za-z0-9+/]{48}$ ]] || return 1
  printf '%s' "$key"
}

# One nextjs-ct account's key, read from that account's own file in its own Run Command (as in
# mac-ssh-connector-setup.sh), so one account's file can never stand in for another's.
NEXTJS_ID=""
nextjs_key() {
  local user=$1 cmd_id
  [[ $user =~ ^[a-z][a-z0-9]*$ ]] || return 1
  cmd_id=$(aws ssm send-command --region "$REGION" --instance-ids "$NEXTJS_ID" --document-name AWS-RunShellScript \
    --parameters "$(python3 -c 'import json,sys; print(json.dumps({"commands": [sys.argv[1]]}))' "cat /home/$user/.ssh/to-$ALIAS.pub")" \
    --query Command.CommandId --output text)
  aws ssm wait command-executed --region "$REGION" --command-id "$cmd_id" --instance-id "$NEXTJS_ID" >/dev/null 2>&1 || true
  aws ssm get-command-invocation --region "$REGION" --command-id "$cmd_id" --instance-id "$NEXTJS_ID" \
    --query StandardOutputContent --output text
}

echo "1/7 reaching $ALIAS from fcvm-arm"
ARCH=$(on_pc '$env:PROCESSOR_ARCHITECTURE') || { echo "$ALIAS is not reachable from fcvm-arm" >&2; exit 1; }
[ "$ARCH" = AMD64 ] || { echo "$ALIAS is not a Windows x64 PC ($ARCH)" >&2; exit 1; }

echo "2/7 OpenSSH Server: keys only, loopback only, no inbound firewall rule"
# Judged by what sshd itself reports (sshd -T, for this account, so a Match block counts), never by the file's text: a
# ListenAddress 0.0.0.0 or an earlier PasswordAuthentication yes beside the right lines would pass a text check.
SSHD_CHECK=$(cat <<'PS'
$svc = Get-CimInstance Win32_Service -Filter "Name = 'sshd'"
if ($null -eq $svc) { throw 'no sshd service' }
# The service's own executable: System32\OpenSSH for the Windows capability, Program Files\OpenSSH for the MSI.
$sshd = if ($svc.PathName -match '^"([^"]+)"') { $Matches[1] } else { ($svc.PathName -split ' ')[0] }
function Get-Effective {
  $ErrorActionPreference = 'Continue'
  # Every connection field: Windows OpenSSH skips a Match LocalAddress/LocalPort block when its field is absent.
  $o = @(& $sshd -T -C "user=$env:USERNAME,host=localhost,addr=127.0.0.1,laddr=127.0.0.1,lport=22" 2>&1 | ForEach-Object { "$_" })
  if ($LASTEXITCODE -ne 0) { throw "sshd -T failed: $($o -join ' ')" }
  $o
}
function Test-KeysOnlyLoopback($o) {
  $listen = @($o | Where-Object { $_ -match '^listenaddress ' } | ForEach-Object { ($_ -split ' ')[1] })
  ($listen.Count -gt 0) -and (@($listen | Where-Object { $_ -notmatch '^127\.0\.0\.1:' }).Count -eq 0) -and
    ($o -contains 'passwordauthentication no') -and
    (($o -contains 'kbdinteractiveauthentication no') -or ($o -contains 'challengeresponseauthentication no'))
}
# The running daemon read its file when it started: a file changed since then is not yet in force.
function Test-StartedAfterConfig {
  $p = Get-CimInstance Win32_Process -Filter "ProcessId = $((Get-CimInstance Win32_Service -Filter "Name = 'sshd'").ProcessId)"
  $p -and $p.CreationDate -gt (Get-Item "$env:ProgramData\ssh\sshd_config").LastWriteTime
}
function Test-Listening {
  $l = @(Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { $_.LocalAddress })
  ($l.Count -gt 0) -and (@($l | Where-Object { $_ -ne '127.0.0.1' }).Count -eq 0)
}
PS
)
on_pc "$SSHD_CHECK
$(cat <<'PS'
$ErrorActionPreference = 'Stop'
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw 'this account is not an administrator in this session; windows-pc-setup needs one'
}
Get-NetFirewallRule -Name OpenSSH-Server-In-TCP -ErrorAction SilentlyContinue | Disable-NetFirewallRule
New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -PropertyType String -Force `
  -Value "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" | Out-Null
$cfg = "$env:ProgramData\ssh\sshd_config"
if ((Test-KeysOnlyLoopback (Get-Effective)) -and (Test-Listening) -and (Test-StartedAfterConfig)) { 'sshd: already keys only, loopback only'; exit 0 }
if (-not (Test-KeysOnlyLoopback (Get-Effective))) {
  # Every such line goes, in a Match block too, and the wanted ones lead, before any Match block (a global setting
  # after one applies only inside it). ChallengeResponseAuthentication, not its newer name, which the OpenSSH 8.1 of
  # older Windows 10 builds rejects.
  $want = @('ListenAddress 127.0.0.1', 'PasswordAuthentication no', 'ChallengeResponseAuthentication no')
  $keep = @(Get-Content $cfg) | Where-Object { $_ -notmatch '^\s*(ListenAddress|PasswordAuthentication|KbdInteractiveAuthentication|ChallengeResponseAuthentication)\s' }
  Copy-Item $cfg "$cfg.before-windows-pc-setup" -Force
  Set-Content -Path $cfg -Encoding ascii -Value ($want + $keep)
  try { $ok = Test-KeysOnlyLoopback (Get-Effective) } catch { $ok = $false; "$_" }
  if (-not $ok) { Copy-Item "$cfg.before-windows-pc-setup" $cfg -Force; throw 'sshd_config is still not keys only and loopback only; restored it' }
}
# A restart from this session would end with it (Windows ends what an SSH session started): a one-off SYSTEM task.
$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -Command Start-Sleep 2; Restart-Service sshd -Force'
Register-ScheduledTask -TaskName 'windows-pc-setup sshd restart' -Action $a -Principal (New-ScheduledTaskPrincipal -UserId SYSTEM -LogonType ServiceAccount -RunLevel Highest) -Force | Out-Null
Start-ScheduledTask -TaskName 'windows-pc-setup sshd restart'
'sshd_config: keys only, loopback only; sshd restarting'
PS
)"
sleep 8
# The restarted daemon must be the one listening, on loopback only; until then nothing else runs.
on_pc "$SSHD_CHECK
$(cat <<'PS'
$ErrorActionPreference = 'Stop'
Unregister-ScheduledTask -TaskName 'windows-pc-setup sshd restart' -Confirm:$false -ErrorAction SilentlyContinue
for ($i = 0; $i -lt 15; $i++) {
  if ((Get-Service sshd).Status -eq 'Running' -and (Test-Listening) -and (Test-StartedAfterConfig)) { 'sshd: running, on 127.0.0.1 only'; exit 0 }
  Start-Sleep -Seconds 2
}
throw 'sshd is not running on 127.0.0.1 only'
PS
)"

echo "3/7 the clients' public keys"
KEYS=""
for client in $CLIENTS; do
  box=${client%%:*} user=${client#*:}
  case "$box" in
    fcvm)
      [ "$user" = ubuntu ] || { echo "only ubuntu on fcvm-arm" >&2; exit 1; }
      key=$(clean_key "$(on_fcvm "cat ~/.ssh/to-$ALIAS.pub" 2>/dev/null || true)") ||
        { echo "fcvm-arm ubuntu has no single valid ~/.ssh/to-$ALIAS.pub (mac-ssh-client.tf makes it)" >&2; exit 1; }
      KEYS+="$key fcvm-arm ubuntu -> $ALIAS"$'\n'
      ;;
    nextjs)
      if [ -z "$NEXTJS_ID" ]; then
        # The containerized box (nextjs-ct.tf), found by its Box tag; each account's container home is the host's
        # /home/<account>.
        NEXTJS_ID=$(aws ec2 describe-instances --region "$REGION" --filters Name=tag:Box,Values=nextjs-ct Name=instance-state-name,Values=running \
          --query 'Reservations[0].Instances[0].InstanceId' --output text)
        [ -n "$NEXTJS_ID" ] && [ "$NEXTJS_ID" != None ] || { echo "the nextjs-ct box is not running" >&2; exit 1; }
      fi
      key=$(clean_key "$(nextjs_key "$user" || true)") ||
        { echo "nextjs-ct $user has no single valid ~/.ssh/to-$ALIAS.pub (the container's provision makes it)" >&2; exit 1; }
      KEYS+="$key nextjs $user -> $ALIAS"$'\n'
      ;;
    *) echo "unknown box $box" >&2; exit 1 ;;
  esac
done
printf '%s' "$KEYS" | sed 's/^/   /'
printf '%s' "$KEYS" | on_pc "$(ps_vars Alias "$ALIAS")
$(cat <<'PS'
$ErrorActionPreference = 'Stop'
$begin = "# >>> $Alias clients (managed by aws scripts/windows-pc-setup.sh) >>>"
$end = "# <<< $Alias clients <<<"
$ak = "$env:ProgramData\ssh\administrators_authorized_keys"
$keys = @([Console]::In.ReadToEnd() -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ -match '^ssh-ed25519 \S+' })
if ($keys.Count -eq 0) { throw 'no client keys' }
$blobs = @($keys | ForEach-Object { ($_ -split ' ')[1] })
$old = @(if (Test-Path $ak) { Get-Content $ak })
$out = New-Object System.Collections.Generic.List[string]
$inBlock = $false
$whole = $old -contains $end
foreach ($line in $old) {
  if ($whole -and $line -eq $begin) { $inBlock = $true; continue }
  if ($inBlock) { if ($line -eq $end) { $inBlock = $false }; continue }
  # A line elsewhere for one of these keys (the bootstrap's) is folded into the block.
  if ($line -notmatch '^#' -and @($blobs | Where-Object { $line -match [regex]::Escape($_) }).Count -gt 0) { continue }
  $out.Add($line)
}
$out.Add($begin); $keys | ForEach-Object { $out.Add($_) }; $out.Add($end)
Set-Content -Path $ak -Encoding ascii -Value $out
icacls $ak /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'could not set the ACL of administrators_authorized_keys' }
"administrators_authorized_keys: $($keys.Count) managed key(s)"
PS
)"

echo "4/7 cloudflared $CFD_VERSION (verified on the PC)"
on_pc "$(ps_vars Alias "$ALIAS" CfdVersion "$CFD_VERSION" CfdSha "$CFD_SHA256_WINDOWS_AMD64")
$(cat <<'PS'
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = 'Tls12'
$dir = "$env:ProgramData\cloudflared-$Alias"
New-Item -ItemType Directory -Force $dir | Out-Null
icacls $dir /inheritance:r /grant 'Administrators:(OI)(CI)F' /grant 'SYSTEM:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw "could not make $dir SYSTEM and Administrators only" }
$exe = "$dir\cloudflared.exe"
# Judged by the hash of the installed executable, never by the version it reports.
if (-not (Test-Path $exe) -or (Get-FileHash $exe).Hash -ne $CfdSha) {
  Invoke-WebRequest -UseBasicParsing "https://github.com/cloudflare/cloudflared/releases/download/$CfdVersion/cloudflared-windows-amd64.exe" -OutFile "$exe.new"
  if ((Get-FileHash "$exe.new").Hash -ne $CfdSha) { Remove-Item "$exe.new"; throw 'cloudflared did not match its pinned sha256' }
  if (-not (Test-Path $exe)) {
    Move-Item -Force "$exe.new" $exe
  } else {
    # A running connector holds the old executable (Windows refuses to replace it) and would keep running it: the
    # verified one waits as cloudflared.exe.new, and step 7 swaps it in while it restarts the connector.
    New-Item -ItemType File -Force "$dir\cloudflared.changed" | Out-Null
    'cloudflared ' + (Get-FileHash "$exe.new").Hash.Substring(0, 16) + ' staged; the connector picks it up in step 7'
    exit 0
  }
}
'cloudflared ' + (Get-FileHash $exe).Hash.Substring(0, 16)
PS
)"

echo "5/7 the connector token (from Secrets Manager, through ssh stdin only)"
{ set +x; } 2>/dev/null
# Read first, then pipe: a PowerShell reached through Windows' OpenSSH never sees stdin that arrives after its session
# has started (measured 2026-10-20: printf piped at once is read; the same text 3 s later hangs ReadToEnd for good), and
# Secrets Manager answers after the session is up. The value stays in this shell's memory, never on a command line.
TUNNEL_TOKEN=$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$TUNNEL_SECRET" --query SecretString --output text)
[ -n "$TUNNEL_TOKEN" ] || { echo "$TUNNEL_SECRET is empty" >&2; exit 1; }
printf '%s' "$TUNNEL_TOKEN" |
  on_pc "$(ps_vars Alias "$ALIAS")
$(cat <<'PS'
$ErrorActionPreference = 'Stop'
$t = "$env:ProgramData\cloudflared-$Alias\token"
$new = [Console]::In.ReadToEnd().Trim()
if (-not $new) { throw 'empty token' }
if ((Test-Path $t) -and ((Get-Content $t -Raw).Trim() -eq $new)) { 'token unchanged'; exit 0 }
# The directory passes on SYSTEM and Administrators only (step 4).
[IO.File]::WriteAllText($t, $new)
New-Item -ItemType File -Force "$t.changed" | Out-Null
'token installed'
PS
)"

unset TUNNEL_TOKEN

echo "6/7 Windows-MCP ${WINDOWS_MCP_COMMIT:0:10} in the user's session (uv $UV_VERSION)"
on_pc "$(ps_vars UvVersion "$UV_VERSION" UvSha "$UV_SHA256_WINDOWS_AMD64_ZIP" McpCommit "$WINDOWS_MCP_COMMIT" McpZipSha "$WINDOWS_MCP_ZIP_SHA256")
$(cat <<'PS'
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = 'Tls12'
$bin = "$env:USERPROFILE\.local\bin"
New-Item -ItemType Directory -Force $bin | Out-Null
$uv = "$bin\uv.exe"
if (-not (Test-Path $uv) -or (& $uv --version) -notmatch "^uv $([regex]::Escape($UvVersion)) ") {
  $zip = "$env:TEMP\uv-$UvVersion.zip"
  Invoke-WebRequest -UseBasicParsing "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip" -OutFile $zip
  if ((Get-FileHash $zip).Hash -ne $UvSha) { Remove-Item $zip; throw 'uv did not match its pinned sha256' }
  Expand-Archive $zip -DestinationPath "$env:TEMP\uv-$UvVersion" -Force
  Copy-Item "$env:TEMP\uv-$UvVersion\*.exe" $bin -Force
  Remove-Item -Recurse -Force $zip, "$env:TEMP\uv-$UvVersion"
}
# Read by the server at start (its logon task gets the user's environment): no anonymous telemetry, and a bearer key on
# every request. The key is made here once and never printed; clients read it over ssh (windows-mcp-bridge).
[Environment]::SetEnvironmentVariable('ANONYMIZED_TELEMETRY', 'false', 'User')
if (-not [Environment]::GetEnvironmentVariable('WINDOWS_MCP_AUTH_KEY', 'User')) {
  $bytes = New-Object byte[] 32
  [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $key = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
  [Environment]::SetEnvironmentVariable('WINDOWS_MCP_AUTH_KEY', $key, 'User')
}
$env:ANONYMIZED_TELEMETRY = 'false'
$env:WINDOWS_MCP_AUTH_KEY = [Environment]::GetEnvironmentVariable('WINDOWS_MCP_AUTH_KEY', 'User')
$ErrorActionPreference = 'Continue'
# Always stopped first: the running server holds its environment's files (uv cannot replace them) and keeps the
# settings it started with (install --force recreates the task, not the process).
Stop-ScheduledTask -TaskName windows-mcp-server -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { $_.CommandLine -like '*windows_mcp*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
$marker = "$env:USERPROFILE\.windows-mcp\installed-commit"
$installed = if (Test-Path $marker) { (Get-Content $marker -Raw).Trim() } else { '' }
if ($installed -eq $McpCommit -and (Test-Path "$bin\windows-mcp.exe")) {
  "windows-mcp $($McpCommit.Substring(0, 10)): already installed"
} else {
  $zip = "$env:TEMP\windows-mcp-$McpCommit.zip"
  $src = "$env:TEMP\windows-mcp-$McpCommit"
  Invoke-WebRequest -UseBasicParsing "https://github.com/CursorTouch/Windows-MCP/archive/$McpCommit.zip" -OutFile $zip
  if ((Get-FileHash $zip).Hash -ne $McpZipSha) { Remove-Item $zip; 'windows-mcp source did not match its pinned sha256'; exit 1 }
  if (Test-Path $src) { Remove-Item -Recurse -Force $src }
  Expand-Archive $zip -DestinationPath $src -Force
  & $uv tool install --force --python 3.14 "$src\Windows-MCP-$McpCommit" 2>&1 | ForEach-Object { "$_" }
  $rc = $LASTEXITCODE
  Remove-Item -Recurse -Force $zip, $src
  if ($rc -ne 0) { 'uv tool install failed'; exit 1 }
  New-Item -ItemType Directory -Force (Split-Path $marker) | Out-Null
  Set-Content -Path $marker -Value $McpCommit -Encoding ascii
}
& "$bin\windows-mcp.exe" install --transport streamable-http --host 127.0.0.1 --port 8000 --force 2>&1 | ForEach-Object { "$_" }
if ($LASTEXITCODE -ne 0) { 'windows-mcp install failed'; exit 1 }
for ($i = 0; $i -lt 30; $i++) {
  if (Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { break }
  Start-Sleep -Seconds 2
}
if (-not (Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)) {
  "windows-mcp did not start listening; see $env:USERPROFILE\.windows-mcp\server.error.log"; exit 1
}
# The server now running must be the one with the key: a request without it is refused.
try {
  Invoke-WebRequest -UseBasicParsing -Method Post -Uri http://127.0.0.1:8000/mcp -Body '{}' -ContentType 'application/json' -TimeoutSec 15 | Out-Null
  $status = 200
} catch {
  $status = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
}
if ($status -ne 401) { "windows-mcp answered a request without its key with $status, not 401"; exit 1 }
'windows-mcp: listening on 127.0.0.1:8000, refusing requests without its key'
PS
)"
# Last: restarting the connector ends every session through it, so nothing may follow it.
echo "7/7 the connector: SYSTEM task 'cloudflared $ALIAS', at boot"
on_pc "$(ps_vars Alias "$ALIAS" CfdSha "$CFD_SHA256_WINDOWS_AMD64")
$(cat <<'PS'
$ErrorActionPreference = 'Stop'
$dir = "$env:ProgramData\cloudflared-$Alias"
$name = "cloudflared $Alias"
$cfArgs = "tunnel --no-autoupdate --logfile `"$dir\cloudflared.log`" run --token-file `"$dir\token`""
$action = New-ScheduledTaskAction -Execute "$dir\cloudflared.exe" -Argument $cfArgs
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId SYSTEM -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
$existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
$changed = @()
if ($null -eq $existing -or $existing.Actions[0].Arguments -ne $cfArgs -or $existing.Actions[0].Execute -ne "$dir\cloudflared.exe") { $changed += 'its task changed' }
if (Test-Path "$dir\token.changed") { $changed += 'its token changed' }
if (Test-Path "$dir\cloudflared.changed") {
  # Checked again here: the staged executable is swapped in by the restart below, as SYSTEM.
  if (-not (Test-Path "$dir\cloudflared.exe.new") -or (Get-FileHash "$dir\cloudflared.exe.new").Hash -ne $CfdSha) { throw 'the staged cloudflared is missing or does not match its pinned sha256' }
  $changed += 'its executable changed'
}
if ($null -eq $existing -or $existing.State -ne 'Running') { $changed += 'it is not running' }
if ($changed.Count -eq 0) { 'connector: unchanged and running; not restarted'; exit 0 }
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
# Restarting ends every session through this connector, this one too when it came that way: a one-off SYSTEM task.
$restart = "Stop-ScheduledTask -TaskName '$name'; Get-Process cloudflared -ErrorAction SilentlyContinue | Where-Object { `$_.Path -eq '$dir\cloudflared.exe' } | Stop-Process -Force; Start-Sleep 2; if (Test-Path '$dir\cloudflared.exe.new') { Move-Item -Force '$dir\cloudflared.exe.new' '$dir\cloudflared.exe' }; Start-ScheduledTask -TaskName '$name'; Remove-Item '$dir\token.changed', '$dir\cloudflared.changed' -ErrorAction SilentlyContinue"
$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -Command Start-Sleep 2; $restart"
Register-ScheduledTask -TaskName "$name restart" -Action $a -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName "$name restart"
"connector: restarting ($($changed -join ', ')); sessions through it will drop"
PS
)"

echo "done. From each client: ssh $ALIAS true; then scripts/windows/windows-mcp-client-setup.sh $ALIAS as that account"
