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
M=$(aws secretsmanager get-secret-value --secret-id cloudflare-account-token \
      --region us-west-1 --query SecretString --output text)
# Registrar Domains Admin = 136d0be1ddc64eaf8516fa6994abfad4
curl -4 -X POST -H "Authorization: Bearer $M" -H 'Content-Type: application/json' \
  -d '{"name":"registrar-agent","policies":[{"effect":"allow",
       "resources":{"com.cloudflare.api.account.<ACCOUNT_ID>":"*"},
       "permission_groups":[{"id":"136d0be1ddc64eaf8516fa6994abfad4"}]}]}' \
  https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/tokens
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
  box. `parallel-box-watchdog.tf` terminates burst compute after 30m CPU idle
- `nextjs-dev` and both jumpboxes are deliberately excluded from idle stop
- There is no application database. DynamoDB is limited to Terraform locking and runner
  registration claims. Older notes about Aurora Serverless auto-pause no longer apply

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

### Common Pitfalls

**Credential ownership**: Personal Codex, Claude, GitHub, and Vercel device logins belong
to one Unix user. Never seed them from Terraform, copy them between users, or overwrite a
working personal login with a bootstrap token.

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
retired with Deny `*`; only the owner-approved AMI publisher uses GitHub OIDC.

**Optional Mac is off and its timestamp is stale**: never set `enable_mac_dev=true` without
also supplying a new `mac_teardown_at` at least 24 hours after allocation and reviewing
Dedicated Host quota/capacity. Teardown deletes the Mac root.

## Development Instances

The long-lived development and administration instances are:

| Instance | Type | Purchase | Purpose | Terraform |
|----------|------|----------|---------|-----------|
| jumpbox | t4g.large (2 vCPU / 8GB) | on-demand | Remote management, admin AWS access | jumpbox.tf |
| jumpbox-2 | t4g.micro (2 vCPU / 1GB) | on-demand | Independent recovery admin host | jumpbox2.tf |
| fcvm-metal-arm | c7gd.metal (64 vCPU) | spot | Firecracker/KVM on ARM64 | firecracker-dev.tf |
| fcvm-metal-x86 | c5d.metal | spot | Firecracker/KVM on x86 | x86-dev.tf |
| nextjs-dev | t4g.medium (2 vCPU / 4GB) | **on-demand** | Kids' Next.js games behind Cloudflare Access | nextjs-dev.tf |
| io-box | i8ge.large | persistent spot | Private ephemeral NFS scratch | io-box.tf |

**nextjs-dev is deliberately on-demand.** It ran as spot until 2026-07-25, when it was
reclaimed six times in one day and then could not restart at all -- the spot request
reported `capacity-not-available` and the kids' URLs were simply down with no ETA. Spot
placement score was 3/10 in every US region and for every alternative instance type, so
neither moving region nor changing family was a way out. Sized down large -> medium so the
durable option costs about what the unreliable one did (~$29/mo vs ~$24/mo spot).

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
uses t-claude's default path-derived sessions. It is guarded by a verified pinned
t-claude implementation and live `claude auth status`, so credentials remain personal and
Terraform never seeds or copies them.

All metal repositories run as `ubuntu` and therefore share one tmux server. Keep one
aggregate systemd service and one cgroup; do not create per-repository units. Stopping or
restarting the aggregate can kill every managed and interactive t-claude session in that
shared server. The Next.js box is different: Colton and Connor are separate Unix users,
so their `claude-rc@` units own separate tmux servers.

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
before assuming the cause is unknowable.

The boxes are also configured to make a hang legible rather than silent: `panic_on_oops`
turns an oops into a reboot (the cmdline carries `panic=-1`) instead of an indefinite
hang, hung-task detection logs D-state pileups, sysrq is available on the console, and
journald is persistent so the last pre-death log survives the reboot.

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

The ARM dev server (c7gd.metal) has a persistent 400GB EBS root and two ephemeral local
NVMe disks. `/home/ubuntu`, including `~/.codex`, lives on that backed-up root; there is
no separate ARM home volume. `nvme-btrfs.service` positively identifies instance-store
devices and creates a Btrfs RAID0 across all of them at `/mnt/fcvm-btrfs`.

**IMPORTANT**: The NVMe drives are ephemeral - data is lost on stop/start. Use for:
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

### Claude Code Sync

All dev instances have [claude-code-sync](https://github.com/ejc3/claude-code-sync) installed:
- Syncs Claude Code conversation history to GitHub
- Config: `~/.claude-code-sync-init.toml`
- Repo: `~/claude-history-sync`
- Remote: `https://github.com/ejc3/claude-code-history.git`

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

`io-box` has no EIP and accepts SSH/NFS only from private fleet networks. It receives a
transient public IPv4 while running for outbound package access, but clients use fixed
private IP `172.31.48.10` across the inter-region VPC peer.

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

**Volume Swap Procedure** (after terraform creates new instance):
```bash
INSTANCE_ID="i-xxx"  # New instance ID from terraform output
PERSISTENT_VOL="vol-09e5c3cee32bb67dc"  # ARM dev server

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
EIP_ALLOC="eipalloc-034a515771765d101"  # ARM
aws ec2 associate-address --instance-id $INSTANCE_ID --allocation-id $EIP_ALLOC --region us-west-1

# 7. Wait for instance and clear old SSH host key
aws ec2 wait instance-running --instance-ids $INSTANCE_ID --region us-west-1
IP="184.72.40.255"  # ARM EIP
ssh-keygen -R $IP
ssh -i ~/.ssh/fcvm-ec2 -o StrictHostKeyChecking=accept-new ubuntu@$IP "hostname; uptime"
echo "Done!"
```

**Persistent Volume IDs** (don't delete these!):
- ARM (fcvm-metal-arm): `vol-09e5c3cee32bb67dc`, EIP: `184.72.40.255` (`eipalloc-034a515771765d101`)
- x86 (fcvm-metal-x86): `vol-071f114b67441e776`, EIP: `50.18.109.164` (`eipalloc-0173c9b5e3d294cc5`)

**When to run the manual swap**: After `terraform apply` creates a new instance (you'll see a new instance ID in the output). Check if data is missing, then run the swap.

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
