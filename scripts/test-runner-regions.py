#!/usr/bin/env python3
"""The runner launcher and cleanup are region-aware: runners may live in more than one region during a move, the control
plane never moves, and with ONE region (today) behavior is identical. Runs the real Lambda code from runner-autoscale.tf
against fake EC2 clients, and checks the Terraform that carries the region list into IAM and the Lambdas."""
import importlib.util
import json
import os
import re
import sys
import textwrap
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "runner-autoscale.tf").read_text()
VPC = (ROOT / "runner-vpc.tf").read_text()
OHIO = (ROOT / "ohio.tf").read_text()


def lambda_sources():
    sources, body, collecting = [], [], False
    for line in TF.splitlines():
        if collecting:
            if line.strip() == "EOF":
                sources.append(textwrap.dedent("\n".join(body)))
                body, collecting = [], False
            else:
                body.append(line)
        elif re.match(r"^\s*content\s*=\s*<<-EOF\s*$", line):
            collecting = True
    return sources


WEBHOOK, CLEANUP = lambda_sources()[0], lambda_sources()[1]


def facade_source(code):
    start = code.index("      CONTROL_REGION") if "      CONTROL_REGION" in code else code.index("CONTROL_REGION = ")
    end = code.index("class RegionalEC2")
    class_end = code.index("def __getattr__", end)
    # the whole block from the constants through __getattr__'s body
    block_end = code.index("\n\n", class_end)
    return code[start:block_end]


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeRegionEC2:
    def __init__(self, region, instances, log):
        self.region, self.instances, self.log = region, {i.get("InstanceId", "malformed-%d" % n): i for n, i in enumerate(instances)}, log

    def describe_instances(self, **kw):
        ids = kw.get("InstanceIds")
        if ids:
            missing = [i for i in ids if i not in self.instances]
            if missing:
                raise ClientError("InvalidInstanceID.NotFound")
            found = [self.instances[i] for i in ids]
        else:
            found = list(self.instances.values())
        return {"Reservations": [{"Instances": [i]} for i in found]}

    def create_tags(self, Resources, Tags, **kw):
        self.log.append((self.region, "create_tags", tuple(Resources)))
        return {}

    def terminate_instances(self, InstanceIds, **kw):
        self.log.append((self.region, "terminate", tuple(InstanceIds)))
        return {}

    def describe_images(self, **kw):
        self.log.append((self.region, "describe_images", ()))
        return {"Images": []}


def build(regions, by_region):
    log = []
    clients = {r: FakeRegionEC2(r, by_region.get(r, []), log) for r in regions}
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda name, region_name=None, **kw: clients[region_name]
    ns = {"json": json, "os": os, "boto3": boto3}
    os.environ["RUNNER_REGIONS"] = json.dumps(regions)
    exec(compile(facade_source(WEBHOOK), "facade", "exec"), ns)
    return ns["RegionalEC2"](ns["RUNNER_REGIONS"]), ns, log


def inst(i):
    return {"InstanceId": i, "Placement": {"AvailabilityZone": "x"}}


class FacadeTests(unittest.TestCase):
    def test_the_two_lambdas_carry_the_identical_facade(self):
        self.assertEqual(facade_source(WEBHOOK), facade_source(CLEANUP), "a fix in one must land in both")

    def test_one_region_is_a_straight_pass_through(self):
        ec2, ns, log = build(["us-west-1"], {"us-west-1": [inst("i-1")]})
        self.assertEqual(ec2.describe_instances()["Reservations"], [{"Instances": [inst("i-1")]}])
        self.assertEqual(ec2.region_of("i-unknown"), "us-west-1")
        with self.assertRaises(ClientError):
            ec2.describe_instances(InstanceIds=["i-nope"])                  # the error passes through as it always did
        ec2.terminate_instances(InstanceIds=["i-1"])
        self.assertEqual(log, [("us-west-1", "terminate", ("i-1",))])

    def test_reads_fan_out_across_every_region(self):
        ec2, _, _ = build(["us-east-2", "us-west-1"], {"us-east-2": [inst("i-ohio")], "us-west-1": [inst("i-old1"), inst("i-old2")]})
        ids = sorted(i["InstanceId"] for r in ec2.describe_instances()["Reservations"] for i in r["Instances"])
        self.assertEqual(ids, ["i-ohio", "i-old1", "i-old2"], "a move counts hosts in BOTH regions")

    def test_each_instance_is_written_in_the_region_that_owns_it(self):
        ec2, _, log = build(["us-east-2", "us-west-1"], {"us-east-2": [inst("i-ohio")], "us-west-1": [inst("i-old")]})
        ec2.describe_instances()
        ec2.terminate_instances(InstanceIds=["i-old", "i-ohio"])
        ec2.create_tags(Resources=["i-old"], Tags=[{"Key": "k", "Value": "v"}])
        self.assertIn(("us-west-1", "terminate", ("i-old",)), log)
        self.assertIn(("us-east-2", "terminate", ("i-ohio",)), log)
        self.assertEqual([e for e in log if e[1] == "create_tags"], [("us-west-1", "create_tags", ("i-old",))])

    def test_an_instance_never_seen_is_located_before_it_is_written(self):
        ec2, _, log = build(["us-east-2", "us-west-1"], {"us-west-1": [inst("i-old")]})
        ec2.terminate_instances(InstanceIds=["i-old"])                     # no read first
        self.assertEqual(log, [("us-west-1", "terminate", ("i-old",))], "it must not be terminated 'in the primary' and fail")

    def test_an_id_that_lives_in_the_other_region_is_not_an_error_but_an_id_in_neither_is(self):
        ec2, _, _ = build(["us-east-2", "us-west-1"], {"us-west-1": [inst("i-old")]})
        self.assertEqual(len(ec2.describe_instances(InstanceIds=["i-old"])["Reservations"]), 1)
        with self.assertRaises(ClientError):
            ec2.describe_instances(InstanceIds=["i-nowhere"])

    def test_a_real_error_is_never_swallowed_as_not_found(self):
        ec2, _, _ = build(["us-east-2", "us-west-1"], {})
        ec2.clients["us-east-2"].describe_instances = lambda **kw: (_ for _ in ()).throw(ClientError("UnauthorizedOperation"))
        with self.assertRaises(ClientError) as ctx:
            ec2.describe_instances(InstanceIds=["i-1"])
        self.assertEqual(ctx.exception.response["Error"]["Code"], "UnauthorizedOperation")

    def test_image_lookups_and_launches_use_the_primary_region(self):
        ec2, ns, log = build(["us-east-2", "us-west-1"], {})
        ec2.describe_images(Owners=["self"])
        self.assertEqual(log, [("us-east-2", "describe_images", ())])
        self.assertEqual(ns["PRIMARY_REGION"], "us-east-2")

    def test_a_malformed_record_is_ignored_not_a_crash(self):
        ec2, _, _ = build(["us-west-1"], {"us-west-1": [{"State": {}}]})
        ec2.describe_instances()
        self.assertEqual(ec2.owner, {})

    def test_after_the_runners_have_left_us_west_1_a_builder_there_can_still_be_found_and_reaped(self):
        log = []
        clients = {"us-east-2": FakeRegionEC2("us-east-2", [inst("i-ohio")], log),
                   "us-west-1": FakeRegionEC2("us-west-1", [inst("i-builder")], log)}
        boto3 = types.ModuleType("boto3")
        boto3.client = lambda name, region_name=None, **kw: clients[region_name]
        ns = {"json": json, "os": os, "boto3": boto3}
        os.environ["RUNNER_REGIONS"] = json.dumps(["us-east-2"])               # the FINAL state of a move
        exec(compile(facade_source(CLEANUP), "facade", "exec"), ns)
        ec2 = ns["RegionalEC2"](ns["RUNNER_REGIONS"], also=["us-west-1"])
        fleet = [i["InstanceId"] for r in ec2.describe_instances()["Reservations"] for i in r["Instances"]]
        self.assertEqual(fleet, ["i-ohio"], "the extra region is never part of the fleet read")
        found = ec2.clients["us-west-1"].describe_instances()["Reservations"]
        self.assertEqual(len(found), 1)
        ec2.owner["i-builder"] = "us-west-1"
        ec2.terminate_instances(InstanceIds=["i-builder"])
        self.assertEqual(log, [("us-west-1", "terminate", ("i-builder",))])

    def test_the_builder_sweep_reads_its_own_fixed_region_not_the_runner_regions(self):
        self.assertIn("BUILDER_REGION = 'us-west-1'", CLEANUP)
        self.assertIn("ec2 = RegionalEC2(RUNNER_REGIONS, also=[BUILDER_REGION])", CLEANUP)
        sweep = CLEANUP[CLEANUP.index("# Phase 4: Clean up stale AMI builder"):CLEANUP.index("# Phase 5")]
        self.assertIn("ec2.clients[BUILDER_REGION].describe_instances(", sweep)
        self.assertNotIn("ec2.describe_instances(", sweep)
        self.assertIn("ec2.owner[instance_id] = BUILDER_REGION", sweep)

    def test_a_bootstrap_credential_is_judged_against_every_configured_region_not_just_the_primary(self):
        src = CLEANUP[CLEANUP.index("def bootstrap_instance_arns"):CLEANUP.index('"""', CLEANUP.index("def bootstrap_instance_arns")) + 3]
        body = CLEANUP[CLEANUP.index("def bootstrap_instance_arns"):]
        body = body[:body.index("\n\n")]
        for regions, expected in ((["us-west-1"], {"arn:aws:ec2:us-west-1:111:instance/i-1"}),
                                  (["us-east-2", "us-west-1"], {"arn:aws:ec2:us-east-2:111:instance/i-1", "arn:aws:ec2:us-west-1:111:instance/i-1"})):
            ns = {"RUNNER_REGIONS": regions}
            exec(compile(textwrap.dedent(body), "arns", "exec"), ns)
            self.assertEqual(ns["bootstrap_instance_arns"]("i-1", "111"), expected)
        self.assertNotIn("arn:aws:ec2:eu-west-1:111:instance/i-1", expected, "a region we do not run in is never authority")
        sweep = CLEANUP[CLEANUP.index("expected_arns = bootstrap_instance_arns"):]
        self.assertIn("tags.get('InstanceArn') not in expected_arns", sweep[:900])
        self.assertNotIn("instance_region(match.group(1))", CLEANUP, "the sweep must not guess the primary for a dead host")

    def test_the_control_plane_clients_stay_in_us_west_1_whatever_the_runner_regions(self):
        for name, code in (("webhook", WEBHOOK), ("cleanup", CLEANUP)):
            for service in ("ssm", "dynamodb", "lambda"):
                self.assertRegex(code, r"boto3\.client\('%s', region_name='us-west-1'" % service, "%s %s" % (name, service))
            self.assertNotIn("boto3.client('ec2', region_name='us-west-1'", code, "%s still pins EC2 to us-west-1" % name)


class WiringTests(unittest.TestCase):
    def test_today_the_runners_run_in_exactly_us_west_1(self):
        self.assertRegex(VPC, r'\n  runner_regions\s*=\s*\["us-west-1"\]\n')
        self.assertIn("runner_primary_region = local.runner_regions[0]", VPC)

    def test_all_three_lambdas_are_told_the_regions(self):
        self.assertEqual(TF.count("RUNNER_REGIONS    = jsonencode(local.runner_regions)")
                         + TF.count("RUNNER_REGIONS     = jsonencode(local.runner_regions)"), 3)

    def test_every_ec2_arn_in_the_launch_iam_follows_the_region_list(self):
        iam = re.search(r'resource "aws_iam_role_policy" "runner_lambda" \{.*?\n\}\n', TF, re.S).group()
        ec2_arns = re.findall(r'arn:aws:ec2:([^:"]*):', iam)
        # every one is the loop variable, except the temporary AMI-builder reaper, which only ever runs in us-west-1a
        self.assertEqual(sorted(set(ec2_arns)), ["${r}", "us-west-1"])
        self.assertEqual(ec2_arns.count("us-west-1"), 1)
        builder = iam[iam.index("ReapExistingTemporaryAMIBuilder"):]
        self.assertIn('"arn:aws:ec2:us-west-1:', builder.split("Condition")[0])

    def test_the_instance_may_assign_ipv6_only_on_its_own_runner_subnets_in_every_listed_region(self):
        pol = re.search(r'resource "aws_iam_role_policy" "runner" \{.*?\n\}\n', VPC, re.S).group()
        self.assertIn('[for r in local.runner_regions : "arn:aws:ec2:${r}:', pol)
        self.assertIn('"ec2:Subnet" = local.runner_fcvm_subnet_arns', pol)

    def test_the_security_group_and_subnet_arns_cover_exactly_the_listed_regions(self):
        self.assertIn("flatten([for r in local.runner_regions : local.runner_networks[r].subnets[*].arn])", VPC)
        self.assertIn("[for r in local.runner_regions : local.runner_networks[r].security_group.arn]", VPC)
        self.assertIn('"us-east-2" = { subnets = aws_subnet.ohio_runner, security_group = aws_security_group.ohio_runner[0] }', VPC)

    def test_the_instance_keeps_ssm_and_dynamodb_in_the_control_region_and_ec2_in_its_own(self):
        script = TF[TF.index("# REGION (from IMDS above)"):]
        self.assertIn('CONTROL_REGION="${local.runner_control_region}"', script)
        ec2_calls = re.findall(r'aws ec2 [^\n]*', TF)
        for call in ec2_calls:
            if "--region" in call:
                self.assertIn('--region "$REGION"', call, "an EC2 call about this host must use ITS region: " + call)
        for line in re.findall(r'[^\n]*--region "\$\$\{CONTROL_REGION:-\$REGION\}"[^\n]*', TF):
            self.assertNotIn("aws ec2", line)
        self.assertEqual(TF.count('--region "$${CONTROL_REGION:-$REGION}"'), 4)
        self.assertIn("CONTROL_REGION=%s", TF)

    def test_the_controller_may_tag_a_bootstrap_credential_with_an_instance_arn_from_any_listed_region(self):
        # Denied after every Ohio RunInstances otherwise: the host is terminated and no Ohio runner can register.
        boot = (ROOT / "runner-bootstrap.tf").read_text()
        self.assertIn('[for r in local.runner_regions : "arn:aws:ec2:${r}:${data.aws_caller_identity.current.account_id}:instance/i-*"]', boot)
        self.assertNotIn('"aws:RequestTag/InstanceArn" = "arn:aws:ec2:us-west-1', boot)

    def test_no_fcvm_runner_policy_pins_a_literal_us_west_1_ec2_arn_except_the_ami_builder_reaper(self):
        # The class of bug that would have broken the first Ohio launch: an EC2 ARN (or tag condition) left on one region.
        for name in ("runner-bootstrap.tf", "runner-vpc.tf", "runner-autoscale.tf"):
            text = (ROOT / name).read_text()
            lines = [l for l in text.splitlines() if "arn:aws:ec2:us-west-1" in l and not l.lstrip().startswith("#")]
            allowed = [l for l in lines if "builders only ever run in us-west-1a" in l]
            self.assertEqual(sorted(set(lines) - set(allowed)), [], "%s pins an EC2 ARN to us-west-1" % name)
        self.assertEqual(sum("builders only ever run in us-west-1a" in l for l in TF.splitlines()), 1)

    def test_ohio_has_the_key_pair_the_launcher_names_on_every_launch(self):
        self.assertIn("KeyName='fcvm-ec2'", TF)
        kp = re.search(r'resource "aws_key_pair" "ohio_runner" \{.*?\n\}', OHIO, re.S).group()
        self.assertIn('key_name   = "fcvm-ec2"', kp)
        self.assertIn("provider   = aws.ohio", kp)
        self.assertIn("AAAAC3NzaC1lZDI1NTE5AAAAINwtXjjTCVgT9OR3qrnz3zDkV2GveuCBlWFXSOBG2joe", kp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
