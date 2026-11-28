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
- A reaper deletes any session older than 60 minutes, any with no timeouts (or timeouts over 10 minutes idle /
  60 minutes total) once it is 5 minutes old, and sandboxes idle 10 minutes. Tag a session `keep=true` only when
  you must keep it longer, and delete it yourself when done.

## 3D models: Meshy

For a game's 3D models and textures, from a text prompt or a picture, use Meshy (meshy.ai, its REST API at
`https://api.meshy.ai`, docs at docs.meshy.ai).

- The key is the Secrets Manager secret `games/meshy-api-key` (us-west-1), a plain string. Read it into the environment
  when you use it, never onto a command line or into a file:
  `export MESHY_API_KEY=$(aws secretsmanager get-secret-value --region us-west-1 --secret-id games/meshy-api-key --query SecretString --output text)`
- Send it to curl on stdin, never as an argument (an argument is readable in `/proc/<pid>/cmdline`):
  `printf 'Authorization: Bearer %s\n' "$MESHY_API_KEY" | curl -sS -H @- https://api.meshy.ai/...`
- Every generation spends credits from one shared balance. Check the docs for the current endpoints and prices, try one
  small preview before a batch, and keep the task ids of what you accept.
- Ship the finished model files (`.glb` and the like) in the game. Never put the key in a game, a commit or a log.

## The GitHub API is a shared budget: wait on events, don't poll

Every `gh` call you make runs as your GitHub account, and one account has one REST limit (5,000 requests an hour).
Every session, agent and script of that account shares it, and so does any automation using a token issued to it.
On 2026-11-26 one account's watchers drained it to zero, and an alert router using a token of the same account could
not reach GitHub until the hourly reset.

- **Wait on events where you can.** `gh event listen` (gh 2.105 and later, GitHub Event Streams, a preview enabled per
  account) waits for a pull request event instead of polling, for example
  `gh event listen --resource <PR URL> --event com.github.pull_request.check_suite.status.changed.v1 --timeout 30m`.
  "Event subscriptions are not enabled for this account" means your account is not in the preview: poll as below.
- **When you poll:**
  - check each thing at most once every 10 minutes;
  - run one watcher per pull request, not one per agent;
  - prefer a repository's own watcher to a hand-written loop;
  - count every call a script makes per check.
- **Check the budget** before starting several watchers (`gh api rate_limit`), and keep about a fifth in reserve. At zero,
  stop your watchers until the reset rather than retrying.
- **Automation uses a GitHub App installation token,** never a person's token.

## Merge through a pooled queue, not one PR at a time

The owner: "We need to move to merge queues. Think Covid pooled testing." Merging PRs one by one moves `main` each time. Every other open PR and stack then merges `main` again and re-runs CI, including native or device jobs that can queue for hours. Two PRs that each pass alone can also break `main` together.

- **Prefer a pooled merge queue.** Ready PRs (each one's own gates passed: reviewed head, resolved threads, required checks) go into one batch: `main` plus every queued PR. That batch runs CI once, and on a pass they all land together, so `main` moves once. On a failure, re-run once for a flake, then split the batch in halves and test each until the culprit is isolated. Land the rest and tell the culprit's author why, with the failing log.
- **Units:** a native stack is one unit, and automated PRs (data refreshes) join the queue rather than racing it.
- **The repository's own queue:** use it where it has one (GitHub's merge queue where the plan allows it, or the repository's own pooled queue). Where it has none, batch by hand: collect the ready PRs, land them back to back with nothing else in between, then bring open stacks up to `main` once, not after each merge.
- **The queue authenticates as a GitHub App,** never a person's token.
