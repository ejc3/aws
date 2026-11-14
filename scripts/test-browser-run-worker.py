#!/usr/bin/env python3
"""Render the actual Terraform policy with fixture inputs and exercise its boundary.

Runs Terraform's built-in console in an empty, credential-free module. It needs
no installed providers, AWS credentials, network or Terraform backend.
"""
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "123456789012"
SECRET = f"arn:aws:secretsmanager:us-west-1:{ACCOUNT}:secret:cloudflare-browser-run-worker-test"
WORKER = f"arn:aws:iam::{ACCOUNT}:role/fcvm-benchmark-worker"
ADMIN = f"arn:aws:iam::{ACCOUNT}:role/jumpbox-admin-role"
VPC = "vpc-0123456789abcd000"
PRIVATE_IPV4 = "10.12.1.25"
PUBLIC_IPV4 = "192.0.2.30"
PUBLIC_IPV6 = "2001:db8::23/128"
READ = "secretsmanager:GetSecretValue"
BATCH = "secretsmanager:BatchGetSecretValue"


def render():
    source = (ROOT / "browser-run-benchmark-worker.tf").read_text()
    locals_block = re.search(r"^locals \{\n(.*?)^\}", source, re.M | re.S)[1]
    for original, fixture in (
        ("local.benchmark_worker_enabled", "enabled"),
        ("aws_iam_role.benchmark_worker[0].arn", "role"),
        ("local.benchmark_worker_vpc_id", "vpc"),
        ("local.benchmark_worker_private_ipv4", "private_ipv4"),
        ("local.benchmark_worker_public_ipv4", "public_ipv4"),
        ("local.benchmark_worker_ipv6_cidr", "public_ipv6"),
        ("try(aws_secretsmanager_secret.cloudflare_browser_run_worker[0].arn, null)", "secret"),
    ):
        locals_block = locals_block.replace(original, "var." + fixture)
    inputs = {
        "role": WORKER,
        "vpc": VPC,
        "private_ipv4": PRIVATE_IPV4,
        "public_ipv4": PUBLIC_IPV4,
        "public_ipv6": PUBLIC_IPV6,
        "secret": SECRET,
    }
    locals_block += f"\n  games_mp_admin_principals = {json.dumps([ADMIN])}\n"
    locals_block += f"  browser_run_read_actions = {json.dumps([READ, BATCH])}\n"
    text = 'variable "enabled" {\n  type = bool\n}\n'
    text += "".join(
        f"variable {json.dumps(name)} {{\n  default = {json.dumps(value)}\n}}\n"
        for name, value in inputs.items()
    )
    text += "\nlocals {\n" + locals_block + "\n}\n"
    result = {}
    with tempfile.TemporaryDirectory(prefix="browser-run-worker-test-") as tmp:
        Path(tmp, "main.tf").write_text(text)
        for enabled in ("true", "false"):
            command = subprocess.run(
                ["terraform", "console", "-no-color", f"-var=enabled={enabled}"],
                input="jsonencode({ policy = jsondecode(local.browser_run_worker_policy_json), pins = local.browser_run_worker_ips })\n",
                capture_output=True, text=True, cwd=tmp, check=True, timeout=10,
            )
            if command.stderr.strip():
                raise RuntimeError(command.stderr)
            rendered = command.stdout.strip()
            if rendered.startswith("<<"):
                rendered = rendered.split("\n", 1)[1].rsplit("\n", 1)[0]
            else:
                rendered = json.loads(rendered)
            result[enabled] = json.loads(rendered)
    return result


def decision(policy, principal, action, vpc=None, private_ip=None):
    """Interpret only this policy's concrete operators; unknown ones fail the test."""
    request = {
        "aws:PrincipalArn": principal,
        "aws:Ec2InstanceSourceVpc": vpc,
        "aws:Ec2InstanceSourcePrivateIPv4": private_ip,
    }

    def condition(op, actual, expected):
        if op in ("ArnEquals", "StringEquals"):
            return actual == expected
        if op == "ArnNotLike":
            return actual not in expected  # fixture principals have no wildcards
        if op == "StringNotEqualsIfExists":
            return actual != expected
        if op == "IpAddress":
            return actual is not None and ipaddress.ip_address(actual) in ipaddress.ip_network(expected)
        if op == "NotIpAddressIfExists":
            return actual is None or ipaddress.ip_address(actual) not in ipaddress.ip_network(expected)
        raise AssertionError(op)

    effects = set()
    for statement in policy["Statement"]:
        actions = statement["Action"]
        principal_match = statement["Principal"] == "*" or statement["Principal"] == {"AWS": principal}
        if not principal_match or action not in ([actions] if isinstance(actions, str) else actions):
            continue
        if statement["Resource"] != SECRET:
            continue
        if all(condition(op, request[key], arg) for op, keys in statement["Condition"].items()
               for key, arg in keys.items()):
            effects.add(statement["Effect"])
    return "Deny" if "Deny" in effects else "Allow" if "Allow" in effects else "NoGrant"


class BenchmarkBrowserWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rendered = render()
        cls.active = cls.rendered["true"]
        cls.policy = cls.active["policy"]

    def test_only_direct_worker_identity_has_a_get_grant(self):
        self.assertEqual(decision(self.policy, WORKER, READ, VPC, PRIVATE_IPV4), "Allow")
        self.assertEqual(decision(self.policy, WORKER, BATCH, VPC, PRIVATE_IPV4), "NoGrant")
        self.assertEqual(decision(self.policy, ADMIN, READ), "NoGrant")  # not denied; admin IAM applies
        self.assertEqual(decision(self.policy, ADMIN, BATCH), "NoGrant")
        for role in ("dev-server-role", "nextjs-dev-role", "dev-ebs-only-role", "member-role"):
            for action in (READ, BATCH):
                who = f"arn:aws:iam::{ACCOUNT}:role/{role}"
                self.assertEqual(decision(self.policy, who, action, VPC, PRIVATE_IPV4), "Deny")

    def test_either_missing_or_wrong_credential_origin_is_specifically_denied(self):
        for action in (READ, BATCH):
            for vpc, ip in ((None, None), (None, PRIVATE_IPV4), (VPC, None),
                            ("vpc-other", PRIVATE_IPV4), (VPC, "10.12.1.26")):
                with self.subTest(action=action, vpc=vpc, ip=ip):
                    self.assertEqual(decision(self.policy, WORKER, action, vpc, ip), "Deny")

    def test_fixed_worker_address_pair_and_no_config_when_disabled(self):
        self.assertEqual(self.active["pins"], [f"{PUBLIC_IPV4}/32", PUBLIC_IPV6])
        self.assertEqual(self.rendered["false"]["pins"], [])
        self.assertEqual(self.rendered["false"]["policy"]["Statement"], [])


if __name__ == "__main__":
    unittest.main(buffer=True)
