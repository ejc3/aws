# GITHUB-RUNNERS.md — how CI reaches this AWS account

How `ejc3` repos get CI into AWS account `928413605543` (us-west-1), and what crosses
the GitHub↔AWS boundary to make it work. Defined in `github-actions.tf`,
`github-ami-builder.tf`, `runner-autoscale.tf`, and `runner-vpc.tf`.

Shared main/staging CI identities are retired and GitHub performs credential-free
Terraform validation. This source additionally retires job-host PAT reads and narrows
the runner controller's launch permissions. Deployment still requires the separate
before/after canaries, fresh old-boot drain check, and real post-cutoff trusted job below.

**The model:** two integrations, opposite directions of trust. GitHub-hosted runners
reach *into* AWS with **no stored credential** — short-lived OIDC federation. Self-hosted
runners run *inside* AWS on spot metal and reach *back* to GitHub using a short-lived
instance-bound registration credential. The controller alone reads the long-lived PAT
in SSM after the gated cutoff. Every resource below belongs to one of those two patterns.

|  | Pattern A — hosted → AWS | Pattern B — self-hosted in AWS |
|--|--|--|
| Repos | `ejc3/fcvm` owner-approved AMI build only | `ejc3/fcvm` |
| Runs on | GitHub's `ubuntu-latest` | spot `*.metal` in this account |
| Secret across the boundary | none (federated token) | GitHub PAT + webhook HMAC |
| What it does | isolated, non-admin AMI builds | KVM/Firecracker CI that needs bare metal |
| Credential lifetime | ~1h STS session | controller PAT until rotated; host credential deleted before its single job |

## Pattern A — hosted runners reach into AWS with OIDC (keyless)

A GitHub-hosted job authenticates to AWS by presenting an OIDC token GitHub signs for it,
which AWS STS exchanges for a ~1-hour role session. Nothing long-lived is stored on either
side — there is no AWS access key in repo secrets to leak or rotate. OIDC by itself
does not make permissions safe: the old role could pass the administrator role to
an EC2 instance. The dedicated role explicitly denies passing any other role.

The trust chain:
- **OIDC provider** (`aws_iam_openid_connect_provider.github`) — `token.actions.githubusercontent.com`,
  audience `sts.amazonaws.com`, thumbprint `6938fd4d98bab03faadb97b34396831e3780aea1` pinned.
- **Role** `github-actions-ami-builder` — requires audience `sts.amazonaws.com` and
  subject `repo:ejc3/fcvm:environment:runner-ami-publish`. This GitHub environment
  permits only the `main` deployment branch and requires approval by `ejc3` with
  administrator bypass disabled. Collaborators can request a main build; only
  the owner may approve publishing the image used by future shared runners.
- **Permissions:** Canonical Ubuntu images; the isolated runner subnet/security
  group; tagged builder instances, volumes and ENIs; image creation from those
  builders; c7gd.8xlarge with IMDSv2 and encrypted 40 GiB gp3 root; `PassRole` only on `ami-builder-role` and
  only to EC2. It cannot mutate IAM, assume another role, read state/SSM parameters/
  Secrets Manager payloads, or launch the administrator profile.
- **Builder host** `ami-builder-profile`: SSM agent connectivity and progress tags,
  no reusable secrets or administrator delegation. CI reads progress tags, not arbitrary
  SSM command output. Detailed builder diagnostics remain administration-only.

The consumer is fcvm's `.github/workflows/build-runner-ami.yml`, after the producer
role/profile exists. Its `workflow_run` must require successful, same-repository
main builds; manual dispatch of main remains available to collaborators. Checkout
uses `github.event.workflow_run.head_sha` for automatic runs and `github.sha` for
manual runs. Normal CI does not require this publishing approval.

`ejc3/aws/.github/workflows/drift.yml` now runs **Terraform Validation**, not live
drift: formatting, locked provider installation with `-backend=false`, `validate`,
and offline security tests. It receives no AWS identity, state, or secret values.
Full plans stay on an administration jumpbox. A plan reads credential-bearing state
and refreshes managed secrets; `sensitive = true` masks display, not access. Do not
broaden a secret policy or restore CI state access just to make a plan green.
Both old `github-actions-terraform` identities (main and staging) are retained with
explicit `Deny *`; they provide no authority, including for old role sessions.

## Pattern B — self-hosted autoscaling runners on spot metal

`ejc3/fcvm` needs bare metal (nested KVM, Firecracker), which GitHub's hosted runners can't
provide. So a job queued on `ejc3/fcvm` triggers AWS to launch a spot `*.metal` instance
that registers itself back as a self-hosted runner, serves the job, and is reaped when idle.

**Launch path.** GitHub fires a `workflow_job` webhook — the hook itself is Terraform-managed
(`github_repository_webhook.runner`, adopted from hook id 589197362) → API Gateway HTTP API
(`POST /webhook`, output `runner_webhook_url`) → Lambda `github-runner-webhook`
(`reserved_concurrent_executions = 1`, so concurrent webhooks can't all read the same count
and over-launch). The Lambda HMAC-verifies `x-hub-signature-256` against `WEBHOOK_SECRET` on
every request that arrives through API Gateway, **failing closed** if the secret is unset (the
cleanup Lambda's direct `lambda:Invoke` retries carry no `requestContext`, so they're trusted
without a forgeable header); it acts only on `action == "queued"`, reads the job labels to pick
an architecture
(`x64`/`x86_64`/`amd64` → x86, else arm64), and launches a one-time spot instance from a
self-built AMI (`tag:Purpose = github-runner`, newest matching the arch) up to **4 runners
per architecture** (`local.runner_max_per_arch`, shared with the cleanup Lambda). ARM tries
`c7gd`/`m7gd`/`r7gd.metal`; x86 tries `c5d`/`m5d`/`r5d`/`m6id.metal`, with any type
that recently failed for capacity moved to the back of that order. Each instance is tagged
with a `LeaseExpires` 60 minutes out.

ARM types must additionally be **Graviton3 or newer** (family digit >= 7): fcvm's nested
virtualisation tests need FEAT_NV2, which Graviton2 lacks, so a job landing on `c6gd.metal`
or `m6gd.metal` fails every `test_kvm` case. Storage is necessary, not sufficient.

Every type in those lists has instance storage (the `d` families). That is a constraint of
**this bootstrap**, not of fcvm: user_data builds `/mnt/fcvm-btrfs` only from instance-store
NVMe, and the asset directories, the `containers` symlink and `CARGO_TARGET_DIR` all sit
inside that branch, so on a storeless box the mount never happens and every job fails.

fcvm itself does not need instance store. `fcvm setup` falls back to a sparse loopback image
formatted as btrfs when the mount point is not already btrfs (`src/setup/storage.rs`, root
required). What fcvm genuinely requires is **btrfs**, because clone disks are reflink CoW
copies; it refuses to run on a non-btrfs mount at that path. Instance store is what makes it
big and fast — 3.8 TB of local NVMe against a 100 GB gp3 root on ARM, for a workload that
writes multi-GB memory snapshots and a container image cache.

So a storeless type is unusable *until user_data grows that fallback*, which is a real option
if spot capacity for the `d` families ever gets tight. Until then it must not be launched: the
lists previously carried `c7g.metal`, `c5.metal` and `c6i.metal`, all
`InstanceStorageSupported=false`, and on 2026-08-15 a `c7g.metal` spot instance came up, died
in user_data with no disk to find, never registered, and billed while jobs queued. Verify any
addition before adding it:

```bash
aws ec2 describe-instance-types --instance-types <type> \
  --query 'InstanceTypes[].InstanceStorageSupported'
```

`case_every_launchable_type_has_instance_storage` in `scripts/test-runner-lambdas.py` fails
if a storeless type is added back.

**What holds a slot.** The cap counts runners that can *take work*, not EC2 instances that
exist. The Lambda reads the same PAT from SSM, lists `GET /repos/ejc3/fcvm/actions/runners`,
and counts an instance only if GitHub has `runner-<instance-id>` **online**, or the instance
is younger than **`BOOT_GRACE_MINUTES` (15)** and no registration is due yet — a `*.metal`
box spends minutes in POST before user_data starts, and without that window every poll would
launch another instance on top of a booting one. Because booting instances still count, the
cap still binds during a cold start and the herd can never exceed `MAX_RUNNERS` in flight.
A second ceiling, `MAX_RUNNERS + LAUNCH_HEADROOM (2)` **instances** per architecture
regardless of health, caps the blast radius if the health signal is ever wrong in the
"nothing is healthy" direction. If GitHub cannot be reached the Lambda **degrades to the
plain instance count**: over-counting only delays CI and self-heals next poll, while
under-counting launches metal spot instances on data it could not verify. A roster it
cannot fully read is the same case: a page short of `total_count`, a `total_count` that is
missing or not an integer, or a record without a usable `name` or `status`. Skipping such a record
kept the launcher up and quietly took one online runner out of the count, so a full pool
read as one short and metal was launched into it.

**Scale-up is observable.** Every decision prints one CloudWatch Embedded Metric Format
line (`event: runner_scale_decision`) carrying `QueuedJobs`, `HealthyRunners`,
`OnlineRunners`, `InstancesCounted`, `ScaleUpStarved`, `ZeroOnlineRunners`, the
`Architecture` dimension, and the reason — a structured log and a real metric in
`GitHubRunners` with no `PutMetricData` call and no extra IAM.

`ScaleUpStarved` is 1 whenever work was queued and the decision refused to add capacity.
It deliberately includes ordinary saturation: it was previously gated on the instance
ceiling blocking a launch that healthy-runner accounting would have allowed, and during
the 2026-08-07 collapse the two wedged ARM runners were GitHub-online the whole time, so
that gate held the metric at 0 for the entire incident it was written for. Nothing in a
single poll separates wedged from busy — a host that wedges mid-job keeps reporting
`busy=true` — so the discrimination is left to time: `runner-scale-up-starved` needs 24
consecutive polls (2 hours), which several waves of the longest measured job (43.5 min)
can also reach, and which is worth investigating either way.

`ZeroOnlineRunners` is 1 when jobs are queued and nothing is online *or booting* to take
them — the pool is empty rather than merely busy. That is the signal for a runner that
refused to register (no global IPv6, no instance-store NVMe), which is otherwise
indistinguishable from one that never launched. `runner-zero-online` fires after 2 polls;
it is suppressed while GitHub is unreachable, where `online` is 0 by construction and
`runner-pat-unusable` covers the gap.

**Registration.** The instance's user_data lives in SSM (`/github-runner/user-data`,
Advanced tier, base64+gzip — too big for Lambda's 4 KB env limit). On boot it sets up the box
(btrfs RAID0 over instance NVMe, `/dev/kvm` permissions, IPv6), reads only its controller-
brokered **registration token** from `/github-runner/bootstrap/<instance-id>`, and runs
`config.sh --url https://github.com/ejc3/fcvm --token <reg> --name runner-<instance-id>
--labels self-hosted,Linux,<ARM64|X64> --unattended --replace --ephemeral --disableupdate`. The controller
uses the PAT to call GitHub's registration-token endpoint; bootstrap has no PAT fallback.
Xtrace is switched off before reading the short-lived token. Older boots may still be
running the prior PAT-reading script until the separately verified drain.

After `config.sh`, bootstrap reads the identity GitHub assigned from
`/opt/actions-runner/.runner` (camelCase keys: `agentName` must equal `runner-<instance-id>`
and `agentId` must be a positive integer) and conditionally creates an item in the DynamoDB
table `github-runner-registration`, keyed by the instance ARN, with `State=registered`,
`InstanceId`, `RunnerName`, `RunnerId` and `RegisteredAt`, under the condition
`attribute_not_exists(InstanceArn)`. The runner service starts only after that write is
accepted, or after a consistent read following a lost answer returns that exact registered
item. If the cleanup Lambda got there first and the row says `State=reaping`, bootstrap
does not start the service and requests termination; controller cleanup owns GitHub
deregistration. Even after a successful claim, the token parameter must be deleted before
service startup. Registration and reaping
compete for one conditional create, so exactly one of them wins.

The launcher tags every new instance `RunnerRegistrationProtocol=ddb-v1`, which says which
handshake its user data runs and nothing about whether it succeeded. The tag is never
backfilled: an instance without it predates the handshake and stays on the rules below that
need no row.

Two of those setup steps are **gates, and they run before `config.sh`**: a runner that
cannot host a job must not join the pool, because from CI's side a broken runner is
indistinguishable from a code defect.

- **Instance store.** No instance-store NVMe means `/mnt/fcvm-btrfs` cannot be built, so the
  script refuses to register.
- **Global IPv6.** Assigning the address to the ENI is only half the job; the guest still has
  to acquire it over DHCPv6, on its own schedule. user_data assigns it (idempotently, with
  retries), runs `netplan apply` / `networkctl renew` to ask for it now rather than at the
  next renew, then polls until a usable address appears, using the same rule fcvm's
  `detect_host_ipv6` uses: a non-deprecated scope-global inet6 that is not a ULA, either /64
  or /128 with an on-link /64 route. No address within ~90s, no registration.

  Not hypothetical: on 2026-08-15 a job ran on a runner whose ENI held
  `2600:1f1c:208:c01::baca` while the OS had nothing, and every routed and IPv6 test in the
  fcvm suite failed with `No global IPv6 address found on host` — on a PR that had touched
  only `bench/chromium/*.py`.

`scripts/test-runner-userdata.sh` extracts the real heredoc from the `.tf` and checks that
both gates refuse the shapes they exist for and that each precedes registration; that the
`.runner` identity is read after `config.sh` and claimed before `svc.sh`; and, by executing
the registration tail against fake `aws`, `curl`, `config.sh` and `svc.sh`, that bootstrap
starts the service when it wins or when a lost answer reads back as its own item, and stops
without starting it when cleanup won or the row cannot be read. The fake `config.sh` writes
`.runner` in the runner's real camelCase shape.

**Reaping.** A second Lambda, `github-runner-cleanup`, runs every 5 minutes
(`rate(5 minutes)`). Its first pass over the fleet is EC2-only and terminates every instance
past the 13h30m hard ceiling before the PAT is read or GitHub is asked anything. It then
does nine things, six of them using the PAT: deregisters GitHub runners whose instance is
gone; renews the lease on busy runners (+60m), lets the lease of runners GitHub reports idle
expire, then terminates anything past its lease, re-reading that one runner by id and
deregistering it whenever the roster or its registration row supplied an id (instances
younger than 10 minutes are skipped so setup isn't interrupted); **holds** the lease of
runners GitHub said nothing usable about; reaps a `ddb-v1` instance that never claimed
registration by claiming `State=reaping` in its row once its lease has expired, unless the
roster lists that runner or it carries a `RunnerSeenAt` stamp or a renewed lease, which
mean the row was lost rather than never written; drains runners past the 12h soft cap;
**terminates any runner whose current job has been `in_progress` longer than
`MAX_JOB_RUNTIME_MINUTES` (180)**; terminates stray `ami-builder-temp` instances older than
2 hours (pure EC2, no PAT); reaps launches still `pending` after 15 minutes; and counts
GitHub's queued jobs to launch what the queue actually needs. GitHub doesn't redeliver
webhooks, so this poll is the retry, and it passes the per-architecture queued count along
so the decision record has the demand side too.

**Why job duration is the health signal.** A wedged host keeps its assigned job, so
`busy` stays true and the lease renews forever; the host is still online, so a liveness check
sees nothing either. GitHub enforces no server-side job-execution limit for self-hosted
runners, and the runner-side `timeout-minutes` default (360) runs *on the box* — exactly what
a wedged box cannot do. The job's own age is the one server-side signal that separates
"holds a job" from "makes progress", and it is per-job, so back-to-back healthy jobs never
accumulate toward it. 180 minutes is over 4x the longest legitimate self-hosted job observed
(43.5m, `Host-Root-arm64-SnapshotEnabled`). The scan is cheap because a job cannot start
before its run: only `in_progress` runs already older than the ceiling get a jobs lookup
(capped at 10 per poll), so steady state is one list call. Every GitHub error yields no
candidates, so an outage or rate limit reaps nothing. This is a control-plane check on
purpose — the runner AMI publishes no CloudWatch metrics, and an on-box guard (the pattern
`ejc3/fcvm` uses for disk in `runner-disk-guard.timer`) cannot be trusted to run on a host
whose scheduler is the thing that failed.

**A lease moves only on an answer.** The lease phase reads GitHub's runner roster and takes
one of three verdicts per instance. `RENEW` when GitHub reports the runner online and busy.
`EXPIRE` when GitHub answered with something that cannot mean "working" — `busy=false`, or
`busy=true` with the runner explicitly `offline` (that is the wedged host below), or no
record at all for an instance no poll has ever seen registered. `HOLD` for everything else:
the roster could not be read, or it could be read and omits a runner that some earlier poll
did see, or the record is `busy=true` with no `status` at all, which is a field GitHub did
not send rather than the wedged host. A held lease is not renewed and not allowed to expire
into a termination, so the instance keeps whatever expiry it already had and the age
ceiling remains the bound on it.

The distinction is the whole point (`ejc3/aws#45`). A missing runner record used to read as
`busy=false`, which is survivable for one blip — the lease gives 60 minutes of grace — and
fatal for an outage: once GitHub had been unreachable, or answering without the runner, for
longer than the remaining lease, every runner took the idle path and was terminated up to 60
minutes in, mid-job, while still under the soft cap. On GitHub that renders as "The
self-hosted runner lost communication with the server", indistinguishable from a spot
reclaim, which is the same misattributed failure `ejc3/fcvm#884` cost a night of reruns to.

A read counts as unread unless it is **complete**: the call has to succeed, the payload has
to carry a `runners` array, the pages collected have to reach the `total_count` GitHub
reports beside them, that `total_count` has to be present and an integer the check can
compare (without it a page-size heuristic accepts any short array as the whole roster), and
every record has to carry a usable `name` and `id`. A short page reads exactly like "that
runner is not registered", so truncation is the same fail-open arriving through pagination.
Reaching `total_count` is what ends a read: a roster of exactly ten full pages is complete,
not a list that ran past the page limit. `total_count` also has to be the same on every page
and at least what the pages hold, a name has to contain text, an id has to be a positive
integer (`True` is an `int` to Python, and neither it nor `0` is an id a DELETE URL can
carry), and no name or id may repeat: offset pagination hands the same record out twice when
a registration moves across a page boundary mid-read, and the runner it displaced is then
missing from a read that still adds up. The cleanup Lambda reads the roster twice and lets
no lease lapse on the roster if the two passes differ, because a roster that changed while it
was read is not evidence about which runners are absent. A `ddb-v1` instance with a
registered row is judged by its own by-id read instead, which an unread roster does not
affect.
A record that cannot be represented is not dropped, for the same reason: dropped, it still
counted toward `total_count`, so the read passed as complete and simply did not list that
runner, which is the never-registered `EXPIRE` for any instance without `RunnerSeenAt` (every
instance, on the first poll after that tag ships). One such record makes the whole roster
unread.

`RunnerSeenAt` is stamped on an instance on every poll whose roster listed its runner. It
separates "GitHub answered and does not list this runner" (ambiguous: a real deregistration
and an answer that dropped records look identical) from "this runner has never registered at
all" — a box that booted and never joined, because the IPv6 gate refused or the PAT did not
read back from SSM. The second is still reaped by the lease at ~60 minutes, and nothing else
would reap it: it sits in `running`, where the stalled-launch phase (which walks `pending`)
cannot see it. The tag also dates an outage: a held lease logs how long the runner has gone
unobserved, so a blip and a three-hour outage are different lines rather than the same one
repeated.

The stamp is a write, and a failed write must not make a later absence look like "never
joined". Two things keep one rejected `CreateTags` from deciding that. The lease renewal
carries `RunnerSeenAt` in the same call as `LeaseExpires`, so a runner whose lease was ever
renewed is marked seen by construction. And a `LeaseExpires` later than the instance's launch
time plus the 60-minute lease is itself proof: the launcher stamps the initial expiry before
`RunInstances`, so only `renew_lease()` writes a later one, and it runs only when GitHub
listed the runner online and busy. That second proof needs no separate write and covers
every runner that was busy before the tag shipped. What remains is an instance whose every
`CreateTags` failed since it registered; it has no renewed lease either, reads as never
registered, and dies at its lease. The stamp is also written only after the ceiling decision
for that instance, because a stalled `CreateTags` ahead of it would defer the one bound that
must run.

**A `ddb-v1` instance is judged by its row, not by the roster.** The cleanup Lambda reads
the instance's DynamoDB item with a consistent read. `State=registered` carries the runner
id bootstrap read from `.runner`; cleanup asks `GET /repos/ejc3/fcvm/actions/runners/<id>`
for that one runner and uses its `busy`/`status` only if the answer's id and name match.
A 404, a GitHub error, a malformed answer or a mismatched identity holds. No item is the one
thing the roster could never say for sure, that bootstrap has not registered: once the
launch lease has expired, cleanup conditionally creates `State=reaping` under the same key
and terminates only after that write is accepted, or after a lost answer reads back as its
own `reaping` item. A `registered` item created in the meantime wins, and the instance is
held. Absence is read as "never registered" only while nothing contradicts it: nothing in
the protocol deletes a row, so a missing row beside a roster that lists this runner, a
`RunnerSeenAt` stamp, or a lease later than the launcher's initial one is a row that was
LOST, and the instance is held to the ceiling instead of reaped. An unreadable table, a missing account id, or an item that does not describe this
instance is unread, and unread holds, up to the ceiling. The ceiling pass never consults
the table.

Instances without the tag are judged by the roster and the two registration signals above.
The never-registered `EXPIRE` for a readable roster that has never listed a legacy runner
is unchanged.

**A lease moves only on an accepted write.** `renewed` in the poll result names the leases
EC2 accepted a `CreateTags` for; a rejected write leaves the lease where it was and the
instance is reported as held.

**Two bounds keep a broken runner from becoming immortal.** Renewal treats a runner as busy
only while GitHub explicitly reports it `online` (a missing `status` renews nothing; the
lease is held), and no instance outlives the hard ceiling below. Both exist because `busy` means
"holds a job", not "makes progress": on 2026-08-07 two ARM runners wedged with ~490 leaked
`firecracker` processes and load averages of 389 and 523 (disk was fine at 46%/67%). They
kept their assigned jobs, so GitHub kept reporting `busy=true`, so the lease was renewed
every 5 minutes for 21 hours. They occupied 2 of the 4 ARM slots while doing no work, and
`get_running_runners` — which counts EC2 instances, not healthy runners — reported the pool
at max, so no replacement ever launched and CI sat queued.

**Runner lifetime: drain at 12h, hard ceiling at 13h30m.** `MAX_INSTANCE_AGE_HOURS` (12h) is
a soft cap. Past it the instance drains: it is terminated on the first poll that sees GitHub
report it idle, and a job already in flight is left to finish. `DRAIN_GRACE_MINUTES` (90) on
top of that is the hard ceiling, **13h30m, the absolute maximum lifetime of a runner
instance** — enforced regardless of busy state, regardless of whether GitHub answered,
regardless of a held lease, regardless of anything else, because it is computed from the
instance's launch time alone.
The poll runs every 5 minutes, so the observed maximum is 13h30m plus at most one poll
interval. A ceiling termination that never observed the runner idle logs `HARD-CEILING KILL`
with the GitHub state it saw, and reports it in the poll result as `hard_killed`; that line
is the only record distinguishing our termination from an AWS spot reclaim.

The soft cap is a deliberate **local policy**, not a platform limit: GitHub allows a
self-hosted job to run for up to 5 days (the 6h cap applies to GitHub-hosted runners
only), and these runners are not ephemeral, so one instance can legitimately chain many
short jobs past 12h of age. Every fcvm CI job finishes in well under 2 hours, so a 12h-old
runner has outlived its usefulness. Raise `MAX_INSTANCE_AGE_HOURS`, not the grace, if a
legitimately long job is ever added — the grace is sized to one job, not to a working day.

The grace exists because the cap used to terminate whatever the runner was doing. On
2026-08-28/29 it killed `i-09fff3a7d97fd4066` at 12.07h and `i-02fefa9deeb59e9c8` at 12.02h
with jobs in flight; EC2 recorded `User initiated` and the spot request said
`instance-terminated-by-user`, but on GitHub both read as "The self-hosted runner lost
communication with the server", indistinguishable from a spot reclaim, and it cost six
reruns in one night (`ejc3/fcvm#884`, symptom first recorded in `ejc3/fcvm#834`). 90 minutes
covers the longest job that can be in flight when a runner crosses the soft cap plus the poll
that notices it finished: the longest legitimate self-hosted job measured here is 43.5
minutes and a full matrix is about 35.

**A bound is only real if the sweep runs and reaches every instance.** The ceiling lives in
a pass of its own, the first thing the handler does after listing the fleet. It reads nothing
but each instance's id and launch time and terminates every instance over the ceiling before
the PAT is read from SSM, before GitHub is asked anything, and before any tag is written. A
per-instance ordering was not enough: a `CreateTags` for a younger instance earlier in the
response can stall for the rest of the Lambda's 240-second budget (boto3 retries a 60-second
read timeout), and so can the SSM read or a slow roster page, and an older instance later in
the response then never reaches its check. Nothing in this account alarms on Lambda errors,
so a deferred ceiling would be invisible. Five more things hold it open.

*Order.* The sweep is **Phase 1**, ahead of the orphan cleanup and the stuck-job scan. Both of
those are unbounded GitHub work (one EC2 describe per registered runner plus deregistrations
at a 10-second timeout; up to eleven calls at five seconds), and a Lambda timeout cannot be
caught, so anything slow ahead of the sweep means the age check simply never happens.

*Every termination goes through one `terminate()` helper*, including the stuck-job and
AMI-builder reaps, which used to call `ec2.terminate_instances` raw. One rejected call there
raised out of the handler and took every later phase with it, the queued-job launcher
included. A rejected call is now reported in `terminate_failed` with a `TERMINATE FAILED`
line naming the instance, and is never counted in `terminated`, because the instance is still
running and a poll that reads as a clean sweep would be lying.

*Terminate first, deregister second.* Deregistering and then failing to terminate is the
worst available outcome: GitHub's DELETE forces the runner out, which is how a job in flight
dies, and the instance is still up afterwards. The next poll would then find no GitHub record
for it, read it as unknown, drain it to the ceiling, and finally report it as work we may have
destroyed. The `HARD-CEILING KILL` line is likewise printed only once EC2 has accepted the
call, so it never claims a job is dead on a poll where the instance survived.

*`parse_ts` returns UTC-aware datetimes.* A GitHub timestamp with no offset used to produce a
naive one, and comparing it raised `TypeError` out of the whole invocation.

*The stuck-job scan is wrapped*, so losing it costs the stuck-job check and nothing else, and
*`get_runners` makes the whole roster unread on a record without a usable `name` or `id`*:
the orphan phase calls `.startswith()` on every key and formats the id straight into a DELETE
URL, and the idle path keys its fresh re-read on the id. An unread roster holds every lease
and leaves the ceiling as the bound.

*Each instance's turn through both loops is wrapped individually.* An instance whose age
cannot be computed has no safe verdict, so it is named with an `UNREADABLE INSTANCE RECORD`
line, which says outright that this instance is not covered by the bound until its record
reads cleanly, and every other instance is still swept.

**Three places that read absence as evidence, all fixed.** Terminating or deregistering a
runner is destructive, so it may only happen on evidence that is both fresh and positive.

`get_instance_state` distinguishes "EC2 says there is no such instance" from "EC2 could not be
asked": the orphan phase deregisters on the first and leaves the second alone, because one
transient `DescribeInstances` failure used to force a healthy runner out mid-job.

The never-registered path for a `ddb-v1` instance follows the same rule: a consistent
no-item read followed by an accepted conditional `State=reaping` create is positive
ownership of that instance's registration boundary, and only then is it terminated.

Even `InvalidInstanceID.NotFound` is not taken as proof on its own — AWS documents it as
transient after `RunInstances` — so the orphan phase first checks the instance against the set
of running runners the sweep listed moments earlier. An id in that set is demonstrably alive
whatever a per-id lookup says.

And an idle verdict past the soft cap is confirmed against a fresh single-runner read before
the instance is terminated, because the flags the poll is acting on were captured before the
rest of the invocation ran and GitHub hands out jobs the whole time. An unanswerable re-read
drains instead, and the ceiling bounds how long that lasts.

A draining runner is **not** deregistered while GitHub reports it busy. GitHub documents that
DELETE as forcing the runner's removal and does not say what happens to a job it is part-way
through, and a forced removal that ends the job is the same failure. What stops a drained
runner picking up new work is that the poll which first observes it idle terminates it, so
the exposure is at most one 5-minute interval after its job ends. A job started inside
that window and still running at the ceiling is still killed; that residual case is what the
`HARD-CEILING KILL` line is for. Closing it entirely would need an on-box graceful shutdown
(`Runner.Listener` finishing its job and exiting), which the Lambda has no channel to request.

**A spot capacity failure has to move the launcher to the next instance type.** It cannot
discover one by itself: `run_instances` returns an instance ID for a spot request AWS cannot
fulfil and terminates the instance afterwards, so no exception is ever raised and the
`except` branch that was supposed to advance the fallback list never fires. On 2026-08-07 the
poll launched `c5d.metal` at 18:41, 18:46 and 18:51; all three were still `pending` when AWS
marked them `instance-terminated-no-capacity` at 20:31, nearly two hours later, and
`c5.metal`, `c6i.metal` and `m5d.metal` were never tried at all. Three things close that
loop. A launch still `pending` after `STARTUP_TIMEOUT_MINUTES` (15) is a failed launch, not a
runner, so it stops counting toward the cap. The cleanup Lambda stamps `CapacityFailedAt` on
it and terminates it — the tag has to be written first, because terminating rewrites the
state reason to `Client.UserInitiatedShutdown` and the evidence would be lost. And the
launcher reads that record — AWS's own capacity state reasons, the tag, and anything
stalling right now — to move failed types to the back of the list. Back, not out: if every
type has failed the list is only reordered, so a launch is still attempted.

**The queue poll counts jobs, not a sample of runs.** It asks for runs with `status=queued`
*and* `status=in_progress`, pages both, pages each run's jobs, and counts the queued
self-hosted jobs themselves. Three bounds, each logged when it fires: a saturation exit
(both architectures already want at least as many runners as the pool holds, so more
scanning cannot change the launch), `QUEUE_SCAN_MAX_RUNS` (200) applied per status — per
status so phantom queued runs can never evict the in-progress runs from the scan — and a
120-second wall-clock budget, because a Lambda timeout is uncatchable and would kill the
whole poll; a truncated scan merely under-counts, and the next poll corrects it. Sampling the newest five `queued` runs hid real work two
ways. A run that is `in_progress` still holds queued jobs and the `queued` query does not
return it: run 31202629167 went `in_progress` at 17:40 on 2026-08-07 and kept two arm64 jobs
queued until 18:25, invisible for 45 minutes. And `ejc3/fcvm` carries six runs from
2026-08-06 that are permanently `status=queued` with **zero jobs** and cannot be cancelled,
force-cancelled or deleted through the API. Runs come back newest first, so those six sit
ahead of any older real work — more than the five-run sample held. The scan sends the whole
per-architecture deficit to the webhook Lambda as `launch_count` in a single invocation —
one runner per poll used to fill the pool at one runner every five minutes however deep
the queue was, and a burst of single-launch invocations could overshoot the cap, because
DescribeInstances is eventually consistent and each invocation can miss the instance the
previous one just launched. Inside one invocation the handler's own loop bounds the total,
and the webhook Lambda stays the single authority on the cap.


## Controller-first credential migration

The controller can classify both legacy PAT-reading user data and the instance-bound
bootstrap now published by this source. During the original migration, controller
deployment preceded bootstrap publication; the PAT grant and broad runner SSM attachment
remained until real CI acceptance and old-boot drain. This source contains the later
[gated IAM cutoff](#runner-iam-cutoff), so do not republish a legacy PAT-reading document.
The independent `iam:PassRole` escalation is closed: both controller
Lambdas may pass only `github-runner-instance-role`, only to EC2. Explicit denies
protect against another policy allowing any other role or service. The existing
launcher already uses exactly `github-runner-profile`, so this does not change jobs.

The launcher classifies the exact document fetched from SSM before allocating an
instance. Old scripts mint no unused bootstrap credential. For a broker script, it
first obtains a short-lived GitHub registration token, then launches one instance,
then creates `/github-runner/bootstrap/<instance-id>` as a SecureString with the
matching `InstanceArn`, `Role=github-runner`, and GitHub's `CredentialExpiresAt`.
The reusable PAT never enters user data or the job-host credential. Invalid documents
or unusable tokens refuse before launch. If publication fails after EC2 accepted a
launch, the controller attempts to terminate that exact instance and delete any
partially published parameter; it does not fall through to another instance type.

Future launches also receive IMDSv2-only metadata, encrypted disposable roots, and
runner-tagged volumes/ENIs so the later least-privilege launch policy can identify all
created resources. Instance-initiated shutdown terminates these disposable instances;
the later ephemeral bootstrap will use that after its single job. This stage changes
no existing instance, disk, runner registration, or service.

An instance can lose Spot capacity or fail early in cloud-init without ever consuming
its credential. The existing five-minute cleanup Lambda therefore scans **metadata
only**, verifies an exact instance-shaped name and matching controller tags, and deletes
only credentials whose recorded GitHub expiry has passed. It reads no token value,
does not infer expiry from an EC2 lookup failure, and ignores non-credential IAM canaries.
Missing/malformed provenance is held; read/parse/delete failures are logged. Each poll admits
at most two pages of 50 records and ten seconds of new cleanup requests; the dedicated
client uses two-second connect/read timeouts without retries. In-flight requests may
finish after that admission budget. Every instance's hard-ceiling pass finishes before
this optional I/O; cleanup failures do not suppress lease handling or queue retries.
The `runner_bootstrap_cleanup` log and invocation result report deleted names, errors,
and scan truncation. This bounded scan is not a guaranteed AWS deletion deadline:
repeated truncation or unprovable records require investigation. It never deletes a
record merely to make progress past that bound.

Deployment order remains controller and additive grant first, broker user data second,
then a real trusted CI registration/job acceptance and old-boot drain, and only then
retirement of the old runner PAT/SSM grant and broad controller launch permissions.
Keep a fresh reviewed plan between those gates. A token broker cannot be proved by an
IAM-only fixture check or by an additive apply that still serves the old script.

### Instance-bound, single-job bootstrap

New user data fetches only `/github-runner/bootstrap/<its-instance-id>`, with no PAT
fallback. It admits credential polling for at most three minutes and 36 attempts;
each AWS command has a twelve-second timeout (plus at most two seconds to kill a stuck
process), so an in-flight request may finish after the polling admission deadline.
It preserves the existing NVMe/IPv6 gates, validates GitHub's exact `.runner` identity,
and conditionally claims the same instance-ARN DynamoDB row before starting any service.

Bootstrap downloads runner `2.337.0` and verifies its architecture-specific SHA-256
against the [official release asset digests](https://github.com/actions/runner/releases/tag/v2.337.0)
before extraction. A failed download or checksum never reaches registration.
Registration uses `--ephemeral --disableupdate`, so automatic updates cannot replace the
verified wrapper. The credential is unset from the shell and its SSM
parameter must be successfully deleted before service installation/start; an uncertain
delete never starts a job. The service drop-in disables systemd restarts, bounds unknown
GitHub service-wrapper failures, and requests poweroff on service exit. The controller's
instance-initiated shutdown setting turns that into termination. Any failure in the
registration tail attempts bounded credential deletion and requests poweroff; earlier
NVMe/IPv6/download failures retain the controller's existing startup/lease reaping.
Controller cleanup remains the fallback if a shutdown request fails.

Verified against released runner `v2.337.0`, source commit
[`397b032`](https://github.com/actions/runner/tree/397b032cbf865e9c3ddfab89d533ec19325e1273):
[`Runner.cs`](https://github.com/actions/runner/blob/397b032cbf865e9c3ddfab89d533ec19325e1273/src/Runner.Listener/Runner.cs#L523)
returns success after an ephemeral job, and
[`RunnerService.js`](https://github.com/actions/runner/blob/397b032cbf865e9c3ddfab89d533ec19325e1273/src/Misc/layoutbin/RunnerService.js#L74)
stops on that exit. The environment setting stops unknown/signal exits after one
failure; known retry/update exits remain capped at ten consecutive attempts. The
standard `runsvc.sh` waits for that wrapper, allowing systemd's post-stop hook to run.

This is a reviewed update cadence, not a permanent version freeze. GitHub's
[runner update policy](https://docs.github.com/en/actions/reference/runners/self-hosted-runners#runner-software-updates-on-self-hosted-runners)
requires an upgrade within 30 days of any new release (including patch releases),
and can stop assigning jobs immediately for a required critical security update.
Monitor `actions/runner` releases. Before that deadline, update `RUNNER_VERSION` and
both official Linux asset digests together, verify the downloaded packages, rerun the
released wrapper's success/known-retry/unknown/signal exit tests and the offline bootstrap
suite, then review and Terraform-publish the new user data. Verify one trusted job and
its termination before considering the bump accepted. Release rollout is progressive;
also confirm the selected version is supported for `ejc3/fcvm`. Never bypass a checksum
or re-enable automatic updates to work around an unreviewed wrapper change.

The `Runner Release Freshness` workflow checks this source pin daily at 09:17 UTC,
only on `ejc3/aws` main, using a GitHub-hosted runner and the anonymous public release
API. Its checkout token is read-only and not persisted; it has no repository secrets,
AWS credentials, OIDC, PR trigger, or auto-upgrade/deploy step. A newer stable release
immediately fails the run with the first missed release's 30-day deadline and the
update procedure; malformed/unavailable/truncated history also fails visibly.
Failures use normal GitHub Actions notifications, whose delivery depends on the
operator's notification settings. This is a reminder, not guaranteed paging or a
substitute for critical-release monitoring: [scheduled runs can be delayed or dropped,
and public-repository inactivity can disable schedules](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
Check that the scheduled run stays enabled and recent. The check only reads git/public
releases, not deployed SSM, so a committed but unapplied pin still needs the deployment
and trusted-job acceptance above.

The bootstrap-only stage does not remove the old PAT permission: a job host can still
read that PAT until the separate IAM cutoff. Require a real trusted registration/job that proves
own-credential deletion, single-job exit, and EC2 termination, then verify old boots are
drained before removing the legacy grants. Retest the non-secret IAM canaries afterward.

### Runner IAM cutoff

The September 8 trusted broker acceptance succeeded: run `34266873459`, job
`102198382569`, instance `i-006e107132a388a2e`; its own credential was deleted before
job startup, the job succeeded, and instance-initiated shutdown terminated the host.
This historical result is not a fresh drain check or post-cutoff acceptance. The
September 12 fixture stage must be deployed and pass its before check separately.
This cutoff source preserves those temporary fixtures, the retired CI identities,
and current account controls; merging it is not proof of live deployment.

Do not deploy this stage before the broker bootstrap has passed a real trusted job,
its own credential deletion, service exit/EC2 termination, and the old-boot drain.
The unchanged instance-bound producer still grants only its source-instance SSM
credential and DynamoDB claim. The replacement runner policy explicitly denies every
non-bootstrap parameter read, plus all batch, history, and recursive-path reads.
These denies remain effective under an accidentally restored broad SSM Allow. Denying
only the PAT ARN is insufficient: [an ancestor recursive path can expose a denied child](https://docs.aws.amazon.com/systems-manager/latest/userguide/parameter-store-setting-up.html).
Bootstrap needs only the current value of its own parameter, never those bulk APIs.
SSM agent/session actions remain through `dev-ssm-managed-instance`; Terraform establishes
the denies first, creates this attachment before destroying the broad Core attachment,
and does not change the role/profile or the bootstrap/DynamoDB producer.

The controller's EC2 grants now require the own-account `Purpose=github-runner` images
already selected by its code, exact runner subnet/security group/keypair/profile, IMDSv2,
encrypted volumes, and `Role=github-runner` tags on every new instance, volume and ENI.
Tag-on-create is constrained by [EC2's service-supplied creation context](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/supported-iam-actions-tagging.html).
Outside launch, only the existing runner lease/health keys can change. An explicit deny
rejects every other tag key, including case variants of ownership tags, even under an
additive broad tag Allow. This prevents adopting an arbitrary dev/admin host or AMI.
Termination is limited to `Role=github-runner` instances and the existing cleanup
exception `Name=ami-builder-temp`; the controller cannot assign that Name to an existing
host. The runner role's IPv6 assignment is limited to tagged runner ENIs in its subnet.

Offline guards and rendered-policy IAM simulations are review evidence, not a real EC2
launch/SSM-agent test. Require the after-cutoff canaries (own read succeeds; peer single
and batch reads fail; simulated PAT access is explicitly denied), then another trusted
broker job to exercise the narrowed launch, IPv6, registration and teardown permissions.
The same before/after checker also makes strongly consistent DynamoDB `GetItem` calls
for only the two verified canary instance-ARN keys. It first requires both keys to be
absent, then requires each own-key read to succeed without an item and each peer-key
read to return `AccessDenied`. No `PutItem`, `DeleteItem`, scan, or real runner-record
read is performed; successful reads do not replace the real broker's write/claim gate.
Keep controller EC2 narrowing separate from PAT retirement if live launch acceptance
cannot establish that every boot uses the atomic tag protocol; never widen permissions
to work around an unverified launch failure. Remove the temporary fixtures through their
reviewed Terraform removal plan after both acceptance stages pass.

## What crosses the boundary (secrets inventory)

| Secret | Where it lives | Direction | Who reads it | Set / rotated by |
|--|--|--|--|--|
| **GitHub PAT** | `/github-runner/pat`, SSM `SecureString` | GitHub-issued, stored in AWS | `github-runner-lambda-role` after the gated IAM cutoff; legacy job-host access is removed in that stage | populated out of band; refresh can persist it in protected TF state despite `ignore_changes = [value]` |
| **Webhook HMAC** | `random_password.github_webhook` → Lambda env `WEBHOOK_SECRET` *and* the GitHub hook's `configuration.secret` | shared, both sides | the webhook Lambda; GitHub signs with it | Terraform generates it; both sides written in one apply. Rotate with `terraform apply -replace='random_password.github_webhook[0]'` |
| **Registration token** | controller-created instance-bound SSM parameter, deleted before job startup | GitHub-issued, short-lived | controller and that booting instance | GitHub API, ~1h lifetime; controller removes expired leftovers |
| **OIDC federation** | no secret — thumbprint pinned on the provider | GitHub asserts, AWS verifies | n/a | exact owner-approved environment trust on `github-actions-ami-builder` |
| **`dev_to_runner` SSH key** | private in SSM `SecureString` `/dev-servers/runner-ssh-key`, public baked into runner `authorized_keys` | AWS-internal (dev box → runner) | dev-server role fetches the private key | TF-generated `tls_private_key` (ED25519) |
| **`fcvm-ec2` keypair** | EC2 keypair `fcvm-ec2` (launch `KeyName`); public key baked into runner `authorized_keys` | AWS-internal (operator → runner) | whoever holds `~/.ssh/fcvm-ec2` (the jumpbox operator) | manual EC2 keypair, never rotated |
| **Webhook admin PAT** | `github-webhook-admin-pat`, Secrets Manager `us-west-1` | GitHub-issued, stored in AWS | the `integrations/github` provider only — no instance role can read it | manual; fine-grained PAT, one permission: `Webhooks: Read and write` on `ejc3/fcvm` |

The one credential GitHub itself holds for Pattern B is the webhook HMAC. Everything else is
either federated (Pattern A) or stored AWS-side and read through IAM.

**Three GitHub PATs, one job each, deliberately not interchangeable.** `github-pat-ejc3`
clones private repos from dev boxes, `/github-runner/pat` registers and reaps runners, and
`github-webhook-admin-pat` owns the webhook. The dev PAT is read by machines that run
other people's code; before the cutoff, the runner PAT was too. Neither may hold
webhook-write: that would let a compromised dev host or a leaked legacy runner token
repoint the launch endpoint. Measured 2026-08-07, both return 403 "Resource not accessible by
personal access token" on `GET /repos/ejc3/fcvm/hooks`, which is the correct answer.

## IAM boundaries — who can read what

- **`github-runner-instance-role`** (after cutoff): `dev-ssm-managed-instance` for
  SSM connectivity without account-wide parameter reads. Only the exact source-instance
  bootstrap credential can be read/deleted; reusable, peer, batch, path and history
  reads are explicitly denied. IPv6 assignment requires a tagged runner ENI in the
  runner subnet; interface metadata reads remain account-wide. `dynamodb:GetItem` + `dynamodb:PutItem` on
  `github-runner-registration`, restricted by `dynamodb:LeadingKeys` to
  `${ec2:SourceInstanceARN}`, so a runner can claim and read its own row and no other
  (no `Scan`, `Query`, `UpdateItem` or `DeleteItem`). Missing source-instance context
  cannot authorize an own-key claim.
- **`github-runner-lambda-role`** (after cutoff): own Lambda logs; EC2 metadata reads;
  launches restricted to approved tagged images, exact network/profile and tagged,
  encrypted IMDSv2 hosts; lease/health tags only after launch; termination of runners
  and the existing named temporary AMI builders only. `iam:PassRole` only on
  `github-runner-instance-role` and only to EC2, with explicit denies for other
  roles and services;
  `ssm:GetParameter` only on `/github-runner/pat` and `/github-runner/user-data`;
  `lambda:InvokeFunction` on the webhook function (for the cleanup retry); and
  `dynamodb:GetItem` + `dynamodb:PutItem` on the registration table, for the cleanup claim.
- **`github-actions-terraform`** (main and staging): explicit Deny `*`, no AWS
  authority, state, or secret payload access. Old CodeArtifact token/publisher
  resource-policy grants are removed; repositories and packages are retained.
- **`github-actions-ami-builder`** (Pattern A): the exact owner-approved environment
  and least-privilege build permissions above. No administrator delegation.

## Network posture

Runners live in an **isolated VPC** (`10.1.0.0/16`) with no peering to the dev VPC — a
single public `/24` (`10.1.1.0/24`) in **`us-west-1a` only**, internet gateway, dual-stack
IPv6, public IP on launch. That one AZ is the ceiling on the spot fallback: the launcher
walks several instance types but never another subnet/AZ, so a `us-west-1a` capacity gap
fails the launch outright (the cleanup poll is the only retry). The security group allows
**inbound SSH (22) from within the VPC** (`10.1.0.0/16` + the VPC's IPv6 block) **and the
operator's three static EIPs** (jumpbox + the two dev servers, so the `dev_to_runner` debug
path works) and all egress; shell access from anywhere else is via **SSM Session Manager**
(the runner role retains the parameter-free SSM connectivity policy). SSH is closed to the public internet
at large; everything else (webhook, registration, job dispatch) is runner-initiated outbound
to GitHub and the AWS APIs.

## Trade-offs and sharp edges

These are deliberate simplifications for a single-owner CI account, not recommendations to
copy blindly.

Closed (were sharp edges, now hardened):

- **The webhook fails closed and verifies every public request.** `verify_signature` rejects
  when `WEBHOOK_SECRET` is unset, and HMAC verification runs on everything arriving through
  API Gateway — identified by `requestContext`, which AWS sets and a caller can't forge. The
  shared secret is set on the GitHub `workflow_job` webhook and in the Lambda env, so an
  anonymous POST to `/webhook` no longer launches instances and no header skips verification
  (the old `x-internal-invoke: cleanup-retry` bypass is gone — cleanup retries are trusted
  by being direct `lambda:Invoke`, which carry no `requestContext`).
- **Both halves of that secret come from one Terraform value.** It used to be two hand-copied
  strings, a tfvar and a form field, with nothing checking they still matched — a real
  structural flaw, though NOT what killed the webhook. The measured root cause: the API
  Gateway route uses payload format 1.0, which preserves header casing, GitHub sends
  `X-Hub-Signature-256`, and the handler looked the header up in lowercase only — so it
  read the signature as absent and fail-closed 401'd every genuine delivery regardless of
  any secret (all 5,026 deliveries retained for hook 589197362 up to 2026-08-07 were 401
  or 503, zero 200). Whether the hand-copied secrets also drifted is unknowable from the
  outside — the case bug alone produces exactly this failure. Both are fixed: the handler
  normalizes header case, and the secret cannot drift again because there is only one.
  `random_password.github_webhook` feeds the Lambda env and the hook's
  `configuration.secret`, so there is no second copy to drift. This ownership begins at
  the one-time import + apply documented below — until that apply lands on a given state,
  the live hook still carries whatever secret it had before, and deliveries keep failing.
- **SSH is restricted to known hosts.** Port 22 is reachable from `10.1.0.0/16` (intra-VPC)
  and the operator's three static EIPs (jumpbox + the two dev servers) — not the public
  internet; shell access from anywhere else is via SSM Session Manager. The runners still run
  with `/dev/kvm` exposed and `iptables -P FORWARD ACCEPT`, so keeping them off the open
  internet matters.

Still open (accepted for now):

- **Controller PAT blast radius.** `/github-runner/pat` can register and remove runners
  on `ejc3/fcvm`; after cutoff only the controller reads it. The one-job host still
  executes privileged code and has an instance-bound AWS identity, so untrusted PRs
  must not be automatically approved for self-hosted CI.

## Operating it

### Temporary runner credential-boundary acceptance

This source removes the September 12 temporary IAM fixtures and their fixture-only
source guards, retaining the live checker and result tests. Deploy this cleanup only
after the before/after IAM checks and a real post-cutoff trusted job pass, including
own-token deletion before job startup and automatic host termination. A merge is not
proof that acceptance passed or that these billed resources were removed.

Only after those gates, inspect a fresh full plan and require **only these four
managed-resource destroys**, with no creates, updates, or replacements:

```text
aws_instance.runner_iam_canary["first"]
aws_instance.runner_iam_canary["second"]
aws_ssm_parameter.runner_iam_canary["first"]
aws_ssm_parameter.runner_iam_canary["second"]
```

These are two small Amazon Linux IAM-test hosts (`Role=runner-iam-canary`, no jobs,
repositories, registrations, or personal logins) and two literal non-credential
SecureStrings. Their two 8 GiB roots delete with the instances; no EIP, snapshot, backup,
runner role/profile, Lambda, network, DynamoDB table/row, or real CI host is changed.
After applying, verify both exact instances terminated, their root volumes deleted, and their two
`/github-runner/bootstrap/security-canary-20260912-*` parameters absent. A source merge
alone does not stop billing. `RemoveAfter=2026-09-13` is a removal reminder,
not an automatic expiry policy. Removing the pair stops approximately $0.032/hour of
instance/public-IPv4/gp3 charges at the original prices, excluding small API usage.

For a later acceptance window, restore the
[reviewed September 12 fixture template](https://github.com/ejc3/aws/blob/3bee7e0e63/runner-bootstrap-canary.tf)
in a separate Terraform change. Revalidate the pinned AMI and removal date, keep fixture
names aligned with the checker, and bind every replacement parameter to its new instance
ARN. Require a fresh four-create-only plan and verify the pair before testing. The
checker deliberately fails when fixtures are absent; never read a real PAT or
registration token as a substitute or reopen retired PAT grants to recreate a historical
before-cutoff result. Future post-cutoff checks use `--phase after`.

Run from the jumpbox after Terraform applies the four temporary resources and SSH is ready:

```bash
python3 scripts/check-runner-iam-canary.py --phase before
```

Each actual EC2 role must read only its own fixture and receive `AccessDenied` for its peer
through both `GetParameter` and batch `GetParameters`.
The test prints parameter **names**, never values. Reusable PAT access is checked with IAM
simulation only: `before` records the still-open permission; `after` requires an explicit
deny. Neither phase attempts to fetch the real PAT. The fixtures contain no registration
credential, and passing this check does not prove controller brokering or CI registration.

Deploy the backward-compatible controller first while retaining the old user-data/PAT
path; then publish broker user data and prove a real runner registers, deletes its own
one-host credential before accepting a job, and completes trusted CI. Only after old boots
have drained may the runner's broad SSM attachment/PAT grant be retired. Re-run with
`--phase after` and verify the SSM agent still checks in. Do not combine these deployment
gates into an unobserved single apply.

- All of Pattern B is gated on `var.enable_github_runner` — flip it to `false` to tear the
  self-hosted side down. Two applies now: `aws_dynamodb_table.runner_registration` sets
  `deletion_protection_enabled = true`, so clear that first. Replacing that table while
  runners are live removes the registration row of every running instance; none is reaped
  for it (a runner GitHub still lists, or one carrying `RunnerSeenAt`, is held to the
  ceiling) but every new boot fails closed until the table is back.
- Populate the PAT out of band; Terraform refresh can retain it in protected state
  even with `ignore_changes = [value]`. Never print it or grant CI backend access:
  `aws ssm put-parameter --name /github-runner/pat --value ghp_xxx --type SecureString --overwrite`.
- The webhook is Terraform's, both halves. It is `github_repository_webhook.runner`, pointed
  at `runner_webhook_url`, event `workflow_job`, secret generated by
  `random_password.github_webhook`. **Import the live hook once before the first apply on any
  state that doesn't already have it** — without this the apply adds a *second* hook to
  `ejc3/fcvm` delivering to the same URL, doubling every event:

  ```bash
  terraform import 'github_repository_webhook.runner[0]' fcvm/589197362
  ```

  Rotate the HMAC with `terraform apply -replace='random_password.github_webhook[0]'`; the
  same apply writes both sides.
- Pattern A requires the workflow's `permissions: id-token: write`, the dedicated
  `github-actions-ami-builder` role ARN, and `environment: runner-ami-publish`.
  Before enabling the publisher, restore and read back the environment using
  `.github/runner-ami-environment.json` and the recovery steps in `README.md`:
  only `ejc3` may approve, administrator bypass is disabled, and the sole deployment
  branch policy is `main` with no allowed tags. The role checks the exact environment
  OIDC subject; omitting the environment fails role assumption. No AWS repo secret
  is required.
