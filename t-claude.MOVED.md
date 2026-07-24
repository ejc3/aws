# t-claude moved

`t-claude.zsh` and `nosync-wrap` now live in their own repo:

**https://github.com/ejc3/t-claude**

They were vendored here and base64-embedded into `dev-user-data.tf`. The dev-box
setup now fetches them from the raw GitHub URLs at boot instead:

```
https://raw.githubusercontent.com/ejc3/t-claude/main/t-claude.zsh
https://raw.githubusercontent.com/ejc3/t-claude/main/nosync-wrap
```

Edit the launcher or the pty shim in that repo, not here. The fetch is non-fatal:
if it fails, the `~/.zshrc` source line no-ops and t-claude falls back to bare claude.
