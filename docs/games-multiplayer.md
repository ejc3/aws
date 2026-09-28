# Games multiplayer: bring-up, shipping and costs

The Colton Games multiplayer platform runs one Fargate task per match. Players enter
through `wss://play.cc-games.app/m/<id>`. The design and the binding names are in the games
repo (`CoderColton/colton-games`): `docs/MULTIPLAYER.md` and `docs/MULTIPLAYER-CONTRACT.md`.

The Terraform is in two files:

- `games-multiplayer.tf` holds the AWS and Cloudflare resources: ECR, the `games` cluster,
  IAM, the Vercel OIDC trust, security groups, the ALB, the certificate and DNS, the router
  and engine task definitions, the launch function (the only way an engine starts) and the
  sweeper.
- `games-multiplayer-bringup.tf` holds everything else a working platform needs: images,
  secrets, Vercel settings, the Supabase migration, and a health check.

## Bring-up: plan, apply, done

From the jumpbox:

```bash
cd ~/aws && git pull --ff-only && terraform plan && terraform apply
```

One apply goes from nothing to a healthy router, once the one input below exists.

**First time only: Colton's read token.** The pinned commit lives in `CoderColton/colton-games`,
a private repo on Colton's personal account. Only a token Colton owns can be limited to it:
a fine-grained token owned by anyone else cannot reach another user's personal repo, and
`github-pat-ejc3` is readable by every dev box anyway. The preflight reads this token, so a
plan fails until it exists, and Terraform cannot create its secret while the plan fails.
Break that loop once:

1. Colton, signed in as `CoderColton`, creates a token at
   <https://github.com/settings/personal-access-tokens/new>: resource owner `CoderColton`,
   **Only select repositories** → `colton-games`, Repository permissions → **Contents:
   Read-only**, nothing else. GitHub shows it once.
2. From a fresh worktree off `origin/main`, create only the empty secret. The targeted plan
   must show exactly these two resources and nothing else:

   ```bash
   terraform apply -target=aws_secretsmanager_secret.games_mp_github_read \
                   -target=aws_secretsmanager_secret_policy.games_mp_github_read
   ```

3. Put the token in without echoing it or placing it in argv (paste, Enter):

   ```bash
   read -rs T; printf %s "$T" | aws secretsmanager put-secret-value --region us-west-1 \
     --secret-id games/colton-games-read --secret-string file:///dev/stdin \
     --query VersionId --output text; unset T
   ```

4. Plan and apply normally. When the token nears its expiry (GitHub sends a reminder), Colton
   regenerates it and step 3 replaces the value; nothing else changes.

**The plan checks first.** Before anything changes, `terraform plan` runs a read-only
preflight (`data "external" "games_mp_preflight"`, `bringup.py preflight`). It proves that
every later step can finish:

- The Vercel token (`vercel-api-token`) is live, not revoked or leaked, not expiring within
  7 days, and scoped to the team, or is a user token.
- The token can read the colton-games project, including its OIDC and protection-bypass
  settings, and the project's env list.
- It can decrypt, on Production, the values the apply uses: the integration's
  `POSTGRES_URL_NON_POOLING` and the Supabase values copied to Preview.
- No variable with a copied key already spans Preview and another environment.
- `games/colton-games-read` (Colton's read-only token for `colton-games`, see *First time
  only* above) can read the pinned commit.
- `psql` is on the jumpbox, or can be installed without a password.

It reports every problem at once, with the exact fix, and fails the plan, so nothing
changes. It prints no secret and returns none to Terraform.

One thing it cannot prove by reading: that Vercel will accept the env and bypass writes.
Vercel tokens carry a scope and an expiry but no per-endpoint permissions. Every fact it
checks was also confirmed by hand on 2026-09-27 with a team member's login.

**Then the apply runs the steps.** The steps that are not plain resources run
`games-multiplayer/bringup.py` on the jumpbox with its administrator role. Each step checks
live state first, changes only what is missing, and verifies the result; anything else
fails the apply. With nothing changed, a second apply is an empty plan.

| Step | What it does |
| --- | --- |
| **Secrets** | The random provider generates the token key (`kid1:<base64 32 bytes>`), `MP_TEST_KEY`, `CRON_SECRET`, `MP_COOKIE_SECRET` (one per environment), Preview's `SKYHOOK_LEADERBOARD_SECRET` and the automation-bypass secret. The token key, test key and cron secret also go to Secrets Manager (`games/mp-token-keys`, `games/mp-test-key`, `games/mp-cron-secret`). The router's task definition names the exact version of its key, so a new key version is a new task definition that the health step verifies. |
| **Images** (`terraform_data.games_mp_build`) | Skips everything if ECR already has both tags. Otherwise the jumpbox downloads the pinned commit (`games_mp_source_ref`) with the `games/colton-games-read` token and uploads it to `s3://games-mp-build-<account>/sources/`. It then runs the CodeBuild project `games-mp-images` (ARM, 2 vCPU) and waits for SUCCEEDED. The build checks that the repo's `scripts/mp-images.mjs` produces exactly the tags Terraform expects, then builds and pushes only the missing ones. Tags are router `<sha12>` and engine `<simVersion>-<sha12>`. The build role has no GitHub or Secrets Manager access. |
| **Task definitions and router** | `games-mp-router`, `games-mptest` and the `mp-router` service are created only after the images exist. The service rolls with no downtime: the new task is started before the old one drains. |
| **Vercel env** | Terraform owns the multiplayer set on the colton-games project (see below), and only that set. |
| **Automation bypass** | Protection Bypass for Automation is on, with `is_env_var`, so deployments see it as `VERCEL_AUTOMATION_BYPASS_SECRET`. The lobby passes it to engines as `MP_API_BYPASS`. |
| **OIDC** (`terraform_data.games_mp_vercel_oidc`) | GETs the project and PATCHes only `oidcTokenConfig` to `{enabled, team}` if it differs, then verifies with another GET. It was already set on 2026-09-27. |
| **Preview Supabase** (`terraform_data.games_mp_preview_supabase`) | Copies `SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_URL` and `SUPABASE_SECRET_KEY` from Production into Preview-only variables. It never writes a variable that reaches Production or Development. |
| **Migration** (`terraform_data.games_mp_migration`) | Applies `supabase/migrations/20260926000000_mp.sql` from the same pinned commit. It connects with `psql` using the Supabase integration's own `POSTGRES_URL_NON_POOLING`, decrypted from Vercel at apply time, with `sslmode=verify-full` against the pinned Supabase Root 2021 CA. It reads `mp_private.schema_revision` first: at the file's revision it does nothing, and a partial or out-of-order state stops it. It runs nothing else. |
| **Health** (`terraform_data.games_mp_healthy`) | Waits for the router's new deployment to report COMPLETED (a rollback fails the apply), for a healthy target, and for `GET /healthz` = `200 ok`. The health request goes to the ALB with the certificate verified for `play.cc-games.app`. It gives up after 15 minutes and fails the apply. |

### The Vercel env Terraform writes

| Variable | Production | Preview |
| --- | --- | --- |
| `MP_LAUNCH_ROLE_ARN` | `games-mp-launcher` | `games-mp-launcher-preview` |
| `MP_LAUNCH_FUNCTION` | `games-mp-launch-production` (its ARN) | `games-mp-launch-preview` (its ARN) |
| `MP_REGION`, `AWS_REGION=us-west-1` | yes | yes |
| `MP_LAUNCHER=ecs`, `MP_PUBLIC_ENTRY=wss://play.cc-games.app` | yes | yes |
| `MP_ENV` | `production` | `preview` |
| `MP_API` | `https://cc-games.app` | not set: the lobby uses `https://$VERCEL_URL` for each deployment |
| `MP_TOKEN_KEYS`, `MP_TEST_KEY`, `CRON_SECRET` (sensitive) | yes | same values |
| `MP_COOKIE_SECRET` (sensitive) | its own value | its own value |
| `SKYHOOK_LEADERBOARD_ENVIRONMENT=preview`, `SKYHOOK_LEADERBOARD_SECRET` (sensitive, preview's own) | no | yes |
| `SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_URL`, `SUPABASE_SECRET_KEY` | the integration's own variables | copied by the Preview Supabase step |

Development gets none of them. Local development uses `MP_LAUNCHER=local`. The launcher
roles and the router trust only Production and Preview, and each environment gets its own
role and its own launch function. The cluster, subnets and engine security group are the
launch functions' settings; the lobby never names them.

Vercel applies an env change only when a deployment is built. The new values reach
Production with its next deployment, and Preview with each new preview.

### What is in Terraform state

The generated secrets are stored in state. That is the encrypted, versioned S3 backend
that only administration can read, the same boundary as the Secrets Manager copies.
Terraform is pinned to 1.10.3, which has no write-only arguments.

Some values are never written to state or to disk:

- the Vercel API token and the GitHub PAT, which are read at apply time;
- the Supabase database URL;
- the Supabase values copied to Preview.

`bringup.py` keeps them in memory. It sends them only in HTTP headers, request bodies or a
child process's environment, never on a command line, and never prints them.

For the remote e2e run:

- `terraform output -raw games_mp_test_key`
- `terraform output -raw games_mp_cron_secret`
- `terraform output -raw games_mp_automation_bypass_secret` (the `x-vercel-protection-bypass` value for previews)

## Shipping a new version

1. Merge the games-repo change and note its full commit sha.
2. Set `games_mp_source_ref` to it in `games-multiplayer.tf`, and `games_mp_sim_versions`
   if a game's `SIM_VERSION` changed. The build fails if the two disagree.
3. Merge that change, then plan and apply.

The apply builds the new tags and registers new task definition revisions. It rolls the
router and waits for it to be healthy, and applies the migration if the commit has a new
one. Old engine revisions stay ACTIVE, so clients still on the old version keep matching
until they update: the launch function runs the revision whose image carries the match's
simVersion (see "The launch function" below). Never deregister an engine revision while
clients may still send its simVersion. An old version's image stays in ECR only while the
repository's last-20 lifecycle rule keeps it; a launch whose image has expired fails its match.

**Adding a game** takes three entries:

- one in `local.mp_games`;
- its `SIM_VERSION` in `games_mp_sim_versions`;
- its image in the games repo's `scripts/mp-images.mjs`.

## Rotating secrets

- **Token key.** Deployments keep the `MP_TOKEN_KEYS` they were built with, and every
  Production and Preview deployment shares the one router, so:
  1. Set `games_mp_token_kids = ["kid2", "kid1"]` and apply. The router rolls onto a new
     task definition that accepts both keys.
  2. Redeploy Production, and redeploy or retire every Preview deployment still in use.
     Until then they keep signing with `kid1`, which the router still accepts.
  3. Wait at least two minutes, so every token signed with `kid1` has expired.
  4. Set `["kid2"]` and apply.
- **Any other generated secret.** Run `terraform apply -replace=random_password.<name>`,
  then redeploy the site. Replacing `games_mp_cookie_secret` resets every guest identity.
- **Preview Supabase values.** If the integration rotates its keys, bump
  `games_mp_preview_supabase_sync` and apply.

## Emergency switches

To take the platform off the internet, fastest first. Every switch that lasts is a committed
default applied from main: a one-shot `terraform apply -var ...` is undone by the next ordinary
apply, so never use one on its own.

- **No new matches, and stop what runs.** Commit `games_mp_engine_ceiling` defaulting to `0`
  and apply: both launch functions' shares become 0, so they refuse every launch, and within a
  minute the sweeper stops every engine (never the router, which it tells apart by family).
  While that commit is in review, break-glass (AGENTS.md allows documented recovery) stops
  launches at once by throttling both functions to zero; the next apply restores their
  concurrency, so land the commit. To stop only Preview, throttle just `games-mp-launch-preview`.

  ```bash
  for env in production preview; do
    aws lambda put-function-concurrency --region us-west-1 --function-name "games-mp-launch-$env" \
      --reserved-concurrent-executions 0
  done
  ```

- **Stop running engines.** Tell the router apart by its task-definition family,
  `games-mp-router`, never by `group` or tags: a `RunTask` caller chooses both, so a malicious
  engine started with `group=service:mp-router` would survive a group-based filter. The family is
  the one thing no launch can use (the launch function runs only the engine revisions and is
  denied `RunTask` on the router's), which is also why the sweeper and the launch function's
  count key on it.

  Launches may still be in flight when you start, so one pass is not enough: repeat until
  three passes in a row, 20 seconds apart, find nothing but the router.

  ```bash
  # A failed AWS call must never count as a quiet pass: it resets the count instead.
  quiet=0
  while [ "$quiet" -lt 3 ]; do
    found=0
    if ! tasks=$(aws ecs list-tasks --region us-west-1 --cluster games --query 'taskArns[]' --output text); then
      echo "list-tasks failed; not a quiet pass" >&2; found=1; tasks=
    fi
    for t in $tasks; do
      td=$(aws ecs describe-tasks --region us-west-1 --cluster games --tasks "$t" \
        --query 'tasks[0].taskDefinitionArn' --output text) || { echo "describe failed: $t" >&2; found=1; continue; }
      case "${td##*/}" in
        games-mp-router:*) ;;
        *) aws ecs stop-task --region us-west-1 --cluster games --task "$t" >/dev/null \
             || echo "stop failed: $t" >&2
           found=1 ;;
      esac
    done
    if [ "$found" -eq 0 ]; then quiet=$((quiet + 1)); else quiet=0; fi
    sleep 20
  done
  ```

- **Close the public entry.** Autoscaling owns the router count (2 to 6), so commit
  `mp_router_min_count` and `mp_router_max_count` defaulting to `0` and apply. The router tasks
  stop, the ALB answers 503, and no connection reaches an engine.
- **Tighten the edge.** The WAF's per-IP limit is `limit` in `rate-per-ip`
  (`games-multiplayer-edge.tf`); lowering it and applying takes effect within a minute.
- **Lower the engine ceiling.** Commit a lower `games_mp_engine_ceiling` default: the launch
  functions refuse launches at their new shares at once (preview `min(8, ceiling)`, production
  the rest, so below 8 production gets nothing), and within a minute the sweeper stops the
  newest engines above it. Terraform warns (check `games_mp_lobby_caps_fit_the_launch_ceilings`)
  when a share falls below that environment's lobby cap.

Other switches:

- **`games_mp_build = false`** stops CodeBuild runs. The task definitions then use
  `mp_router_image_tag` and `mp_engine_image_tags` exactly as given, and those tags must
  already be in ECR. Use it when GitHub or CodeBuild is down and a known-good image must be
  pinned.
- **`games_mp_migrate = false`** skips the migration step.

## What still needs a human

- **Merging the games PRs to production**, which is EJ's "push to prod". Terraform
  prepares everything, but the lobby code and its env reach users only with a production
  deployment.
- **Fixing whatever a failed preflight names**, for example storing a new Vercel token. The
  plan fails with the exact fix, and nothing has changed.

## The launch functions: how an engine starts

There is one launch function per environment, `games-mp-launch-production` and
`games-mp-launch-preview`, deployed from the same source (`games-multiplayer.tf`, source
`games-multiplayer/launch.py`). The lobby's roles can do one thing: invoke their own
environment's function. They have no ECS, IAM or EC2 permission at all.

- **Request.** `{"action":"start","matchId","game","simVersion","secret","hardCapSec","apiBase"}`,
  plus `"apiBypass"` from a preview. Any other field (`overrides`, `tags`, `taskDefinition`, `env`,
  a size, a role) is refused, not dropped. `matchId` is a lowercase UUID, `game` must have a
  registered task definition, `simVersion` is the match's (the lobby's shape, 1 to 64 of
  `A-Za-z0-9._-`, matched in full), `hardCapSec` is 60 to 14,400, `apiBase` must be
  `https://cc-games.app` on production or this project's own `*.vercel.app` deployment URL on
  preview, and only preview may pass a bypass secret. `{"action":"stop","matchId"}` stops that
  match's engine of the caller's own environment, never another environment's or the router.
- **Environment from the function.** Each function's environment is its own configuration
  (`LAUNCH_ENV`, with its ceiling, allowed `apiBase` and whether a bypass may be passed), set by
  Terraform. That, not the request or the invoked ARN, decides the engine's `MP_ENV`, its `env`
  tag, the allowed `apiBase`, the bypass and the ceiling. Each launcher role's policy names only
  its own environment's function ARN. A function deployed without a usable environment refuses
  every call (`forbidden`). The functions publish no versions or aliases.
- **Which revision: the simVersion.** For the game's current simVersion (Terraform passes it
  per game in `ENGINE_IMAGES`), the exact revision Terraform registered (`TASK_DEFINITIONS`),
  with no ECS read. For any other, the newest ACTIVE revision of `games-<game>` (Terraform's
  `skip_destroy` keeps old ones ACTIVE) whose only container is `engine` with image exactly
  `<the game's ECR repository>:<simVersion>-<12 hex>`; the repository is the one Terraform
  created for the game, so a revision pointing at any other image is never run whatever its tag.
  None: `{"ok":false,"error":"unknown-sim-version"}` and nothing is launched. A lookup (a miss
  too) is cached for a minute per game and simVersion in the warm function.
- **What it runs.** That revision (IAM allows `RunTask` on `games-<game>:*` for exactly the
  games in `local.mp_games`, only on cluster `games`, and denies the router's family;
  `ecs:ListTaskDefinitions` and `ecs:DescribeTaskDefinition` are read-only and take no resource,
  so they are on `*`), `launchType FARGATE`, the engine
  subnets and security group with `assignPublicIp=ENABLED`, `clientToken` = `<env>-<match id>`
  and `startedBy` = the match id. Container `engine` gets `MATCH_ID`, `MATCH_SECRET`, `MP_API`, `GAME_ID`, `MP_ENV`,
  `PORT=8080` and, on preview, `MP_API_BYPASS`; tags `game`, `match`, `env`, `hardcap`. Nothing
  else: no command, role, size or capacity provider.
- **Retries.** Before admission it looks the match up by `startedBy`: if this environment's live
  engine for that match already runs (a retry after a lost response, even in a fresh execution
  environment), it returns that task (`"repeat": true`) instead of counting it against the
  ceiling or launching again. `clientToken` is `<env>-<match id>`, so a preview launch can never
  hold a production match's token and a retry cannot start a second engine; when the match's
  engine has died, ECS answers that token with the dead task (or a conflict, if the launch
  parameters changed), and the function relaunches once with a token of its own.
- **Admission.** Before `RunTask`, it lists the cluster's running tasks and describes them.
  Every task whose family is not `games-mp-router` and that is not stopping is an engine, the
  sweeper's rule. Each function counts the engines whose `env` tag is its own environment, plus
  every engine with no readable `env` tag (those count against both), and refuses
  (`{"ok":false,"error":"capacity"}`) at its own ceiling. The `env` tag is set only by these
  functions (and administrators).
- **The ceiling is split, not shared.** `games_mp_engine_ceiling` (30) is divided between the
  functions: preview `min(8, ceiling)`, production the rest, so 22 + 8 by default and 0 + 0 at
  the kill switch. The shares sum to the total, so the total is bounded by construction with no
  lock between the functions, and one environment's engines never take the other's room. The
  lobby's caps (production 20, preview 5) fit inside the shares; Terraform warns if a lower
  ceiling makes one not fit.
- **Why the count is safe.** Each function has reserved concurrency 1: one invocation per
  environment runs at a time, so that environment's count-then-launch never interleaves, and
  the two environments never wait on each other's slot. ECS reads lag a new task by a moment, so
  it also counts every task it launched in the last two minutes that ECS does not yet show,
  until ECS reports it stopping. The gap it cannot close: right after a cold start, tasks the
  previous execution environment launched in its last seconds may be missed, an overshoot of a
  few engines that the sweeper stops within a minute. A launch that arrives while another of
  the same environment runs is throttled; the lobby's client retries it (up to 6 attempts).
  Asynchronous invocations (`InvocationType=Event`, which IAM cannot refuse) are never retried
  and are dropped after 60 seconds (`aws_lambda_function_event_invoke_config`), and each
  function has its own queue.
- **Stop right after a launch.** ECS may not list or describe a task it started a moment ago,
  so a stop can find nothing while the engine is starting. The function then calls `StopTask`
  directly on the task it launched for that match in the last two minutes (and only that one),
  retrying briefly while ECS cannot find it; if it still cannot, it answers
  `{"ok":false,"error":"not-yet-visible"}` and keeps counting the task, and the lobby retries
  the stop. A stop in a fresh execution environment has no record of that task, so an engine
  missed there runs until it exits or the sweeper's hard cap.
- **Reply.** `{"ok":true,"taskArn","taskDefinition"}` for a start, `{"ok":true,"stopped"}` for a
  stop, or `{"ok":false,"error"}` with `capacity`, `unknown-sim-version`, `not-yet-visible`,
  `bad-request` (and the `field`) or `forbidden`. An ECS failure raises (a Lambda error, in its
  metrics). One log line per call in `/aws/lambda/games-mp-launch-<environment>`, without the
  secret or the bypass.

The sweeper stays as the backstop. It runs every minute, stops any engine older than its
`hardcap` + 10 minutes (the hardcap clamped to 4 hours; 2 hours without a valid tag), and stops
the newest engines above the ceiling, which now happens only for launches around the
functions (an administrator) or their cold-start gap. The only task it never stops is one of the router's
family.

### Cutover from direct RunTask (once)

Before this, the lobby held `ecs:RunTask` itself. There is no dual path: the apply removes it.

1. Push the colton-games change that invokes the launch function
   (`lib/multiplayer/launcher/ecs.ts` using `MP_LAUNCH_FUNCTION`, sending the match's
   `simVersion`; a request without one is refused `bad-request`; retrying a stop answered
   `not-yet-visible`), but do not deploy it to production yet. A preview built before step 2 answers `503 multiplayer-not-configured`,
   because `MP_LAUNCH_FUNCTION` does not exist yet; that is expected.
2. Plan and apply here. The plan must show: the two functions (`games-mp-launch-production`,
   `games-mp-launch-preview`), their log groups and async-invoke configs, their shared role
   `games-mp-launch` and its policy, the new `games-mp-launcher-preview` role and policy, the
   production role's policy replaced
   by `invoke-games-mp-launch` (its role only moves to `["production"]`, not recreated), the
   Vercel variables `MP_LAUNCH_ROLE_ARN` and `MP_LAUNCH_FUNCTION` per environment created, and
   `MP_ROLE_ARN`, `AWS_ROLE_ARN`, `MP_CLUSTER`, `MP_SUBNETS`, `MP_ENGINE_SG` deleted. From this
   moment, any deployment still running the old lobby fails its launches with AccessDenied
   (closed, not open).
3. Redeploy: rebuild the previews that should keep working from the new code, and deploy
   production when the games PRs merge. Vercel applies env only at build, so only deployments
   built after step 2 have the new variables.
4. Check: start a match on a preview; `/aws/lambda/games-mp-launch-preview` shows
   `"env": "preview"` and a task ARN, and `aws ecs describe-tasks` shows its `env=preview` tag.

### Moving the router and engines into their own subnets (once)

The router and the engines used to run in the dev fleet's `subnet_a`/`subnet_b`. They now have
their own (`games-multiplayer.tf`, "Network"), and the ALB stays where it was:

| Subnets | Holds | Route table | ACL |
| --- | --- | --- | --- |
| `10.0.1.0/24`, `10.0.2.0/24` (`subnet_a`, `subnet_b`) | dev fleet, jumpboxes, the ALB | public: internet + I/O-box peer | default |
| `10.0.64.0/24`, `10.0.65.0/24` (`games-engine-a/b`) | match engines | `games-rt`: internet only | `games-engine` |
| `10.0.66.0/24`, `10.0.67.0/24` (`games-router-a/b`) | `mp-router` | `games-rt`: internet only | default |

Each pair is one subnet in each of `subnet_a`'s and `subnet_b`'s AZs, with an IPv6 /64 like
theirs, so tasks keep their IPv6 address and engine egress stays 443 on v4 and v6. Tasks still
get a public IPv4 for outbound; there is no NAT gateway.

The plan adds the four subnets, `games-rt` and its four associations, and the `games-engine`
ACL; updates in place the `mp-router` service's subnets, `games-mp-launch`'s `SUBNETS` and the
I/O box's security group; and registers a new router task definition. The ALB, the service
and the I/O box are not replaced. `games-mp-launch` switches subnets only after the health step
has seen every router task on the new definition. Apply it when no engine runs
(`aws ecs list-tasks --cluster games` lists only the router) **and nothing can launch one**
during the apply, so no engine is left in, or started into, the old subnets. The first time,
that holds by order: it is applied right after the launch function is created and before any
lobby that calls it is deployed (CoderColton/colton-games#64). If launches are ever live when
subnets move, pause them for the apply with the committed switch `games_mp_engine_ceiling = 0`
(Emergency switches) and restore it afterwards. Check afterwards: a new engine's ENI is in a `games-engine-*` subnet, and a match
plays.

## Environments and the router

One router serves Production and Preview. It accepts a join token whose `n` is in
`MP_ENVS`, which is `production,preview`. That check is a correctness guard, not a security
boundary: the real boundary is which Vercel environments hold `MP_TOKEN_KEYS`, and those
are Production and Preview only. Preview origins
(`https://colton-games-<hash>-coltons-projects-7f9a4e8b.vercel.app` and the
`colton-games-git-<branch>-...` aliases) match the router's `MP_ALLOWED_ORIGINS` entry
`https://colton-games-*-coltons-projects-7f9a4e8b.vercel.app`, where `*` is one DNS label.

## Security design and threat model

This is the first surface where people outside the family reach AWS resources we run. The
site at `cc-games.app` is public, so anyone can ask the lobby for a match, and
`play.cc-games.app` is an internet-facing ALB with no Cloudflare or Access in front. Checked
against the code and live state on 2026-09-27 unless marked otherwise.

**The chain, and what each hop checks**

1. **Browser → ALB** (`games-multiplayer.tf`, `aws_lb.games_play`). Ports 80 and 443 from
   anywhere, IPv4 and IPv6; 80 only redirects. TLS policy `ELBSecurityPolicy-TLS13-1-2-2021-06`,
   `drop_invalid_header_fields = true`. **WAF** web ACL `games-play`
   (`games-multiplayer-edge.tf`): blocks any IP above 6,000 requests per 60 s, blocks the AWS
   IP-reputation and known-bad-inputs managed groups, and runs the common rule set in COUNT
   until its matches on game traffic have been reviewed. WebSocket frames after the upgrade
   are not WAF requests, so play costs one request per connection. Blocks and counts are
   logged to `aws-waf-logs-games-play` (30 days); ALB access logs go to
   `games-play-alb-logs-<account>` (30 days).
2. **ALB → mp-router** (`aws_ecs_service.games_mp_router`). At least two router tasks,
   autoscaled on CPU up to six, so one task is never every match's single point of failure.
   The router's security group admits only the ALB. The router (games repo `server/mp-router/`) requires a valid join token,
   checks `Origin` against `MP_ALLOWED_ORIGINS` (live: the three production hosts and
   Colton's preview pattern; no-`Origin` clients are refused because `MP_ALLOW_NO_ORIGIN` is
   unset), and limits each client to 20 open connections and 50 requests/s (burst 100). The
   client address is the one the ALB appended to `X-Forwarded-For` (`MP_TRUSTED_HOPS=1`), so
   a client cannot spoof it; IPv6 clients are limited per /64.
3. **Join token.** HMAC-SHA256 with the keys in `games/mp-token-keys`, bound to one match,
   one environment and one seat, and valid for two minutes. The key is symmetric, so everything
   that verifies a token can also mint one: the Vercel lobby (`MP_TOKEN_KEYS` on Production and
   Preview) and the router, which gets the key as `MP_TOKEN_KEYS` to check tokens. The router's
   execution role and administrators are the only AWS readers (secret policy,
   `games-multiplayer.tf`).
4. **Router → engine.** The router runs in its own subnets (`10.0.66.0/24`, `10.0.67.0/24`) and
   forwards only to addresses inside `MP_TARGET_CIDRS` (the two engine subnets,
   `10.0.64.0/24` and `10.0.65.0/24`); its security group's egress reaches only the engine
   group on 8080 plus HTTPS. The engine group admits only the router, which is why an engine may
   trust the router's `X-MP-*` identity headers.
5. **Lobby → AWS.** Vercel functions exchange a Vercel OIDC token for a launcher role; no AWS
   key is stored. Production assumes `games-mp-launcher`, Preview `games-mp-launcher-preview`;
   each trust is exactly project `colton-games` in team `coltons-projects-7f9a4e8b` and that one
   environment. Each role's only permission is `lambda:InvokeFunction` on its own environment's
   launch function, `games-mp-launch-<environment>`: no ECS, no PassRole, no EC2, no secrets.
6. **Launch functions → ECS.** `games-mp-launch-<environment>` (above) validates the request,
   counts its environment's engines and refuses at its share of the ceiling, then runs a revision of the game's own engine family whose image
   is in the game's own ECR repository with the match's simVersion, in cluster `games` only
   (router **explicitly denied**). It can pass only the two
   engine roles (to ECS tasks only), tag only at launch, and stop only `match`-tagged tasks
   (router tasks explicitly denied). Reserved concurrency 1 per function.
7. **Engines.** The task role has **no policies** (live: 0 attached, 0 inline). Engines run in
   their own subnets, `10.0.64.0/24` and `10.0.65.0/24`, apart from the dev fleet. Their route
   table (`games-rt`) has the internet and nothing else: **no route to the I/O box's VPC peer**,
   so an engine cannot send a packet toward its NFS export, and the I/O box admits NFS only
   from the dev-fleet and parallel-box subnets anyway. Engines get a public IPv4 for outbound
   traffic; their security group's egress is **TCP 443 only** (plus the ECS task metadata
   endpoint), which is all the engine kit uses: HTTPS callbacks to the lobby, image pulls and
   logs. Inbound is router-only. Under the security group, the subnets' network ACL
   `games-engine` is a second, stateless fence: in, 8080 only from the router subnets and
   replies from the internet; out, 443 to the internet and replies to the router; everything
   else to or from `10.0.0.0/16` or the VPC's IPv6 /56 is denied. The container still runs as
   root with a writable root filesystem.

**What an attacker can and cannot do**

| Attacker | Can | Cannot |
| --- | --- | --- |
| Anyone on the internet | Reach the ALB and router, up to 6,000 requests per IP per minute; hold connections open (idle timeout 3600 s); push a *distributed* flood that stays under the per-IP limit against 2–6 autoscaled routers | Reach an engine without a token; spoof its IP past the ALB; talk to any other port or host; get past the WAF from a known-bad IP |
| A lobby user | Ask for matches, within the lobby's admission limits (below) | Launch a task directly; see other players' tokens |
| Code in any Preview build (every writer on `CoderColton/colton-games`, and every dependency such a build pulls in) | Invoke `games-mp-launch-preview`: start up to 8 preview engines of the registered engine images (the preview share), each for up to its `hardCapSec` (at most 4 h) plus 10 minutes, calling back only a `colton-games-*` preview URL; stop preview engines; keep the preview function's own slot busy, which slows only preview launches; read the lobby's Supabase data | Run a command, image, role or size of its choosing; launch production engines or more than 8 of its own; take production's share or its launch slot; stop production engines or the router; reach ECS, EC2, IAM or secrets directly |
| A compromised production deployment | The same through `games-mp-launch-production`, up to production's share of 22 engines | Everything in the row above, with production and preview swapped: it cannot launch preview engines or take preview's 8 |
| A compromised router (it parses internet input) | Mint valid join tokens for any match (it holds the HMAC key), so join any match as any seat; see and drop every player's traffic; reach every engine | Launch or stop tasks; read other secrets (its execution role reads only its key); connect anywhere but the engines on 8080 and HTTPS on 443 (its security group's only egress), so not the admin fleet's SSH or ET either |
| A compromised engine | Reach any internet host on TCP 443; use the Vercel protection-bypass secret it is given as `MP_API_BYPASS` | Call AWS (empty task role); reach another engine (router-only ingress); reach any host in the VPC, on any port, over IPv4 or IPv6 (its own subnets, security group and `games-engine` ACL); route to the I/O box or its NFS export (no peer route, and NFS admits only the dev-fleet and parallel-box subnets); reach SSH, databases or any non-443 service on the internet |

**Cost-abuse limits, and what happens at each**

- **Lobby admission (games repo, enforced in SQL at the launch claim; values pinned by
  Terraform** in the Vercel env, `games-multiplayer.tf` locals). At most
  `MP_MAX_ACTIVE_MATCHES` unfinished matches per environment (production 20, preview 5); per
  client `MP_IP_MAX_ACTIVE` (3) unfinished and `MP_IP_MAX_PER_HOUR` (10) launches; one
  unfinished seat per player. At the limit the lobby answers `429 launch-limit` or
  `503 capacity`. Saturated, that is 25 engines, about $2.40/hour. Today production exposes only
  the hidden `mptest` game, which needs `MP_TEST_KEY`, so the public cannot launch anything yet.
- **AWS-side admission, independent of the lobby.** Each environment's launch function
  refuses at its share of `games_mp_engine_ceiling` (30): production 22, preview 8 (every task
  except the router's family counts, untagged ones against both). Saturated, that is 30
  engines, about $2.90/hour; preview alone cannot take more than 8, nor production more than 22.
- **AWS-side ceiling backstop.** Every minute the sweeper counts running engines the same way
  and, above the same ceiling, stops the newest excess ones and alerts. It publishes
  `GamesMultiplayer/RunningEngines`; `games-mp-engines-over-lobby-caps` pages when more than the
  lobby's 25 run for 5 minutes, which means the lobby's own admission is broken or something is
  launching through a launch function up to its share.
- **Per-match caps.** Each engine exits at its own `hardCapSec` (30 minutes for `mptest`);
  the sweeper stops any task at `hardcap` + 10 minutes (the hardcap itself clamped to 4 hours), or 2
  hours + 10 minutes without a valid tag, plus up to one sweep interval. That is a bound only
  while sweeps and `StopTask` succeed: a failed stop is caught and alerted, and the task keeps
  running until someone intervenes. Two alarms page if the sweeper errors or stops running.
- **Spend.** Budget `games-ecs-daily` ($15/day, ECS only) plus the account's $200/day budget.
  Both lag by hours; the ceiling is the fastest control. `AWS/Billing` metrics are not
  published in this account (billing alerts are off), so `EstimatedCharges` alarms, including
  the older `high-ec2-daily-spend`, never have data.

**Monitoring.** Launch refusals and errors in `/aws/lambda/games-mp-launch-production` and
`/aws/lambda/games-mp-launch-preview` (one line per call) and their `AWS/Lambda` metrics; sweeper errors and silence (`games-mp-sweeper-errors`,
`games-mp-sweeper-not-running`); running engines (`games-mp-engines-over-lobby-caps`); router
health (`games-play-unhealthy-router`, `games-play-no-healthy-router`); 5xx share at the router
and the ALB (`games-play-target-5xx-rate`, `games-play-elb-5xx-rate`); the ECS and account
budgets; WAF metrics and logs; ALB access logs; router and engine logs in CloudWatch (14-day
retention); and the all-traffic VPC flow log (`aws_flow_log.security_main`, archived under
`vpc-flow/` in the security-audit bucket), which records every connection by address and port. The ALB appends to `X-Forwarded-For` (live: mode `append`), which the router's
client-address logic relies on.

**Closed** (2026-09-27): the AWS-side engine ceiling (detective: a sweep every minute) and its alarm, the ECS budget, the WAF,
two-plus autoscaled routers, ALB access logs, router health and 5xx alarms, HTTPS-only engine
egress, and the lobby's limits pinned in Terraform. Admission control on launches: the lobby
holds no ECS permission, and a launch function per environment builds every `RunTask` from
fixed settings and refuses at its share of the ceiling. (2026-09-28) Preview no longer slows
production launches: each environment has its own function with its own concurrency slot and
async queue, and the ceiling is split between them (22 + 8) instead of shared, so neither
needs a lock or can take the other's room. Preview can still spend its own 8 engines for up to
about 4 hours each; that share is the accepted cost of letting every writer test multiplayer. Engine and router subnets of their own, with no route to the I/O box
peer and an ACL fencing engines off from the VPC, and the I/O box's NFS narrowed from both
whole VPCs to its clients' subnets.

**Still open, most severe first:**

1. The common managed rule set runs in COUNT. Review its matches in `aws-waf-logs-games-play`
   after real play, then flip `common` to BLOCK.
2. Engines run as root with a writable root filesystem and receive the preview
   protection-bypass secret. Fix: a non-root read-only container, callbacks without the bypass.
   (Their subnets are closed: engines no longer share the dev fleet's subnets and cannot route
   to the I/O box's NFS export; see chain item 7.)
3. The router can mint join tokens, because the token key is symmetric. Fix: sign with a
   private key only the lobby holds and verify with its public key in the router (Ed25519),
   so a compromised router can no longer mint tokens.
4. Billing alerts are off account-wide, so no `EstimatedCharges` alarm can fire. Turning them on
   is an account setting outside Terraform (Billing preferences, "Receive Billing Alerts").

## Monthly cost (us-west-1 list prices, checked 2026-09-26/27)

Fargate ARM in us-west-1 costs $0.03725 per vCPU-hour and $0.00409 per GB-hour. An ALB costs
$0.0252 per hour plus $0.008 per LCU-hour. A public IPv4 address costs $0.005 per hour.
CodeBuild ARM small costs $0.00425 per build minute.

| Item | Monthly |
| --- | --- |
| ALB `games-play`: hours, about $18.40, plus its two public IPv4 addresses, $7.30 | about $26 |
| ALB LCUs at family scale | about $1–3 |
| `mp-router`, 2 tasks (autoscaling's minimum) at 0.25 vCPU / 0.5 GB: compute about $16.60, plus public IPv4 $7.30 | about $24 |
| WAF `games-play`: $5 per web ACL plus $1 per rule (4), plus $0.60 per million requests; the three AWS managed groups carry no extra fee | about $9–10 |
| WAF logs (blocks and counts only) and ALB access logs, 30 days each | under $1 |
| Secrets Manager, 3 secrets at $0.40 | $1.20 |
| ECR storage, 14-day logs, sweeper Lambda (now every minute) and Scheduler, the two launch Lambdas (one call per match start or stop; reserved concurrency costs nothing), the build bucket | under $2 |
| **Always on** | **about $64** |
| Per build: about 5 minutes of CodeBuild | about $0.02 |
| Per match: 2 vCPU / 4 GB plus a public IPv4, about $0.096 per hour | **about $0.016 per 10-minute match** |

Internet data out beyond the account's free 100 GB per month costs about $0.09/GB.
