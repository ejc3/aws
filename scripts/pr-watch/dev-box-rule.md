## Pull requests: this box proposes, the jumpbox merges

The owner's rule (2026-10-12): agents on this box may open pull requests and issues, and do nothing else with them. A
watch on the main jumpbox (`ejc3/aws` `scripts/pr-watch/SKILL.md`) runs every 30 minutes over every open pull request
and issue authored by `ejc3` in the `ejc3` and `dolphin-labs-hq` repositories: it reviews each one, fixes what the
review finds, gets the reviewers (Codex, Greptile, the review gate) to sign off, merges it, and applies `ejc3/aws` and
`dolphin-aws` changes. That machine is the only one that merges or changes anything live.

- **Open a pull request as a draft while you are working on it, and mark it ready (`gh pr ready`) when it is done.** The
  watch leaves drafts alone and takes a ready pull request on its next run.
- **Never merge a pull request, push to a default branch, resolve review threads, apply Terraform (`terraform apply`,
  dolphin-aws `scripts/apply.sh`) or deploy by hand** from this box, even when a tool would let you. Propose it in a pull
  request; the jumpbox does the rest.
- Something only the jumpbox can do (Terraform in `ejc3/aws`, a live AWS or Cloudflare change)? File an issue in the
  repository it belongs to; the watch picks up issues filed by `ejc3` the same way.
