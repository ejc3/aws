#!/usr/bin/env bash
#
# simulator-listeners -- keep the shared `ios-simulator` label on exactly the runners that take iPhone simulator test
# jobs across dolphin-labs-hq, one in each Mac's CI VM, and on no other runner.
#
#   scripts/simulator-listeners.sh            check, and add or remove the label where it is wrong
#   scripts/simulator-listeners.sh --dry-run  print what it would change, change nothing
#
# The listeners (owner, 2026-11-27: "MacBook Pro can be used for Mac testing", dolphin-labs #2650):
#   mini-4               the Mac mini's CI VM `ci` (scripts/mac-vm-setup.sh), which also keeps `mini-simulator`
#   macbook-kidsports-1  the MacBook's pool VM `kidsports-ci` (kid-sports-highlights scripts/ci-vm.sh, under
#                        TART_HOME=/Volumes/Scratch/.tart-kidsports), its own user `admin`, never the owner's session
#   air-ci-1             the MacBook Air's VM `air-ci` (dolphin-labs apps/native/scripts/ci-vm.sh, GWW_CI_VM=air-ci,
#                        6 cores / 18 GB; owner, 2026-11-27: the Air is not used by anyone, so the VM takes most of it)
# Each runner runs one job at a time, so three listeners take three simulator jobs at once, one on each Mac.
#
# It only labels: it never registers, replaces or removes a runner, and needs no registration token. A listener that
# is not registered stops it with an error (register it where its VM is made). Re-registering a runner with
# `config.sh --replace` resets its labels, so run this again after one (mac-vm-setup.sh --runners gives mini-4 the
# label itself). Needs a gh login with the admin:org scope (gh auth refresh -h github.com -s admin:org).
set -euo pipefail

CI_ORG=dolphin-labs-hq
LABEL=ios-simulator
LISTENERS=(mini-4 macbook-kidsports-1 air-ci-1)

DRY=""
case "${1:-}" in
  "") ;;
  --dry-run) DRY=1 ;;
  *) echo "usage: simulator-listeners.sh [--dry-run]" >&2; exit 2 ;;
esac

# id, name, and whether it holds the label, one runner a line.
runners=$(gh api --paginate "orgs/$CI_ORG/actions/runners?per_page=100" --jq "
  .runners[] | [.id, .name, any(.labels[]; (.name | ascii_downcase) == \"$LABEL\")] | @tsv")

is_listener() { local n; for n in "${LISTENERS[@]}"; do [[ $n == "$1" ]] && return 0; done; return 1; }
act() { # act <what> <gh api args...>
  echo "   $1"
  [ -n "$DRY" ] || gh api "${@:2}" --silent
}

# Every listener first, so a missing one changes nothing.
for name in "${LISTENERS[@]}"; do
  count=$(awk -F'\t' -v n="$name" '$2 == n' <<<"$runners" | wc -l | tr -d ' ')
  [[ $count == 1 ]] || { echo "$name is registered $count times in $CI_ORG, not once: register it where its VM is made" >&2; exit 1; }
done

changed=0
while IFS=$'\t' read -r id name holds; do
  [[ -n $id ]] || continue
  [[ $id =~ ^[1-9][0-9]*$ ]] || { echo "unexpected runner id $id" >&2; exit 1; }
  if is_listener "$name" && [[ $holds == false ]]; then
    act "$name: add $LABEL" -X POST "orgs/$CI_ORG/actions/runners/$id/labels" -f "labels[]=$LABEL"
    changed=1
  elif ! is_listener "$name" && [[ $holds == true ]]; then
    act "$name: remove $LABEL (not a listener)" -X DELETE "orgs/$CI_ORG/actions/runners/$id/labels/$LABEL"
    changed=1
  fi
done <<<"$runners"
[[ $changed == 1 ]] || echo "   $LABEL: on ${LISTENERS[*]} and no other runner, as it should be"
