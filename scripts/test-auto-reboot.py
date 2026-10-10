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
    """series: metric -> list of values (placed in consecutive buckets ending one period before now, like fresh
    CloudWatch data) or a list of (timestamp, value) pairs for stale and gappy data."""

    def __init__(self, series):
        self.series = series

    def get_metric_statistics(self, MetricName, Statistics, Period, **kw):
        stat, values = Statistics[0], self.series.get(MetricName, [])
        if values and isinstance(values[0], tuple):
            pts = values
        else:
            pts = [(NOW - Period * (len(values) - i), v) for i, v in enumerate(values)]
        return {"Datapoints": [{"Timestamp": t, stat: v} for t, v in pts]}


class ConditionalCheckFailedException(Exception):
    pass


class FakeDDB:
    def __init__(self, calls, items=None, fail=False):
        self.calls, self.items, self.fail = calls, items or {}, fail

    def get_item(self, TableName, Key):
        return {"Item": self.items[Key["instance_id"]["S"]]} if Key["instance_id"]["S"] in self.items else {}

    def put_item(self, TableName, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        if self.fail:
            raise RuntimeError("dynamodb unavailable")
        iid = Item["instance_id"]["S"]
        if ConditionExpression:
            cutoff = int(ExpressionAttributeValues[":cutoff"]["N"])
            current = self.items.get(iid, {}).get("last_reboot")
            if current is not None and int(current["N"]) >= cutoff:
                raise ConditionalCheckFailedException("condition failed")
        self.items[iid] = Item
        self.calls.log.append(("save", iid))


    def update_item(self, TableName, Key, UpdateExpression, ExpressionAttributeValues):
        iid = Key["instance_id"]["S"]
        item = dict(self.items.get(iid, {"instance_id": {"S": iid}}))
        item["last_alert"] = ExpressionAttributeValues[":a"]
        self.items[iid] = item
        self.calls.log.append(("alert-note", iid))


class FakeSNS:
    def __init__(self, calls):
        self.calls = calls

    def publish(self, TopicArn, Subject, Message):
        self.calls.log.append(("sns", Subject))


class _Body:
    def __init__(self, text):
        self.text = text

    def read(self):
        return self.text.encode()


class FakeLambda:
    """result: what the real capture Lambda returns. It reports a failed read or an empty buffer INSIDE a
    successful invocation, not as a FunctionError."""

    def __init__(self, calls, fail=False, result=None):
        self.calls, self.fail, self.result = calls, fail, result

    def invoke(self, FunctionName, InvocationType, Payload):
        iid = json.loads(json.loads(Payload)["Records"][0]["Sns"]["Message"])["Trigger"]["Dimensions"][0]["value"]
        self.calls.log.append(("capture", iid))
        if self.fail:
            raise RuntimeError("throttled")
        result = self.result if self.result is not None else {"captured": [{"instance": iid, "stream": "%s/2026" % iid, "signatures": 0}]}
        return {"Payload": _Body(json.dumps(result))}


class FakeEC2Reboot(FakeEC2):
    def __init__(self, calls, instances, fail=False):
        super().__init__(calls, instances)
        self.fail = fail

    def reboot_instances(self, InstanceIds):
        if self.fail:
            raise RuntimeError("UnauthorizedOperation")
        super().reboot_instances(InstanceIds)


def inst(iid="i-1", name="nextjs-dev", launched=OLD):
    return {"InstanceId": iid, "LaunchTime": launched, "Tags": [{"Key": "Name", "Value": name}]}


def history_item(times, last_alert=0):
    return {"reboots": {"L": [{"N": str(t)} for t in times]}, "last_alert": {"N": str(last_alert)}}


def world(series=None, instances=None, items=None, fail_capture=False, regions=None, capture_result=None,
          ddb_fail=False, reboot_fail=False):
    calls = Calls()
    series = series if series is not None else {"StatusCheckFailed_Instance": HEALTHY_STATUS, "NetworkOut": HEALTHY_NET,
                                                  "StatusCheckFailed_System": HEALTHY_STATUS}
    instances = instances if instances is not None else [inst()]
    ec2 = {r: FakeEC2Reboot(calls, instances if i == 0 else [], reboot_fail) for i, r in enumerate(["us-west-1", "us-west-2"])}
    if regions:
        ec2 = {r: FakeEC2Reboot(calls, instances if r == regions else [], reboot_fail) for r in ["us-west-1", "us-west-2"]}
    clients = {"ec2": ec2, "cw": {r: FakeCW(series) for r in ec2}, "ddb": FakeDDB(calls, items, ddb_fail), "sns": FakeSNS(calls),
               "lambda": FakeLambda(calls, fail_capture, capture_result)}
    return calls, clients


def run(clients, dry_run=False):
    return ar.lambda_handler({"dry_run": dry_run}, None, clients=clients, now=NOW)["results"]


def fresh(values, period):
    return [(NOW - period * (len(values) - i), v) for i, v in enumerate(values)]


class DecisionTests(unittest.TestCase):
    def test_only_a_full_unbroken_FRESH_run_of_bad_buckets_counts(self):
        bad = lambda n: fresh([True] * n, 60)
        self.assertTrue(ar.failing_for(bad(15), 15, 60, NOW))
        self.assertFalse(ar.failing_for(bad(14), 15, 60, NOW), "fewer buckets than the window is not evidence")
        gap = fresh([True] * 15, 60)
        gap[7] = (gap[7][0] - 60, True)                       # a bucket missing in the middle: not consecutive
        self.assertFalse(ar.failing_for(gap, 15, 60, NOW))
        self.assertFalse(ar.failing_for(fresh([True] * 7 + [False] + [True] * 7, 60), 15, 60, NOW), "one good minute breaks it")
        self.assertFalse(ar.failing_for([], 15, 60, NOW))

    def test_the_bucket_still_filling_is_not_a_completed_period(self):
        # Evenly spaced five-minute buckets (so contiguity is satisfied), the newest of which started two minutes
        # ago and is still being filled: three COMPLETED periods plus a partial one, i.e. fifteen minutes, not twenty.
        started = [NOW - 120 - 300 * k for k in (3, 2, 1, 0)]
        partial = [(t, True) for t in started]
        self.assertTrue(partial[-1][0] + 300 > NOW, "the newest bucket is still filling")
        self.assertFalse(ar.failing_for(partial, 20, 300, NOW), "fifteen minutes of silence plus a partial bucket is not twenty")
        self.assertIsNone(ar.wedged([(NOW - 60 * k, 0) for k in range(25, 0, -1)], partial, NOW))
        # one more period later the same four buckets are all complete, and then it IS twenty minutes
        self.assertTrue(ar.failing_for(partial, 20, 300, NOW + 180))
        self.assertTrue(ar.failing_for(fresh([True] * 4, 300), 20, 300, NOW), "four completed buckets are")

    def test_any_current_host_failure_is_a_veto_however_young(self):
        self.assertTrue(ar.host_failing_now(fresh([0] * 10 + [1], 60), NOW), "one failing minute is already a host fault")
        self.assertFalse(ar.host_failing_now(fresh([1] * 5 + [0], 60), NOW), "recovered")
        self.assertFalse(ar.host_failing_now([(t - 3600, 1) for t, _ in fresh([1] * 5, 60)], NOW), "an old failure is not current")
        self.assertFalse(ar.host_failing_now([], NOW))

    def test_old_bad_buckets_never_reboot_a_box_that_is_fine_now(self):
        old = [(t - 7200, True) for t, _ in fresh([True] * 15, 60)]
        self.assertFalse(ar.failing_for(old, 15, 60, NOW), "the failures ended two hours ago")
        stale = [(t - 1200, True) for t, _ in fresh([True] * 15, 60)]
        self.assertFalse(ar.failing_for(stale, 15, 60, NOW), "the newest bucket is twenty minutes old")

    def test_the_two_signals(self):
        self.assertIn("status check", ar.wedged(fresh([1] * 15, 60), fresh([5e6] * 4, 300), NOW))
        self.assertIn("no network traffic", ar.wedged(fresh([0] * 25, 60), fresh([0, 0, 0, 0], 300), NOW))
        live = ar.NETWORK_FLOOR_BYTES
        self.assertIsNone(ar.wedged(fresh([0] * 25, 60), fresh([0, 0, 0, live], 300), NOW), "real traffic in the window is a live box")
        self.assertIsNone(ar.wedged(fresh([0] * 25, 60), fresh([0, 0, 0], 300), NOW), "fifteen minutes of silence is not twenty")

    def test_the_paging_signal(self):
        healthy = fresh([0] * 25, 60), fresh([12e6] * 4, 300)
        # 2026-10-10 nextjs-dev, as CloudWatch had it: reads pinned at the gp3 cap, writes all but gone.
        reads, writes = fresh([35.8e9, 39.35e9, 39.37e9, 36e9], 300), fresh([233e6, 40e6, 3e6, 3e6], 300)
        self.assertIn("paging for 20 minutes", ar.wedged(*healthy, NOW, reads, writes))
        self.assertIsNone(ar.wedged(*healthy, NOW, reads[1:], writes[1:]), "fifteen minutes of paging is not twenty")
        build = fresh([39e9] * 4, 300), fresh([9e9] * 4, 300)
        self.assertIsNone(ar.wedged(*healthy, NOW, *build), "a box reading hard and writing too is working, not paging")
        busy = fresh([20e9] * 4, 300), fresh([0] * 4, 300)
        self.assertIsNone(ar.wedged(*healthy, NOW, *busy), "reads well under the cap are not paging")
        self.assertIsNone(ar.wedged(*healthy, NOW, reads, []), "no write samples is no evidence of low writes")
        self.assertIsNone(ar.wedged(*healthy, NOW, reads, writes[:3]), "one missing write bucket breaks the run")
        recovered = fresh([39e9, 39e9, 39e9, 2e9], 300), fresh([0] * 4, 300)
        self.assertIsNone(ar.wedged(*healthy, NOW, *recovered), "one normal bucket breaks the run")

    def test_a_trickle_is_not_life(self):
        """2026-10-08: the jumpbox's RCU stall left the kernel answering TCP while userspace starved, so NetworkOut
        fell to 11,468 bytes per five minutes and never to zero. An exactly-zero rule never fired and the box sat
        wedged with its status check reading "ok"."""
        stalled = fresh([11468] * 4, 300)
        self.assertIn("no network traffic", ar.wedged(fresh([0] * 25, 60), stalled, NOW), "the measured RCU-stall trickle is a wedge")
        # The floor must clear the quietest HEALTHY bucket measured on any box on the list (jumpbox-2, 34,909 bytes).
        self.assertLess(ar.NETWORK_FLOOR_BYTES, 34909, "the floor must sit below the quietest healthy box")
        self.assertGreater(ar.NETWORK_FLOOR_BYTES, 11468, "and above the measured wedge")
        for healthy in (34909, 68259, 152048, 684878):
            self.assertIsNone(ar.wedged(fresh([0] * 25, 60), fresh([healthy] * 4, 300), NOW), "%d bytes is a live box" % healthy)
        # One healthy bucket in the window still breaks the run, exactly as a non-zero one used to.
        self.assertIsNone(ar.wedged(fresh([0] * 25, 60), fresh([11468, 11468, 68259, 11468], 300), NOW), "one live bucket breaks it")

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
        self.assertEqual([c[0] for c in calls.log], ["save", "capture", "reboot", "sns"],
                         "reserve it, take the evidence, THEN reboot")
        self.assertEqual(calls.log[2], ("reboot", ("i-1",)))
        self.assertIn(NOW, [int(x["N"]) for x in clients["ddb"].items["i-1"]["reboots"]["L"]])

    def test_a_dead_network_with_an_ok_status_check_is_a_wedge_too(self):
        calls, clients = world({"StatusCheckFailed_Instance": HEALTHY_STATUS, "NetworkOut": [0, 0, 0, 0, 0]})
        r = run(clients)[0]
        self.assertEqual(r["action"], "rebooted")
        self.assertIn("no network traffic", r["reason"])

    def test_paging_with_an_ok_status_check_and_some_network_is_a_wedge_too(self):
        calls, clients = world({"StatusCheckFailed_Instance": HEALTHY_STATUS, "NetworkOut": [53e6, 12e6, 27e3, 27e3],
                                "EBSReadBytes": [35.8e9, 39.35e9, 39.37e9, 36e9], "EBSWriteBytes": [233e6, 40e6, 3e6, 3e6]})
        r = run(clients)[0]
        self.assertEqual(r["action"], "rebooted")
        self.assertIn("paging", r["reason"])
        self.assertEqual([c[0] for c in calls.log], ["save", "capture", "reboot", "sns"])

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

    def test_an_empty_or_failed_capture_is_reported_honestly_not_as_archived(self):
        for result, word in (({"captured": [{"instance": "i-1", "note": "console buffer empty"}]}, "buffer empty"),
                             ({"captured": [{"instance": "i-1", "error": "console read failed: x"}]}, "console read failed"),
                             ({"captured": []}, "no result")):
            calls, clients = world({"StatusCheckFailed_Instance": [1] * 25, "NetworkOut": [0] * 5}, capture_result=result)
            r = run(clients)[0]
            self.assertEqual(r["action"], "rebooted", "the box still comes back")
            self.assertTrue(r["console"].startswith("NOT archived"), r["console"])
            self.assertIn(word, r["console"])
        calls, clients = world({"StatusCheckFailed_Instance": [1] * 25, "NetworkOut": [0] * 5})
        self.assertIn("console archived", run(clients)[0]["console"])

    def test_old_data_does_not_reboot_a_box_that_is_healthy_now(self):
        old = {"StatusCheckFailed_Instance": [(NOW - 7200 - 60 * i, 1) for i in range(25)][::-1],
               "NetworkOut": [(NOW - 7200 - 300 * i, 0) for i in range(6)][::-1]}
        calls, clients = world(old)
        self.assertEqual(run(clients)[0]["action"], "none")
        self.assertEqual(calls.log, [])

    def test_a_gap_in_the_data_is_not_a_wedge(self):
        gappy = [(NOW - 300 * i, 0) for i in (1, 2, 4, 5)][::-1]       # a bucket missing
        calls, clients = world({"StatusCheckFailed_Instance": [0] * 25, "NetworkOut": gappy})
        self.assertEqual(run(clients)[0]["action"], "none")

    def test_a_host_problem_vetoes_a_reboot_even_when_the_guest_looks_dead_too(self):
        # A dead host also zeroes the network and fails the instance check: it must still be alert-only.
        calls, clients = world({"StatusCheckFailed_Instance": [1] * 25, "NetworkOut": [0] * 5, "StatusCheckFailed_System": [1] * 25})
        self.assertEqual(run(clients)[0]["action"], "alert")
        self.assertNotIn("reboot", [c[0] for c in calls.log])
        self.assertNotIn("capture", [c[0] for c in calls.log])

    def test_a_young_host_fault_with_a_dead_network_is_still_not_rebooted(self):
        calls, clients = world({"StatusCheckFailed_Instance": [1] * 25, "NetworkOut": [0] * 5,
                                "StatusCheckFailed_System": [0] * 20 + [1] * 2})
        self.assertEqual(run(clients)[0]["action"], "alert")
        self.assertNotIn("reboot", [c[0] for c in calls.log])

    def test_recording_an_alert_never_overwrites_a_reservation_another_run_just_made(self):
        calls, clients = world({"StatusCheckFailed_Instance": HEALTHY_STATUS, "NetworkOut": HEALTHY_NET,
                                "StatusCheckFailed_System": [1] * 5},
                               items={"i-1": history_item([NOW - 10], 0)})
        before = dict(clients["ddb"].items["i-1"])
        # this run read the table BEFORE the other run reserved; it must still leave the reservation alone
        stale = {"reboots": {"L": []}, "last_alert": {"N": "0"}}
        clients["ddb"].get_item = lambda TableName, Key: {"Item": stale}
        self.assertEqual(run(clients)[0]["action"], "alert")
        self.assertEqual(clients["ddb"].items["i-1"]["reboots"], before["reboots"], "the reservation survives")
        self.assertNotIn(("save", "i-1"), calls.log, "the alert path never rewrites the whole item")
        self.assertIn(("alert-note", "i-1"), calls.log)

    def test_if_the_reservation_cannot_be_written_nothing_is_rebooted(self):
        calls, clients = world({"StatusCheckFailed_Instance": [1] * 25, "NetworkOut": [0] * 5}, ddb_fail=True)
        with self.assertRaises(RuntimeError):
            run(clients)
        self.assertNotIn("reboot", [c[0] for c in calls.log], "no bookkeeping, no reboot: it would otherwise repeat every five minutes")

    def test_two_overlapping_runs_cannot_both_reboot_the_box(self):
        calls, clients = world({"StatusCheckFailed_Instance": [1] * 25, "NetworkOut": [0] * 5})
        self.assertEqual(run(clients)[0]["action"], "rebooted")
        # a second run that read the table BEFORE the first one wrote it: the conditional write refuses
        clients["ddb"].get_item = lambda TableName, Key: {}
        again = run(clients)[0]
        self.assertEqual(again["action"], "blocked")
        self.assertIn("another run", again["why"])
        self.assertEqual([c[0] for c in calls.log].count("reboot"), 1)

    def test_a_reboot_that_fails_gives_the_reservation_back_and_is_an_error(self):
        calls, clients = world({"StatusCheckFailed_Instance": [1] * 25, "NetworkOut": [0] * 5}, reboot_fail=True)
        with self.assertRaises(RuntimeError):
            run(clients)
        item = clients["ddb"].items["i-1"]
        self.assertEqual(item["reboots"]["L"], [], "it was not rebooted, so it is not on cooldown")

    def test_a_failure_on_one_box_fails_the_invocation_after_the_others_were_handled(self):
        calls, clients = world({"StatusCheckFailed_Instance": [1] * 25, "NetworkOut": [0] * 5},
                               instances=[inst("i-bad"), inst("i-ok", "jumpbox")])
        real = clients["cw"]["us-west-1"].get_metric_statistics
        def flaky(**kw):
            if {"Name": "InstanceId", "Value": "i-bad"} in kw["Dimensions"]:
                raise RuntimeError("AccessDenied")
            return real(**kw)
        clients["cw"]["us-west-1"].get_metric_statistics = flaky
        with self.assertRaises(RuntimeError) as ctx:
            run(clients)
        self.assertIn("AccessDenied", str(ctx.exception), "so AWS/Lambda Errors counts it and the alarm fires")
        self.assertIn(("reboot", ("i-ok",)), calls.log, "the other box was still looked after")
        self.assertIn("sns", [c[0] for c in calls.log])

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


class TerraformTests(unittest.TestCase):
    def names(self):
        return re.findall(r'"([a-z0-9-]+)"', re.search(r"auto_reboot_names\s*=\s*\[(.*?)\]", TF, re.S).group(1))

    def test_the_list_is_the_on_demand_boxes_and_no_spot_or_ephemeral_one(self):
        self.assertEqual(sorted(self.names()), sorted(["jumpbox", "jumpbox-2", "nextjs-dev", "claude-master-server"]))
        for spot_or_ephemeral in ("metal", "io-box", "parallel", "gpu", "wbox", "runner", "mac"):
            self.assertFalse(any(spot_or_ephemeral in n for n in self.names()), spot_or_ephemeral)

    def test_no_listed_box_is_a_spot_instance(self):
        # The owner wants automatic restarts for on-demand boxes only. Find each listed box's instance resource by
        # its Name tag and require that it has no spot market options.
        for name in self.names():
            for path in ROOT.glob("*.tf"):
                text = path.read_text()
                for m in re.finditer(r'^resource "aws_instance" "[a-z0-9_]+" \{\n.*?^\}', text, re.S | re.M):
                    body = m.group()
                    if re.search(r'Name\s*=\s*"%s"' % re.escape(name), body):
                        self.assertNotIn("instance_market_options", body, "%s is a spot instance" % name)

    def test_only_the_home_region_is_searched(self):
        self.assertEqual(re.search(r"auto_reboot_regions\s*=\s*(\[[^\]]*\])", TF).group(1), '["us-west-1"]')

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
        self.assertEqual((ar.THRASH_MINUTES, ar.THRASH_READ_BYTES, ar.THRASH_WRITE_SHARE), (20, 30 * 1024 ** 3, 0.1))
        self.assertEqual(ar.NETWORK_FLOOR_BYTES, 20 * 1024)
        for words in ("15 minutes in a row", "under a 20 KiB floor for 20", "3 hours or 3 times in 24",
                      "paging for 20 minutes", "30 GiB or more per five minutes", "under a tenth"):
            self.assertIn(words, TF)

    def test_a_failed_run_is_not_retried_by_aws(self):
        self.assertIn("maximum_retry_attempts = 0", TF)
        self.assertIn('aws_lambda_function_event_invoke_config" "auto_reboot"', TF)

    def test_the_capture_function_is_the_existing_one(self):
        self.assertIn("aws_lambda_function.console_capture", TF)
        self.assertIn("lambda:InvokeFunction", TF)


if __name__ == "__main__":
    unittest.main(verbosity=2)
