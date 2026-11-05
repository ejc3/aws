#!/usr/bin/env python3
"""Offline checks for boot-race/launcher.py, the only caller of RunTask and StopTask for boot-race tasks.

RunTask has no IAM condition key for the subnet or a public IP, so the launcher's code is what keeps a Fargate task
in the parallel-box subnet, in the boot-race group and without a public address. This imports the real file with a
fake ECS client (no AWS credentials, no network, no boto3) and pins the Terraform around it: the driver may only
invoke it, and nothing lets the driver change it.

Run from the repo root:  python3 -S -B scripts/test-boot-race-launcher.py
"""
import importlib.util
import os
import re
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "boot-race.tf").read_text()
CLUSTER_TASK = "arn:aws:ecs:us-east-2:111122223333:task/boot-race/" + "0" * 32


class FakeEcs:
    def __init__(self):
        self.runs, self.stops = [], []

    def run_task(self, **kw):
        self.runs.append(kw)
        return {"tasks": [{"taskArn": CLUSTER_TASK}], "failures": []}

    def stop_task(self, **kw):
        self.stops.append(kw)
        return {}


def load():
    ecs = FakeEcs()
    sys.modules["boto3"] = types.SimpleNamespace(client=lambda name: ecs)
    os.environ.update(CLUSTER="boot-race", FARGATE_FAMILY="boot-race-fargate", EC2_FAMILY="boot-race-ec2",
                      SUBNET="subnet-fixed", SECURITY_GROUP="sg-fixed")
    spec = importlib.util.spec_from_file_location("launcher", ROOT / "boot-race" / "launcher.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, ecs


class Launcher(unittest.TestCase):
    def setUp(self):
        self.mod, self.ecs = load()

    def test_fargate_network_is_fixed_whatever_the_caller_sends(self):
        out = self.mod.lambda_handler({"action": "run", "lane": "fargate", "assignPublicIp": "ENABLED",
                                       "subnets": ["subnet-other"], "networkConfiguration": {"x": 1}}, None)
        run = self.ecs.runs[0]
        self.assertEqual(run["cluster"], "boot-race")
        self.assertEqual(run["taskDefinition"], "boot-race-fargate")
        self.assertEqual(run["networkConfiguration"], {"awsvpcConfiguration": {
            "subnets": ["subnet-fixed"], "securityGroups": ["sg-fixed"], "assignPublicIp": "DISABLED"}})
        self.assertLessEqual(out["before_unix_ms"], out["after_unix_ms"])
        self.assertEqual(out["task_arn"], CLUSTER_TASK)

    def test_ec2_lane_is_placed_on_the_named_instance_only(self):
        self.mod.lambda_handler({"action": "run", "lane": "ec2", "instance": "i-0123456789abcdef0"}, None)
        run = self.ecs.runs[0]
        self.assertEqual(run["taskDefinition"], "boot-race-ec2")
        self.assertEqual(run["placementConstraints"],
                         [{"type": "memberOf", "expression": "ec2InstanceId == i-0123456789abcdef0"}])
        for bad in (None, "i-x", "i-0123456789abcdef0 or true", 5):
            with self.assertRaises(ValueError):
                self.mod.lambda_handler({"action": "run", "lane": "ec2", "instance": bad}, None)

    def test_only_tasks_of_the_boot_race_cluster_are_stopped(self):
        self.mod.lambda_handler({"action": "stop", "task": CLUSTER_TASK}, None)
        self.assertEqual(self.ecs.stops[0]["cluster"], "boot-race")
        for bad in (CLUSTER_TASK.replace("boot-race", "games"), "arn", None):
            with self.assertRaises(ValueError):
                self.mod.lambda_handler({"action": "stop", "task": bad}, None)
        self.assertEqual(len(self.ecs.stops), 1)

    def test_unknown_actions_and_lanes_are_refused(self):
        for event in ({"action": "run", "lane": "other"}, {"action": "delete"}, {}):
            with self.assertRaises(ValueError):
                self.mod.lambda_handler(event, None)
        self.assertEqual(self.ecs.runs, [])


def block(text, header):
    start = text.index(header)
    return text[start:text.index("\n}\n", start)]


class Terraform(unittest.TestCase):
    def test_the_launcher_is_terraform_owned_and_takes_its_network_from_terraform(self):
        fn = block(TF, 'resource "aws_lambda_function" "boot_race_launcher"')
        self.assertNotIn("ignore_changes", fn)
        self.assertIn("data.archive_file.boot_race_launcher.output_base64sha256", fn)
        self.assertIn("SUBNET         = aws_subnet.ohio_pbox.id", fn)
        self.assertIn("SECURITY_GROUP = aws_security_group.boot_race.id", fn)

    def test_the_driver_may_only_invoke_the_launcher(self):
        driver = block(TF, 'data "aws_iam_policy_document" "boot_race_driver"')
        for action in ("ecs:RunTask", "ecs:StopTask", "ecs-tasks.amazonaws.com"):
            self.assertNotIn(action, driver)
        invoke = re.search(r'sid\s*=\s*"InvokeTheLauncher".*?\n  \}', driver, re.S).group()
        self.assertIn("aws_lambda_function.boot_race_launcher.arn", invoke)
        # the functions the driver may update are the two workload functions, never the launcher
        update = re.search(r'sid\s*=\s*"DriveBootRaceFunctions".*?\n  \}', driver, re.S).group()
        self.assertIn("aws_lambda_function.boot_race :", update)
        self.assertNotIn("boot_race_launcher", update)

    def test_the_ecs_host_has_no_managed_policy_and_pulls_only_the_workload(self):
        self.assertNotIn("policy/service-role/AmazonEC2ContainerServiceforEC2Role", TF)
        self.assertNotIn('resource "aws_iam_role_policy_attachment" "boot_race_ecs_host"', TF)
        host = block(TF, 'resource "aws_iam_role_policy" "boot_race_ecs_host"')
        self.assertIn("Resource = aws_ecr_repository.boot_race.arn", host)
        self.assertIn('ArnEquals = { "ecs:cluster" = aws_ecs_cluster.boot_race.arn }', host)


if __name__ == "__main__":
    unittest.main(buffer=True)
