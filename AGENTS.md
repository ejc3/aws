# AWS infrastructure agent guide

This file provides guidance to automation agents working with this repository. Read
`README.md` first for the operator-facing system map and first-session runbook.

## Nested Virtualization (NV2) Kernel

**CRITICAL**: the metal boxes run a custom kernel for nested virtualization. Both the
host kernel and the VM (guest) kernel come from the SAME fcvm profile and the SAME patch
set -- there is no separate host build path, and no `kernel/build.sh` (that script is
gone; earlier revisions of this file documented it).

The version is pinned in ONE place, `fcvm/rootfs-config.toml`, across six
`kernel_version` entries: `nested` arm64/amd64, each of their `host_kernel`
sub-profiles, and `btrfs` arm64/amd64. Bumping a kernel means editing those together and
re-verifying the patches, not editing a build script.

Patches live in `fcvm/kernel/patches-arm64/` and `patches-x86/`. The FUSE ones are
symlinks into `fcvm/kernel/patches/`, so a fix there lands on both architectures at once.
`*.vm.patch` is applied to the VM kernel only -- the `host_kernel` `build_inputs` glob
deliberately excludes it, so the host takes a strict subset.

**Rebuild and install the host kernel** (fcvm drives the whole thing -- it downloads the
kernel.org tarball for the pinned version, applies the patch set, builds, installs to
`/boot`, and runs `update-grub`):

```bash
cd /home/ubuntu/fcvm
sudo ./fcvm setup --kernel-profile nested --install-host-kernel
sudo reboot
```

**Current kernel on instance:** `uname -r` reports `<version>-fcvm-<build-sha>`, e.g.
`7.0.14-fcvm-<sha>`. The `-nested-dsb` suffix in older notes was never what the build
produced. GRUB keeps previously installed kernels, so a bad build can be backed out by
selecting the prior entry rather than rebuilding from scratch.

**Rebasing the kernel is periodically necessary, not optional.** The 6.18.3 pin sat
seven months and three upstream releases stale until 2026-08-08, when the ARM box hung
on `kernel BUG at arch/arm64/kvm/nested.c:754` in the NV2 path. Upstream churns
`arch/arm64/kvm/nested.c` heavily (21 commits between v6.18 and v7.1-rc7), and no
upstream bug report is actionable against a stale tree carrying local KVM/NV patches.

## Project Philosophy

**KISS - Keep It Simple, Stupid**

This project is opinionated and minimal:
- One supported control-plane workflow
- Direct Terraform from an administration jumpbox
- Agent-readable context and generated machine-local session seeds
- Automatic convergence with sensible defaults

## Key Principles

1. **Terraform is the source of truth**: Run Terraform directly; there is no Make or
   container wrapper.
2. **Agent-first operation**: Orient from `README.md`, this file, the current Terraform,
   and live read-only state. Never rely on remembered instance IDs or stale plans.
3. **Sensible defaults**: Keep the deployed path opinionated and minimize operator choices.
4. **One user entry point**: Keep `README.md` complete enough for a new operator; put deep
   runner internals in `GITHUB-RUNNERS.md` and agent-only safety constraints here.
5. **MANAGED AWS CHANGES VIA TERRAFORM**: Never use the AWS CLI for ordinary
   create/modify/delete operations. Read-only `describe`/`list`/`get` commands are fine.
   The scoped `DevEBS=true` working-volume policy, automation Lambdas, and documented
   break-glass recovery are deliberate exceptions; do not expand them casually.
6. **THE BROWSER IS THE ABSOLUTE LAST RESORT**: If a thing can be done by API or CLI, do it
   that way. Do not hand the operator a dashboard click-path until you have checked that no
   API exists AND no available credential can reach it, and can say which call failed and
   how. "You'll need to do this in the dashboard" is a claim that requires evidence like any
   other -- see the Cloudflare Registrar note below for what happens when it is guessed.

## Cloudflare: domains, tokens, and what is actually true

Registered through **Cloudflare Registrar**: `cc-games.dev` (2026-07-25 -> 2027-07-25,
registrar of record "CloudFlare, Inc."). The account therefore already has a billing profile
with a default payment method, a registrant contact, and an accepted Domain Registration
Agreement. Do not tell the operator to go set those up; they exist.

**Domains CAN be registered by API.** Cloudflare shipped a Registrar API (beta) with a real
registration workflow -- Search -> Check -> Register:

```
POST https://api.cloudflare.com/client/v4/accounts/$ACCOUNT_ID/registrar/registrations
```

Call `Check` immediately before `Register`: the docs are explicit that Search is not the
source of truth, and a successful registration is **billable and non-refundable**. Confirm
the price from `Check` with the operator before committing.

This was previously asserted here, and to the operator, as "dashboard only, no purchase
API". That was stale knowledge stated as fact, and it wasted a round trip. If a capability
question turns on a vendor's current API surface, search the docs before answering.

**Tokens.** `cloudflare-api-token` in Secrets Manager (us-west-1) is a SCOPED ACCOUNT token,
53 chars. It can read zones and drive DNS/Zero Trust/tunnels -- which is all the Terraform
here needs -- and it CANNOT:

| call | result | meaning |
|---|---|---|
| `GET /accounts/$ID/registrar/*` | 403 `10000` | no Registrar permission; route exists |
| `GET /user/tokens` | 403 `9109` | account token, not user-level: cannot mint tokens |

**Minting tokens.** `cloudflare-account-token` (us-west-1) is an ACCOUNT token (`cfat_`)
with Account API Tokens Read+Write. It mints scoped tokens on demand -- that is how
`dolphin-labs.dev` was bought without a browser:

```bash
# Send the token on curl's stdin, never on its command line: argv is readable by every local
# user through /proc/<pid>/cmdline, and this token can mint other tokens.
{ set +x; } 2>/dev/null
M=$(aws secretsmanager get-secret-value --secret-id cloudflare-account-token \
      --region us-west-1 --query SecretString --output text)
# Registrar Domains Admin = 136d0be1ddc64eaf8516fa6994abfad4
printf 'Authorization: Bearer %s\n' "$M" | curl -4 -X POST -H @- -H 'Content-Type: application/json' \
  -d '{"name":"registrar-agent","policies":[{"effect":"allow",
       "resources":{"com.cloudflare.api.account.<ACCOUNT_ID>":"*"},
       "permission_groups":[{"id":"136d0be1ddc64eaf8516fa6994abfad4"}]}]}' \
  https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/tokens
unset M
```

Three things that cost time and are not guessable:

- **`curl -4` is required.** The token is IP-pinned to the jumpboxes' IPv4 addresses, and this
  host egresses IPv6 by default -- you get `9109 Cannot use the access token from location:`
  with an IPv6 address, which reads like a permissions error and is not.
- **Account tokens use account endpoints.** `POST /accounts/{id}/tokens`, and verify is
  `/accounts/{id}/tokens/verify`; `/user/tokens*` returns 1000 Invalid API Token for them.
- **Registrar paths are `domain-search`, `domain-check`, `registrations`** -- not `/check`.
  `privacy_mode` is a string (`"redaction"` / `"off"`), not a boolean. A first
  `domain-check` may return `1011 Unable to check...`; it is transient, but do not blind-retry
  `registrations` the same way -- that one spends money.

Revoke minted tokens when the task is done; re-minting is one command. The old user-scoped
`cloudflare-bootstrap-token` was revoked and deleted -- it could not attach account
permissions, which is exactly why it was useless for this.

Account id: `12ea67fb7ced068de03f35c22688e436`.

**Domains registered here so far** (all Cloudflare Registrar, auto-renew off unless stated): `cc-games.dev`, `cc-games.app`,
`cc-games.net`, `cc-games.org`, `ccgames.app`, `dolphin-labs.dev`, and `yourfantasymovie.com` (2026-10-05, $10.46, the Fantasy
Films site; DNS in `yourfantasymovie.tf`). Registration via API needs a short-lived Registrar Domains Admin token, a `domain-check`
immediately before, exactly one `registrations` call, and revoking the token after.

**September 8, 2026 check:** `/home/ubuntu/aws/.env` was absent, and Secrets Manager
`cloudflare-account-token` had an `AWSCURRENT` version. Keep this account-level token-minting
credential in Secrets Manager only; do not recreate a plaintext `.env` copy. This checked
only that file path and secret metadata, not token usability or historical/other copies.

## Preventing Terraform Drift

**Fixed drift sources:**
- Removed CloudWatch alarms that referenced instance IDs (caused drift on spot instance recreation)
- Backup plans now terraform-managed (were manually created)
- Auto-stop uses Lambda instead of CloudWatch EC2 actions (works with spot instances)

**Rules to prevent drift:**
- Make managed changes in `.tf` files, not with mutating AWS CLI commands
- Run `terraform plan` before `terraform apply` to catch issues
- Keep all infrastructure changes in git
- Avoid resources that reference instance IDs directly (they change on spot recreation)

## Project Overview

AWS infrastructure for a personal development fleet: two administration jumpboxes, two
Firecracker metal servers, a Next.js box behind Cloudflare Access, ephemeral shared I/O
and burst compute, autoscaled GitHub runners, Workers Builds-backed previews, and
recovery/monitoring infrastructure.

- Terraform infrastructure-as-code, versioned S3 backend (`ejc3-terraform-state`) +
  DynamoDB locks
- Roughly 195 managed resources across `us-west-1`, `us-west-2`, `us-east-1`, the isolated
  staging account, and Cloudflare
- No application database or Aurora. DynamoDB is used for Terraform locking and the runner
  registration handshake

## User Workflow

**Run Terraform directly on a jumpbox.**

```bash
cd ~/aws
terraform plan
terraform apply
```

The jumpboxes have Terraform, the AWS CLI, Git, and administrator instance roles. There is
no AWS login or credential-refresh step on them. The obsolete Mac/container/Make workflow
has been removed.

Commit AND push every change: the live infrastructure, the terraform files and git must
not drift apart.

## Technical Details

### How this repo is driven

Terraform runs directly on the jumpbox, against the S3 backend with DynamoDB locking. The
instance role supplies credentials, so there is no login step and nothing to auto-refresh.
Keep `.terraform.lock.hcl` tracked so both admin boxes resolve the same providers.
`main.tf` enforces Terraform 1.10.3 because the Workers Builds control token uses an
ephemeral resource. Bump that constraint, both jumpboxes, and the validation workflow together.
When the exact Cloudflare fork pin advances, use targeted `terraform providers lock` for
`registry.terraform.io/ejc3/cloudflare`, then `terraform init -lockfile=readonly`; never use
broad `terraform init -upgrade`, which can advance unrelated `~>` providers.

### Cost notes

- The dev boxes are the cost. Two metal spot instances dominate; the small boxes are noise
- `dev-auto-stop-lambda.tf` applies intended 12h idle policies to the metal boxes and I/O
  box. `parallel-box-watchdog.tf` terminates burst compute and the four GPU test boxes
  (`gpu-box.tf`, slots `gpu-box` and `gpu-box-2`..`-4`) after 30m CPU idle, and each GPU box
  at 4h from launch (its own shutdown timer is only a backup)
- `nextjs-dev` and both jumpboxes are deliberately excluded from idle stop
- There is no application database. DynamoDB is limited to Terraform locking and runner
  registration claims. Older notes about Aurora Serverless auto-pause no longer apply

### Ohio (us-east-2) is being prepared, not yet used

The 2026-10-03 cost review found us-west-1 is the most expensive US region (on-demand and EBS 17-20% over the
others) and that the spot metal we run is 15-65% cheaper in Ohio with no worse placement scores. `ohio.tf` builds the
runner side next to what runs today, switched on NOTHING: an isolated VPC (10.11.0.0/16, IPv6, three AZs), the two
runner security groups, VPC flow logs to the audit bucket, and `ami-replicator`, which copies the newest two runner
AMIs of each architecture to Ohio every hour (the launcher picks its AMI by tag in its own region, and fcvm publishes
only in us-west-1). Nothing there bills while idle: no Elastic IP, NAT, instance or endpoint (a test enforces it). The
cutover is a separate act the owner schedules. The fcvm metal runners are region-aware: `local.runner_regions` in
`runner-vpc.tf` (primary first; now `["us-east-2"]`: the move is finished and us-west-1 holds no runners; `["us-east-2", "us-west-1"]` was the draining step) drives the launcher, reuse and cleanup Lambdas, the IAM ARNs and the
subnet and security-group choice. A move is two applies: `["us-east-2", "us-west-1"]` (new runners in Ohio, old ones still
counted, reused and reaped while they drain), then `["us-east-2"]`. The control plane (SSM, DynamoDB, the Lambdas) stays in
us-west-1; an instance uses `CONTROL_REGION` for those and its own region for EC2. The app runners (runner-app.tf) have not
moved. **The parallel boxes** live in Ohio (`ohio-pbox.tf`): their own VPC, security group, key pair, launch templates and
idle watchdog, and a peering route to the I/O box's one subnet for the NFS scratch. They moved from us-west-2 on 2026-10-05
from a snapshot copy of each work volume (no fresh snapshot, as the owner accepted), and the us-west-2 site was retired:
the old volumes, the move snapshots, the launch templates and the security group are deleted, so the Ohio volumes are the
only copy (`prevent_destroy`). `scripts/parallel-box.sh` reads the region from `/infra/parallel-box`, published from
`local.parallel_box_region`. The I/O box ignores `user_data`, so an SSM association (`aws_ssm_association.io_box_exports`)
keeps its `/etc/exports.d/io-box.exports` equal to the security group's client list whenever it is up. Known region
pins to lift when each move is wired up:
`runner-bootstrap.tf` (RequestedRegion us-west-1) and `dev-ebs.tf` (west-1/west-2 only).

### File Structure

```
.
├── main.tf                         # Providers, protected backend, and primary network
├── variables.tf                    # Opinionated variables and defaults
├── firecracker-dev.tf/x86-dev.tf   # Persistent Spot metal boxes
├── nextjs-dev.tf/cloudflare.tf      # Kids' environment, Access, and Workers Builds
├── io-box.tf/parallel-box.tf        # Ephemeral I/O and burst compute
├── runner-autoscale.tf              # Disposable GitHub runners
├── .terraform.lock.hcl              # Shared provider selections
├── README.md                        # Complete operator entry point
└── AGENTS.md                        # Agent constraints and recovery detail
```

## When Working on This Project

### Do:
- **Run terraform directly on the jumpbox** - `terraform plan`, `terraform apply`, `terraform fmt`
- **Read the plan before applying.** It has caught real damage: an apply that would have
  dropped the Cloudflare Access policy and left every kid's dev URL publicly reachable
- Maintain single opinionated way to do things
- Keep README.md accurate and sufficient for an unfamiliar operator
- Test "wake up" scenario (expired sessions)
- Run `terraform fmt -recursive` and `terraform validate` before planning

### Don't:
- Add options or alternatives
- Create extra documentation files
- Add avoidable manual setup steps
- Make users think about configuration
- Cache auth state without re-checking (breaks session expiration)
- Reintroduce a Make/container wrapper around Terraform
- **NEVER add Claude Code attribution to git commits** - no "Generated with Claude Code" or "Co-Authored-By: Claude" in commit messages

### Code review

Greptile reviews pull requests. `.greptile/` holds its settings, rules and context, and
`scripts/test-greptile-config.py` pins them.

- A push does not start a review. Comment `@greptileai review`: Greptile reacts 👍 and runs
  the `Greptile Review` check, whose summary reads `N files reviewed, M comments added`.
- Findings arrive as inline comments. `main` requires conversation resolution, so answer
  each one and resolve its thread before merging, then ask for another review. A clean
  re-review adds no comments and no review object.
- Greptile writes its summary into the PR description below `<!-- greptile_comment -->`.
  Edit only the text above that marker.
- Greptile read #135's instructions from the PR's head commit, so a PR that edits
  `.greptile/` changes its own review. Changing a rule's severity, scope or text, the
  instructions, `files.json` or `rules.md` fails the pin test until the matching pin in
  `scripts/test-greptile-config.py` changes in the same PR.

**Stop rule: do not churn on reviewer bots.** Codex, CodeRabbit and Greptile always find
another edge case, so "no findings" is not a goal. On 2026-09-27/28 about 8 hours went into
review loops: 22 Codex rounds on one PR, and 5 rounds on a one-word fix (#179) spent hardening
its *test* while the real fix sat unapplied and the repo stayed drifted. Per PR:

1. **Security and correctness P1s:** fix them, with tests. Care here is the point.
2. **P2s that change what production does:** fix them.
3. **Tests-of-tests, doc wording and hypothetical edges:** at most one fix round, then a
   follow-up PR. They never block the merge.
4. **Merge and apply** once CI is green and the P1s are addressed. Do not re-request review
   "once more" for a small change. Resolve a thread you disagree with by replying with the
   evidence.
5. **A live fix ships first.** For drift, an outage or a found security hole, harden in a
   follow-up.
6. **After about three review rounds,** stop and tell the owner what is left and whether it
   matters.

**Mutation checks: bounded.** Breaking the code to show a new test fails is worth doing, but only
for what changed. On 2026-09-28 an agent spent 15+ minutes re-running a full 48-mutation set
(each a whole timing-sensitive test file) after a three-line fix.

- Check only the mutations for the tests you added or changed in this commit, typically 1 to 5.
  Never re-run the whole historical set.
- Confirm each mutant really **fails**. A crash, a mutation that did not apply, or a skipped test
  is not "caught". Check that the file actually changed and that the test output shows a
  failure, and report a survivor as a survivor.
- Keep a mutation run to a few minutes. If it would take longer, run the few mutations that
  matter and say so.
- Mutation checks are a local, per-change check. There is no CI mutation job, and none is
  needed per PR.

### Common Pitfalls

**Dev boxes and Terraform state**: dev boxes cannot read state or its lock table, on
purpose: state holds credentials. To check from a dev box whether main is applied, read
`/infra/applied-status` (see `applied-status.tf`). The jumpbox's Stop hook publishes it
after every plan of a clean origin/main.

**Credential ownership**: Personal Codex, Claude, GitHub, and Vercel device logins belong
to one Unix user. Never seed them from Terraform, copy them between users, or overwrite a
working personal login with a bootstrap token.

**Email addresses stay out of the repository**: never write a person's email address into a file, a comment, a
test, a default value, a commit message, or a pull request title, description or review comment, however the
configuration needs it (an Access allowlist, an owner, an account's root address, a site admin). Those are read from
outside git: the `people/addresses` secret in Secrets Manager (`people.tf`) for the infrastructure, the feature's own
secret or the ignored `terraform.tfvars` where its rules say so. In text, code and tests use a made-up address at `example.com`, `example.net` or `example.org`.
`scripts/test-people.py` fails on a personal mailbox in any tracked file, so a leaked one fails CI. Git history keeps what it
already holds; do not rewrite it for this.

**Session values stay out of the repository**: A value copied from a working session (a shell,
an environment variable, a launch command or a transcript) is not useful to the repository and
goes stale: profile names, account names, one-off paths, model names, local ports. Don't put
one in a file, a comment, a commit message, or a pull request title, description or review
comment. Use a variable or a made-up placeholder (`example-profile`, `example-name`). Where
something specific has to be named, use its publicly documented identifier. Check the diff,
the commit messages and the PR text before every push.

**Terraform worktrees fill the jumpbox root**: each fresh worktree's `terraform init` downloads about 1 GB of
providers into its own `.terraform`. They were never cleaned up, and on 2026-10-01 28 of them took the
40 GB root volume to 100% (`terraform init` then failed with "no space left on device"; a full root is the
same failure that hung ssh on 2026-07-25). Remove the worktree when the apply and its empty follow-up plan
are done: `git worktree remove --force /tmp/tf-<stamp>`. If one is left behind, `rm -rf <worktree>/.terraform`
frees the space and `terraform init` regenerates it; the source and any saved plan stay. Check
`df -h /` before an init.

**Stale plans**: `terraform plan` refreshes state against AWS, so a plan from before someone else's change is not safe to apply. Re-plan if the apply is not immediate.

**Cold-bootstrap inputs**: Some secrets, backend resources, and device logins necessarily
pre-exist Terraform. Keep the exact prerequisite list and login sequence in `README.md`.

**Workers Builds credential boundary**: its control API requires a user-owned Cloudflare
token; the existing account-owned `cloudflare-tunnel-token` cannot be reused. Terraform
manages only the `cloudflare-workers-builds-control-token` container and reads its payload
ephemerally, so the value is not stored in state. At
[Create Additional Tokens](https://dash.cloudflare.com/profile/api-tokens), choose **Use
template** because Custom Token cannot grant API Tokens Edit. Retain User → API Tokens →
Edit (API name: API Tokens Write) and add Workers Builds Configuration Edit plus Workers
Scripts Read for account `12ea67fb7ced068de03f35c22688e436`. Terraform then mints the
narrower Workers Scripts Write deploy token. Start the GitHub connection in Cloudflare at
Workers & Pages → target Worker → Settings → Builds → Connect → GitHub. The `CoderColton`
repository owner must authorize only `CoderColton/colton-games`; `ejc3` has write, not
admin, and cannot grant the App. When authorization returns to Cloudflare, stop before
selecting/saving the repository or any build settings. Terraform owns the connection and
triggers, and the connection cannot be imported.

**Backend versioning gate**: apply only `aws_s3_bucket_versioning.terraform_state`, verify
S3 reports `Enabled`, and wait at least 15 minutes. Only then apply the empty control-token
secret container so that Terraform writes the backend state after propagation. The state
object's read-only `head-object` result must contain a non-null `VersionId` before any
deployment token or repository connection is created. With both gates still false, next
run and apply a full saved plan that contains only the account Workers subdomain/base
configuration and require an empty follow-up. Follow the exact commands and remaining
gates in `README.md`; never substitute a manual S3 test-object write.

**Write-only Cloudflare state**: the narrower build-deploy token is returned only once,
and repository connections have no read/list endpoint or import path. Keep backend
versioning and every related `prevent_destroy` guard. Never disable the Builds gate after
these resources exist, and never apply a plan that replaces them without explicit recovery
intent.

**GitHub runs credential-free validation, not live drift**: `drift.yml` installs locked
providers with `-backend=false`, runs `validate`, and checks CI boundaries. Never restore
state, secret-payload, Lambda-environment, or log-read access to make a GitHub full plan
pass. Those reads can convey administrator credentials. Full plans belong on a jumpbox;
a successful validation is not an empty live plan. The shared main/staging CI roles are
retired with Deny `*`. GitHub OIDC is used by the owner-approved AMI publisher, `imagine-deploy`
(`imagine.tf`) and `dolphin-labs-news-bedrock` (`bedrock-haiku.tf`), each trusted for one repository's `main`
or environment.

**Optional Mac is off and its timestamp is stale**: never set `enable_mac_dev=true` without
also supplying a new `mac_teardown_at` at least 24 hours after allocation and reviewing
Dedicated Host quota/capacity. Teardown deletes the Mac root.

## Development Instances

The long-lived development and administration instances are:

| Instance | Type | Purchase | Purpose | Terraform |
|----------|------|----------|---------|-----------|
| jumpbox | t4g.large (2 vCPU / 8GB) | on-demand | Remote management, admin AWS access | jumpbox.tf |
| jumpbox-2 | t4g.micro (2 vCPU / 1GB) | on-demand | Independent recovery admin host | jumpbox2.tf |
| fcvm-metal-arm | r8gd.metal-24xl (96 vCPU / 768GB, Graviton4) | spot | Firecracker/KVM on ARM64 | firecracker-dev.tf |
| fcvm-metal-x86 | c5d.metal | spot | Firecracker/KVM on x86 | x86-dev.tf |
| nextjs-dev | t4g.xlarge (4 vCPU / 16GB) | **on-demand** | Kids' Next.js games behind Cloudflare Access | nextjs-dev.tf |
| io-box | i8ge.large | persistent spot | Private ephemeral NFS scratch | io-box.tf |
| wbox | g4dn.xlarge, Windows Server 2025 | on-demand, stopped when idle 1h | Windows game playtesting; RDP/DCV from dev boxes only; `wbox up/down/run/launch` | wbox.tf |
| claude-master-server | t4g.micro | on-demand | Holds the Claude subscription logins and serves every other box; clients authenticate with a certificate it issued; private only | claude-master-server.tf |

**nextjs-dev is deliberately on-demand.** It ran as spot until 2026-07-25, when it was
reclaimed six times in one day and then could not restart at all -- the spot request
reported `capacity-not-available` and the kids' URLs were simply down with no ETA. Spot
placement score was 3/10 in every US region and for every alternative instance type, so
neither moving region nor changing family was a way out. It was first sized down large ->
medium so the durable option cost about what the unreliable one did (~$29/mo vs ~$24/mo spot),
then doubled to t4g.xlarge on 2026-09-15 (#139) once four accounts shared it: at 4 GB it paged
until the disk saturated.

### Shape of the shared-box design

One machine, several people, and no coordination between them. Three ideas do the work, and
they recur -- reach for them before inventing a registry or a lock:

1. **Derive, don't allocate.** A port is `3100 + cksum(hostname) % 800`; a t-claude window
   key is `cksum(path)`. Nothing is handed out, so nothing needs releasing, and the same
   input gives the same answer after a reboot, a rebuild, or a spot reclaim.
2. **Be explicit where a default would drift.** `ndev` passes `--port "$PORT"` rather than
   letting `next dev` pick: on a busy port Next does not fail, it silently moves to the next
   one, and the tunnel then serves someone else's app. An explicit value turns a silent
   mix-up into a loud bind error.
3. **Separate identity, shared machine.** Own Unix account, own GitHub login, own Anthropic
   login, own checkout. Only the admin key is common. Per-user systemd units (`ndev@`,
   `claude-rc@`, `codex-rc@`) mean each person's services start and fail independently.

The failure this prevents is not a crash -- it is two people quietly sharing one thing and
believing it is theirs.

### The kids' dev box and `cc-games.dev`

Each kid has their own Unix account, GitHub login, Vercel identity, and a permanent URL:

| account | URL | port |
|---------|-----|------|
| colton | https://colton.cc-games.dev | 3729 |
| connor | https://connor.cc-games.dev | 3641 |

Nothing listens publicly. `next dev` binds 127.0.0.1, and `cloudflared` dials **out** to
Cloudflare, so there is no inbound web port to open:

```
phone -> Cloudflare edge -> Access (Google login, email allowlist) -> tunnel -> 127.0.0.1:<port>
```

- Ports are derived (`3100 + cksum(hostname) % 800`), so they survive restarts and rebuilds
- `ndev` inside a project publishes it: registers the hostname and starts a systemd unit
- Each server runs as `ndev@<user>.service`; agents run as `claude-rc@` and `codex-rc@`.
  All are enabled at boot -- a reboot restores every URL with nobody logged in
- A deleted project directory must not leave a unit restarting forever: `ndev@`'s
  `ExecCondition` skips it, and `ndev-prune` (setup, setup-sync, nightly timer) unpublishes
  it -- unit, env, drop-in, every registry row carrying its hostname or dir (pinned aliases
  included) and ingress. It keys only on the recorded `DIR` being
  gone; never widen that to anything a live checkout could match
- `cloudflare.tf` holds the tunnel, wildcard DNS, Access app and policies. A service token
  (`cc-games-access-service-token` in Secrets Manager) allows non-interactive access.
  Terraform also owns the account Workers subdomain and Colton Games Builds configuration
- The kids have **full passwordless sudo**. The instance is the sandbox: no inbound web
  ports. Its IAM role reads one S3 object and two secrets, can describe instances for hop
  aliases, and has the scoped `DevEBS=true` temporary-volume policy. Don't re-narrow it

### Colton Games Workers Builds

The ordinary Cloudflare provider remains authenticated with the account-owned tunnel
token. Only resources that call user-scoped Workers Builds or API-token endpoints use the
`cloudflare.workers_builds` alias. Do not broaden that alias to existing tunnel, DNS, or
Access resources.

The checked-in `colton_games_workers_builds_enabled` gate exists solely for the cold
bootstrap and must start false. Before changing it, verify signed fork release `5.24.0`,
the 15-minute backend-versioning wait and non-null state-object `VersionId`, the raw
dedicated control-token secret, and the repository-only Cloudflare GitHub App grant from
the `CoderColton` owner. Once the protected repository connection and write-only build
token exist, do not turn the gate off: the resulting destroys are intentionally blocked.

The Worker and Access application must remain acyclic. Access targets the immutable Worker
tag from `local.colton_games_worker_id`; it must not reference the Worker resource. The
Worker explicitly depends on Access and the account subdomain so a later
`colton_games_worker_urls_enabled = true` cannot expose workers.dev or preview URLs first.
The repository's Wrangler config enables workers.dev and previews, so the two triggers and
environment maps use a combined Builds-and-URLs gate. The Builds-only apply may create only
the deploy token, its registration, and repository connection and must be followed by an
empty plan. The URLs apply then performs the Worker update and creates both triggers and
environment maps; their explicit Worker dependency prevents a push race. Require another
fresh empty plan before testing anything, then test unauthenticated denial and service-token
access. Cause a `synchronize` event on still-open Colton Games pull request #26 and verify
its protected immutable preview and branch alias. Only after that succeeds may #26 merge
to `main` to trigger and verify staging; a preview cannot be tested while previews are
disabled or after its pull request is already merged. Never turn either gate back off:
`prevent_destroy` intentionally blocks that rollback.

### Hopping between dev servers

ARM, x86, and the `ubuntu` account on Next.js receive a dedicated hop key
(`dev-hop-key.tf`), never the jumpbox key. I/O and parallel boxes trust its public half for
inbound access but do not receive the private half:

```bash
ssh fcvm-arm      # stable EIP
ssh fcvm-x86      # stable EIP
ssh nextjs
ssh io            # fixed private address across the inter-region VPC peer
```

`~/.ssh/fcvm-ec2` is deliberately **absent** from the dev servers. It opens the jumpbox,
where the AWS admin session lives, and nothing on a dev box used it. The hop key's public
half is authorized on dev servers only, so holding it gets you another dev box and nothing
more. Verified: dev -> dev succeeds, dev -> jumpbox gives `Permission denied (publickey)`.

**DEV BOX -> JUMPBOX CONNECTIONS ARE NOT PERMITTED**, by any mechanism: not SSH, not a
forced-command key, not `ssm:SendCommand`, not a queue the jumpbox polls. That rule exists
because it was broken once already -- `pbox-key.tf` reintroduced the path with a
forced-command key shortly after `dev-hop-key.tf` closed it, and it survived because "it
can only run one script" sounds safe. It is not the transport that matters: every
delegation shape ends with "a compromised dev box makes the jumpbox run something as
root", so they differ in audit trail, not capability. Do not offer one as a mitigation for
another.

When a dev box needs a privileged action, give it a **narrow, tag-scoped IAM grant** and
let it call AWS directly. `parallel-box-launch.tf` is the worked example. Two traps that
make such a policy fail closed rather than silently over-grant:

- **A tag condition denies any resource the call creates without that tag.** `RunInstances`
  creates an instance, a volume AND an ENI; gating all three on `aws:RequestTag/Name` means
  the launch template must tag all three, with a value the condition actually allows. A
  root volume tagged `parallel-box-root` fails a condition that lists `parallel-box`. Both
  mistakes were made here and both failed identically for every instance type, which reads
  exactly like a spot-capacity drought rather than a policy bug.
- **`iam:PassRole` is the escalation.** Pin it to one role ARN with `iam:PassedToService`.

`aws iam simulate-principal-policy` settles these in seconds without launching anything --
check both the allow and a deliberate deny.

### Metal Claude startup

`fcvm-claude-rc.service` starts `t-claude --remote-control` at boot for host-local active
repositories. Interactive t-claude use writes an unsynchronized marker; recent local HEAD
reflog movement and dirty worktrees seed checkouts automatically. Do not use Claude's
`history.jsonl` as the activity signal: `claude-code-sync` merges it across ARM and x86;
the launcher reads it only as a bounded index of possible nested roots and still requires
host-local Git activity. It restricts roots to `/home/ubuntu` and `github.com/ejc3/*`, and
uses t-claude's default path-derived sessions. t-claude follows its `main` branch, so the unit
is guarded by an integrity check (`zsh -n` on the installed file) rather than a fixed hash,
and by live `claude auth status`, so credentials remain personal and Terraform never seeds or
copies them.

The unit holds ubuntu's tmux server, so restarting it kills every session. Nothing automatic
does: needrestart lists it as deferred after a library update
(`/etc/needrestart/conf.d/fcvm-claude-rc.conf`), and unattended-upgrades does not reboot the
metal boxes (`/etc/apt/apt.conf.d/52unattended-upgrades-no-reboot`). They are spot boxes and
stop and start on their own terms; the on-demand boxes keep the 03:00 reboot.

Every folder that gets a window at boot also gets a Codex thread, so it shows up in the Codex
app: `fcvm-codex-seed.service` reads the launcher's list (`~/.local/state/fcvm-claude/repos`)
and seeds one message per folder through the running Codex daemon, and a timer retries until
Codex is logged in. nextjs-dev does the same per user with `codex-seed@<user>`, for
`local.nextjs_codex_seed_users` only (Colton, Connor and ejc3). A folder that already has a
thread is only checked.

The tmux these sessions run is `tmux-scroll` (t-claude prefers it), pinned by release tag and
sha256 in `tmux-scroll.tf`: the metal updater, nextjs-dev's setup and a one-shot SSM install on
both jumpboxes all use that one pin. **Exception: fcvm-metal-x86.** The pinned release has only
an aarch64 build, so the x86 box keeps whatever tmux-scroll it already has (its updater logs "no
pinned build for x86_64") until an x86_64 asset is published and pinned.

All metal repositories run as `ubuntu` and therefore share one tmux server. Keep one
aggregate systemd service and one cgroup; do not create per-repository units. Stopping or
restarting the aggregate can kill every managed and interactive t-claude session in that
shared server. The Next.js box is different: Colton and Connor are separate Unix users,
so their `claude-rc@` units own separate tmux servers.

### The shared claude-master server

Subscription logins rotate on every refresh and the previous access token stops working at once,
so a login copied to several boxes destroys itself within hours (measured: refresh, then the old
access token returns 401). One box therefore owns each login: `claude-master-server`
(`claude-master-server.tf`, 10.0.1.50:8443). Everything else is a client; none holds a login.

- **No password anywhere.** The proxy is TLS and requires a client certificate signed by the
  server's own CA. The CA key lives only in `/var/lib/claude-master/state` on that box. Client
  certificates last 30 days (at most 90), so a lost box expires on its own.
- **claude-master has no AWS in it.** It is a generic binary (`ejc3/CLIProxyAPI`, `docs/claude-master.md`):
  `serve`, `issue`, `client-init`, `connect`. All AWS glue is in this repo. The pinned release is
  `local.claude_master_tag` plus its sha256 in `claude-master-server.tf`; bump both together.
- **Logins are interactive and belong to the server.** `scripts/claude-master-login.sh --server`
  drives the five paste-a-code logins; the service stays idle until all exist. Never copy a login
  from another box. Nothing on the box restarts a server or Envoy on its own; `claude-master-status` shows
  running versus pinned.
- **Restarts nobody notices: Envoy and blue/green** (`claude-master-server.tf`, ROLLING RESTARTS). Envoy (pinned
  version and sha256) listens on the client address and the tunnel's loopback port and passes TCP through,
  unchanged, to one of two servers on the box, `claude-master-server@blue` or `@green`, each on its own port of
  the same address (the server certificate names the address clients dial; the color ports are in no security
  group). `sudo claude-master-rollout` (over SSM) starts the idle color, waits until it listens, points Envoy's
  NEW connections at it (an endpoint file Envoy watches, replaced by a rename), checks Envoy's admin API reports
  it, then stops the old color. The old one drains (`serve --balanced`): it keeps serving the connections it has,
  every response closing its connection, so each client moves over on its next request without an error, and
  running requests get up to `local.claude_master_drain_seconds` (the unit's stop timeout is a minute longer).
  That is how a new binary goes live. Each color has its own `--instance` (metrics) and log file
  (`server-blue.log`, `server-green.log`). Through Envoy, claude-master sees every connection come from the box
  itself: a handshake failure's `remote=` no longer names the client box. The first rollout on a box from
  before Envoy stops `claude-master-server.service` (it drains for 60 s), lets Envoy take the client port, and
  starts the old server again if Envoy does not come up.
- **Adding a client** (a new dev box, a Mac): `scripts/claude-master-enroll.sh NAME [--ssh HOST]`.
  The key is made on the client; only the request goes up. Signing runs on the server over SSM, so
  it is an AWS-authenticated action. Renewal is automatic (next bullet); the script swaps only after the
  new certificate is in place.
- **Reaching it.** Dev boxes: the private address. A Mac with AWS access: `scripts/claude-master-tunnel.sh`
  (an SSM port forward, session type `AWS-StartPortForwardingSessionToRemoteHost` aimed at 10.0.1.50: the
  plain forwarding session reaches the instance's loopback, where nothing listens), then `claude-master
  connect --server 127.0.0.1:8443`; the certificate also names loopback for exactly this. Nothing is opened to the
  internet; a Cloudflare tunnel with Access is the path for machines without AWS access, and would
  be a new ingress in `cloudflare.tf`, not a new open port.
- **Macs outside the VPC (the kids' laptops).** A long-lived Cloudflare tunnel (`claude-master-tunnel.tf`,
  hostname `inference.cc-games.dev`): cloudflared on the server dials OUT and forwards to claude-master's
  OPEN listener, which is bound to loopback only (`--open-loopback 127.0.0.1:8444`) and asks for no
  client certificate; the trust is the tunnel. Cloudflare Access on that exact hostname admits one
  thing, a service token (no person, no wildcard; a year long). The Mac runs `cloudflared access tcp`
  and `claude-master connect --open 127.0.0.1:8444 --ca ca.pem`. Hand a Mac its one private file with
  `scripts/claude-master-mac-bundle.sh OUTDIR` (an administrator action; it contains the token). The
  certificate listener on the private address is unchanged: dev boxes keep per-box certificates. Never
  put the open port in a security group or bind it to anything but loopback.
- **Client certificates renew themselves** (`claude-master-cert-renew.tf`, `scripts/claude-master-cert-renew.py`). A Lambda runs daily at
  08:00 UTC; for each client in `local.claude_master_clients` it reads the certificate's end date over SSM and, with under 14 days
  left, renews it: the client makes a NEW key and request (the key never leaves the box), the server signs it, the client verifies it
  (chains to the CA, names this client, matches the pending key, lasts 20+ days) and swaps it in with two renames, so a failure
  leaves the old identity working. It works on an already-expired certificate, so a stopped spot box catches up when it next runs.
  The role can send exactly four custom SSM documents (`scripts/ssm/claude-master-cert-{status,request,sign,install}.sh`, every
  parameter pinned by a regex) to the server and the client boxes, never `AWS-RunShellScript`; the signing document signs only for
  the exact name asked for. It never enrols (an account with no certificate is skipped: enrolling stays the switch), never touches
  a stopped box, never restarts anything. Alarms: the job errors, it has not run for two days, or any certificate has under 7 days
  left (`ClaudeMasterCerts/ClientCertDaysLeft`). A NEW client is added by enrolling it by hand once and adding it to
  `claude_master_clients`. `aws lambda invoke --function-name claude-master-cert-renew --payload '{"dry_run": true}' ...` shows what it
  would do; `{"renew_before_days": 60}` forces a renewal. The CA lasts ten years and the proxy renews its own certificate.
- **nextjs-dev accounts use the pool through t-claude.** The setup script installs the pinned `claude-master` (the server's tag and
  sha256, one pin) and writes a marker block into `/etc/zsh/zshenv`: for an account whose own client certificate
  (`~/.config/claude-master/client.pem`) is readable it exports `TCLAUDE_INFERENCE_SERVER`, and t-claude (`--inference-server`,
  ejc3/t-claude) then runs `claude-master connect` in place of plain claude, keeping `--continue`, `--remote-control` and the hooks.
  An account with no certificate is unchanged, so **enrolling is the switch**: `scripts/claude-master-enroll.sh nextjs-USER --ssh HOST
  --as USER` (one certificate per account, so the `client` metric shows who). The account keeps its own native login for Remote
  Control; only inference goes to the pool. t-claude hands Claude its session-sync and notification hooks as `--settings FILE`; claude-master
  allows `--settings` unless the settings are known to conflict (login/provider keys, or provider/proxy variables in `env`), and names the
  setting it refused (release `claude-master-4d4910f` onward; before it every `--settings` was refused and a pool launch failed with
  "launcher flags cannot override master identity or provider routing"). Release `claude-master-f9e4b9e` adds two things learned on
  2026-10-09: a conversation whose subscription runs out MOVES to another one even though it carries signed thinking (Anthropic binds
  a thinking signature to the model and the conversation prefix, not the account: checked on the pool's own logins, an Opus 5.5 block
  replays on another subscription untouched and a Sonnet 5.5 block is dropped by the API with the request still succeeding; only signed
  compaction, container, file and encrypted server-tool state still pin a conversation); and a `NODE_OPTIONS` that only sizes the V8
  heap (`--max-old-space-size=N`) is allowed in settings and the environment, where before any `NODE_OPTIONS` refused the launch. From
  `claude-master-c366cea` errors reach the session exactly as Anthropic sent them (status, body, `request-id`, `retry-after`,
  `anthropic-ratelimit-*`), including after a retry and a refusal, and a bound subscription's last upstream error for that model is
  relayed again on later requests; a refusal with nothing upstream behind it is synthesized in Anthropic's shape with the type and status
  Anthropic would use (429 `rate_limit_error` with the unified headers and `retry-after` at the earliest reset when the pool is out of
  weekly quota, 404 `not_found_error` for a model the bound subscription does not serve, 400 for opaque state, 529 for a cooldown), so
  Claude Code reacts as it would to Anthropic instead of retrying a generic 503. From `claude-master-7934205` a failure benches
  nothing: the scheduler underneath benched a subscription twelve hours for one 404, and two 404s Anthropic did not repeat took the
  pool down twice on 2026-10-09 ("no auth available" for every Opus request until a restart). Only a used-up weekly quota (429) or a
  dead login (401) moves work; every other failure is relayed and the next request goes to the same subscription. Each upstream error
  is in the server log (type and message). A model only one subscription serves stays with that subscription. From
  `claude-master-5c78013` a 404 whose body is not Anthropic's error object is retried once on the same subscription (Anthropic
  names a model it does not serve with `not_found_error`; a bodiless 404 is a path, an intermediary or a transient: 139 of them in
  26 hours on 2026-10-09/10, across every subscription and the API-key backup, while the same subscriptions served the same
  models), the log line for a non-Anthropic body carries content type, request-id, cf-ray, server and a 120-character body
  prefix, and the usage-poll warning says what failed. That evidence line showed what they were: Anthropic's own
  `not_found_error`, 234 bytes of JSON behind a `Content-Encoding` the native passthrough hands over as sent and the proxy never
  decoded (the session read it; the log read `type= message=`). From `claude-master-87f05f5` a compressed upstream error is decoded
  once in the record (gzip, deflate, br, zstd), relayed plain, logged readable, and a `not_found_error` about something in the request
  other than the model (a file, a container, another account's resource) is named as such instead of "does not serve the model".
  From `claude-master-069ec38` a stopping server drains: new inference requests get Anthropic's retryable 529 and running ones
  get up to 60 seconds to finish before they are cancelled (inside systemd's default 90-second stop timeout; do not lower it), and
  a cancelled request is written as a retryable 529, not the 499 "Configured inference failed" Claude Code does not retry (a
  restart at 2026-10-10 05:45 failed a running turn that way). A restart still interrupts a stream that runs past the drain.
  `claude-master-09015c6` adds four things. A local forwarder: `connect` gives Claude a plain-HTTP CONNECT proxy on
  127.0.0.1 with a per-launch token; only `api.anthropic.com` with that token goes through the pool, everything else (git, curl,
  gh, hooks, MCP servers that inherit `HTTPS_PROXY`) is dialled from the box itself, and Claude no longer holds the client
  certificate. Before it those tools failed the proxy's certificate handshake: the `client TLS handshake failed` lines from the dev
  boxes. Token counters read from each response's `usage`: `claude_master.inference.tokens` {profile, type},
  `.tokens.by_client_account` and `.tokens.by_client`, type input / output / cache_read / cache_creation (about 50 new series).
  Five-hour window gauges (`claude_master.quota.five_hour.*`). WebFetch's domain check (`GET /api/web/domain_info`) is relayed to
  Anthropic instead of refused. The next PR in that stack (`service.instance.id` on every metric) is NOT released: it adds a
  dimension to every series, so it must ship together with every `SCHEMA(...)` query here and in the dashboard repository.
  `claude-master-dcb7263` fixes two things 09015c6 got wrong in production. No token series appeared in two hours of traffic:
  Anthropic answers compressed and the scanner read compressed bytes; it now decodes a copy (gzip, deflate, br, zstd) and the
  client's bytes stay as sent. And the first drained restart "waited 2ms" for 2 running requests: the backend's lifetime was the
  serve context, which the stop signal cancels, and every request handler stops with it; the backend now outlives the signal and
  stops at Close, after the drain. There is no fallback to a login on the box: a server that is down or out of quota is an
  error in the session, not a quiet switch. Certificates last 30 days and renew themselves (the bullet on certificate renewal). A window running when an
  account is enrolled keeps plain claude until it is relaunched; the window saves the server, so a relaunch (`/clear`, `/cd`) keeps it.
- **fcvm-metal-arm uses the pool the same way** (`claude-master-client.tf`): an SSM association installs the pinned client (the
  server's tag and sha256) and the same certificate-gated `/etc/zsh/zshenv` block, daily and once when applied, and restarts nothing.
  The block also exports `TCLAUDE_CLAUDE_MASTER=/usr/local/bin/claude-master` (here and on nextjs-dev): this box has a personal build in
  `~/.local/bin` ahead of the shared one on PATH, an older build that refuses t-claude's flags, and the route must use the pinned one.
  The `ubuntu` account's sessions share one tmux server, so a running session keeps plain claude; a window launched afterwards goes
  through the pool and saves the server. Enrolling is the switch: `scripts/claude-master-enroll.sh fcvm-arm --ssh HOST`. Mind the
  capacity: one heavy account on a pool of five subscriptions drains it for everyone (see `claude-master-status` quotas).
- **Convergence.** The instance ignores user_data, so `terraform_data.claude_master_server_converge`
  re-runs the bootstrap through SSM when the script, the pin or the instance changes. It never restarts
  a running server or Envoy; `claude-master-status` shows running versus pinned and a new binary takes
  effect at the next `sudo claude-master-rollout`.
- **Size.** t4g.micro with a 1GB swapfile. t4g.nano was tried first and was OOM-killed during its own first boot.
- **Logs.** The proxy writes its own log (`/var/log/claude-master/server-<color>.log`, always `info`, rotated by
  the program at 20 MiB x 5, 0600) and the CloudWatch agent ships it to log group `/claude-master/server`
  (90 days). `info` says when something CHANGES: a conversation moved to another subscription (from, to,
  reason, both quotas), a profile rate limited or available again, a quota band crossed (ok / reserve at
  90% / exhausted), the paid API-key backup used, a login rejected or refreshed, a client's first
  connection, refused handshakes; plus a per-profile quota snapshot and routing summary every 5 minutes.
  It never contains tokens, bodies, URLs, account ids or upstream text. For `debug` (every routing
  decision) run `claude-master serve` by hand with `--log-level debug`; do not change the unit to it. The
  journal is capped at 200M.
- **Metrics.** The proxy exports OpenTelemetry metrics (`--otlp-endpoint`) to the CloudWatch agent on
  `127.0.0.1:4318`, which publishes them in namespace `ClaudeMaster` (the agent turns cumulative counters
  into deltas) along with `mem_used_percent` and `swap_used_percent`. Every inference request carries
  `profile`, `client` (the box's certificate name), `model`, `status_class` and `client_account`, the
  incoming user's Anthropic account: a name from `/etc/claude-master/account-labels`
  (`ACCOUNT_UUID=NAME`, UUID = `oauthAccount.accountUuid` in that user's `~/.claude.json`), else
  `acct-<8 hex of a hash>`; the account id is never exported (`claude-master account-key UUID` prints the
  key, to tell which `acct-` is whose). The file is written from the Secrets Manager secret
  `claude-master/account-labels` (administration and the server's role only) by
  `/usr/local/bin/claude-master-account-labels`, which systemd runs as root before every start; claude-master
  reads it only when it starts, so a new value takes effect at the next restart. With no value, no access, or
  a line that is not `ACCOUNT_UUID=NAME` the old file is kept and the server starts anyway. Set the value
  from a file (README); never put a UUID or a name in this repository.
  Metric names and meanings: `docs/claude-master.md` in `ejc3/CLIProxyAPI`. Split with a Metrics Insights
  query: `SELECT SUM(...) FROM "ClaudeMaster" GROUP BY client_account`.
- **Alarms** (shared alert topic): instance status check, memory above 85%, heavy swap, and four taken from
  the log: `claude-master-CredentialRejected` (a subscription's login expired or was revoked: redo it with
  `scripts/claude-master-login.sh --server`), `claude-master-NoAccountAvailable` (clients are being
  refused) and `claude-master-UnlistedRoute`. Routing is compatibility first (owner, 2026-10-10: "our primary goal
  is compatibility and seamlessness, not pedantry"): from `claude-master-5b83ce9` inference (`POST /v1/messages`,
  `/v1/messages/count_tokens`) goes to the pool and every other Anthropic route goes to Anthropic unchanged, on the
  session's own login, exactly as Claude Code sends it without claude-master; only ambiguous or encoded paths (400)
  and the pool's own routes with the wrong method (405) are refused. Each refusal before this broke sessions
  (WebFetch's domain check; 2.1.296's `/api/hello`, which stopped every interactive start; claude.ai MCP connectors).
  UnlistedRoute is a review queue, not an outage: the route already works; list it in `proxyControlPath` once
  reviewed. Each route shape is logged once per server process, so an unreviewed route fires again after a rollout.
  To review a new Claude Code release, diff its `strings` for `"/api/`, `"/v1/` and `"/mcp-registry/` paths
  against `proxyControlPath`. The agent's IAM grant can publish to the one namespace and write the one log group.
- **Dashboard** `claude-master` (`claude-master-dashboard.tf`): requests by user, subscription, box, model and
  outcome; latency percentiles, Anthropic's first-byte time against claude-master's own overhead; each
  subscription's allowance, resets and Anthropic's own utilization; switches, rate limits, API-backup use;
  login health; connections; host memory; the log-derived alarms. Two Metrics Insights QUERY alarms with no
  dimension names (so a renamed dimension cannot turn one silently green): every subscription past 90% of its
  weekly allowance, and more than 20 Anthropic errors in 10 minutes. Every query was checked against the live
  API (`window` and `result` are reserved words and must be quoted); `scripts/test-claude-master-server.py`
  keeps them valid.
- **Cost shape.** CloudWatch bills every distinct combination of a metric's dimensions as its own custom metric
  (about $0.30 a month each) and every OTLP attribute becomes a dimension, so the proxy never crosses its axes:
  each metric carries at most three attributes and each axis (profile, user, box, model) has its own
  projection. Measured through the live agent: counters arrive as deltas, histograms as approximate statistic
  sets (no percentiles; the proxy publishes its own p50/p95/p99 gauges), and `service.name` is an extra
  dimension. Do not add an attribute to a request metric without checking how many series it multiplies.
- **Backup.** The root volume (the five logins and the CA key) is in the dev backup selection and the
  recovery controller's protected list, with the same cross-region re-encryption hop as nextjs-dev.

### New repositories appear in Claude Code and Codex within seconds

`agent-session-sync` (`scripts/agent-session-sync.py`, `agent-session-sync.tf`) is a 5-second watch on every
box that keeps remote-control sessions: both metal boxes, nextjs-dev's accounts and both jumpboxes. Before
it, the metal boxes looked once at boot (`fcvm-claude-rc`), nextjs-dev started only each user's one working
folder every 5 minutes (`agents-enable`) and the jumpboxes looked nowhere. Measured on fcvm with a real
launcher: a live `--remote-control` session existed 0.9 s after a clone finished; worst case is the 5 s
scan plus about a second.

- **What is new.** A top-level checkout directly under `~/*` or `~/src/*`, appearing after the watcher first
  looked. A MAIN checkout only: `.git` must be a directory, so a linked `git worktree` (its `.git` is a
  file) is never new, and nothing nested is scanned. The clone must be finished (no `*.lock`, the index
  exists). The first run only records what exists and launches nothing, so an old clone the boot launcher
  skipped stays skipped.
- **Whose repos.** The account's own GitHub login (from gh's `hosts.yml`) plus
  `~/.config/agent-session-sync/owners` and `--owner`. The ubuntu account on the metal boxes and jumpboxes
  also gets `dolphin-labs-hq/dolphin-labs` by exact name; never an organisation wildcard.
- **What it never does.** Own the user's tmux server (it waits for `claude-rc@<user>` or the boot launcher
  to have made it), start a window for an account that is not logged in, or take a session down when it is
  restarted (`KillMode=process`). Claude and Codex are tracked separately, so Codex not being logged in
  yet does not hold up Claude.
- **Surviving the things that happen to a box.** t-claude is looked for on every attempt, so a watcher that
  starts before the installer has put it in place retries instead of recording "done". tmux windows do not
  survive a reboot but `known.json` does, so it keeps the boot id: after a reboot, Claude is started again
  once for every repository THIS watcher launched (Codex threads live in Codex and are left alone; repos
  recorded at the first run belong to the boot launcher). A running Claude rewrites `~/.claude.json` from its
  cached copy and drops a trust entry added meanwhile (the same reason `claude-remote-control.tf` trusts every
  repo before starting any session), so the watcher keeps trust for what it launched: one stat per tick, a
  re-add only when the file changed and an entry is missing.
- **What it judges by outcome, not by intent.** A Codex seed turn takes minutes and its child can fail (the
  daemon starting or refusing), so starting it is not success: the thread is marked done only when the child
  exits 0, polled each tick while it runs, retried after a minute if it failed. The allowed owners are
  re-read when gh's `hosts.yml` or the owners file changes (two stats per tick), so `gh auth login` or an edit
  after the watcher started takes effect; a repo from a not-yet-allowed owner is remembered, launched if it
  appeared after the first run once its owner is allowed, and never if it was there at the first run (adding
  an owner must not start every old clone). A checkout is identified by the inode of its `.git` directory (git
  replaces HEAD and config constantly, never that directory), so a clone deleted and replaced between two
  ticks is new again; a filesystem that hands the same inode to the replacement within one tick would be
  missed.
- **Known gap: the jumpboxes have no boot-time owner of the tmux server.** The watcher never starts one (it
  would own the user's sessions), and neither jumpbox has `claude-rc@ubuntu` or `fcvm-claude-rc`. After a
  reboot there is no tmux until someone connects; the watcher retries every tick and starts the sessions
  (including the post-reboot ones) the moment a server exists. A boot launcher that owns the server is a
  design decision of its own (what owns the cgroup, what a restart kills), not part of this watcher.
- **Rollout and updates.** One script and one template unit (`agent-session-sync@.service`) installed by the
  same snippet everywhere, replaced atomically. The watcher re-executes itself when its own file changes, so
  no installer restarts it. Running jumpboxes converge through `terraform_data.admin_agent_session_sync`,
  which restarts the watcher only when its unit or policy changed (harmless to sessions: `KillMode=process`,
  state on disk). On nextjs-dev the watcher is enabled for an account logged in to Claude OR Codex.

### Codex updates and restarts

Updating Codex and running the update are two steps on purpose. A daemon keeps the binary it started
with, so after `current` moves to a new release nothing changes until the daemon restarts, and a
restart interrupts the turns it is running. On 2026-09-30 fcvm-arm's daemon (started Sep 28) still ran
0.154.0 with 0.159.2 installed, and the model list a client gets is filtered by its version.

- **Refresh is automatic.** `codex-update.tf`: a weekly SSM association re-runs the installer as
  `ubuntu` on both jumpboxes and both metal boxes (a stopped spot box catches up at its next run).
  nextjs-dev refreshes every account daily in its own updater and restarts them there.
- **Restart is a decision.** `codex-restart` (installed on every box; `scripts/codex-restart.sh`)
  prints, per account, the version its daemon RUNS vs the one INSTALLED, the connected app clients
  and the commands running under it. `codex-restart --restart` restarts only the stale daemons,
  through the `codex-rc@<user>` unit where there is one, and refuses while commands are running
  unless `--force`. It never signals a process itself and records each restart in
  `~/.local/state/codex-restart/restart.txt`. To see another account's daemon, run it with `sudo`.
- Claude has the same shape: `t-claude --restart`. Neither touches the other.

### Starter user-level agent instructions

`user-agents.tf`: every setup script that provisions homes (metal boxes, nextjs-dev's accounts, admin boxes)
runs `user-agents-seed`, which gives an account with no user-level instructions `~/.codex/AGENTS.md` (the real
file) and `~/.claude/CLAUDE.md` (a symlink to it), so Codex and Claude Code read one file.

- **One source.** The text is `scripts/user-agents.md`. Terraform embeds that file; never paste its text into a
  `.tf` file or another script (`scripts/test-user-agents.py` fails if a copy appears).
- **Create only.** If either path exists (a file, a symlink, a dangling one) the account is left as it is:
  nothing is overwritten, appended to or re-linked. The file is seeded once, not managed, so editing
  `scripts/user-agents.md` reaches new accounts only; an existing account is changed by its owner.
- **As the account, never as root.** Root only dispatches (`runuser`); the files are created by the account
  that owns them.
- **No box is replaced or rebooted for it.** It is published inside the S3 setup scripts, and every instance
  that fetches one ignores user_data changes. nextjs-dev takes it through `setup-sync`, a metal box at its next
  boot (`dev-selfupdate`), an admin box only when its setup script is re-run by hand.

### Diagnosing a wedged dev box

When an instance fails its status check, the reason is in the EC2 serial console ring
buffer and usually nowhere else. Three things about that buffer decide whether you ever
learn what happened:

```bash
# WRONG -- returns a CACHED snapshot that can be hours stale. During the 2026-08-08
# hang (12:19) this answered with 05:37 data and showed nothing wrong.
aws ec2 get-console-output --instance-id <id> --region us-west-1

# RIGHT -- reads the live buffer, which held "kernel BUG at arch/arm64/kvm/nested.c:754"
aws ec2 get-console-output --instance-id <id> --region us-west-1 --latest
```

**Capture before you recover.** A reboot preserves the buffer; a **stop/start clears it**.
Stop/start is the remedy for a wedged box, so recovering it destroys the evidence. Always
snapshot with `--latest` first. `dev-diagnostics.tf` now does this automatically on every
status-check alarm (archived to CloudWatch Logs `/dev-servers/console-capture`, with the
panic signature quoted in the SNS alert), so the archive should already exist -- check it
before assuming the cause is unknowable. Private key blocks, including a partial one at
either end of the buffer, are replaced with `[redacted private key]` before the text is
matched, archived or emailed: boot-time xtrace has put keys on these consoles before.

The boxes are also configured to make a hang legible rather than silent: `panic_on_oops`
turns an oops into a reboot (the cmdline carries `panic=-1`) instead of an indefinite
hang, hung-task detection logs D-state pileups, sysrq is available on the console, and
journald is persistent so the last pre-death log survives the reboot.

### A wedged on-demand box is rebooted automatically

`auto-reboot.tf` and `scripts/auto-reboot.py`: a Lambda every five minutes. The owner asked for it after
nextjs-dev sat dead for 20 hours on 2026-10-01 (it is a standing authorization: this is the one automated
restart, and it is narrow). It is for ON-DEMAND boxes only: for each RUNNING instance named `jumpbox`,
`jumpbox-2`, `nextjs-dev` or `claude-master-server` it asks CloudWatch whether the instance status
check has failed for 15 minutes in a row, or NetworkOut has been exactly zero for 20 (the second catches the
wedges where the status check still reads ok: 2026-07-25, 2026-08-16, 2026-10-01). Wedged means: console
snapshotted first through the existing redacting capture Lambda, then an OS reboot, then a message on the alert
topic.

- **Never stop/start** (a reboot keeps the console buffer and instance-store disks), never a box that is not
  running (a deliberate stop stays a stop), never a SPOT box (`fcvm-metal-arm`, `fcvm-metal-x86`, `io-box`: the
  owner does not want those restarted automatically) and never an ephemeral one (parallel, GPU, wbox, runners, mac).
- **Brakes:** one reboot per 3 hours and 3 per 24 per box, kept in the `auto-reboot-state` table; after that it
  alerts once every 3 hours and leaves the box for a person. A box that wedges again straight after a reboot
  has a cause worth reading, not a loop to run.
- **Host problems are a veto.** A failing SYSTEM status check is AWS hardware: a reboot does not fix it, and a
  dead host also zeroes the network, so it is checked FIRST and only ever alerts.
- **Evidence must be fresh.** Only a full window of consecutive buckets ending recently counts: old zeros, a gap
  or missing data never reboot a box that is fine now.
- **The reboot is reserved before it is done.** A conditional write to the state table comes first, so a failed
  write means no reboot and two overlapping runs cannot both reboot; if the reboot call itself fails the
  reservation is given back. A failure on any box fails the invocation (after the others are handled) so the
  Lambda `Errors` alarm fires; AWS does not retry a failed run.
- **Its only EC2 write is `ec2:RebootInstances`, by Name tag.** A new persistent box joins by adding its Name
  to `local.auto_reboot_names`.
- **Limits.** Rebooting does not cure what caused the wedge: after one, read the capture
  (`/dev-servers/console-capture`), as for any wedge. `aws lambda invoke --function-name auto-reboot --payload
  '{"dry_run": true}' --cli-binary-format raw-in-base64-out /dev/stdout` shows what it would do.
- It is watched: alarms fire if it errors or has not run for 15 minutes (a missing datapoint is breaching).

### Do not run recursive greps on the jumpbox

Its two volumes are gp3 capped at **125 MB/s**. On 2026-07-25 a
`grep -rl <pattern> /home/ubuntu /tmp` pinned both at exactly 125.19 MB/s for twenty
minutes, starving writes to zero -- journald stopped mid-heartbeat, and SSH accepted the
TCP connection then hung because PAM and lastlog never got I/O. It needed a reboot.

Scope searches to the directory you actually need, prefer `rg` over `grep -r`, and push
genuinely large scans to fcvm-metal-arm (64 vCPU) instead.

### Jumpbox Storage

The jumpbox has separate root and home volumes:
- **Root volume**: 40GB (`/dev/nvme0n1`) - OS, packages, boot
- **Home volume**: 40GB (`/dev/nvme1n1`) mounted at `/home/ubuntu` - user data, projects
- **Swap**: 4GB at `/home/ubuntu/.swapfile` (on home volume to save root space)

The home volume is backed up daily/weekly via AWS Backup.

### ARM Dev Server Storage (fcvm-metal-arm)

The ARM dev server (r8gd.metal-24xl) has a persistent 800GB EBS root and three ephemeral local
NVMe disks. `/home/ubuntu`, including `~/.codex`, lives on that backed-up root; there is
no separate ARM home volume. `nvme-btrfs.service` positively identifies instance-store
devices and creates a Btrfs RAID0 across all of them at `/mnt/fcvm-btrfs`.

**IMPORTANT**: The NVMe drives are ephemeral - data is lost on stop/start (and when AWS reclaims the spot box).
A plain **reboot keeps them**: `nvme-btrfs-setup.sh` reuses a filesystem both disks already carry (same UUID, all devices
present) and formats only blank disks, so a reboot, the usual way back from a wedged box, loses nothing. (Until 2026-10-06
it ran `mkfs` on every boot and a reboot wiped them.) `earlyoom` kills a runaway process at 4% available memory before the
box wedges (the ARM box has no swap), and the system `dnsmasq` service is masked because it races `systemd-resolved` for
port 53 (fcvm does not use it). All three come from `metal_boot_hardening` and the NVMe script in `dev-user-data.tf`. Use for:
- VM images and caches (`/mnt/fcvm-btrfs/image-cache`)
- Build artifacts and temp files
- Firecracker VM storage

The service owns setup. Inspect it without modifying disks:

```bash
systemctl status nvme-btrfs.service --no-pager
findmnt /mnt/fcvm-btrfs
lsblk -o NAME,SIZE,TYPE,MODEL,FSTYPE,MOUNTPOINTS
```

Never run `mkfs` against a guessed `/dev/nvme*` name. “Not mounted” does not prove a
device is blank, and one disk may be a member of an existing array. Do not use loop-device
images (`/var/fcvm-btrfs.img`) either.

### SSH Access

```bash
# Get current IPs from terraform output
cd ~/aws && terraform output

# Or use the SSH commands directly
ssh -i ~/.ssh/fcvm-ec2 ubuntu@<jumpbox_public_ip>
ssh -i ~/.ssh/fcvm-ec2 ubuntu@<firecracker_dev_public_ip>
ssh -i ~/.ssh/fcvm-ec2 ubuntu@<x86_dev_public_ip>
```

### Shared Configuration

Common user_data scripts are in `dev-instance-common.tf`:
- `local.gh_auth_script` - GitHub CLI auth from Secrets Manager
- `local.claude_sync_script` - Claude Code Sync installation
- `local.gh_and_claude_sync_script` - Combined script

### GitHub PAT in Secrets Manager

GitHub authentication for private repos is stored in AWS Secrets Manager:
- **Secret name**: `github-pat-ejc3`
- **Region**: us-west-1
- **Used by**: claude-code-sync to clone private history repo

This is not the only GitHub PAT. Each is scoped to one job and they are not
interchangeable: `/github-runner/pat` (SSM) registers runners, and
`github-webhook-admin-pat` (Secrets Manager) is webhook-write only and exists solely for
the `integrations/github` provider. See `GITHUB-RUNNERS.md` for the full inventory.

Instances fetch the token during user_data bootstrap:
```bash
GH_TOKEN=$(aws secretsmanager get-secret-value \
  --secret-id github-pat-ejc3 \
  --region us-west-1 \
  --query SecretString \
  --output text)
```

### AI services on the dev boxes

Available if a task needs them; neither needs a key on disk.

- **DeepSeek (opencode), metal boxes only.** `opencode` on fcvm-metal-arm/x86 runs DeepSeek on
  Amazon Bedrock (`deepseek.v3.2`, us-west-2; `deepseek.r1-v1:0` also granted) through the
  instance role. The managed `~/.zshrc` wraps `opencode` so it names the `[default]` profile when
  nothing else supplies credentials: opencode does not use an instance role on its own, and
  asked for a key. The pinned release is `local.opencode_version` in `dev-user-data.tf`. Not on
  nextjs-dev: DeepSeek and the other Anthropic models stay on the metal boxes.
- **Claude Haiku 5.5 on Bedrock, on every box** (`bedrock-haiku.tf`). The kids' box and the other
  non-metal boxes had no Bedrock until the owner, 2026-10-10: "I am fine if this box has haiku access. All
  boxes can be." `nextjs-dev-role`, `dev-ebs-only-role` (I/O and parallel boxes), `wbox-role`,
  `claude-master-server-role` and, when the Mac is on, `mac-dev-instance` may invoke Haiku 5.5 and no other
  model; the metal boxes keep every `anthropic.*` model. The GPU boxes have no instance profile and get
  nothing. A new box role joins through `local.bedrock_haiku_box_roles`.
  Call it on bedrock-runtime, region `us-west-2`, model `us.anthropic.claude-haiku-5-5` (the US
  inference profile; Haiku 5.5 has no in-Region endpoint and is not on bedrock-mantle outside GovCloud, so
  the Mantle URL Claude Code uses on the metal boxes does not serve it). Credentials come from the
  instance role; there is no key.
- **Haiku 5.5 for dolphin-labs' news pass** (`bedrock-haiku.tf`). Role `dolphin-labs-news-bedrock`,
  assumed through GitHub OIDC by `refresh-news.yml` on `main` of `dolphin-labs-hq/dolphin-labs` and no
  other workflow, branch or pull request (the trust pins GitHub's immutable `sub` and `job_workflow_ref`).
  Same grant as the boxes. Output `dolphin_labs_news_bedrock` holds the region, role ARN and model id the
  workflow needs.
- **ElevenLabs** (voice and sound for the games). The key is in Secrets Manager,
  `games/elevenlabs-api-key` (us-west-1), readable by dev-server-role and nextjs-dev-role only
  (`dev-ai-services.tf`). Fetch it into the environment when needed, never onto a command line
  or into a file or a commit:
  `export ELEVENLABS_API_KEY=$(aws secretsmanager get-secret-value --region us-west-1 --secret-id games/elevenlabs-api-key --query SecretString --output text)`.
  The deployed games get it as the Vercel env `ELEVENLABS_API_KEY` (server-side only).
- **Browserbase** (hosted headless browsers, for pages that block plain requests), metal boxes and
  nextjs-dev. One JSON secret, `browserbase/credentials` (us-west-1), holds `BROWSERBASE_API_KEY` and
  `BROWSERBASE_PROJECT_ID`; only dev-server-role, nextjs-dev-role and admins may read it
  (`dev-ai-services.tf`). Export both with `eval "$(aws secretsmanager get-secret-value --region us-west-1
  --secret-id browserbase/credentials --query SecretString --output text | jq -r 'to_entries[] |
  "export \(.key)=\(.value|@sh)"')"`. Never onto a command line, into a file in a repo or a commit.
- **claude-master backup API key**, metal boxes only. `claude-master/backup-api-key` (us-west-1) holds
  the paid Anthropic API key that claude-master uses only after every subscription profile is out of
  quota (`dev-ai-services.tf`). Readable by dev-server-role and admins, not nextjs-dev-role (a box-wide
  grant on a box where every account has sudo). Export it as `CLAUDE_MASTER_BACKUP_API_KEY` from
  `get-secret-value`; never into a file in a repo, a command line or a commit.
- **Turso API token**, metal boxes only. `turso/api-token` (us-west-1) holds the Turso platform API token (it can create
  and delete databases) (`dev-ai-services.tf`). Readable by dev-server-role and admins, not nextjs-dev-role (a box-wide grant
  on a box where every account has sudo). Export it as `TURSO_API_TOKEN` from `get-secret-value`; never into a file in a
  repo, a command line or a commit.
- **AWS list prices**, on the metal boxes and nextjs-dev: the Price List API (`aws pricing
  get-products --region us-east-1 --service-code AmazonEC2 ...`), public prices only. Account
  spend (Cost Explorer, billing) is not granted (`dev-ai-services.tf`).

### Colton Games accounts credentials

`colton-games-accounts.tf` holds what the colton-games site needs for Google sign-in, site admins and browser
push as four JSON secrets in us-west-1, split by environment so that production and non-production share
nothing. `README.md` ("Colton Games accounts") has the owner's steps.

- `colton-games/nonprod/auth` (`AUTH_GOOGLE_ID`, `AUTH_GOOGLE_SECRET`) and
  `colton-games/nonprod/push` (`NEXT_PUBLIC_VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT`): the dev
  boxes and Vercel previews. Readable by dev-server-role, nextjs-dev-role and admins. Export one without
  printing it:
  `eval "$(aws secretsmanager get-secret-value --region us-west-1 --secret-id colton-games/nonprod/auth
  --query SecretString --output text | jq -r 'to_entries[] | "export \(.key)=\(.value|@sh)"')"`.
  Never onto a command line, into a file in a repo or a commit. `AUTH_SECRET` is not in it: a dev box makes
  its own (`openssl rand -base64 33`).
- `colton-games/prod/auth` and `colton-games/prod/push`: production. Admins only; a dev box gets
  `AccessDeniedException`, on purpose.
- The site admins' addresses are the `colton_games_site_admins` key (lists `prod` and `nonprod`) of the
  `people/addresses` secret (`people.tf`), read by every jumpbox that plans, so no machine needs a local file. They
  live there, in Terraform state and in Vercel (`SITE_ADMIN_EMAILS`): never in a repository, and not in a
  colton-games secret's JSON. Never write one into a
  file, a test, a commit or a pull request, in this repository or the games one; use an address at
  `example.com`. Do not read `terraform.tfvars` to learn them. A dev box that wants site admins locally
  sets `SITE_ADMIN_EMAILS` in its own environment.
- The rules for those lists are preconditions on an output, not variable `validation` blocks, on purpose:
  a failed validation prints the `terraform.tfvars` lines that set the variable, addresses included. Do
  not move them back.
- Terraform writes the Vercel variables (Production from `prod/*`, Preview from `nonprod/*`) once
  `colton_games_accounts_ready` names a secret. Do not set these names by hand in the Vercel project: Vercel
  refuses a second variable with the same key on a target, and the next apply would fail.
- `ACCOUNT_SAVES=on` switches account saves on. It is not a secret and comes from no container: Terraform
  writes it, not sensitive, to the targets `colton_games_account_saves_targets` names (default: production), and
  the same rule applies: never set it by hand in Vercel. The site reads exactly `on`, so do not write
  `true` or `1`. Sign-in for that target comes first, then the games repository's saves migration
  (`games-mp-migrate` applies it when it merges to `main`; nobody runs SQL by hand), then the target. A
  dev box sets `ACCOUNT_SAVES=on` in its own environment.

### dolphin-films credentials

`dolphin-films.tf` holds the credentials of dolphin-films (`dolphin-labs-hq/dolphin-films`: a Next.js site
on Vercel, Google sign-in, a Turso store) as four JSON secrets in us-west-1, split by environment so
that production and non-production share nothing:

- `dolphin-films/nonprod/auth` (`AUTH_SECRET`, `AUTH_GOOGLE_ID`, `AUTH_GOOGLE_SECRET`) and
  `dolphin-films/nonprod/turso` (`TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`): local development and Vercel
  previews. Readable by dev-server-role (the metal boxes) and admins; not nextjs-dev-role. Export one
  without printing it:
  `eval "$(aws secretsmanager get-secret-value --region us-west-1 --secret-id dolphin-films/nonprod/auth
  --query SecretString --output text | jq -r 'to_entries[] | "export \(.key)=\(.value|@sh)"')"`.
  Never onto a command line, into a file in a repo or a commit.
- `dolphin-films/prod/auth` and `dolphin-films/prod/turso`: production. Admins only; a dev box gets
  `AccessDeniedException`, on purpose. Terraform writes the Vercel project's Production environment from them
  (`dolphin-films-vercel.tf`); never set one of those names by hand in Vercel, or the next apply fails.
- Turso has two kinds of credential. `turso/api-token` (above) is the account's platform token: it makes and
  deletes databases and mints their tokens, and the site never gets it. `TURSO_DATABASE_URL` and
  `TURSO_AUTH_TOKEN` are one database's address and a token for that database alone; those are the site's.
  One database per environment, made once with the platform token from a metal box. When you mint a pair,
  write it to a 0600 JSON file for an administrator to put and never print it: a dev box cannot write a
  container. The site applies its own migrations when it deploys.
- `FILMS_SEED_EMAILS` and `FILMS_ADMIN_EMAILS`, the lists of addresses the site reads, are the `dolphin_films`
  key of `people/addresses` (`people.tf`); Terraform writes them with their environment's auth secret. Never
  write an address into a file, a test, a commit or a pull request here.
- `dolphin_films_vercel_ready` (`dolphin-films-vercel.tf`) names the containers Terraform reads. It is a
  committed default, never a `-var`: a plan without the flag would propose deleting every variable.
- The asset builders run on the metal boxes with what the role already has: `browserbase/credentials`,
  `games/elevenlabs-api-key` (above) and Claude on Amazon Bedrock through the instance role
  (`BedrockRuntimeInvoke` in `dev-instance-common.tf`). There is no LLM API key to fetch.
- Setting a value (admins): a 0600 JSON file and `put-secret-value`, as in `README.md`.
- State: once `dolphin_films_vercel_ready` names a container, Terraform reads it and writes its values into
  Vercel, so those values, the address lists and the dolphin-labs token's use are in Terraform state, as
  colton-games' accounts are (`colton-games-accounts.tf`). State is readable by administration only, the same
  boundary as the containers. Terraform is pinned to 1.10.3, so write-only arguments (1.11) are not
  available. While the gate is empty nothing is read and nothing reaches state.
- CI runs on the app runners under the `dolphin` label with its own cap (`runner-app.tf`).

### Sites also deploying to Cloudflare Workers (staging copies)

The owner's Vercel sites (dolphin-labs, dolphin-films, imagine, remote-claw, colton-games, nest-step) also deploy a second copy
to Cloudflare Workers through OpenNext, from each repo's own GitHub Actions.

**Standing rule (owner, 2026-10-10): every web app supports both clouds, and secrets reach both.** It covers the apps
deployed as sites (Vercel projects and `<site>-stage` Workers), not host-local tools such as `browser-manager/` (bound to
loopback on its host, reached through its own tunnel). Each app repository's agent instructions carry the same section
("Two clouds: Vercel and Cloudflare"): a change is done only when both deploys are green; no platform-only code without a
path on the other; every secret lives in AWS Secrets Manager and must reach both platforms. Where it lives today:
Cloudflare from `workers-stage/<site>` (loaded by `scripts/workers-stage-secrets.sh`); Vercel from the containers Terraform
writes into the project for colton-games (`colton-games-accounts.tf`) and dolphin-films (`dolphin-films-vercel.tf`). For
the other sites the Vercel environment is still set in Vercel by hand and `vercel-env/<site>/<target>` is a record captured
from it afterwards (`scripts/vercel-env-capture.py`), not a source: a change there is made in Vercel and re-recorded, and in
`workers-stage/<site>`. Moving each of them to a Terraform-written Vercel environment (the dolphin-films shape) is what makes
AWS the one source. A new app joins both clouds before it ships. Two apps do not meet the rule yet: dolphin-maps
(`dolphin-labs-hq/dolphin-maps`, Vercel only, no `<site>-stage` Worker) and claude-master-dashboard (Cloudflare only, no
Vercel project). Bringing each to the other cloud follows the order below for Cloudflare, or a Vercel project with its
environment written by Terraform. Production stays on Vercel; the Worker is a
**staging** copy named `<site>-stage`, behind Cloudflare Access, running with the site's NON-PRODUCTION credentials.
(`ts-api` already deploys to Cloudflare from its own workflow and its own Cloudflare account; it is not part of this.)

- **One deploy credential, a typed Terraform resource.** `workers-deploy.tf` owns the container `cloudflare-workers-deploy-token`
  (administration read only) and `cloudflare_account_token.workers_deploy`, minted through the `token_minter` provider alias that
  reads `cloudflare-account-token` ephemerally; Terraform writes its value into the container (value in state, as the Workers
  Builds deploy token's is; dev boxes cannot read state). Permissions: Workers Scripts Write and Account Settings Read on the one
  account. There is deliberately no mint script (issue #16: no curl or local-exec for Cloudflare resources). Rotate with
  `terraform apply -replace=cloudflare_account_token.workers_deploy`, then `scripts/workers-deploy-secret.sh OWNER/REPO [account]`
  for each site repo (as `colton` on nextjs-dev for the CoderColton repos, through that account's own gh login; nothing is copied).
- **The account token is pinned to the jumpboxes' addresses, IPv6 included.** `cloudflare-account-token` allows only
  `52.9.31.202/32`, `13.56.106.229/32` and the two jumpboxes' IPv6 `/128`s. Terraform's HTTP client prefers IPv6, so an IPv4-only
  pin fails every plan with `9109 Cannot use the access token from location: 2600:...`. If a jumpbox is replaced or gains a new
  address, add it to that token's `request_ip` list (one PUT with the token's own definition) before planning there.
- **This is a deploy-capable credential in GitHub**, chosen by the owner on 2026-10-06 over Workers Builds (a browser
  authorization per repository owner; colton-games' version of that path is still gated off). Its reach is Workers scripts in
  one account: no DNS, Access or tunnels. Workflows must deploy only on `push` to `main` or `workflow_dispatch`, never on
  `pull_request_target` or a fork's pull request.
- **Terraform owns the envelope, Wrangler owns the code.** `workers-stage.tf` holds the one Worker-native Access application
  (a `worker` destination per Worker, the family allowlist and the service-token policy, as colton-games-stage in
  `cloudflare.tf`) and, through `cloudflare_workers_script_subdomain`, each Worker's `workers.dev` and preview switches. It
  deliberately does not manage the whole Worker: `cloudflare_worker` sends the adopted object back on every update with Cloudflare's
  own observability defaults and the API refuses it ("propagation_policy requires the trace propagation feature"). Note
  `cloudflare_worker.colton_games_stage` in `cloudflare.tf` is adopted the same way and has never been updated, so expect that error
  there when its URL gate is first turned on. The
  rule from #16 holds here: Cloudflare infrastructure is created by `terraform apply` with the typed provider, never by `curl`,
  `local-exec` or the dashboard. (The deploy token is the one documented exception: a credential, minted by script as the
  registrar token is.)
- **The Workers' runtime secrets are kept in AWS, not in Cloudflare.** A Worker's secrets are write-only (nothing can read one
  back), so `workers-stage-secrets.tf` owns one administration-only container per site, `workers-stage/<site>` (JSON of variable
  name to value), and `scripts/workers-stage-secrets.sh SITE` loads it into `<site>-stage` through `wrangler secret bulk`
  (stdin, names printed only; it refuses an empty value or a bracketed placeholder). Change a value by editing the container and
  re-running the script, after the site's own deploy has finished (a secret change made while a deploy uploads can be lost when
  it activates). Take values from the real source, never from Vercel: a variable of type `sensitive` is never returned by any
  Vercel API, and `vercel env pull` writes the text `[SENSITIVE]` in its place. Use the site's non-production values where it
  has them (its own OAuth client, the non-production database) and fresh random ones where a value only has to be unguessable.
- **AWS holds a record of every site's Vercel environment, too** (`vercel-env-secrets.tf`): `vercel-env/<site>/<target>`, one
  administration-only container per project per target (JSON name to value), so Vercel is not the only holder of a site's
  credentials. Vercel never returns a `sensitive` variable after it is saved, so `scripts/vercel-env-capture.py SITE TARGET` reads
  them from inside a deployment: a throwaway route deployed as a staged production build (`--skip-domain`) or a preview, behind
  Deployment Protection, answering only a one-time token, deleted by its `dpl_` id afterwards. It requests the URL without
  credentials first and goes no further unless Vercel answers 401, and it needs the project's `ssoProtection` set. A target with no
  sensitive variable is read with `vercel env pull` instead. Never run `vercel remove <project name>`: it deletes every
  deployment of the project. A project with no automation-bypass secret (imagine) gets a temporary one for the run, passed to curl on
  stdin and revoked and verified gone afterwards; an existing secret is never touched. `--skip-domain` keeps the custom domains off the throwaway but Vercel still moves the project's
  default `*.vercel.app` aliases to it (found on dolphin-labs 2026-10-07, which left that alias dangling for a few minutes), so the tool
  records the aliases first and sets them back after the delete. Re-run the tool for a site when its Vercel environment changes.
- **Order for a new Worker, because Access protects a Worker by its immutable id and the id exists only after the first
  deploy:** (1) the site's first deploy with `workers_dev` and `preview_urls` false in its `wrangler.jsonc` creates the Worker
  with no public URL; (2) add its name and id to `local.workers_stage` with `urls = false` and apply (adopted, covered by
  Access, still unreachable); (3) set `urls = true` and, in the repo, `workers_dev`/`preview_urls` true. Never reverse 2 and 3.

### Claude Code Sync

All dev instances have [claude-code-sync](https://github.com/ejc3/claude-code-sync) installed:
- Syncs Claude Code conversation history to GitHub
- Config: `~/.claude-code-sync-init.toml`
- Repo: `~/claude-history-sync`
- Remote: `https://github.com/ejc3/claude-code-history.git`
- **No cron.** The every-5-minutes sync job was retired: it failed on every run (the tool asks for a
  TTY) and alerted `cost-alerts` each time, and the private history repo has had no push since
  2026-07-27. Nothing syncs history automatically now; the setup script removes the job if a box
  still has it.

To sync manually:
```bash
claude-code-sync push   # Push local history to GitHub
claude-code-sync pull   # Pull history from GitHub
claude-code-sync        # Bidirectional sync (default)
```

### Stable addressing

The ARM, x86, and Next.js dev instances have Elastic IPs for static addressing:
- IPs persist across stop/start cycles
- Defined in each instance's .tf file

`io-box` has no EIP and accepts SSH/NFS only from private fleet networks: SSH from both peered
VPCs, NFS only from `local.io_box_nfs_client_cidrs` (the dev fleet subnets and the parallel
boxes' us-west-2d subnet), never a whole VPC, because the games router and engines share the
us-west-1 VPC. Keep games subnets out of `local.dev_fleet_subnets` and off the peer route. It
receives a transient public IPv4 while running for outbound package access, but clients use
fixed private IP `172.31.48.10` across the inter-region VPC peer.

### Auto-Stop Lambdas

An hourly Lambda in `dev-auto-stop-lambda.tf` applies an intended 12-hour CPU-idle policy
to the two metal servers.

**How it works:**
- Queries five-minute CloudWatch CPU maximums over the preceding 12-hour range
- Any returned CPU point at or above 5% keeps the instance running
- Only counts metrics since instance `LaunchTime` (prevents false positives after restart)
- Sends SNS notification on stop (or if stop fails)
- Currently requires only 12 returned CPU datapoints, not the roughly 144 in a complete
  series. Treat it as a cost heuristic: missing periods can count as idle

**Why Lambda instead of CloudWatch alarms:**
- CloudWatch EC2 stop actions don't work reliably with spot instances
- Lambda can check LaunchTime to avoid stale metric issues
- More control over logic (peak CPU vs average)

**Configuration:**
- `CPU_THRESHOLD` (default 5) - a five-minute window at or above this CPU % keeps the box alive. wbox sets 15: idle Windows with
  its DCV agent measured 6.4% average and 11.7% peak, so at 5 it ran 4+ days (about $20/day) before this existed
- `IDLE_HOURS = 12` - Hours of idle before auto-stop
- `INSTANCE_IDS` - Comma-separated list of instances to monitor
- `SNS_TOPIC_ARN` - For notifications

The I/O-box invocation of the same Lambda also checks five-minute disk and network sums.
Returned disk activity of at least 1 MiB or network activity of at least 64 MiB keeps it
running, but a missing I/O series currently counts as zero. The parallel box uses
`parallel-box-watchdog.tf` and terminates after 30 minutes below 5% CPU. `nextjs-dev` and
the jumpboxes never idle-stop.

### Backup boundary

AWS Backup selects the ARM and x86 roots, the Next.js root, the original jumpbox home
volume, and the `jumpbox-2` root. It does not cover instance-store NVMe, `io-box` scratch,
the I/O-box root, the Mac root, or the parallel box's persistent `/mnt/work` volume.
`prevent_destroy` on `/mnt/work` prevents a Terraform deletion; it does not provide
versioned or offsite recovery.

### Persistent Root Volumes (Spot Instances)

The two metal dev instances use **spot instances** for cost savings, with persistent EBS
root volumes to preserve data.

**The Challenge**: Spot instances can be terminated by AWS at any time. When terraform recreates the instance, it creates a NEW root volume from the AMI, orphaning the old volume with user data.

**Our Approach**:
1. Use spot with `persistent` type + `stop` interruption behavior
2. Set `delete_on_termination = false` on root volume
3. **Manual one-time volume swap** when instance is recreated

The procedure below is destructive break-glass recovery, not the normal wake path. It
stops an instance, detaches/attaches roots, deletes the disposable replacement root, and
reassociates an address. Obtain explicit user authorization, resolve every current ID with
read-only checks, and compare it with Terraform state immediately before running it. Never
use it merely because a persistent Spot instance is stopped.

**CRITICAL: Spot Instance Restart Timing**

When you stop a persistent spot instance, AWS transitions the spot request state:
- `active` / `fulfilled` → `disabled` / `instance-stopped-by-user`

**There is a DELAY in this transition!** If you try to `start-instances` before the transition completes, you get:
```
IncorrectSpotRequestState: You can't start the Spot Instance because the associated Spot Instance request is not in an appropriate state
```

**Solution**: Wait for the spot request to show `disabled` state before starting:
```bash
# Check spot request state
aws ec2 describe-spot-instance-requests --region us-west-1 \
  --query "SpotInstanceRequests[?InstanceId=='$INSTANCE_ID'].{State:State,Status:Status.Code}" \
  --output table

# Wait until it shows: State=disabled, Status=instance-stopped-by-user
# Then start-instances will work
```

**Moving fcvm-metal-arm between AZs** is terraform-native, with no manual swap: stop the box,
set `firecracker_move_from_instance_id` to its instance ID and `firecracker_availability_zone`
to the target in `firecracker-dev.tf`, merge, and apply from a fresh worktree. Terraform images
the stopped disk, builds the new box from that image before destroying the old one, moves the
Elastic IP, and keeps the SSH host keys. The target AZ needs a subnet in
`local.subnet_ids_by_az` (`main.tf`). The old root volume is left as a rollback copy. Once the
new box checks out and a completed backup holds the disk as of the move (the new root volume's
first daily backup, or the old volume's if it ran after the stop), set
`firecracker_move_from_instance_id` back to `""` and apply, which deletes the move image. While
it is set, any later replacement boots from that dated image and rolls the disk back to the day
of the move.

**Volume Swap Procedure** (x86 only, after terraform creates new instance):
```bash
INSTANCE_ID="i-xxx"  # New instance ID from terraform output
PERSISTENT_VOL="vol-071f114b67441e776"  # x86 dev server

# 1. Stop the new instance
aws ec2 stop-instances --instance-ids $INSTANCE_ID --region us-west-1
aws ec2 wait instance-stopped --instance-ids $INSTANCE_ID --region us-west-1

# 2. WAIT for spot request state transition (critical!)
echo "Waiting for spot request to transition to disabled state..."
while true; do
  STATE=$(aws ec2 describe-spot-instance-requests --region us-west-1 \
    --query "SpotInstanceRequests[?InstanceId=='$INSTANCE_ID'].State" --output text)
  echo "Spot request state: $STATE"
  if [ "$STATE" = "disabled" ]; then break; fi
  sleep 5
done

# 3. Swap volumes
CURRENT_VOL=$(aws ec2 describe-instances --instance-ids $INSTANCE_ID --region us-west-1 \
  --query 'Reservations[0].Instances[0].BlockDeviceMappings[?DeviceName==`/dev/sda1`].Ebs.VolumeId' \
  --output text)
echo "Swapping $CURRENT_VOL -> $PERSISTENT_VOL"

aws ec2 detach-volume --volume-id $CURRENT_VOL --region us-west-1
sleep 10
aws ec2 attach-volume --volume-id $PERSISTENT_VOL --instance-id $INSTANCE_ID \
  --device /dev/sda1 --region us-west-1
sleep 5

# 4. Start instance (now it will work!)
aws ec2 start-instances --instance-ids $INSTANCE_ID --region us-west-1

# 5. Cleanup temp volume
aws ec2 delete-volume --volume-id $CURRENT_VOL --region us-west-1

# 6. Re-associate EIP (terraform loses the association on recreate)
# ARM: eipalloc-034a515771765d101, x86: eipalloc-0173c9b5e3d294cc5
EIP_ALLOC="eipalloc-0173c9b5e3d294cc5"  # x86
aws ec2 associate-address --instance-id $INSTANCE_ID --allocation-id $EIP_ALLOC --region us-west-1

# 7. Wait for instance and clear old SSH host key
aws ec2 wait instance-running --instance-ids $INSTANCE_ID --region us-west-1
IP="50.18.109.164"  # x86 EIP
ssh-keygen -R $IP
ssh -i ~/.ssh/fcvm-ec2 -o StrictHostKeyChecking=accept-new ubuntu@$IP "hostname; uptime"
echo "Done!"
```

**Persistent Volume IDs** (don't delete these!):
- ARM (fcvm-metal-arm): root volume is built by terraform and changes on every move
  (`terraform state show 'aws_instance.firecracker_dev[0]'`); EIP: `184.72.40.255` (`eipalloc-034a515771765d101`)
- x86 (fcvm-metal-x86): `vol-071f114b67441e776`, EIP: `50.18.109.164` (`eipalloc-0173c9b5e3d294cc5`)

**When to run the manual swap** (x86): After `terraform apply` creates a new instance (you'll see a new instance ID in the output). Check if data is missing, then run the swap.

## Common Tasks

**Add a new Terraform variable**:
1. Add to `variables.tf` with sensible default
2. Update `README.md` if an operator must supply or understand it
3. Never commit a real value from the ignored `terraform.tfvars`

**Change authentication**:
Keep personal Codex, Claude, GitHub, and Vercel login interactive and per Unix user. The
exact first-session sequence is in `README.md`.

**Add alternative regions**:
Do not add a speculative option. Region placement is intentional: the main fleet is in
`us-west-1`, the I/O/parallel/CodeArtifact resources are in `us-west-2`, and recovery
copies use `us-east-1`.

**Add deployment options**:
Prefer the deployed opinionated path. There is no application database and no Aurora
auto-pause; DynamoDB is limited to locking and runner registration claims.

## Philosophy in Action

User says: "I want options for..."
First determine whether the live platform needs a new capability. If it does, model one
clear supported path and document it.

User says: "Can I mutate it with the AWS CLI?"
Answer: Managed resources change through reviewed Terraform. Use read-only CLI inspection,
or a narrowly documented runtime/recovery exception.

User starts a Codex session from the app:
The generated `~/Documents/Codex/AGENTS.md` points the agent to the real repositories and
machine constraints; the scratch session directory is never treated as the project.

The goal is low ambiguity, reproducible infrastructure, and enough context for an agent or
new operator to act safely without tribal knowledge.
