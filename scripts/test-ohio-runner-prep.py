#!/usr/bin/env python3
"""ohio.tf and scripts/ami-replicator.py: Ohio is prepared and IDLE (nothing points at it, nothing costs money while idle),
its runner network mirrors the us-west-1 one, and the runner AMIs are copied and kept current. Offline."""
import importlib.util
import ipaddress
import json
import os
import re
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "ohio.tf").read_text()
VPC = (ROOT / "runner-vpc.tf").read_text()
sys.modules.setdefault("boto3", types.ModuleType("boto3"))
SPEC = importlib.util.spec_from_file_location("rep", ROOT / "scripts" / "ami-replicator.py")
rep = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rep)


def img(i, arch="arm64", date="2026-09-08", state="available", tags=()):
    return {"ImageId": i, "Name": "n-" + i, "Architecture": arch, "CreationDate": date + "T00:00:00.000Z", "State": state,
            "Tags": [{"Key": k, "Value": v} for k, v in tags]}


class FakeEC2:
    def __init__(self, images):
        self.images, self.copies, self.tags = images, [], []

    def describe_images(self, Owners, Filters):
        assert Owners == ["self"] and {"Name": "tag:Purpose", "Values": ["github-runner"]} in Filters
        return {"Images": list(self.images)}

    def copy_image(self, **kw):
        self.copies.append(kw)
        return {"ImageId": "ami-new%d" % len(self.copies)}

    def create_tags(self, Resources, Tags):
        self.tags.append((Resources, {t["Key"]: t["Value"] for t in Tags}))


class ReplicatorTests(unittest.TestCase):
    def run_it(self, src, dst, event=None, fail=False):
        s, d = FakeEC2(src), FakeEC2(dst)
        if fail:
            d.copy_image = lambda **kw: (_ for _ in ()).throw(RuntimeError("limit exceeded"))
        sns = types.SimpleNamespace(msgs=[], publish=lambda **kw: sns.msgs.append(kw))
        rep.TOPIC = "arn:topic"
        try:
            out = rep.lambda_handler(event or {}, None, clients={"src": s, "dst": d, "sns": sns})
        except RuntimeError as exc:
            return exc, s, d, sns
        return out, s, d, sns

    def test_copies_the_newest_two_of_each_architecture_and_no_more(self):
        src = [img("a1", date="2026-01-04"), img("a2", date="2026-08-15"), img("a3", date="2026-09-08"), img("a4", date="2026-09-09"),
               img("x1", "x86_64", "2026-01-19")]
        out, _, d, _ = self.run_it(src, [])
        self.assertEqual(sorted(c["SourceImageId"] for c in d.copies), ["a3", "a4", "x1"])
        self.assertTrue(all(c["CopyImageTags"] is True and c["SourceRegion"] == "us-west-1" for c in d.copies))

    def test_it_is_idempotent_a_copy_is_recorded_by_a_tag_and_never_repeated(self):
        src = [img("a3"), img("a4", date="2026-09-09")]
        out, _, d, _ = self.run_it(src, [img("o1", tags=[("SourceImageId", "a3")])])
        self.assertEqual([c["SourceImageId"] for c in d.copies], ["a4"])
        self.assertEqual(d.tags, [(["ami-new1"], {"SourceImageId": "a4", "SourceRegion": "us-west-1"})])
        out, _, d, _ = self.run_it(src, [img("o1", tags=[("SourceImageId", "a3")]), img("o2", state="pending", tags=[("SourceImageId", "a4")])])
        self.assertEqual(d.copies, [], "a copy still pending counts: no second copy")

    def test_an_unavailable_image_is_never_copied(self):
        _, _, d, _ = self.run_it([img("a1", state="pending"), img("a2", state="failed")], [])
        self.assertEqual(d.copies, [])

    def test_a_dry_run_changes_nothing(self):
        out, _, d, _ = self.run_it([img("a1")], [], event={"dry_run": True})
        self.assertEqual(out["results"][0]["action"], "would-copy")
        self.assertEqual((d.copies, d.tags), ([], []))

    def test_a_failed_copy_fails_the_run_after_telling_you_and_does_not_hide_behind_success(self):
        out, _, _, sns = self.run_it([img("a1")], [], fail=True)
        self.assertIsInstance(out, RuntimeError)
        self.assertIn("limit exceeded", str(out))
        self.assertEqual(len(sns.msgs), 1)

    def test_it_only_ever_copies_it_has_no_deregister_or_modify_of_the_source(self):
        code = (ROOT / "scripts" / "ami-replicator.py").read_text()
        code = re.sub(r'""".*?"""', "", code, flags=re.S)                      # the docstring says what it never does
        code = re.sub(r"#[^\n]*", "", code)
        for forbidden in ("deregister", "delete_snapshot", "modify_image", "terminate", "delete_"):
            self.assertNotIn(forbidden, code)


class TerraformTests(unittest.TestCase):
    def resources(self):
        return re.findall(r'^resource "(\w+)" "(\w+)" \{\n(.*?)^\}', TF, re.S | re.M)

    def test_every_ohio_network_resource_is_on_the_ohio_provider(self):
        for kind, name, body in self.resources():
            if name.startswith("ohio_") or name == "security_ohio_runner":
                self.assertIn("provider = aws.ohio", re.sub(r"\s+=\s+", " = ", body), "%s.%s would land in us-west-1" % (kind, name))

    def test_the_cidr_never_overlaps_another_network_we_run(self):
        cidr = re.search(r'resource "aws_vpc" "ohio_runner".*?cidr_block\s+=\s+"([^"]+)"', TF, re.S).group(1)
        others = ["10.0.0.0/16"] + re.findall(r'cidr_block\s+=\s+"(10\.1\.0\.0/16)"', VPC)
        for other in others:
            self.assertFalse(ipaddress.ip_network(cidr).overlaps(ipaddress.ip_network(other)), "%s overlaps %s" % (cidr, other))

    def test_three_azs_each_with_ipv6_because_the_runner_user_data_refuses_without_it(self):
        self.assertIn('ohio_azs = ["us-east-2a", "us-east-2b", "us-east-2c"]', TF)
        subnet = re.search(r'resource "aws_subnet" "ohio_runner" \{.*?^\}', TF, re.S | re.M).group()
        self.assertIn("assign_ipv6_address_on_creation = true", subnet)
        self.assertIn("map_public_ip_on_launch         = true", subnet)
        uncommented = re.sub(r"#[^\n]*", "", subnet)
        self.assertNotIn('"github-runner-subnet"', uncommented, "build-ami.sh needs that exact Name to match ONE subnet")
        self.assertIn('Name = "github-runner-subnet-ohio-', uncommented)

    def test_the_security_groups_mirror_the_ones_we_run_today(self):
        def rules(text, name):
            body = re.search(r'resource "aws_security_group" "%s" \{.*?^\}' % name, text, re.S | re.M).group()
            return sorted(re.findall(r"(from_port|to_port|protocol|self)\s+=\s+(\S+)", body)), body.count("ingress {")
        self.assertEqual(rules(TF, "ohio_runner"), rules(VPC, "runner"))
        app = (ROOT / "runner-app.tf").read_text()
        self.assertEqual(rules(TF, "ohio_runner_app")[1], 0, "app runners run other people's code: no inbound")
        self.assertEqual(rules(TF, "ohio_runner_app"), rules(app, "runner_app"))
        ops = ["aws_eip.jumpbox[0]", "aws_eip.firecracker_dev[0]", "aws_eip.x86_dev[0]"]
        for op in ops:
            self.assertIn(op, re.search(r'resource "aws_security_group" "ohio_runner" \{.*?^\}', TF, re.S | re.M).group())

    def test_nothing_that_costs_money_while_idle_and_nothing_points_here_yet(self):
        for costly in ("aws_eip", "aws_nat_gateway", "aws_instance", "aws_spot", "aws_launch_template", "aws_vpc_endpoint", "aws_ec2_transit"):
            self.assertNotRegex(TF, r'resource "%s' % costly, "%s would bill while Ohio sits idle" % costly)
        for pointer in ("LAUNCH_SUBNETS", "SUBNET_ID", "REGION_NAME"):
            self.assertNotIn(pointer, TF)

    def test_the_replicators_only_ec2_writes_are_copy_and_tag_into_ohio(self):
        role = re.search(r'resource "aws_iam_role_policy" "ami_replicator" \{.*?^\}', TF, re.S | re.M).group()
        actions = re.findall(r'"(ec2:[A-Za-z]+)"', role)
        self.assertEqual(sorted(actions), ["ec2:CopyImage", "ec2:CreateTags", "ec2:DescribeImages"])
        self.assertIn('"aws:RequestedRegion" = "us-east-2"', role)
        self.assertNotRegex(role, r'"\*"\s*\n\s*Condition')

    def test_it_runs_hourly_and_fails_loudly(self):
        self.assertIn('schedule_expression = "rate(1 hour)"', TF)
        self.assertIn("maximum_retry_attempts = 0", TF)
        self.assertIn('"ami-replicator-errors"', TF)

    def test_the_flow_log_goes_to_the_same_audit_bucket(self):
        self.assertIn('log_destination          = "${aws_s3_bucket.security_audit.arn}/vpc-flow"', TF)


if __name__ == "__main__":
    unittest.main(verbosity=2)
