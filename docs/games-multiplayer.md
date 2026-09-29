# Games multiplayer: bring-up, shipping and costs

The Colton Games multiplayer platform runs one Fargate task per match. Players enter
through `wss://play.cc-games.app/m/<id>`. The design and the binding names are in the games
repo (`CoderColton/colton-games`): `docs/MULTIPLAYER.md` and `docs/MULTIPLAYER-CONTRACT.md`.

The Terraform is in three files:

- `games-multiplayer.tf` holds the AWS and Cloudflare resources: ECR, the `games` cluster,
  IAM, the Vercel OIDC trust, security groups, the ALB, the certificate and DNS, the router
  task definition and service, the launch functions (the only way an engine starts) and the
  sweeper.
- `games-multiplayer-bringup.tf` holds what a working platform needs besides: secrets, Vercel
  settings, the main image build project, and a health check.
- `games-multiplayer-deploy.tf` deploys the games repo automatically: every commit on `main`
  goes to production and every other branch to preview, with no Terraform change (see
  [Automatic deploys](#automatic-deploys)).

## Bring-up: plan, apply, done

From the jumpbox:

```bash
cd ~/aws && git pull --ff-only && terraform plan && terraform apply
```

One apply goes from nothing to a healthy router, once the one input below exists. On a
platform built from nothing the apply waits (up to 30 minutes) for the first automatic main
release to create the router image tag it runs; on an existing one it never names a commit.

**First time only: Colton's read token.** The code lives in `CoderColton/colton-games`,
a private repo on Colton's personal account, and AWS reads it (GitHub is never given an AWS
credential). Only a token Colton owns can be limited to it:
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
  only* above) can read `main`, which is what `games-mp-poller` does every minute.

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
| **Secrets** | The tls provider generates the join-token keys: one Ed25519 key pair per lobby environment (Production, Preview) and per entry of `games_mp_token_kids`, key id `<env>-<kid>` (`production-kid1`, `preview-kid1`). Each environment's private keys go only to its own Vercel `MP_TOKEN_SIGNING_KEYS`; the public keys go to the router's task definition as the plain variable `MP_TOKEN_PUBLIC_KEYS` (`<env>:<env>-<kid>:<base64 SPKI>`), so a key change is a new task definition that the health step verifies, and to each environment's launch function (`games-mp-launch-<environment>`, setting `TOKEN_PUBLIC_KEYS`: that environment's keys only), which gives each engine it starts those public keys. The random provider generates `MP_TEST_KEY`, `CRON_SECRET`, `MP_COOKIE_SECRET` (one per environment), Preview's `SKYHOOK_LEADERBOARD_SECRET` and the automation-bypass secret; the test key and cron secret also go to Secrets Manager (`games/mp-test-key`, `games/mp-cron-secret`). No private token key is in Secrets Manager: nothing on AWS needs one. |
| **Router image** (`terraform_data.games_mp_router_live`) | Once. The router task definition runs `games/mp-router:live`, the one mutable tag in the games repositories, which the release function moves (see [Automatic deploys](#automatic-deploys)). This step creates it: as the image the `mp-router` service already runs, or, on a platform built from nothing, by waiting for the first main release. |
| **Current release** (`terraform_data.games_mp_current_bootstrap`) | Once. Writes production's current release (`current#main` in the `games-mp-releases` table) from the newest engine revision Terraform registered before automatic deploys, so production launches never wait for the first release. Written only if missing. |
| **Router** | The `games-mp-router` task definition and the `mp-router` service are created only after `live` exists. A rollout starts the new tasks before the old ones drain, and the old ones keep their connections for up to an hour. Engine task definitions are the release function's, never Terraform's. |
| **Vercel env** | Terraform owns the multiplayer set on the colton-games project (see below), and only that set. |
| **Automation bypass** | Protection Bypass for Automation is on, with `is_env_var`, so deployments see it as `VERCEL_AUTOMATION_BYPASS_SECRET`. The lobby passes it to engines as `MP_API_BYPASS`. |
| **OIDC** (`terraform_data.games_mp_vercel_oidc`) | GETs the project and PATCHes only `oidcTokenConfig` to `{enabled, team}` if it differs, then verifies with another GET. It was already set on 2026-09-27. |
| **Preview Supabase** (`terraform_data.games_mp_preview_supabase`) | Copies `SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_URL` and `SUPABASE_SECRET_KEY` from Production into Preview-only variables. It never writes a variable that reaches Production or Development. |
| **Database URL** (`terraform_data.games_mp_db_url`) | Copies the Supabase integration's own `POSTGRES_URL_NON_POOLING`, decrypted from Vercel's Production env, into `games/mp-db-url`, the one secret `games-mp-migrate` reads (administration and that CodeBuild role only). It writes only when the value changed, on stdin, never to state or disk. Bump `games_mp_db_url_sync` after the integration rotates its password. |
| **Health** (`terraform_data.games_mp_healthy`) | Waits for the router's new deployment to run its desired count (a circuit-breaker rollback fails the apply), for every one of its tasks to be a healthy target, and for `GET /healthz` = `200 ok`. It does not wait for ECS's COMPLETED, which comes only after the old tasks' hour-long drain. The health request goes to the ALB with the certificate verified for `play.cc-games.app`. It gives up after 15 minutes and fails the apply. |

### The Vercel env Terraform writes

| Variable | Production | Preview |
| --- | --- | --- |
| `MP_LAUNCH_ROLE_ARN` | `games-mp-launcher` | `games-mp-launcher-preview` |
| `MP_LAUNCH_FUNCTION` | `games-mp-launch-production` (its ARN) | `games-mp-launch-preview` (its ARN) |
| `MP_REGION`, `AWS_REGION=us-west-1` | yes | yes |
| `MP_LAUNCHER=ecs` | yes | yes |
| `MP_PUBLIC_ENTRY` | `wss://play.cc-games.org,wss://play.cc-games.net,wss://play.cc-games.app` (each page gets its own domain's entry) | not set: the lobby's default, `wss://play.cc-games.app` |
| `MP_ENV` | `production` | `preview` |
| `MP_API` | `https://cc-games.org` | not set: the lobby uses `https://$VERCEL_URL` for each deployment |
| `MP_TOKEN_SIGNING_KEYS` (sensitive): Ed25519 private keys, `<env>-<kid>:<base64 PKCS#8>`, first signs | Production's own | Preview's own |
| `MP_TEST_KEY`, `CRON_SECRET` (sensitive) | yes | same values |
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
Terraform is pinned to 1.10.3, which has no write-only arguments. The join-token private keys
(`tls_private_key.games_mp_token`) are in state exactly as the shared HMAC key they replaced
was; their only other copies are the two Vercel environments. The router's public keys are
not secret.

Some values are never written to state or to disk:

- the Vercel API token and the GitHub read token, which are read at apply time (and by
  `games-mp-poller`);
- the Supabase database URL (in `games/mp-db-url` for the migration, never in state);
- the Supabase values copied to Preview.

`bringup.py` keeps them in memory. It sends them only in HTTP headers, request bodies or a
child process's environment, never on a command line, and never prints them.

For the remote e2e run:

- `terraform output -raw games_mp_test_key`
- `terraform output -raw games_mp_cron_secret`
- `terraform output -raw games_mp_automation_bypass_secret` (the `x-vercel-protection-bypass` value for previews)

## Automatic deploys

Nothing ships through Terraform any more: there is no pinned commit. AWS pulls from GitHub;
GitHub holds no AWS credential and can start nothing here.

```
EventBridge Scheduler, every minute
  games-mp-poller      reads every branch head of CoderColton/colton-games (Colton's read-only
                       token), and for each commit it has not seen uploads the source to S3 and
                       starts a build:
                         main           games-mp-images          router + engines -> games/*
                         any other      games-mp-images-preview  engines only     -> games-preview/*
CodeBuild finished  --EventBridge-->  games-mp-release
  main       registers games-<game> engine revisions for the commit; runs games-mp-migrate
             first when the commit's mp schema revision is not the database's; makes the
             commit production's current release; moves games/mp-router:live and rolls the
             router when the router's own files changed
  preview    registers games-preview-<game> revisions for exactly that commit
  failed     recorded; main and migration failures mail cost-alerts
```

- **Production** (`games-mp-launch-production`) launches, on every call, the revision in
  `current#main` of the `games-mp-releases` table for the game's current simVersion, and for an
  older simVersion (a client still on the previous site) the newest main revision released for
  it (`sim#<game>#<simVersion>`). A merge reaches new matches about 5 minutes after it lands
  (poll, build, release). Main releases are ordered by the poller's sequence number, so a slow
  build of an older commit never replaces a newer release.
- **Preview** (`games-mp-launch-preview`) launches exactly the revision built from the lobby's
  own commit: a preview lobby sends `commit` (its `VERCEL_GIT_COMMIT_SHA`) with every launch.
  Until that commit's images are built and released it answers `engine-building` (a few
  minutes after the push), and `engine-build-failed` if its build failed; the lobby shows
  either and requeues the players. Every branch pushed to the repo is built, not only those
  with a pull request: Vercel builds a preview for every pushed branch, and listing pull
  requests would need a wider token. A fork's branches are never built.
- **What changed is what is built.** A main build pushes only the tags ECR lacks (tags are
  immutable, so an existing tag is already pushed), but every commit gets its own tags and
  revisions: engines `games/engines:<game>_<simVersion>-<sha12>` (previews
  `games-preview/engines:...`), the router `games/mp-router:<sha12>`. The router
  rolls only when its inputs changed: the Dockerfile and every file it copies, hashed by the
  build (`bringup.py dockerfile_inputs`). Previews build engines only.
- **Watching it.** `aws dynamodb get-item --region us-west-1 --table-name games-mp-releases
  --key '{"id":{"S":"current#main"}}'` is production's current release; `build#main#<commit>`
  and `build#preview#<commit>` are each commit's status (`building`, `migrating`, `released`,
  `superseded`, `failed`). Logs: `/aws/lambda/games-mp-poller`, `/aws/lambda/games-mp-release`,
  `/aws/codebuild/games-mp-images`, `/aws/codebuild/games-mp-images-preview`,
  `/aws/codebuild/games-mp-migrate`.
- **Rolling production back.** Revert on `main` (the normal path), or, at once, re-promote an
  earlier released main commit; it takes a fresh sequence number, so it wins until the next
  merge:

  ```bash
  aws lambda invoke --region us-west-1 --function-name games-mp-release \
    --cli-binary-format raw-in-base64-out --payload '{"action":"promote","commit":"<40 hex>"}' out.json
  ```

- **Stopping deploys.** Commit `games_mp_autodeploy` defaulting to `false` and apply: the poller
  stops, production keeps its current release, and nothing new is built.
- **Retrying.** A commit is built once. A failed main build alerts; push a fix (a new commit is
  a new build), or `aws codebuild retry-build --id <build id>`, whose success is released like
  the first. A failed release alerts too (`games-mp-release-errors`), and is not retried by
  itself: fix the cause and re-promote.
- **The token.** When Colton's `games/colton-games-read` expires, the poller fails every minute
  and `games-mp-poller-errors` alerts after 15 minutes; Colton regenerates the token and step 3
  of *First time only* replaces the value.

### What a release never does: kick a live match

- **Engines never change under a match.** A release only decides what the next launch runs.
  A running task keeps its image and revision; no revision is ever deregistered; production
  repositories never expire a tagged image (their lifecycle rule removes only untagged ones),
  so every selectable revision's image is there. Preview repositories expire images 45 days
  after the push, and a preview release record lives 30 (a branch still there is rebuilt).
- **Router rollouts drain.** A rollout (a router release, a key rotation, a scale-in) starts the
  new tasks, waits until they are healthy, then deregisters the old ones. The ALB sends a
  deregistered task no new connection but keeps every open one, a player's WebSocket
  included, for the target group's deregistration delay, 3600 s, its maximum; only then does
  ECS stop the task. The launch functions refuse a match hardcap above 3600 s, and an engine
  ends its match at its hardcap after boot, so every match connected through an old router
  ends before that router's drain does. The mp-test client does not reconnect by itself (a
  reconnect is its "reconnect" button, which fetches a fresh token), so the drain is what keeps
  a match alive; a game that wants matches longer than an hour must first reconnect its
  clients automatically on a dropped socket, and then `mp_router_drain_sec` can stay at the
  ALB's maximum while `MAX_HARDCAP_SEC` rises. The price: an old router task runs up to an hour
  after each rollout, a few cents.
- **Migrations run before the release that needs them, and must be backward compatible.**
  The lobby is on Vercel, which deploys `main` on its own, minutes before this release, and
  the engines and lobbies already running keep their code. So an mp migration must work with
  both the previous and the new lobby and engines: add (a column, a function, a new RPC
  version) in one merge, and remove what the old code used only in a later merge, after
  every match on the old code has ended (at most an hour and a half). A migration that is not
  backward compatible must be gated: ship the new code first without depending on it, then
  the migration. `games-mp-migrate` applies the mp migrations only: the files under
  `supabase/migrations` that set `mp_private.schema_revision` (the first inserts revision 1,
  each later one `UPDATE mp_private.schema_revision SET revision = <n> WHERE id = 1`), in
  file-name order, which must be revisions 1..n. It refuses a database newer than the commit
  (a rollback never runs against a newer schema) and a partial state, and the release is not
  promoted when it fails. The site's and Skyhook's migrations there are still applied by hand.

### The trust this adds

- **A push to `main` deploys production.** Whoever can merge to colton-games `main` changes the
  code production engines and the router run, and the mp database schema (as the Supabase
  integration's `postgres` role), with no further review. That is the owner's choice: `main`
  already deploys the production lobby on Vercel.
- **Any branch's code runs in preview engines.** Every writer on the repo, and every
  dependency a build pulls in, can run code in `games-mp-images-preview` (privileged Docker,
  up to 20 minutes, at most 3 at once and 2 new per minute) and in preview engines. Neither
  can reach production: the preview build role pushes only to `games-preview/*` and reads no
  secret; its revisions are registered only in the `games-preview-<game>` families, which only
  the preview launch function (its own role) may run, and the release checks every tag it
  reports against ECR and its own commit. One preview build can push an image for another
  branch's commit that has not been built yet; that image still only ever runs as a preview.
- **Unchanged:** engines have no AWS permission, run in their own subnets with 443-only egress,
  get only their environment's public keys, and carry the token-verifier marker (the release
  registers every revision with it). The router rolls only from `main`.

### Adding a game: the games repo only

Games are dynamic, and nothing in this repo lists them. A commit's own
`scripts/mp-images.mjs` defines its engines. For each engine, the games repo provides:

1. **An image entry in `scripts/mp-images.mjs`.** It is named `games/<game>-engine`, tagged
   `<simVersion>-<sha12>`, and built from its own Dockerfile, as mptest's is. The `--dry-run`
   output must show its `docker build ... -f <Dockerfile>` line and its
   `<name>: <registry>/games/<game>-engine:<tag>` line. Do not change that format; the build
   reads it.
2. **A game id** following the rules below.
3. **Optionally, `mp-engine.json` beside that Dockerfile.** Its only keys are `cpu` and
   `memory`, for example `{"cpu": 2048, "memory": 4096}`. Without the file the engine gets
   2 vCPU / 4 GB.
4. **The engine itself.** It must meet the contract: it listens on 8080, calls back
   `MP_API`, and verifies join tokens with `MP_TOKEN_PUBLIC_KEYS`, as mptest does.

The game id must:

- be 2 to 32 characters of `a-z0-9-`, starting with a letter and not ending with `-`;
- not begin with `preview-`;
- not be `mp-router` or `engines`.

The size must be one Fargate accepts, within this repo's maximums of 4 vCPU (`4096`) and
8 GB (`8192`).

The first build of a commit that lists the engine then does the rest:

- pushes `games/engines:<game>_<simVersion>-<sha12>`, or `games-preview/engines:...` for a
  preview;
- `games-mp-release` registers `games-<game>` or `games-preview-<game>` revisions;
- `games-mp-launch` launches the game once a release has it. Before that it answers
  `unknown-sim-version`.

A build that breaks any of these rules fails before pushing anything. The release checks the
rules again, because a preview build runs untrusted code.

**What a commit cannot change.** Everything in a revision except the image and the size is
fixed here:

- the task and execution roles (no AWS permissions);
- the log group, `awsvpc` networking, arm64 and port 8080;
- the token-verifier marker.

The image can only come from the channel's engine repository, tagged with the game's own
name and the commit's own 12 hex. Production's launch function may run any `games-*`
family except `games-preview-*` and `games-mp-router`, both explicitly denied. The preview
function may run only `games-preview-*` families.

**mptest's images from before games were dynamic** stay in its old repositories,
`games/mptest-engine` and `games-preview/mptest-engine`. Revisions released from them keep
launching (`LEGACY_REPOSITORIES`) and are never expired. New builds push only to
`games/engines`.

**ECR quota.** A channel's games share one repository's images-per-repository quota. That
quota is adjustable in Service Quotas. Production keeps every tagged image: one per game per
main commit.

### Checking a deploy from a dev box

The metal boxes and nextjs-dev can see what the pipeline did with a push, read-only
(`games-multiplayer-observe.tf`): builds and their logs, the functions' logs, ECR tags, task
definitions, the release table, running tasks, the games alarms, and the mp test key for the
live smoke. They cannot start, retry or promote anything, and cannot read any other secret,
a function's or project's environment, or Terraform state.

```bash
export AWS_REGION=us-west-1   # not R="--region ...": zsh (the dev boxes' shell) does not split $R
aws codebuild list-builds-for-project --project-name games-mp-images          # main; -preview, games-mp-migrate
aws codebuild batch-get-builds --ids <id> --query 'builds[].[buildStatus,exportedEnvironmentVariables]'
aws logs filter-log-events --log-group-name /aws/lambda/games-mp-release --start-time <ms>
aws dynamodb get-item --table-name games-mp-releases --key '{"id":{"S":"current#main"}}'
aws dynamodb get-item --table-name games-mp-releases --key '{"id":{"S":"schema#main"}}'
aws ecr describe-images --repository-name games/engines
aws ecs list-task-definition-families --family-prefix games-   # list-task-definitions --family-prefix wants a whole family
aws ecs describe-task-definition --task-definition games-<game>
aws cloudwatch describe-alarms --alarm-name-prefix games-
aws ecs list-tasks --cluster games --desired-status STOPPED
aws ecs describe-tasks --cluster games --tasks <id> --query 'tasks[].[stopCode,stoppedReason,containers[].exitCode]'
```

Name the cluster on every task call: without `--cluster games` the CLI asks about the
`default` cluster, and IAM denies `task/default/<id>` (the grant covers `task/games/*`).
In zsh, split a captured list before passing it on: after `T=$(aws ecs list-tasks ... --output text)`,
`--tasks $T` is one tab-joined argument, which ECS cannot parse and IAM then denies on `*`; use
`--tasks ${=T}`.
ECS forgets a stopped task after about an hour; its output stays in the logs: an engine's in
`/games/engines`, the router's in `/games/mp-router`.

The live smoke reads the key into the environment, never onto a command line:
`export MP_TEST_KEY=$(aws secretsmanager get-secret-value --secret-id games/mp-test-key --query SecretString --output text)`,
then `node scripts/mp-e2e.mjs --remote --base https://cc-games.app --origin https://cc-games.app`.
Production admits 10 launches per IP per hour (`MP_IP_MAX_PER_HOUR`) and the full scenario
list needs about 20, so split a full run over two hours with `--only <scenario,...>`.

## Rotating secrets

- **Join-token keys.** Each entry of `games_mp_token_kids` is one Ed25519 key pair per
  environment; the first entry signs, every entry verifies. Deployments keep the
  `MP_TOKEN_SIGNING_KEYS` they were built with, and every Production and Preview deployment
  shares the one router, so add the new key to the router before any lobby signs with it:
  1. **Add.** Set `games_mp_token_kids = ["kid1", "kid2"]` (new id LAST) and apply. Terraform
     creates `production-kid2` and `preview-kid2`; the router rolls onto a task definition whose
     `MP_TOKEN_PUBLIC_KEYS` lists both generations, and the health step waits for it. Lobbies
     still sign with `kid1`; Vercel now holds both private keys per environment, but only
     new builds see that, and they too sign with `kid1`.
  2. **Switch.** Set `["kid2", "kid1"]` and apply (only the order changes, so no key is
     replaced). Then redeploy Production, and redeploy or retire every Preview deployment still
     in use: from their next build they sign with `kid2`. The router accepts both meanwhile.
     Engines keep the keys they were launched with, so before this step wait until every engine
     launched before step 1 has exited: at most its `hardcap` + 10 minutes (4 h 10 min for the
     longest allowed), or check that `aws ecs list-tasks --cluster games` shows none that
     started earlier (the router's family aside). Otherwise a player of such a match gets
     `401 token` from its engine on reconnect.
  3. **Retire.** Once no deployment built before step 2 serves traffic, wait two minutes (every
     `kid1` token has expired) and set `["kid2"]` and apply. The router and new engines stop
     accepting `kid1` and Terraform destroys both `kid1` pairs.

  Id length is at most 20 characters, because the key id on the wire is `<env>-<kid>`.
  **A leaked private key** (one environment's) cannot wait for that: run
  `terraform apply -replace='tls_private_key.games_mp_token["<env>-<kid>"]'`. The router
  rolls onto the new public key and refuses every new connection the old one signed as soon as
  the health step passes (connections already open keep their old router task while it drains,
  up to an hour; stop those matches' engines to cut them); that environment's deployments answer `401 token` from the router until
  they are rebuilt, and its engines already running keep only the old key, so their players
  cannot reconnect (stop them, see Emergency switches). The other environment is untouched:
  its keys are separate.
- **Any other generated secret.** Run `terraform apply -replace=random_password.<name>`,
  then redeploy the site. Replacing `games_mp_cookie_secret` resets every guest identity.
- **Preview Supabase values.** If the integration rotates its keys, bump
  `games_mp_preview_supabase_sync` and apply.

### Cutover from the shared HMAC key to Ed25519 (once)

Before this, the lobby and the router shared one HMAC key (`MP_TOKEN_KEYS`, Secrets Manager
`games/mp-token-keys`), so the router could mint. There is deliberately no dual-accepting
router: accepting both schemes would keep the HMAC key on the router, the exact capability
being removed. Instead both sides switch in one apply, and every mismatch fails closed (a
`401 token` from the router or `503 multiplayer-not-configured` from the lobby), never open.
Tokens live two minutes and Production does not serve multiplayer yet.

1. **Games repo.** Push the Ed25519 change (colton-games `mp-asymmetric-tokens`, `1eb83917`) and
   stack it under anything that ships multiplayer, so no build that reads `MP_TOKEN_KEYS` ever
   reaches Production. Do not deploy it to Production. A preview built from it before step 2
   answers `503 multiplayer-not-configured` (no `MP_TOKEN_SIGNING_KEYS` yet): expected.
2. **Pin** (historical: before automatic deploys). `games_mp_source_ref` had to be a commit with the Ed25519 router and the
   token-verifying engine kit (first applied at `1eb83917`; now pinned to `dae003a9`, the
   squash of colton-games #57 on main, whose image inputs are identical). An older router
   image with this task definition refuses to boot (no `MP_TOKEN_KEYS`), so the health step
   would fail the apply rather than serve.
3. **Plan and apply** from a fresh worktree. The plan must show: `random_bytes.games_mp_token_key`
   destroyed; `tls_private_key.games_mp_token["production-kid1"]` and `["preview-kid1"]` created;
   the Vercel `MP_TOKEN_KEYS` variable destroyed and `MP_TOKEN_SIGNING_KEYS` created once per
   environment (targets `["production"]` and `["preview"]`, sensitive); the router task definition
   replaced with `MP_TOKEN_PUBLIC_KEYS` in `environment` and no `secrets`; the router execution
   role's inline policy `pull-log-and-token-key` updated in place to drop
   `secretsmanager:GetSecretValue` (the name is kept so it is never replaced); and
   `games/mp-token-keys`, its version and its policy destroyed; both launch functions,
   `games-mp-launch-production` and `games-mp-launch-preview`, updated in place (new code, and
   `TOKEN_PUBLIC_KEYS`: each its own environment's public keys). New router and engine images
   for the new commit; the new engine revisions carry `MP_TOKEN_VERIFIER=ed25519-v2`, and the
   pre-cutover ones stay ACTIVE but are never launched again (a client still on an older
   simVersion gets `unknown-sim-version` until it updates; `mptest` keeps `mptest-1`, so
   nothing is stranded). Nothing else. In the apply, the router rolls with no downtime (new tasks
   healthy before old ones drain) and the health step waits for the new task definition. The
   launch functions depend on that health step, so they start launching new engines (which accept only the
   `X-MP-Token` a new router forwards) only once every router task forwards it. Engines already
   running from the old image keep trusting the `X-MP-*` hints, which the new router still
   sends, until their matches end.
   During those minutes a request may reach an old task (HMAC only) or a new one (Ed25519
   only): neither accepts a token it did not before, and a token that meets the other kind is
   refused. Running old tasks keep their already-injected key, so deleting the secret
   (7-day recovery window, administrators only) does not disturb them.
4. **Redeploy** only after the apply (and its health step) succeeded: rebuild the previews that
   should keep working from the new code; Production gets the change when the games PRs merge.
   Vercel applies env only at build, so only builds after step 3 have `MP_TOKEN_SIGNING_KEYS`.
   Previews built before step 3 still hold the old HMAC key, which nothing accepts any more.
5. **Check.** The router's `listening` log line lists `keys` as `production:production-kid1`,
   `preview:preview-kid1`, and a new engine's `boot` line lists only its own environment's key;
   a match started on a rebuilt preview connects; a token from a
   pre-cutover preview gets `401 token`; `aws secretsmanager describe-secret --secret-id
   games/mp-token-keys` shows a `DeletedDate`; and
   `aws iam simulate-principal-policy --policy-source-arn <games-mp-router-execution role arn>
   --action-names secretsmanager:GetSecretValue` answers `implicitDeny`.

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

- **`games_mp_autodeploy = false`** stops the poller: nothing new is built or released, and
  production keeps its current release. To go back to a known-good release at once, re-promote
  it (Automatic deploys, "Rolling production back").

## What still needs a human

- **Merging the games PRs to production**, which is EJ's "push to prod". A merge to `main`
  then deploys everything by itself: Vercel the lobby, and `games-mp-release` the engines, the
  router and the mp migrations.
- **Fixing whatever a failed preflight names**, for example storing a new Vercel token. The
  plan fails with the exact fix, and nothing has changed.

## The launch functions: how an engine starts

There is one launch function per environment, `games-mp-launch-production` and
`games-mp-launch-preview`, deployed from the same source (`games-multiplayer.tf`, source
`games-multiplayer/launch.py`). The lobby's roles can do one thing: invoke their own
environment's function. They have no ECS, IAM or EC2 permission at all.

- **Request.** `{"action":"start","matchId","game","simVersion","secret","hardCapSec","apiBase"}`,
  plus `"apiBypass"` and `"commit"` from a preview. Any other field (`overrides`, `tags`,
  `taskDefinition`, `env`, a size, a role) is refused, not dropped. `matchId` is a lowercase UUID,
  `game` must be a valid game id (Adding a game), `simVersion` is the match's (the lobby's shape, 1 to 64 of
  `A-Za-z0-9._-`, matched in full), `commit` is 40 lowercase hex (required on preview, ignored
  on production), `hardCapSec` is 60 to 3,600 (no match may outlive a draining router, see
  Automatic deploys), `apiBase` must be
  `https://cc-games.org` (or `.net`/`.app`, for a lobby built before a switch) on production or
  this project's own `*.vercel.app` deployment URL on
  preview, and only preview may pass a bypass secret. `{"action":"stop","matchId"}` stops that
  match's engine of the caller's own environment, never another environment's or the router.
- **Environment from the function.** Each function's environment is its own configuration
  (`LAUNCH_ENV`, with its ceiling, allowed `apiBase` and whether a bypass may be passed), set by
  Terraform. That, not the request or the invoked ARN, decides the engine's `MP_ENV`, its `env`
  tag, the allowed `apiBase`, the bypass and the ceiling. Each launcher role's policy names only
  its own environment's function ARN. A function deployed without a usable environment refuses
  every call (`forbidden`). The functions publish no versions or aliases.
- **Which revision.** From `games-mp-release`'s table (`RELEASES_TABLE`), read on every call:
  production takes `current#main` for the game's current simVersion and `sim#<game>#<simVersion>`
  for an older one; preview takes `build#preview#<commit>` (`engine-building` until it is
  released, `engine-build-failed` if its build failed). Whatever the table says, the revision
  must be of the function's own family (`ENGINE_FAMILY_PREFIX` + game: `games-<game>` on
  production, `games-preview-<game>` on preview) in this account and region, its only container
  `engine` running exactly `<the environment's engine repository>:<game>_<simVersion>-<12 hex>`
  (or, for mptest's pre-dynamic revisions, `<its old repository>:<simVersion>-<12 hex>`; a
  preview's: its commit's 12 hex), with `MP_TOKEN_VERIFIER=ed25519-v2` in its environment (read
  once per revision with `DescribeTaskDefinition`, then cached). Otherwise
  `{"ok":false,"error":"unknown-sim-version"}` and nothing is launched; a game no release has
  gets the same answer. Revisions registered
  before engines verified tokens have no marker and are never launched.
- **What it runs.** That revision (IAM allows each function's own role `RunTask` on its own
  family prefix only, `games-*:*` or `games-preview-*:*`, as games are dynamic, only on
  cluster `games`; production's role also denies `games-preview-*`, and both deny the router's
  family;
  `ecs:DescribeTaskDefinition` is read-only and takes no resource, so it is on `*`; the table
  reads are `GetItem` on its own items only), `launchType FARGATE`, the engine
  subnets and security group with `assignPublicIp=ENABLED`, `clientToken` = `<env>-<match id>`
  and `startedBy` = the match id. Container `engine` gets `MATCH_ID`, `MATCH_SECRET`, `MP_API`, `GAME_ID`, `MP_ENV`,
  `MP_TOKEN_PUBLIC_KEYS` (the function's own environment's Ed25519 public keys, its
  `TOKEN_PUBLIC_KEYS` setting, checked to be exactly that before any launch: a private key, a raw
  HMAC key or another environment's key makes the launch fail with nothing started),
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
  stop, or `{"ok":false,"error"}` with `capacity`, `unknown-sim-version`, `engine-building`,
  `engine-build-failed`, `not-yet-visible`, `bad-request` (and the `field`) or `forbidden`. An ECS failure raises (a Lambda error, in its
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
`MP_ENVS` (`production,preview`) AND is the environment of the Ed25519 key that signed it:
every entry of `MP_TOKEN_PUBLIC_KEYS` is bound to one environment, and each Vercel
environment holds only its own private keys. So this is a security boundary: a Preview
build, which every writer on the games repo controls, cannot mint a token the router
honours as Production. Development holds no key at all. Preview origins
(`https://colton-games-<hash>-coltons-projects-7f9a4e8b.vercel.app` and the
`colton-games-git-<branch>-...` aliases) match the router's `MP_ALLOWED_ORIGINS` entry
`https://colton-games-*-coltons-projects-7f9a4e8b.vercel.app`, where `*` is one DNS label.

**Production domain.** `cc-games.org` is becoming canonical (a school network blocks `.app`);
`cc-games.net` serves too until it redirects, and every other name 308-redirects. Each serving
name has its own multiplayer entry (`play.cc-games.org`, `play.cc-games.net`;
`local.mp_play_extra`): a certificate each on the same ALB and the same router, which accepts
those origins. Production's `MP_PUBLIC_ENTRY` lists `wss://play.cc-games.org`,
`wss://play.cc-games.net` and `wss://play.cc-games.app`, and the lobby
(CoderColton/colton-games#83) gives each player the entry on its page's domain, so no player
touches `.app`. Engines call back `MP_API=https://cc-games.org`. Previews set no entry and use the lobby's default,
`play.cc-games.app`. Do not redirect a domain that `MP_API` names or that running engines were
launched with: `fetch` drops the engine's `Authorization` header on a cross-origin redirect,
its heartbeats then fail, and the lobby fails its match.

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
3. **Join token.** Ed25519 (`v2.<kid>.<payload>.<signature>`), bound to one match, one
   environment and one seat, and valid for two minutes. Only the Vercel lobby can mint one:
   each environment holds its own private keys (`MP_TOKEN_SIGNING_KEYS`, sensitive, per
   target). The router holds public keys only (`MP_TOKEN_PUBLIC_KEYS`, a plain task-definition
   variable), each bound to its environment, and refuses to boot if a signing key is ever in
   its environment; its execution role reads no secret at all. It accepts `v2` only, so an
   HMAC or other-algorithm token is refused before a key is chosen. Nothing on AWS holds a
   private key; administrators can read them from Terraform state.
4. **Router → engine.** The router runs in its own subnets (`10.0.66.0/24`, `10.0.67.0/24`) and
   forwards only to addresses inside `MP_TARGET_CIDRS` (the two engine subnets,
   `10.0.64.0/24` and `10.0.65.0/24`); its security group's egress reaches only the engine
   group on 8080 plus HTTPS. The engine group admits only the router. The router forwards the
   verified token in `X-MP-Token`, and the engine verifies it again (games repo
   `server/mp-kit/`) with `MP_TOKEN_PUBLIC_KEYS`, its own environment's public keys only, given
   by its environment's `games-mp-launch-<environment>`, which refuses to launch with anything
   but that environment's Ed25519 public keys: signature, key environment, match = its
   `MATCH_ID`, environment = its `MP_ENV`, expiry, and a seat and player of its spec. The engine
   takes the seat from the token alone; the router's `X-MP-Match/Player/Seat` headers are
   ignored. A WebSocket's token is checked when it opens, every HTTP request's each time, so a
   reconnect needs a fresh token. An engine refuses to start without keys or with any signing
   key.
5. **Lobby → AWS.** Vercel functions exchange a Vercel OIDC token for a launcher role; no AWS
   key is stored. Production assumes `games-mp-launcher`, Preview `games-mp-launcher-preview`;
   each trust is exactly project `colton-games` in team `coltons-projects-7f9a4e8b` and that one
   environment. Each role's only permission is `lambda:InvokeFunction` on its own environment's
   launch function, `games-mp-launch-<environment>`: no ECS, no PassRole, no EC2, no secrets.
6. **Launch functions → ECS.** `games-mp-launch-<environment>` (above) validates the request,
   counts its environment's engines and refuses at its share of the ceiling, then runs a revision of the game's own engine family whose image
   is in the environment's engine repository under the game's own name with the match's
   simVersion, in cluster `games` only (router and, from production, preview families
   **explicitly denied**). It can pass only the two
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
| Code in any Preview build (every writer on `CoderColton/colton-games`, and every dependency such a build pulls in) | Invoke `games-mp-launch-preview`: start up to 8 preview engines of its own branch's engine image (the preview share), each for up to its `hardCapSec` (at most 1 h) plus 10 minutes, calling back only a `colton-games-*` preview URL; stop preview engines; keep the preview function's own slot busy, which slows only preview launches; read the lobby's Supabase data. In `games-mp-images-preview`: run as root in a privileged build for up to 20 minutes (3 at once) and push `games-preview/*` images | Run a command, role or size of its choosing in production; push or register anything production runs (no push to `games/*`, no production family); launch production engines or more than 8 of its own; take production's share or its launch slot; stop production engines or the router; reach ECS, EC2, IAM or secrets directly |
| A merge to colton-games `main` | Deploy production engines, the router and mp migrations within minutes, with no further review (by design: `main` is production) | Change anything Terraform owns: roles, network, ceilings, keys, the functions |
| A compromised production deployment | The same through `games-mp-launch-production`, up to production's share of 22 engines | Everything in the row above, with production and preview swapped: it cannot launch preview engines or take preview's 8 |
| A compromised router task (it parses internet input) | While the compromise lasts, only for connections that pass through it: see their tokens and bytes, and drop or rewrite that traffic (TLS ends at the ALB; router to engine is plain HTTP inside the VPC); replay a token it saw, for that same match and seat, until it expires (two minutes) | Mint a join token: it holds only public keys (it refuses to boot with a signing key), its execution role reads no secret, its task role has no policies. So nothing it can read or leak (env, logs, a memory disclosure) lets anyone mint tokens later, through another router task or from anywhere else, and a Preview-side key cannot pass for Production. Claim a seat it holds no fresh token for: engines verify the token themselves and ignore the router's identity headers. Launch or stop tasks; connect anywhere but the engines on 8080 and HTTPS on 443 (its security group's only egress), so not the admin fleet's SSH or ET either |
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

**Closed** (2026-09-27): join tokens are Ed25519 (ejc3/aws#173): the router, which holds the
verifying keys, could mint a valid HMAC token for any match and seat; it now holds public keys
only and cannot, and each lobby environment has its own key pair, so a Preview build cannot
mint a Production token. Engines verify the join token themselves (games repo mp-kit, keys
from each environment's `games-mp-launch-<environment>`) instead of trusting the router's identity headers, so a compromised
router can no longer claim any seat of any running match. The AWS-side engine ceiling (detective: a sweep every minute) and its alarm, the ECS budget, the WAF,
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
3. Billing alerts are off account-wide, so no `EstimatedCharges` alarm can fire. Turning them on
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
| Secrets Manager, 2 secrets at $0.40 (`games/mp-test-key`, `games/mp-cron-secret`) | $0.80 |
| ECR storage, 14-day logs, sweeper Lambda (now every minute) and Scheduler, the two launch Lambdas (one call per match start or stop; reserved concurrency costs nothing), the build bucket | under $2 |
| **Always on** | **about $64** |
| Per build: about 5 minutes of CodeBuild, one per commit on `main` or any other branch | about $0.02 |
| After each router rollout: the old router tasks drain for up to an hour | about $0.02 |
| Poller (every minute), release function, releases table | under $1 |
| Per match: 2 vCPU / 4 GB plus a public IPv4, about $0.096 per hour | **about $0.016 per 10-minute match** |

Internet data out beyond the account's free 100 GB per month costs about $0.09/GB.
