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
[ -n "$root" ] && cd "$root" 2>/dev/null
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
else
  problems+=("Branch '$branch' has no upstream. Push it: git push -u origin $branch")
fi

# 3. Terraform drift -- live AWS must match config
if command -v terraform >/dev/null 2>&1; then
  tflog="/tmp/stop-hook-tfplan.log"
  terraform plan -detailed-exitcode -lock=false -input=false -no-color >"$tflog" 2>&1
  rc=$?
  if [ "$rc" -eq 2 ]; then
    problems+=("Terraform drift: live AWS does not match config (terraform plan shows changes). Review $tflog and either 'terraform apply' the intended change or reconcile config to match live BEFORE stopping, so nothing is lost.")
  elif [ "$rc" -ne 0 ]; then
    problems+=("terraform plan failed (exit $rc). See $tflog -- resolve before stopping.")
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
