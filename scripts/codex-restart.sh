#!/usr/bin/env bash
#
# codex-restart -- see, and deliberately apply, a Codex update. The Codex analogue of
# `t-claude --restart`.
#
#   codex-restart                      status: per account, the version its daemon is RUNNING, the
#                                      version INSTALLED, what is connected and what is running
#   codex-restart --restart            restart the daemon of every account that is out of date
#   codex-restart --restart --all      restart even the ones already current
#   codex-restart --user NAME ...      only that account (root or sudo required for another user)
#   codex-restart --force              restart although commands are running under the daemon
#
# WHY THIS EXISTS. The installer swaps ~/.codex/packages/standalone/current to the new release, but a
# running daemon keeps the binary it started with (Sep 28's daemon was still on 0.154.0 with 0.159.2
# installed). Nothing picks the update up until that daemon restarts, and a restart interrupts the
# turns it is running, so this never happens on its own: updating is automatic, restarting is a
# decision. This is the one place that decision is made.
#
# WHAT A RESTART TOUCHES. Only that account's `codex app-server --remote-control` daemon and
# what it started (`codex remote-control stop`, then `start`). Where the account has a
# codex-rc@<user> unit (the metal boxes, nextjs-dev) it goes through systemd, so the unit's state
# stays true; otherwise through the CLI directly. Claude, tmux and t-claude are never touched. Apps
# connected through `codex app-server proxy` reconnect; a turn that was mid-flight is lost, which
# is why it refuses while commands are running unless --force.
#
# Records go to ${XDG_STATE_HOME:-~/.local/state}/codex-restart/restart.txt.
set -uo pipefail

PROC=${CODEX_RESTART_PROC:-/proc}   # tests point this at a fake /proc
SYSTEMCTL=${CODEX_RESTART_SYSTEMCTL:-systemctl}   # tests point this at nothing
WAIT=${CODEX_RESTART_WAIT:-40}      # seconds to wait for the old daemon to exit / the new one to appear

restart=0 all=0 force=0 only=""
while [ $# -gt 0 ]; do
  case "$1" in
    --restart) restart=1 ;;
    --all) all=1 ;;
    --force) force=1 ;;
    --user) only=${2:-}; [ -n "$only" ] || { echo "codex-restart: --user needs a name" >&2; exit 2; }; shift ;;
    -h|--help) sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "usage: codex-restart [--restart [--all] [--force]] [--user NAME]" >&2; exit 2 ;;
  esac
  shift
done

release_of() {   # .../releases/0.159.2-aarch64-unknown-linux-musl/... -> 0.159.2
  sed -nE 's#.*/releases/([0-9][0-9A-Za-z.+-]*)-[a-z0-9_]+-unknown-linux-[a-z]+.*#\1#p' <<< "$1"
}

daemons() {   # prints "pid uid" for each remote-control app-server daemon
  local d args uid
  for d in "$PROC"/[0-9]*; do
    [ -r "$d/cmdline" ] || continue
    args=$({ tr '\0' ' ' < "$d/cmdline"; } 2>/dev/null) || continue
    case "$args" in
      *"app-server --remote-control"*) ;;
      *) continue ;;
    esac
    uid=$({ awk '/^Uid:/{print $2}' "$d/status"; } 2>/dev/null)
    echo "${d##*/} ${uid:-?}"
  done
}

running_version() { release_of "$(readlink "$PROC/$1/exe" 2>/dev/null)"; }

installed_version() {   # $1 = home dir
  release_of "$(readlink -f "$1/.codex/packages/standalone/current" 2>/dev/null)"
}

descendants() {   # every pid below $1
  local d kid
  for d in "$PROC"/[0-9]*; do
    kid=${d##*/}
    [ "$({ awk '/^PPid:/{print $2}' "$d/status"; } 2>/dev/null)" = "$1" ] && { echo "$kid"; descendants "$kid"; }
  done
}

commands_under() {   # running work started by the daemon: anything that is not Codex's own helpers
  local p n=0 c
  for p in $(descendants "$1"); do
    c=$(cat "$PROC/$p/comm" 2>/dev/null) || continue
    case "$c" in codex|codex-code-mode*) ;; *) n=$((n + 1)) ;; esac   # comm is cut to 15 chars: codex-code-mode
  done
  echo "$n"
}

clients_of() {   # apps attached through the proxy, for this account
  local d n=0 uid
  uid=$1
  for d in "$PROC"/[0-9]*; do
    [ -r "$d/cmdline" ] || continue
    case "$({ tr '\0' ' ' < "$d/cmdline"; } 2>/dev/null)" in *"app-server proxy"*) ;; *) continue ;; esac
    [ "$({ awk '/^Uid:/{print $2}' "$d/status"; } 2>/dev/null)" = "$uid" ] && n=$((n + 1))
  done
  echo "$n"
}

as_user() {   # as_user NAME cmd...   (no sudo when already that user)
  local u=$1; shift
  if [ "$(id -un)" = "$u" ]; then "$@"; else sudo -n -u "$u" -H "$@"; fi
}

restart_one() {   # restart_one NAME HOME OLDPID UID
  local u=$1 home=$2 old=$3 uid=$4 cx="$2/.local/bin/codex" i new
  if command -v "$SYSTEMCTL" >/dev/null 2>&1 && [ "$("$SYSTEMCTL" is-active "codex-rc@$u" 2>/dev/null)" = active ]; then
    if [ "$(id -u)" = 0 ]; then "$SYSTEMCTL" restart "codex-rc@$u"; else sudo -n "$SYSTEMCTL" restart "codex-rc@$u"; fi || return 1
  else
    as_user "$u" "$cx" remote-control stop >/dev/null 2>&1 || true
    for i in $(seq 1 "$WAIT"); do [ -d "$PROC/$old" ] || break; sleep 1; done
    if [ -d "$PROC/$old" ] && kill -0 "$old" 2>/dev/null; then
      echo "  the old daemon ($old) did not exit within ${WAIT}s; not forcing it. Look at it, then re-run." >&2
      return 1
    fi
    as_user "$u" "$cx" remote-control start >/dev/null 2>&1 || true
  fi
  for i in $(seq 1 "$WAIT"); do   # a new daemon, not the old pid
    new=$(daemons | awk -v uid="$uid" '$2 == uid {print $1}' | grep -vx "$old" | head -1)
    [ -n "$new" ] && { echo "$new"; return 0; }
    sleep 1
  done
  return 1
}

record() {
  local dir="${XDG_STATE_HOME:-$HOME/.local/state}/codex-restart"
  mkdir -p "$dir" 2>/dev/null && printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$dir/restart.txt" 2>/dev/null || true
}

found=0 stale=0 failed=0
while read -r pid uid; do
  [ -n "$pid" ] || continue
  user=$(getent passwd "$uid" | cut -d: -f1); home=${CODEX_RESTART_HOME:-$(getent passwd "$uid" | cut -d: -f6)}
  [ -n "$user" ] || continue
  [ -z "$only" ] || [ "$only" = "$user" ] || continue
  if [ "$(id -u)" != 0 ] && [ "$(id -un)" != "$user" ] && ! sudo -n true 2>/dev/null; then
    echo "$user: not visible without sudo, skipped"; continue
  fi
  found=$((found + 1))
  run=$(running_version "$pid"); inst=$(installed_version "$home")
  cmds=$(commands_under "$pid"); clients=$(clients_of "$uid")
  if [ -n "$run" ] && [ "$run" = "$inst" ]; then state="current"; else state="RESTART NEEDED"; stale=$((stale + 1)); fi
  printf '%s: running %s, installed %s -> %s  (daemon %s; %s app client(s), %s command(s) running)\n' \
    "$user" "${run:-?}" "${inst:-?}" "$state" "$pid" "$clients" "$cmds"

  [ "$restart" = 1 ] || continue
  if [ "$state" = current ] && [ "$all" != 1 ]; then continue; fi
  if [ "$cmds" != 0 ] && [ "$force" != 1 ]; then
    echo "  $cmds command(s) are running under this daemon; a restart would kill them. Wait, or use --force." >&2
    failed=$((failed + 1)); continue
  fi
  echo "  restarting ..."
  if new=$(restart_one "$user" "$home" "$pid" "$uid"); then
    now=$(running_version "$new")
    echo "  restarted: daemon $pid -> $new, now running ${now:-?}"
    record "$user ${run:-?} -> ${now:-?} (daemon $pid -> $new, clients $clients, commands $cmds, force $force)"
    [ "$now" = "$inst" ] || { echo "  WARNING: running ${now:-?} but ${inst:-?} is installed" >&2; failed=$((failed + 1)); }
  else
    echo "  restart FAILED for $user; check: systemctl status codex-rc@$user" >&2
    failed=$((failed + 1))
  fi
done < <(daemons)

if [ "$found" = 0 ]; then
  echo "no Codex remote-control daemon is running${only:+ for $only}"
  exit 0
fi
[ "$restart" = 1 ] || { [ "$stale" -gt 0 ] && echo "$stale account(s) need a restart to run the installed version: codex-restart --restart"; }
[ "$failed" = 0 ]
