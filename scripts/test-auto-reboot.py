#!/usr/bin/env python3
"""scripts/auto-reboot.py and auto-reboot.tf: a wedged persistent box is rebooted, and nothing else is.
Offline: fake AWS clients and the Terraform source."""
import datetime
import importlib.util
import json
import os
import re
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.update({
    "TARGET_NAMES": "jumpbox,nextjs-dev,io-box", "REGIONS": "us-west-1,us-west-2", "STATE_TABLE": "t",
    "SNS_TOPIC_ARN": "arn:topic", "CONSOLE_CAPTURE_FUNCTION": "capture", "AWS_REGION": "us-west-1"})
sys.modules.setdefault("boto3", types.ModuleType("boto3"))
SPEC = importlib.util.spec_from_file_location("ar", ROOT / "scripts" / "auto-reboot.py")
ar = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ar)
TF = (ROOT / "auto-reboot.tf").read_text()

NOW = 1_800_000_000
OLD = datetime.datetime.fromtimestamp(NOW - 5 * 86400, datetime.timezone.utc)
HEALTHY_STATUS = [0] * 25
HEALTHY_NET = [5e6] * 6


class Calls:
    def __init__(self):
        self.log = []


class FakeEC2:
    def __init__(self, calls, instances):
        self.calls, self.instances, self.filters = calls, instances, None

    def describe_instances(self, Filters):
        self.filters = Filters
        return {"Reservations": [{"Instances": self.instances}]}

    def reboot_instances(self, InstanceIds):
        self.calls.log.append(("reboot", tuple(InstanceIds)))


class FakeCW:
    def __init__(self, series):
        self.series = series                      # metric name -> list of values

    def get_metric_statistics(self, MetricName, Statistics, **kw):
        stat = Statistics[0]
        return {"Datapoints": [{"Timestamp": i, stat: v} for i, v in enumerate(self.series.get(MetricName, []))]}


class FakeDDB:
    def __init__(self, calls, items=None):
        self.calls, self.items = calls, items or {}

    def get_item(self, TableName, Key):
        return {"Item": self.items[Key["instance_id"]["S"]]} if Key["instance_id"]["S"] in self.items else {}

    def put_item(self, TableName, Item):
        self.items[Item["instance_id"]["S"]] = Item
        self.calls.log.append(("save", Item["instance_id"]["S"]))


class FakeSNS:
    def __init__(self, calls):
        self.calls = calls

    def publish(self, TopicArn, Subject, Message):
        self.calls.log.append(("sns", Subject))


class FakeLambda:
    def __init__(self, calls, fail=False):
        self.calls, self.fail = calls, fail

    def invoke(self, FunctionName, InvocationType, Payload):
        self.calls.log.append(("capture", json.loads(json.loads(Payload)["Records"][0]["Sns"]["Message"])["Trigger"]["Dimensions"][0]["value"]))
        if self.fail:
            raise RuntimeError("throttled")
        return {}


def inst(iid="i-1", name="nextjs-dev", launched=OLD):
    return {"InstanceId": iid, "LaunchTime": launched, "Tags": [{"Key": "Name", "Value": name}]}


def history_item(times, last_alert=0):
    return {"reboots": {"L": [{"N": str(t)} for t in times]}, "last_alert": {"N": str(last_alert)}}


def world(series=None, instances=None, items=None, fail_capture=False, regions=None):
    calls = Calls()
    series = series if series is not None else {"StatusCheckFailed_Instance": HEALTHY_STATUS, "NetworkOut": HEALTHY_NET,
                                                  "StatusCheckFailed_System": HEALTHY_STATUS}
    instances = instances if instances is not None else [inst()]
    ec2 = {r: FakeEC2(calls, instances if i == 0 else []) for i, r in enumerate(["us-west-1", "us-west-2"])}
    if regions:
        ec2 = {r: FakeEC2(calls, instances if r == regions else []) for r in ["us-west-1", "us-west-2"]}
    clients = {"ec2": ec2, "cw": {r: FakeCW(series) for r in ec2}, "ddb": FakeDDB(calls, items), "sns": FakeSNS(calls),
               "lambda": FakeLambda(calls, fail_capture)}
    return calls, clients


def run(clients, dry_run=False):
    return ar.lambda_handler({"dry_run": dry_run}, None, clients=clients, now=NOW)["results"]


class DecisionTests(unittest.TestCase):
    def test_only_a_full_unbroken_run_of_bad_minutes_counts(self):
        self.assertTrue(ar.failing_for([True] * 15, 15, 60))
        self.assertFalse(ar.failing_for([True] * 14, 15, 60), "fewer points than the window is not evidence")
        self.assertFalse(ar.failing_for([True] * 7 + [False] + [True] * 7, 15, 60), "one good minute breaks it")
        self.assertTrue(ar.failing_for([False] * 5 + [True] * 15, 15, 60), "only the last window matters")
        self.assertFalse(ar.failing_for([], 15, 60))

    def test_the_two_signals(self):
        self.assertIn("status check", ar.wedged([1] * 15, HEALTHY_NET))
        self.assertIn("no network traffic", ar.wedged(HEALTHY_STATUS, [0, 0, 0, 0]))
        self.assertIsNone(ar.wedged(HEALTHY_STATUS, [0, 0, 0, 5]), "any traffic in the window is a live box")
        self.assertIsNone(ar.wedged(HEALTHY_STATUS, [0, 0, 0]), "fifteen minutes of silence is not twenty")

    def test_the_brakes(self):
        self.assertEqual(ar.may_reboot([], NOW), (True, ""))
        self.assertFalse(ar.may_reboot([NOW - 3600], NOW)[0], "inside the cooldown")
        self.assertTrue(ar.may_reboot([NOW - 4 * 3600], NOW)[0])
        spread = [NOW - 20 * 3600, NOW - 12 * 3600, NOW - 5 * 3600]
        self.assertFalse(ar.may_reboot(spread, NOW)[0], "three in a day is the limit")
        self.assertTrue(ar.may_reboot([NOW - 30 * 3600, NOW - 26 * 3600, NOW - 25 * 3600], NOW)[0], "older than a day does not count")


class BehaviourTests(unittest.TestCase):
    def test_a_healthy_box_is_left_alone(self):
        calls, clients = world()
        self.assertEqual(run(clients)[0]["action"], "none")
        self.assertEqual(calls.log, [])

    def test_only_running_boxes_on_the_list_are_even_asked_about(self):
        calls, clients = world()
        run(clients)
        f = {x["Name"]: x["Values"] for x in clients["ec2"]["us-west-1"].filters}
        self.assertEqual(f["instance-state-name"], ["running"], "a deliberately stopped box is never touched")
        self.assertEqual(f["tag:Name"], ["jumpbox", "nextjs-dev", "io-box"])

    def test_a_failing_status_check_gets_the_console_captured_then_one_reboot_then_a_message(self):
        calls, clients = world({"StatusCheckFailed_Instance": [1] * 25, "NetworkOut": HEALTHY_NET})
        r = run(clients)[0]
        self.assertEqual(r["action"], "rebooted")
        self.assertEqual([c[0] for c in calls.log], ["capture", "reboot", "save", "sns"], "the evidence is taken BEFORE the reboot")
        self.assertEqual(calls.log[1], ("reboot", ("i-1",)))
        self.assertIn(NOW, [int(x["N"]) for x in clients["ddb"].items["i-1"]["reboots"]["L"]])

    def test_a_dead_network_with_an_ok_status_check_is_a_wedge_too(self):
        calls, clients = world({"StatusCheckFailed_Instance": HEALTHY_STATUS, "NetworkOut": [0, 0, 0, 0, 0]})
        r = run(clients)[0]
        self.assertEqual(r["action"], "rebooted")
        self.assertIn("no network traffic", r["reason"])

    def test_missing_data_is_not_a_wedge(self):
        calls, clients = world({"StatusCheckFailed_Instance": [1] * 5, "NetworkOut": [0, 0]})
        self.assertEqual(run(clients)[0]["action"], "none")
        self.assertNotIn("reboot", [c[0] for c in calls.log])

    def test_a_box_that_just_started_is_given_time_to_boot(self):
        young = datetime.datetime.fromtimestamp(NOW - 600, datetime.timezone.utc)
        calls, clients = world({"StatusCheckFailed_Instance": [1] * 25, "NetworkOut": [0] * 5}, instances=[inst(launched=young)])
        self.assertEqual(run(clients)[0]["action"], "skip")
        self.assertEqual(calls.log, [])

    def test_a_recently_rebooted_box_is_not_rebooted_again_and_the_alert_is_not_repeated(self):
        bad = {"StatusCheckFailed_Instance": [1] * 25, "NetworkOut": [0] * 5}
        calls, clients = world(bad, items={"i-1": history_item([NOW - 3600])})
        r = run(clients)[0]
        self.assertEqual(r["action"], "blocked")
        self.assertNotIn("reboot", [c[0] for c in calls.log])
        self.assertEqual([c[0] for c in calls.log].count("sns"), 1, "it says so once")
        calls.log.clear()
        run(clients)
        self.assertEqual(calls.log, [], "and not again on the next five-minute tick")

    def test_three_in_a_day_stops_it_for_good_until_a_person_looks(self):
        bad = {"StatusCheckFailed_Instance": [1] * 25, "NetworkOut": [0] * 5}
        calls, clients = world(bad, items={"i-1": history_item([NOW - 20 * 3600, NOW - 12 * 3600, NOW - 5 * 3600])})
        self.assertEqual(run(clients)[0]["action"], "blocked")
        self.assertNotIn("reboot", [c[0] for c in calls.log])

    def test_a_dry_run_changes_nothing(self):
        calls, clients = world({"StatusCheckFailed_Instance": [1] * 25, "NetworkOut": [0] * 5})
        self.assertEqual(run(clients, dry_run=True)[0]["action"], "would-reboot")
        self.assertEqual(calls.log, [])

    def test_a_failed_console_capture_never_keeps_a_wedged_box_down(self):
        calls, clients = world({"StatusCheckFailed_Instance": [1] * 25, "NetworkOut": [0] * 5}, fail_capture=True)
        r = run(clients)[0]
        self.assertEqual(r["action"], "rebooted")
        self.assertIn("capture failed", r["console"])
        self.assertIn("reboot", [c[0] for c in calls.log])

    def test_a_box_in_another_region_is_rebooted_but_its_console_is_not_claimed_captured(self):
        calls, clients = world({"StatusCheckFailed_Instance": [1] * 25, "NetworkOut": [0] * 5},
                               instances=[inst("i-2", "io-box")], regions="us-west-2")
        r = run(clients)[0]
        self.assertEqual(r["action"], "rebooted")
        self.assertIn("not captured", r["console"])
        self.assertNotIn("capture", [c[0] for c in calls.log])

    def test_a_host_problem_is_an_alert_not_a_reboot(self):
        calls, clients = world({"StatusCheckFailed_Instance": HEALTHY_STATUS, "NetworkOut": HEALTHY_NET, "StatusCheckFailed_System": [1] * 25})
        self.assertEqual(run(clients)[0]["action"], "alert")
        self.assertNotIn("reboot", [c[0] for c in calls.log])
        self.assertIn("sns", [c[0] for c in calls.log])

    def test_one_box_failing_to_check_does_not_stop_the_others_being_checked(self):
        calls, clients = world({"StatusCheckFailed_Instance": [1] * 25, "NetworkOut": [0] * 5},
                               instances=[inst("i-bad"), inst("i-ok", "jumpbox")])
        real = clients["cw"]["us-west-1"].get_metric_statistics
        def flaky(**kw):
            if {"Name": "InstanceId", "Value": "i-bad"} in kw["Dimensions"]:
                raise RuntimeError("throttled")
            return real(**kw)
        clients["cw"]["us-west-1"].get_metric_statistics = flaky
        by = {r["instance"]: r["action"] for r in run(clients)}
        self.assertEqual(by, {"i-bad": "error", "i-ok": "rebooted"})


class TerraformTests(unittest.TestCase):
    def names(self):
        return re.findall(r'"([a-z0-9-]+)"', re.search(r"auto_reboot_names\s*=\s*\[(.*?)\]", TF, re.S).group(1))

    def test_the_list_is_the_persistent_boxes_and_no_ephemeral_one(self):
        self.assertEqual(sorted(self.names()), sorted(["jumpbox", "jumpbox-2", "fcvm-metal-arm", "fcvm-metal-x86", "nextjs-dev", "claude-master-server", "io-box"]))
        for ephemeral in ("parallel", "gpu", "wbox", "runner", "mac"):
            self.assertFalse(any(ephemeral in n for n in self.names()), ephemeral)

    def test_every_name_is_a_real_name_tag_in_the_repo(self):
        text = "".join(p.read_text() for p in ROOT.glob("*.tf"))
        for name in self.names():
            self.assertRegex(text, r'Name\s*=\s*"%s"' % re.escape(name), name)

    def test_the_role_can_reboot_only_by_name_and_do_nothing_else_to_an_instance(self):
        role = re.search(r'resource "aws_iam_role_policy" "auto_reboot" \{.*?\n\}', TF, re.S).group()
        self.assertIn('"ec2:ResourceTag/Name" = local.auto_reboot_names', role)
        self.assertIn('Action   = "ec2:RebootInstances"', role)
        actions = re.findall(r"[a-z0-9]+:[A-Za-z*]+", " ".join(re.findall(r"Action\s*=\s*(\[[^\]]*\]|\"[^\"]*\")", role)))
        self.assertEqual(sorted(a for a in actions if a.startswith("ec2:")), ["ec2:DescribeInstances", "ec2:RebootInstances"],
                         "the only EC2 actions: look, and reboot")
        self.assertFalse([a for a in actions if a.endswith("*") or a.startswith(("iam:", "ssm:"))], "no wildcard action, no IAM, no SSM")

    def test_it_runs_every_five_minutes_and_reports_when_it_does_not(self):
        self.assertIn('schedule_expression = "rate(5 minutes)"', TF)
        alarm = re.search(r'"auto_reboot_not_running" \{.*?\n\}', TF, re.S).group()
        self.assertIn('treat_missing_data  = "breaching"', alarm)
        self.assertIn("aws_sns_topic.cost_alerts.arn", alarm)
        self.assertIn('"auto_reboot_errors"', TF)

    def test_the_brake_state_is_a_table_the_lambda_can_only_read_and_write_an_item_of(self):
        self.assertIn('hash_key     = "instance_id"', TF)
        self.assertIn('["dynamodb:GetItem", "dynamodb:PutItem"]', TF)

    def test_the_comment_and_the_code_agree_on_the_thresholds(self):
        self.assertEqual((ar.STATUS_MINUTES, ar.NETWORK_MINUTES, ar.COOLDOWN_SECONDS, ar.MAX_PER_DAY), (15, 20, 10800, 3))
        for words in ("15 minutes in a row", "exactly zero for 20", "3 hours or 3 times in 24"):
            self.assertIn(words, TF)

    def test_the_capture_function_is_the_existing_one(self):
        self.assertIn("aws_lambda_function.console_capture", TF)
        self.assertIn("lambda:InvokeFunction", TF)


if __name__ == "__main__":
    unittest.main(verbosity=2)
