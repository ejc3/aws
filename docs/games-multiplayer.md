# Games multiplayer: bring-up, shipping and costs

The Colton Games multiplayer platform runs one Fargate task per match. Players enter
through `wss://play.cc-games.app/m/<id>`. The design and the binding names are in the games
repo (`CoderColton/colton-games`): `docs/MULTIPLAYER.md` and `docs/MULTIPLAYER-CONTRACT.md`.

The Terraform is in two files:

- `games-multiplayer.tf` holds the AWS and Cloudflare resources: ECR, the `games` cluster,
  IAM, the Vercel OIDC trust, security groups, the ALB, the certificate and DNS, the router
  and engine task definitions, and the sweeper.
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
| `MP_ROLE_ARN`, `AWS_ROLE_ARN` (same value; the lobby reads `MP_ROLE_ARN` first) | yes | yes |
| `MP_CLUSTER`, `MP_SUBNETS`, `MP_ENGINE_SG`, `MP_REGION`, `AWS_REGION=us-west-1` | yes | yes |
| `MP_LAUNCHER=ecs`, `MP_PUBLIC_ENTRY=wss://play.cc-games.app` | yes | yes |
| `MP_ENV` | `production` | `preview` |
| `MP_API` | `https://cc-games.app` | not set: the lobby uses `https://$VERCEL_URL` for each deployment |
| `MP_TOKEN_KEYS`, `MP_TEST_KEY`, `CRON_SECRET` (sensitive) | yes | same values |
| `MP_COOKIE_SECRET` (sensitive) | its own value | its own value |
| `SKYHOOK_LEADERBOARD_ENVIRONMENT=preview`, `SKYHOOK_LEADERBOARD_SECRET` (sensitive, preview's own) | no | yes |
| `SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_URL`, `SUPABASE_SECRET_KEY` | the integration's own variables | copied by the Preview Supabase step |

Development gets none of them. Local development uses `MP_LAUNCHER=local`. The launcher
role and the router trust only Production and Preview.

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
until they update.

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

To take the platform off the internet, fastest first:

- **No new matches.** The launcher role is the lobby's only way to start a task. Break-glass
  (AGENTS.md allows documented recovery): attach a deny-all inline policy, then record it and
  remove it by hand afterwards, because Terraform does not manage other inline policies on the
  role and would leave it in place:

  ```bash
  aws iam put-role-policy --role-name games-mp-launcher --policy-name emergency-deny \
    --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Deny","Action":"*","Resource":"*"}]}'
  ```

- **Stop running engines.** Tell the router apart by its task-definition family,
  `games-mp-router`, never by `group` or tags: a `RunTask` caller chooses both, so a malicious
  engine started with `group=service:mp-router` would survive a group-based filter. The family is
  the one thing the launcher cannot use (it is denied `RunTask` on it), which is also why the
  sweeper keys on it (`games-multiplayer/sweeper.py`).

  ```bash
  for t in $(aws ecs list-tasks --region us-west-1 --cluster games --query 'taskArns[]' --output text); do
    td=$(aws ecs describe-tasks --region us-west-1 --cluster games --tasks "$t" \
      --query 'tasks[0].taskDefinitionArn' --output text)
    case "${td##*/}" in
      games-mp-router:*) ;;
      *) aws ecs stop-task --region us-west-1 --cluster games --task "$t" >/dev/null ;;
    esac
  done
  ```

- **Close the public entry.** Autoscaling owns the router count (2 to 6), so set its range to
  zero: `terraform apply -var mp_router_min_count=0 -var mp_router_max_count=0`. The router
  tasks stop, the ALB answers 503, and no connection reaches an engine.
- **Tighten the edge.** The WAF's per-IP limit is `limit` in `rate-per-ip`
  (`games-multiplayer-edge.tf`); lowering it and applying takes effect within a minute.
- **Lower the engine ceiling.** `-var games_mp_engine_ceiling=<n>`: within a minute the sweeper
  stops the newest engines above it.

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

## What the lobby must do on RunTask

- Use task definition `games-<game>`, cluster `games`, subnets `MP_SUBNETS`, security group
  `MP_ENGINE_SG`, and `assignPublicIp=ENABLED`.
- Set the tags `game`, `match`, `env` and `hardcap` (the match's hard cap in seconds).
- The sweeper runs every 5 minutes. It stops any task in the cluster older than `hardcap` +
  10 minutes. If `hardcap` is missing or invalid, the limit is 2 hours, and `hardcap` is
  clamped to 4 hours.
- The only task the sweeper never stops is one from the router's task definition family,
  `games-mp-router`, which the launcher is denied RunTask on. RunTask's `group`,
  `startedBy` and tags are set by the caller, so none of them exempts a task.
- The launcher can tag only at RunTask, so it can't extend a running task's cap later.
- The launcher can StopTask only tasks tagged `match`, never a router task. Router tasks
  carry `games-role=router`, which an explicit Deny blocks.

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
   one environment and one seat, and valid for two minutes. Only the Vercel lobby (which holds
   `MP_TOKEN_KEYS` on Production and Preview) can mint one; the router's execution role and
   administrators are the only AWS readers (secret policy, `games-multiplayer.tf`).
4. **Router → engine.** The router forwards only to addresses inside `MP_TARGET_CIDRS`
   (the two engine subnets), and its security group's egress reaches only the engine group on
   8080 plus HTTPS. The engine group admits only the router, which is why an engine may trust
   the router's `X-MP-*` identity headers.
5. **Lobby → AWS.** Vercel functions exchange a Vercel OIDC token for the `games-mp-launcher`
   role; no AWS key is stored. The trust is exactly project `colton-games` in team
   `coltons-projects-7f9a4e8b`, environments `production` and `preview` only. The role can
   `RunTask` only `games-*` task definitions in cluster `games` (live simulation: engine
   allowed, router **explicitly denied**), pass only the two engine roles, tag only at launch,
   and stop only `match`-tagged tasks. It cannot read any secret (simulated). Its only EC2 access
   is read-only `ec2:DescribeNetworkInterfaces`, on every ENI in us-west-1
   (`ReadTaskNetworkInterfaces`, games-multiplayer.tf), which the launcher uses to find an
   engine's address; it exposes the region's ENI inventory to a compromised deployment.
6. **Engines.** The task role has **no policies** (live: 0 attached, 0 inline). Engines get
   a public IPv4 for outbound traffic; their egress is **TCP 443 only** (plus the ECS task
   metadata endpoint), which is all the engine kit uses: HTTPS callbacks to the lobby, image
   pulls and logs. Inbound is router-only. The container still runs as root with a writable
   root filesystem.

**What an attacker can and cannot do**

| Attacker | Can | Cannot |
| --- | --- | --- |
| Anyone on the internet | Reach the ALB and router, up to 6,000 requests per IP per minute; hold connections open (idle timeout 3600 s); push a *distributed* flood that stays under the per-IP limit against 2–6 autoscaled routers | Reach an engine without a token; spoof its IP past the ALB; talk to any other port or host; get past the WAF from a known-bad IP |
| A lobby user | Ask for matches, within the lobby's admission limits (below) | Launch a task directly; see other players' tokens |
| A compromised Vercel deployment, or code in any Preview build (every writer on `CoderColton/colton-games`, and every dependency such a build pulls in) | Call `RunTask` with any container override (IAM cannot restrict overrides), so run arbitrary commands in an engine image, with HTTPS-only egress. The sweeper stops the newest engines above `games_mp_engine_ceiling` (30) every minute and alarms, but that is cleanup, not admission control: between sweeps the caller can launch up to the account's Fargate quota (4,000 vCPU, about 2,000 engines) and relaunch what is stopped (open gap below). Each engine runs until the sweeper stops it (about 4h15 at most, and only while sweeps and `StopTask` succeed). Read the lobby's Supabase data; list every ENI in us-west-1 (read-only `ec2:DescribeNetworkInterfaces`) | Run the router's task definition; stop the router; pass any other role; read AWS secrets; change anything in EC2; touch other accounts |
| A compromised engine | Reach any host on TCP 443; use the Vercel protection-bypass secret it is given as `MP_API_BYPASS` | Call AWS (empty task role); reach another engine (router-only ingress); reach SSH, databases or any non-443 service, here or on the internet |

**Cost-abuse limits, and what happens at each**

- **Lobby admission (games repo, enforced in SQL at the launch claim; values pinned by
  Terraform** in the Vercel env, `games-multiplayer.tf` locals). At most
  `MP_MAX_ACTIVE_MATCHES` unfinished matches per environment (production 20, preview 5); per
  client `MP_IP_MAX_ACTIVE` (3) unfinished and `MP_IP_MAX_PER_HOUR` (10) launches; one
  unfinished seat per player. At the limit the lobby answers `429 launch-limit` or
  `503 capacity`. Saturated, that is 25 engines, about $2.40/hour. Today production exposes only
  the hidden `mptest` game, which needs `MP_TEST_KEY`, so the public cannot launch anything yet.
- **AWS-side engine ceiling, independent of the lobby.** Every minute the sweeper counts running
  engines (every task except the router's family) and, above `games_mp_engine_ceiling` (30),
  stops the newest excess ones and alerts. It publishes `GamesMultiplayer/RunningEngines`;
  `games-mp-engines-over-lobby-caps` pages when more than the lobby's 25 run for 5 minutes,
  which means something is launching around the lobby.
- **Per-match caps.** Each engine exits at its own `hardCapSec` (30 minutes for `mptest`);
  the sweeper stops any task at `hardcap` + 10 minutes (the hardcap itself clamped to 4 hours), or 2
  hours + 10 minutes without a valid tag, plus up to one sweep interval. That is a bound only
  while sweeps and `StopTask` succeed: a failed stop is caught and alerted, and the task keeps
  running until someone intervenes. Two alarms page if the sweeper errors or stops running.
- **Engine ceiling (detective, not preventive).** Every minute the sweeper stops the newest
  engines above `games_mp_engine_ceiling` (30) and alarms. It bounds sustained cost, not a burst:
  between sweeps a holder of the launcher's credentials can still launch up to the Fargate quota.
  True admission control needs launches to go through an AWS-controlled path (open gap below).
- **Spend.** Budget `games-ecs-daily` ($15/day, ECS only) plus the account's $200/day budget.
  Both lag by hours; the ceiling is the fastest control. `AWS/Billing` metrics are not
  published in this account (billing alerts are off), so `EstimatedCharges` alarms, including
  the older `high-ec2-daily-spend`, never have data.

**Monitoring.** Sweeper errors and silence (`games-mp-sweeper-errors`,
`games-mp-sweeper-not-running`); running engines (`games-mp-engines-over-lobby-caps`); router
health (`games-play-unhealthy-router`, `games-play-no-healthy-router`); 5xx share at the router
and the ALB (`games-play-target-5xx-rate`, `games-play-elb-5xx-rate`); the ECS and account
budgets; WAF metrics and logs; ALB access logs; router and engine logs in CloudWatch (14-day
retention). The ALB appends to `X-Forwarded-For` (live: mode `append`), which the router's
client-address logic relies on.

**Closed** (2026-09-27): the AWS-side engine ceiling (detective: a sweep every minute) and its alarm, the ECS budget, the WAF,
two-plus autoscaled routers, ALB access logs, router health and 5xx alarms, HTTPS-only engine
egress, and the lobby's limits pinned in Terraform.

**Still open, most severe first:**

1. **No admission control on launches.** The launcher's credentials call `RunTask` directly, so
   the ceiling is enforced only by the next sweep: a burst can reach the Fargate quota between
   sweeps, and IAM cannot restrict container overrides. The launcher also trusts **Preview**
   deployments, so every writer on `CoderColton/colton-games` (and every dependency a branch build
   pulls in) can obtain those credentials. Fix: route launches through an AWS Lambda that checks
   the ceiling with consistent counting and starts tasks with fixed settings (the launcher role
   keeps only `lambda:InvokeFunction`); as a stopgap, drop Preview from the launcher trust.
2. The common managed rule set runs in COUNT. Review its matches in `aws-waf-logs-games-play`
   after real play, then flip `common` to BLOCK.
3. Engines share the admin VPC's subnets (defence in depth; their egress is now 443-only), run
   as root with a writable root filesystem, and receive the preview protection-bypass secret.
   Fix: dedicated engine subnets, a non-root read-only container, callbacks without the bypass.
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
| ECR storage, 14-day logs, sweeper Lambda (now every minute) and Scheduler, the build bucket | under $2 |
| **Always on** | **about $64** |
| Per build: about 5 minutes of CodeBuild | about $0.02 |
| Per match: 2 vCPU / 4 GB plus a public IPv4, about $0.096 per hour | **about $0.016 per 10-minute match** |

Internet data out beyond the account's free 100 GB per month costs about $0.09/GB.
