#!/bin/bash
# TERRAFORM-RUN-LOCK-WRAPPER (the installer recognises this file by that marker)
# terraform (wrapper): one Terraform run at a time on this host (#408).
#
# Installed on the jumpboxes AT /usr/local/bin/terraform, the path every shell already resolves (zsh caches command
# paths), with the real binary moved to /usr/local/lib/terraform-run-lock/terraform (scripts/terraform-run-lock-install.sh,
# run by an SSM association in terraform-run-lock.tf, daily and on change). Two plans at once on a 2-vCPU, 8 GB jumpbox ran it out of memory
# (73 provider processes, 2026-10-12) and staled an apply (2026-10-11). Every command that loads providers or touches
# state takes one host-wide lock and holds it until Terraform exits; the rest pass straight through.
#
#   waits for the lock:   init plan apply destroy import refresh state taint untaint force-unlock test console validate,
#                         providers lock, providers mirror, providers schema (they rewrite .terraform.lock.hcl,
#                         download providers or start every provider plugin;
#                         console holds the state lock, and console -plan plans; validate starts provider plugins)
#   passes through:       everything else (fmt, version, providers, output, show, graph, get, -help)
#
#   TF_RUN_LOCK_NOWAIT=1  do not wait: exit 75 at once if another run holds the lock (the Stop hook uses this)
#   TF_RUN_LOCK_WAIT=N    seconds to wait before giving up with 75 (default 1800)
#   TF_RUN_LOCK_FILE      the lock file (default /var/lib/terraform-run-lock/lock, made by the installer)
#   TF_RUN_LOCK_REAL      the real terraform (default /usr/local/lib/terraform-run-lock/terraform, else the first
#                         terraform on PATH that is not this wrapper)
#
# The lock is held by util-linux flock(1) with -o: flock takes it, closes its descriptor before starting Terraform, and
# holds it until Terraform exits, so no descendant of Terraform (a provider plugin that outlives a killed Terraform)
# ever holds it. Killing flock releases it too. A probe on fd 9 first, only to say who holds a busy lock. It never adds
# -lock=false: Terraform's own state lock is a separate, cross-host guard.
set -u

self=$(readlink -f "${BASH_SOURCE[0]}")

real=${TF_RUN_LOCK_REAL:-}
[ -z "$real" ] && [ -x /usr/local/lib/terraform-run-lock/terraform ] && real=/usr/local/lib/terraform-run-lock/terraform
if [ -z "$real" ]; then
  IFS=: read -r -a dirs <<<"${PATH:-}"
  for d in "${dirs[@]}"; do
    c="$d/terraform"
    if [ -x "$c" ] && [ ! -d "$c" ] && [ "$(readlink -f "$c")" != "$self" ]; then real=$c; break; fi
  done
fi
if [ -z "$real" ] || [ ! -x "$real" ] || [ "$(readlink -f "$real")" = "$self" ]; then
  echo "terraform-run-lock: no real terraform found on PATH (set TF_RUN_LOCK_REAL)" >&2
  exit 127
fi

# The subcommand is the first argument that is not a global option (-chdir=DIR comes before it).
sub="" sub2=""
for a in "$@"; do
  case "$a" in
    -chdir=*) [ -z "$sub" ] && continue ;;
  esac
  if [ -z "$sub" ]; then
    case "$a" in -*) break ;; *) sub=$a; continue ;; esac
  fi
  case "$a" in -*) continue ;; *) sub2=$a; break ;; esac
done

case "$sub $sub2" in
  "init "* | "plan "* | "apply "* | "destroy "* | "import "* | "refresh "* | "state "* | "taint "* | "untaint "* | \
    "force-unlock "* | "test "* | "console "* | "validate "* | "providers lock" | "providers mirror" | "providers schema") ;;
  *) exec "$real" "$@" ;;
esac

lock=${TF_RUN_LOCK_FILE:-/var/lib/terraform-run-lock/lock}
# Not installed host-wide (a laptop, a fresh box): fall back to a per-user lock, still one run at a time per account.
[ -e "$lock" ] || [ -n "${TF_RUN_LOCK_FILE:-}" ] || lock="${XDG_RUNTIME_DIR:-/tmp}/terraform-run-lock-$(id -u)"
if ! { exec 9<"$lock"; } 2>/dev/null && ! { exec 9>>"$lock"; } 2>/dev/null; then
  echo "terraform-run-lock: cannot open $lock" >&2
  exit 1
fi

wait=${TF_RUN_LOCK_WAIT:-1800}
mode=(-w "$wait")
if ! flock -n 9; then
  holder=""
  target=$(readlink -f "$lock")
  for fd in /proc/[0-9]*/fd/*; do
    [ "$(readlink "$fd" 2>/dev/null)" = "$target" ] || continue
    pid=${fd#/proc/}; pid=${pid%%/*}
    [ "$pid" = "$$" ] && continue
    holder="pid $pid ($(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | cut -c1-120))"
    break
  done
  holder=${holder:-another process}
  if [ -n "${TF_RUN_LOCK_NOWAIT:-}" ]; then
    echo "terraform-run-lock: a Terraform run is in progress, by $holder; not waiting (TF_RUN_LOCK_NOWAIT)" >&2
    exit 75
  fi
  echo "terraform-run-lock: waiting for the Terraform run by $holder (up to ${wait}s)" >&2
fi
exec 9<&- # the probe's descriptor (and its lock, if it got one) goes; flock below takes the lock for the run
[ -n "${TF_RUN_LOCK_NOWAIT:-}" ] && mode=(-n)

flock -o "${mode[@]}" -E 75 "$lock" "$real" "$@"
rc=$?
if [ "$rc" -eq 75 ] && ! flock -n "$lock" true 2>/dev/null; then
  echo "terraform-run-lock: still busy; giving up" >&2
fi
exit "$rc"
