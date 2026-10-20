# `ssh mac`, `ssh colton-mac` and `ssh colin-pc`: the ONE way to each Mac (and Colin's Windows PC), the same on every box
# that has it. Each client goes through Cloudflare Access (macbook-ssh.tf, colton-mac-ssh.tf, colin-pc-ssh.tf) with ITS
# OWN service token, to the machine's connector, and ends at its SSH server (macOS Remote Login, or OpenSSH Server on
# Windows; 127.0.0.1:22). No inbound port anywhere, and no reverse tunnel.
#
#   alias        box, account          service token (Secrets Manager)          configured by
#   mac          fcvm-arm, ubuntu      macbook-ssh-access                       dev-user-data.tf (fcvm_mac_ssh_clients)
#                nextjs-dev, ejc3      macbook-ssh-access-nextjs-ejc3           nextjs-user-data.tf (ejc3 only)
#   colton-mac   fcvm-arm, ubuntu      colton-mac-ssh-access-fcvm               dev-user-data.tf (fcvm_mac_ssh_clients)
#                nextjs-dev, colton    colton-mac-ssh-access-nextjs-colton      nextjs-user-data.tf
#                nextjs-dev, ejc3      colton-mac-ssh-access-nextjs-ejc3        nextjs-user-data.tf
#   colin-pc     fcvm-arm, ubuntu      colin-pc-ssh-access-fcvm                 dev-user-data.tf (fcvm_mac_ssh_clients)
#
# The private key (~/.ssh/to-<alias>) never leaves its box. EJ's `to-mac` is never installed or made by anything: it
# stays where the owner put it. For a Mac with make_key, the installer makes the account's key AS that account when it
# is missing and prints only its public half, which then goes on the Mac (scripts/mac-ssh-connector-setup.sh) or the PC
# (scripts/windows-pc-setup.sh).
locals {
  # One entry per Mac, keyed by its ssh alias. Every value is public. host_key is the Mac's Remote Login host key
  # (/etc/ssh/ssh_host_ed25519_key.pub, read on the Mac itself), pinned under host_key_alias in ~/.ssh/known_hosts.<alias>;
  # hostname is the Access application's (macbook-ssh.tf, colton-mac-ssh.tf). A reinstalled Mac has a new host key.
  mac_ssh_macs = {
    mac = {
      hostname       = "macbook-ssh.cc-games.dev"
      user           = "ejcampbell"
      host_key_alias = "ejs-macbook-pro-via-fcvm"
      host_key       = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAID1DlX7dlpsRWSQuPOHoiDxtiyZd7/W4I1toKoLi7zfL"
      make_key       = false
    }
    colton-mac = {
      hostname       = "colton-mac-ssh.cc-games.dev"
      user           = "colton"
      host_key_alias = "colton-mac"
      host_key       = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHwu+CCNChdYQcpAVyCXMIkwoSP75e159hJk1a/tpOkO"
      make_key       = true
    }
    # Colin's Windows PC: OpenSSH Server's host key (C:\ProgramData\ssh\ssh_host_ed25519_key.pub, read on the PC).
    colin-pc = {
      hostname       = "colin-pc-ssh.cc-games.dev"
      user           = "colin"
      host_key_alias = "colin-pc"
      host_key       = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILoueLCDZs2ZUj+FBJcE++RvLicN7v0rY9Nxvr9lEmp4"
      make_key       = true
    }
  }

  # The map as the rows the installer reads: alias hostname user host_key_alias make_key host_key.
  mac_ssh_client_table = join("", [for alias, m in local.mac_ssh_macs : "${alias} ${m.hostname} ${m.user} ${m.host_key_alias} ${m.make_key ? "yes" : "no"} ${m.host_key}\n"])

  # cloudflared for the proxy command, pinned with claw-master's (claw-master-tunnel.tf), into the account's
  # ~/.local/bin. Expects MAC_USER (see mac_ssh_client_setup). Kept out of mac_ssh_client_setup, which
  # scripts/test-mac-ssh-client.py runs as plain bash.
  mac_cloudflared_install = <<-MACCFD
install -d -o "$MAC_USER" -g "$MAC_USER" "/home/$MAC_USER/.local" "/home/$MAC_USER/.local/bin"
if ! echo "${local.cloudflared_sha256_linux_arm}  /home/$MAC_USER/.local/bin/cloudflared" | sha256sum -c - >/dev/null 2>&1; then
  if curl -fsSL "https://github.com/cloudflare/cloudflared/releases/download/${local.cloudflared_version}/cloudflared-linux-arm64" -o /tmp/cloudflared-mac.new \
     && echo "${local.cloudflared_sha256_linux_arm}  /tmp/cloudflared-mac.new" | sha256sum -c - >/dev/null; then
    install -o "$MAC_USER" -g "$MAC_USER" -m 755 /tmp/cloudflared-mac.new "/home/$MAC_USER/.local/bin/cloudflared"
  else
    echo "WARNING: cloudflared ${local.cloudflared_version} did not download or match its sha256; ssh mac needs it" >&2
  fi
  rm -f /tmp/cloudflared-mac.new
fi
MACCFD

  # `Host <alias>` for one account and one Mac. Run as root with MAC_USER (the account), MAC_SECRET (the Secrets Manager
  # secret holding that account's Access service token for this Mac) and MAC_ALIAS (a key of mac_ssh_macs; `mac` when
  # unset) set; MAC_HOME defaults to /home/$MAC_USER.
  mac_ssh_client_setup = <<-MACSETUP
MAC_ALIAS="$${MAC_ALIAS:-mac}"
MAC_HOME="$${MAC_HOME:-/home/$MAC_USER}"
MAC_ROW=$(awk -v a="$MAC_ALIAS" '$1 == a' <<'MACTABLE'
${local.mac_ssh_client_table}MACTABLE
)
[ -n "$MAC_ROW" ] || { echo "mac-ssh-client: no Mac named '$MAC_ALIAS' in mac_ssh_macs (mac-ssh-client.tf)" >&2; exit 1; }
read -r _ MAC_HOSTNAME MAC_REMOTE_USER MAC_HOST_KEY_ALIAS MAC_MAKE_KEY MAC_HOST_KEY <<MACROW
$MAC_ROW
MACROW
install -d -m 700 -o "$MAC_USER" -g "$MAC_USER" "$MAC_HOME/.ssh"

# The account's own key for this Mac, made as the account, only for a Mac with make_key and only when missing. Only
# its public half is printed; it goes on the Mac (scripts/mac-ssh-connector-setup.sh).
if [ "$MAC_MAKE_KEY" = yes ] && [ ! -e "$MAC_HOME/.ssh/to-$MAC_ALIAS" ]; then
  runuser -u "$MAC_USER" -- ssh-keygen -q -t ed25519 -N '' -C "$(hostname -s) $MAC_USER -> $MAC_ALIAS" \
    -f "$MAC_HOME/.ssh/to-$MAC_ALIAS" </dev/null >/dev/null
  echo "mac-ssh-client: made $MAC_HOME/.ssh/to-$MAC_ALIAS; authorize on $MAC_ALIAS: $(cat "$MAC_HOME/.ssh/to-$MAC_ALIAS.pub")"
fi

cat > "$MAC_HOME/.ssh/config.d-$MAC_ALIAS" <<MACCONFIG
# Generated by Terraform (mac-ssh-client.tf). $MAC_ALIAS, the one way in: Cloudflare Access ($MAC_HOSTNAME)
# with this account's service token, then the Mac's Remote Login.
Host $MAC_ALIAS
    HostName $MAC_HOSTNAME
    User $MAC_REMOTE_USER
    IdentityFile ~/.ssh/to-$MAC_ALIAS
    IdentitiesOnly yes
    ProxyCommand ~/.ssh/$MAC_ALIAS-access-proxy %h
    HostKeyAlias $MAC_HOST_KEY_ALIAS
    UserKnownHostsFile ~/.ssh/known_hosts.$MAC_ALIAS
    StrictHostKeyChecking yes
    ServerAliveInterval 30
    ServerAliveCountMax 3
MACCONFIG

# ProxyCommand for Host <alias>. The Access service token reaches cloudflared through its environment,
# never its arguments, and is read for each connection (a rotation needs nothing here).
cat > "$MAC_HOME/.ssh/$MAC_ALIAS-access-proxy.new" <<'MACPROXY'
#!/bin/sh
set -eu
set +x
s=$(aws secretsmanager get-secret-value --region us-west-1 --secret-id @MAC_SECRET@ \
      --query SecretString --output text)
TUNNEL_SERVICE_TOKEN_ID=$(printf '%s' "$s" | jq -r .client_id)
TUNNEL_SERVICE_TOKEN_SECRET=$(printf '%s' "$s" | jq -r .client_secret)
export TUNNEL_SERVICE_TOKEN_ID TUNNEL_SERVICE_TOKEN_SECRET
PATH="$HOME/.local/bin:$PATH" exec cloudflared access ssh --hostname "$1"
MACPROXY
sed -i "s|@MAC_SECRET@|$MAC_SECRET|" "$MAC_HOME/.ssh/$MAC_ALIAS-access-proxy.new"
mv -f "$MAC_HOME/.ssh/$MAC_ALIAS-access-proxy.new" "$MAC_HOME/.ssh/$MAC_ALIAS-access-proxy"

printf '%s %s\n' "$MAC_HOST_KEY_ALIAS" "$MAC_HOST_KEY" > "$MAC_HOME/.ssh/known_hosts.$MAC_ALIAS"

# What earlier routes of `ssh mac` left behind: the reverse tunnels' and the first Cloudflare aliases' files.
if [ "$MAC_ALIAS" = mac ]; then
  rm -f "$MAC_HOME/.ssh/config.d-mac-tunnels" "$MAC_HOME/.ssh/known_hosts.mac-tunnels" \
        "$MAC_HOME/.ssh/macbook-access-proxy" "$MAC_HOME/.ssh/config.d-macbook"
fi

# The Include goes before any Host or Match so an old Host * cannot override it. One already there stays where it
# is, so two Macs' installers never fight over the first line; otherwise it goes first. Retire the old Includes and
# any standalone stanza for this alias (and, for mac, the old names macbook and macbook-desktop) with the comment
# lines directly above it; keep unrelated hosts, includes and their comments. The original is backed up once per
# content.
MAC_HOME="$MAC_HOME" MAC_ALIAS="$MAC_ALIAS" python3 - <<'MACMIGRATE'
from pathlib import Path
import hashlib
import os
import shlex
import tempfile

alias = os.environ['MAC_ALIAS']
path = Path(os.environ['MAC_HOME']) / '.ssh' / 'config'
original = path.read_text() if path.exists() else ''
include = f'Include ~/.ssh/config.d-{alias}'
legacy_files = {'mac': ('config.d-mac-tunnels', 'config.d-macbook')}.get(alias, ())
old_includes = {f'Include {prefix}{name}' for name in (f'config.d-{alias}',) + legacy_files
                for prefix in ('~/.ssh/', '')} - {include}
retired = (alias,) + {'mac': ('macbook', 'macbook-desktop')}.get(alias, ())
kept = []
pending = []
skip = False

def flush(lines, dropped):
    if not dropped:
        kept.extend(lines)

def directive(line):
    fields = shlex.split(line, comments=True)
    return fields[0].lower() if fields else None

for line in original.splitlines(keepends=True):
    if line.strip() in old_includes:
        continue
    fields = shlex.split(line, comments=True)
    if not fields:
        pending.append(line)
        continue
    if fields[0].lower() in ('host', 'match'):
        attached = len(pending)
        while attached and pending[attached - 1].lstrip().startswith('#'):
            attached -= 1
        flush(pending[:attached], skip)
        skip = (fields[0].lower() == 'host' and len(fields) == 2 and fields[1] in retired)
        flush(pending[attached:], skip)
    else:
        flush(pending, skip)
    pending = []
    flush([line], skip)
flush(pending, skip)

first_host = next((i for i, line in enumerate(kept) if directive(line) in ('host', 'match')), len(kept))
early = [i for i, line in enumerate(kept[:first_host]) if line.strip() == include]
if early:
    updated = ''.join(line for i, line in enumerate(kept) if line.strip() != include or i == early[0])
else:
    updated = include + '\n\n' + ''.join(line for line in kept if line.strip() != include).lstrip('\n')
if updated != original:
    if path.exists():
        backup = path.with_name('config.before-mac.' + hashlib.sha256(original.encode()).hexdigest()[:12])
        if not backup.exists():
            backup.write_text(original)
            backup.chmod(0o600)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
        f.write(updated)
        temporary = f.name
    os.replace(temporary, path)
MACMIGRATE

chown "$MAC_USER:$MAC_USER" "$MAC_HOME/.ssh/config" "$MAC_HOME/.ssh/config.d-$MAC_ALIAS" "$MAC_HOME/.ssh/known_hosts.$MAC_ALIAS" "$MAC_HOME/.ssh/$MAC_ALIAS-access-proxy"
chmod 600 "$MAC_HOME/.ssh/config" "$MAC_HOME/.ssh/config.d-$MAC_ALIAS" "$MAC_HOME/.ssh/known_hosts.$MAC_ALIAS"
chmod 700 "$MAC_HOME/.ssh/$MAC_ALIAS-access-proxy"
MACSETUP

  # fcvm-arm's ubuntu: `ssh mac`, `ssh colton-mac` and `ssh colin-pc`, each with fcvm-arm's own service token. The arm setup script runs
  # it at boot (dev-user-data.tf) and the association below runs it on the running box, daily.
  fcvm_mac_ssh_clients = <<-FCVMMACS
MAC_USER=ubuntu
${local.mac_cloudflared_install}
MAC_ALIAS=mac
MAC_SECRET=macbook-ssh-access
${local.mac_ssh_client_setup}
MAC_ALIAS=colton-mac
MAC_SECRET=colton-mac-ssh-access-fcvm
${local.mac_ssh_client_setup}
MAC_ALIAS=colin-pc
MAC_SECRET=colin-pc-ssh-access-fcvm
${local.mac_ssh_client_setup}
FCVMMACS
}

# fcvm-arm reruns its setup script only at boot, so a change here (a new Mac, a new host key) would sit unapplied on the
# running box. This association runs the same clients' snippet there daily and once when created (the pattern of
# claw-master-client.tf): idempotent, it restarts nothing. Run Command uses sh, so the snippet runs under bash from a file
# (a script read from stdin could have its text eaten by a command that reads stdin).
resource "aws_ssm_association" "fcvm_mac_ssh_clients" {
  count = var.enable_firecracker_instance ? 1 : 0

  name                = "AWS-RunShellScript"
  association_name    = "fcvm-mac-ssh-clients"
  schedule_expression = "rate(1 day)"
  max_concurrency     = "1"
  max_errors          = "1"

  targets {
    key    = "InstanceIds"
    values = [aws_instance.firecracker_dev[0].id]
  }

  parameters = {
    commands         = "f=$(mktemp)\ncat > \"$f\" <<'FCVMMACSSH'\nset -eu\n${local.fcvm_mac_ssh_clients}FCVMMACSSH\nbash \"$f\" </dev/null\nrc=$?\nrm -f \"$f\"\nexit $rc\n"
    executionTimeout = "300"
  }
}

output "mac_ssh_usage" {
  description = "SSH to EJ's MacBook Pro"
  value       = "ssh mac, as ubuntu on fcvm-arm or ejc3 on nextjs-dev (Cloudflare Access, macbook-ssh.cc-games.dev, to the Mac's Remote Login)"
}

output "colton_mac_ssh_usage" {
  description = "SSH to Colton's Mac"
  value       = "ssh colton-mac, as ubuntu on fcvm-arm or colton or ejc3 on nextjs-dev (Cloudflare Access, colton-mac-ssh.cc-games.dev, to the Mac's Remote Login)"
}
