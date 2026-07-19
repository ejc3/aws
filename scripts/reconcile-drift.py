#!/usr/bin/env python3
"""Find AWS resources that exist but are NOT managed by terraform.

Why this exists: `terraform plan` only reports drift for resources terraform already
knows about. Anything created outside terraform is completely invisible to it -- which
is exactly how the hand-made `fcvm-backups` vault sat unmanaged and unnoticed. This
enumerates real AWS resources and diffs them against terraform state.

Run from the repo root:  python3 scripts/reconcile-drift.py
Exit code 1 if unmanaged resources are found (so it can gate a check).
"""
import json
import subprocess
import sys

REGIONS = ["us-west-1", "us-west-2"]

# Resources AWS creates for you, or that are managed elsewhere on purpose.
IGNORE_SUBSTRINGS = (
    "AWSServiceRole",                     # service-linked roles
    "AWSBackupDefaultServiceRole",        # created by AWS Backup
    "AWSDataLifecycleManagerDefaultRole", # created by DLM
    "AWSReservedSSO",                     # IAM Identity Center
    "OrganizationAccountAccessRole",      # created with the member account
    "aws-service-role",
    "default",                            # default VPC/SG/subnets/route tables
    "ejc3-terraform-state",               # the state bucket itself (bootstrap chicken-and-egg)
    "ejc3-terraform-locks",
)


def sh(args):
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return r.stdout


def terraform_ids():
    """Every id/arn/name terraform has in state."""
    out = sh(["terraform", "show", "-json"])
    if not out:
        print("ERROR: could not read terraform state", file=sys.stderr)
        sys.exit(2)
    data = json.loads(out)
    ids = set()

    def walk(module):
        for res in module.get("resources", []):
            vals = res.get("values") or {}
            for key in ("id", "arn", "name", "bucket", "key_name",
                        "function_name", "host_id", "allocation_id"):
                v = vals.get(key)
                if isinstance(v, str) and v:
                    ids.add(v)
        for child in module.get("child_modules", []):
            walk(child)

    walk(data.get("values", {}).get("root_module", {}))
    return ids


def aws(region, *args):
    out = sh(["aws", "--region", region] + list(args) + ["--output", "json"])
    if not out:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return []


def collect(region):
    """(kind, identifier, label) for live resources worth tracking."""
    found = []

    for r in aws(region, "ec2", "describe-instances",
                 "--query", "Reservations[].Instances[?State.Name!='terminated'].[InstanceId,Tags[?Key=='Name']|[0].Value]"):
        for i in r:
            found.append(("ec2-instance", i[0], i[1] or "-"))

    for v in aws(region, "ec2", "describe-volumes", "--query", "Volumes[].[VolumeId,Tags[?Key=='Name']|[0].Value]"):
        found.append(("ebs-volume", v[0], v[1] or "-"))

    for s in aws(region, "ec2", "describe-security-groups", "--query", "SecurityGroups[].[GroupId,GroupName]"):
        found.append(("security-group", s[0], s[1]))

    for e in aws(region, "ec2", "describe-addresses", "--query", "Addresses[].[AllocationId,PublicIp]"):
        found.append(("elastic-ip", e[0], e[1]))

    for k in aws(region, "ec2", "describe-key-pairs", "--query", "KeyPairs[].[KeyPairId,KeyName]"):
        found.append(("key-pair", k[1], k[1]))

    for h in aws(region, "ec2", "describe-hosts", "--query", "Hosts[].[HostId,HostProperties.InstanceType]"):
        found.append(("dedicated-host", h[0], h[1]))

    for f in aws(region, "lambda", "list-functions", "--query", "Functions[].[FunctionName]"):
        found.append(("lambda", f[0], f[0]))

    for v in aws(region, "backup", "list-backup-vaults", "--query", "BackupVaultList[].[BackupVaultName]"):
        found.append(("backup-vault", v[0], v[0]))

    for p in aws(region, "backup", "list-backup-plans", "--query", "BackupPlansList[].[BackupPlanId,BackupPlanName]"):
        found.append(("backup-plan", p[0], p[1]))

    for t in aws(region, "sns", "list-topics", "--query", "Topics[].[TopicArn]"):
        found.append(("sns-topic", t[0], t[0].rsplit(":", 1)[-1]))

    for p in aws(region, "ssm", "describe-parameters", "--query", "Parameters[].[Name]"):
        found.append(("ssm-parameter", p[0], p[0]))

    return found


def collect_global():
    found = []
    for r in aws("us-east-1", "iam", "list-roles", "--query", "Roles[].[RoleName]"):
        found.append(("iam-role", r[0], r[0]))
    for b in aws("us-east-1", "s3api", "list-buckets", "--query", "Buckets[].[Name]"):
        found.append(("s3-bucket", b[0], b[0]))
    for o in aws("us-east-1", "iam", "list-open-id-connect-providers", "--query", "OpenIDConnectProviderList[].[Arn]"):
        found.append(("oidc-provider", o[0], o[0].rsplit("/", 1)[-1]))
    return found


def ignored(identifier, label):
    blob = f"{identifier} {label}"
    return any(s in blob for s in IGNORE_SUBSTRINGS)


def main():
    managed = terraform_ids()
    print(f"terraform state knows {len(managed)} ids/arns/names\n")

    unmanaged = []
    for region in REGIONS:
        for kind, ident, label in collect(region):
            if ident in managed or label in managed or ignored(ident, label):
                continue
            unmanaged.append((region, kind, ident, label))
    for kind, ident, label in collect_global():
        if ident in managed or label in managed or ignored(ident, label):
            continue
        unmanaged.append(("global", kind, ident, label))

    if not unmanaged:
        print("✅ no unmanaged resources found")
        return 0

    print(f"⚠️  {len(unmanaged)} resource(s) exist in AWS but are NOT in terraform state:\n")
    for region, kind, ident, label in sorted(unmanaged):
        print(f"  [{region:<9}] {kind:<16} {ident:<45} {label}")
    print("\nEach is either drift to import/codify, or belongs on the ignore list.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
