---
name: backup-restore
description: Find and restore files from the fleet's EBS backups (fcvm-metal-arm, fcvm-metal-x86, nextjs-ct, jumpbox home, jumpbox-2, claude-master-server) from a jumpbox, fast. Use when someone asks for an old file, folder or disk back, or whether a backup of a date exists.
---

# Restoring from the fleet backups

Written after ejc3/aws#988 (2026-11-22): an hour went into the wrong vaults, the only surviving point was a day from
deletion, and a full-disk transfer would have taken a day. The path below took minutes once known. Run it from a
jumpbox (administrator); a dev box cannot read the backups.

## 1. Where the points are

The live pipeline (README "Backups") backs each protected volume up daily into `ejc3-backup-processing` (us-west-1),
copies it to the recovery account's **logically air-gapped vault `ejc3-backup-recovery` in us-east-1**, and then
deletes the processing copy. So the us-west-1 vaults look nearly empty: **search the recovery vault first.**

```bash
# A profile for the recovery (dev-staging) account: each command assumes the role afresh from the instance role,
# so nothing expires mid-restore and no credential is exported or written down. Add once to ~/.aws/config:
#   [profile recovery]
#   role_arn = arn:aws:iam::<recovery account>:role/OrganizationAccountAccessRole
#   credential_source = Ec2InstanceMetadata
#   region = us-east-1
aws --profile recovery backup list-recovery-points-by-backup-vault --backup-vault-name ejc3-backup-recovery \
  --query "RecoveryPoints[?contains(ResourceArn,'<vol-id>')].[CreationDate,CalculatedLifecycle.DeleteAt,RecoveryPointArn]" \
  --output text | sort
```

- The recovery account id is `aws organizations list-accounts` (dev-staging). `<vol-id>` is the **source** volume:
  a box's root changes when it is moved or rebuilt, so check the volume it had on the date wanted (CloudTrail
  `CreateVolume`/`AttachVolume`, or `get-recovery-point-restore-metadata` on a candidate shows its `volumeId`).
- Retention, by the point's UTC date: the 1st of a month 365 days, other Sundays 30 days, other days 7 days. Losses
  are usually noticed late: the newest point **before** the loss is often a weekly or monthly one, and its
  `DeleteAt` may be close. Note it before anything else.
- AWS Backup's job history only reaches back about 30 days, so a "first job" date is not when backups began.
- Older points of pre-cutover volumes may still sit in us-west-1 `ejc3-backup` or as EBS snapshots there.

## 2. Beat the deadline first: restore to a volume

**Ask the owner first.** Everything from here creates resources by CLI (a volume, then a role, a security group, an
instance and a bucket in the recovery account), outside Terraform. That is the documented break-glass exception for
recovery, not a routine change: present the point found (its date and `DeleteAt`), what will be created and roughly
what it costs, and get the owner's approval in the current conversation before the first create. Tag everything with
the incident so the cleanup finds it.

A restored EBS volume outlives its recovery point, so this one call removes any expiry pressure. Restore in the
recovery account, with the settings the monthly restore tests use:

```bash
aws --profile recovery backup start-restore-job --recovery-point-arn <arn> --resource-type EBS \
  --iam-role-arn arn:aws:iam::<recovery account>:role/fleet-backup-detached-restore-test \
  --idempotency-token <issue>-<date> \
  --metadata '{"availabilityZone":"us-east-1a","volumeType":"gp3","encrypted":"true","kmsKeyId":"arn:aws:kms:us-east-1:<recovery account>:alias/aws/ebs"}'
aws --profile recovery backup describe-restore-job --restore-job-id <id>     # COMPLETED in about 10 minutes; CreatedResourceArn = the volume
aws --profile recovery ec2 create-tags --resources <vol> --tags Key=Incident,Value=<repo>-<issue>
```

The volume is encrypted with the recovery account's default EBS key, which cannot be shared across accounts.

## 3. Copy out only what is needed (do not move the disk)

**Do not snapshot the restored volume to move it.** Every block of a freshly restored volume is fetched from the
backup on first read, so the first snapshot of 800 GB crawled at about 8% in 100 minutes. Instead, mount it on a small
instance next to it and copy just the files:

1. **An outbound-only security group.** The account's default security group has **no egress rules** (CIS defaults),
   so an instance in it never reaches SSM. Create one with egress TCP 443 only and no ingress.
2. **A role with SSM and one write:** `AmazonSSMManagedInstanceCore`, plus `s3:PutObject` on a dedicated, private,
   tagged bucket in the recovery account.
3. **An instance in the volume's AZ:** `t4g.small`, current AL2023 arm64 (`/aws/service/ami-amazon-linux-latest/
   al2023-ami-kernel-default-arm64`), the default VPC's public subnet, IMDSv2 required, that role and that group,
   tagged with the incident. Attach the volume (`/dev/sdf`). SSM registration takes a minute or two.
4. **Mount read-only without replaying the journal:** `mount -o ro,noload /dev/nvme1n1p1 /mnt/restored` (the ext4
   root partition of the attached disk; `lsblk` shows it).
5. **Never walk the whole disk.** A `find` over a restored home stays in disk wait for tens of minutes. Read the
   metadata that names what you need. For git worktrees: `<repo>/.git/worktrees/*/gitdir` holds each worktree's path
   (`dirname` of its content); the checkouts are `~/*/.git`, `~/src/*/.git`, `~/*/*/.git`. That listed 324 `.scratch`
   folders in seconds.
6. **Keep only what is actually gone:** compare the list with the live box over ssh (jumpbox to dev box is allowed).
7. **Pack straight to S3:** `tar -C /mnt/restored -czf - -T list | aws s3 cp - s3://<bucket>/<name>.tar.gz
   --expected-size <bytes>`, all through SSM `send-command` (put long scripts in a file and pass them with `jq
   --rawfile`). Reading a restored disk runs at about 100 MB a minute; #988's 208 folders (3.9 GB packed, 11 GB
   unpacked) took about an hour. tar skips sockets, which is fine.
8. **Hand it to the dev box without new grants:** a presigned URL made immediately before the handoff (`aws --profile
   recovery s3 presign --expires-in 3600`; a URL lives no longer than the session that signed it), sent on stdin so it
   never sits in an argument list, into a private temporary file that is removed after unpacking:
   `printf 'url = "%s"\n' "$url" | ssh <box> 'umask 077; t=$(mktemp); curl -fsS -K - -o "$t" && mkdir -p
   ~/recovered-<what> && tar -C ~/recovered-<what> -xzf "$t"; rm -f "$t"'`. A dev box never gets a role, key or policy
   into the recovery account. Check the box's free disk first.

## 4. Clean up, and say so on the issue

Delete, in the recovery account: detach the restored volume (`detach-volume --force`) and terminate the instance,
then the security group, the instance profile and role (remove the role from the profile, delete the inline policy,
detach the managed one), the bucket (objects first), any snapshot or transfer KMS key left from an attempt to move the
whole disk (schedule the key's deletion), and the restored volume once the requester confirms. Access Analyzer reports a KMS key shared across accounts and CloudTrail reports the role creation:
both are your own steps, triaged as such in the watch's alert step.

Post on the issue: which point (date) was used, what was restored, where it is, what could not be recovered (anything
newer than the point), and that the temporary resources are gone.
