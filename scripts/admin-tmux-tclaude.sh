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
# Every download is checked before it replaces anything, so a failure leaves the current copy.
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
mkdir -p "$bin" "$HOME/.config"

# Replace $2 with $1 by rename, keeping the old copy as .prev. Renaming, not overwriting,
# because a running binary cannot be written in place ("Text file busy").
put() {
  local new=$1 dest=$2 mode=$3
  if [ -e "$dest" ] && cmp -s "$new" "$dest"; then
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

# tmux-scroll
url="https://github.com/ejc3/tmux/releases/download/$TMUX_SCROLL_TAG/tmux-scroll-$arch.tar.gz"
curl -fsSL --retry 3 -o "$tmp/tmux-scroll.tgz" "$url"
if ! echo "$TMUX_SCROLL_SHA256  $tmp/tmux-scroll.tgz" | sha256sum -c --quiet -; then
  echo "admin-tmux-tclaude: $url does not match the pinned sha256; nothing changed" >&2
  exit 1
fi
tar xzf "$tmp/tmux-scroll.tgz" -C "$tmp" tmux-scroll
grep -qa scroll-replay "$tmp/tmux-scroll" || { echo "admin-tmux-tclaude: build lacks scroll-replay" >&2; exit 1; }
"$tmp/tmux-scroll" -V >/dev/null
put "$tmp/tmux-scroll" "$bin/tmux-scroll" 755

# t-claude and nosync-wrap, at the pinned commit
raw="https://raw.githubusercontent.com/ejc3/t-claude/$TCLAUDE_REF"
curl -fsSL --retry 3 -o "$tmp/t-claude.zsh" "$raw/t-claude.zsh"
curl -fsSL --retry 3 -o "$tmp/nosync-wrap" "$raw/nosync-wrap"
zsh -n "$tmp/t-claude.zsh"
python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$tmp/nosync-wrap"
put "$tmp/t-claude.zsh" "$HOME/.config/t-claude.zsh" 644
put "$tmp/nosync-wrap" "$bin/nosync-wrap" 755

echo "admin-tmux-tclaude: $("$bin/tmux-scroll" -V), t-claude ${TCLAUDE_REF:0:12}"
