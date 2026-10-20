#!/bin/bash
# terraform-run-lock-install.sh WRAPPER_FILE: put the run-lock wrapper in front of terraform on this host (#408).
#
# Run as root by the SSM association in terraform-run-lock.tf, when the wrapper changes and daily. The wrapper goes AT
# /usr/local/bin/terraform, the path every shell already resolves (zsh caches command paths, so a wrapper earlier on
# PATH would be bypassed by a shell that already ran terraform), and the real binary lives in
# /usr/local/lib/terraform-run-lock/terraform. Whatever real binary is found at /usr/local/bin/terraform (a first install,
# or an upgrade that overwrote the wrapper) is moved there first, so the daily run repairs an upgrade on its own.
# Idempotent; every replacement is a rename, so a running Terraform keeps the file it started from. Restarts nothing.
# BIN_DIR, LIB_DIR, LOCK_DIR and CACHE_DIR exist for the tests.
set -euo pipefail
src=${1:?usage: terraform-run-lock-install.sh WRAPPER_FILE}
bin=${BIN_DIR:-/usr/local/bin} lib=${LIB_DIR:-/usr/local/lib/terraform-run-lock} lockdir=${LOCK_DIR:-/var/lib/terraform-run-lock}
cache=${CACHE_DIR:-/var/cache/terraform-plugins}
marker=TERRAFORM-RUN-LOCK-WRAPPER

grep -q "$marker" "$src" || { echo "terraform-run-lock: $src is not the wrapper" >&2; exit 1; }
bash -n "$src"
install -d -m 0755 "$lib" "$lockdir"
# The lock file exists before anyone needs it, owned by root and writable by every account, so opening it never has to
# create a file.
[ -e "$lockdir/lock" ] || install -m 0666 /dev/null "$lockdir/lock"
chmod 0666 "$lockdir/lock"
# The shared provider cache the wrapper points TF_PLUGIN_CACHE_DIR at: the administrators' group (adm on Ubuntu) may write
# it, setgid so new entries keep the group. Terraform checks a cached provider against the lock file's checksums before
# using it, so a tampered entry is refused, not run.
# Setgid keeps the group but not group write: Terraform makes each provider's directories 0755, so the next administrator
# could not add a version beside the first one's. A default ACL keeps everything made in the cache group-writable, and the
# wrapper's umask stays as it is (plan files must not become group-readable). setfacl comes from the acl package, which
# the Ubuntu jumpbox image lacks; root installs it here.
install -d -m 2775 "$cache"
chgrp adm "$cache" 2>/dev/null || true
if ! command -v setfacl >/dev/null && [ "$(id -u)" = 0 ] && command -v apt-get >/dev/null; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q acl >/dev/null 2>&1 || echo "terraform-run-lock: could not install acl" >&2
fi
if command -v setfacl >/dev/null; then
  chgrp -R adm "$cache" 2>/dev/null || true
  setfacl -R -m g:adm:rwX,m::rwX "$cache" && find "$cache" -type d -exec setfacl -d -m g:adm:rwx,m::rwx {} + ||
    echo "terraform-run-lock: could not set the cache's ACL; other administrators may not add providers to it" >&2
else
  echo "terraform-run-lock: no setfacl; the provider cache is shared only as far as setgid and modes allow" >&2
fi

if [ -f "$bin/terraform" ] && ! grep -aq "$marker" "$bin/terraform"; then
  install -m 0755 "$bin/terraform" "$lib/terraform.new" && mv -f "$lib/terraform.new" "$lib/terraform"
  echo "terraform-run-lock: moved the real terraform to $lib/terraform"
fi
[ -x "$lib/terraform" ] || { echo "terraform-run-lock: no real terraform at $lib/terraform; not installing" >&2; exit 1; }

if ! cmp -s "$src" "$bin/terraform"; then
  install -m 0755 "$src" "$bin/terraform.new" && mv -f "$bin/terraform.new" "$bin/terraform"
  echo "terraform-run-lock: installed the wrapper at $bin/terraform"
fi
TF_RUN_LOCK_REAL="$lib/terraform" "$bin/terraform" version | head -1
