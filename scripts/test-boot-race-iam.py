#!/usr/bin/env python3
"""Every AWS call boot-race-reaper and boot-race-launcher make is granted on the resource AWS authorizes it against.

A fake client accepts any call, so the reaper's own tests could not see that a cluster-wide ecs:ListTasks
authorizes against "*" while its grant named container-instance ARNs: the call would have been denied in AWS on
every run. This reads the calls out of each function's code and checks each one's grant, statically, against the
resource types the Service Authorization Reference lists for that action as the code calls it.

Run from the repo root:  python3 -S -B scripts/test-boot-race-iam.py
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "boot-race.tf").read_text()

# action -> the resource kinds AWS authorizes the call against, as these functions make it
#   ec2 Describe*: no resource-level permissions, "*"
#   ecs:ListTasks with only `cluster` (no containerInstance filter): "*", scoped by the ecs:cluster condition key
DOCUMENTED = {
    "ec2:DescribeInstances": {"*"},
    "ec2:DescribeImages": {"*"},
    "ec2:DescribeSnapshots": {"*"},
    "ec2:TerminateInstances": {"instance"},
    "ec2:DeregisterImage": {"image"},
    "ec2:DeleteSnapshot": {"snapshot"},
    "ecs:ListTasks": {"*"},
    "ecs:DescribeTasks": {"task"},
    "ecs:StopTask": {"task"},
    "ecs:RunTask": {"task-definition"},
}
# Actions that must carry a condition holding them to the boot-race cluster or tag.
CONDITIONED = {
    "ecs:ListTasks": '"ecs:cluster"', "ecs:DescribeTasks": '"ecs:cluster"', "ecs:StopTask": '"ecs:cluster"',
    "ecs:RunTask": '"ecs:cluster"', "ec2:TerminateInstances": '"ec2:ResourceTag/Project"',
    "ec2:DeregisterImage": '"ec2:ResourceTag/Project"', "ec2:DeleteSnapshot": '"ec2:ResourceTag/Project"',
}
FUNCTIONS = {"reaper.py": "boot_race_reaper", "launcher.py": "boot_race_launcher"}


def camel(snake):
    return "".join(w.capitalize() for w in snake.split("_"))


def calls(source):
    """{service:Action} for every client call in the source (paginators included)."""
    found = set()
    for client, method in re.findall(r"\b(ec2|ecs|ECS)\.(\w+)\(", source):
        if method != "get_paginator":
            found.add(f"{client.lower()}:{camel(method)}")
    for client, method in re.findall(r"\b(ec2|ecs|ECS)\.get_paginator\(\"(\w+)\"\)", source):
        found.add(f"{client.lower()}:{camel(method)}")
    return found


def statements(doc_name):
    start = TF.index(f'data "aws_iam_policy_document" "{doc_name}"')
    doc = TF[start:TF.index("\n}\n", start)]
    out = []
    for st in re.findall(r"statement \{.*?\n  \}", doc, re.S):
        actions = re.findall(r'"([a-z0-9]+:[A-Za-z]+)"', re.search(r"actions\s*=\s*\[(.*?)\]", st, re.S).group(1))
        resources = re.search(r"resources\s*=\s*\[(.*?)\]", st, re.S).group(1)
        out.append((set(actions), resources, st))
    return out


def kind(resources):
    kinds = set()
    if re.search(r'"\*"', resources):
        kinds.add("*")
    for k, rx in (("instance", r":instance/"), ("image", r"::image/"), ("snapshot", r"::snapshot/"),
                  ("task-definition", r":task-definition/"), ("task", r":task/"),
                  ("container-instance", r":container-instance/")):
        if re.search(rx, resources):
            kinds.add(k)
    return kinds


class Grants(unittest.TestCase):
    def test_every_call_is_granted_on_a_documented_resource(self):
        for source, doc in FUNCTIONS.items():
            code = (ROOT / "boot-race" / source).read_text()
            called = calls(code)
            self.assertTrue(called, f"{source}: found no AWS calls; the check would be vacuous")
            sts = statements(doc)
            for action in sorted(called):
                self.assertIn(action, DOCUMENTED, f"{source} calls {action}; add its documented resource here")
                granting = [(r, st) for acts, r, st in sts if action in acts]
                self.assertTrue(granting, f"{source} calls {action}, which {doc} does not grant")
                ok = [st for r, st in granting if kind(r) & DOCUMENTED[action]]
                self.assertTrue(ok, f"{doc} grants {action} on {[kind(r) for r, _ in granting]}, "
                                    f"AWS authorizes it against {DOCUMENTED[action]}")
                if action in CONDITIONED:
                    self.assertTrue(any(CONDITIONED[action] in st for st in ok),
                                    f"{doc}: {action} is not held by {CONDITIONED[action]}")

    def test_run_task_comes_with_pass_role_of_the_execution_role(self):
        sts = statements("boot_race_launcher")
        self.assertTrue(any("iam:PassRole" in acts and "boot_race_task_execution" in r for acts, r, _ in sts))


class SharedRoute(unittest.TestCase):
    def test_the_parallel_box_route_table_gains_no_route_or_association(self):
        self.assertNotIn("aws_route_table.ohio_pbox", TF)
        for ref in re.findall(r"route_table_ids?\s*=\s*\[?([^\]\n]+)", TF):
            self.assertIn("aws_route_table.boot_race", ref, ref)
        self.assertNotRegex(TF, r'resource "aws_route" ')

    def test_fargate_and_the_endpoints_live_in_the_campaign_subnet(self):
        self.assertIn('SUBNET         = local.boot_race_fargate_enabled ? aws_subnet.boot_race[0].id : ""', TF)
        ecr = TF[TF.index('resource "aws_vpc_endpoint" "boot_race_ecr"'):]
        self.assertIn("subnet_ids          = [aws_subnet.boot_race[0].id]", ecr[:ecr.index("\n}\n")])
        route_table = TF[TF.index('resource "aws_route_table" "boot_race"'):]
        self.assertNotIn("route {", route_table[:route_table.index("\n}\n")], "the campaign subnet needs no internet route")


class Retirement(unittest.TestCase):
    def test_ordinary_apply_leaves_the_driver_and_paid_network_dormant(self):
        def block(header):
            content = TF[TF.index(header):]
            return re.sub(r"\s+", " ", content[:content.index("\n}\n")])

        self.assertIn("default = false", block('variable "boot_race_enabled"'))
        for header in ('resource "aws_iam_role" "boot_race_driver"',
                       'resource "aws_iam_role_policy" "boot_race_driver"',
                       'resource "aws_iam_role_policy" "dev_server_boot_race"'):
            self.assertIn("count = var.boot_race_enabled ? 1 : 0", block(header), header)
        self.assertIn('state = var.boot_race_enabled ? "ENABLED" : "DISABLED"',
                      block('resource "aws_scheduler_schedule" "boot_race_reaper"'))

    def test_opening_driver_never_opens_fargate_network(self):
        self.assertIn("boot_race_fargate_enabled = false", TF)
        for header in ('resource "aws_subnet" "boot_race"', 'resource "aws_route_table" "boot_race"',
                       'resource "aws_route_table_association" "boot_race"', 'resource "aws_security_group" "boot_race_endpoints"',
                       'resource "aws_vpc_endpoint" "boot_race_s3"'):
            chunk = TF[TF.index(header):]
            self.assertIn("count = local.boot_race_fargate_enabled ? 1 : 0",
                          re.sub(r"\s+", " ", chunk[:chunk.index("\n}\n")]), header)
        ecr = TF[TF.index('resource "aws_vpc_endpoint" "boot_race_ecr"'):]
        self.assertIn('local.boot_race_fargate_enabled ? toset(["ecr.api", "ecr.dkr"]) : toset([])',
                      ecr[:ecr.index("\n}\n")])

    def test_exact_operator_and_window_bound_both_assume_role_policies(self):
        self.assertIn("!var.boot_race_enabled || var.boot_race_campaign != null", TF)
        self.assertIn('instance/i-[0-9a-f]{17}$', TF)
        self.assertIn('timeadd(var.boot_race_campaign.starts_at, "3h")', TF)
        self.assertIn('"ec2:SourceInstanceARN" = var.boot_race_campaign.source_instance_arn', TF)
        self.assertIn('DateGreaterThanEquals = { "aws:CurrentTime" = var.boot_race_campaign.starts_at }', TF)
        self.assertRegex(TF, r'DateLessThan\s*= \{ "aws:CurrentTime" = var.boot_race_campaign.expires_at \}')
        self.assertEqual(TF.count("Condition = local.boot_race_assume_conditions"), 2)

    def test_already_issued_sts_sessions_expire_and_cannot_carry_forward(self):
        for sid, op, field, value in (
            ("DenyAfterCampaignExpiry", "DateGreaterThanEquals", "aws:CurrentTime", "expires_at"),
            ("DenySessionsFromBeforeCampaign", "DateLessThan", "aws:TokenIssueTime", "starts_at"),
        ):
            chunk = TF.split(f'Sid       = "{sid}"', 1)[1]
            self.assertTrue(chunk.startswith(
                f'\n        Effect    = "Deny"\n        Action    = "*"\n        Resource  = "*"\n'
                f'        Condition = {{ {op} = {{ "{field}" = var.boot_race_campaign.{value} }} }}'
            ), sid)


if __name__ == "__main__":
    unittest.main(buffer=True)
