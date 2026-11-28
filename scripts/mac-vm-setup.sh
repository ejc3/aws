#!/usr/bin/env bash
#
# mac-vm-setup -- create one of the Mac mini's Tart macOS VMs (mac-vms.tf) and keep it running, from a Mac on the mini's
# LAN. Then put it on its Cloudflare route with scripts/mac-ssh-connector-setup.sh <vm>-mac --lan <mini>.
#
#   scripts/mac-vm-setup.sh ci    --lan mini-lan
#   scripts/mac-vm-setup.sh skevh --lan mini-lan
#   scripts/mac-vm-setup.sh ci    --lan mini-lan --runners    then: Xcode and the build tools in ci, and its runners
#
# --runners (ci only) provisions ci the way dolphin-labs' apps/native/scripts/ci-vm.sh `provision` does (Xcode copied
# in from the mini's ~/Applications/Xcode.app, Homebrew tools, the builds' signing keychain), then registers a
# pool of GitHub Actions runners named mini-<n> in dolphin-labs-hq's Default group. An eligible private
# repository can select any runner in the shared iOS pool or require the mini using its host label.
# To install only the test slots and the hourly disk cleaner (ci-disk-clean) on a running CI VM:
#   scripts/mac-vm-setup.sh ci --lan mini-lan --simulator-slots
# To install only Rust (dolphin-labs' pinned channel with the Apple targets, for jobs that build a crate as an Apple
# static library), which is safe while the runners build (it adds to the guest's ~/.cargo and nothing else):
#   scripts/mac-vm-setup.sh ci --lan mini-lan --rust
# Registration tokens are minted with this Mac's gh login (it needs the admin:org scope: gh auth refresh -s admin:org)
# and reach the VM on stdin only. Existing services keep running while GitHub reconciles the simulator label.
#
# On the mini (ssh <mini>), as its login user, with Tart at ~/.local/bin/tart (README "The Mac mini"):
#   * clones the VM from the base image pinned below, by digest, and sizes it (ci 9 cores / 28 GB / 200 GB disk, skevh
#     4 / 16 / 100: a Mac runs at most two macOS VMs at once, and these two leave the host 2 cores and 12 GB). An
#     existing VM is kept as it is: this never deletes or re-clones one.
#   * the LaunchAgent com.ejcampbell.tart.<vm>: `tart run --no-graphics <vm>` at login, restarted by launchd. ci also
#     gets the host's ~/Applications/Xcode.app read-only at /Volumes/My Shared Files/xcode.
#   * waits for the guest agent, grows the guest's disk, replaces the image's shared SSH host keys with the guest's
#     own (once), and for skevh authorizes Steve's own keys (nextjs_user_keys.skevh in nextjs-user-data.tf) for the
#     guest's admin, in a managed block.
#   * prints the guest's SSH host key, which mac-ssh-client.tf pins for `ssh <vm>-mac`.
# Re-running is safe: nothing that is already right is changed, and the VM is restarted only when its LaunchAgent changed.
set -euo pipefail

BASE=ghcr.io/cirruslabs/macos-tahoe-base@sha256:1b093499716409d29e8b5336844528e1cae375db97d2ad8e5aeff78cf0da201e
ROOT=$(cd "$(dirname "$0")/.." && pwd)

# GitHub adds self-hosted, macOS and ARM64. ios-pool is shared with the MacBook.
# mini selects this host and gww-ios supports workflows that still use the older label.
CI_ORG=dolphin-labs-hq
# ios-queue is on every mini: dolphin-labs' merge queue batches ask for it (docs/MERGE_QUEUE.md, "Runners: batches
# first"), so a batch runs on any free mini.
CI_LABELS=gww-ios,mini,ios-pool,ios-queue
# Two runners in this VM (owner, 2026-11-28), not four: four jobs at once on one VM made a 28-minute swift test run
# past its 2-hour limit (dolphin-labs batch #2689). mini-3 takes queue batches alone; mini-4 is the shared runner and
# the one simulator. mini-1 and mini-2 are retired: deregistered, their service and directory removed.
CI_RUNNERS="mini-3 mini-4"
RETIRED_RUNNERS="mini-1 mini-2"
# One runner kept for those batches alone: it carries ios-queue and nothing else of ours. Not ios-pool, so no
# dolphin-labs PR job takes it; not mini, so no dolphin-maps job (they ask for mini) takes it; not gww-ios. A batch never
# waits behind other jobs for every Mac.
QUEUE_RUNNER=mini-3
QUEUE_LABELS=ios-queue
SIMULATOR_RUNNER=mini-4
SIMULATOR_LABEL=mini-simulator
# Shared with the MacBook's listener: jobs that may run on either Mac's simulator ask for it. Its holders are kept by
# scripts/simulator-listeners.sh; a registration here gives it to mini-4 too, so a rebuilt guest keeps it.
SHARED_SIMULATOR_LABEL=ios-simulator
# Rust in ci, as dolphin-labs' ci-vm.sh installs it on the MacBook's VM: rustup's own installer, pinned (its version and
# the SHA-256 rust-lang publishes for its aarch64-apple-darwin build), and the channel dolphin-labs' rust-toolchain.toml
# pins (keep in step; a build on another pin still has rustup fetch it). The targets are the device, the simulator and
# the Mac, both architectures, and wasm.
RUST_CHANNEL=1.99.1
RUSTUP_VERSION=1.29.3
RUSTUP_SHA256=63752a1919a3a4793173ec70f8f212621a7ce38ba6a246095984482f4de20f9f
RUST_TARGETS=aarch64-apple-ios,aarch64-apple-ios-sim,x86_64-apple-ios,aarch64-apple-darwin,x86_64-apple-darwin,wasm32-unknown-unknown
# The runner, pinned: dolphin-labs' ci-vm.sh's version and the SHA-256 GitHub publishes for it.
RUNNER_VERSION=2.338.0
RUNNER_SHA256=df4cebda25c86a886ed204e49fee63f5c2e7cec5f447b5c98440a826bbdf9df2

usage() { echo "usage: mac-vm-setup.sh ci|skevh --lan SSH_DEST [--runners|--simulator-slots|--rust]" >&2; exit 2; }
VM=${1:-}
shift || true
LAN=""
RUNNERS=""
SLOTS_ONLY=""
RUST_ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --lan) [ $# -ge 2 ] || usage; LAN=$2; shift 2 ;;
    --runners) RUNNERS=1; shift ;;
    --simulator-slots) SLOTS_ONLY=1; shift ;;
    --rust) RUST_ONLY=1; shift ;;
    *) usage ;;
  esac
done
[[ $LAN =~ ^[A-Za-z0-9][A-Za-z0-9.@_-]*$ ]] || usage
[ -z "$RUNNERS$SLOTS_ONLY$RUST_ONLY" ] || [ "$VM" = ci ] || usage
# At most one of the three modes.
[ $(( ${#RUNNERS} + ${#SLOTS_ONLY} + ${#RUST_ONLY} )) -le 1 ] || usage
case "$VM" in
  # The two guests' RAM stays resident on the host (a guest fills it with its own cache), so together they must
  # leave macOS room beside its ~7 GB wired: at 36 + 16 of 64 GB the host ran out and hung (2026-11-22, #999).
  ci) CPU=9 MEMORY_MB=28672 DISK_GB=200 DIRS='--dir=xcode:$HOME/Applications/Xcode.app:ro' ;;
  skevh) CPU=4 MEMORY_MB=16384 DISK_GB=100 DIRS='' ;;
  *) usage ;;
esac
on_host() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$LAN" "$@"; }

# Existing listeners already search /usr/local/bin. Updating the tools there also works during a job.
# The same install loads (or reloads, when it changed) the admin's hourly disk cleaner LaunchAgent and the
# root coreaudiod check LaunchDaemon.
install_sim_slots() {
  tar -C "$ROOT/scripts/mac" -cf - bin/mac-build ci/sim-slot-shim ci/swift-test-shim ci/ci-disk-clean ci/ci-coreaudio-check install-ci-sim-slots.sh |
    on_host "\$HOME/.local/bin/tart exec -i $VM /bin/zsh -lc $(printf '%q' '
      set -e
      d=$(mktemp -d)
      trap "rm -rf -- \"$d\"" EXIT
      tar -C "$d" -xf -
      sudo /bin/sh "$d/install-ci-sim-slots.sh"
    ')"
}
if [ -n "$SLOTS_ONLY" ]; then
  install_sim_slots
  exit 0
fi

# rustup into the guest's ~/.cargo (where rustup expects itself: one installed elsewhere exits 1 after a toolchain
# install), the pinned channel and targets, and ~/.cargo/bin in .zprofile, where the runners take their PATH.
install_rust() {
  on_host "\$HOME/.local/bin/tart exec $VM /bin/zsh -lc $(printf '%q' "
    set -e
    if [ ! -x \$HOME/.cargo/bin/rustup ]; then
      curl -fsSL -o /tmp/rustup-init https://static.rust-lang.org/rustup/archive/$RUSTUP_VERSION/aarch64-apple-darwin/rustup-init
      echo '$RUSTUP_SHA256  /tmp/rustup-init' | shasum -a 256 -c - >/dev/null
      chmod +x /tmp/rustup-init
      /tmp/rustup-init -y --profile minimal --default-toolchain none --no-modify-path >/dev/null
      rm /tmp/rustup-init
    fi
    grep -q '.cargo/bin' ~/.zprofile 2>/dev/null || echo 'export PATH=\"\$HOME/.cargo/bin:\$PATH\"' >> ~/.zprofile
    export PATH=\"\$HOME/.cargo/bin:\$PATH\"
    rustup toolchain install '$RUST_CHANNEL' --profile minimal --component rustfmt,clippy --target $RUST_TARGETS >/dev/null
    echo \"   \$(rustup run '$RUST_CHANNEL' rustc --version)\"
  ")"
}
if [ -n "$RUST_ONLY" ]; then
  install_rust
  exit 0
fi

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
# No python3 here: a fresh Mac has only Apple's stub, which pops up the developer-tools installer instead of running.
if tart get "$VM" >/dev/null 2>&1; then
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
')" || { echo "growing $VM's disk failed (it can, while the guest is still booting): run this again" >&2; exit 1; }

# The image's sshd host keys are baked into ghcr.io/cirruslabs/macos-tahoe-base: every clone shares them, private
# halves included, so a pinned key would identify nothing. Each guest makes its own once (marker in /etc/ssh).
echo "   $VM's own SSH host keys"
on_host "\$HOME/.local/bin/tart exec $VM /bin/bash -c $(printf '%q' '
  set -e
  m=/etc/ssh/.host-keys-made-by-mac-vm-setup
  if [ -e $m ]; then echo "   already its own"; exit 0; fi
  sudo rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub
  sudo ssh-keygen -A >/dev/null
  sudo touch $m
  echo "   made"
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

echo "runners 1/2 provision: Xcode, build tools and the signing keychain in $VM (the steps of dolphin-labs' ci-vm.sh)"
# dolphin-labs' apps/native/scripts/ci-vm.sh `provision` does this on the MacBook, but it needs python3 on the host,
# and a fresh mini has only Apple's stub (Xcode's own python3 is refused until `sudo xcodebuild -license`). The same
# steps, with nothing run as root on the host.
in_ci() { on_host "\$HOME/.local/bin/tart exec $VM /bin/zsh -lc $(printf '%q' "$1")"; }
# Xcode, the mini's copy, streamed in as a tar through tart exec (the share's virtiofs mount breaks an app bundle's
# symlinks), whole only once its marker is written after the tar ended well; again only when the mini's is another
# version or build.
want=$(on_host 'p=$HOME/Applications/Xcode.app/Contents; /usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" $p/Info.plist; /usr/libexec/PlistBuddy -c "Print :ProductBuildVersion" $p/version.plist' | paste -sd' ' -)
[[ $want =~ ^[0-9][0-9.]*\ [0-9A-Za-z]+$ ]] || { echo "no Xcode version on the mini ($want)" >&2; exit 1; }
have=$(in_ci "cat /Applications/Xcode.app/.ci-vm-copied 2>/dev/null || true")
if [ "$have" != "$want" ]; then
  echo "   copying Xcode $want in"
  in_ci "sudo rm -rf /Applications/.xcode-incoming && sudo mkdir /Applications/.xcode-incoming"
  on_host "tar -C \$HOME/Applications -cf - Xcode.app | \$HOME/.local/bin/tart exec -i $VM /bin/zsh -lc 'sudo tar -C /Applications/.xcode-incoming -xf -'"
  in_ci "set -e; sudo rm -rf /Applications/Xcode.app; sudo mv /Applications/.xcode-incoming/Xcode.app /Applications/Xcode.app; sudo rmdir /Applications/.xcode-incoming; echo '$want' | sudo tee /Applications/Xcode.app/.ci-vm-copied >/dev/null"
fi
in_ci "
  set -e
  sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
  sudo xcodebuild -license accept
  sudo xcodebuild -runFirstLaunch >/dev/null
  echo \"   \$(xcodebuild -version | head -1)\"
"
# The rest from Homebrew, which the base image carries; Node 22 ahead of any other in every login shell (the runners'
# services take their PATH from one).
in_ci "
  set -e
  brew update >/dev/null 2>&1 || true
  brew install node@22 python@3.12 xcodegen cocoapods awscli gh >/dev/null
  grep -q 'node@22/bin' ~/.zprofile 2>/dev/null || echo 'export PATH=\"/opt/homebrew/opt/node@22/bin:\$PATH\"' >> ~/.zprofile
  export PATH=\"/opt/homebrew/opt/node@22/bin:\$PATH\"
  echo \"   node \$(node --version), \$(python3.12 --version), xcodegen \$(xcodegen --version | tail -1), pod \$(pod --version)\"
"
install_rust
# A keychain of the builds' own: the runners run without a login session's unlocked keychain, so signing would ask a
# person ("User interaction is not allowed"). Its password is made here and kept only in the VM.
in_ci "
  set -e
  kc=\$HOME/Library/Keychains/ci.keychain-db
  if [ ! -f \$kc ]; then
    umask 077
    openssl rand -hex 24 > \$HOME/.ci-keychain-password
    security create-keychain -p \"\$(cat \$HOME/.ci-keychain-password)\" ci.keychain
  fi
  # Unlocked first: changing a locked keychain's settings asks for its password in a dialog nobody answers, and
  # tart exec waited on it until killed (2026-11-28, the keychain having locked after its 6 hours).
  security unlock-keychain -p \"\$(cat \$HOME/.ci-keychain-password)\" \$kc
  security set-keychain-settings -lut 21600 \$kc
  security list-keychains -d user -s \$kc \$(security list-keychains -d user | tr -d '\"' | grep -v ci.keychain)
  security default-keychain -d user -s \$kc
  echo '   signing keychain ready'
"

install_sim_slots

echo "runners 2/2 registrations"
on_host "\$HOME/.local/bin/tart exec $VM /bin/zsh -lc $(printf '%q' "
  set -e
  mkdir -p ~/runners && cd ~/runners
  tarball=actions-runner-osx-arm64-$RUNNER_VERSION.tar.gz
  # Kept only once its checksum matches: a cut-off or wrong download is never the file a re-run finds.
  ok() { echo '$RUNNER_SHA256  '\$1 | shasum -a 256 -c - >/dev/null 2>&1; }
  if ! ok \$tarball; then
    rm -f \$tarball \$tarball.part
    curl -fsSLo \$tarball.part https://github.com/actions/runner/releases/download/v$RUNNER_VERSION/\$tarball
    ok \$tarball.part || { rm -f \$tarball.part; echo \"runner \$tarball did not match its pinned sha256\" >&2; exit 1; }
    mv -f \$tarball.part \$tarball
  fi
")"
registered_ids=""
for name in $CI_RUNNERS; do
  labels=$CI_LABELS
  [[ $name == "$QUEUE_RUNNER" ]] && labels=$QUEUE_LABELS
  if [[ $name == "$SIMULATOR_RUNNER" ]]; then
    labels+=",$SIMULATOR_LABEL,$SHARED_SIMULATOR_LABEL"
    # One listener must serve every simulator-testing job across repositories. Refuse a conflicting
    # assignment rather than modifying another operator's runner.
    slot=$(gh api --paginate "orgs/$CI_ORG/actions/runners?per_page=100" --jq "
      .runners[] | any(.labels[]; (.name | ascii_downcase) == \"$SIMULATOR_LABEL\") as \$holds
      | select(.name == \"$name\" or \$holds) | [.id,.name,.status,.busy,\$holds] | @tsv")
    IFS=$'\t' read -r slot_id slot_name slot_status slot_busy slot_labeled <<< "$slot"
    if [[ -n $slot && ( $slot_name != "$name" || $slot == *$'\n'* ) ]]; then
      echo "$SIMULATOR_LABEL has a conflicting registration; check the organization runners" >&2; exit 1
    fi
  fi
  registration="/Users/admin/runners/$name/.runner"
  # A failed SSH or guest command must not be mistaken for a missing registration.
  registered=$(in_ci "if test -f '$registration'; then echo yes; else echo no; fi")
  case "$registered" in yes|no) ;; *) echo "could not check $name's registration" >&2; exit 1 ;; esac
  if [[ $registered == yes ]]; then
    # Its labels are brought to what this script says after every runner is checked, so a conflict found later
    # changes nothing (a registration is not redone for a label).
    id=$(on_host "\$HOME/.local/bin/tart exec $VM cat $registration" |
      python3 -c 'import json, sys; print(json.load(sys.stdin.buffer)["agentId"])')
    [[ $id =~ ^[1-9][0-9]*$ ]] || { echo "no runner id in $registration" >&2; exit 1; }
    registered_ids+=" $name:$id:$labels"
    if [[ $name == "$SIMULATOR_RUNNER" ]]; then
      # The runner's JSON may start with a UTF-8 BOM. Use its actual id even after a rebuild.
      runner_id=$(on_host "\$HOME/.local/bin/tart exec $VM cat $registration" |
        python3 -c 'import json, sys; print(json.load(sys.stdin.buffer)["agentId"])')
      [[ $runner_id =~ ^[1-9][0-9]*$ && $runner_id == "$slot_id" ]] ||
        { echo "GitHub registration does not match $registration" >&2; exit 1; }
      if [[ $slot_labeled == false ]]; then
        gh api -X POST "orgs/$CI_ORG/actions/runners/$runner_id/labels" -f "labels[]=$SIMULATOR_LABEL" --silent
      fi
    fi
    echo "   $name: already registered"
    continue
  fi
  if [[ $name == "$SIMULATOR_RUNNER" && -n $slot ]]; then
    # A rebuilt guest can reclaim its own offline registration. Leave an active listener running.
    [[ $slot_status == offline && $slot_busy == false ]] ||
      { echo "$name is in use; check the organization registration before replacing it" >&2; exit 1; }
  fi
  # A token per runner (they last an hour), on stdin from here on: ssh -> tart exec -> config.sh. Minted first, so a
  # refusal (no admin:org) stops here instead of handing config.sh an empty token.
  token=$(gh api -X POST "orgs/$CI_ORG/actions/runners/registration-token" --jq .token) && [ -n "$token" ] ||
    { echo "no registration token for $CI_ORG: gh needs the admin:org scope (gh auth refresh -h github.com -s admin:org)" >&2; exit 1; }
  printf '%s' "$token" |
    on_host "\$HOME/.local/bin/tart exec -i $VM /bin/zsh -lc $(printf '%q' "
      set -e
      token=\$(cat)
      mkdir -p ~/runners/$name && cd ~/runners/$name
      [ -x ./config.sh ] || tar xzf ../actions-runner-osx-arm64-$RUNNER_VERSION.tar.gz
      ./config.sh --unattended --url https://github.com/$CI_ORG --token \"\$token\" --name $name --labels $labels --work _work --replace >/dev/null
      ./svc.sh install >/dev/null
      ./svc.sh start >/dev/null
    ")"
  unset token
  echo "   $name: registered at $CI_ORG ($labels)"
done

# Each runner registered before: the labels this script manages (CI_LABELS and QUEUE_LABELS) made what its line above
# says, by the API. Only those it should not have are removed and only missing ones added; the simulator labels are
# mini-4's own, kept above. Plain words, not an associative array: the Mac's /bin/bash is 3.2.
managed=$CI_LABELS,$QUEUE_LABELS
for entry in $registered_ids; do
  name=${entry%%:*}; rest=${entry#*:}; id=${rest%%:*}; want=${rest#*:}
  current=$(gh api "orgs/$CI_ORG/actions/runners/$id" --jq '[.labels[].name] | join(",")')
  for label in ${managed//,/ }; do
    [[ ,$want, == *,$label,* ]] && continue
    if [[ ,$current, == *,$label,* ]]; then
      gh api -X DELETE "orgs/$CI_ORG/actions/runners/$id/labels/$label" --silent
    fi
  done
  for label in ${want//,/ }; do
    [[ ,$managed, == *,$label,* ]] || continue
    [[ ,$current, == *,$label,* ]] && continue
    gh api -X POST "orgs/$CI_ORG/actions/runners/$id/labels" -f "labels[]=$label" --silent
  done
  if [[ $name == "$QUEUE_RUNNER" ]]; then echo "   $name: labels $QUEUE_LABELS"; fi
done

# Retired runners: deregistered cleanly, so no job is offered to a runner that is gone and no label is left with only a
# dead holder (every label a workflow asks for stays on mini-3, mini-4 or the MacBook). One running a job is left for a
# re-run once it ends.
retire_pending=""
for name in $RETIRED_RUNNERS; do
  registration="/Users/admin/runners/$name/.runner"
  present=$(in_ci "if test -f '$registration'; then echo yes; else echo no; fi")
  case "$present" in yes|no) ;; *) echo "could not check $name's registration" >&2; exit 1 ;; esac
  if [[ $present == no ]]; then echo "   $name: retired"; continue; fi
  id=$(on_host "\$HOME/.local/bin/tart exec $VM cat $registration" |
    python3 -c 'import json, sys; print(json.load(sys.stdin.buffer)["agentId"])')
  [[ $id =~ ^[1-9][0-9]*$ ]] || { echo "no runner id in $registration" >&2; exit 1; }
  if [[ $(gh api "orgs/$CI_ORG/actions/runners/$id" --jq .busy 2>/dev/null || echo gone) == true ]]; then
    echo "   $name: running a job; run this again to retire it once that ends" >&2
    retire_pending=1
    continue
  fi
  token=$(gh api -X POST "orgs/$CI_ORG/actions/runners/remove-token" --jq .token) && [ -n "$token" ] ||
    { echo "no removal token for $CI_ORG: gh needs the admin:org scope" >&2; exit 1; }
  # A failed removal in the guest (GitHub unreachable from it) must not stop the run: the registration is then deleted
  # from here, and the other retired runner is still handled. The directory goes either way, so a later run does not
  # retry a removal that already happened.
  if ! printf '%s' "$token" |
    on_host "\$HOME/.local/bin/tart exec -i $VM /bin/zsh -lc $(printf '%q' "
      set -e
      token=\$(cat)
      cd ~/runners/$name
      ./svc.sh stop >/dev/null 2>&1 || true
      ./svc.sh uninstall >/dev/null 2>&1 || true
      rc=0
      ./config.sh remove --token \"\$token\" >/dev/null || rc=\$?
      cd .. && rm -rf $name
      exit \$rc
    ")"; then
    echo "   $name: config.sh remove failed in the guest; deregistering from the host" >&2
  fi
  unset token
  # A registration config.sh could not remove: removed from here.
  if gh api "orgs/$CI_ORG/actions/runners/$id" --jq .id >/dev/null 2>&1; then
    gh api -X DELETE "orgs/$CI_ORG/actions/runners/$id" --silent
  fi
  echo "   $name: retired"
done
[[ -z $retire_pending ]] || exit 4
