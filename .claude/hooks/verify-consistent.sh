#!/usr/bin/env bash
# Stop hook: verify this repo is consistent before a Claude Code session stops.
#
# Enforces the repo's standing rules:
#   1. git working tree is clean (no uncommitted / untracked changes)
#   2. local branch is in sync with its origin upstream (everything pushed)
#   3. terraform config matches live AWS (no drift) -- the repo is terraform-driven
#
# On any problem it emits {"decision":"block","reason":...} so Claude is told to
# fix it before stopping. The stop_hook_active guard prevents an infinite loop:
# once Claude is already continuing because of this hook, the next stop is allowed.
set -uo pipefail

input=$(cat)

# Loop guard: if we're already continuing from a previous Stop-hook block, allow the stop.
if printf '%s' "$input" | jq -e '.stop_hook_active == true' >/dev/null 2>&1; then
  exit 0
fi

# Locate the repo root. Claude Code sets CLAUDE_PROJECT_DIR for hooks; fall back to
# the session cwd from the hook payload, then to git's toplevel.
root="${CLAUDE_PROJECT_DIR:-}"
if [ -z "$root" ]; then
  root=$(printf '%s' "$input" | jq -r '.cwd // empty')
fi
if [ -n "$root" ]; then
  cd "$root" 2>/dev/null || exit 0
fi
root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$root" || exit 0

problems=()

# 1. Uncommitted / staged / untracked changes
if ! git diff --quiet || ! git diff --cached --quiet; then
  problems+=("Uncommitted changes in the working tree. Stage and commit them: git add -A && git commit")
fi
if [ -n "$(git ls-files --others --exclude-standard)" ]; then
  problems+=("Untracked files present. Commit them or add to .gitignore.")
fi

# 2. Unpushed commits (local ahead of upstream)
branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
if git rev-parse --abbrev-ref "@{u}" >/dev/null 2>&1; then
  git fetch -q origin "$branch" 2>/dev/null || true
  ahead=$(git rev-list --count "@{u}..HEAD" 2>/dev/null || echo 0)
  if [ "${ahead:-0}" -gt 0 ]; then
    problems+=("$ahead local commit(s) not pushed. Run: git push origin $branch")
  fi
elif [ "$branch" != HEAD ]; then
  problems+=("Branch '$branch' has no upstream. Push it: git push -u origin $branch")
fi

# 3. Terraform drift -- live AWS must match config. Only origin/main is ever applied, so only
# a checkout of origin/main is compared with live AWS. Any other branch predates or differs
# from what is applied, and planning it reports the difference as "drift" that must not be
# "fixed" (a review branch built on an old base did exactly that).
if git fetch -q origin '+refs/heads/main:refs/remotes/origin/main' 2>/dev/null; then
  if [ "$branch" = main ] && [ "$(git rev-list --count HEAD..origin/main 2>/dev/null)" -gt 0 ]; then
    problems+=("Local main is behind origin/main. Fast-forward it (git merge --ff-only origin/main) so the drift check plans what is applied.")
  elif [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] && [ -z "$(git status --porcelain)" ]; then
    # The full plan takes 10-15 minutes, so the hook never waits for it. It judges by the last
    # FINISHED check of this exact origin/main: publish-applied-status.sh writes
    # "main=<sha> checked_at=<UTC> terraform_exit=<rc>" as the log's first line (atomically, and only
    # once Terraform ran), and the background job records its own exit in background.rc, so a check
    # that failed before planning (init, STS) is not silent. A missing result, one for another commit,
    # a failed or drifted one, or one older than VERIFY_DRIFT_REFRESH_SECONDS starts a fresh check,
    # detached, one at a time (flock -o: the lock is not inherited by Terraform or its providers).
    state="${XDG_STATE_HOME:-$HOME/.local/state}/aws-applied-status"
    tflog="$state/last-plan.log"
    refresh_after=${VERIFY_DRIFT_REFRESH_SECONDS:-1800}
    main_sha=$(git rev-parse origin/main)
    last_main="" last_at="" last_rc="" bg_main="" bg_rc=""
    if [ -r "$tflog" ]; then
      read -r last_main last_at last_rc < <(head -n 1 "$tflog" |
        sed -nE 's/^main=([0-9a-f]{40}) checked_at=([0-9TZ:-]+) terraform_exit=([0-9]+)$/\1 \2 \3/p')
    fi
    if [ -r "$state/background.rc" ]; then
      read -r bg_main bg_rc < <(sed -nE 's/^([0-9a-f]{40}) ([0-9]+)$/\1 \2/p' "$state/background.rc")
    fi
    age=""
    if [ "$last_main" = "$main_sha" ] && last_epoch=$(date -u -d "$last_at" +%s 2>/dev/null); then
      age=$(($(date +%s) - last_epoch))
      if [ "$last_rc" -eq 2 ]; then
        problems+=("Terraform drift: live AWS does not match config (check of $last_at). Review $tflog and apply or reconcile the intended change.")
      elif [ "$last_rc" -ne 0 ] && [ "$last_rc" -ne 75 ]; then
        problems+=("Terraform status check failed (exit $last_rc, check of $last_at). Look for the latest run in $tflog.")
      fi
    fi
    if [ "$bg_main" = "$main_sha" ] && [ "${bg_rc:-0}" -ne 0 ] && [ "$bg_rc" -ne 2 ] && [ "$bg_rc" -ne 75 ] &&
      { [ -z "$age" ] || [ "$state/background.rc" -nt "$tflog" ]; }; then
      problems+=("The background drift check of ${main_sha:0:7} failed (exit $bg_rc) before it wrote a result. See $state/background.log.")
    fi
    if [ -z "$age" ] || [ "$age" -gt "$refresh_after" ] || [ "$last_rc" -ne 0 ]; then
      mkdir -p "$state"
      setsid -f flock -n -o "$state/background.lock" bash -c 'cd "$1" || exit 0; rc=0
        { if [ -d .terraform ]; then TF_RUN_LOCK_NOWAIT=1 terraform init -input=false -lockfile=readonly >/dev/null; fi &&
          bash scripts/publish-applied-status.sh --if-idle; } >"$2/background.log" 2>&1 || rc=$?
        printf "%s %s\n" "$3" "$rc" >"$2/background.rc.tmp" && mv -f "$2/background.rc.tmp" "$2/background.rc"' \
        _ "$root" "$state" "$main_sha" </dev/null >/dev/null 2>&1
      echo "verify-consistent: requested a background drift check of ${main_sha:0:7} ($state/background.log)" >&2
    fi
  fi
fi

if [ "${#problems[@]}" -eq 0 ]; then
  exit 0
fi

reason="Repo consistency check failed -- this repo must stay terraform-driven, committed, and pushed before stopping:"
for p in "${problems[@]}"; do
  reason+=$'\n'"  - $p"
done

jq -cn --arg r "$reason" '{decision:"block", reason:$r}'
exit 0
