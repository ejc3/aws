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

- a firewall that stops your container reaching the host itself, other people's containers, other people's Windows and
  GPU boxes, the shared scratch box, and the real instance metadata service. Each person's Ohio pbox is the exception:
  it has a public address that anyone can reach, and what keeps you out of someone else's is that you do not have their
  SSH key;
- AWS through your own narrowed role (`nextjs-dev-role/ct-<you>`): it can read only what your account is meant to have.

A connection those rules refuse is refused on purpose. Report it to the person you are working for and stop. Do not
look for another route around it. Only the owner changes those rules.

## Name a cause only after you have seen it

When something fails, read its actual error before you say why:

- run it again with the output visible (`-v`, no `| tail`, no `2>/dev/null`);
- and check the thing you suspect (for example, run the same command with and without a setting).

"X was blocking it" is a claim. Make it only when you saw X refuse. If you changed two things and it worked, say you
are not sure which one mattered.

## iOS Simulators: run.cloud

When a job needs an iOS Simulator and you have no Mac simulator free, use run.cloud: cloud iOS
simulators that take a simulator build (`.app`, `.zip`, `.tar.gz`), with tap, type, screenshot,
pasteboard, open-URL and accessibility-tree calls, from the `runcloud` CLI or `@run-cloud/sdk`.

- The key is the Secrets Manager secret `run-cloud/api-key` (us-west-1), JSON `{"RUN_CLOUD_API_KEY": ...}`.
  Read it into the environment when you use it, never onto a command line or into a file:
  `export RUN_CLOUD_API_KEY=$(aws secretsmanager get-secret-value --region us-west-1 --secret-id run-cloud/api-key --query SecretString --output text | jq -r .RUN_CLOUD_API_KEY)`
  and `export RUN_CLOUD_API_URL=https://api.run.cloud`.
- It is billed per active simulator minute against a small shared balance. Give every session an
  inactivity timeout and a hard timeout (`runcloud ios create --inactivity-timeout 2m --hard-timeout 30m`),
  delete it when you are done (`runcloud ios delete <id>`), and never touch a session you did not
  start (`runcloud ios list` shows everyone's).
- Simulator builds only: never sign or ship an app from it.
