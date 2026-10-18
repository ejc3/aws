#!/bin/sh
# set-zshenv.sh FILE: make FILE (~/.zshenv) end with the managed block that puts ~/.local/bin first on
# PATH, so every zsh, non-interactive SSH sessions included, finds the swift/xcodebuild/xcrun shims and
# the other agent tools before /usr/bin. The block is authoritative: whatever FILE does to PATH before it,
# the block runs last. An earlier copy of the block is removed and the current one appended, so a rerun
# leaves an up-to-date FILE unchanged. Installed by ejc3/aws scripts/mac-setup.sh (scripts/mac/install.zsh).
set -eu
f=${1:?usage: set-zshenv.sh FILE}
begin='# >>> mac-setup >>>'
end='# <<< mac-setup <<<'
block="$begin (ejc3/aws scripts/mac-setup.sh; rewritten on every run, keep it last)
typeset -U path
path=(\"\$HOME/.local/bin\" \$path)
export PATH
$end"
[ -e "$f" ] || : > "$f"
tmp="$f.mac-setup.$$"
# Everything outside the block, then the block once at the end.
awk -v b="$begin" -v e="$end" '
  index($0, b) == 1 { skip = 1; next }
  skip && index($0, e) == 1 { skip = 0; next }
  !skip { print }
' "$f" > "$tmp"
# No blank-line pile-up at the end before the block.
while [ -s "$tmp" ] && [ -z "$(tail -n 1 "$tmp")" ]; do sed -i.bak '$d' "$tmp" && rm -f "$tmp.bak"; done
{ [ -s "$tmp" ] && printf '\n'; printf '%s\n' "$block"; } >> "$tmp"
if cmp -s "$tmp" "$f"; then rm -f "$tmp"; else cat "$tmp" > "$f"; rm -f "$tmp"; fi
