## ejc3/aws pull requests: this box proposes, the jumpbox merges

The owner's rule (2026-10-12), for the `ejc3/aws` repository only: agents on this box may open pull requests and issues
there, and do nothing else with them. A watch on the main jumpbox (`ejc3/aws` `scripts/pr-watch/SKILL.md`) runs every
30 minutes over every open `ejc3/aws` pull request and issue authored by `ejc3`: it reviews each one, fixes what the
review finds, gets the reviewers (Codex, CodeRabbit, Greptile, the review gate) to sign off, merges it and applies it.

In `ejc3/aws`:

- **Open a pull request as a draft while you are working on it, and mark it ready (`gh pr ready`) when it is done.** The
  watch leaves drafts alone and takes a ready pull request on its next run.
- **Never merge a pull request, push to `main`, resolve review threads or apply Terraform** for it from this box. Propose
  it in a pull request; the jumpbox does the rest.
- Something only the jumpbox can do (a live AWS or Cloudflare change)? File an issue in `ejc3/aws`; the watch picks up
  issues filed by `ejc3` there the same way.

Every other repository works as it always has: this rule does not apply to them.
