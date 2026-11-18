#!/usr/bin/env python3
"""Exercise both runner loss collector inputs without credentials or network."""

import base64
import contextlib
import gzip
import importlib.util
import io
import json
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


class ServiceError(Exception):
    pass


boto3 = types.ModuleType("boto3")
errors = types.ModuleType("botocore.exceptions")
errors.BotoCoreError = errors.ClientError = ServiceError
spec = importlib.util.spec_from_file_location(
    "collector", Path(__file__).resolve().parents[1] / "runner-loss/collector.py")
collector = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"boto3": boto3, "botocore": types.ModuleType("botocore"),
                              "botocore.exceptions": errors}):
    spec.loader.exec_module(collector)

REGION = "us-east-2"
IID = "i-000000000000000a7"
SID = "sir-a1b2c3d4"
TIME = datetime(2026, 11, 13, 18, 4, 29, tzinfo=timezone.utc)
SCHEDULE = {"source": "aws.events", "detail-type": "Scheduled Event"}


def runner(iid=IID, name="github-runner-x86_64", role="github-runner", state="terminated"):
    return {"InstanceId": iid, "InstanceType": "r5d.metal", "State": {"Name": state},
            "StateReason": {"Code": "Server.SpotInstanceTermination", "Message": "PRIVATE"},
            "StateTransitionReason": "PRIVATE", "Placement": {"AvailabilityZone": "us-east-2a"},
            "LaunchTime": TIME - timedelta(hours=1), "Tags": [{"Key": "Name", "Value": name},
            {"Key": "Role", "Value": role}, {"Key": "project", "Value": "PRIVATE"}]}


def instance_page(*instances):
    return {"Reservations": [{"Instances": list(instances)}]}


class EC2:
    def __init__(self, instances=(), spots=()):
        self.pages = {"describe_instances": instances, "describe_spot_instance_requests": spots}
        self.calls = []

    def get_paginator(self, api):
        def paginate(**kwargs):
            self.calls.append((api, kwargs))
            for page in self.pages[api]:
                if isinstance(page, Exception):
                    raise page
                yield page
        return types.SimpleNamespace(paginate=paginate)


def subscription(messages, group="/aws/lambda/github-runner-cleanup", kind="DATA_MESSAGE"):
    records = [{"id": "1", "timestamp": 1000, "message": msg if isinstance(msg, str) else json.dumps(msg)}
               for msg in messages]
    payload = {"messageType": kind, "logGroup": group, "logStream": "PRIVATE", "logEvents": records}
    return {"awslogs": {"data": base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()}}


def accepted(**updates):
    return {"event": "runner_termination_accepted", "reason": "hard-ceiling", "region": REGION,
            "instance_id": IID, **updates}


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.clients = {REGION: EC2()}
        self.env = patch.dict(os.environ, {"RUNNER_REGIONS": json.dumps([REGION])})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.api = patch.object(boto3, "client", create=True, side_effect=self.client).start()
        self.addCleanup(patch.stopall)
        self.output = io.StringIO()

    def client(self, service, region_name):
        self.assertEqual(service, "ec2")
        return self.clients[region_name]

    def collect(self, event):
        with contextlib.redirect_stdout(self.output):
            collector.handler(event, None)
        return self.records()

    def records(self):
        self.assertNotIn("PRIVATE", self.output.getvalue())
        return [json.loads(line) for line in self.output.getvalue().splitlines()]

    def test_scan_projects_exact_fcvm_fleet_and_follows_both_paginators(self):
        spot = {**runner(), "InstanceLifecycle": "spot", "SpotInstanceRequestId": SID}
        other = runner("i-11111111", name="github-runner-arm64", state="shutting-down")
        other.pop("StateReason")
        client = EC2([instance_page(spot, runner(role="app"), runner(name="app-ci"),
                                    runner(state="running"), runner(iid="PRIVATE")), instance_page(other)],
                     [{"SpotInstanceRequests": [{"InstanceId": IID, "SpotInstanceRequestId": "sir-wrong"}]},
                      {"SpotInstanceRequests": [{"InstanceId": IID, "SpotInstanceRequestId": SID,
                       "Status": {"Code": "instance-terminated-no-capacity", "UpdateTime": TIME,
                                  "Message": "PRIVATE"}}]}])
        self.clients[REGION] = client
        end, progress, heartbeat = self.collect(SCHEDULE)
        self.assertEqual(end["event"], "runner_instance_end_observed")
        self.assertEqual((end["instance_id"], end["state"], end["state_reason_code"]),
                         (IID, "terminated", "Server.SpotInstanceTermination"))
        self.assertEqual((end["spot_status_code"], end["spot_request_id"], end["spot_status_at"]),
                         ("instance-terminated-no-capacity", SID, TIME.isoformat()))
        self.assertEqual((end["market"], end["architecture"], end["launch_time"], end["zone"], end["instance_type"]),
                         ("spot", "x86_64", (TIME - timedelta(hours=1)).isoformat(), "us-east-2a", "r5d.metal"))
        self.assertIn("observed_at", end)
        self.assertNotIn("ended_at", end)
        self.assertEqual((progress["event"], progress["market"], progress["architecture"], progress["state_reason_code"]),
                         ("runner_instance_shutdown_observed", "on-demand", "arm64", None))
        self.assertEqual(heartbeat, {"event": "runner_loss_scan_complete"})
        filters = {f["Name"]: set(f["Values"]) for f in client.calls[0][1]["Filters"]}
        self.assertEqual(filters, {"tag:Role": {"github-runner"}, "tag:Name": {"github-runner-arm64",
                         "github-runner-x86_64"}, "instance-state-name": {"shutting-down", "terminated"}})
        self.assertEqual(client.calls[1], ("describe_spot_instance_requests", {
            "Filters": [{"Name": "instance-id", "Values": [IID]}]}))

    def test_unknown_evidence_stays_unknown_and_observation_is_repeatable(self):
        spot = {**runner(), "StateReason": {}, "InstanceLifecycle": "spot", "SpotInstanceRequestId": SID}
        self.clients[REGION] = EC2([instance_page(spot)], [{"SpotInstanceRequests": []}])
        self.collect(SCHEDULE)
        first, _, second, _ = self.collect(SCHEDULE)
        for obs in (first, second):
            self.assertEqual(obs["instance_id"], IID)
            self.assertIsNone(obs["spot_status_code"])
            self.assertIsNone(obs["state_reason_code"])
            self.assertNotIn("reason", obs)

    def test_spot_enrichment_failure_preserves_ec2_evidence_and_fails_scan(self):
        self.clients[REGION] = EC2([instance_page({**runner(), "InstanceLifecycle": "spot",
                                   "SpotInstanceRequestId": SID})], [ServiceError("PRIVATE")])
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            self.collect(SCHEDULE)
        error, obs = self.records()
        self.assertEqual(error["operation"], "DescribeSpotInstanceRequests")
        self.assertEqual(obs["state_reason_code"], "Server.SpotInstanceTermination")
        self.assertIsNone(obs["spot_status_code"])

    def test_failed_later_page_does_not_discard_other_regions_or_send_heartbeat(self):
        self.clients[REGION] = EC2([instance_page(), ServiceError("PRIVATE")])
        self.clients["us-west-1"] = EC2([instance_page(runner())])
        os.environ["RUNNER_REGIONS"] = json.dumps(list(self.clients))
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            self.collect(SCHEDULE)
        error, obs = self.records()
        self.assertEqual(error, {"event": "runner_loss_scan_error", "region": REGION,
                                 "operation": "DescribeInstances"})
        self.assertEqual(obs["region"], "us-west-1")

    def test_empty_fleet_signals_success_without_secondary_lookup(self):
        self.assertEqual(self.collect(SCHEDULE), [{"event": "runner_loss_scan_complete"}])
        self.assertEqual(len(self.clients[REGION].calls), 1)

    def test_controller_records_are_projected_and_only_accepted_events_reported(self):
        for group in ("cleanup", "reuse", "webhook"):
            self.output = io.StringIO()
            [entry] = self.collect(subscription(["not json", [], accepted(reason="PRIVATE"),
                accepted(instance_id="PRIVATE"), accepted(region="us-east-1"), accepted(reason=[]),
                accepted(event="runner_termination_requested"), accepted(extra="PRIVATE")],
                group=f"/aws/lambda/github-runner-{group}"))
            self.assertEqual(entry, {"event": "runner_termination_accepted", "controller": group,
                             "instance_id": IID, "reason": "hard-ceiling", "region": REGION,
                             "accepted_at": "1970-01-01T00:00:01+00:00"})
        self.api.assert_not_called()

    def test_unsolicited_records_and_control_messages_evidence_nothing(self):
        self.assertEqual(self.collect(subscription([accepted()], group="/aws/lambda/other")), [])
        self.assertEqual(self.collect(subscription([accepted()], kind="CONTROL_MESSAGE")), [])
        with self.assertRaisesRegex(ValueError, "Expected a scheduled"):
            self.collect({"source": "aws.events", "detail-type": "Other"})
        with self.assertRaisesRegex(ValueError, "Malformed CloudWatch"):
            self.collect({"awslogs": {"data": "*invalid"}})
        self.api.assert_not_called()

    def test_empty_configuration_cannot_report_a_successful_scan(self):
        os.environ["RUNNER_REGIONS"] = "[]"
        with self.assertRaisesRegex(ValueError, "regions"):
            self.collect(SCHEDULE)
        self.assertEqual(self.records(), [])


if __name__ == "__main__":
    unittest.main(buffer=True)
