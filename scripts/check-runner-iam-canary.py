#!/usr/bin/env python3
"""Read-only runner IAM acceptance from the jumpbox; never reads a real token.

The two fixtures contain only the literal security-canary-not-a-credential.
Real EC2-role GetParameter calls return only Parameter.Name. The runner PAT is
tested solely with IAM simulation, not GetParameter, GetParameters or history.
"""

import argparse
import ipaddress
import shlex
import subprocess

ACCOUNT = "928413605543"
REGION = "us-west-1"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/github-runner-instance-role"
PURPOSE = "runner-bootstrap-iam-canary"
PREFIX = "/github-runner/bootstrap/security-canary-20260912-"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def accepted_parameter_result(result, name, allowed, operation="GetParameter"):
    if allowed:
        return result.returncode == 0 and result.stdout.strip() == name
    # A missing parameter, network error, missing CLI, or SSH failure is not a
    # successful deny test. Never print the raw response as acceptance evidence.
    return (result.returncode not in (0, 255)
            and f"An error occurred (AccessDeniedException) when calling the {operation} operation"
            in result.stderr)


def instance_aws(ip, key, args):
    ipaddress.IPv4Address(ip)
    command = [
        "env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/home/ec2-user",
        "AWS_CONFIG_FILE=/dev/null", "AWS_SHARED_CREDENTIALS_FILE=/dev/null",
        "AWS_EC2_METADATA_DISABLED=false", "AWS_PAGER=",
        "aws", "--region", REGION, "--cli-connect-timeout", "10",
        "--cli-read-timeout", "15", *args,
    ]
    return subprocess.run([
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new", "-i", key,
        f"ec2-user@{ip}", shlex.join(command),
    ], capture_output=True, text=True, timeout=45, check=False)


def check(phase, key):
    import boto3

    require(boto3.client("sts").get_caller_identity()["Account"] == ACCOUNT,
            "This acceptance belongs to the main AWS account only")
    ec2 = boto3.client("ec2", region_name=REGION)
    ssm = boto3.client("ssm", region_name=REGION)
    iam = boto3.client("iam")
    response = ec2.describe_instances(Filters=[
        {"Name": "tag:Purpose", "Values": [PURPOSE]},
        {"Name": "tag:Role", "Values": ["runner-iam-canary"]},
        {"Name": "instance-state-name", "Values": ["running"]},
    ])
    instances = [i for r in response["Reservations"] for i in r["Instances"]]
    require(len(instances) == 2, "Expected exactly two running Terraform IAM canaries")
    by_name = {dict((t["Key"], t["Value"]) for t in i["Tags"])["Name"]: i
               for i in instances}
    require(set(by_name) == {"runner-iam-canary-first", "runner-iam-canary-second"},
            "Canary instance names are not the exact expected pair")

    for label in ("first", "second"):
        instance = by_name[f"runner-iam-canary-{label}"]
        instance_id = instance["InstanceId"]
        source_arn = f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{instance_id}"
        require(instance.get("IamInstanceProfile", {}).get("Arn") ==
                f"arn:aws:iam::{ACCOUNT}:instance-profile/github-runner-profile",
                "Canary does not use the real runner instance profile")
        name = PREFIX + label
        metadata = ssm.describe_parameters(ParameterFilters=[
            {"Key": "Name", "Option": "Equals", "Values": [name]},
        ])["Parameters"]
        require(len(metadata) == 1 and metadata[0]["Type"] == "SecureString",
                "Expected non-credential SecureString fixture is absent")
        tags = {t["Key"]: t["Value"] for t in ssm.list_tags_for_resource(
            ResourceType="Parameter", ResourceId=name)["TagList"]}
        require(tags.get("InstanceArn") == source_arn and tags.get("Purpose") == PURPOSE,
                "Fixture is not bound to its exact current canary instance")

    for label in ("first", "second"):
        instance = by_name[f"runner-iam-canary-{label}"]
        instance_id = instance["InstanceId"]
        ip = instance["PublicIpAddress"]
        identity = instance_aws(ip, key, ["sts", "get-caller-identity", "--query", "Arn", "--output", "text"])
        require(identity.returncode == 0 and identity.stdout.strip() ==
                f"arn:aws:sts::{ACCOUNT}:assumed-role/github-runner-instance-role/{instance_id}",
                "SSH command is not using this canary's own EC2 instance-role credentials")
        for target in ("first", "second"):
            name = PREFIX + target
            result = instance_aws(ip, key, [
                "ssm", "get-parameter", "--name", name, "--with-decryption",
                "--query", "Parameter.Name", "--output", "text",
            ])
            allowed = label == target
            require(accepted_parameter_result(result, name, allowed),
                    f"{label} -> {target}: expected {'allow' if allowed else 'AccessDenied'}")
            print(f"PASS real EC2 role: {label} -> {target}: {'allow' if allowed else 'deny'}")

        # Batch access must not evade the peer boundary. This request also names
        # only a non-credential fixture, never a real bootstrap token or PAT.
        peer = PREFIX + ("second" if label == "first" else "first")
        batch = instance_aws(ip, key, [
            "ssm", "get-parameters", "--names", peer, "--with-decryption",
            "--query", "Parameters[].Name", "--output", "text",
        ])
        require(accepted_parameter_result(batch, peer, False, "GetParameters"),
                f"{label}: batch peer request did not return AccessDenied")
        print(f"PASS real EC2 role: {label}: batch peer access = deny")

        # Deliberately never make a real request for /github-runner/pat.
        simulation = iam.simulate_principal_policy(
            PolicySourceArn=ROLE,
            ActionNames=["ssm:GetParameter", "ssm:GetParameters"],
            ResourceArns=[f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter/github-runner/pat"],
            ContextEntries=[{
                "ContextKeyName": "ec2:SourceInstanceARN",
                "ContextKeyType": "string",
                "ContextKeyValues": [f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{instance_id}"],
            }],
        )["EvaluationResults"]
        expected = "allowed" if phase == "before" else "explicitDeny"
        require(len(simulation) == 2 and all(r["EvalDecision"] == expected for r in simulation),
                f"{label}: reusable PAT policy did not return the expected {phase}-cutoff decision")
        print(f"PASS simulation only: {label}: reusable PAT access = {expected}")
    print("IAM canary passed. This does not prove controller brokering or a real CI job.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("before", "after"), required=True)
    parser.add_argument("--ssh-key", default="/home/ubuntu/.ssh/fcvm-ec2")
    args = parser.parse_args()
    check(args.phase, args.ssh_key)
