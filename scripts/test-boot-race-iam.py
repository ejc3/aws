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


def block(header):
    content = TF[TF.index(header):]
    return re.sub(r"\s+", " ", content[:content.index("\n}\n")])


class StandingDriver(unittest.TestCase):
    """Owner, 2026-11-14: "Kill the window. We shouldn't need back and forth with jumphost"."""

    def test_driver_reaper_and_cap_are_on_by_default(self):
        self.assertIn("default = true", block('variable "boot_race_enabled"'))
        self.assertRegex(TF, r"boot_race_driver_enabled\s*= var\.boot_race_enabled && var\.enable_firecracker_instance")
        self.assertIn("value = local.boot_race_driver_enabled ?", block('output "boot_race"'))
        for header in ('resource "aws_iam_role" "boot_race_driver"',
                       'resource "aws_iam_role_policy" "boot_race_driver"',
                       'resource "aws_budgets_budget_action" "boot_race"'):
            self.assertIn("count = local.boot_race_driver_enabled ? 1 : 0", block(header), header)
        self.assertIn('state = var.boot_race_enabled ? "ENABLED" : "DISABLED"',
                      block('resource "aws_scheduler_schedule" "boot_race_reaper"'))

    def test_no_campaign_window_remains(self):
        for gone in ("boot_race_campaign", "boot_race_assume_conditions", "aws:CurrentTime", "aws:TokenIssueTime",
                     "DenyAfterCampaignExpiry", "DenySessionsFromBeforeCampaign"):
            self.assertNotIn(gone, TF)

    def test_only_primary_arm_instance_credentials_are_trusted_with_no_inline_grant(self):
        trust = block('resource "aws_iam_role" "boot_race_driver"')
        self.assertIn("Principal = { AWS = aws_iam_role.dev_server.arn }", trust)
        self.assertIn('Condition = { ArnEquals = { "ec2:SourceInstanceARN" = aws_instance.firecracker_dev[0].arn } }', trust)
        # The trust policy alone admits the role within the account; dev-server-role's inline policies are full.
        self.assertNotRegex(TF, r"role\s*=\s*aws_iam_role\.dev_server\.")
        policy = block('resource "aws_iam_role_policy" "boot_race_driver"')
        self.assertIn("policy = data.aws_iam_policy_document.boot_race_driver.json", policy)
        self.assertIn("depends_on = [aws_budgets_budget_action.boot_race]", policy)

    def test_the_driver_keeps_its_tag_template_and_type_scopes(self):
        driver = block('data "aws_iam_policy_document" "boot_race_driver"')
        run = driver[driver.index('sid = "RunTaggedBootRaceOnly"'):driver.index('sid = "DenyOtherInstanceTypes"')]
        self.assertIn('variable = "aws:RequestTag/Project" values = ["boot-race"]', run)
        self.assertIn("values = [aws_launch_template.boot_race_ec2.arn, aws_launch_template.boot_race_ecs_host.arn]", run)
        deny = driver[driver.index('sid = "DenyOtherInstanceTypes"'):driver.index('sid = "RunInstancesReferencedResources"')]
        self.assertIn('effect = "Deny"', deny)
        self.assertIn('test = "StringNotEquals" variable = "ec2:InstanceType" values = ["c8g.medium"]', deny)
        manage = driver[driver.index('sid = "ManageTaggedBootRaceInstances"'):driver.index('sid = "ImageFromTaggedInstance"')]
        self.assertIn('variable = "ec2:ResourceTag/Project" values = ["boot-race"]', manage)

    def test_reaper_age_limits_are_unchanged(self):
        reaper = block('resource "aws_lambda_function" "boot_race_reaper"')
        for limit in ('INSTANCE_LIMIT_S = "10800"', 'TASK_LIMIT_S = "3600"', 'IMAGE_LIMIT_S = "86400"'):
            self.assertIn(limit, reaper)

    def test_fargate_network_stays_off(self):
        self.assertIn("boot_race_fargate_enabled = false", TF)
        for header in ('resource "aws_subnet" "boot_race"', 'resource "aws_route_table" "boot_race"',
                       'resource "aws_route_table_association" "boot_race"', 'resource "aws_security_group" "boot_race_endpoints"',
                       'resource "aws_vpc_endpoint" "boot_race_s3"'):
            self.assertIn("count = local.boot_race_fargate_enabled ? 1 : 0", block(header), header)
        self.assertIn('local.boot_race_fargate_enabled ? toset(["ecr.api", "ecr.dkr"]) : toset([])',
                      block('resource "aws_vpc_endpoint" "boot_race_ecr"'))


class SpendCap(unittest.TestCase):
    def test_a_25_dollar_monthly_budget_on_the_project_tag(self):
        budget = block('resource "aws_budgets_budget" "boot_race"')
        self.assertIn('limit_amount = "25"', budget)
        self.assertIn('time_unit = "MONTHLY"', budget)
        self.assertIn('name = "TagKeyValue" values = ["user:Project$boot-race"]', budget)
        self.assertIn("depends_on = [aws_ce_cost_allocation_tag.project]", budget)
        tag = block('resource "aws_ce_cost_allocation_tag" "project"')
        self.assertIn('tag_key = "Project"', tag)
        self.assertIn('status = "Active"', tag)
        self.assertIn('prevent_destroy = true', tag)

    def test_the_driver_uploads_workload_code_but_cannot_change_function_configuration(self):
        grants = set().union(*(set(acts) for acts, _, _ in statements("boot_race_driver")))
        self.assertIn("lambda:UpdateFunctionCode", grants)
        self.assertNotIn("lambda:UpdateFunctionConfiguration", grants)

    def test_breaching_the_budget_caps_the_driver_without_a_person(self):
        action = block('resource "aws_budgets_budget_action" "boot_race"')
        self.assertIn("budget_name = aws_budgets_budget.boot_race.name", action)
        self.assertIn('action_type = "APPLY_IAM_POLICY"', action)
        self.assertIn('approval_model = "AUTOMATIC"', action)
        self.assertIn('notification_type = "ACTUAL"', action)
        self.assertIn('action_threshold_type = "PERCENTAGE" action_threshold_value = 100', action)
        self.assertIn("policy_arn = aws_iam_policy.boot_race_spend_cap.arn", action)
        self.assertIn("roles = [aws_iam_role.boot_race_driver[0].name]", action)

    def test_the_cap_denies_everything_but_cleanup_and_reads(self):
        cap = block('resource "aws_iam_policy" "boot_race_spend_cap"')
        self.assertIn('Effect = "Deny"', cap)
        self.assertIn('Resource = "*"', cap)
        allowed = set(re.findall(r'"([a-z0-9]+:[A-Za-z*]+)"', cap[cap.index("NotAction"):cap.index("Resource")]))
        for cleanup in ("ec2:StopInstances", "ec2:TerminateInstances", "ec2:DeregisterImage", "ec2:DeleteSnapshot",
                        "ecs:StopTask", "ecr:BatchDeleteImage"):
            self.assertIn(cleanup, allowed)
        for spend in ("ec2:RunInstances", "ec2:StartInstances", "ec2:CreateImage", "ecs:RunTask", "lambda:InvokeFunction",
                      "lambda:InvokeFunctionUrl", "lambda:UpdateFunctionCode", "ecr:PutImage"):
            self.assertNotIn(spend, allowed)
        for verb in allowed:
            self.assertRegex(verb.split(":")[1], r"^(Stop|Terminate|Deregister|Delete|BatchDelete|Describe|List|Get|Filter)",
                             verb)

    def test_stop_only_path_is_held_to_tasks_in_the_exact_cluster(self):
        grants = [(r, st) for actions, r, st in statements("boot_race_driver") if "ecs:StopTask" in actions]
        self.assertEqual(len(grants), 1)
        resources, statement = grants[0]
        self.assertEqual(resources.strip(), "local.boot_race_task_arn")
        self.assertRegex(TF, r'boot_race_task_arn\s*= "\$\{local\.boot_race_ecs_arn\}:task/boot-race/\*"')
        self.assertIn('variable = "ecs:cluster"', statement)
        self.assertIn('values   = [aws_ecs_cluster.boot_race.arn]', statement)
        self.assertFalse(any("ecs:RunTask" in acts for acts, _, _ in statements("boot_race_driver")))
        cap = block('resource "aws_iam_policy" "boot_race_spend_cap"')
        self.assertIn('Effect = "Deny" Action = "ecs:StopTask" '
                      'NotResource = local.boot_race_task_arn', cap)

    def test_the_budget_role_may_only_attach_the_cap_to_the_driver(self):
        trust = block('resource "aws_iam_role" "boot_race_budget_action"')
        self.assertIn('Principal = { Service = "budgets.amazonaws.com" }', trust)
        self.assertIn('"aws:SourceAccount" = local.boot_race_account', trust)
        self.assertIn('ArnLike = { "aws:SourceArn" = "arn:aws:budgets::${local.boot_race_account}:budget/*" }', trust)
        grant = block('resource "aws_iam_role_policy" "boot_race_budget_action"')
        self.assertIn('Action = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]', grant)
        self.assertIn('Resource = "arn:aws:iam::${local.boot_race_account}:role/boot-race-driver"', grant)
        self.assertIn('ArnEquals = { "iam:PolicyARN" = aws_iam_policy.boot_race_spend_cap.arn }', grant)


if __name__ == "__main__":
    unittest.main(buffer=True)
