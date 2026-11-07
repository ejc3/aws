#!/usr/bin/env python3
"""Offline checks for boot-race/reaper.py, the server-side expiry of the boot-race campaign's compute.

Imports the real file with fake EC2 and ECS clients (no AWS credentials, no network, no boto3) and pins the
Terraform that schedules it: every 15 minutes, its grants held to the Project=boot-race tag and the boot-race
cluster.

Run from the repo root:  python3 -S -B scripts/test-boot-race-reaper.py
"""
import datetime
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "boot-race.tf").read_text()
NOW = datetime.datetime(2026, 11, 7, 12, 0, tzinfo=datetime.timezone.utc)
H = datetime.timedelta(hours=1)


class ClientError(Exception):
    pass


def load():
    sys.modules["boto3"] = types.SimpleNamespace(client=lambda name: None)
    sys.modules["botocore"] = types.ModuleType("botocore")
    sys.modules["botocore.exceptions"] = types.SimpleNamespace(ClientError=ClientError)
    spec = importlib.util.spec_from_file_location("reaper", ROOT / "boot-race" / "reaper.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Pages:
    def __init__(self, pages):
        self.pages, self.kwargs = pages, None

    def paginate(self, **kw):
        self.kwargs = kw
        return self.pages


class FakeEc2:
    def __init__(self, instances, images, snapshots, in_use=()):
        self.instances, self.images, self.snapshots, self.in_use = instances, images, snapshots, set(in_use)
        self.paginators, self.terminated, self.deregistered, self.deleted, self.image_filters = {}, [], [], [], None

    def get_paginator(self, name):
        if name == "describe_instances":
            p = Pages([{"Reservations": [{"Instances": self.instances}]}])
        else:
            p = Pages([{"Snapshots": self.snapshots}])
        self.paginators[name] = p
        return p

    def terminate_instances(self, InstanceIds):
        self.terminated += InstanceIds

    def describe_images(self, Owners, Filters):
        self.image_filters = (Owners, Filters)
        return {"Images": self.images}

    def deregister_image(self, ImageId):
        self.deregistered.append(ImageId)

    def delete_snapshot(self, SnapshotId):
        if SnapshotId in self.in_use:
            raise ClientError("InvalidSnapshot.InUse")
        self.deleted.append(SnapshotId)


class FakeEcs:
    def __init__(self, tasks):
        self.tasks, self.stopped, self.list_kwargs = tasks, [], None

    def get_paginator(self, name):
        p = Pages([{"taskArns": [t["taskArn"] for t in self.tasks]}])
        self.list_kwargs = p
        return p

    def describe_tasks(self, cluster, tasks):
        assert cluster == "boot-race"
        return {"tasks": [t for t in self.tasks if t["taskArn"] in tasks]}

    def stop_task(self, cluster, task, reason):
        assert cluster == "boot-race"
        self.stopped.append(task)


class Reaper(unittest.TestCase):
    def setUp(self):
        self.mod = load()

    def run_with(self, ec2, ecs):
        return self.mod.lambda_handler({}, None, ec2=ec2, ecs=ecs, now=NOW)

    def test_only_instances_past_the_limit_are_terminated_and_only_by_tag(self):
        ec2 = FakeEc2([{"InstanceId": "i-old", "LaunchTime": NOW - 3.5 * H},
                       {"InstanceId": "i-young", "LaunchTime": NOW - 2.5 * H}], [], [])
        out = self.run_with(ec2, FakeEcs([]))
        self.assertEqual(out["instances"], ["i-old"])
        self.assertEqual(ec2.terminated, ["i-old"])
        filters = ec2.paginators["describe_instances"].kwargs["Filters"]
        self.assertIn({"Name": "tag:Project", "Values": ["boot-race"]}, filters)
        self.assertIn("stopped", [v for f in filters if f["Name"] == "instance-state-name" for v in f["Values"]])

    def test_tasks_older_than_an_hour_are_stopped_in_the_boot_race_cluster_only(self):
        ecs = FakeEcs([{"taskArn": "t-old", "createdAt": NOW - 1.5 * H},
                       {"taskArn": "t-young", "createdAt": NOW - 0.5 * H}])
        out = self.run_with(FakeEc2([], [], []), ecs)
        self.assertEqual(out["tasks"], ["t-old"])
        self.assertEqual(ecs.list_kwargs.kwargs["cluster"], "boot-race")

    def test_images_and_snapshots_older_than_a_day_go_and_an_in_use_snapshot_does_not_stop_the_run(self):
        ec2 = FakeEc2([], [{"ImageId": "ami-old", "CreationDate": (NOW - 25 * H).isoformat().replace("+00:00", "Z")},
                           {"ImageId": "ami-new", "CreationDate": (NOW - 2 * H).isoformat()}],
                      [{"SnapshotId": "snap-old", "StartTime": NOW - 25 * H},
                       {"SnapshotId": "snap-busy", "StartTime": NOW - 26 * H},
                       {"SnapshotId": "snap-new", "StartTime": NOW - 2 * H}], in_use=["snap-busy"])
        out = self.run_with(ec2, FakeEcs([]))
        self.assertEqual(out["images"], ["ami-old"])
        self.assertEqual(out["snapshots"], ["snap-old"])
        self.assertEqual(ec2.image_filters, (["self"], [{"Name": "tag:Project", "Values": ["boot-race"]}]))


def block(text, header):
    start = text.index(header)
    return text[start:text.index("\n}\n", start)]


class Terraform(unittest.TestCase):
    def test_it_runs_every_15_minutes(self):
        sched = block(TF, 'resource "aws_scheduler_schedule" "boot_race_reaper"')
        self.assertIn('schedule_expression = "rate(15 minutes)"', sched)
        self.assertIn("aws_lambda_function.boot_race_reaper.arn", sched)

    def test_its_limits_are_three_hours_one_hour_and_a_day(self):
        fn = block(TF, 'resource "aws_lambda_function" "boot_race_reaper"')
        self.assertIn('INSTANCE_LIMIT_S = "10800"', fn)
        self.assertIn('TASK_LIMIT_S     = "3600"', fn)
        self.assertIn('IMAGE_LIMIT_S    = "86400"', fn)
        self.assertNotIn("ignore_changes", fn)

    def test_every_mutating_grant_is_held_to_the_tag_or_the_cluster(self):
        doc = block(TF, 'data "aws_iam_policy_document" "boot_race_reaper"')
        for st in re.findall(r"statement \{.*?\n  \}", doc, re.S):
            actions = re.search(r"actions\s*=\s*\[(.*?)\]", st, re.S).group(1)
            if re.search(r"Terminate|Deregister|DeleteSnapshot|StopTask", actions):
                self.assertTrue('"ec2:ResourceTag/Project"' in st or '"ecs:cluster"' in st, st)
                self.assertNotIn('"*"', st)


class AmiPin(unittest.TestCase):
    def test_the_template_and_the_grant_name_the_same_ecs_image(self):
        # An AWS "recommended" release must not move the grant away from the image the template launches.
        self.assertNotIn("aws_ssm_parameter", TF)
        template = block(TF, 'resource "aws_launch_template" "boot_race_ecs_host"')
        self.assertIn("image_id      = var.boot_race_ecs_ami", template)
        self.assertNotIn("ignore_changes", template)
        self.assertIn('image/${var.boot_race_ecs_ami}"', TF)


if __name__ == "__main__":
    unittest.main(buffer=True)
