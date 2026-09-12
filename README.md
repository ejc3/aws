# AWS development infrastructure

This is the live Terraform control plane for the `ejc3` development fleet. It is not a
generic module or a tutorial environment: the defaults describe the deployed system.

It manages roughly 195 resources across the main AWS account, an isolated staging account,
Cloudflare, and several regions. The platform provides:

- persistent ARM64 and x86 bare-metal Firecracker development servers;
- two independent Terraform administration jumpboxes;
- authenticated, permanent URLs for the kids' Next.js projects;
- ephemeral high-speed shared storage and on-demand 96/192-core compute;
- autoscaled ARM64 and x86 GitHub Actions runners;
- backups, cross-region/cross-account recovery, and cost alerts;
- credential-free Terraform validation in GitHub, with live plans confined to admin hosts;
- scoped IAM capabilities for agents, including temporary EBS volumes and Bedrock access.

## Start here

The supported control plane is the original jumpbox, with `jumpbox-2` as the independent
fallback. Terraform runs directly there. There is no container wrapper and no Makefile.

The operating rules are:

1. Make permanent AWS and Cloudflare changes in Terraform. Do not mutate managed
   infrastructure with the AWS CLI.
2. Run `terraform plan` immediately before `terraform apply` and read the plan. A plan has
   previously caught a change that would have exposed every kids' URL publicly.
3. Commit and push every applied change. Git, Terraform state, and live infrastructure
   should describe the same system.
4. Do not run concurrent plans or applies. State uses an S3 backend with DynamoDB locking,
   but a fresh plan is still required after another operator changes anything.
5. Treat every instance-store NVMe disk as disposable. Persistent data belongs on EBS or
   in a backed-up repository.

Normal operation from an admin box:

```bash
cd ~/aws
git pull --ff-only
terraform init
terraform fmt -recursive
terraform validate
terraform providers
terraform plan
terraform apply
git add <changed-files>
git commit -m "<what changed>"
git push origin main
```

The jumpboxes already have Terraform, Git, the AWS CLI, this repository, and an EC2
administrator instance role. There is no AWS login or credential-refresh step. The
committed `.terraform.lock.hcl` keeps provider resolution identical across admin hosts;
review and commit any intentional provider upgrade.

`main.tf` enforces Terraform `1.10.3`, which is the version on both jumpboxes and supports
the ephemeral Workers Builds control-token read. Any Terraform upgrade must update the
constraint, both jumpboxes, and the Terraform validation workflow together.

Cloudflare is intentionally pinned to the signed `ejc3/cloudflare` `5.24.0`
provider release. That fork adds typed Worker-native Access destinations and the Workers
Builds APIs used below while preserving ordinary `terraform init` on both ARM64 jumpboxes
and amd64 CI; there is no local provider override or machine-specific installation step.
Return to `cloudflare/cloudflare` only after an upstream release includes the same fields
and regression coverage.

The fork has a different provider source address. On the first live-jumpbox update to the
fork, stop after `terraform providers` if state still lists `cloudflare/cloudflare`.
Under the exclusive state lock, migrate all existing Cloudflare resources before
planning:

```bash
terraform state replace-provider \
  registry.terraform.io/cloudflare/cloudflare \
  registry.terraform.io/ejc3/cloudflare
```

Terraform writes a mandatory state backup. Do not repeat the command once state lists
`ejc3/cloudflare`, and never apply a plan made before the migration. The reverse command
is required when the stack eventually returns to the upstream provider namespace.

## Prerequisites

### Existing deployment

To operate the existing system, a new person or agent needs:

- SSH access to either jumpbox as `ubuntu`;
- the private `fcvm-ec2` SSH key;
- access to the `ejc3/aws` GitHub repository;
- a clean, current `main` checkout at `~/aws`;
- exclusive use of the Terraform state lock for the duration of plan/apply.

Run `terraform output` for the current addresses and ready-to-use SSH commands. The
`fcvm-ec2` private key is backed up in Secrets Manager for jumpbox disaster recovery; AWS
itself stores only the public half of an EC2 key pair.

### Replacement admin host and empty-state recovery

This repository is the control plane for one existing account, not a portable new-account
installer. A replacement admin host should use the existing S3 state. It additionally
needs:

- Terraform 1.10.3 to match both jumpboxes, plus Git and AWS CLI v2;
- AWS administrator credentials in the Organizations management account;
- access to the existing S3 backend bucket `ejc3-terraform-state` and DynamoDB lock table
  `ejc3-terraform-locks`;
- the existing `fcvm-ec2` EC2 key pair and its private key;
- the existing `AWSBackupDefaultServiceRole`;
- the Cloudflare `cc-games.dev` zone and Secrets Manager values
  `cloudflare-tunnel-token` (including Workers Scripts Read/Write),
  `cloudflare-tunnel-credentials`, and
  `cloudflare-google-idp`;
- the raw user-owned Cloudflare token in
  `cloudflare-workers-builds-control-token` when the checked-in Workers Builds rollout
  gate is enabled. From
  [Create Additional Tokens](https://dash.cloudflare.com/profile/api-tokens), choose
  **Use template**, retain User → API Tokens → Edit, and add Workers Builds Configuration
  Edit plus Workers Scripts Read for account `12ea67fb7ced068de03f35c22688e436`;
  an account-owned token is rejected;
- the Cloudflare Workers and Pages GitHub App authorized for only
  `CoderColton/colton-games` by that repository's owner, starting from the target Worker's
  Settings → Builds → Connect → GitHub flow. `ejc3` has write, not admin, and cannot grant
  the App;
- the `github-pat-ejc3` Secrets Manager value, the runner-only GitHub PAT, and
  `github-webhook-admin-pat` in Secrets Manager -- a fine-grained PAT whose only
  repository permission is `Webhooks: Read and write` on `ejc3/fcvm`, used by the
  `integrations/github` provider to own the runner webhook;
- current ARM64 and x86 runner AMIs tagged `Purpose=github-runner`;
- the Google OAuth callback
  `https://ejc3.cloudflareaccess.com/cdn-cgi/access/callback` when Google login is enabled.

Clone and initialize the configuration:

```bash
git clone https://github.com/ejc3/aws.git ~/aws
cd ~/aws
terraform init
```

Follow the normal provider-migration gate above before planning from a replacement host.

`terraform plan` should then refresh the recovered remote state and propose no unexplained
re-creation. Stop if the backend appears empty. The configuration contains imported
resources, fixed account identities, and protected volumes; applying it against an empty
state or a different account is not a supported bootstrap and can collide with or replace
live infrastructure. Recover the backend state first, or inventory and import every
pre-existing resource before any full apply.

The alert address, runner PAT, and SSH-key backup use Terraform-managed containers whose
payloads are intentionally kept out of Terraform state. For a true cold start, create
those three containers only after state/import reconciliation is complete:

```bash
terraform apply \
  -target=aws_ssm_parameter.alert_email \
  -target='aws_ssm_parameter.github_runner_pat[0]' \
  -target=aws_secretsmanager_secret.fcvm_ec2_ssh_key
```

Then populate `/alerts/email`, `/github-runner/pat`, and `fcvm-ec2-ssh-key` through the AWS
console or AWS CLI without printing their values. This is a one-time secret-payload
bootstrap, not a parallel way to manage infrastructure. The alert sender address must also
be verified in SES in `us-west-1`.

Do not create the Workers Builds control-token container in this generic bootstrap. Its
first creation is deliberately deferred until after the 15-minute S3 versioning wait in
the staged rollout below; that Terraform write is the backend `VersionId` probe.

Terraform owns the `ejc3/fcvm` `workflow_job` webhook and generates its HMAC secret, so
there is nothing to mirror by hand. **Before the first full apply**, if the recovered
state does not already contain the hook, adopt it -- otherwise the apply adds a *second*
hook delivering to the same URL and every event arrives twice:

```bash
terraform import 'github_repository_webhook.runner[0]' fcvm/589197362
```

Only then, with state, prerequisites, secret payloads, and the hook import reconciled,
run and review the full plan:

```bash
terraform plan
terraform apply
```

Then confirm the SNS email subscription. Do not leave placeholder secret/parameter values
in a live account. Optional Mac development needs available EC2 Dedicated Host quota and an
explicit new `mac_teardown_at` at least 24 hours after allocation; its checked-in date is
historical and must not be reused. Terraform creates its VNC password.

## Fleet

| Resource | Region / purchase | Persistent state | Purpose and lifecycle |
|---|---|---|---|
| `jumpbox` | `us-west-1`, on-demand `t4g.large` | 40 GB protected `/home/ubuntu` EBS; separate 40 GB root | Primary admin host. Runs Terraform with an administrator instance role and stays up. |
| `jumpbox-2` | `us-west-1`, on-demand `t4g.micro` | Protected 20 GB encrypted root | Fully Terraform-bootstrapable backup admin host with the same IAM reach. |
| `fcvm-metal-arm` | `us-west-1`, persistent Spot `c7gd.metal` | 400 GB backed-up EBS root, including `/home/ubuntu`; local NVMe is ephemeral | 64-vCPU ARM64 Firecracker/KVM and nested-virtualization work. Uses the 12-hour idle-stop policy. |
| `fcvm-metal-x86` | `us-west-1`, persistent Spot `c5d.metal` | 300 GB EBS root; 3.6 TB local NVMe is ephemeral | x86 Firecracker/KVM work. Uses the 12-hour idle-stop policy. |
| `nextjs-dev` | `us-west-1`, on-demand `t4g.large` | 200 GB encrypted EBS root (including Dolphin's `/home/ejc3`), protected by AWS Backup | Always-on shared development box. Deliberately not Spot and not idle-stopped. |
| `io-box` | `us-west-2d`, persistent Spot `i8ge.large` | 20 GB EBS root; 1.25 TB shared NVMe is ephemeral | Private NFS bulk scratch at `/mnt/io`. Uses a 12-hour multi-metric idle policy and returns with an empty scratch disk after every stop. |
| `parallel-box`, `parallel-box-2` | `us-west-2d`, one-time Spot, normally 96 or 192 vCPU | Protected 100 GB EBS each at `/mnt/work`; roots are disposable | Temporary fan-out compute, two independent boxes so two jobs can run at once. Each terminates after 30 idle minutes; `pbox` recreates them. |
| GitHub runners | `us-west-1`, one-time Spot metal | Disposable | Webhook-launched ARM64/x86 runners. Four healthy runners per architecture maximum; idle, expired, and wedged runners terminate. Maximum instance lifetime 13h30m (drains from 12h). |
| Mac dev | `us-west-2`, optional Dedicated Host | Disposable 200 GB gp3 root | Temporary macOS build host. Disabled by default; teardown terminates the instance and releases the host after its 24-hour minimum. |

The primary VPC is `10.0.0.0/16` in `us-west-1`. A private inter-region VPC peer reaches
the `172.31.0.0/16` default VPC in `us-west-2`; NFS is never exposed publicly.

```text
GitHub workflow jobs -> API Gateway -> Lambda -> disposable Spot runners

Cloudflare Access -> outbound tunnel -> nextjs-dev -> 127.0.0.1 project ports

us-west-1 dev VPC <-> private VPC peer <-> us-west-2 io-box + parallel-box
        |
        +-> AWS Backup -> us-east-1 DR vault + dev-staging account vault
```

## Access and trust boundaries

List current connection commands:

```bash
cd ~/aws
terraform output
```

The `ubuntu` account on ARM, x86, and `nextjs-dev` has stable aliases:

```bash
ssh fcvm-arm
ssh fcvm-x86
ssh nextjs
ssh io
```

The aliases use Elastic IPs for the three long-lived `us-west-1` boxes and fixed private
address `172.31.48.10` for `io-box`. Dev servers carry a dedicated dev-hop key, not the
`fcvm-ec2` admin key, so ordinary dev-to-dev access does not grant an admin shell on a
jumpbox. Nothing on a dev server can reach a jumpbox at all -- there is no key, and no
delegation of any kind. `pbox` launches the parallel boxes itself with a tag-scoped IAM
grant (`parallel-box-launch.tf`); the forced-command key it used to carry is gone.

The metal, Next.js, and temporary-compute roles use a shared SSM connectivity policy
without account-wide Parameter Store reads. Application parameter access is explicit;
the metal role retains its intended runner SSH key access, but cannot send commands to
AMI builders. Runner debug output uses that SSH path: the metal role explicitly denies
account-wide Run Command input/output listing and reads, which AWS cannot scope to a
runner. Its IPv6 assignment permissions cover only the two Terraform-managed metal ENIs.
The unused predecessor `aws-infrastructure-dev-instance-role` retains its role/profile
for history but has a protected Terraform-managed denial of all AWS actions.
Next.js can read only its two Cloudflare connector credentials and the
dev-only hop key through its setup policy, not the Cloudflare control-plane API token.
The separate Cloudflare Access service-token workflow is unchanged. Runner credential
retirement is a separate, canary-gated rollout; this dev-host change does not complete it.

`security-iam-passwords.tf` sets both accounts' IAM-user console-password minimum to
14 characters, requires uppercase/lowercase/number/symbol, and prevents reuse of the last
24 passwords. It adds no periodic expiry or account-wide password-change permission.
The checked-in import adopts main's existing singleton policy; recovery receives its
first custom policy. At the September 12 inventory, neither account had an enabled IAM
user console password, so this is a future-login guardrail. It does not create passwords,
change root/SSO credentials or MFA, or enable centralized root management. Those controls
are independent; [IAM password policies](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_passwords_account-policy.html)
do not govern root or [Identity Center authentication](https://docs.aws.amazon.com/singlesignon/latest/userguide/password-requirements.html).

SSH and Eternal Terminal provide interactive access. `t-claude` supplies Claude's
phone-oriented remote-control workflow; Codex uses its own app-server remote-control
daemon. Conversation history is synchronized through `claude-code-sync`. On each metal
box, `fcvm-claude-rc.service` starts remote-control sessions at boot for the user's
active repositories under `/home/ubuntu`. “Active” means t-claude was used for that
repository on this host during the preceding 30 days, its local Git checkout has moved
during that period, or its worktree has uncommitted changes. Only `github.com/ejc3/*`
checkouts qualify. The launcher uses the normal path-derived `t-claude` session so a later
interactive launch attaches to the same work when run from that repository root.

## Start a Codex session

Codex is installed per Unix account on the metal boxes, `jumpbox-2`, and `nextjs-dev`.
Authentication is deliberately not stored in Terraform. On a fresh account, SSH in as the
person who will own the sessions and complete the one-time logins. That person needs
Codex access in their ChatGPT account/workspace, the current ChatGPT app with Remote
available, and device-code login enabled by either their account or workspace admin.

```bash
codex --version
codex login --device-auth
codex login status

gh auth login
gh auth setup-git
```

Device authentication prints a URL and one-time code for a browser or phone. Use the same
ChatGPT account and workspace that will open the remote session. Device-code login may
first need to be enabled in the account's ChatGPT security settings or by its workspace
administrator. `~/.codex/auth.json` is a persistent credential: never copy it between
users, print it, or commit it. See the official
[Codex authentication guide](https://learn.chatgpt.com/docs/auth).

GitHub authentication is separate from Codex. The metal boxes have a limited PAT fallback
for a brand-new bootstrap, but each person should run `gh auth login` for their own
identity. On `nextjs-dev`, log in as `colton`, `connor`, `ejc3`, or `skevh` rather than sharing
credentials, then also run:

```bash
vercel login
claude login
```

Claude login is also personal and persistent on each metal box. After the first login,
start the already-enabled boot service once if the machine is currently running:

```bash
claude login
claude auth status
sudo systemctl start fcvm-claude-rc.service
systemctl status fcvm-claude-rc.service --no-pager
```

The service checks `claude auth status` on every boot and skips cleanly if the login is
missing or expired. To seed a new repository, open it once with
`t-claude --remote-control`; a host-local marker makes subsequent boots include it while
it remains recent. All metal repositories share one tmux server and one aggregate service,
so do not restart or stop this service just to refresh one repository: that can end every
managed and interactive `t-claude` session on the host. Check the most recent aggregate
result with `cat ~/.local/state/fcvm-claude/last-start`.

Refresh the generated instructions that seed app-created Codex sessions:

```bash
# ubuntu on a metal box or jumpbox-2
sudo codex-agents-refresh

# a personal account on nextjs-dev
sudo kid-agents-refresh "$USER"
```

The first command inventories the repositories under `/home/ubuntu`. The second points a
kids' session at that user's published project and explains that its `ndev` service is
already running. Both write `~/Documents/Codex/AGENTS.md`.

Enable remote control only after `codex login` succeeds:

```bash
sudo systemctl enable --now "codex-rc@$USER.service"
systemctl status "codex-rc@$USER.service" --no-pager
codex remote-control start --json
```

Open Remote in ChatGPT while signed in to that same account and workspace. If it asks for
a manual pairing code, generate one with:

```bash
codex remote-control pair
```

The command creates a short-lived code; see the official
[remote-control CLI reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli#cli-codex-remote-control).
Remote control is currently marked experimental upstream.

A phone-created task starts in
`~/Documents/Codex/<date>/<task>/`, which is an empty scratch directory, not a checkout.
The generated parent `AGENTS.md` tells Codex where the real repositories are. Name the
repository in the first prompt; if no repository is named, the agent should ask instead
of editing the scratch directory. For a terminal session that should start in this
repository directly:

```bash
cd ~/aws
codex
```

The ChatGPT client sends the approval and sandbox mode for each remote thread, and those
settings override `~/.codex/config.toml`. Enable the Remote setting called dangerous mode
only when full host access is intended. These EC2 development machines are designed as
the external isolation boundary; the generated config supplies their real project roots
when the app selects workspace-write mode.

Once enabled, `codex-rc@` returns on every boot. Machine convergence also enables it when
it sees an existing login. `nextjs-dev` refreshes the seed files, updates
Codex/Claude/Vercel, and restarts enabled agent services nightly. Codex, GitHub, Vercel,
and Claude remain four independent logins.

## Firecracker development

The two metal boxes are the main `fcvm` workstations:

- ARM64 uses `c7gd.metal`; x86 uses `c5d.metal`.
- Both are persistent Spot instances with `stop` interruption behavior and static EIPs.
- Their EBS roots survive a normal stop/start: 400 GB on ARM and 300 GB on x86.
- Instance-store NVMe is for VM images, build output, caches, and temporary Btrfs data.
  It is erased by a stop, Spot interruption, or host loss.
- ARM nested virtualization requires the custom fcvm kernel and patch set documented in
  `AGENTS.md`; `uname -r` reports `<version>-fcvm-<build-sha>` on a correctly installed
  host.
- An hourly Lambda queries a 12-hour range of five-minute peak CPU points. Any returned
  point at or above 5% keeps the instance running; otherwise it stops the instance and
  sends an SNS notification.

Terraform manages the configuration of a stopped metal box, but an `aws_instance` resource
does not itself wake an already-stopped instance. Unlike `io-box`, ARM and x86 currently
have no `aws_ec2_instance_state` wake resource. A normal plan/apply therefore will not wake
them. Do not hide a mutating AWS CLI call in a script: either obtain explicit authorization
for the break-glass start procedure in `AGENTS.md`, or add and review a Terraform-modeled
lifecycle first. Persistent Spot can start only after its request reaches
`disabled / instance-stopped-by-user`.

The idle check is a cost-saving heuristic, not proof of 12 uninterrupted idle hours. The
current Lambda accepts 12 returned CPU datapoints even though a complete 12-hour,
five-minute series contains about 144; missing metric periods can therefore count as idle.

## Shared high-speed I/O

`io-box` exports its local 1.25 TB NVMe disk as private NFSv4.2. ARM, x86, Next.js, and
new parallel boxes receive a bounded soft automount:

```text
/mnt/io -> 172.31.48.10:/  (the server's `/srv/io` export)
```

Use it for bulk scratch, caches, and reproducible artifacts. Do not put the only copy of
source or results there: every stop creates a new empty filesystem. Clients idle-unmount
after ten minutes so a stopped server does not block boot or hang a shell indefinitely.

The I/O watchdog queries CPU, disk bytes, and network bytes over the same intended 12-hour
window. A returned five-minute point with CPU at least 5%, disk I/O at least 1 MiB, or
network I/O at least 64 MiB keeps it up. Missing I/O series currently count as zero, so
this is also a heuristic rather than a continuity guarantee. Wake it through the modeled
Terraform state resource:

```bash
terraform apply -target=aws_ec2_instance_state.io_box
```

A normal full `terraform apply` also reconciles that resource to `running`; the target is
just the narrow wake command. The root EBS volume survives the stop; `/mnt/io` data does
not.

## On-demand parallel compute

Two independent boxes (`parallel-box`, `parallel-box-2`), each with its own persistent
100 GB work volume, so two jobs can run at once. From ARM or x86:

```bash
pbox status      # both boxes
pbox up          # launch box 1 from its launch template, then connect
pbox up 2        # launch box 2 (independent volume and lifecycle)
pbox ssh [2]
pbox down [2]    # terminate compute; keep that box's /mnt/work
```

The launcher tries several 96/192-vCPU Graviton Spot pools in the availability zone that
contains the persistent work volumes. One shared watchdog checks every five minutes and
terminates either box after 30 minutes below 5% CPU.

Use only `pbox` (or `scripts/parallel-box.sh`, which is the same script) for this
lifecycle. Terraform owns the durable half -- work volumes, security group, key pair and
the two launch templates -- while the instances themselves are deliberately not in state.
That removes the old hazard entirely: an unrelated full apply can no longer propose
terminating a box in the middle of a live job.

## Temporary EBS for agents

Permanent fleet infrastructure remains Terraform-managed. As a deliberate runtime
exception, development boxes have a narrow IAM policy for temporary working volumes:

- region must be `us-west-1` or `us-west-2`;
- volume type must be encrypted `gp3`;
- creation must include the tag `DevEBS=true`;
- attach/detach is allowed only between tagged volumes and tagged dev instances;
- deletion is allowed only for tagged volumes.

EBS volumes are availability-zone scoped. An agent must create the volume in the same AZ
as its target instance, mount it safely by volume ID rather than a guessed NVMe name, and
delete it when no longer needed. This permission does not make `/mnt/io` persistent.

## Kids' Next.js environment

`nextjs-dev` hosts separate Unix, GitHub, Vercel, Claude, and Codex identities for Colton
and Connor. It is intentionally on-demand after repeated Spot reclamations made the URLs
unavailable.

Inside a project:

```bash
ndev
```

`ndev` derives a stable port, registers the hostname, and enables a systemd service.
`cloudflared` dials out to Cloudflare, so there are no inbound web ports and `next dev`
remains bound to `127.0.0.1`.

**The zone follows the account, not a flag.** `colton` and `connor` publish to
`https://<name>.cc-games.dev`; `ejc3` and `skevh` publish to `https://<name>.dolphin-labs.dev`.
`/usr/local/bin/ndev-zone` is the single source of truth for user -> zone -> tunnel, and
`ndev-register` refuses a hostname outside the caller's zone -- it runs as root through
sudoers, so that check is a privilege boundary, not a convenience.

Each zone has its own tunnel, its own credentials, its own registry, and its own
`cloudflared@<zone>.service`, so a bad ingress rewrite on one project cannot take the other
project's URLs down. Access gates both: `cc-games.dev` through Google or one-time PIN
against an email allowlist, `dolphin-labs.dev` through GitHub against membership of the
`dolphin-labs-hq` organization. A separate service token supports non-interactive checks.

Once credentials exist and their units have been enabled, services and remote-control
agents start at boot. A reboot restores the published URLs without another interactive
login.

### Dolphin SSH through Cloudflare

`ssh.dolphin-labs.dev` reaches this host's existing SSH daemon on `127.0.0.1:22`
through the Dolphin tunnel. The existing wildcard DNS and GitHub Access application
protect it: first sign in as a member of `dolphin-labs-hq`, then authenticate to SSH
with your existing key. This does not grant any new Unix accounts or change direct
public SSH/ET access. For the owner's Dolphin checkout, use the `ejc3` account.

On a Mac or Linux client with `cloudflared` installed:

```bash
ssh -o 'ProxyCommand=cloudflared access ssh --hostname %h' \
  ejc3@ssh.dolphin-labs.dev
```

Add `-i /path/to/your/private-key` if the key is not already in your SSH agent/config.
Cloudflare opens a browser for GitHub sign-in when your Access session needs renewal.
The server's connector credential is never needed on the client. Plain SSH to this
hostname without the proxy command will not work. Phone SSH apps that cannot run
`cloudflared` need a separately configured supported network/client path; this route
does not configure WARP or an iOS VPN. See the [Cloudflare SSH client guide](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/use-cases/ssh/ssh-cloudflared-authentication/).

`ndev-rebuild` always retains this static Dolphin-only SSH route, and `ndev-register`
reserves its hostname so a project cannot replace it. Other project routes and the
kids' tunnel are unchanged. Local cloudflared ingress requires a connector restart,
not SIGHUP; only a zone whose config changes is restarted, briefly disconnecting its
existing tunnel sessions. An unchanged rebuild does not restart either connector.

### Colton Games Cloudflare staging and previews

The `colton-games-stage` Worker is deployed from `CoderColton/colton-games` with Wrangler
and OpenNext. Terraform owns the Worker envelope and its Worker-native Access boundary;
Workers Builds owns versions, application assets, bindings, and deployments. This prevents
an infrastructure apply from replacing an application bundle.

The existing Worker is adopted through the checked-in import block with both `workers.dev`
and preview URLs still disabled, matching its current remote state. Read the plan and stop if
it proposes creating, replacing, or deleting the Worker or changing its deployed code.

The checked-in Worker-native Access application uses a `worker` destination with no domain.
That single application follows every request routed to this Worker, including its
`workers.dev` hostname once enabled, immutable and branch previews, future Custom Domains,
and future zone routes. It reuses the family email allowlist and the non-interactive Access
service-token policy. The URL switches intentionally remain off until the application has
been applied and a second clean plan confirms the policy attachment. Do not use hostname
wildcard applications or a generic REST/local-exec bridge as a substitute.

The pinned fork manages the account's `cc-games.workers.dev` label, a dedicated deployment
API token, the GitHub repository connection, separate staging and preview triggers, and
their complete build-time environment maps. The ordinary `cloudflare-tunnel-token` is
account-owned and cannot call Workers Builds. The aliased provider instead reads the raw
user-owned control token ephemerally from `cloudflare-workers-builds-control-token`, so
its value is not persisted in Terraform state; do not reuse, copy, or replace the ordinary
Cloudflare credential.

Open [Create Additional Tokens](https://dash.cloudflare.com/profile/api-tokens) and choose
**Use template** for that template, not the Custom Token builder: API Tokens Edit is
unavailable in the Custom Token permission list. Keep the template's User → API Tokens →
Edit permission (called API Tokens Write by the API), then add account-level Workers
Builds Configuration Edit and Workers Scripts Read scoped only to account
`12ea67fb7ced068de03f35c22688e436`. Those permissions let Terraform configure Builds and
mint the narrower Workers Scripts Write token that build jobs receive. Store the raw
control-token string in the existing Secrets Manager container without JSON wrapping.
Terraform creates the deploy token; do not make or paste a second deploy credential.

The other one-time prerequisite starts in Cloudflare: Workers & Pages →
`colton-games-stage` → Settings → Builds → Connect → GitHub. The `CoderColton` repository
owner must then authorize the Cloudflare Workers and Pages GitHub App for **only**
`CoderColton/colton-games`; `ejc3` has write access, not administration access, and cannot
make the grant. The
[GitHub App page](https://github.com/apps/cloudflare-workers-and-pages) is a reference,
not the primary setup entry point because a bare install can miss the Cloudflare account
connection context. When authorization returns to Cloudflare, **stop**: do not select,
save, or connect the repository and do not create build settings in the dashboard.
Terraform owns those objects. It cannot perform the interactive GitHub grant, and the
repository-connection API has no read/list endpoint or import path, so an out-of-band
connection cannot be adopted safely. The managed resource is protected from destroy and
its last confirmed UUID exists only in versioned, encrypted Terraform state.

The managed builds are:

| Deployment | Branches | Build command | Deploy command |
|---|---|---|---|
| Production staging | `main` | `npm run cf:build` | `npm run cf:deploy:built` |
| Pull-request preview | all except `main` | `npm run cf:build` | `npm run cf:upload:built` |

Use `/` as the root directory and Node 24 from the application's `.nvmrc`. Preview builds
must remain limited to trusted branches because they inherit the staging Worker's bindings
and secrets. `stage.colton-games.com` and the production Vercel serving path remain outside
this change until the domain is deliberately moved to Cloudflare.

Roll this out in order; do not collapse the safety gates into one apply:

1. After signed provider `5.24.0` is visible in the Terraform Registry, update only its
   stale lock selection, then initialize without allowing any further lock changes:

   ```bash
   terraform providers lock \
     -platform=linux_arm64 -platform=linux_amd64 \
     registry.terraform.io/ejc3/cloudflare
   git diff -- .terraform.lock.hcl
   terraform init -lockfile=readonly
   terraform validate
   ```

   The diff must change only the `ejc3/cloudflare` block to signed `5.24.0` hashes for
   both platforms. Commit that lock update. Do **not** use broad `terraform init -upgrade`;
   it can advance unrelated providers allowed by their `~>` constraints.
2. Leave both `colton_games_workers_builds_enabled` and
   `colton_games_worker_urls_enabled` false. Save and review a targeted backend-versioning
   plan, then apply that exact plan:

   ```bash
   terraform plan \
     -target=aws_s3_bucket_versioning.terraform_state \
     -out=/tmp/colton-games-backend-versioning.tfplan
   terraform show -no-color /tmp/colton-games-backend-versioning.tfplan
   terraform apply /tmp/colton-games-backend-versioning.tfplan
   aws s3api get-bucket-versioning --bucket ejc3-terraform-state \
     --region us-west-1 --query Status --output text
   ```

   Require the saved plan to contain only the versioning resource, require `Enabled` after
   applying it, then wait **at least 15 minutes** for S3 versioning to propagate.
3. After that wait, apply only the control-token container. This legitimate Terraform
   state write is also the versioning probe. Again, save and review the targeted plan,
   then apply that exact plan:

   ```bash
   terraform plan \
     -target=aws_secretsmanager_secret.cloudflare_workers_builds_control_token \
     -out=/tmp/colton-games-control-token-container.tfplan
   terraform show -no-color /tmp/colton-games-control-token-container.tfplan
   terraform apply /tmp/colton-games-control-token-container.tfplan
   aws s3api head-object --bucket ejc3-terraform-state \
     --key aws-infrastructure/terraform.tfstate --region us-west-1 \
     --query VersionId --output text
   ```

   Require the saved plan to contain only the empty secret container. The backend object's
   `VersionId` must then be non-empty and not `null` or `None`. Stop if it is not: do not
   create a deployment token or repository connection without a verified versioned state
   write.
4. Keep both gates false and return to a full saved plan:

   ```bash
   terraform plan -out=/tmp/colton-games-base.tfplan
   terraform apply /tmp/colton-games-base.tfplan
   terraform plan
   ```

   The saved plan should contain only the account Workers subdomain and any other already
   reviewed base configuration from this change. It must not create Builds credentials,
   connections, triggers, or environment maps; update the Worker; change Access; or
   destroy anything. Apply the saved plan immediately and require the follow-up plan to
   be empty.
5. Create and store the raw control token, then have the `CoderColton` repository owner
   complete the repository-only GitHub App authorization.
6. Change only `colton_games_workers_builds_enabled` to true. Re-plan immediately. This
   stage may create only the narrow deploy API token, its Builds registration, and the
   repository connection. It must not create either trigger or environment map, update
   the Worker, or change Access. Apply, then require an empty follow-up plan.
7. Change only `colton_games_worker_urls_enabled` to true. The plan should contain one
   in-place Worker update plus the two triggers and their two environment maps. The
   trigger dependency guarantees the already-protected URL surfaces are enabled before a
   GitHub event can deploy. Apply, immediately run a fresh plan, and require it to be
   empty. Only then verify an unauthenticated request is denied and the existing Access
   service token succeeds.
8. While pull request `CoderColton/colton-games#26` is still open, cause a `synchronize`
   event with a new commit and verify both its protected immutable preview and branch
   alias. A preview cannot exist while `previews_enabled` is false, so do not perform this
   check before step 7 or after merging the pull request.
9. Only after that preview succeeds, merge pull request #26 to `main`. Verify the
   resulting staging build and protected `workers.dev` deployment.

Every gate change is a reviewed, committed Terraform change. Do not disable either gate
after its protected resources exist; `prevent_destroy` is intended to stop that rollback.

## GitHub Actions and package infrastructure

The `workflow_job` webhook launches one-time Spot runners from prebuilt ARM64 or x86 AMIs.
Labels select the architecture; the launcher tries several metal families when capacity is
scarce, moving a family that just failed for capacity to the back of that order. Each new
instance carries `RunnerRegistrationProtocol=ddb-v1`. After GitHub configuration, bootstrap
validates the exact identity in `.runner`, conditionally records `State=registered` under its
instance ARN in DynamoDB, and starts the service only after that identity is confirmed.

Cleanup runs every five minutes. A registered `ddb-v1` runner is checked by its exact
GitHub runner id, and one with no row is reaped after its lease through a conditional
`State=reaping` claim on that row, so a bootstrap arriving at the same moment finds the row
taken and does not start the service. A missing row beside evidence that the runner did
register (GitHub still lists it, a `RunnerSeenAt` stamp, or a renewed lease) is a lost row,
and the instance is held to the age ceiling rather than reaped. Instances launched before the tag
existed keep the roster-based rules: a renewed lease or a `RunnerSeenAt` stamp holds them
on roster absence, and a readable roster that has never listed one expires its lease.
Cleanup also reaps stalled launches, terminates jobs running for more than three hours,
and counts queued jobs to retry scale-up.

A runner instance lives at most **13 hours 30 minutes**, checked on the five-minute poll,
so the observed maximum is that plus one interval. Past 12 hours it drains:
GitHub-idle means terminate now, and a job already in flight is left to finish. 13h30m is
the hard ceiling, computed from the instance's launch time alone and applied whatever the
runner is doing and whatever GitHub says, so a wedged host cannot outlive it. A ceiling
termination that never saw the runner idle logs `HARD-CEILING KILL`; that job will appear
on GitHub as "the self-hosted runner lost communication with the server", which is
otherwise indistinguishable from an AWS Spot reclaim.

The EC2 hard-ceiling pass completes before cleanup reads the PAT or calls GitHub, so GitHub
latency cannot defer the bound.

Terraform owns both halves of that webhook: it generates the HMAC secret, sets it on the
`ejc3/fcvm` hook through the `integrations/github` provider, and passes the same value to
the Lambda. There is no operator-held copy to fall out of sync -- which it did, silently
and completely, until 2026-08-07. That ownership takes effect at the one-time hook import
and the first apply after it (see Prerequisites); a state that has not run them yet still
has the pre-Terraform secret live on the hook.

The maximum is four runners per architecture, counted as runners GitHub can actually hand a
job to rather than instances that merely exist, so a wedged box cannot hold a slot. Each
scale-up decision emits a `GitHubRunners` metric and a structured log line. Two alarms
watch them: `runner-scale-up-starved` fires when queued work goes unserved for two hours
(a wedged pool, or one genuinely that far behind — a single poll cannot tell them apart,
because a host that wedges mid-job keeps reporting `busy` to GitHub), and
`runner-zero-online` fires within ten minutes when jobs are queued and nothing is online
or booting to take them.
Runner roots and instance-store data are disposable. See `GITHUB-RUNNERS.md` for the
webhook, AMI, lease, health, and cleanup internals.

Runner-image publication is a separate privilege from running CI. The replacement
`github-actions-ami-builder` role trusts only the `ejc3/fcvm` environment
`runner-ami-publish`. That environment permits only the `main` branch, requires
approval from `ejc3`, and disables administrator bypass. Collaborators can still
launch ordinary CI; publishing a shared runner image requires the owner's explicit
approval of the exact commit. `ejc3` may approve their own dispatch. Do not approve a
publication without reviewing the workflow and all build inputs at that commit.

The environment settings are versioned in `.github/runner-ami-environment.json`.
They are applied with the operator's existing GitHub CLI login; Terraform's
webhook-only PAT cannot administer repository environments, and personal GitHub
credentials must not be copied into Terraform state or fleet secrets. To recover
this GitHub prerequisite, create/update the environment, then add its sole branch
policy only if the read-only policy listing does not already contain it:

```bash
gh api --method PUT repos/ejc3/fcvm/environments/runner-ami-publish \
  --input .github/runner-ami-environment.json
gh api repos/ejc3/fcvm/environments/runner-ami-publish/deployment-branch-policies
# Only when the main branch policy is absent:
gh api --method POST repos/ejc3/fcvm/environments/runner-ami-publish/deployment-branch-policies \
  -f name=main -f type=branch
```

Read back the environment and branch policies before creating/enabling the AWS role.
There must be no other allowed branch/tag, reviewer, or bypass. Credential migration
has separate deployment gates; merging source is not evidence that a gate is live:

1. Create the replacement builder role/profile and own-instance bootstrap grant.
2. Deploy the backward-compatible runner controller and expiry cleanup. Its
   `iam:PassRole` grant is independently restricted to the existing runner role and
   EC2, with explicit denies for other roles/services. Verify the deployed code and
   full IAM allow/deny cases before changing bootstrap. The controller-only stage
   keeps the PAT-reading document and its instance-role permissions intact.
3. Publish this source's instance-bound bootstrap only after controller acceptance, then verify
   a real trusted CI registration/job and drain instances booted with the old script.
   Non-secret IAM fixtures test authorization, not registration or job execution.
   New hosts register for one job, delete their bootstrap credential before service
   startup, and power off when the service ends; EC2 then terminates them. At this
   intermediate stage the old PAT grant and broad SSM attachment remained; the
   separately verified cutoff below removes them.
   The runner release and asset checksums are pinned and automatic updates disabled;
   review each release bump before GitHub's 30-day update deadline (immediately for
   required critical fixes), following the [runner update procedure](GITHUB-RUNNERS.md#instance-bound-single-job-bootstrap).
   A daily main-only `Runner Release Freshness` workflow fails when a newer stable
   version exists; it never upgrades or deploys automatically. Keep its scheduled
   runs and GitHub failure notifications enabled.
4. Retire runner PAT reads and the broad SSM attachment only after those tests. Narrow
   the remaining controller EC2 launch resources after all launched resources carry
   the required tags. The old CI authority is retired separately, only after the
   protected `fcvm` AMI workflow has adopted the replacement builder role/profile.

Keep each gate in a reviewed Terraform change with a fresh plan. The optional cleanup
scans only bootstrap metadata/tags after every hard-ceiling termination attempt and
deletes only positively identified, expired controller credentials. Its work is bounded;
repeated truncation or held/error records require investigation, not a claim that every
orphan has expired out of AWS. See [controller-first migration](GITHUB-RUNNERS.md#controller-first-credential-migration)
for the protocol and failure behavior. An additive apply alone does not close the old
PAT or CI escalation paths.

This source removes the September 12 temporary runner IAM fixtures. Apply cleanup only
after the before/after IAM checks and real post-cutoff trusted job pass, including
own-token deletion before job startup and automatic host termination. Require a fresh
full plan destroying only the two small canary hosts and their two non-credential
parameters, then verify both instances, their disposable roots, and parameters are gone.
No live runner, role/profile, Lambda, network, backup, or real credential is removed.
`RemoveAfter=2026-09-13` was a reminder, not automatic expiry; a merge does not stop billing.
See the [exact fixture gates and cleanup scope](GITHUB-RUNNERS.md#temporary-runner-credential-boundary-acceptance).
The checker and result tests remain for later reviewed acceptance windows.

The repository also manages:

- a private CodeArtifact npm repository in `us-west-2`;
- an owner-approved GitHub OIDC role for runner AMI builds;
- retired shared main/staging CI identities with explicit denial of all AWS actions;
- credential-free Terraform validation on pull requests, main pushes, a daily schedule,
  and manual dispatch.

The historical `drift.yml` now runs **Terraform Validation**: formatting, locked provider
installation with `-backend=false`, configuration validation, and CI boundary tests. It
does not request an OIDC token, assume an AWS role, or read state or secret payloads.
Full Terraform plans run only on an administration jumpbox. Even a read-only plan loads
credential-bearing state and configures privileged Cloudflare/GitHub providers; calling
that permission set read-only would not prevent administrator access through those secrets.
A green validation check is not evidence of zero live drift.

CI identity retirement was separate from runner bootstrap and controller hardening.
The September 12, 2026 IAM deployment (`2968911`, PRs #104 and #105) removed the
runner's reusable PAT access and broad SSM attachment, narrowed controller launch and
runner IPv6 permissions, closed metal-host account-wide command-output reads, and
retired the unused predecessor dev role with an explicit Deny-all. The exact rendered
policies passed AWS allow/deny simulations; real before/after EC2-role canaries proved
own-versus-other SSM and DynamoDB access, and the post-cutoff PAT simulation changed
from allowed to explicit Deny. Dev SSH/SSM remained healthy. The real post-cutoff
[trusted ARM64 job](https://github.com/ejc3/fcvm/actions/runs/34672446556/job/103600236701)
then passed at 18:43:15 UTC: the controller launched it under the narrowed policy,
its own role assigned IPv6 and deleted the one-use credential at 18:28:49 before
job startup at 18:28:52. The guest powered itself off; EC2 reported
`Client.InstanceInitiatedShutdown` and terminated it by 18:49:37, with its exact root
volume and ENI absent. This is actual post-cutoff acceptance, not only simulation.

## Bootstrap, authentication, and convergence

Large instance setup scripts are rendered by Terraform and versioned in S3. Metal boxes run
`dev-selfupdate.service` at boot: it compares the architecture script checksum and replays
the complete bootstrap only when the published script changed. Inspect:

```bash
cat /var/lib/dev-selfupdate/status
sudo less /var/log/dev-selfupdate.log
```

Do not casually run `dev-selfupdate.sh` to deploy one small edit; it reapplies the whole
machine configuration. Existing personal `gh auth login` state is preserved. The
`github-pat-ejc3` secret provides a fresh-box fallback and a separately scoped credential
for `claude-code-sync`; it must never overwrite a person's GitHub login.

Next.js users authenticate independently. Jumpboxes intentionally leave interactive
Claude/Codex login to the operator rather than storing those device credentials in
Terraform.

## Backups, safety, and cost controls

- AWS Backup covers the ARM and x86 roots, the Next.js root, the original jumpbox home
  volume, and the `jumpbox-2` root with daily, weekly, and monthly retention.
- The parallel box's persistent `/mnt/work` volume is protected from Terraform destroy but
  is **not backed up**. `prevent_destroy` is not a backup; keep important results elsewhere.
- The legacy primary vault keeps its governance Vault Lock. Existing primary and
  original `us-east-1` history retain their lifecycles; neither receives new scheduled
  fleet copies. The active recovery pipeline below replaces the old staging-copy
  path without replacing live disks or deleting that existing history.
- Daily cost reports, AWS Budget notifications, runner-count/age alarms, the
  `runner-scale-up-starved` alarm, EC2 spend alarms, and instance-status alarms publish
  through SNS/email.
- The original jumpbox's protected home volume and the fully reproducible `jumpbox-2`
  provide two administration recovery paths.
- Terraform state is encrypted in S3 and locked with DynamoDB.

The active backup pipeline keeps **one new long-term history**, in the recovery account's
`ejc3-backup-recovery` logically air-gapped vault in **us-east-1**, separate from the
source account and region. It is immediately compliance-locked and uses an AWS-owned
key. The already-created empty west1 air-gapped vault remains untouched and receives
no new copies. Empty vaults have no backup-storage charge.

`fleet-backup-recovery` copies only the five configured fleet volumes. Unencrypted
ARM, x86 and jumpbox-home snapshots go directly to the final vault. The aws/ebs-encrypted
Next.js and jumpbox-2 roots first copy to `ejc3-backup-dr-cmk` in the main account's
us-east-1 region, then across accounts within that region. Live disks are not migrated.
Final retention uses the original UTC backup date: the first of a month gets 365 days,
other Sundays 30 days, and other days 7 days. This does not depend on temporary-copy TTLs.

Daily backup plans write to the unlocked `ejc3-backup-processing` vault
with **no expiration while a copy is pending**. Only confirmed final copies permit
source cleanup. The controller keeps the latest CMK checkpoint for each encrypted root
until a verified successor exists: deleting every checkpoint would force expensive
full transfers again. Older checkpoints are removed only with complete copy lineage.
Recovery-point deletion is granted only by those two temporary-vault policies. AWS
Backup also forwards the underlying EC2 deletion using the controller's credentials;
that permission requires Backup in `aws:CalledVia`, our snapshot owner and pipeline
tags. Direct EC2 deletion remains explicitly denied. The two untagged bootstrap CMK
seeds were removed after verification; their exact-snapshot exceptions are disabled
by the completed cutover. The controller cannot
delete legacy history or final recovery points. Copy failures retain pending
data and raise alarms rather than silently expiring it. Stalled cleanup costs storage.

The September 8, 2026 cutover followed five successful detached-volume restore tests
(including validation and test-volume removal), verified final-copy lineage for all
five new captures, and actual removal of all seven disposable processing/bootstrap
snapshots. Both current CMK checkpoints, ten final points, and all 97 legacy points
and their lifecycles were preserved. The two fleet selections now use the daily
processing plans; the old plans remain for history but no longer select fleet volumes.
Both rollout gates stay true. Monthly restore tests remain scheduled for the eighth
at 12:00 UTC; the past one-off definitions are retained only for audit.

For a genuinely new deployment, cold bootstrap must start with both cleanup and
selection-cutover gates false. Do not reset the gates on this deployed pipeline. Bootstrap
leaves the existing daily/weekly/monthly plans and their selections unchanged; it seeds
from their latest recovery points and has no recovery-point deletion permission.
Roll out with fresh full Terraform plans in this order:

1. Apply the new destination/controller with both gates false. Require recent
   COMPLETED final points for all five exact source ARNs, both required CMK checkpoints
   encrypted with `alias/fleet-backup-dr`, and completed job-to-point lineage.
2. Set `backup_initial_capture_at` in `backup-restore-canary.tf` to a future UTC minute
   and apply the one-off processing capture. It backs up the same five volumes into
   the processing vault with no expiration and the required provenance tag, without
   moving either existing backup selection. Keep both cleanup and cutover gates false.
3. Set `backup_initial_restore_at` in `backup-restore-canary.tf` to a UTC minute at
   least 30 minutes in the future. Once step 1 has verified all five initial final
   copies, steps 2 and 3 may be applied together and run in parallel: restoring from
   the final vault does not depend on the new processing capture finishing. These
   are date/year crons, not recurring tests. The restore plan selects the latest
   completed point per volume, which may be a newer copy arriving during capture;
   record the actual recovery-point ARNs used by its jobs and verify their copy lineage.
4. Before enabling any recovery-point cleanup, require both independent checks:
   the five new processing captures have exact COMPLETED final copies and both CMK
   successor checkpoints; and all five initial restore jobs are COMPLETED, validation
   SUCCESSFUL, with test resources successfully deleted. Metadata verification does
   not prove guest data or boot integrity. Inspect copy/restore logs; do not infer
   success from Terraform apply.
   If a restore attempt fails, repair the evidenced cause, retain its job history,
   and set a new future `backup_initial_restore_at` for the same one-off plan.
   The controller checks a current-attempt job for every configured source volume.
   Rescheduling does not clear a preceding failure: the new job must complete,
   validate, and delete its test resource first. Missing or unfinished current jobs
   alarm after the one-hour start window plus one hour of propagation grace.
   Older jobs still receive validation/cleanup, and their failures remain in AWS
   job history and logs. Require all five jobs from the new attempt to pass.
5. Set only `backup_recovery_cleanup_enabled=true` and apply the two temporary-vault
   cleanup grants and controller update. Existing selections must remain unchanged.
   Verify the five processing captures and superseded CMK checkpoints actually disappear,
   not merely that deletion returned HTTP 200; keep each newest checkpoint. A permission
   failure must retain data, not lead to a broad identity-policy deletion grant.
   AWS can return HTTP 200 yet leave a point `EXPIRED` when its forwarded EC2 call
   fails authorization. Inspect the recovery point's status message and CloudTrail;
   after repair, the controller retries only expired points with the same complete
   final-copy lineage (and a strictly newer verified successor for CMK checkpoints).
   An expired processing retry also checks its provenance tag. Errors remain visible
   until the point disappears. Verify underlying snapshot absence as well.
6. Only after that cleanup acceptance succeeds, set `backup_recovery_cutover_enabled=true`
   and apply the two changed selections plus the controller's acceptance handoff.
   Their preconditions reject cutover while cleanup is disabled; replacement creates
   the new selection before removing the old one. This verified handoff retires the
   one-off health check so eventual AWS job-history expiration cannot create a false
   alarm; monthly per-volume restore checks and historical test-volume cleanup continue.
   Verify an EBS `copySnapshot` event reports incremental copying on a
   subsequent cycle before claiming measured incremental-transfer savings. Keep the
   past one-off capture and restore definitions for audit; their explicit years prevent
   recurring test jobs.

Main-account us-east-1 EBS `copySnapshot` completion events are retained for seven days
in CloudWatch Logs `/aws/events/fleet-backup-copy-snapshots`. Correlate each event's
source and destination snapshot IDs with Backup copy-job lineage before using its
`detail.incremental` value as evidence for the CMK checkpoint leg. This log does not
recover old events or prove the service-owned final air-gapped storage leg.

Existing recovery points keep their original lifecycles and age out normally; switching
selections does not shorten them. Never bulk-delete old history. Preserve the unmanaged
`fcvm-backups` vault's two indefinitely retained snapshots. Do not add compliance locks
to the temporary processing/checkpoint vaults. Final-vault immutability is not absolute
protection against AWS account closure.

The recovery plan `fleet_monthly_detached_ebs` runs on the eighth at 12:00 UTC. It
restores encrypted, detached test volumes only: no instance launch, volume attach,
or administrator role. Both initial and monthly tests explicitly use the recovery
account's us-east-1 `alias/aws/ebs` key: copied restore metadata can still reference
the original source-account key. This changes only test volumes, not live disks or
the final vault's AWS-owned encryption. Validation checks metadata/isolation, not
filesystem contents or bootability. The restore role also needs `kms:ReEncryptFrom`
on the final vault's exact service-owned source key, through EC2 in the recovery
region. Without it, EC2 initially returns a volume ID but the volume disappears
when asynchronous key authorization fails. The pinned provider does not expose
the air-gapped key, and its standard-vault data source rejects this vault type.
`backup_recovery_source_key_arn` therefore pins the exact public key ARN verified
with `DescribeBackupVault`; the vault has `prevent_destroy`. Recheck the pin against
live vault metadata after any deliberate vault recovery/replacement. It does not
grant access to every key in other accounts.
AWS Backup starts cleanup after successful validation (or the two-hour validation
window); the controller checks expected per-volume jobs and backs up cleanup after four hours.
Require a real successful restore/validation/cleanup cycle before claiming recovery
testing is proven. Five east1 tests cost $7.50 plus roughly $0.094 per hour retaining
all restored volumes. At September 7 pricing and measured written snapshot sizes,
east1 air-gapped storage starts around $39.85/month (693 GiB at $0.0575), plus roughly
$5.78 for the two rolling CMK checkpoints (115.5 GiB at $0.05). These are baseline
estimates, not a fixed bill: changed blocks, temporary-source lifetime, old history
aging out, transfer, KMS/API usage and restore tests add cost. A one-day source TTL
would retain nearly a continuous extra baseline; cleanup after verified completion
avoids that steady-state design, but slow or failed copies still accrue storage.

The jumpbox's gp3 volumes are capped at 125 MB/s. Never run broad recursive searches across
`/home/ubuntu` or `/tmp`; one such scan saturated both disks and made SSH unusable. Scope
searches to the repository, prefer `rg`, and move large scans/builds to ARM or the parallel
box.

## Regional security defaults

`security-defaults.tf` manages 102 account settings: three defaults across 17 enabled
regions in both the main and recovery accounts (34 account/region combinations).
Coverage is `ap-south-1`, `ap-northeast-{1,2,3}`, `ap-southeast-{1,2}`, `ca-central-1`,
`eu-central-1`, `eu-north-1`, `eu-west-{1,2,3}`, `sa-east-1`, `us-east-{1,2}` and
`us-west-{1,2}`. Newly enabled regions must be added explicitly; this does not enable
opt-in regions. `security-regions.tf` reuses the existing primary/DR providers and
adds only the missing regional aliases, with the existing recovery-account admin role.

- EBS encryption by default protects new volumes and new snapshot copies. Existing
  disks and snapshots are not re-encrypted; snapshots of an existing volume still
  inherit that volume's encryption. No KMS key is created or changed. The deferred
  existing-disk migration is not part of this rollout. See [EBS encryption defaults](https://docs.aws.amazon.com/ebs/latest/userguide/encryption-by-default.html).
- Snapshot `block-all-sharing` prevents new public sharing and also blocks public
  access to already-public owned snapshots. Private sharing and cross-account backups
  remain allowed. It does not erase the underlying public permission: disabling the
  block could re-expose old public snapshots. EBS-backed AMI public sharing is a
  separate control. See [snapshot public-access blocking](https://docs.aws.amazon.com/ebs/latest/userguide/block-public-access-snapshots.html).
- IMDS `HttpTokens=required` is the default for future launches, not hard enforcement:
  explicit launch options can override it. Existing instances are not changed. The
  pinned provider also writes `no-preference` for the regional endpoint/tag defaults
  and `-1` for the hop limit, matching the September 8 inventory's unset values.
  Review any non-default regional metadata setting before applying. See [regional IMDS defaults](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-IMDS-new-instances.html).

These settings have no recurring service subscription and create no detectors, log
pipelines, keys or compute. Ordinary future storage, copy and KMS usage still costs
money; in particular copying with a different key can require a full snapshot copy.
This is separate from the approved paid monitoring foundation, not evidence
that security logging/detection is active. Public SSH/ET, Cloudflare Access service-token access and
the verified backup plans, recovery history and cleanup permissions remain unchanged.

Before applying, read the full fresh plan: expect only these regional account settings,
no instance, volume, backup or access-policy changes. Inventory owned publicly restorable
snapshots in both accounts using `describe-snapshots --owner-ids <account-id>
--restorable-by-user-ids all` in every listed region; investigate any result before
changing its public visibility. After applying, require an empty follow-up plan and
read back `get-ebs-encryption-by-default`, `get-snapshot-block-public-access-state` and
`get-instance-metadata-defaults` in all 34 combinations. Expect `true`,
`block-all-sharing` and `AccountLevel.HttpTokens=required`, respectively. Account defaults
do not prove existing hosts or disks have been remediated.

## Security monitoring rollout

The owner approved the monitoring foundation on September 8, 2026, at approximately
$9/month fixed plus metered usage. Deployment requires the reviewed-plan, regional
compatibility and delivery gates below; approval is not evidence of live delivery. The
owner separately approved paid scanning on September 12, 2026. The source now enables
the five-region/account Config, Security Hub CSPM and Inspector stage described below;
the known alert's actual mailbox receipt was independently verified before rollout.
A merge alone is not evidence that recording or scanning has started.

`security-monitoring.tf` and `modules/security-region/main.tf` define the monitoring
foundation in both accounts across all 17 currently enabled regions (34 account/region
pairs). The foundation records organization-wide management events and object access
to the Terraform-state and dev-script buckets, enables base GuardDuty, and records
traffic for the three active VPCs. Regional EBS, IMDS and snapshot-sharing defaults
and the free external-access analyzers have independent owners; these monitoring
modules do not recreate them. Monitoring does **not**
migrate existing disks, restrict the required public SSH/ET access, remove Cloudflare
service-token access, or change the verified backup pipeline.

For a new deployment, roll out the foundation first with
`local.security_posture_enabled = false`. After delivery acceptance, a usage/cost review
and separate owner approval, enable that single bootstrap gate
for Config, Security Hub CSPM foundational checks and Inspector EC2/Lambda scanning:
main account `us-west-1`, `us-west-2`, `us-east-1`, and recovery account `us-west-1`,
`us-east-1`. The final recovery vault is in `us-east-1` and must not be omitted.
Config records global IAM once per account; EC2 instances, interfaces and volumes use
daily recording to bound runner churn, so those posture checks can lag by 24 hours.
Daily recording produces a configuration item only when the last recorded state has
changed; it is not a daily charge for every unchanged resource. Other supported
resources are recorded continuously. This uses the existing CSPM API, not the newer
Security Hub Essentials bundle. See [Config recording](https://docs.aws.amazon.com/config/latest/developerguide/select-resources.html)
and [CSPM pricing](https://aws.amazon.com/security-hub/cspm/pricing/).

GuardDuty uses the pinned provider's `aws_cloudcontrolapi_resource` for the single
`AWS::GuardDuty::Detector`, because its typed detector feature enum predates
`AI_PROTECTION` and `AI_ANALYST`. This is still Terraform-managed, without a second
CloudFormation stack or provisioning script. The initial request explicitly disables
every regionally supported optional feature, including AI Protection and runtime agents.
`AI_ANALYST` is specified only in the ten regions where AWS currently offers GuardDuty
Investigation; the explicit regional list lives in the module. The September 8
single-region compatibility check confirmed that requesting this unavailable feature
in `us-west-1` is rejected even with status `DISABLED`; no detector was created.
See [GuardDuty Investigation availability](https://docs.aws.amazon.com/guardduty/latest/ug/guardduty-investigation.html).
Supported features must be present and disabled in live readback. A documented
unavailable feature may be absent or disabled, but any unexpected enabled feature or
nested agent still fails the postconditions. Missing fields are never a blanket
substitute for disabling supported features. Regional handler support must still be
verified during deployment: schema validation is not proof that each regional service
accepts every feature. Do not silently omit an unsupported feature or accept an
unreviewed provider upgrade. See [CreateDetector defaults](https://docs.aws.amazon.com/guardduty/latest/APIReference/API_CreateDetector.html)
and [Cloud Control resource](https://registry.terraform.io/providers/hashicorp/aws/5.100.0/docs/resources/cloudcontrolapi_resource).
The pinned generic resource has no import handler. Preserve versioned Terraform state;
if its state entry is lost, recover the reviewed state version rather than creating a
second detector. A failed live-property postcondition requires an administrator to
diagnose and review a repair or future typed-resource migration; it does not perform
automatic remediation. Never remove `prevent_destroy` merely to make a plan pass.

After the separate 102 regional defaults, 36 external-analysis resources and two
global S3 controls, the monitoring foundation adds 293 managed Terraform resource
instances plus one existing SNS topic-policy update; the posture gate adds 45 more.
These are source counts, not
a substitute for the fresh plan. Many are free control settings or permissions, not
individually billed workloads. No existing compute, volume or backup resource should
be replaced by this rollout.

Security findings, root-account activity (including read-only management calls),
and high-risk IAM, network, KMS and backup changes forward to the
existing confirmed `cost-alerts` SNS topic. Inspector and GuardDuty use their native
finding events only; their Security Hub imports are excluded from forwarding to avoid
duplicate notifications. Foundational checks and other Security Hub products retain
their existing notification path. Normal processing/checkpoint cleanup does
not page the owner; deletion attempts against legacy/final backup history do. Every
region has two disjoint forwarding rules sharing one encrypted 14-day dead-letter
queue; each event pattern stays within AWS's default 2,048-character limit. A
five-minute read-only watchdog verifies enabled rules, exact event filters and targets
(destination, execution role, retries and DLQ), and queued events in all 34 pairs.
It alternates regional failure-metric queries between the two rules: each rule is
sampled every ten minutes with a fifteen-minute lookback, preserving the approved
CloudWatch retrieval budget. Central delivery metrics are still sampled every five
minutes. Independent missing-heartbeat and Lambda-error alarms cover observer failure.
It never consumes or deletes queued events.
Both the regional audit rule and the central notification rule use
`ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS`: forwarding to a custom bus does
not provide a documented exemption from the read-only management-event rule state.
Their event patterns stay unchanged, and the watchdog checks both states. See
[AWS read-only management-event routing](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-service-event-cloudtrail-management.html).
Regional event-bus forwarding targets retain their execution roles and regional DLQs,
but omit `retry_policy`: AWS rejects that setting for
[event-bus targets](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_events_targets.EventBusProps.html).
The central SNS target still has a one-day/185-attempt retry policy; the watchdog's
Lambda target retains its five-minute/two-attempt policy. Target readback checks this
distinction exactly rather than ignoring retry settings.
The S3 audit archive has 90-day **governance** retention and one-year expiry; privileged
administrators can bypass governance, unlike the recovery vault's compliance lock.
Config/Session Manager records use a separate versioned bucket. Only standard SSM shell
sessions are transcribed; SSH, Eternal Terminal and SSM port forwarding are not. The
builder instance role receives only bucket-location/encryption metadata reads and
transcript-prefix writes, not transcript reads or broader S3 access. Runner/builder
and shared jumpbox role grants follow their existing optional-resource gates.

An apply is not delivery proof. Require a fresh empty plan, then read back trail logging
status and recent digest/log objects, all three flow-log delivery statuses/objects,
regional detector/feature/analyzer configuration, event targets and DLQ policies,
three clean watchdog executions, and five healthy delivery alarms. Confirm SNS metrics
and the actual email for a known event. A GuardDuty sample finding is a **synthetic
service write** and sends a real notification; use it only as an explicitly authorized
acceptance canary, not as a read-only check. Without an observed matching event, record
the end-to-end alert path as unproven. A later posture rollout also requires Config
recording/delivery, enabled CSPM standards and Inspector coverage checks; managed/agentless
scan coverage is not established merely by enabling the service.

September 8, 2026 deployment receipt: infrastructure revision `80bbd3a` was applied and
a fresh full Terraform plan reported no changes. Readback verified 34 base-only GuardDuty
detectors, 70 routes (68 regional forwarding targets, the central SNS target and the
watchdog schedule target), three clean watchdog executions and five healthy delivery
alarms. The RSA-2048 signature of one initial main-account `us-west-1` CloudTrail digest
was verified; it referenced zero log files, and no event-log bodies were read. Event-log
hashes/full-chain integrity, actual email receipt, an SSM session transcript and a
flow-log object from the quiet `us-west-2` VPC remain unproven; this is not yet complete
end-to-end delivery acceptance.

September 12, 2026 acceptance: the owner-authorized GuardDuty high-severity sample
`570453a9836e4b60be3faecb4ec18612` (`Backdoor:EC2/C&CActivity.B!DNS`, severity 8,
`sample=true`, fictitious instance `i-99999999`) was created at 18:06 UTC. The regional
forwarder and central SNS target each recorded one successful invocation, and SNS
recorded one publication, two deliveries to its existing email/Lambda subscriptions,
and zero failures. A preceding severity-2 SSH sample was below the intended notification
threshold. Read-only access to the owner's configured mailbox verified the exact positive
sample message received at 18:06:30 UTC; this closes the delivery gate without inferring
mailbox receipt from SNS metrics or claiming the owner had read the message.
Four clean 34-region watchdog
executions and all five healthy alarms were read back. A bounded standard SSM shell
session on fcvm produced a 426-byte encrypted, versioned transcript whose unique test
marker was verified after the session terminated. Retained September 9 S3 flow-log
objects match the exact active west2 flow-log ID; west2 compute was stopped during
this check and no extra instance was launched. All three flow logs report successful
delivery and have matching objects. CloudTrail digest signatures, chain links and
referenced log hashes were verified with the official CLI for a bounded 15:00–17:00 UTC
window across all 34 account/region pairs (102/102 digests and 1,074/1,074 logs valid).
This window is not a claim of integrity for all historical log objects; audit log
bodies were not printed or sent to development hosts.

### Incremental monitoring cost

September 8, 2026 rate checks put the watchdog's CloudWatch component around **$8.06 per
30-day month**, not the whole monitoring bill: 622,080 metric queries (including the
higher São Paulo rate), four custom metrics and five standard alarms. The trail's
rotating customer key adds about $1/month. The first management-event trail copy has
no CloudTrail event charge; selected S3 data events cost $0.10 per 100,000 events.
Lambda, SQS, SNS, KMS requests, S3 storage/requests and flow-log delivery are additional.
See [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/),
[CloudTrail pricing](https://aws.amazon.com/cloudtrail/pricing/) and
[KMS pricing](https://aws.amazon.com/kms/pricing/).

Usage-based rates in the primary region include GuardDuty management analysis at
$4.40/million events and flow/DNS analysis at $1.10/GB, plus flow-log delivery to S3 at
$0.335/GB. The deferred posture stage adds Config at $0.003/continuous or $0.012/daily
configuration item and CSPM at $0.001/security check. Security Hub service-linked
Config rule evaluations are not separately charged. Recent measured EC2 usage
(777.46 instance-hours over September 1–7, both accounts) and ten covered functions
including the new watchdog imply roughly **$10–13/month for Inspector** if that usage
continues; agentless scanning can also create billable temporary EBS snapshots.
See [GuardDuty](https://aws.amazon.com/guardduty/pricing/),
[Config](https://aws.amazon.com/config/pricing/), [CSPM](https://aws.amazon.com/security-hub/cspm/pricing/)
and [Inspector pricing](https://aws.amazon.com/inspector/pricing/).

For scale only, an assumed month with one million GuardDuty events, 5 GB analyzed logs,
5 GB flow-log delivery, 1,000 daily and 2,000 continuous Config items, and 10,000 CSPM
checks would be about **$59–62** including the above fixed components and projected
Inspector, before storage/API charges. Those event/configuration volumes are examples,
not measured forecasts or a spending cap. Review Cost Explorer usage types and GuardDuty
usage statistics after the first complete week; initial Config inventory, rapid runner
churn or unusually noisy network activity can raise the bill. The existing account-wide
cost alerts remain useful, but do not enforce a monitoring-only budget limit.

## Account-wide S3 public-access blocking

`security-s3-account.tf` manages two global account controls, one each for main
and recovery, with all four S3 Block Public Access flags enabled. They cover current
and future buckets/access points in every region; unlike the future-only EBS defaults,
these controls can also block existing public access. They do not rewrite bucket
policies/ACLs, migrate disks, create paid monitoring services, or change public SSH,
Cloudflare service-token access or the verified backup pipeline.

Before deployment, the September 8, 2026 16:41 UTC read-only inventory found six main
account buckets and no recovery-account buckets. All six already had all four
bucket-level blocks, `BucketOwnerEnforced`, no bucket policies, no effective public
ACL grants and no S3 website configuration. All 34 regional access-point listings
and both multi-region listings were empty. No objects were enumerated or read.
Private operator evidence is in
`/tmp/aws-guardrails-review.inJgVqJ5/s3-public-metadata.json` and
`s3-access-point-metadata.json`; these ephemeral files are not cold-bootstrap inputs.
Recheck metadata if inventory changes before apply.

S3 combines account, bucket and access-point settings using the most restrictive
values. Fixed-principal private sharing remains possible; a public bucket policy can
cause `RestrictPublicBuckets` to block even its otherwise private cross-account grants.
Review intended S3 websites/sharing before changing these controls, and do not disable
the account block to make an unreviewed public policy pass. See
[AWS Block Public Access semantics](https://docs.aws.amazon.com/AmazonS3/latest/userguide/access-control-block-public-access.html).
Expect a fresh full plan with exactly two account-setting creates and no bucket,
object, policy or backup changes. After applying, require an empty follow-up plan and
read back `s3control get-public-access-block --account-id <account-id>` under each
account's verified credentials: all four flags must be true. Account-wide settings
propagate globally but not necessarily simultaneously across regions.

## External access findings

`security-external-access.tf` creates 34 external-access analyzers: one `ACCOUNT`
analyzer named `security-external-access` in each account/region covered by the
regional defaults above. It reuses the same providers and does not enable regions.
AWS lists external analysis at [no additional charge](https://aws.amazon.com/iam/access-analyzer/pricing/);
paid internal-access/unused-access analyzers and paid custom policy checks are not
enabled. Terraform also owns the two required `AWSServiceRoleForAccessAnalyzer` roles,
one per account (36 resources total). Each account's analyzers depend on its own role
to avoid racing first-time service-role creation. Only Access Analyzer can assume
this read-only, service-owned resource-metadata role; its trust/permissions are defined
by AWS, not custom grants to operators or workloads. No workload role gains permissions.
See [service-role boundary](https://docs.aws.amazon.com/IAM/latest/UserGuide/access-analyzer-using-service-linked-roles.html).

This reports supported resource-policy access from outside the account, including
public and cross-account sharing. It is not an access-control enforcement or proof
that all identity-policy escalation paths are closed. Intentional sharing between
the main and recovery accounts can generate findings because each account is its
own trust boundary. No findings are automatically archived; inspect their resource,
principal and policy before deciding whether access is intended. Creating an analyzer
does not change public SSH, Cloudflare service-token access, disks or backup sharing.

This free stage does not add email delivery. Findings are available through the
Access Analyzer API; the separately reviewed monitoring stage can forward finding
events later. After the fresh full plan/apply, verify all 34 analyzers have exactly
`type=ACCOUNT` and `status=ACTIVE` using `list-analyzers`/`get-analyzer`, then inspect
active findings with `list-findings`. Allow the documented analysis delay (policy
changes can take up to 30 minutes); an immediately empty list is not a completed
security audit. Do not silently create a second analyzer or replace an existing
one if preflight finds account-level analysis already configured.

## Private browser manager

`browser-manager/` is a separate, single-owner Next.js dashboard and `browserctl` CLI for
multiple named browser desktops on one host. One dedicated Cloudflare Tunnel hostname routes
`/browsers/<name>` to that exact desktop; it does not modify Dolphin Labs or its tunnel.
The dashboard can create/start/stop browsers and rename their display labels; the CLI prints the same shareable desktop URLs.
It is single-owner, not a multi-tenant service: separate profiles isolate browser sessions,
but are not separate OS security boundaries. These browsers have the host user's normal
network and filesystem access. Only that owner should have Cloudflare Access permission.

The dashboard and browser-view WebSocket both require a verified Cloudflare Access
JWT for the configured audience and owner. Mutations require the expected Origin. The local
CLI uses an owner-only Unix socket, not a public bearer token. VNC uses private Unix sockets,
not public ports; unknown instance names cannot become arbitrary network or filesystem targets.
Every managed desktop has its own display and persistent browser profile. Stopping a desktop
retains its profile. There is no profile-deletion command, arbitrary executable launcher,
or remote CDP endpoint. Browser processes remain sandboxed. An expired Access session loses
its live desktop connection and must sign in again before reconnecting.

### 1. Apply on your administration host

Use this repository's Terraform 1.10.3 and existing backend/provider setup. Review the plan
for the whole stack; do not use `-target` to bypass unrelated changes. No Terraform plan or
apply runs on the browser/dev host, and it does not delegate one to a jumpbox.

```bash
umask 077
bm_handoff=$(mktemp -d)
terraform init -lockfile=readonly
terraform plan -out="$bm_handoff/plan"
terraform apply "$bm_handoff/plan"
terraform output -raw browser_manager_env > "$bm_handoff/browser-manager.env"
```

The hostname is fixed at `browsers.cc-games.dev`. Terraform creates a dedicated tunnel,
DNS record, and owner-only Access application using the existing Google/one-time-PIN providers.
It does not reuse the kids' family policy or Dolphin's GitHub policy. The browser host needs no Terraform,
AWS administration credentials, or Cloudflare account API token.

Terraform stores the raw connector token in AWS Secrets Manager as
`browser-manager-tunnel-token` in `us-west-1`. The shared ARM/x86 dev-server instance role
can read only this secret through its dedicated policy. Fetching it requires no additional
AWS login; the Next.js, runner, and temporary-compute roles do not receive this grant.

Transfer the public environment file privately to the browser host, owned by the browser
user with mode `0600`. On the ARM/x86 host, retrieve the connector token as `ubuntu`:

```bash
umask 077
install -d -m 0700 /home/ubuntu/.config/browser-manager
bm_token_tmp=$(mktemp /home/ubuntu/.config/browser-manager/.tunnel-token.XXXXXX)
if aws secretsmanager get-secret-value --region us-west-1 \
  --secret-id browser-manager-tunnel-token --query SecretString --output text > "$bm_token_tmp" \
  && test -s "$bm_token_tmp"; then
  mv -f "$bm_token_tmp" /home/ubuntu/.config/browser-manager/tunnel-token
else
  rm -f "$bm_token_tmp"
  echo 'Tunnel token retrieval failed; the existing token file was preserved.' >&2
fi
```

The connector token and saved Terraform plan are sensitive. Do not print or check them in.
The application receives the public Access settings; only `cloudflared` receives the token
file path. A public URL cannot work until both this apply and the host installation are done.

Terraform also creates separate Cloudflare Access service tokens for OpenClaw viewers of
the AWS and Mac browser hosts. Each is accepted only by its exact Access application, and
the origin independently verifies that the signed JWT's `common_name` equals that token's
client ID. The client ID is public application configuration. The secrets are stored in
AWS Secrets Manager as `browser-manager-openclaw-access` and
`browser-manager-mac-openclaw-access`, and also in sensitive Terraform state. Resource
policies deny direct reads by dev and CI roles. Account administrators can still change
those policies; this is not an immutable boundary against an administrator.

The local Mac uses its existing AWS SSO login to assume
`browser-manager-openclaw-client`. That role can read only those two viewer secrets; it has no
Cloudflare administration, tunnel, or other Secrets Manager access. Configure the local
profile after apply. Obtain `browser_manager_openclaw_client_role_arn` with Terraform
on the administration jumpbox and hand that public ARN to the Mac operator; do not
run Terraform on the Mac. For this account the commands are (no credential value):

```bash
aws configure set profile.browser-manager-openclaw role_arn \
  arn:aws:iam::928413605543:role/browser-manager-openclaw-client
aws configure set profile.browser-manager-openclaw source_profile AdministratorAccess-928413605543
aws configure set profile.browser-manager-openclaw region us-west-1
```

Both service-token resources have `prevent_destroy`: replacing one changes the client ID
pinned in the browser-manager environment. Do not remove the guard for an ordinary
apply. A deliberate replacement requires a coordinated maintenance deployment: save
the corresponding `browser_manager_env` or `browser_manager_mac_env` output,
reinstall/restart that origin with its environment,
refresh the local viewer session, and verify both machine access and unauthenticated
denial. The existing owner-login policy stays attached throughout. A stale origin must
fail closed, not accept an arbitrary replacement service identity.

### 2. Install on the browser host

Use Linux with Node.js 22.13+ and an installed, sandbox-capable Chromium/Chrome. Install the
desktop prerequisites (`sudo apt-get install xvfb x11vnc openbox x11-xserver-utils x11-utils wmctrl python3 dbus libglib2.0-bin at-spi2-core python3-dbus`) and a current
[`cloudflared`](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/)
with `--token-file` support. Do not disable the Chrome sandbox to make installation pass.

In the transferred environment file, add `BM_BROWSER_BIN=/absolute/path/to/chrome` if Chrome
is not installed at a standard `/usr/bin/chromium`, `/usr/bin/chromium-browser`, or
`/usr/bin/google-chrome` path. The installer never downloads/replaces a browser or imports
any browser's credentials. Then, from this checkout:

```bash
cd browser-manager
npm ci --ignore-scripts
npm run build
./scripts/install.sh /absolute/path/browser-manager.env /home/ubuntu/.config/browser-manager/tunnel-token
```

Run the installer as the browser owner, not root. It installs two **user** systemd units,
`browser-manager.service` and `browser-manager-tunnel.service`, and `~/.local/bin/browserctl`.
Only these units are restarted. Existing browser services and Dolphin remain untouched.
The checkout must stay at the same path; to update it, rebuild and rerun the installer.
If prompted, an administrator can enable logout/reboot persistence with
`loginctl enable-linger <browser-user>`.

State and profiles live under `~/.local/state/browser-manager` (private to the owner).
Configuration and token files live under `~/.config/browser-manager`. The supported installer
uses that fixed state location so the CLI and service always agree. The application listens
only on `127.0.0.1:3210`; raw VNC and local CLI traffic use private Unix sockets.

### 3. Use it

```bash
browserctl start claude --url https://claude.ai
browserctl start research --url https://example.com
browserctl list
browserctl url claude
browserctl rename claude "Claude · Personal"
browserctl stop research
```

Open `https://browsers.cc-games.dev`, sign in through Google as the configured owner, then
open either desktop (for example `/browsers/claude`). Log in to websites within that desktop;
those logins stay in its host-side profile. Closing a viewer tab does not stop the browser.
Up to 32 names can be registered. Stop retains a name and its profile, and Start reuses it.
On service restart, previously running managed desktops are restored from saved desired state.

Use **Rename** on a browser card to edit its display label (1–80 characters, no control characters).
Labels are trimmed and saved; the dashboard and desktop title show them. Renaming never changes
the stable name used by CLI commands, `/browsers/<name>` URL, profile, saved logins, or live connection,
and does not restart the browser. Older browsers initially display their stable name as the label.

For machine-controlled viewers, create the two dedicated OpenClaw browser profiles and
an empty tab in each once. Leave existing profiles in place. Install the helper's dependencies
on the Mac, then stream each
AWS secret directly into it. The helper exchanges the service credential over HTTPS
with only the credential's allowlisted browser-manager hostname, refuses redirects,
verifies the returned Access JWT and a cookie-only API request, then installs a host-scoped cookie.
The helper pins each hostname to its own dedicated profile, never a normal browsing profile.
It never installs the long-lived service
secret as profile-wide browser headers or puts it in argv, a file, or command output:

```bash
openclaw browser create-profile --name secure-browser-viewer --color '#6B5BFF'
openclaw browser --browser-profile secure-browser-viewer open about:blank
openclaw browser create-profile --name secure-mac-browser-viewer --color '#16A34A'
openclaw browser --browser-profile secure-mac-browser-viewer open about:blank
npm --prefix browser-manager ci --ignore-scripts
aws --profile browser-manager-openclaw secretsmanager get-secret-value \
  --secret-id browser-manager-openclaw-access --query SecretString --output text \
  | node browser-manager/scripts/configure-openclaw-viewer.mjs secure-browser-viewer
aws --profile browser-manager-openclaw secretsmanager get-secret-value \
  --secret-id browser-manager-mac-openclaw-access --query SecretString --output text \
  | node browser-manager/scripts/configure-openclaw-viewer.mjs secure-mac-browser-viewer
```

Use `secure-browser-viewer` for `browsers.cc-games.dev` and `secure-mac-browser-viewer`
for `mac-browsers.cc-games.dev`. An expired AWS SSO session must be renewed with
`aws sso login --profile AdministratorAccess-928413605543` before secret retrieval;
the helper does not copy administrator credentials or silently bypass failed login.
The helper clears any legacy
profile-wide headers, sets only the short-lived host-scoped Access cookie, and navigates
to the dashboard. Browser cookies are credentials and may be persisted in that local
profile; the one-year service-token secret never enters it. Run the helper again when
the Access session expires. Website passwords entered inside a remote
desktop stay in that remote desktop's persistent host-side profile; they are not copied to
the local viewer.

Use **Back** beside the desktop title to go back in the remote browser's history. The separate
top-left arrow returns to the browser-manager dashboard instead. Back follows the active remote
Chrome window/tab's native toolbar state, including navigation from other viewers. It is disabled
while disconnected or when the native state is unavailable. Foreground viewers refresh this
read-only state every 500 ms after the previous read completes; background polling pauses.
Each desktop uses its own private accessibility bus and one persistent native reader, kept alive
until Chrome exits. Repeatedly subscribing and disconnecting short-lived readers can crash
Chromium's ATK keyboard-event handler, including on versions 151 and 153. A slow read returns
unknown without tearing down the subscription. No browsing history URLs are returned and no
remote-debugging endpoint is exposed. Host accessibility preferences are not changed.
On a phone, use **Fit to screen**, or turn it off for an actual-size scrollable desktop.
Use **Phone** to switch the remote browser itself to a narrow, responsive layout. On a phone,
the size follows the viewer's available width and height when tapped (bounded to 320–500 ×
480–900 logical CSS pixels); desktop viewers use a 390 × 844 preset. Tap **Phone** again to return to
the 1440 × 900 desktop. This changes only the selected browser's shared display, so other
viewers of that same browser also see the change; separate browser instances are unaffected.
The Phone button follows the actual shared framebuffer in every connected viewer, not a cached
metadata response. A second viewer can toggle the current mode without refreshing or reconnecting;
local Fit scaling and viewer-window resizing do not change the shared mode.
Tabs, page state, and logins remain in place: switching modes does not restart or reload the
browser. The mode survives viewer reconnects, but a browser/service restart starts in Desktop
mode. Phone changes the viewport, not the browser's user agent; it is not an iOS emulator.
**Keyboard** opens explicit text/paste and special-key controls; desktop keyboards and pointer
input also work directly. **Fit to screen** scales independently for each viewer; only the
explicit **Phone** toggle resizes the shared desktop. **Fullscreen** is shown only where the
viewing browser supports it.

In the Linux VNC viewer, swipe with one finger to scroll the remote page or the panel
where the swipe starts. A quick swipe has a short glide; touching again stops it. Reduced-motion
preferences disable the glide. Tap normally to click; to drag an item, tap once, then touch
and drag within a moment. Long-press and multitouch gestures keep noVNC's existing behavior.
Mouse input is unchanged. VNC carries discrete wheel steps, so this improves touch handling
but cannot promise pixel-for-pixel scrolling or a native phone's frame rate over the network.

In the Linux VNC viewer, **Fit** stays visible but is greyed out when fitting and actual size
are already the same (less than one pixel of difference), or while disconnected. It re-enables
when the shared display or available viewer space changes, including rotation and opening the
keyboard, and retains its on/off setting.

The Linux VNC browser renders at fixed 2× density: a 390 × 844 Phone viewport has a
780 × 1688 framebuffer, while Desktop has 2880 × 1800 pixels. Text and browser controls
keep their logical size; **Fit** off means logical actual size, not one physical framebuffer
pixel per CSS pixel. This supplies more detail on high-density screens at four times the
source pixel count; it is not lossless or full native density on every phone. Existing
desktops pick up the density change when restarted.

VNC uses 24-bit true color with high JPEG quality (9/9) by default. When the viewing browser
reports Data Saver, a 2G/3G connection, or a positive downlink below 2 Mbps, it uses quality 6/9 and
stronger compression; changing connection hints updates the live connection. Browsers without
these hints, including Safari, keep high quality. These are browser hints, not a throughput test.

Disconnected viewers retry automatically while their tab is visible and focused, waiting
1, 2, 4, 8, then at most 10 seconds between failed attempts. Returning to the tab reconnects
immediately, including after phone suspension; background retries pause. A successful connection
resets the delay. Manual **Reconnect** remains available. Retries never bypass Access sign-in;
if it has expired, reload to sign in again. Reconnecting the viewer does not restart the browser.

To reuse an existing login profile without copying it, first close its existing browser using
that browser's normal service controls, then run
`browserctl start <name> --profile /absolute/profile/path` on this host. The CLI alone permits
explicit profiles; public API requests cannot select a filesystem path. External profiles with
Chrome locks are refused. Do not remove locks while another browser could be running. Reusing
a profile starts a new managed desktop; it does not attach VNC to an already-running display.

For service diagnostics use `systemctl --user status browser-manager browser-manager-tunnel`
and `journalctl --user -u browser-manager -u browser-manager-tunnel`. If the shell has no user
bus address, prefix these with `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus`.
Cloudflare errors before sign-in usually mean the apply/connector is not ready; a stopped
desktop can be started from the dashboard. A profile remains on disk even if startup fails.

The AWS browser connector secret is Terraform-managed. For an intentional rotation, bump
`random_bytes.browser_manager_tunnel_secret.keepers.rotation`, review a fresh full plan, and
apply the in-place tunnel update and new Secrets Manager version. Never replace the tunnel,
DNS, Access application, or Mac connector. Read the replacement secret directly on ARM
using its instance role into a private temporary file, verify it, and atomically install it
as `~/.config/browser-manager/tunnel-token` (owner `ubuntu`, mode `0600`). Restart only
`browser-manager-tunnel.service`; the browser manager and its desktops stay running.
After a suspected exposure, also invalidate existing connections for this exact tunnel
through Cloudflare's connections API, then verify the new connector is ready. Rotation
alone prevents new connections with the old credential but does not disconnect old ones.
Never print either token or place it in command arguments, Git, or a dev-host AWS admin session.
Installed connector units read a local file; they do not automatically refresh from Secrets
Manager. A stopped AWS browser host (including x86) is not verified by an ARM rotation:
leave it stopped, and refresh its token through its own instance role before restarting
its connector on the next authorized boot. Do not start an extra replica against a different
browser manager, because Cloudflare would distribute traffic between unrelated desktops.
See [Cloudflare token rotation and connection invalidation](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/remote-tunnel-permissions/#rotate-a-compromised-token).

September 12, 2026 receipt: revision `cc87713` rotated the AWS tunnel in place and
published a new Secrets Manager version. ARM fetched it using its own instance role;
the installed file was verified `ubuntu:ubuntu`, mode `0600`. Existing connections were
invalidated and a new connector became healthy. The browser-manager process stayed
running; unauthenticated access returned the Access login redirect and the dedicated
service-token request reached the application with HTTP 200. The Mac tunnel was not
changed, and x86 remained stopped with the next-boot refresh requirement above.

### Development and acceptance

`npm test`, `npm run typecheck`, and `npm run build` are the focused CI gate. Install `x11vnc`
before testing (CI does this automatically). Tests cover signed Access identities, Origin
enforcement, exact socket routing, expiry, and lifecycle without needing a Cloudflare account.
A small native VNC regression checks that the real server accepts Unix-socket connections
and owns no IPv4 or IPv6 TCP listeners; it does not need Chrome or the full UI. After building,
`BM_BROWSER_BIN=/absolute/path/to/chrome npm run test:live` runs two real sandboxed desktops and the production UI
on loopback with an ephemeral signed test identity; it does not add a production auth bypass.
It checks desktop/phone layout, 2× rendering with unchanged logical sizes, real VNC input
including small-target clicks with Fit on and off, native phone-width reflow and restoration,
phone-mode navigation, reconnect, and profile retention. Screenshots
go to `~/browser-manager-ui-artifacts`, never Git; test-only profiles are retained under the
printed temporary directory and all test desktops are stopped. New E2E findings should get
the smallest practical regression at the layer responsible, not a new proof framework.

### Native browsers on the personal Mac

`https://mac-browsers.cc-games.dev` is an independent browser manager on the personal
Mac, not the optional AWS Mac instance. Chrome renders webpages, stores its profiles and
uses the Mac's network locally. The AWS browser list at `browsers.cc-games.dev` is separate;
there is no profile synchronization or automatic federation. The Mac has its own tunnel
and Access application/audience. Never run its connector with the AWS browser tunnel token:
Cloudflare would load-balance requests between two different machines' browser lists.

The Mac backend starts installed, sandboxed **native headed Chrome** with a dedicated
profile per browser. It streams webpage images and accepts only bounded tab, navigation,
pointer, key and explicit text commands through an authenticated WebSocket. Browser-manager
supplies the tabs and address bar; Chrome's native toolbar, macOS file dialogs, Touch ID and
the rest of the Mac desktop are not streamed. Downloads are disabled in managed Mac browsers.
Chrome debugging uses inherited private pipes, never a listening debug port or public CDP
proxy. No Screen Recording or Accessibility grant is requested. The normal Chrome profile
and existing OpenClaw services are not touched, and website logins must be made in the new
managed profiles. Profiles are private but are not separate OS security boundaries.

Terraform runs only on the jumpbox. After reviewing/applying the full plan, export
`terraform output -raw browser_manager_mac_env` into a private environment file. The Mac's
connector credential is stored in Secrets Manager as `browser-manager-mac-tunnel-token`;
it is also sensitive Terraform state. Transfer that token and public environment file over
the existing authenticated SSH connection, owned by the Mac user and mode `0600`. Do not
print tokens or place them in Git, command arguments, browser profiles or LaunchAgent XML.
For later credential retrieval, the Mac's existing SSO administrator session can assume
`browser-manager-mac-connector`, which can read only that connector secret. No AWS
credentials are required by the running browser-manager or tunnel process.

Install as the logged-in Mac user from a full checkout at a permanent path (the deployed
path is `/Users/ejcampbell/src/aws-browser-manager`). Existing Chrome and Node.js 22.13+
are prerequisites; `brew install cloudflared` supplies the connector. From `browser-manager`:

```bash
npm ci --ignore-scripts
npm run build
node scripts/install-macos.mjs /absolute/private/browser-manager.env /absolute/private/tunnel-token
browserctl start personal --url https://example.com
```

The installer owns only the user LaunchAgents `com.ejc3.browser-manager` and
`com.ejc3.browser-manager-tunnel`. They restart on that user's login, not before FileVault
unlock; logout or sleep makes the Mac URL unavailable. It does not change power settings.
State stays under `~/.local/state/browser-manager`, configuration under
`~/.config/browser-manager`. Rerun the installer after rebuilding to update only these
services; previously running managed browsers are restored and their profiles retained.
Always use this GUI LaunchAgent startup path: Chrome started directly under SSH on this
Mac cannot access Keychain (`errSecInteractionNotAllowed`) and cannot persist encrypted
cookies. Do not substitute an unencrypted password store or a mock Keychain. If macOS
requests Keychain authorization, the owner must approve it in their GUI login session.
Open the Mac URL on your phone and sign in as the same configured owner. Multiple viewers
of one named browser share its selected tab and input; different names have separate profiles.

`npm test`, `npm run typecheck`, and `npm run build` also run on macOS; the native Linux
VNC test is skipped there. The Mac backend and authenticated page-transport regressions
cover private pipes, bounded actions, profile isolation and immediate session expiry.

## File map

| Area | Main files |
|---|---|
| Backend, providers, VPC, routing | `main.tf`, `.terraform.lock.hcl`, `variables.tf`, `runner-vpc.tf` |
| Admin hosts | `jumpbox.tf`, `jumpbox2.tf`, `jumpbox2-user-data.tf` |
| Metal dev boxes | `firecracker-dev.tf`, `x86-dev.tf`, `dev-user-data.tf` |
| Shared dev setup | `dev-instance-common.tf`, `dev-selfupdate.tf`, `dev-hop-key.tf`, `dev-ebs.tf` |
| Kids' environment | `nextjs-dev.tf`, `nextjs-user-data.tf`, `cloudflare.tf` |
| Shared I/O and burst compute | `io-box.tf`, `parallel-box.tf`, `parallel-box-watchdog.tf`, `scripts/parallel-box.sh` |
| GitHub runners and OIDC | `runner-autoscale.tf`, `github-actions.tf`, `GITHUB-RUNNERS.md` |
| Recovery and monitoring | `backups.tf`, `backup-security.tf`, `security-monitoring.tf`, `modules/security-region/main.tf`, `cost-alerts.tf`, `fcvm-ec2-key-backup.tf` |
| Regional account defaults | `security-defaults.tf`, `security-regions.tf`, `modules/security-defaults/main.tf` |
| Free external-access findings | `security-external-access.tf` |
| Global S3 public-access defaults | `security-s3-account.tf` |
| Private browser desktops (AWS and personal Mac) | `browser-manager/`, `browser-manager.tf`, `browser-manager-mac.tf` |
| Optional Mac | `mac-dev.tf`, `mac-dev-secrets.tf`, `mac-dev-teardown.tf` |
| Staging and packages | `dev-staging-account.tf`, `dev-staging-bootstrap.tf`, `codeartifact.tf` |

`AGENTS.md` contains the deeper operational constraints, nested-virtualization details,
persistent-Spot recovery procedure, and live-system safety notes that an automation agent
must read before changing the repository.
