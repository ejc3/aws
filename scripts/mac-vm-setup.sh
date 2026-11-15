#!/usr/bin/env bash
#
# mac-vm-setup -- create one of the Mac mini's Tart macOS VMs (mac-vms.tf) and keep it running, from a Mac on the mini's
# LAN. Then put it on its Cloudflare route with scripts/mac-ssh-connector-setup.sh <vm>-mac --lan <mini>.
#
#   scripts/mac-vm-setup.sh ci    --lan mini-lan
#   scripts/mac-vm-setup.sh skevh --lan mini-lan
#   scripts/mac-vm-setup.sh ci    --lan mini-lan --runners    then: Xcode and the build tools in ci, and its runners
#
# --runners (ci only) runs dolphin-labs' own apps/native/scripts/ci-vm.sh `provision` on the mini against ci (Xcode
# copied in from the mini's ~/Applications/Xcode.app, Homebrew tools, the builds' signing keychain), then registers
# GitHub Actions runners in the VM for every private repository whose jobs ask for a self-hosted macOS runner
# (CI_RUNNERS below), each named mini-<repo>-<n> and labelled with the job's label and `mini`. Registration tokens are
# minted with this Mac's gh login (an admin of each repository) and reach the VM on stdin only. A runner already
# registered is left alone.
#
# On the mini (ssh <mini>), as its login user, with Tart at ~/.local/bin/tart (README "The Mac mini"):
#   * clones the VM from the base image pinned below, by digest, and sizes it (ci 9 cores / 36 GB / 200 GB disk, skevh
#     4 / 16 / 100: a Mac runs at most two macOS VMs at once, and these two leave the host 2 cores and 12 GB). An
#     existing VM is kept as it is: this never deletes or re-clones one.
#   * the LaunchAgent com.ejcampbell.tart.<vm>: `tart run --no-graphics <vm>` at login, restarted by launchd. ci also
#     gets the host's ~/Applications/Xcode.app read-only at /Volumes/My Shared Files/xcode.
#   * waits for the guest agent, then for skevh authorizes Steve's own keys (nextjs_user_keys.skevh in
#     nextjs-user-data.tf) for the guest's admin, in a managed block.
#   * prints the guest's SSH host key, which mac-ssh-client.tf pins for `ssh <vm>-mac`.
# Re-running is safe: nothing that is already right is changed, and the VM is restarted only when its LaunchAgent changed.
set -euo pipefail

BASE=ghcr.io/cirruslabs/macos-tahoe-base@sha256:1b093499716409d29e8b5336844528e1cae375db97d2ad8e5aeff78cf0da201e
ROOT=$(cd "$(dirname "$0")/.." && pwd)

# The ci VM's runners: repository, the label its jobs ask for, how many. All private (self-hosted runners must never
# serve a public repository's pull requests). The MacBook's own gww-ci and kidsports-ci VMs serve the first and last
# too; GitHub gives a job to whichever matching runner is idle.
CI_RUNNERS="dolphin-labs-hq/dolphin-labs:gww-ios:2 dolphin-labs-hq/dolphin-films:gww-ios:1 dolphin-labs-hq/kid-sports-highlights:kidsports-ios:2"
# The runner, pinned: dolphin-labs' ci-vm.sh's version and the SHA-256 GitHub publishes for it.
RUNNER_VERSION=2.338.0
RUNNER_SHA256=df4cebda25c86a886ed204e49fee63f5c2e7cec5f447b5c98440a826bbdf9df2

usage() { echo "usage: mac-vm-setup.sh ci|skevh --lan SSH_DEST [--runners]" >&2; exit 2; }
VM=${1:-}
shift || true
LAN=""
RUNNERS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --lan) [ $# -ge 2 ] || usage; LAN=$2; shift 2 ;;
    --runners) RUNNERS=1; shift ;;
    *) usage ;;
  esac
done
[[ $LAN =~ ^[A-Za-z0-9][A-Za-z0-9.@_-]*$ ]] || usage
[ -z "$RUNNERS" ] || [ "$VM" = ci ] || usage
case "$VM" in
  ci) CPU=9 MEMORY_MB=36864 DISK_GB=200 DIRS='--dir=xcode:$HOME/Applications/Xcode.app:ro' ;;
  skevh) CPU=4 MEMORY_MB=16384 DISK_GB=100 DIRS='' ;;
  *) usage ;;
esac
on_host() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$LAN" "$@"; }

# Steve's keys, read from Terraform's declaration so this stays in step with nextjs-dev's (one key per line).
OWNER_KEYS=""
if [ "$VM" = skevh ]; then
  OWNER_KEYS=$(python3 - "$ROOT/nextjs-user-data.tf" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
block = re.search(r'\n    skevh = \[\n(.*?)\n    \]', text, re.S).group(1)
for key in re.findall(r'"((?:ssh-ed25519|sk-ssh-ed25519@openssh.com|ecdsa-sha2-nistp256|sk-ecdsa-sha2-nistp256@openssh.com) [A-Za-z0-9+/=]+)[^"]*"', block):
    print(key)
PY
)
  [ -n "$OWNER_KEYS" ] || { echo "no keys found for skevh in nextjs-user-data.tf" >&2; exit 1; }
fi

echo "1/4 $VM on $LAN"
on_host bash -s <<REMOTE
set -euo pipefail
export PATH=\$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin
command -v tart >/dev/null || { echo "no tart in ~/.local/bin on the mini (README 'The Mac mini')" >&2; exit 1; }
if tart list --format json | python3 -c "import json,sys; sys.exit(0 if any(v['Name']=='$VM' for v in json.load(sys.stdin)) else 1)"; then
  echo "$VM exists; kept as it is"
else
  [ "$VM" != ci ] || [ -d "\$HOME/Applications/Xcode.app" ] || { echo "ci needs ~/Applications/Xcode.app on the mini" >&2; exit 1; }
  tart clone "$BASE" "$VM"
  tart set "$VM" --cpu $CPU --memory $MEMORY_MB --disk-size $DISK_GB
  echo "$VM cloned: $CPU cores, $MEMORY_MB MB, $DISK_GB GB"
fi
REMOTE

echo "2/4 its LaunchAgent"
on_host bash -s <<REMOTE
set -euo pipefail
LABEL=com.ejcampbell.tart.$VM
PLIST=\$HOME/Library/LaunchAgents/\$LABEL.plist
mkdir -p "\$HOME/Library/LaunchAgents" "\$HOME/Library/Logs"
DIRS="$DIRS"
DIRS=\${DIRS:+<string>\$(eval echo "\$DIRS")</string>}
cat > "\$PLIST.new" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>\$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>\$HOME/.local/bin/tart</string><string>run</string><string>--no-graphics</string>\$DIRS<string>$VM</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>\$HOME/Library/Logs/\$LABEL.log</string>
  <key>StandardErrorPath</key><string>\$HOME/Library/Logs/\$LABEL.log</string>
</dict></plist>
PL
plutil -lint "\$PLIST.new" >/dev/null
uid=\$(id -u)
if cmp -s "\$PLIST.new" "\$PLIST" && launchctl print "gui/\$uid/\$LABEL" >/dev/null 2>&1; then
  rm -f "\$PLIST.new"; echo "unchanged and loaded"
else
  mv -f "\$PLIST.new" "\$PLIST"
  launchctl bootout "gui/\$uid/\$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/\$uid" "\$PLIST"
  echo "loaded"
fi
REMOTE

echo "3/4 waiting for the guest agent"
for _ in $(seq 1 60); do
  on_host "\$HOME/.local/bin/tart exec $VM true" >/dev/null 2>&1 && break
  sleep 5
done
on_host "\$HOME/.local/bin/tart exec $VM true" >/dev/null || { echo "$VM did not answer within five minutes" >&2; exit 3; }
# The disk was made larger than the image's: the system's APFS container takes the rest (as dolphin-labs' ci-vm.sh does;
# already the whole disk is not a failure).
on_host "\$HOME/.local/bin/tart exec $VM /bin/bash -c $(printf '%q' '
  set -e
  container=$(diskutil info / | awk -F": *" "/APFS Container:/ {print \$2}")
  store=$(diskutil apfs list $container | awk "/Physical Store/ {print \$4; exit}")
  yes | sudo diskutil repairDisk ${store%s*} >/dev/null 2>&1 || true
  out=$(sudo diskutil apfs resizeContainer $store 0 2>&1) || echo "$out" | grep -q "must be different"
  echo "   disk: $(df -h / | tail -1 | awk "{print \$2}")"
')"

if [ -n "$OWNER_KEYS" ]; then
  echo "   $VM owner's keys"
  printf '%s\n' "$OWNER_KEYS" | on_host "\$HOME/.local/bin/tart exec -i $VM /bin/bash -c $(printf '%q' '
set -euo pipefail
BEGIN="# >>> owner keys (managed by aws scripts/mac-vm-setup.sh) >>>"
END="# <<< owner keys <<<"
AK=$HOME/.ssh/authorized_keys
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh" && touch "$AK"
keys=$(cat)
awk -v b="$BEGIN" -v e="$END" "\$0 == b { skip = 1; next } skip { if (\$0 == e) skip = 0; next } { print }" "$AK" > "$AK.new"
{ echo "$BEGIN"; printf "%s\n" "$keys"; echo "$END"; } >> "$AK.new"
if cmp -s "$AK.new" "$AK"; then rm -f "$AK.new"; else mv -f "$AK.new" "$AK"; fi
chmod 600 "$AK"
echo "   $(printf "%s\n" "$keys" | grep -c .) keys"
')"
fi

echo "4/4 the guest's SSH host key (pin it in mac-ssh-client.tf as ${VM}-mac)"
on_host "\$HOME/.local/bin/tart exec $VM cat /etc/ssh/ssh_host_ed25519_key.pub" | cut -d' ' -f1-2

[ -n "$RUNNERS" ] || exit 0

echo "runners 1/2 provision (dolphin-labs apps/native/scripts/ci-vm.sh, on the mini)"
gh api repos/dolphin-labs-hq/dolphin-labs/contents/apps/native/scripts/ci-vm.sh -H 'Accept: application/vnd.github.raw' |
  on_host "cat > \$HOME/.local/bin/ci-vm.sh && chmod 755 \$HOME/.local/bin/ci-vm.sh"
on_host "PATH=\$HOME/.local/bin:\$PATH GWW_CI_VM=$VM GWW_CI_XCODE=\$HOME/Applications/Xcode.app ci-vm.sh provision"

echo "runners 2/2 registrations"
on_host "\$HOME/.local/bin/tart exec $VM /bin/zsh -lc $(printf '%q' "
  set -e
  mkdir -p ~/runners && cd ~/runners
  tarball=actions-runner-osx-arm64-$RUNNER_VERSION.tar.gz
  [ -f \$tarball ] || curl -fsSLo \$tarball https://github.com/actions/runner/releases/download/v$RUNNER_VERSION/\$tarball
  echo '$RUNNER_SHA256  '\$tarball | shasum -a 256 -c - >/dev/null
")"
for entry in $CI_RUNNERS; do
  IFS=: read -r repo label count <<<"$entry"
  short=${repo#*/}
  for i in $(seq 1 "$count"); do
    name="mini-$short-$i"
    if on_host "\$HOME/.local/bin/tart exec $VM test -f /Users/admin/runners/$name/.runner"; then
      echo "   $name: already registered"
      continue
    fi
    # A token per runner (they last an hour), on stdin end to end: gh -> ssh -> tart exec -> config.sh.
    gh api -X POST "repos/$repo/actions/runners/registration-token" --jq .token |
      on_host "\$HOME/.local/bin/tart exec -i $VM /bin/zsh -lc $(printf '%q' "
        set -e
        token=\$(cat)
        mkdir -p ~/runners/$name && cd ~/runners/$name
        [ -x ./config.sh ] || tar xzf ../actions-runner-osx-arm64-$RUNNER_VERSION.tar.gz
        ./config.sh --unattended --url https://github.com/$repo --token \"\$token\" --name $name --labels $label,mini --work _work --replace >/dev/null
        ./svc.sh install >/dev/null
        ./svc.sh start >/dev/null
      ")"
    echo "   $name: registered for $repo ($label)"
  done
done
