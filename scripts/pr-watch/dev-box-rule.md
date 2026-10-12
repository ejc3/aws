## Pull requests: this box proposes, the jumpbox merges

The owner's rule (2026-10-12): agents on this box may open pull requests and issues, and do nothing else with them. A
watch on the main jumpbox (`ejc3/aws` `scripts/pr-watch/SKILL.md`) runs every 30 minutes over every open `ejc3/aws`
pull request and issue authored by `ejc3`: it reviews each one, fixes what the review finds, gets the reviewers (Codex,
CodeRabbit, Greptile, the review gate) to sign off, merges it and applies it. In every other repository a ready pull
request waits for the owner. Nothing on this box merges or changes anything live.

- **Open a pull request as a draft while you are working on it, and mark it ready (`gh pr ready`) when it is done.** The
  watch leaves drafts alone and takes a ready pull request on its next run.
- **Never merge a pull request, push to a default branch, resolve review threads, apply Terraform (`terraform apply`,
  dolphin-aws `scripts/apply.sh`) or deploy by hand** from this box, even when a tool would let you. Propose it in a pull
  request; the jumpbox does the rest for `ejc3/aws`, the owner for everything else. (dolphin-aws can only be applied
  from nextjs-dev: run its `scripts/apply.sh` only when the owner asks for that apply in your session.)
- Something only the jumpbox can do (Terraform in `ejc3/aws`, a live AWS or Cloudflare change)? File an issue in
  `ejc3/aws`; the watch picks up issues filed by `ejc3` there the same way.
