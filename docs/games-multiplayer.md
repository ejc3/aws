# Games multiplayer: bring-up and costs

`games-multiplayer.tf` holds the AWS and Cloudflare side of the Colton Games multiplayer
platform: one Fargate task per match, entered through `wss://play.cc-games.app/m/<id>`.
The design and the binding names are in the games repo (`CoderColton/colton-games`):
`docs/MULTIPLAYER.md` and `docs/MULTIPLAYER-CONTRACT.md`.

## Apply order

Everything runs from the jumpbox. Read each plan before applying it.

1. **Apply** with no image tags set.

   ```bash
   cd ~/aws && git pull --ff-only && terraform plan && terraform apply
   ```

   This creates ECR, the `games` cluster, IAM (including the Vercel OIDC provider and
   `games-mp-launcher`), the security groups, the `games-play` ALB, the ACM certificate and
   its Cloudflare validation record, the `play` / `*.play` CNAMEs, the token-key secret
   container and the sweeper. The certificate validation waits for ACM, usually a few
   minutes. No router or engine task definition exists yet, so the ALB answers 503.

2. **Set the token key.** It goes through stdin, never argv:

   ```bash
   printf 'kid1:%s' "$(openssl rand -base64 32)" | aws secretsmanager put-secret-value \
     --region us-west-1 --secret-id games/mp-token-keys --secret-string file:///dev/stdin
   ```

   The format is the contract's `MP_TOKEN_KEYS`: `kid1:<base64 32+ bytes>[,kid2:<...>]`.
   The first key signs and every listed key verifies. The same string goes into Vercel in
   step 5.

3. **Build and push the images** with the games repo's image build script: `games/mp-router`
   and `games/mptest-engine`, both `linux/arm64`. The engine tag is mptest's `simVersion`.
   Tags are immutable, so the script must treat "tag already exists" as done. Get the
   repository URLs from `terraform output games_mp_ecr_repositories`.

   Today only the administrator role can push to these repositories. No pbox or dev-box
   role has ECR access, so push from the jumpbox or add a scoped push role first.

4. **Set the image tags and apply again.** Change the variable defaults in
   `games-multiplayer.tf` and merge that change, rather than using the ignored
   `terraform.tfvars`, so git keeps describing what is live. The tags are not secret.

   ```hcl
   variable "mp_router_image_tag"  { default = "<router tag>" }
   variable "mp_engine_image_tags" { default = { mptest = "<simVersion>" } }
   ```

   This apply creates the `games-mp-router` and `games-mptest` task definitions and the
   `mp-router` service. Check that the target group turns healthy and that
   `curl https://play.cc-games.app/healthz` returns `ok`.

   After that, ship a new router by changing `mp_router_image_tag`. ECS starts the new
   task before it drains the old one, and rolls back on its own if the new task never
   passes its health check. A new engine `simVersion` is a new `mp_engine_image_tags` value.
   The old task definition revision stays ACTIVE, so matches on the old version can still
   launch while clients update.

5. **Put the outputs into the colton-games Vercel project env** (production, preview and
   development):

   ```bash
   terraform output -json games_mp_vercel_env
   ```

   | Vercel env var | Value |
   | --- | --- |
   | `MP_ROLE_ARN` | the launcher role. The contract's launcher reads `AWS_ROLE_ARN`, Vercel's convention, so set that to the same ARN until the two names are aligned |
   | `MP_CLUSTER`, `MP_SUBNETS`, `MP_ENGINE_SG`, `MP_REGION`, `MP_LAUNCHER`, `MP_PUBLIC_ENTRY` | as output |
   | `AWS_REGION` | `us-west-1`. Vercel otherwise sets it to the function's own region |
   | `MP_TOKEN_KEYS` | the string from step 2, marked sensitive, in **Production and Preview only** (see below) |

   The project needs **Secure backend access with OIDC federation** on, in **Team** issuer
   mode. The launcher must call `awsCredentialsProvider({ roleArn })` **without** an
   `audience`: the role trusts only the default audience `https://vercel.com/coltons-projects-7f9a4e8b`.

After a token-key change, restart the router so it reads the new value:
`aws ecs update-service --cluster games --service mp-router --force-new-deployment`.

## What the lobby must do on RunTask

- Use task definition `games-<game>`, cluster `games`, subnets `MP_SUBNETS`, security group
  `MP_ENGINE_SG`, and `assignPublicIp=ENABLED`.
- Set the tags `game`, `match`, `env` and `hardcap`. `hardcap` is the match's hard cap in
  seconds.
- The sweeper runs every 5 minutes. It stops any standalone task in the cluster older than
  `hardcap` + 10 minutes. If `hardcap` is missing or invalid, the limit is 2 hours, and
  `hardcap` is clamped to 4 hours.
- The launcher can tag only at RunTask, so it can't extend a running task's cap later.
- The launcher's StopTask works only on tasks that carry the `match` tag, so the lobby can
  stop its own engines. It can never stop a router task: those are started by the ECS
  service, carry no `match` tag, and carry `games-role=router`, which an explicit Deny
  blocks.
- Tagging tasks at launch needs the account's long task-ARN format. It is on by default
  for current accounts. If RunTask with tags ever fails, check it (read-only) with
  `aws ecs list-account-settings --effective-settings --name taskLongArnFormat --region us-west-1`.

## Environments and the router

One router serves every lobby environment. The lobby puts its env in each join token's
`n`. The router accepts a token whose `n` is any value in `MP_ENVS`, which is
`var.mp_router_envs` comma-joined, `production,preview` by default. `MP_ENV` is still set,
to the primary env.

This check is a correctness guard, not a security boundary: any lobby that holds
`MP_TOKEN_KEYS` can mint a token with any `n`. The real boundary is which Vercel
environments hold the key:

- **Production and Preview hold it**, and the router accepts both.
- **Development does not.** `vercel env pull` would otherwise write the signing key into a
  `.env.local` on a laptop. Local development uses `MP_LAUNCHER=local` with its own
  throwaway key, and never needs this router.

The launcher role still trusts Development, so a laptop can start an ECS engine. Nobody
could join that engine through the router, and the sweeper stops it at its cap. To let
Development use the router after all, add `development` to `mp_router_envs` and give
Development the key.

## Monthly cost (us-west-1 list prices, checked 2026-09-26)

Fargate ARM in us-west-1 costs $0.03725 per vCPU-hour and $0.00409 per GB-hour. An ALB costs
$0.0252 per hour plus $0.008 per LCU-hour. A public IPv4 address costs $0.005 per hour.

| Item | Monthly |
| --- | --- |
| ALB `games-play`: hours, about $18.40, plus its two public IPv4 addresses, $7.30 | about $26 |
| ALB LCUs at family scale | about $1–3 |
| `mp-router`, 0.25 vCPU / 0.5 GB: compute about $8.30, plus public IPv4 $3.65 | about $12 |
| Secrets Manager ($0.40), ECR storage, 14-day logs, sweeper Lambda and Scheduler (inside the free tier) | under $2 |
| **Always on** | **about $40** |
| Per match: 2 vCPU / 4 GB plus a public IPv4, about $0.096 per hour | **about $0.016 per 10-minute match** |

Internet data out beyond the account's free 100 GB per month costs about $0.09/GB.

This is about $13 more than the design doc's $27 for three reasons:

- us-west-1 costs more than us-west-2.
- The design doc left out the ALB's and the router's public IPv4 charges.
- A NAT gateway would cost more still: about $33 per month plus data.
