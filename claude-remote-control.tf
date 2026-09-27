# Boot-time Claude remote control for the two metal development hosts.
#
# Colton and Connor each have a claude-rc@ unit because they are separate Unix users and
# therefore own separate tmux servers. All metal repositories belong to ubuntu and share
# one default tmux server, so they must share one systemd cgroup too. Independent per-repo
# units would make whichever unit created tmux own the server; stopping that unit would
# then kill every other repository's Claude session.
locals {
  metal_claude_remote_control = <<-SETUP
# The managed boot service needs t-claude's argument-passthrough support, so systemd gets
# its own copy rather than depending on a user's interactive one.
#
# Tracks main, deliberately unpinned. t-claude is ours: fixes land upstream and should reach
# every box on the next setup run, which is how the empty-picker fix arrived. A commit pin
# plus a sha256 was worse than nothing here -- the two constants have to be updated in
# lockstep, and when only the commit was bumped the download failed its hash check, fell
# through to "keeping verified installed copy", and would have frozen these boxes on the old
# version while printing a reassuring message.
#
# Integrity is still checked, just not against a constant that rots: a truncated or garbled
# download fails `zsh -n`, and the previous copy is kept instead.
install -d -m 755 /usr/local/lib/fcvm
TCLAUDE_SYSTEM=/usr/local/lib/fcvm/t-claude.zsh
TCLAUDE_TMP=$(mktemp)
if curl -fsSL --retry 3 \
  "https://raw.githubusercontent.com/ejc3/t-claude/main/t-claude.zsh" \
  -o "$TCLAUDE_TMP" \
  && [ -s "$TCLAUDE_TMP" ] \
  && zsh -n "$TCLAUDE_TMP" 2>/dev/null; then
  install -m 644 -o root -g root "$TCLAUDE_TMP" "$TCLAUDE_SYSTEM"
elif [ -s "$TCLAUDE_SYSTEM" ]; then
  echo "fcvm-claude: t-claude download failed or was malformed; keeping the installed copy"
else
  echo "WARNING: fcvm-claude has no usable t-claude; boot service will stay skipped" >&2
fi
rm -f "$TCLAUDE_TMP"

# Record interactive t-claude use in host-local state. Claude history itself cannot be
# used for this: claude-code-sync deliberately merges history.jsonl across ARM and x86.
cat > /usr/local/bin/fcvm-claude-track <<'TRACKER'
#!/bin/bash
set -uo pipefail

export HOME=/home/ubuntu
export PATH=/home/ubuntu/.local/bin:/usr/local/bin:/usr/bin:/bin
candidate="$1"
repo=$(git -C "$candidate" rev-parse --show-toplevel 2>/dev/null) || exit 0
case "$repo" in
  /home/ubuntu/*) ;;
  *) exit 0 ;;
esac

# MAIN CHECKOUTS ONLY -- never a linked `git worktree`. In a linked worktree the
# per-worktree git dir differs from the repository's common dir; in the main
# checkout they are the same. Branch worktrees are short-lived and numerous (73 on
# this box at one point, one agent each), and an agent per branch is neither wanted
# nor affordable: they consume memory, each stalls on its own trust prompt, and they
# bury the session that matters.
gitdir=$(git -C "$repo" rev-parse --path-format=absolute --git-dir 2>/dev/null) || exit 0
commondir=$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
[ "$gitdir" = "$commondir" ] || exit 0

origin=$(git -C "$repo" remote get-url origin 2>/dev/null) || exit 0
case "$origin" in
  https://github.com/ejc3/*|git@github.com:ejc3/*|ssh://git@github.com/ejc3/*) ;;
  # Collaboration repos owned by someone else, allowed ONE AT A TIME by exact name.
  # dolphin-labs moved from another owner to dolphin-labs-hq on 2026-08-12 and the
  # launcher correctly stopped starting an agent there until the new owner was named
  # here. That is the gate working, not a bug: a repo that gets an agent with
  # --dangerously-skip-permissions changed hands, and automation paused for a human.
  # Deliberately not an org wildcard: this gate decides which checkouts get an agent
  # started with --dangerously-skip-permissions at boot, and a wildcard would opt in
  # every future repo of that owner -- including ones added without our review.
  https://github.com/dolphin-labs-hq/dolphin-labs|https://github.com/dolphin-labs-hq/dolphin-labs.git) ;;
  git@github.com:dolphin-labs-hq/dolphin-labs|git@github.com:dolphin-labs-hq/dolphin-labs.git) ;;
  ssh://git@github.com/dolphin-labs-hq/dolphin-labs|ssh://git@github.com/dolphin-labs-hq/dolphin-labs.git) ;;
  *) exit 0 ;;
esac
case "$origin" in
  */claude-code-history|*/claude-code-history.git|*/claude-code-sync|*/claude-code-sync.git) exit 0 ;;
esac

STATE="$HOME/.local/state/fcvm-claude"
umask 077
mkdir -p "$STATE"
key=$(printf '%s' "$repo" | sha256sum | awk '{print $1}')
tmp=$(mktemp "$STATE/.repo.XXXXXX") || exit 0
printf '%s\n' "$repo" > "$tmp"
mv -f "$tmp" "$STATE/repo-$key"
TRACKER
chmod 755 /usr/local/bin/fcvm-claude-track

# The interactive function remains the upstream t-claude implementation, with one small
# host-local bookkeeping call in front. The boot launcher sources the pinned implementation
# directly and therefore does not refresh these markers merely because the machine booted.
cat > /home/ubuntu/.config/fcvm-t-claude.zsh <<'WRAPPER'
# Use the same verified implementation as the boot service. Otherwise an upstream
# session-key change could make interactive t-claude miss the already-running window.
if [ -r /usr/local/lib/fcvm/t-claude.zsh ]; then
  source /usr/local/lib/fcvm/t-claude.zsh
  functions[_fcvm_t_claude_impl]="$${functions[t-claude]}"
elif (( ! $+functions[_fcvm_t_claude_impl] && $+functions[t-claude] )); then
  functions[_fcvm_t_claude_impl]="$${functions[t-claude]}"
fi
if (( $+functions[_fcvm_t_claude_impl] )); then
  t-claude() {
    /usr/local/bin/fcvm-claude-track "$PWD" >/dev/null 2>&1 || true
    _fcvm_t_claude_impl "$@"
  }
fi
WRAPPER
chown ubuntu:ubuntu /home/ubuntu/.config/fcvm-t-claude.zsh
grep -qxF '[ -f ~/.config/fcvm-t-claude.zsh ] && source ~/.config/fcvm-t-claude.zsh' \
  /home/ubuntu/.zshrc \
  || printf '%s\n' '[ -f ~/.config/fcvm-t-claude.zsh ] && source ~/.config/fcvm-t-claude.zsh' \
    >> /home/ubuntu/.zshrc
chown ubuntu:ubuntu /home/ubuntu/.zshrc

# Launch the host-local active set. It is the union of repositories used through
# interactive t-claude on this host during the last 30 days and local user-owned checkouts
# that have recent host-local HEAD movement or uncommitted work. Shallow home-directory
# globs cover ordinary clones; synchronized Claude history is used only to FIND possible
# nested roots, never as the activity signal.
cat > /usr/local/bin/fcvm-claude-active-repos <<'LAUNCHER'
#!/bin/bash
set -uo pipefail

export HOME=/home/ubuntu
export PATH=/home/ubuntu/.local/bin:/home/ubuntu/.npm-global/bin:/usr/local/bin:/usr/bin:/bin
STATE="$HOME/.local/state/fcvm-claude"
CUTOFF_S=$(( $(date +%s) - 30 * 24 * 60 * 60 ))
CUTOFF_MS=$(( CUTOFF_S * 1000 ))
REPOS=$(mktemp)
trap 'rm -f "$REPOS"' EXIT

# Pre-accept Claude Code's per-directory trust prompt for a repo we are about to
# launch in. Without this the agent starts, prints "Is this a project you created or
# one you trust?", and waits forever for a keypress nobody will send: the session
# looks alive in tmux but never finishes starting, so it never registers with remote
# control and never appears in the client. 16 of 50 sessions were wedged exactly this
# way on 2026-08-12.
#
# The flag lives in ~/.claude.json under projects["<abs path>"].hasTrustDialogAccepted.
# We only ever set it for paths this launcher itself selected -- repos that already
# passed the origin allowlist above -- so this does not blanket-trust the filesystem.
#
# Written via a temp file + atomic rename, and re-read immediately before each write,
# because Claude Code rewrites this file itself; a read-modify-write held open across
# the whole loop would clobber concurrent updates.
trust_repo() {
  local repo="$1"
  python3 - "$repo" <<'TRUSTPY'
import json, os, sys, tempfile

repo = sys.argv[1]
path = os.path.expanduser("~/.claude.json")
try:
    with open(path) as fh:
        cfg = json.load(fh)
except (OSError, ValueError):
    return_code = 0
    sys.exit(0)          # no config yet: Claude will create it and ask once

projects = cfg.setdefault("projects", {})
entry = projects.setdefault(repo, {})
if entry.get("hasTrustDialogAccepted") is True:
    sys.exit(0)          # already trusted, leave the file untouched
entry["hasTrustDialogAccepted"] = True

d = os.path.dirname(path)
fd, tmp = tempfile.mkstemp(dir=d, prefix=".claude.json.")
try:
    with os.fdopen(fd, "w") as fh:
        json.dump(cfg, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
except Exception:
    os.unlink(tmp)
    raise
TRUSTPY
}

add_repo() {
  local candidate="$1" repo origin
  [ -d "$candidate" ] || return 0
  repo=$(git -C "$candidate" rev-parse --show-toplevel 2>/dev/null) || return 0
  case "$repo" in
    /home/ubuntu/*) ;;
    *) return 0 ;;
  esac

  # Main checkouts only -- skip linked worktrees. See the note at the gate above.
  local gitdir commondir
  gitdir=$(git -C "$repo" rev-parse --path-format=absolute --git-dir 2>/dev/null) || return 0
  commondir=$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || return 0
  [ "$gitdir" = "$commondir" ] || return 0

  origin=$(git -C "$repo" remote get-url origin 2>/dev/null) || return 0
  case "$origin" in
    https://github.com/ejc3/*|git@github.com:ejc3/*|ssh://git@github.com/ejc3/*) ;;
    # Exact-name collaboration repos; see the note at the marker-writing gate above.
    https://github.com/dolphin-labs-hq/dolphin-labs|https://github.com/dolphin-labs-hq/dolphin-labs.git) ;;
    git@github.com:dolphin-labs-hq/dolphin-labs|git@github.com:dolphin-labs-hq/dolphin-labs.git) ;;
    ssh://git@github.com/dolphin-labs-hq/dolphin-labs|ssh://git@github.com/dolphin-labs-hq/dolphin-labs.git) ;;
    *) return 0 ;;
  esac
  case "$origin" in
    */claude-code-history|*/claude-code-history.git|*/claude-code-sync|*/claude-code-sync.git) return 0 ;;
  esac
  printf '%s\n' "$repo" >> "$REPOS"
}

locally_active() {
  local repo="$1" head_log head_activity dirty
  head_log=$(git -C "$repo" rev-parse --path-format=absolute --git-path logs/HEAD 2>/dev/null)
  head_activity=$(stat -c %Y "$head_log" 2>/dev/null || echo 0)
  dirty=$(git -C "$repo" status --porcelain --untracked-files=normal 2>/dev/null | sed -n '1p')
  [ "$head_activity" -ge "$CUTOFF_S" ] 2>/dev/null || [ -n "$dirty" ]
}

# Exact host-local t-claude use, including nested repositories and worktrees.
for marker in "$STATE"/repo-*; do
  [ -f "$marker" ] || continue
  marker_time=$(stat -c %Y "$marker" 2>/dev/null) || continue
  [ "$marker_time" -ge "$CUTOFF_S" ] || continue
  IFS= read -r repo < "$marker" || continue
  add_repo "$repo"
done

# First-deployment seed and automatic coverage for locally active top-level checkouts.
for candidate in /home/ubuntu/* /home/ubuntu/src/*; do
  [ -e "$candidate/.git" ] || continue
  repo=$(git -C "$candidate" rev-parse --show-toplevel 2>/dev/null) || continue
  locally_active "$repo" && add_repo "$repo"
done

# Claude sync merges history across hosts, so these paths are candidates only. Requiring
# this host's reflog/dirty state prevents activity on ARM from selecting an idle x86 clone
# (and vice versa) while still finding active nested worktrees without a recursive scan.
HISTORY="$HOME/.claude/history.jsonl"
if [ -s "$HISTORY" ]; then
  while IFS= read -r project; do
    case "$project" in
      /home/ubuntu/*) ;;
      *) continue ;;
    esac
    [ -d "$project" ] || continue
    repo=$(git -C "$project" rev-parse --show-toplevel 2>/dev/null) || continue
    locally_active "$repo" && add_repo "$repo"
  done < <(
    jq -r --argjson cutoff "$CUTOFF_MS" \
      'select((.timestamp? | type) == "number" and .timestamp >= $cutoff) | .project? // empty' \
      "$HISTORY" 2>/dev/null | sort -u
  )
fi

sort -u "$REPOS" -o "$REPOS"
mkdir -p "$STATE"
# The exact folders this boot opens windows for. fcvm-codex-seed.service reads it, so Codex
# gets a thread in the same set and the two can never disagree.
cp "$REPOS" "$STATE/repos"
if [ ! -s "$REPOS" ]; then
  echo "fcvm-claude: no host-local repositories are currently active"
  printf 'ready=0 failed=0 at=%s\n' "$(date -Is)" > "$STATE/last-start"
  exit 0
fi

# Trust EVERY selected repo before launching ANY of them. Doing it per-repo inside the
# launch loop is not enough: a Claude started for an earlier repo has already read
# ~/.claude.json, and when it later writes the file back it drops entries added after
# that read -- which silently undid the trust for the last repos in the list (observed
# with dolphin-labs on 2026-08-12). Seeding the whole set first means every instance
# reads a file that already contains all of them.
while IFS= read -r repo; do
  trust_repo "$repo" || echo "fcvm-claude: WARNING could not pre-accept trust for $repo"
done < "$REPOS"

ready_count=0
failed_count=0
while IFS= read -r repo; do
  echo "fcvm-claude: starting remote control in $repo"

  # Idempotent safety net: a no-op when the bulk pass above already trusted this repo.
  trust_repo "$repo" || true

  # t-claude intentionally tries to attach after creating/reusing its tmux window. There
  # is no terminal under systemd, so that final attach fails harmlessly after launch.
  /usr/bin/zsh -c '
    source /usr/local/lib/fcvm/t-claude.zsh || exit 1
    cd -- "$1" || exit 1
    t-claude --auto --remote-control
  ' fcvm-claude "$repo" || true

  key="$(printf '%s' "$repo" | cksum | awk '{print $1}')_$(printf '' | cksum | awk '{print $1}')"
  live=0
  for _attempt in $(seq 1 20); do
    window=$(tmux list-windows -a -F '#{window_id} #{@tclaude_key}' 2>/dev/null \
      | awk -v key="$key" '$2 == key {print $1; exit}')
    if [ -n "$window" ]; then
      pane_pid=$(tmux display-message -p -t "$window" '#{pane_pid}' 2>/dev/null)
      child_args=$(ps -o args= --ppid "$pane_pid" 2>/dev/null)
      if printf '%s\n' "$child_args" | grep -Fq -- '--remote-control' \
        && printf '%s\n' "$child_args" | grep -Eq 'claude|nosync-wrap'; then
        live=1
        break
      fi
    fi
    sleep 0.25
  done

  if [ "$live" -eq 1 ]; then
    ready_count=$((ready_count + 1))
    echo "fcvm-claude: ready in $repo"
  else
    failed_count=$((failed_count + 1))
    echo "fcvm-claude: no live --remote-control process in $repo" >&2
  fi
done < "$REPOS"

printf 'ready=%s failed=%s at=%s\n' "$ready_count" "$failed_count" "$(date -Is)" \
  > "$STATE/last-start"
echo "fcvm-claude: ready=$ready_count failed=$failed_count"
LAUNCHER
chmod 755 /usr/local/bin/fcvm-claude-active-repos

cat > /etc/systemd/system/fcvm-claude-rc.service <<'UNIT'
[Unit]
Description=Claude Code remote control for active FCVM repositories
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=ubuntu
Environment=HOME=/home/ubuntu
Environment=TERM=xterm-256color
Environment=PATH=/home/ubuntu/.local/bin:/home/ubuntu/.npm-global/bin:/usr/local/bin:/usr/bin:/bin
WorkingDirectory=/home/ubuntu
# Integrity, not identity. This used to be a sha256 of one revision, left behind when the
# installer above dropped its pin to track main. The two then disagreed by construction:
# every upstream t-claude commit changed the file, the hash stopped matching, ExecCondition
# returned non-zero, and systemd SKIPPED the unit -- silently, because a skip is the
# correct, quiet behaviour for a condition that says "nothing to do". The ARM box booted
# with no Claude session twice before anyone asked why, and it would have recurred on every
# t-claude commit forever.
#
# zsh -n is the same check the installer uses to decide the download is sound, so the guard
# now asks the question that actually matters -- is this file valid t-claude -- rather than
# is it one specific historical copy.
ExecCondition=/bin/sh -c 'zsh -n /usr/local/lib/fcvm/t-claude.zsh >/dev/null 2>&1'
ExecCondition=/bin/sh -c 'command -v claude >/dev/null 2>&1 && claude auth status >/dev/null 2>&1'
ExecStart=/usr/local/bin/fcvm-claude-active-repos
TimeoutStartSec=300

# Every repository uses ubuntu's default tmux server. Keep all boot-launched sessions in
# one cgroup; separate per-repo units would let one unit kill the shared server.
KillMode=control-group

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
# Enable even before the personal Claude login exists. ExecCondition skips cleanly while
# auth is absent/expired, and a later boot retries without another provisioning pass.
systemctl enable --now fcvm-claude-rc.service >/dev/null 2>&1 || true

# ------------------------------------------------------------- Codex thread per window
# Every folder that got a tmux window above also gets a Codex thread, so it shows up in the
# Codex app. Runs after the launcher (which writes the list) and the Codex daemon; a folder
# that already has a thread is only checked, so after the first boot this is quick.
${local.codex_seed_thread_install}
cat > /etc/systemd/system/fcvm-codex-seed.service <<'UNIT'
[Unit]
Description=Seed a Codex thread for every repository fcvm-claude-rc opened a window for
After=fcvm-claude-rc.service codex-rc@ubuntu.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=ubuntu
Environment=HOME=/home/ubuntu
WorkingDirectory=/home/ubuntu
ExecCondition=/bin/sh -c 'test -s /home/ubuntu/.local/state/fcvm-claude/repos && /home/ubuntu/.local/bin/codex login status >/dev/null 2>&1'
ExecStart=/usr/local/bin/codex-seed-thread --from /home/ubuntu/.local/state/fcvm-claude/repos
# A new folder costs one short model turn; the first boot after this lands may seed several.
TimeoutStartSec=1800

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable fcvm-codex-seed.service >/dev/null 2>&1 || true
# --no-block: this setup often runs inside dev-selfupdate at boot, and the seed waits on the
# launcher and on model turns.
systemctl start --no-block fcvm-codex-seed.service >/dev/null 2>&1 || true
  SETUP
}
