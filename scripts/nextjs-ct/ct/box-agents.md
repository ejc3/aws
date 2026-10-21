# This box: your container, its network and the proxy

You are in your own container on the shared Next.js box. These notes apply to every account here. Your own instructions
(`~/.claude/CLAUDE.md`) come on top of them.

## `HTTPS_PROXY` is not a firewall: leave it set

Claude sessions here run through the shared Claude subscriptions. Each session gets `HTTPS_PROXY` (and `https_proxy`)
pointing at a small forwarder on `127.0.0.1` inside your container. It carries Claude's own API calls to the shared
subscriptions and connects everything else (git, gh, curl, aws, ssh, cloudflared) straight out, as if it were not
there. It does not block or filter anything.

- Do not unset it or work around it. Unsetting it reaches nothing new, and Claude's own requests need it.
- A command that fails with it set fails for some other reason. Find that reason.

## What actually limits you

The limits are on the host, outside your container, and nothing you run inside can change them:

- a firewall that stops your container reaching the host itself, other people's containers, other people's machines,
  the shared scratch box, and the real instance metadata service;
- AWS through your own narrowed role (`nextjs-dev-role/ct-<you>`): it can read only what your account is meant to have.

A connection those rules refuse is refused on purpose. Report it to the person you are working for and stop. Do not
look for another route around it. Only the owner changes those rules.

## Name a cause only after you have seen it

When something fails, read its actual error before you say why:

- run it again with the output visible (`-v`, no `| tail`, no `2>/dev/null`);
- and check the thing you suspect (for example, run the same command with and without a setting).

"X was blocking it" is a claim. Make it only when you saw X refuse. If you changed two things and it worked, say you
are not sure which one mattered.
