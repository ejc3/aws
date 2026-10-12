---
name: pr-watch
description: Review, fix, get sign-off on and merge every open pull request and issue authored by ejc3 (ejc3 and dolphin-labs-hq repositories); run on a recurring schedule from a jumpbox session.
---

# PR and issue watch

The owner's standing request (2026-10-12): "look for new PRs from ejc3 and issues from ejc3. If you see one, do the
full code review loop, make sure reviewers sign off and push", and "I want this box to handle everything": the main
jumpbox does it for every repository, and no other box runs a watch. One Claude session on the jumpbox keeps it running
as a recurring job (`CronCreate`, cron `7,37 * * * *`, prompt `/pr-watch`). A scheduled job lives only as long as its
session, so a SessionStart hook (`scripts/pr-watch/session-start.sh`) hands the job to a new session when the owning
one is gone. The dev boxes (fcvm-metal-arm's `ubuntu`, nextjs-dev's `ejc3`) carry `scripts/pr-watch/dev-box-rule.md`
in their user instructions: they may open pull requests and issues and touch nothing else (no merge, no push to a
default branch, no apply, no deploy); this watch is the only thing that merges or changes anything live. It listens to
items authored by `ejc3`, the GitHub login of the jumpbox's `ubuntu`, fcvm's `ubuntu` and nextjs-dev's `ejc3` alike.
`scripts/pr-watch-install.sh` installs both (see its header).

## Each run

1. **Find work.** Open PRs and open issues authored by `ejc3` in repositories owned by `ejc3` and `dolphin-labs-hq`:
   `gh search prs --author ejc3 --state open --owner ejc3` and `--owner dolphin-labs-hq`, the same with
   `gh search issues`. For dolphin-labs-hq use the GitHub App: `GH_TOKEN=$(scripts/github-app-token.py dolphin-labs-hq)`.
   Keep `~/.local/state/pr-watch/state.json` (repo, number, head sha, last action, outcome) and work only items that are
   new or changed since the last run. With nothing new, say so in one line. Several items: work them one at a time.
2. **Leave work in progress alone.** Only note, never touch: a draft PR (the dev boxes keep a PR in draft until it is
   ready); a PR with a push in the last 45 minutes from another session (head commit time, a local worktree on its
   branch); an issue already linked to an open PR.
3. **Each PR: the full review loop.** Run `/code-review` at `high` on the PR. Fix real P1s and P2s that change what
   production does, with tests; push. Ask the repository's reviewers again (`@codex review`; `@greptileai review` where
   `.greptile/` exists; the repository's review gate). Answer every thread and resolve it, replying with evidence where
   you disagree. Loop until CI is green and the reviewers have signed off, within the stop rule in AGENTS.md (about three
   rounds, then report what is left). Then merge with a merge commit. Steve may also merge dolphin-labs-hq pull requests
   from his own server when needed (owner, 2026-10-12): a pull request he merged is done, never reverted by this watch.
4. **ejc3/aws after a merge: apply it.** Fresh worktree off origin/main, `umask 002`,
   `terraform init -input=false -lockfile=readonly`, plan, read the plan, apply the saved plan, require an empty
   follow-up plan, `git worktree remove --force`. One Terraform run at a time on the jumpbox (check
   `pgrep -af "terraform (plan|apply)"` and `uptime`; `-lock-timeout=10m`, never `-lock=false`). A plan that replaces or
   destroys something unexpected stops the run and goes to the owner.
   **dolphin-labs-hq/dolphin-aws after a merge:** it applies only from nextjs-dev, as `ejc3` there, with its
   `scripts/apply.sh` (reach it with `ssh -i ~/.ssh/fcvm-ec2 ubuntu@<nextjs-dev private address>` and `sudo -u ejc3 -H`);
   read its plan the same way before it applies.
5. **Each issue.** Confirm it from live state, open a PR with the fix and tests, run step 3 on it, and close the issue
   from the PR.
6. **Report:** what was reviewed, fixed, merged and applied, and what is left for the owner.

The repository rules apply throughout: no secrets on a command line or in output, no email addresses in files, commits
or PRs, no attribution lines, never `git stash` here (use a worktree), ask before restarting or rebooting a host
(claw-master only through `sudo claw-master-rollout`), and no apt, dpkg or snap commands on fcvm-metal-arm.
