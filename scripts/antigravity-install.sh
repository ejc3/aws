#!/bin/bash
# Bootstrap agy as the account that will run it. Google's CLI self-updates on use.
# Release URLs and SHA-512 digests come from the manifests in https://antigravity.google/cli/install.sh.
set -euo pipefail
[[ $(id -u) != 0 ]] || { echo "Run the Antigravity installer as the intended user." >&2; exit 1; }
guide=${AGY_MODEL_GUIDE:-}
if [[ ${1:-} == --binary-only ]]; then
  # Control-plane instructions are installed by an administrator after review.
  guide=""
elif [[ -z $guide && -n ${BASH_SOURCE[0]:-} ]]; then
  guide=$(cat "$(dirname "${BASH_SOURCE[0]}")/antigravity-models.md")
fi
cd "$HOME"
bin=$HOME/.local/bin
version=1.5.18
base=https://storage.googleapis.com/antigravity-public/antigravity-cli/1.5.18-6386224290725888
case "$(uname -sm)" in
  'Linux aarch64')
    archive=linux-arm/cli_linux_arm64.tar.gz
    digest=6c59cbce5a7632083dd4ef594296e52ce0abdc12f8b4ecb55523e093748cf178f8f8383d7e51e9ed7859ea6e46c83a43a7b46364b0e033dd13afb24d6bfbe4c8 ;;
  'Linux x86_64')
    archive=linux-x64/cli_linux_x64.tar.gz
    digest=ef379ec869719112f696e8c7cfddf776618712e58fc38e53df80801d6528abb4677782c7c8778f9c09c9123fa44c2c32675d228c06f2a8f7a8f4214e75f5bb26 ;;
  'Darwin arm64')
    archive=darwin-arm/cli_mac_arm64.tar.gz
    digest=d10e124f5eb4da94ebb6c494cd2e7b38744f9f66e8e87145d2cbad1cbd3c4160fd88a79cd4220dfce7c9da0ddead652346bcc839914575f6447438ae8d1d5dcc ;;
  'Darwin x86_64')
    archive=darwin-x64/cli_mac_x64.tar.gz
    digest=c6ab39b41511ee0951223b6c178e0571e83105800701041d9abc520161ef0381724425ec6e9273fbc609411941c8986ed9aafd711b1a3f86857c705efe3ac2a0 ;;
  *) echo "Antigravity does not have a pinned build for $(uname -sm)." >&2; exit 1 ;;
esac

stage=""
installed=$("$bin/agy" --version 2>/dev/null) || installed=""
if [[ ! $installed =~ ^[0-9]+\.[0-9]+\.[0-9]+([+-][a-zA-Z0-9.+-]+)?$ ]]; then
  mkdir -p "$bin"
  stage=$(mktemp -d "$bin/.agy-install.XXXXXXXX")
  trap 'rm -rf -- "$stage"' EXIT
  curl --proto '=https' --proto-redir '=https' -fsSL --retry 2 -o "$stage/agy.tar.gz" "$base/$archive"
  if [[ $archive = darwin-* ]]; then
    actual=$(shasum -a 512 "$stage/agy.tar.gz")
  else
    actual=$(sha512sum "$stage/agy.tar.gz")
  fi
  [[ ${actual%% *} = "$digest" ]] || { echo "Antigravity $archive SHA-512 mismatch." >&2; exit 1; }
  tar -xzOf "$stage/agy.tar.gz" antigravity > "$stage/agy"
  chmod 700 "$stage/agy"
  installed=$("$stage/agy" --version)
  [[ $installed = "$version" ]] || { echo "Expected Antigravity $version, got $installed." >&2; exit 1; }
fi

# Only bootstrap a new settings file. The live CLI can atomically replace it
# from /config, including when the user returns to the stricter review default.
if [[ ${1:-} != --binary-only ]]; then
  (
    umask 077
    dir=$HOME/.gemini/antigravity-cli
    mkdir -p "$dir"
    cd "$dir"
    # The hourly installer and a manual install can overlap on Macs.
    perl - "$stage" "$bin/agy" <<'SEED'
use strict;
use warnings;
use Errno qw(EEXIST);
use Fcntl qw(:flock :DEFAULT O_NOFOLLOW);
use File::Temp qw(tempfile);
sysopen(my $lock, '.fleet-permission.lock', O_WRONLY | O_CREAT | O_NOFOLLOW, 0600) or die "Open preferences lock: $!";
flock($lock, LOCK_EX) or die "Lock preferences: $!";
my $stamp = '.fleet-permission-default-v1';
exit 0 if -e $stamp || -l $stamp;
my ($stage, $binary) = @ARGV;
if (length($stage) && !-e $binary && !-l $binary && !-e 'settings.json' && !-l 'settings.json') {
    my ($out, $tmp) = tempfile('.agy-settings-XXXXXXXX', DIR => '.', UNLINK => 1);
    print {$out} qq({"toolPermission":"always-proceed"}\n) or die "Write settings: $!";
    close($out) or die "Finish settings: $!";
    link($tmp, 'settings.json') or $! == EEXIST or die "Publish settings: $!";
}
sysopen(my $done, $stamp, O_WRONLY | O_CREAT | O_EXCL, 0600) or die "Record provisioned profile: $!";
SEED
  )
fi

if [[ -n $stage ]]; then
  mv -f "$stage/agy" "$bin/agy"
fi

# SSH commands run the fleet's zsh noninteractively, which reads .zshenv but skips .zshrc.
# Test with sshd's system PATH so an interactive caller cannot mask a missing entry.
if ! env PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin zsh -c '[[ ${commands[agy]:-} -ef $HOME/.local/bin/agy ]]'; then
  line='[[ ! -x $HOME/.local/bin/agy || ${commands[agy]:-} -ef $HOME/.local/bin/agy ]] || path=("$HOME/.local/bin" ${path:#"$HOME/.local/bin"})'
  grep -Fqx -- "$line" "$HOME/.zshenv" 2>/dev/null || printf '\n%s\n' "$line" >> "$HOME/.zshenv"
  env PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin zsh -c '[[ ${commands[agy]:-} -ef $HOME/.local/bin/agy ]]' \
    || { echo "Noninteractive zsh cannot find $bin/agy. Check ~/.zshenv." >&2; exit 1; }
fi
printf 'Antigravity %s installed at %s/agy\n' "$installed" "$bin"

if [[ -n $guide ]]; then
  rules=$HOME/.gemini/config/rules
  mkdir -p "$rules"
  rule=$(mktemp "$rules/.ejc3-models.XXXXXXXX")
  printf '%s\n' '---' 'trigger: always_on' '---' "$guide" > "$rule"
  if cmp -s "$rule" "$rules/ejc3-antigravity-models.md"; then
    rm -f "$rule"
  else
    mv -f "$rule" "$rules/ejc3-antigravity-models.md"
  fi
fi
