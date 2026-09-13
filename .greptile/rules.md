# ejc3/aws review context

This repository is the Terraform for one AWS account: EC2 dev and admin boxes, the GitHub
Actions runner autoscaler, the AWS Backup pipeline, IAM, and the scripts those hosts run.
`.greptile/files.json` attaches `AGENTS.md` to every review, `GITHUB-RUNNERS.md` to runner
and CI files, and `README.md` to the rollout-gate and operator-helper files.

## What a finding here needs

- `main` requires conversation resolution, so every inline comment blocks merge until
  someone answers it. Report a defect only when you can name the failure: the plan action
  (`must be replaced`, `destroy`), the AWS API error, the principal that gains which
  access, or the boot step that stops.
- Do not report pre-existing problems in code the diff does not change, either inline or
  in the summary. Report one only when the diff makes it reachable, copies it or makes it
  worse.
- CI runs `terraform fmt -check`, `init -backend=false`, `validate` and offline harnesses;
  nothing plans or applies before merge. `validate` parses HCL templates, so it already
  rejects an unescaped bash `${VAR}` or `${VAR:-default}` inside a heredoc. It does not
  parse the bash or Python inside heredocs, see shebang indentation, or check inner
  heredoc terminators.
- Before saying an AWS, EventBridge or provider feature is unsupported, cite current
  documentation.
- Check the "Do not flag" part of each rule before reporting.
- A PR that edits `.greptile/` or `scripts/test-greptile-config.py` changes how its own
  review runs. Report any edit that skips or narrows reviews, widens triggers, removes,
  disables or re-scopes a rule, or loosens the pin test.

## Known false positives

- `user_data = base64encode(<<-BOOTSTRAP ... )` on `aws_instance` delivers the script, not
  base64 text. hashicorp/aws 5.100.0 does not re-encode an already-base64 value, and
  `aws ec2 describe-instance-attribute --attribute userData`, decoded once, gives
  `#!/bin/bash` on the live boxes (#125). The same form is in `jumpbox.tf`, `jumpbox2.tf`,
  `nextjs-dev.tf`, `firecracker-dev.tf` and `x86-dev.tf`.
- An EventBridge `anything-but` pattern can take `suffix` with an array of values. A
  reviewer said otherwise on #106 from outdated documentation, and the owner rebutted it
  with the current AWS operator guide.

## Where things live

- The runner webhook and cleanup Lambdas are Python heredocs (`content = <<-EOF`) inside
  `runner-autoscale.tf`; no `.py` file holds them. The runner boot script is
  `local.runner_user_data` in the same file, stored with `base64gzip` in the Advanced-tier
  SSM parameter `/github-runner/user-data`.
- Box setup scripts are `local.arm_user_data` and `local.x86_user_data`
  (`dev-user-data.tf`), `local.nextjs_user_data` (`nextjs-user-data.tf`) and
  `local.jumpbox_2_user_data` (`jumpbox2-user-data.tf`). They are published to
  `s3://ejc3-dev-scripts` and fetched by a thin instance `user_data`. The instances ignore
  `user_data` changes. The two metal dev boxes re-run their published script through
  `dev-selfupdate.tf`; nextjs-dev and the jumpboxes are re-run by hand.
- Code that runs with administrator credentials before review: every `terraform plan` on a
  jumpbox runs `data "external" "google_redirect_uri"` (`scripts/check-google-redirect.sh`),
  and the Stop hook in `.claude/settings.json` runs `.claude/hooks/verify-consistent.sh`,
  which plans whatever branch the session has checked out.
- `aws_instance.jumpbox` uses `aws_security_group.firecracker_dev`, the security group of
  fcvm-metal-arm, so an ingress change for the ARM box also changes the jumpbox.
  `jumpbox2.tf` gives jumpbox-2 its own group for that reason.
- Backup pipeline: `backups.tf`, `backup-security.tf`, `backup-restore-canary.tf` and
  `scripts/backup-recovery.py`.
- Offline harnesses are `scripts/test-*.py` and `scripts/test-*.sh`.
  `.github/workflows/lambda-tests.yml` runs all of them with `contents: read` except
  `scripts/test-ci-security.py`, which only `drift.yml` runs.
- Trust runs one way: phone -> jumpbox -> dev box, never dev box -> jumpbox.

## Measured limits

- On origin/main 2073aa4 (2026-09-13), `base64gzip(local.runner_user_data)` is 7,584
  characters, with both Terraform interpolations padded to 40 characters, against the
  8,192-character Advanced-tier limit: about 600 characters of headroom. The 4,712 and
  "about 6,100" figures in the comment beside `aws_ssm_parameter.runner_user_data` are
  older than the current script.
- The inline policies on `dev-server-role` totalled 8,679 non-whitespace characters in a
  state snapshot taken on 2026-09-13, against IAM's 10,240-character aggregate limit for
  one role.

## Published on purpose

This repository is public and documents its own infrastructure. None of these is a leak or
a hardcoding defect:

- AWS account IDs `928413605543` and `249042068453` (tests assert them) and the Cloudflare
  account ID `12ea67fb7ced068de03f35c22688e436`
- Elastic IPs and the `eipalloc-` and `vol-` IDs in `AGENTS.md`, the VPC CIDRs
  `10.0.0.0/16`, `10.1.0.0/16` and `172.31.0.0/16`, and io-box's `172.31.48.10`
- hostnames under `cc-games.dev` and `dolphin-labs.dev`, SSH public keys, pinned SHA-256
  digests, Cloudflare permission-group IDs, and the names and ARNs of secrets and
  parameters
- test fixtures such as `ghp_test` and `111111111111`

The kids' full passwordless sudo on nextjs-dev is deliberate: the instance is the sandbox.

## Incidents behind the rules

- `aws-dev-box-never-reaches-jumpbox`: `pbox-key.tf` reintroduced a dev -> jumpbox path
  with a forced-command key shortly after `dev-hop-key.tf` removed `~/.ssh/fcvm-ec2` from
  the dev boxes, and it survived because "it can only run one script" sounded safe. Every
  delegation transport (forced command, SSM, a polled queue, a branch a jumpbox plans) has
  the same blast radius.
- `aws-iam-no-silent-widening`: on #124, CodeRabbit found `ec2:DescribeVolumes` on `"*"`
  in `backup-security.tf` with no region condition, although its caller uses one region.
  Both `RunInstances` tag-condition traps in `parallel-box-launch.tf` failed for every
  instance type and read as a spot capacity drought.
- `aws-secrets-never-xtraced-or-published`: `dev-instance-common.tf`,
  `jumpbox2-user-data.tf` and `runner-autoscale.tf` turn xtrace off before reading a
  secret, and #125 did the same for the `fcvm-ec2` key on the admin boxes.
- `aws-no-committed-credentials`: the repository is public, and `AGENTS.md` shows
  token-minting commands next to where a real value could be pasted. Markdown, workflows
  and `browser-manager/` are outside the xtrace rule's scope.
- `aws-persistent-box-replacement-safety`: on #124 the non-empty
  `firecracker_move_from_instance_id` default kept a dated image selected for any later
  replacement. On io-box, `associate_public_ip_address` on a stopped box planned "2 to add,
  2 to destroy". The Cloudflare account token is IP-pinned to the jumpbox addresses, whose
  Elastic IPs carry no `prevent_destroy`.
- `aws-ci-stays-credential-free`: the shared CI roles are retired with `Deny *`; a full
  plan reads credential-bearing state, so GitHub runs validation and offline harnesses
  only, and those are the only checks before merge.
- `aws-embedded-code-has-a-failing-case`: on 2026-08-07 the untested inline runner Lambdas
  cost hours of queued CI; on 2026-08-15 a runner registered with no global IPv6.
- `aws-apply-time-limits-validate-cannot-catch`: #116's ARM move set a shutdown behaviour
  AWS refuses to modify on spot instances, so the create failed right after
  `RunInstances` and left the new instance tainted (#117). #122 renamed an SSM parameter
  because names starting with `aws` are reserved. The runner user data once reached 11,184
  base64 characters against the 8,192-character Advanced-tier limit.
- `aws-unattended-boot-script-robustness`: a `grep` with no match on a storeless instance
  type killed the runner bootstrap under `set -e`; an indented shebang made the kernel run
  `/bin/sh`; the `runner_key` block in `~/.ssh/config` grew to three identical copies.
- `aws-rollout-gates-match-their-runbook`: on #106 a review found
  `security_posture_enabled` switched on while the same revision recorded alert delivery as
  unaccepted, and the owner held the merge until receipt was confirmed. On #103 the ndev
  hostname and worktree behaviour changed without the README runbook.
