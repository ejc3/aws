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
    # This command independently verifies the jumpbox and latest origin/main.
    # Busy locks and ineligible hosts skip. Results and raw logs stay distinct.
    bash scripts/publish-applied-status.sh --if-idle >&2
    rc=$?
    tflog="${XDG_STATE_HOME:-$HOME/.local/state}/aws-applied-status/last-plan.log"
    if [ "$rc" -eq 2 ]; then
      problems+=("Terraform drift: live AWS does not match config. Review $tflog and apply or reconcile the intended change.")
    elif [ "$rc" -ne 0 ] && [ "$rc" -ne 75 ]; then
      problems+=("Terraform status check failed (exit $rc). Look for the latest run in $tflog or see the command diagnostic.")
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
