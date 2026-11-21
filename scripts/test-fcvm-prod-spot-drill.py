#!/usr/bin/env python3
"""Offline FIS execution and recovery checks. Each call reads a fresh copy of durable state."""

import copy
import datetime as dt
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location("drill", Path(__file__).with_name("fcvm-prod-spot-drill.py"))
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)
FIRST = "20000000-0000-4000-a000-000000000001"
SECOND = "20000000-0000-4000-a000-000000000002"
A, B = "i-00000001", "i-00000002"


class FisClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class DrillTests(unittest.TestCase):
    def setUp(self):
        self.zones = {A: "us-east-1a", B: "us-east-1b"}
        self.spot = {A}
        self.target = A
        self.unhealthy = set()
        self.experiments = {}
        self.state = {}
        self.now = 123456
        self.fis = Mock(
            spec=[
                "list_experiments",
                "get_experiment",
                "start_experiment",
                "stop_experiment",
                "list_experiment_resolved_targets",
                "exceptions",
            ]
        )
        self.fis.list_experiments.side_effect = lambda: {"experiments": list(self.experiments.values())}
        self.fis.get_experiment.side_effect = self.get_fis
        self.fis.start_experiment.side_effect = self.start_fis
        self.fis.stop_experiment.side_effect = self.stop_fis
        self.fis.list_experiment_resolved_targets.side_effect = self.targets
        self.fis.exceptions.ClientError = FisClientError
        self.ec2 = Mock(spec=["describe_instances", "describe_instance_status"])
        self.ec2.describe_instances.side_effect = self.describe
        self.ec2.describe_instance_status.side_effect = self.health

    def get_fis(self, id):
        if id not in self.experiments:
            # The broker's GetExperiment permission requires the Drill resource tag.
            raise FisClientError("AccessDeniedException")
        return {"experiment": self.experiments[id]}

    def targets(self, *, experimentId, targetName):
        self.assertIn(experimentId, self.experiments)
        self.assertEqual(targetName, "one-prod-host")
        return {
            "resolvedTargets": [
                {
                    "targetInformation": {
                        "resourceArn": "arn:aws:ec2:us-east-1:fixture:instance/" + self.target
                    }
                }
            ]
        }

    def describe(self, Filters):
        self.assertEqual(
            {f["Name"]: f["Values"] for f in Filters},
            {"tag:Fleet": ["prod"], "tag:Purpose": ["fcvm-fleet-host"], "instance-state-name": ["running"]},
        )
        return {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": i,
                            "Placement": {"AvailabilityZone": az},
                            "InstanceLifecycle": "spot" if i in self.spot else "on-demand",
                        }
                        for i, az in self.zones.items()
                    ]
                }
            ]
        }

    def health(self, InstanceIds):
        return {
            "InstanceStatuses": [
                {
                    "InstanceId": i,
                    "SystemStatus": {"Status": "ok"},
                    "InstanceStatus": {"Status": "impaired" if i in self.unhealthy else "ok"},
                }
                for i in InstanceIds
            ]
        }

    def start_fis(self, experimentTemplateId, clientToken, tags):
        self.assertEqual(experimentTemplateId, "template")
        self.assertEqual(tags, {"Drill": "fcvm-fleet-prod-spot", "Request": clientToken})
        eid = "EXP" + clientToken[-1]
        self.experiments.setdefault(
            eid,
            {
                "id": eid,
                "creationTime": dt.datetime.fromtimestamp(self.now, dt.timezone.utc),
                "experimentTemplateId": experimentTemplateId,
                "tags": tags,
                "state": {"status": "pending"},
            },
        )
        return {"experiment": self.experiments[eid]}

    def stop_fis(self, id):
        self.experiments[id]["state"] = {"status": "stopping"}
        return {"experiment": self.experiments[id]}

    def save(self, obj):
        self.state = copy.deepcopy(obj)

    def invoke(self, action, **args):
        return drill.process(
            {"action": action, **args},
            self.fis,
            self.ec2,
            lambda: copy.deepcopy(self.state),
            self.save,
            "template",
            self.now,
        )

    def test_two_az_start_and_stable_replay(self):
        first = self.invoke("start", request_id=FIRST)
        self.assertEqual(first["experiment_id"], "EXP1")
        self.assertEqual(self.invoke("start", request_id=FIRST), first)
        self.fis.start_experiment.assert_called_once()
        self.experiments["EXP1"]["state"] = {"status": "running"}
        self.experiments["EXP1"]["startTime"] = dt.datetime.fromtimestamp(self.now, dt.timezone.utc)
        status = self.invoke("status")
        self.assertEqual(status["target_azs"], ["us-east-1a"])
        self.assertEqual(
            status["started_at"], dt.datetime.fromtimestamp(self.now, dt.timezone.utc).isoformat()
        )
        self.assertNotIn(A, str(status))
        self.assertEqual(self.invoke("stop", experiment_id="EXP1")["state"], "stopping")
        self.invoke("stop", experiment_id="EXP1")
        self.fis.stop_experiment.assert_called_once()

    def test_eligibility_requires_every_eligible_spot_be_healthy_and_two_zones(self):
        self.zones[B] = "us-east-1a"
        with self.assertRaises(ValueError):
            self.invoke("start", request_id=FIRST)
        self.zones[B] = "us-east-1b"
        self.unhealthy = {A}
        with self.assertRaises(ValueError):
            self.invoke("start", request_id=FIRST)
        self.unhealthy = set()
        self.spot = set()
        with self.assertRaises(ValueError):
            self.invoke("start", request_id=FIRST)
        self.fis.start_experiment.assert_not_called()

    def test_active_and_recently_completed_runs_block_other_tokens(self):
        self.invoke("start", request_id=FIRST)
        self.fis.list_experiments.side_effect = lambda: {"experiments": []}
        with self.assertRaises(ValueError):
            self.invoke("start", request_id=SECOND)
        old = self.experiments["EXP1"]
        old["state"] = {"status": "completed"}
        old["endTime"] = dt.datetime.fromtimestamp(self.now, dt.timezone.utc)
        with self.assertRaises(ValueError):
            self.invoke("start", request_id=SECOND)
        self.now += drill.RECOVERY_SECONDS + 1
        self.assertEqual(self.invoke("start", request_id=SECOND)["experiment_id"], "EXP2")
        self.assertEqual(self.invoke("start", request_id=FIRST)["experiment_id"], "EXP1")
        self.assertEqual(self.fis.start_experiment.call_count, 2)

    def test_lost_start_reply_recovers_by_request_tag_and_never_blindly_starts_another(self):
        def fail(**params):
            self.start_fis(**params)
            raise TimeoutError

        self.fis.start_experiment.side_effect = fail
        with self.assertRaises(TimeoutError):
            self.invoke("start", request_id=FIRST)
        with self.assertRaisesRegex(ValueError, "already active"):
            self.invoke("start", request_id=SECOND)
        self.assertEqual(self.invoke("status")["experiment_id"], "EXP1")
        self.assertEqual(self.invoke("start", request_id=FIRST)["experiment_id"], "EXP1")
        self.fis.start_experiment.assert_called_once()

    def test_crash_before_fis_rechecks_health_before_replay(self):
        self.fis.start_experiment.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.invoke("start", request_id=FIRST)
        self.assertEqual(self.invoke("status")["request_id"], FIRST)
        self.unhealthy = {A}
        with self.assertRaises(ValueError):
            self.invoke("start", request_id=FIRST)
        self.fis.start_experiment.side_effect = self.start_fis
        self.unhealthy = set()
        self.assertEqual(self.invoke("start", request_id=FIRST)["experiment_id"], "EXP1")

    def test_stop_and_status_reject_foreign_template_or_foreign_tag(self):
        self.invoke("start", request_id=FIRST)
        e = self.experiments["EXP1"]
        for template, owner in (("other", "fcvm-fleet-prod-spot"), ("template", "other")):
            e["experimentTemplateId"] = template
            e["tags"]["Drill"] = owner
            for action in ("stop", "status"):
                with self.assertRaisesRegex(ValueError, "does not belong"):
                    self.invoke(action, experiment_id="EXP1")
        self.fis.stop_experiment.assert_not_called()

    def test_recorded_experiment_survives_fis_history_expiry_without_being_restarted(self):
        self.invoke("start", request_id=FIRST)
        self.experiments["EXP1"]["state"] = {"status": "running"}
        self.experiments["EXP1"]["startTime"] = dt.datetime.fromtimestamp(self.now, dt.timezone.utc)
        self.assertEqual(self.invoke("status")["target_azs"], ["us-east-1a"])
        del self.experiments["EXP1"]
        with self.assertRaisesRegex(FisClientError, "AccessDeniedException"):
            self.invoke("start", request_id=SECOND)
        self.now += drill.HISTORY_SECONDS + 1
        for action, opts in (
            ("status", {}),
            ("status", {"experiment_id": "EXP1"}),
            ("start", {"request_id": FIRST}),
        ):
            result = self.invoke(action, **opts)
            self.assertEqual(result["state"], "expired")
            self.assertEqual(result["target_azs"], ["us-east-1a"])
            self.assertIsNotNone(result["started_at"])
        self.assertEqual(self.invoke("start", request_id=SECOND)["experiment_id"], "EXP2")

    def test_expiry_requires_a_complete_listing_without_the_recorded_id(self):
        self.invoke("start", request_id=FIRST)
        self.now += drill.HISTORY_SECONDS + 1
        exp = self.experiments["EXP1"]
        for code in ("AccessDeniedException", "ResourceNotFoundException"):
            with self.subTest(code=code):
                self.fis.get_experiment.side_effect = FisClientError(code)
                self.fis.list_experiments.side_effect = [
                    {"experiments": [], "nextToken": "more"},
                    {"experiments": [exp]},
                ]
                with self.assertRaisesRegex(FisClientError, code):
                    self.invoke("status")
                self.fis.list_experiments.assert_called_with(nextToken="more")
                self.fis.list_experiments.side_effect = [{"experiments": []}]
                self.assertEqual(self.invoke("status")["state"], "expired")
        self.fis.list_experiments.side_effect = [
            {"experiments": [], "nextToken": "more"},
            FisClientError("ThrottlingException"),
        ]
        with self.assertRaisesRegex(FisClientError, "ThrottlingException"):
            self.invoke("status")
        self.fis.get_experiment.side_effect = FisClientError("InternalServerException")
        self.fis.list_experiments.reset_mock()
        with self.assertRaisesRegex(FisClientError, "InternalServerException"):
            self.invoke("status")
        self.fis.list_experiments.assert_not_called()
        self.fis.start_experiment.assert_called_once()

    def test_pending_unknown_retired_without_replaying(self):
        self.fis.start_experiment.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.invoke("start", request_id=FIRST)
        with self.assertRaisesRegex(ValueError, "reservation"):
            self.invoke("start", request_id=SECOND)
        self.fis.start_experiment.side_effect = self.start_fis
        self.now += drill.HISTORY_SECONDS + 1
        self.assertEqual(self.invoke("status")["state"], "expired")
        self.assertEqual(self.invoke("start", request_id=FIRST)["state"], "expired")
        self.assertEqual(self.fis.start_experiment.call_count, 1)
        self.assertEqual(self.invoke("start", request_id=SECOND)["experiment_id"], "EXP2")

    def test_retry_adds_new_host_az_to_original_reservation(self):
        self.fis.start_experiment.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.invoke("start", request_id=FIRST)
        self.fis.start_experiment.side_effect = self.start_fis
        self.target = "i-00000003"
        self.zones[self.target] = "us-east-1c"
        self.spot = {self.target}
        self.invoke("start", request_id=FIRST)
        self.experiments["EXP1"]["state"] = {"status": "running"}
        self.assertEqual(self.invoke("status")["target_azs"], ["us-east-1c"])

    def test_reject_arbitrary_aws_parameters(self):
        for args in (
            {"targets": ["i-other"]},
            {"template": "other"},
            {"tags": {}},
            {"experiment_id": "EXP1"},
        ):
            with self.assertRaises(ValueError):
                self.invoke("start", request_id=FIRST, **args)
        with self.assertRaises(ValueError):
            self.invoke("stop")
        self.fis.start_experiment.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)
