#!/usr/bin/env python3
"""Offline Mac lease lifecycle regression tests: no credentials or AWS calls."""

import copy
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


spec = importlib.util.spec_from_file_location("macpbox_cleanup", Path(__file__).with_name("macpbox-cleanup.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
LEASE_ID = "f7267481-dfc5-49d9-afbd-8b01b1894db4"


def labels(**extra):
    values = {"Name": "macpbox", "Purpose": "macpbox", "LeaseHours": "24", "LeaseId": LEASE_ID}
    values.update(extra)
    return [{"Key": key, "Value": value} for key, value in values.items()]


def host(age=24, state="available"):
    return {"HostId": "h-owned", "AllocationTime": NOW - timedelta(hours=age), "State": state,
            "HostProperties": {"InstanceType": "mac-m4pro.metal"}, "Tags": labels()}


def instance(state="running"):
    return {"InstanceId": "i-owned", "InstanceType": "mac-m4pro.metal", "State": {"Name": state},
            "Placement": {"HostId": "h-owned"}, "Tags": labels()}


class EC2:
    def __init__(self, hosts, instances=(), release=True):
        self.hosts, self.instances, self.release = hosts, list(instances), release
        self.calls = []

    def describe_hosts(self, **kwargs):
        self.calls.append(("describe_hosts", kwargs))
        return {"Hosts": copy.deepcopy(self.hosts)}

    def describe_instances(self, **kwargs):
        assert kwargs["Filters"] == [{"Name": "host-id", "Values": ["h-owned"]}]
        self.calls.append(("describe_instances", kwargs))
        return {"Reservations": [{"Instances": copy.deepcopy(self.instances)}]}

    def terminate_instances(self, **kwargs):
        self.calls.append(("terminate_instances", kwargs))
        return {}

    def release_hosts(self, **kwargs):
        self.calls.append(("release_hosts", kwargs))
        return {"Successful": kwargs["HostIds"] if self.release else [], "Unsuccessful": []}

    def writes(self):
        return [call for call in self.calls if call[0] in ("terminate_instances", "release_hosts")]


class CleanupTests(unittest.TestCase):
    def test_allocation_not_instance_age_enforces_full_24_hours(self):
        ec2 = EC2([host(age=23.999)], [instance()])
        self.assertEqual(module.cleanup(ec2, NOW)["within_lease"], 1)
        self.assertEqual(ec2.writes(), [])

    def test_exact_deadline_requests_termination_but_never_claims_released(self):
        ec2 = EC2([host()], [instance()])
        result = module.cleanup(ec2, NOW)
        self.assertEqual(result["terminating"], 1)
        self.assertEqual(result["released"], 0)
        self.assertEqual(ec2.writes(), [("terminate_instances", {"InstanceIds": ["i-owned"]})])

    def test_repeated_pending_runs_survive_four_and_half_hour_scrub(self):
        ec2 = EC2([host(age=24, state="pending")], [instance("terminated")])
        for minutes in range(0, 271, 10):
            self.assertEqual(module.cleanup(ec2, NOW + timedelta(minutes=minutes))["waiting"], 1)
            self.assertEqual(ec2.writes(), [])
        ec2.hosts[0]["State"] = "available"
        result = module.cleanup(ec2, NOW + timedelta(hours=4.5))
        self.assertEqual(result["released"], 1)
        self.assertEqual(ec2.writes(), [("release_hosts", {"HostIds": ["h-owned"]})])

    def test_unsuccessful_release_is_an_error_and_next_invocation_retries(self):
        ec2 = EC2([host(age=29)], release=False)
        result = module.cleanup(ec2, NOW)
        self.assertEqual((result["errors"], result["released"]), (1, 0))
        ec2.release = True
        self.assertEqual(module.cleanup(ec2, NOW)["released"], 1)

    def test_foreign_host_or_invalid_lease_metadata_is_never_mutated(self):
        cases = [labels(Name="other"), labels(Purpose="other"), labels(LeaseHours="12"),
                 labels(LeaseId="not-a-uuid"), labels(LeaseId="")]
        for value in cases:
            with self.subTest(tags=value):
                item = host(age=30)
                item["Tags"] = value
                ec2 = EC2([item], [instance()])
                self.assertEqual(module.cleanup(ec2, NOW)["errors"], 1)
                self.assertEqual(ec2.writes(), [])
        item = host(age=30)
        item["HostProperties"]["InstanceType"] = "mac2-m2pro.metal"
        ec2 = EC2([item], [instance()])
        self.assertEqual(module.cleanup(ec2, NOW)["errors"], 1)
        self.assertEqual(ec2.writes(), [])

    def test_foreign_occupant_blocks_both_termination_and_host_release(self):
        for key in ("Name", "Purpose", "LeaseId"):
            foreign = instance()
            foreign["Tags"] = labels(**{key: "other"})
            ec2 = EC2([host(age=30)], [foreign, instance()])
            self.assertEqual(module.cleanup(ec2, NOW)["errors"], 1)
            self.assertEqual(ec2.writes(), [])

    def test_missing_or_future_allocation_time_fails_closed(self):
        for allocated in (None, NOW.replace(tzinfo=None), NOW + timedelta(seconds=1)):
            item = host(age=30)
            item["AllocationTime"] = allocated
            ec2 = EC2([item])
            self.assertEqual(module.cleanup(ec2, NOW)["errors"], 1)
            self.assertEqual(ec2.writes(), [])

    def test_shutting_down_is_waited_for_without_replaying_termination(self):
        ec2 = EC2([host(age=25)], [instance("shutting-down")])
        self.assertEqual(module.cleanup(ec2, NOW)["waiting"], 1)
        self.assertEqual(ec2.writes(), [])

    def test_stuck_scrub_is_visible_not_a_successful_release(self):
        ec2 = EC2([host(age=31, state="pending")])
        result = module.cleanup(ec2, NOW)
        self.assertEqual((result["waiting"], result["errors"], result["released"]), (1, 1, 0))
        self.assertEqual(ec2.writes(), [])

    def test_released_hosts_are_ignored(self):
        ec2 = EC2([host(age=30, state="released")])
        self.assertEqual(module.cleanup(ec2, NOW)["errors"], 0)
        self.assertEqual(ec2.writes(), [])


if __name__ == "__main__":
    unittest.main(buffer=True)
