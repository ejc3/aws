#!/bin/bash
# admin-tmux-tclaude.sh -- install the pinned tmux-scroll and t-claude for the CURRENT user.
#
# Run by terraform_data.admin_tmux_tclaude (tmux-scroll.tf) on the jumpboxes, as ubuntu,
# through SSM. Inputs come from Terraform:
#   TMUX_SCROLL_TAG     ejc3/tmux release tag
#   TMUX_SCROLL_SHA256  sha256 of that release's tmux-scroll-aarch64.tar.gz
#   TCLAUDE_REF         ejc3/t-claude commit
#
# Writes only ~/.local/bin/tmux-scroll, ~/.local/bin/nosync-wrap and ~/.config/t-claude.zsh.
# All three are downloaded and checked BEFORE any is replaced: they move together under one
# pin, so a failed download leaves all three at their previous versions.
set -euo pipefail
: "${TMUX_SCROLL_TAG:?}" "${TMUX_SCROLL_SHA256:?}" "${TCLAUDE_REF:?}"
cd "$HOME"

arch=$(uname -m)
if [ "$arch" != aarch64 ]; then
  echo "admin-tmux-tclaude: no pinned tmux-scroll build for $arch" >&2
  exit 1
fi
case "$TCLAUDE_REF" in *[!0-9a-f]*|"") echo "admin-tmux-tclaude: TCLAUDE_REF must be a commit sha" >&2; exit 1 ;; esac

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
bin="$HOME/.local/bin"

# ---- fetch and check everything first
url="https://github.com/ejc3/tmux/releases/download/$TMUX_SCROLL_TAG/tmux-scroll-$arch.tar.gz"
curl -fsSL --retry 3 -o "$tmp/tmux-scroll.tgz" "$url"
if ! echo "$TMUX_SCROLL_SHA256  $tmp/tmux-scroll.tgz" | sha256sum -c --quiet -; then
  echo "admin-tmux-tclaude: $url does not match the pinned sha256; nothing changed" >&2
  exit 1
fi
tar xzf "$tmp/tmux-scroll.tgz" -C "$tmp" tmux-scroll
grep -qa scroll-replay "$tmp/tmux-scroll" || { echo "admin-tmux-tclaude: build lacks scroll-replay; nothing changed" >&2; exit 1; }
chmod 755 "$tmp/tmux-scroll"
"$tmp/tmux-scroll" -V >/dev/null

raw="https://raw.githubusercontent.com/ejc3/t-claude/$TCLAUDE_REF"
curl -fsSL --retry 3 -o "$tmp/t-claude.zsh" "$raw/t-claude.zsh"
curl -fsSL --retry 3 -o "$tmp/nosync-wrap" "$raw/nosync-wrap"
zsh -n "$tmp/t-claude.zsh"
python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$tmp/nosync-wrap"

# ---- then install
# Replace $2 with $1 by rename, keeping the old copy as .prev. Renaming, not overwriting,
# because a running binary cannot be written in place ("Text file busy"). A file that already
# has the right contents still gets its mode set, so a copy that lost its execute bit is fixed.
put() {
  local new=$1 dest=$2 mode=$3
  mkdir -p "$(dirname "$dest")"
  if [ -e "$dest" ] && cmp -s "$new" "$dest"; then
    chmod "$mode" "$dest"
    echo "admin-tmux-tclaude: $dest already current"
    return 0
  fi
  [ -e "$dest" ] && mv -f "$dest" "$dest.prev"
  if ! install -m "$mode" "$new" "$dest"; then
    [ -e "$dest.prev" ] && mv -f "$dest.prev" "$dest"
    echo "admin-tmux-tclaude: could not install $dest; restored the previous copy" >&2
    return 1
  fi
  echo "admin-tmux-tclaude: installed $dest"
}
put "$tmp/tmux-scroll" "$bin/tmux-scroll" 755
put "$tmp/t-claude.zsh" "$HOME/.config/t-claude.zsh" 644
put "$tmp/nosync-wrap" "$bin/nosync-wrap" 755

echo "admin-tmux-tclaude: $("$bin/tmux-scroll" -V), t-claude ${TCLAUDE_REF:0:12}"
