#!/usr/bin/env python3
"""Offline release tests: image-only changes, backend-lock ownership and rollback."""
import copy
import datetime
import importlib.util
import json
import os
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class ClientError(Exception):
    def __init__(self, response, operation="test"):
        self.response = response


class BotoCoreError(Exception):
    pass


stubs = {"boto3": types.ModuleType("boto3"), "botocore": types.ModuleType("botocore"),
         "botocore.config": types.SimpleNamespace(Config=object),
         "botocore.exceptions": types.SimpleNamespace(ClientError=ClientError, BotoCoreError=BotoCoreError)}
ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("release", ROOT / "fcvm-coordinator-release/release.py")
release = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, stubs):
    spec.loader.exec_module(release)

OLD, NEW = "arn:example:task-definition/fcvm-fleet-coordinator:6", "arn:example:task-definition/fcvm-fleet-coordinator:7"
REPO = "registry.example/fcvm-fleet/coordinator"
DIGEST = "sha256:" + "a" * 64
EVENT = {"commit": "b" * 40, "digest": DIGEST}
ENV = {"REPOSITORY_URL": REPO, "LOCK_TABLE": "locks", "LOCK_ID": "state-bucket/state-key",
       "IMAGE_PARAMETER": "/fleet/stage/image", "STACK": "stage", "CLUSTER": "fcvm-fleet",
       "SERVICE": "fcvm-fleet-coordinator", "FAMILY": "fcvm-fleet-coordinator",
       "LANE_PARAMETER": "/fleet/stage/lambda-image"}


class AWS:
    def __init__(self):
        self.lock = None
        self.image = REPO + "@sha256:" + "c" * 64
        self.version = 1
        self.arn = OLD
        self.calls = []
        self.fail_rollout = False
        self.old = {"taskDefinition": {
            "family": "fcvm-fleet-coordinator", "taskDefinitionArn": OLD,
            "taskRoleArn": "arn:example:role/runtime", "executionRoleArn": "arn:example:role/execution",
            "runtimePlatform": {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"},
            "cpu": "512", "memory": "1024", "networkMode": "awsvpc", "requiresCompatibilities": ["FARGATE"],
            "containerDefinitions": [
                {"name": "coordinator", "image": self.image, "readonlyRootFilesystem": True,
                 "command": ["--dns-nameservers", "ns.example"], "environment": [{"name": "STATE", "value": "bucket"}]},
                {"name": "cloudflared", "image": "cloudflared:pinned", "readonlyRootFilesystem": True,
                 "secrets": [{"name": "TUNNEL_TOKEN", "valueFrom": "arn:example:secret/token"}]}]},
            "tags": [{"key": "Project", "value": "fcvm-fleet"}]}

    def describe_images(self, **kwargs):
        return {"imageDetails": [{"imageDigest": DIGEST}]}

    def refused(self, kwargs):
        """DynamoDB's refusal; with ALL_OLD it carries the item the condition was checked against."""
        response = {"Error": {"Code": "ConditionalCheckFailedException", "Message": "secret detail"}}
        if kwargs.get("ReturnValuesOnConditionCheckFailure") == "ALL_OLD" and self.lock:
            response["Item"] = {"LockID": {"S": ENV["LOCK_ID"]}, "Info": {"S": self.lock}}
        return ClientError(response)

    def put_item(self, **kwargs):
        if self.lock and kwargs.get("ConditionExpression") == "attribute_not_exists(LockID)":
            raise self.refused(kwargs)
        self.lock = kwargs["Item"]["Info"]["S"]
        self.calls.append("lock")

    def delete_item(self, **kwargs):
        if (kwargs.get("ConditionExpression") == "#info = :owner"
                and kwargs["ExpressionAttributeValues"][":owner"]["S"] != self.lock):
            raise self.refused(kwargs)
        self.lock = None
        self.calls.append("unlock")

    def describe_services(self, **kwargs):
        assert kwargs == {"cluster": os.environ["CLUSTER"], "services": [os.environ["SERVICE"]]}, kwargs
        return {"services": [{"taskDefinition": self.arn, "desiredCount": 1, "runningCount": 1,
                              "pendingCount": 0, "deployments": [{"rolloutState":
                                  "FAILED" if self.fail_rollout and self.arn == NEW else "COMPLETED"}]}]}

    def describe_task_definition(self, **kwargs):
        return copy.deepcopy(self.old)

    lane_pointer = "none"

    def get_parameter(self, **kwargs):
        if kwargs["Name"] == os.environ.get("LANE_PARAMETER"):
            return {"Parameter": {"Value": self.lane_pointer}}
        return {"Parameter": {"Value": self.image, "Version": self.version}}

    def put_parameter(self, **kwargs):
        assert self.lock, "pointer write outside the Terraform backend lock"
        if kwargs["Name"] == os.environ.get("LANE_PARAMETER"):
            self.lane_pointer = kwargs["Value"]
            self.calls.append("lane-pointer")
            return
        self.image = kwargs["Value"]
        self.version += 1
        self.calls.append("pointer")

    def register_task_definition(self, **kwargs):
        assert self.lock, "task registration outside the Terraform backend lock"
        self.registered = kwargs
        self.calls.append("register")
        return {"taskDefinition": {"taskDefinitionArn": NEW}}

    def update_service(self, **kwargs):
        assert self.lock, "service change outside the Terraform backend lock"
        assert (kwargs["cluster"], kwargs["service"]) == (os.environ["CLUSTER"], os.environ["SERVICE"]), kwargs
        self.arn = kwargs["taskDefinition"]
        self.calls.append(self.arn)

    def deregister_task_definition(self, **kwargs):
        assert self.lock
        self.calls.append("deregister:" + kwargs["taskDefinition"])


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.aws = AWS()
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_release(self, event=EVENT):
        return release.deploy(event, self.aws, self.aws, self.aws, self.aws)

    def test_only_the_image_changes(self):
        original = copy.deepcopy(self.aws.old)
        before = copy.deepcopy(original)
        actual = release.task_input(original, REPO + "@" + DIGEST)
        self.assertEqual(original, before)
        expected = {k: v for k, v in original["taskDefinition"].items() if k != "taskDefinitionArn"}
        expected["containerDefinitions"][0]["image"] = REPO + "@" + DIGEST
        expected["tags"] = original["tags"]
        self.assertEqual(actual, expected)
        self.assertEqual(self.aws.old["taskDefinition"]["containerDefinitions"][0]["image"], self.aws.image)

    def test_release_policy_allows_exactly_the_active_task_size_with_broadcast_on_or_off(self):
        infrastructure = (ROOT / "fcvm-fleet.tf").read_text()
        policy = (ROOT / "fcvm-fleet-ci.tf").read_text()
        registration = policy.split('Action = "ecs:RegisterTaskDefinition"', 1)[1]
        registration = registration.split('NumericEquals = {', 1)[1].split('}', 1)[0]
        limits = dict(re.findall(r'"ecs:task-(cpu|memory)"\s*=\s*(tonumber\([^)]*\)|[0-9]+)', registration))
        self.assertEqual(set(limits), {"cpu", "memory"})
        sizes = {field: re.search(r'\b' + field + r'\s*=\s*local\.fcvm_fleet_broadcast\s*\?\s*'
                                 r'"([0-9]+)"\s*:\s*"([0-9]+)"', infrastructure).groups()
                 for field in ("cpu", "memory")}
        for index, broadcast in enumerate((True, False)):
            with self.subTest(broadcast=broadcast):
                current = copy.deepcopy(self.aws.old)
                for field in sizes:
                    current["taskDefinition"][field] = sizes[field][index]
                request = release.task_input(current, REPO + "@" + DIGEST)
                for field, limit in limits.items():
                    if limit.startswith("tonumber("):
                        self.assertEqual(limit, "tonumber(aws_ecs_task_definition.fcvm_fleet_coordinator." + field + ")")
                        allowed = int(current["taskDefinition"][field])
                    else:
                        allowed = int(limit)
                    self.assertEqual(int(request[field]), allowed)

    def test_invalid_input_never_takes_the_lock_or_changes_a_service(self):
        for event in ({}, {**EVENT, "digest": "tag:latest"}, {**EVENT, "commit": "main"}):
            with self.assertRaises(ValueError):
                self.run_release(event)
        self.assertEqual(self.aws.calls, [])

    def test_a_digest_cannot_impersonate_another_commit(self):
        with self.assertRaises(ValueError):
            self.run_release({**EVENT, "digest": "sha256:" + "d" * 64})
        self.assertEqual(self.aws.calls, [])

    def test_an_existing_terraform_lock_is_not_touched(self):
        self.aws.lock = "another operator"
        self.assertEqual(self.run_release(), {"status": "busy"})
        self.assertEqual(self.aws.lock, "another operator")
        self.assertEqual(self.aws.calls, [])

    def test_success_is_serialized_and_records_the_running_digest(self):
        result = self.run_release()
        self.assertEqual(result["status"], "deployed")
        self.assertEqual(self.aws.arn, NEW)
        self.assertEqual(self.aws.image, REPO + "@" + DIGEST)
        self.assertEqual(self.aws.calls, ["lock", "pointer", "register", NEW, "pointer", "unlock"])
        self.assertIsNone(self.aws.lock)

    def test_an_already_deployed_digest_is_idempotent(self):
        self.aws.image = REPO + "@" + DIGEST
        self.aws.old["taskDefinition"]["containerDefinitions"][0]["image"] = self.aws.image
        self.assertEqual(self.run_release()["status"], "unchanged")
        self.assertEqual(self.aws.calls, ["lock", "unlock"])

    def test_inconsistent_release_state_refuses_to_overwrite_and_unlocks(self):
        self.aws.image = "unrecorded"
        with self.assertRaisesRegex(RuntimeError, "disagree"):
            self.run_release()
        self.assertEqual(self.aws.calls, ["lock", "unlock"])

    def test_another_task_family_cannot_be_released(self):
        self.aws.old["taskDefinition"]["family"] = "another-service"
        with self.assertRaisesRegex(ValueError, "task family"):
            self.run_release()
        self.assertNotIn("register", self.aws.calls)
        self.assertEqual(self.aws.calls[-1], "unlock")

    def test_a_failed_registration_still_invalidates_earlier_saved_plans(self):
        old_image, old_version = self.aws.image, self.aws.version
        def failed(**kwargs):
            raise RuntimeError("registration failed")
        self.aws.register_task_definition = failed
        with self.assertRaisesRegex(RuntimeError, "registration failed"):
            self.run_release()
        self.assertEqual(self.aws.image, old_image)
        self.assertGreater(self.aws.version, old_version)

    def test_a_failed_rollout_restores_the_previous_task_and_pointer(self):
        self.aws.fail_rollout = True
        old = self.aws.image
        with self.assertRaisesRegex(RuntimeError, "rollout failed"):
            self.run_release()
        self.assertEqual(self.aws.arn, OLD)
        self.assertEqual(self.aws.image, old)
        self.assertEqual(self.aws.calls, ["lock", "pointer", "register", NEW, OLD, "pointer", "deregister:" + NEW, "unlock"])

    def test_a_lock_taken_over_during_the_release_is_left_alone_and_the_release_reports_deployed(self):
        real = self.aws.update_service
        def changed(**kwargs):
            real(**kwargs)
            self.aws.lock = "new owner"  # force-released and taken by Terraform while the rollout ran
        self.aws.update_service = changed
        self.assertEqual(self.run_release()["status"], "deployed")
        self.assertEqual(self.aws.lock, "new owner")
        self.assertEqual(self.aws.image, REPO + "@" + DIGEST)

    def test_a_retried_unlock_whose_first_attempt_landed_reports_deployed(self):
        real = self.aws.delete_item
        def retried(**kwargs):
            real(**kwargs)  # the first attempt deletes the lock, its reply is lost, the SDK retries
            real(**kwargs)
        self.aws.delete_item = retried
        result = self.run_release()
        self.assertEqual(result["status"], "deployed")
        self.assertIsNone(self.aws.lock)

    def test_a_retried_lock_write_whose_first_attempt_landed_owns_the_lock(self):
        real = self.aws.put_item
        def retried(**kwargs):
            real(**kwargs)  # the first attempt writes the lock, its reply is lost, the SDK retries
            real(**kwargs)
        self.aws.put_item = retried
        self.assertEqual(self.run_release()["status"], "deployed")
        self.assertIsNone(self.aws.lock, "the release left its own lock behind")

    def test_a_failed_unlock_of_a_lock_still_held_is_reported(self):
        def throttled(**kwargs):
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}})
        self.aws.delete_item = throttled
        progress = {}
        with self.assertRaises(ClientError):
            release.deploy(EVENT, self.aws, self.aws, self.aws, self.aws, progress=progress)
        self.assertEqual(progress["step"], "unlock")


PROD_ENV = {**ENV, "STACK": "prod", "CLUSTER": "fcvm-fleet-prod", "SERVICE": "fcvm-fleet-prod-coordinator",
            "FAMILY": "fcvm-fleet-prod-coordinator", "IMAGE_PARAMETER": "/fleet/prod/image",
            "STAGE_IMAGE_PARAMETER": "/fleet/stage/image", "RELEASE_RECORD": "/fleet/prod/release",
            "METRICS_NAMESPACE": "FCVM/Fleet", "METRIC_FLEET": "prod", "STAGE_CLUSTER": "fcvm-fleet",
            "STAGE_SERVICE": "fcvm-fleet-coordinator",
            "LANE_REPOSITORY_URL": "registry.example/fcvm-fleet/lambda-tile-prod",
            "STAGE_LANE_REPOSITORY_URL": "registry.example/fcvm-fleet/lambda-tile"}
PROD_OLD = PROD_ENV["FAMILY"]


LANE_URL = "registry.example/fcvm-fleet/lambda-tile"
LANE = "sha256:" + "e" * 64
LANE_ENV = {**ENV, "LANE_REPOSITORY_URL": LANE_URL, "LANE_PARAMETER": "/fleet/stage/lambda-image"}


class LaneECR:
    """The lane repository (fcvm-fleet/lambda-tile, us-east-1): which digest keep-<stack>-current holds."""

    def __init__(self, current=LANE, error=None):
        self.current, self.error, self.calls = current, error, []

    def describe_images(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise ClientError({"Error": {"Code": self.error, "Message": "secret detail"}})
        return {"imageDetails": [{"imageDigest": self.current}]}


class LaneReleaseTests(unittest.TestCase):
    """A release may carry lambda_digest, the lane image staging selected (ejc3/fcvm fleet/coord/publish.py). The
    function sets the coordinator's FLEET_LAMBDA_IMAGE to it only when keep-<stack>-current holds it, and refuses
    and changes nothing otherwise. Without lambda_digest the release is exactly the one without a lane."""

    def setUp(self):
        self.aws, self.lane = AWS(), LaneECR()
        # The coordinator repository: the release's commit tag names DIGEST; a digest lookup names commit b.
        self.aws.describe_images = lambda **kwargs: {"imageDetails": [{
            "imageDigest": kwargs["imageIds"][0].get("imageDigest", DIGEST), "imageTags": ["b" * 40]}]}
        p = patch.dict(os.environ, LANE_ENV)
        p.start()
        self.addCleanup(p.stop)

    def run_release(self, event):
        return release.deploy(event, self.aws, self.aws, self.aws, self.aws, lane_ecr=self.lane)

    def coordinator_env(self, definition):
        container = next(c for c in definition["containerDefinitions"] if c["name"] == "coordinator")
        return {e["name"]: e["value"] for e in container.get("environment", [])}

    def test_a_release_without_a_lane_digest_is_exactly_todays(self):
        result = self.run_release(EVENT)
        self.assertEqual(result, {"status": "deployed", "taskDefinition": NEW, "digest": DIGEST})
        self.assertEqual(self.lane.calls, [])
        self.assertEqual(self.aws.registered, release.task_input(AWS().old, REPO + "@" + DIGEST))
        self.assertEqual(self.aws.calls, ["lock", "pointer", "register", NEW, "pointer", "unlock"])

    def test_a_selected_lane_image_is_set_with_the_coordinator_image(self):
        result = self.run_release({**EVENT, "lambda_digest": LANE})
        self.assertEqual(result, {"status": "deployed", "taskDefinition": NEW, "digest": DIGEST, "lambda_digest": LANE})
        self.assertEqual(self.lane.calls, [{"repositoryName": "fcvm-fleet/lambda-tile",
                                            "imageIds": [{"imageTag": "keep-stage-current"}]}])
        env = self.coordinator_env(self.aws.registered)
        self.assertEqual(env, {"STATE": "bucket", "FLEET_LAMBDA_IMAGE": LANE_URL + "@" + LANE})
        # Everything else is today's registration.
        today = release.task_input(AWS().old, REPO + "@" + DIGEST)
        for c in self.aws.registered["containerDefinitions"]:
            c.pop("environment", None)
        for c in today["containerDefinitions"]:
            c.pop("environment", None)
        self.assertEqual(self.aws.registered, today)

    def test_a_lane_digest_keep_current_does_not_hold_changes_nothing(self):
        self.lane.current = "sha256:" + "f" * 64
        with self.assertRaises(release.Refused) as refused:
            self.run_release({**EVENT, "lambda_digest": LANE})
        self.assertEqual(refused.exception.code, "lane-not-selected")
        self.assertEqual(self.aws.calls, ["lock", "unlock"])
        self.assertEqual(self.aws.arn, OLD)

    def test_an_unreadable_lane_repository_changes_nothing(self):
        for error in ("AccessDeniedException", "RepositoryNotFoundException", "ImageNotFoundException"):
            with self.subTest(error=error):
                self.setUp()
                self.lane.error = error
                with self.assertRaises(Exception):
                    self.run_release({**EVENT, "lambda_digest": LANE})
                self.assertEqual(self.aws.calls, ["lock", "unlock"])
                self.assertEqual(self.aws.arn, OLD)
        self.lane = LaneECR(current=None)
        self.lane.describe_images = lambda **kwargs: {"imageDetails": []}
        with self.assertRaises(release.Refused):
            self.run_release({**EVENT, "lambda_digest": LANE})

    def test_a_malformed_lane_digest_is_refused_before_anything(self):
        # null is valid: it removes the lane image (LaneClearTests).
        for value in ("latest", "sha256:" + "E" * 64, "", 7):
            with self.assertRaises(ValueError):
                self.run_release({**EVENT, "lambda_digest": value})
        self.assertEqual(self.aws.calls, [])
        self.assertEqual(self.lane.calls, [])

    def test_a_new_lane_image_with_the_same_coordinator_is_a_release(self):
        self.aws.image = REPO + "@" + DIGEST
        self.aws.old["taskDefinition"]["containerDefinitions"][0]["image"] = self.aws.image
        self.assertEqual(self.run_release({**EVENT, "lambda_digest": LANE})["status"], "deployed")
        self.aws.old["taskDefinition"]["containerDefinitions"][0]["environment"].append(
            {"name": "FLEET_LAMBDA_IMAGE", "value": LANE_URL + "@" + LANE})
        self.aws.calls = []
        self.assertEqual(self.run_release({**EVENT, "lambda_digest": LANE})["status"], "unchanged")

    def test_the_lane_action_reads_what_the_coordinator_runs(self):
        self.assertEqual(self.run_release({"action": "lane"}),
                         {"status": "ok", "lambda_digest": None, "commits": ["b" * 40]})
        self.aws.old["taskDefinition"]["containerDefinitions"][0]["environment"].append(
            {"name": "FLEET_LAMBDA_IMAGE", "value": LANE_URL + "@" + LANE})
        self.assertEqual(self.run_release({"action": "lane"}),
                         {"status": "ok", "lambda_digest": LANE, "commits": ["b" * 40]})
        self.assertEqual(self.aws.calls, [])
        self.aws.old["taskDefinition"]["containerDefinitions"][0]["environment"][-1]["value"] = "elsewhere@" + LANE
        with self.assertRaises(release.Refused):
            self.run_release({"action": "lane"})
        with self.assertRaises(ValueError):
            self.run_release({"action": "lane", "commit": "b" * 40})

    def test_the_lane_action_names_the_commits_of_the_running_coordinator_image(self):
        # ejc3/fcvm orders releases with it: a release of an ancestor of what staging runs is skipped.
        asked = []
        self.aws.describe_images = lambda **kwargs: asked.append(kwargs) or {"imageDetails": [{
            "imageDigest": "sha256:" + "c" * 64, "imageTags": ["b" * 40, "d" * 40, "latest"]}]}
        self.assertEqual(self.run_release({"action": "lane"})["commits"], ["b" * 40, "d" * 40])
        self.assertEqual(asked, [{"repositoryName": "fcvm-fleet/coordinator", "imageIds": [{"imageDigest": "sha256:" + "c" * 64}]}])
        # A running image that is not a digest in the coordinator repository is refused, not guessed at.
        self.aws.image = "elsewhere@sha256:" + "c" * 64
        self.aws.old["taskDefinition"]["containerDefinitions"][0]["image"] = self.aws.image
        with self.assertRaises(release.Refused):
            self.run_release({"action": "lane"})

    def test_each_stack_reads_its_own_lane_repository(self):
        prod = LaneECR(current=LANE)
        with patch.dict(os.environ, {**PROD_ENV, "LANE_REPOSITORY_URL": "registry.example/fcvm-fleet/lambda-tile-prod"}):
            release.lane_selected(prod, "prod", LANE)
        self.assertEqual(prod.calls, [{"repositoryName": "fcvm-fleet/lambda-tile-prod",
                                       "imageIds": [{"imageTag": "keep-prod-current"}]}])

    def test_a_lane_repository_of_the_other_stack_is_refused(self):
        # Prod's function configured with staging's repository (or the reverse) reads and names nothing.
        for env, url in ((PROD_ENV, LANE_URL), (ENV, "registry.example/fcvm-fleet/lambda-tile-prod")):
            with self.subTest(stack=env["STACK"]), patch.dict(os.environ, {**env, "LANE_REPOSITORY_URL": url}):
                lane = LaneECR()
                with self.assertRaises(ValueError):
                    release.lane_selected(lane, env["STACK"], LANE)
                self.assertEqual(lane.calls, [])

    def test_the_lane_action_never_waits_for_the_lock(self):
        # Read only: it answers while a release or a Terraform run holds the backend lock (never "busy").
        self.aws.lock = "another operator"
        self.assertEqual(self.run_release({"action": "lane"})["status"], "ok")
        self.assertEqual(self.aws.lock, "another operator")
        self.assertEqual(self.aws.calls, [])

    def test_a_release_with_a_lane_records_it_where_terraform_renders_it(self):
        # Terraform renders the task definition: without the pointer, the next apply drops the lane (#997's P1).
        self.assertEqual(self.run_release({**EVENT, "lambda_digest": LANE})["status"], "deployed")
        self.assertEqual(self.aws.lane_pointer, LANE_URL + "@" + LANE)
        self.assertEqual(self.aws.calls, ["lock", "pointer", "register", NEW, "lane-pointer", "pointer", "unlock"])

    def test_a_lane_pointer_that_disagrees_with_the_running_task_changes_nothing(self):
        for running, pointer in ((None, LANE_URL + "@" + LANE), (LANE, "none"), (LANE, LANE_URL + "@sha256:" + "f" * 64)):
            with self.subTest(running=running, pointer=pointer):
                self.aws = AWS()
                self.aws.describe_images = lambda **kwargs: {"imageDetails": [{
                    "imageDigest": kwargs["imageIds"][0].get("imageDigest", DIGEST), "imageTags": ["b" * 40]}]}
                if running:
                    self.aws.old["taskDefinition"]["containerDefinitions"][0]["environment"].append(
                        {"name": "FLEET_LAMBDA_IMAGE", "value": LANE_URL + "@" + running})
                self.aws.lane_pointer = pointer
                with self.assertRaises(release.Refused) as refused:
                    self.run_release({**EVENT, "lambda_digest": LANE})
                self.assertEqual(refused.exception.code, "lane-pointer-mismatch")
                self.assertEqual((self.aws.calls, self.aws.arn, self.aws.lane_pointer), (["lock", "unlock"], OLD, pointer))

    def test_a_failed_rollout_restores_the_lane_pointer_with_the_image_pointer(self):
        before = self.aws.image
        self.aws.fail_rollout = True
        with self.assertRaises(Exception):
            self.run_release({**EVENT, "lambda_digest": LANE})
        self.assertEqual((self.aws.arn, self.aws.image, self.aws.lane_pointer), (OLD, before, "none"))
        self.assertNotIn("lane-pointer", self.aws.calls)

    def test_a_failure_after_the_lane_pointer_is_written_restores_it(self):
        before, writes = self.aws.image, []
        put = self.aws.put_parameter
        def failing_image_pointer(**kwargs):
            writes.append(kwargs["Name"])
            if kwargs["Name"] == ENV["IMAGE_PARAMETER"] and kwargs["Value"] == REPO + "@" + DIGEST:
                raise RuntimeError("throttled")
            return put(**kwargs)
        self.aws.put_parameter = failing_image_pointer
        with self.assertRaises(RuntimeError):
            self.run_release({**EVENT, "lambda_digest": LANE})
        self.assertEqual((self.aws.arn, self.aws.image, self.aws.lane_pointer), (OLD, before, "none"))
        self.assertEqual(writes.count(LANE_ENV["LANE_PARAMETER"]), 2)
        # The lane pointer is restored before the image pointer: if that restore fails too, nothing is left ahead.
        rollback = writes[writes.index(ENV["IMAGE_PARAMETER"], writes.index(LANE_ENV["LANE_PARAMETER"])) + 1:]
        self.assertEqual(rollback[:2], [LANE_ENV["LANE_PARAMETER"], ENV["IMAGE_PARAMETER"]])

    def test_a_release_without_a_lane_still_refuses_a_lane_pointer_ahead_of_the_task(self):
        # A pointer left ahead (a Lambda that died between its two pointer writes) would make the next Terraform
        # apply change the lane unasked: every release refuses it, not only one that carries a lane.
        self.aws.lane_pointer = LANE_URL + "@" + LANE
        with self.assertRaises(release.Refused) as refused:
            self.run_release(EVENT)
        self.assertEqual(refused.exception.code, "lane-pointer-mismatch")
        self.assertEqual((self.aws.calls, self.aws.arn), (["lock", "unlock"], OLD))

    def test_the_same_lane_and_image_are_unchanged_and_write_nothing(self):
        self.aws.image = REPO + "@" + DIGEST
        self.aws.old["taskDefinition"]["containerDefinitions"][0]["image"] = self.aws.image
        self.aws.old["taskDefinition"]["containerDefinitions"][0]["environment"].append(
            {"name": "FLEET_LAMBDA_IMAGE", "value": LANE_URL + "@" + LANE})
        self.aws.lane_pointer = LANE_URL + "@" + LANE
        self.assertEqual(self.run_release({**EVENT, "lambda_digest": LANE})["status"], "unchanged")
        self.assertEqual(self.aws.calls, ["lock", "unlock"])

    def test_terraform_renders_the_lane_from_the_pointer_in_both_stacks(self):
        def block(text, header):
            start = text.index(header)
            return text[start:text.index("\n}\n", start) + 2]
        cases = (("stage", "fcvm-fleet-ci.tf", "fcvm-fleet.tf", "fcvm_coordinator_lane", "",
                  'resource "aws_ecs_task_definition" "fcvm_fleet_coordinator"',
                  'resource "aws_iam_role_policy" "fcvm_coordinator_release_fleet"',
                  'resource "aws_lambda_function" "fcvm_coordinator_release"', "fcvm_fleet_lambda_tile"),
                 ("prod", "fcvm-fleet-prod.tf", "fcvm-fleet-prod.tf", "fcvm_fleet_prod_coordinator_lane", "[0]",
                  'resource "aws_ecs_task_definition" "fcvm_fleet_prod_coordinator"',
                  'resource "aws_iam_role_policy" "fcvm_coordinator_release_prod_fleet"',
                  'resource "aws_lambda_function" "fcvm_coordinator_release_prod"', "fcvm_fleet_lambda_tile_prod[0]"))
        ci = {"stage": (ROOT / "fcvm-fleet-ci.tf").read_text(), "prod": (ROOT / "fcvm-fleet-prod-ci.tf").read_text()}
        for stack, pointer_file, task_file, pointer, index, task, policy, function, repository in cases:
            with self.subTest(stack=stack):
                param = block((ROOT / pointer_file).read_text(), f'resource "aws_ssm_parameter" "{pointer}"')
                self.assertIn(f'name     = "/fcvm-fleet/{stack}/lambda-image"', param)
                self.assertIn('value    = "none"', param)
                self.assertRegex(param, r"ignore_changes = \[value\]")
                ref = f"aws_ssm_parameter.{pointer}{index}"
                definition = block((ROOT / task_file).read_text(), task)
                self.assertIn(f"[for lane in [nonsensitive({ref}.value)] :", definition)
                self.assertIn('{ name = "FLEET_LAMBDA_IMAGE", value = lane } if lane != "none"', definition)
                # Only none or a digest in the stack's own lane repository is rendered.
                self.assertIn(f'"^(none|${{replace(aws_ecr_repository.{repository}.repository_url', definition)
                # The release role reads and writes the pointer; the function names it.
                rules = block(ci[stack], policy)
                self.assertRegex(rules, r'Action = \["ssm:GetParameter", "ssm:PutParameter"(, "ssm:GetParameterHistory")?\]'
                                 r'\s+Resource = [^\n]*'
                                 + re.escape(f"{ref}.arn"))
                self.assertRegex(block(ci[stack], function), r"LANE_PARAMETER\s+= " + re.escape(f"{ref}.name"))

    def test_the_functions_read_only_the_lane_repository_and_name_it(self):
        def block(text, header):
            start = text.index(header)
            return text[start:text.index("\n}\n", start) + 2]
        for stack, path, role, function in (
                ("stage", "fcvm-fleet-ci.tf", 'resource "aws_iam_role_policy" "fcvm_coordinator_release_fleet"',
                 'resource "aws_lambda_function" "fcvm_coordinator_release"'),
                ("prod", "fcvm-fleet-prod-ci.tf", 'resource "aws_iam_role_policy" "fcvm_coordinator_release_prod_fleet"',
                 'resource "aws_lambda_function" "fcvm_coordinator_release_prod"')):
            with self.subTest(stack=stack):
                tf = (ROOT / path).read_text()
                policy = block(tf, role)
                # One new statement: read the lane repository's images. Nothing writes to it.
                # Each function's own stack's repository (ejc3/aws#986: one lane repository per stack), never the other's.
                own = "aws_ecr_repository.fcvm_fleet_lambda_tile" + ("_prod[0]" if stack == "prod" else "")
                lane = [line.strip() for line in policy.splitlines() if "fcvm_fleet_lambda_tile" in line]
                self.assertEqual(lane, ['{ Effect = "Allow", Action = "ecr:DescribeImages", '
                                        f'Resource = {own}.arn }},'])
                self.assertRegex(block(tf, function), r"LANE_REPOSITORY_URL\s+= " + re.escape(own) + r"\.repository_url")


class ProdAWS(AWS):
    """Prod's service, its pointer history, the release record and the ActiveViewers metric."""

    def __init__(self):
        super().__init__()
        self.old["taskDefinition"]["family"] = PROD_OLD
        self.viewers = [0.0] * 5
        self.metric_status = "Complete"
        self.metric_reads = 0
        self.history = [self.image]
        self.stage_image = REPO + "@" + DIGEST
        self.record = "{}"
        self.tags = {DIGEST: ["b" * 40], "sha256:" + "c" * 64: ["e" * 40, "bdaa0003"]}
        self.lane_history = ["none"]

    def get_metric_data(self, **kwargs):
        query = kwargs["MetricDataQueries"][0]["MetricStat"]
        assert query["Metric"] == {"Namespace": "FCVM/Fleet", "MetricName": "ActiveViewers",
                                   "Dimensions": [{"Name": "Fleet", "Value": "prod"}]}, query
        assert query["Stat"] == "Maximum" and 300 <= (kwargs["EndTime"] - kwargs["StartTime"]).total_seconds() < 360
        self.metric_reads += 1
        stamps = [kwargs["StartTime"] + datetime.timedelta(minutes=i) for i in range(len(self.viewers))]
        return {"MetricDataResults": [{"Id": "viewers", "StatusCode": self.metric_status,
                                       "Values": list(self.viewers), "Timestamps": stamps}]}

    def get_parameter(self, **kwargs):
        name = kwargs["Name"]
        if name == os.environ.get("LANE_PARAMETER"):
            return super().get_parameter(**kwargs)
        if name == PROD_ENV["STAGE_IMAGE_PARAMETER"]:
            return {"Parameter": {"Value": self.stage_image}}
        if name == PROD_ENV["RELEASE_RECORD"]:
            return {"Parameter": {"Value": self.record}}
        assert name == PROD_ENV["IMAGE_PARAMETER"], name
        return super().get_parameter(**kwargs)

    def put_parameter(self, **kwargs):
        if kwargs["Name"] == os.environ.get("LANE_PARAMETER"):
            self.lane_history.append(kwargs["Value"])
            return super().put_parameter(**kwargs)
        if kwargs["Name"] == PROD_ENV["RELEASE_RECORD"]:
            self.record = kwargs["Value"]
            self.calls.append("record")
            return
        assert kwargs["Name"] == PROD_ENV["IMAGE_PARAMETER"], kwargs["Name"]
        super().put_parameter(**kwargs)
        self.history.append(kwargs["Value"])

    lane_history = None

    def get_parameter_history(self, **kwargs):
        assert kwargs["WithDecryption"] is False
        if kwargs["Name"] == os.environ.get("LANE_PARAMETER"):
            return {"Parameters": [{"Value": value} for value in (self.lane_history or ["none"])]}
        assert kwargs["Name"] == PROD_ENV["IMAGE_PARAMETER"]
        return {"Parameters": [{"Value": value} for value in self.history]}

    def describe_services(self, **kwargs):
        # Staging's service, which the candidate reads for the lane image staging runs.
        if kwargs.get("services") == [PROD_ENV["STAGE_SERVICE"]]:
            assert kwargs["cluster"] == PROD_ENV["STAGE_CLUSTER"]
            return {"services": [{"taskDefinition": "arn:example:task-definition/fcvm-fleet-coordinator:3",
                                  "deployments": [{"rolloutState": "COMPLETED"}]}]}
        return super().describe_services(**kwargs)

    def describe_task_definition(self, **kwargs):
        # Staging's running task: its image is the one staging's pointer names.
        if kwargs["taskDefinition"].endswith("fcvm-fleet-coordinator:3"):
            stage = copy.deepcopy(self.old)
            stage["taskDefinition"]["family"] = "fcvm-fleet-coordinator"
            stage["taskDefinition"]["containerDefinitions"][0]["image"] = self.stage_image
            return stage
        return super().describe_task_definition(**kwargs)

    def describe_images(self, **kwargs):
        ids = kwargs["imageIds"][0]
        if "imageDigest" in ids:
            return {"imageDetails": [{"imageDigest": ids["imageDigest"], "imageTags": self.tags.get(ids["imageDigest"], [])}]}
        return super().describe_images(**kwargs)


class ProdReleaseTests(unittest.TestCase):
    """The prod function: the viewer hold, force for rollback only, and the release record."""

    def setUp(self):
        self.aws = ProdAWS()
        self.env = patch.dict(os.environ, PROD_ENV)
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_release(self, event=EVENT):
        return release.deploy(event, self.aws, self.aws, self.aws, self.aws, self.aws)

    def test_a_viewer_in_the_last_five_minutes_holds_the_release_without_a_lock_or_a_change(self):
        self.aws.viewers = [0.0, 1.0, 0.0]
        self.assertEqual(self.run_release(), {"status": "held", "reason": "viewers"})
        self.assertEqual(self.aws.calls, [])
        self.assertIsNone(self.aws.lock)

    def test_no_viewer_data_holds_the_release(self):
        for values, status in (([], "Complete"), ([0.0], "Complete"), ([0.0], "PartialData")):
            with self.subTest(values=values, status=status):
                self.setUp()
                self.aws.viewers, self.aws.metric_status = values, status
                self.assertEqual(self.run_release(), {"status": "held", "reason": "no-viewer-data"})
                self.assertEqual(self.aws.calls, [])

    def test_quiet_releases_prod_and_checks_the_viewers_again_under_the_lock(self):
        result = self.run_release()
        self.assertEqual(result["status"], "deployed")
        self.assertEqual(self.aws.metric_reads, 2)
        self.assertEqual(self.aws.calls, ["lock", "pointer", "register", NEW, "pointer", "unlock"])

    def test_a_viewer_who_arrives_while_the_lock_is_taken_holds_the_release(self):
        real = self.aws.put_item
        def arrives(**kwargs):
            real(**kwargs)
            self.aws.viewers = [1.0]
        self.aws.put_item = arrives
        self.assertEqual(self.run_release(), {"status": "held", "reason": "viewers"})
        self.assertEqual(self.aws.calls, ["lock", "unlock"])
        self.assertEqual(self.aws.arn, OLD)

    def test_force_skips_the_hold_only_for_an_image_prod_ran_before(self):
        self.aws.viewers = [3.0]
        with self.assertRaisesRegex(RuntimeError, "ran before"):
            self.run_release({**EVENT, "force": True})
        self.assertEqual(self.aws.calls, [])
        self.aws.history.insert(0, REPO + "@" + DIGEST)
        self.assertEqual(self.run_release({**EVENT, "force": True})["status"], "deployed")
        self.assertEqual(self.aws.metric_reads, 0)

    def test_force_cannot_change_the_lane(self):
        # force skips the viewer hold to return to an image prod ran before; a lane prod does not run is no rollback.
        lane_ecr = LaneECR(current=LANE)
        prod_lane = "registry.example/fcvm-fleet/lambda-tile-prod"
        self.aws.history.insert(0, REPO + "@" + DIGEST)
        self.aws.viewers = [3.0]
        with patch.dict(os.environ, {"LANE_REPOSITORY_URL": prod_lane, "LANE_PARAMETER": "/fleet/prod/lambda-image"}):
            with self.assertRaises(release.Refused) as refused:
                release.deploy({**EVENT, "force": True, "lambda_digest": LANE}, self.aws, self.aws, self.aws, self.aws,
                               self.aws, lane_ecr=lane_ecr)
            self.assertEqual(refused.exception.code, "not-a-rollback")
            self.assertEqual((self.aws.calls, self.aws.arn), (["lock", "unlock"], OLD))
            # The lane prod already runs may ride along with a forced rollback of the coordinator image.
            self.aws.old["taskDefinition"]["containerDefinitions"][0]["environment"].append(
                {"name": "FLEET_LAMBDA_IMAGE", "value": prod_lane + "@" + LANE})
            self.aws.lane_pointer = prod_lane + "@" + LANE
            result = release.deploy({**EVENT, "force": True, "lambda_digest": LANE}, self.aws, self.aws, self.aws,
                                    self.aws, self.aws, lane_ecr=lane_ecr)
        self.assertEqual(result["status"], "deployed")
        self.assertEqual(self.aws.metric_reads, 0)

    def test_force_must_be_true_and_staging_takes_no_force_or_action(self):
        with self.assertRaises(ValueError):
            self.run_release({**EVENT, "force": "yes"})
        with patch.dict(os.environ, ENV):
            for event in ({**EVENT, "force": True}, {"action": "quiet"}, {"action": "candidate"}):
                with self.subTest(event=event), self.assertRaises(ValueError):
                    self.run_release(event)
        self.assertEqual(self.aws.calls, [])

    def test_the_prod_function_never_releases_another_family(self):
        self.aws.old["taskDefinition"]["family"] = "fcvm-fleet-coordinator"
        with self.assertRaisesRegex(ValueError, "task family"):
            self.run_release()
        self.assertNotIn("register", self.aws.calls)

    def test_quiet_is_read_only(self):
        self.assertEqual(self.run_release({"action": "quiet"}), {"status": "quiet"})
        self.aws.viewers = [2.0]
        self.assertEqual(self.run_release({"action": "quiet"}), {"status": "held", "reason": "viewers"})
        self.assertEqual(self.aws.calls, [])

    def test_candidate_names_both_images_by_digest_and_commit(self):
        self.assertEqual(self.run_release({"action": "candidate"}), {
            "status": "ok", "stage": {"digest": DIGEST, "commits": ["b" * 40], "lambda_digest": None},
            "prod": {"digest": "sha256:" + "c" * 64, "commits": ["e" * 40], "lambda_digest": None}, "release": None})

    def test_a_lane_image_candidate_cannot_read_is_a_per_stack_error(self):
        # The manual rollback reads the record through candidate also with the prod lane off: staging's service
        # missing, or a running task that disagrees with its pointer or is still rolling out, is reported per
        # stack (ejc3/fcvm's decide refuses on it) and never fails the whole answer.
        real = self.aws.describe_services
        def broken(**kwargs):
            if kwargs["services"] == [PROD_ENV["STAGE_SERVICE"]]:
                return {"services": [], "failures": [{"reason": "MISSING"}]}
            return real(**kwargs)
        self.aws.describe_services = broken
        result = self.run_release({"action": "candidate"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual((result["stage"]["lambda_digest"], result["stage"]["lane_error"]), (None, "service-unavailable"))
        self.assertNotIn("lane_error", result["prod"])
        self.assertIsNone(result["release"])

    def test_a_lane_read_that_fails_outside_aws_is_a_per_stack_error(self):
        # A timeout or endpoint error (not a ClientError), or a task definition without a coordinator container,
        # leaves the answer ok with the record in it: the manual rollback reads the record this way.
        real_services, real_definition = self.aws.describe_services, self.aws.describe_task_definition
        def timeout(**kwargs):
            if kwargs["services"] == [PROD_ENV["STAGE_SERVICE"]]:
                raise BotoCoreError("read timeout")
            return real_services(**kwargs)
        self.aws.describe_services = timeout
        result = self.run_release({"action": "candidate"})
        self.assertEqual(result["status"], "ok")
        self.assertIn("release", result)
        self.assertEqual((result["stage"]["lambda_digest"], result["stage"]["lane_error"]), (None, "unreadable"))
        self.assertNotIn("lane_error", result["prod"])
        self.aws.describe_services = real_services
        def no_coordinator(**kwargs):
            answer = copy.deepcopy(real_definition(**kwargs))
            for container in answer["taskDefinition"]["containerDefinitions"]:
                container["name"] = "other"
            return answer
        self.aws.describe_task_definition = no_coordinator
        result = self.run_release({"action": "candidate"})
        self.assertEqual(result["status"], "ok")
        self.assertIn("release", result)
        self.assertEqual((result["prod"]["lane_error"], result["stage"]["lane_error"]), ("unreadable", "unreadable"))

    def test_a_single_deployment_still_rolling_out_is_not_idle(self):
        real = self.aws.describe_services
        def rolling(**kwargs):
            answer = real(**kwargs)
            answer["services"][0]["deployments"] = [{"rolloutState": "IN_PROGRESS"}]
            return answer
        self.aws.describe_services = rolling
        result = self.run_release({"action": "candidate"})
        self.assertEqual((result["stage"]["lane_error"], result["prod"]["lane_error"]), ("not-idle", "not-idle"))

    def test_candidate_refuses_a_lane_image_from_a_task_its_pointer_does_not_name(self):
        # Prod's pointer names one image, its service runs another (a release in progress or rolled back).
        self.aws.old["taskDefinition"]["containerDefinitions"][0]["image"] = REPO + "@sha256:" + "9" * 64
        result = self.run_release({"action": "candidate"})
        self.assertEqual(result["prod"]["lane_error"], "pointer-mismatch")
        self.aws.old["taskDefinition"]["containerDefinitions"][0]["image"] = self.aws.image
        real = self.aws.describe_services
        def rolling(**kwargs):
            answer = real(**kwargs)
            answer["services"][0]["deployments"] = [{"rolloutState": "IN_PROGRESS"}, {"rolloutState": "COMPLETED"}]
            return answer
        self.aws.describe_services = rolling
        self.assertEqual(self.run_release({"action": "candidate"})["prod"]["lane_error"], "not-idle")

    def test_candidate_names_the_lane_image_each_coordinator_runs(self):
        # What a stack runs comes from its running task, never from the lane repository's tags.
        # Each stack's task names its own stack's lane repository (ejc3/aws#986).
        stage_url, prod_url = PROD_ENV["STAGE_LANE_REPOSITORY_URL"], PROD_ENV["LANE_REPOSITORY_URL"]
        stage_lane, prod_lane = "sha256:" + "4" * 64, "sha256:" + "5" * 64
        stage_def = copy.deepcopy(self.aws.old)
        stage_def["taskDefinition"]["family"] = "fcvm-fleet-coordinator"
        stage_def["taskDefinition"]["containerDefinitions"][0]["image"] = self.aws.stage_image
        stage_def["taskDefinition"]["containerDefinitions"][0]["environment"].append(
            {"name": "FLEET_LAMBDA_IMAGE", "value": stage_url + "@" + stage_lane})
        self.aws.old["taskDefinition"]["containerDefinitions"][0]["environment"].append(
            {"name": "FLEET_LAMBDA_IMAGE", "value": prod_url + "@" + prod_lane})
        asked = []
        real_services, real_definition = self.aws.describe_services, self.aws.describe_task_definition
        def describe_services(**kwargs):
            asked.append(kwargs)
            if kwargs["services"] == [PROD_ENV["STAGE_SERVICE"]]:
                assert kwargs["cluster"] == PROD_ENV["STAGE_CLUSTER"]
                return {"services": [{"taskDefinition": "arn:example:task-definition/fcvm-fleet-coordinator:3",
                                      "deployments": [{"rolloutState": "COMPLETED"}]}]}
            return real_services(**kwargs)
        def describe_task_definition(**kwargs):
            if kwargs["taskDefinition"].endswith("fcvm-fleet-coordinator:3"):
                return copy.deepcopy(stage_def)
            return real_definition(**kwargs)
        self.aws.describe_services, self.aws.describe_task_definition = describe_services, describe_task_definition
        result = self.run_release({"action": "candidate"})
        self.assertEqual((result["stage"]["lambda_digest"], result["prod"]["lambda_digest"]), (stage_lane, prod_lane))
        self.assertEqual(self.aws.calls, [])
        # A task that names the other stack's repository is refused for that stack.
        stage_def["taskDefinition"]["containerDefinitions"][0]["environment"][-1]["value"] = prod_url + "@" + stage_lane
        result = self.run_release({"action": "candidate"})
        self.assertEqual((result["stage"]["lane_error"], result["prod"]["lambda_digest"]), ("lane-invalid", prod_lane))

    def entry(self, digest="sha256:" + "c" * 64, commit="e" * 40, generation=7):
        return {"coordinator": {"commit": commit, "digest": digest}, "web_commit": "f" * 40, "worker_version": "0" * 8 + "-0000-0000-0000-" + "0" * 12,
                "generation": generation, "build_id": "factory:1", "smoke_run": 42}

    def test_the_record_names_what_prod_runs_and_swaps_only_its_predecessor(self):
        first = {**self.entry(), "previous": None}
        self.assertEqual(self.run_release({"action": "record", "release": first}), {"status": "recorded"})
        self.assertEqual(json.loads(self.aws.record), first)
        self.assertEqual(self.run_release({"action": "record", "release": first}), {"status": "unchanged"})
        stale = {**self.entry(generation=8), "previous": self.entry(generation=6)}
        with self.assertRaisesRegex(RuntimeError, "changed since"):
            self.run_release({"action": "record", "release": stale})
        other = {**self.entry(digest=DIGEST, commit="b" * 40), "previous": self.entry()}
        with self.assertRaisesRegex(RuntimeError, "prod runs"):
            self.run_release({"action": "record", "release": other})
        for field, value in (("worker_version", "latest"), ("web_commit", "main")):
            with self.assertRaises(ValueError):
                self.run_release({"action": "record", "release": {**self.entry(), field: value, "previous": None}})
        self.assertEqual(json.loads(self.aws.record), first)


class ProdLaneRecordTests(ProdReleaseTests):
    """ejc3/fcvm's prod promotion records the set's lane image (lambda_digest) so a rollback returns to it."""

    def test_a_record_may_name_the_sets_lane_image(self):
        lane, earlier = "sha256:" + "4" * 64, "sha256:" + "5" * 64
        first = {**self.entry(), "lambda_digest": lane, "previous": {**self.entry(generation=6), "lambda_digest": earlier}}
        self.assertEqual(self.run_release({"action": "record", "release": first}), {"status": "recorded"})
        self.assertEqual(json.loads(self.aws.record), first)
        # The next record's previous is that entry, lane image included.
        second = {**self.entry(), "lambda_digest": earlier, "previous": {k: v for k, v in first.items() if k != "previous"}}
        self.assertEqual(self.run_release({"action": "record", "release": second}), {"status": "recorded"})

    def test_a_malformed_lane_image_is_refused(self):
        for value in ("latest", None, 7, "sha256:" + "A" * 64):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.run_release({"action": "record", "release": {**self.entry(), "lambda_digest": value, "previous": None}})


class ProdForcedLaneTests(unittest.TestCase):
    """force skips the viewer hold to return to a set prod ran before (ejc3/fcvm's rollback). Its lane image, or no
    lane, must be one prod's lane pointer has held; a lane prod never ran waits for the hold like any release."""
    PROD_LANE = "registry.example/fcvm-fleet/lambda-tile-prod"

    def setUp(self):
        self.aws, self.lane = ProdAWS(), LaneECR(current=LANE)
        self.aws.history.insert(0, REPO + "@" + DIGEST)  # the coordinator image prod ran before
        self.aws.viewers = [3.0]  # a viewer: only force can go ahead
        p = patch.dict(os.environ, {**PROD_ENV, "LANE_REPOSITORY_URL": self.PROD_LANE,
                                    "LANE_PARAMETER": "/fleet/prod/lambda-image"})
        p.start()
        self.addCleanup(p.stop)

    def running(self, lane):
        self.aws.old["taskDefinition"]["containerDefinitions"][0]["environment"].append(
            {"name": "FLEET_LAMBDA_IMAGE", "value": self.PROD_LANE + "@" + lane})
        self.aws.lane_pointer = self.PROD_LANE + "@" + lane

    def release(self, lane):
        return release.deploy({**EVENT, "force": True, "lambda_digest": lane}, self.aws, self.aws, self.aws, self.aws,
                              self.aws, lane_ecr=self.lane)

    def test_force_returns_to_a_lane_prod_ran_before(self):
        self.running("sha256:" + "9" * 64)
        self.aws.lane_history = ["none", self.PROD_LANE + "@" + LANE, self.PROD_LANE + "@sha256:" + "9" * 64]
        self.assertEqual(self.release(LANE)["status"], "deployed")
        self.assertEqual((self.aws.lane_pointer, self.aws.metric_reads), (self.PROD_LANE + "@" + LANE, 0))

    def test_force_returns_to_no_lane_when_prod_ran_none(self):
        self.running(LANE)
        self.aws.lane_history = ["none", self.PROD_LANE + "@" + LANE]
        self.assertEqual(self.release(None)["status"], "deployed")
        self.assertEqual(self.aws.lane_pointer, "none")

    def test_force_refuses_a_lane_prod_never_ran(self):
        self.running("sha256:" + "9" * 64)
        self.aws.lane_history = ["none", self.PROD_LANE + "@sha256:" + "9" * 64]
        with self.assertRaises(release.Refused) as refused:
            self.release(LANE)
        self.assertEqual(refused.exception.code, "not-a-rollback")
        self.assertEqual((self.aws.calls, self.aws.arn), (["lock", "unlock"], OLD))

    def test_force_refuses_no_lane_when_the_pointer_never_held_none(self):
        self.running(LANE)
        self.aws.lane_history = [self.PROD_LANE + "@" + LANE]
        with self.assertRaises(release.Refused) as refused:
            self.release(None)
        self.assertEqual(refused.exception.code, "not-a-rollback")
        self.assertEqual(self.aws.arn, OLD)


class LaneClearTests(LaneReleaseTests):
    """A rollback to a set that ran no lane image (ejc3/fcvm --lambda-digest none) sends lambda_digest null: the
    function removes FLEET_LAMBDA_IMAGE and reads no tag."""

    def test_a_null_lane_digest_removes_the_lane_image(self):
        self.aws.old["taskDefinition"]["containerDefinitions"][0]["environment"].append(
            {"name": "FLEET_LAMBDA_IMAGE", "value": LANE_URL + "@" + LANE})
        self.aws.lane_pointer = LANE_URL + "@" + LANE
        result = self.run_release({**EVENT, "lambda_digest": None})
        self.assertEqual(result, {"status": "deployed", "taskDefinition": NEW, "digest": DIGEST, "lambda_digest": None})
        self.assertEqual(self.lane.calls, [])
        self.assertEqual(self.coordinator_env(self.aws.registered), {"STATE": "bucket"})
        # The lane pointer follows, so the next Terraform apply renders no lane either.
        self.assertEqual(self.aws.lane_pointer, "none")

    def test_a_null_lane_digest_with_no_lane_image_running_is_unchanged(self):
        self.aws.image = REPO + "@" + DIGEST
        self.aws.old["taskDefinition"]["containerDefinitions"][0]["image"] = self.aws.image
        self.assertEqual(self.run_release({**EVENT, "lambda_digest": None})["status"], "unchanged")
        self.assertEqual(self.lane.calls, [])


class ProdLaneTerraformTests(unittest.TestCase):
    def test_the_prod_function_reads_stagings_service_only(self):
        tf = (ROOT / "fcvm-fleet-prod-ci.tf").read_text()
        policy = tf.split('resource "aws_iam_role_policy" "fcvm_coordinator_release_prod_fleet"', 1)[1].split("\n}\n", 1)[0]
        self.assertIn('{ Effect = "Allow", Action = "ecs:DescribeServices", Resource = aws_ecs_service.fcvm_fleet_coordinator.id }', policy)
        self.assertNotIn('"ecs:UpdateService", Resource = aws_ecs_service.fcvm_fleet_coordinator.id', policy)
        function = tf.split('resource "aws_lambda_function" "fcvm_coordinator_release_prod"', 1)[1].split("\n}\n", 1)[0]
        self.assertRegex(function, r"STAGE_CLUSTER\s+= aws_ecs_cluster\.fcvm_fleet\.name")
        self.assertRegex(function, r"STAGE_SERVICE\s+= aws_ecs_service\.fcvm_fleet_coordinator\.name")
        self.assertRegex(function, r"STAGE_LANE_REPOSITORY_URL\s+= aws_ecr_repository\.fcvm_fleet_lambda_tile\.repository_url")
        self.assertRegex(function, r"LANE_REPOSITORY_URL\s+= aws_ecr_repository\.fcvm_fleet_lambda_tile_prod\[0\]\.repository_url")


class ProdCiTerraformTests(unittest.TestCase):
    """fcvm-fleet-prod-ci.tf: what the prod CI role and the prod functions may do, and that all of it is gated."""

    def setUp(self):
        self.text = (ROOT / "fcvm-fleet-prod-ci.tf").read_text()

    def block(self, kind, name):
        start = self.text.index(f'{kind} "{name}"')
        depth, i = 0, self.text.index("{", start)
        for j in range(i, len(self.text)):
            depth += {"{": 1, "}": -1}.get(self.text[j], 0)
            if depth == 0:
                return self.text[start:j + 1]
        raise AssertionError("unterminated block")

    def test_every_resource_exists_only_with_the_prod_stack(self):
        names = re.findall(r'^resource "([a-z0-9_]+)" "([a-z0-9_]+)"', self.text, re.M)
        self.assertGreater(len(names), 15)
        for kind, name in names:
            with self.subTest(resource=name):
                head = self.block(f'resource "{kind}"', name).split("\n")[1:4]
                self.assertTrue(any(re.match(r"\s*count\s*=\s*local\.fcvm_fleet_prod$", line) or
                                    re.match(r"\s*for_each\s*=\s*var\.fcvm_fleet_prod_enabled \?", line) or
                                    re.match(r"\s*provider\s*=", line) for line in head[:1]) and
                                any("local.fcvm_fleet_prod" in line or "var.fcvm_fleet_prod_enabled" in line for line in head),
                                f"{name} is not behind fcvm_fleet_prod_enabled")

    def test_the_ci_role_is_the_prod_environment_on_main_and_may_only_invoke_the_prod_functions(self):
        role = self.block('resource "aws_iam_role"', "fcvm_prod_ci")
        self.assertIn('"repo:ejc3/fcvm:environment:${local.fcvm_prod_environment}"', role)
        self.assertIn('fcvm_prod_environment           = "fleet-prod-deploy"', self.text)
        self.assertIn('branch_pattern = "main"', self.block('resource "github_repository_environment_deployment_policy"', "fcvm_prod_main"))
        self.assertIn("can_admins_bypass = false", self.block('resource "github_repository_environment"', "fcvm_prod"))
        policy = self.block('resource "aws_iam_role_policy"', "fcvm_prod_ci")
        allows = re.findall(r'Effect = "Allow", Action = "([^"]+)", Resource = ([^ }]+)', policy)
        self.assertEqual(allows, [("lambda:InvokeFunction", "local.fcvm_prod_release_functions")])
        # Besides invoking, only the prod lane's copy (ejc3/fcvm fleet/lambda/release.py copy): read the image staging
        # tested from staging's lane repository, write it into prod's by digest and move keep-prod-* there.
        read = self.statement(policy, "ReadTheTestedLaneImage")
        self.assertCountEqual(re.findall(r'"(ecr:[^"]+)"', read), ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"])
        self.assertIn("Resource = aws_ecr_repository.fcvm_fleet_lambda_tile.arn", read)
        write = self.statement(policy, "CopyAndRetagTheLaneImage")
        self.assertCountEqual(re.findall(r'"(ecr:[^"]+)"', write), [
            "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload",
            "ecr:PutImage", "ecr:BatchGetImage"])
        self.assertIn("Resource = aws_ecr_repository.fcvm_fleet_lambda_tile_prod[0].arn", write)
        token = self.statement(policy, "SignInToTheLaneRegistry")
        self.assertIn('Action    = "ecr:GetAuthorizationToken"', token)
        self.assertIn('"aws:RequestedRegion" = "us-east-1"', token)
        # Staging's repository: those two reads and nothing else.
        stage_only = self.statement(policy, "OnlyReadStagingsLaneRepository")
        self.assertRegex(stage_only, r'Effect\s+= "Deny"')
        self.assertIn('NotAction = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]', stage_only)
        self.assertIn("Resource  = aws_ecr_repository.fcvm_fleet_lambda_tile.arn", stage_only)
        admin = self.statement(policy, "NeverAdministerProdsLaneRepository")
        self.assertRegex(admin, r'Effect\s+= "Deny"')
        self.assertCountEqual(re.findall(r'"(ecr:[^"]+)"', admin),
                              ["ecr:BatchDeleteImage", "ecr:DeleteRepository", "ecr:SetRepositoryPolicy", "ecr:DeleteRepositoryPolicy",
                               "ecr:PutLifecyclePolicy", "ecr:DeleteLifecyclePolicy", "ecr:PutImageTagMutability",
                               "ecr:PutImageScanningConfiguration", "ecr:PutReplicationConfiguration"])
        self.assertIn("Resource = aws_ecr_repository.fcvm_fleet_lambda_tile_prod[0].arn", admin)
        # Every other repository stays denied. The deny names actions, not ecr:*, because GetAuthorizationToken's
        # resource is "*": a NotResource deny of ecr:* would deny the sign-in too.
        other = self.statement(policy, "NoOtherRepository")
        self.assertIn("NotResource = [aws_ecr_repository.fcvm_fleet_lambda_tile.arn, aws_ecr_repository.fcvm_fleet_lambda_tile_prod[0].arn]", other)
        patterns = re.findall(r'"ecr:([^"]+)"', other)
        known = ["BatchCheckLayerAvailability", "BatchDeleteImage", "BatchGetImage", "BatchGetRepositoryScanningConfiguration",
                 "CompleteLayerUpload", "CreatePullThroughCacheRule", "CreateRepository", "CreateRepositoryCreationTemplate",
                 "DeleteLifecyclePolicy", "DeletePullThroughCacheRule", "DeleteRegistryPolicy", "DeleteRepository",
                 "DeleteRepositoryCreationTemplate", "DeleteRepositoryPolicy", "DescribeImageReplicationStatus",
                 "DescribeImageScanFindings", "DescribeImages", "DescribePullThroughCacheRules", "DescribeRegistry",
                 "DescribeRepositories", "DescribeRepositoryCreationTemplates", "GetAccountSetting", "GetDownloadUrlForLayer",
                 "GetLifecyclePolicy", "GetLifecyclePolicyPreview", "GetRegistryPolicy", "GetRegistryScanningConfiguration",
                 "GetRepositoryPolicy", "InitiateLayerUpload", "ListImages", "ListTagsForResource", "PutAccountSetting",
                 "PutImage", "PutImageScanningConfiguration", "PutImageTagMutability", "PutLifecyclePolicy",
                 "PutRegistryPolicy", "PutRegistryScanningConfiguration", "PutReplicationConfiguration", "ReplicateImage",
                 "SetRepositoryPolicy", "StartImageScan", "StartLifecyclePolicyPreview", "TagResource", "UntagResource",
                 "UpdatePullThroughCacheRule", "UpdateRepositoryCreationTemplate", "UploadLayerPart",
                 "ValidatePullThroughCacheRule"]
        def denied(action):
            return any(re.fullmatch(p.replace("*", ".*"), action) for p in patterns)
        self.assertEqual([a for a in known if not denied(a)], [])
        self.assertFalse(denied("GetAuthorizationToken"))
        never = self.statement(policy, "NeverPublishAdministerOrReadCredentials")
        self.assertNotIn('"ecr:*"', never)
        # Lambda tests the resolved $LATEST even for CI's unqualified Invoke. Versions, aliases and other functions
        # must still match the explicit deny, and only the unqualified production ARNs appear in the Allow above.
        deny = re.search(r'Sid\s+= "NoOtherFunction"\s+Effect\s+= "Deny"\s+Action\s+= "lambda:\*"\s+NotResource\s+= ([^\n]+)', policy)
        self.assertIsNotNone(deny)
        self.assertEqual(deny[1], 'concat(local.fcvm_prod_release_functions, [for arn in local.fcvm_prod_release_functions : "${arn}:$LATEST"])')
        functions = self.text.split("fcvm_prod_release_functions = concat(", 1)[1].split("\n  )", 1)[0]
        self.assertEqual(re.findall(r"aws_lambda_function\.([a-z_]+)", functions),
                         ["fcvm_coordinator_release_prod", "fcvm_ami_prod_release"])
        # The ECR repository's policy grants push to staging's CI role only.
        ci = (ROOT / "fcvm-fleet-ci.tf").read_text()
        repo_policy = ci.split('resource "aws_ecr_repository_policy" "fcvm_coordinator_ci"', 1)[1].split("\n}\n", 1)[0]
        self.assertNotIn("fcvm_prod_ci", repo_policy)

    def statement(self, policy, sid):
        return re.split(r'Sid\s+= "' + sid + '"', policy, maxsplit=1)[1].split("},", 1)[0]

    def test_the_prod_ci_role_reads_staging_and_writes_prods_lane_repository(self):
        secrets = self.block('resource "github_actions_environment_secret"', "fcvm_prod")
        self.assertIn('"LAMBDA_TILE_REPO"', secrets)
        self.assertIn("LAMBDA_TILE_REPO        = aws_ecr_repository.fcvm_fleet_lambda_tile_prod[0].repository_url", secrets)
        lane = (ROOT / "fcvm-fleet-lambda.tf").read_text()
        def policy(name):
            return lane.split(f'resource "aws_ecr_repository_policy" "{name}"', 1)[1].split("\n}\n", 1)[0]
        stage, prod = policy("fcvm_fleet_lambda_tile"), policy("fcvm_fleet_lambda_tile_prod")
        # Staging's repository admits prod CI to read only, and only while prod exists.
        read = self.statement(stage, "ProdCiReadsTheTestedLaneImage")
        self.assertIn("Principal = { AWS = aws_iam_role.fcvm_prod_ci[0].arn }", read)
        self.assertCountEqual(re.findall(r'"(ecr:[^"]+)"', read), ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"])
        self.assertIn("var.fcvm_fleet_prod_enabled ?", stage)
        self.assertNotIn("ProdCiRetagsTheLaneImage", lane)
        # Prod's admits prod CI's copy and retag, the same actions its role holds there.
        write = self.statement(prod, "ProdCiCopiesAndRetagsTheLaneImage")
        self.assertIn("Principal = { AWS = aws_iam_role.fcvm_prod_ci[0].arn }", write)
        self.assertCountEqual(re.findall(r'"(ecr:[^"]+)"', write), [
            "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload",
            "ecr:PutImage", "ecr:BatchGetImage"])

    def test_the_prod_functions_reach_prod_only(self):
        fleet = self.block('resource "aws_iam_role_policy"', "fcvm_coordinator_release_prod_fleet")
        # Staging's service only to read which lane image it runs.
        self.assertEqual([line.strip() for line in fleet.splitlines() if "aws_ecs_service.fcvm_fleet_coordinator." in line],
                         ['{ Effect = "Allow", Action = "ecs:DescribeServices", Resource = aws_ecs_service.fcvm_fleet_coordinator.id },'])
        self.assertNotIn("aws_ecs_task_definition.fcvm_fleet_coordinator.", fleet)
        # Staging's pointer: read only.
        self.assertRegex(fleet, r'Action = "ssm:GetParameter", Resource = aws_ssm_parameter\.fcvm_coordinator_release\.arn')
        images = self.block('resource "aws_iam_role_policy"', "fcvm_ami_prod_release_fleet")
        self.assertIn("module.fcvm_fleet_prod_east1[0].image_templates", images)
        self.assertNotIn("local.fcvm_ami_templates", images)
        self.assertRegex(images, r'Action = "ssm:GetParameter", Resource = aws_ssm_parameter\.fcvm_ami_release\.arn')
        function = self.block('resource "aws_lambda_function"', "fcvm_ami_prod_release")
        self.assertIn('handler                        = "publish.prod_handler"', function)
        self.assertIn("data.archive_file.fcvm_ami_prod_release.output_path", function)
        archive = self.block('data "archive_file"', "fcvm_ami_prod_release")
        self.assertNotIn("publisher-config.json", archive)

    def test_the_prod_worker_token_lives_only_in_the_prod_environment(self):
        secrets = self.block('resource "github_actions_environment_secret"', "fcvm_prod")
        self.assertIn("cloudflare_account_token.fcvm_prod_workers_deploy[0].value", secrets)
        self.assertIn("github_repository_environment.fcvm_prod[0].environment", secrets)
        self.assertEqual(len(re.findall(r"cloudflare_account_token\.fcvm_prod_workers_deploy", self.text)), 2)

    def test_both_functions_name_their_stack(self):
        staging = (ROOT / "fcvm-fleet-ci.tf").read_text()
        self.assertRegex(staging, r'STACK   = "stage"\n\s*CLUSTER = aws_ecs_cluster\.fcvm_fleet\.name')
        prod = self.block('resource "aws_lambda_function"', "fcvm_coordinator_release_prod")
        for key, value in (("STACK", '"prod"'), ("CLUSTER", "aws_ecs_cluster.fcvm_fleet_prod[0].name"),
                           ("FAMILY", "aws_ecs_task_definition.fcvm_fleet_prod_coordinator[0].family"),
                           ("METRICS_NAMESPACE", '"FCVM/Fleet"')):
            self.assertRegex(prod, r"\n\s+" + key + r"\s+= " + re.escape(value) + "\n")


class Session:
    def __init__(self, aws):
        self.aws = aws

    def client(self, name, config=None, region_name=None):
        return self.aws


class HandlerTests(unittest.TestCase):
    """The failure result names the step and the AWS error code, and nothing else."""

    def setUp(self):
        self.aws = AWS()
        self.aws.assume_role = lambda **kwargs: {"Credentials": {
            "AccessKeyId": "id", "SecretAccessKey": "secret", "SessionToken": "token"}}
        fake = types.SimpleNamespace(client=lambda name, config=None: self.aws,
                                     Session=lambda **kwargs: Session(self.aws))
        self.patches = [patch.object(release, "boto3", fake), patch.object(release, "Config", lambda **kwargs: None),
                        patch.dict(os.environ, {**ENV, "RELEASE_ROLE": "arn:example:role/release"})]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.context = types.SimpleNamespace(get_remaining_time_in_millis=lambda: 850_000)

    def break_call(self, method, code="AccessDeniedException"):
        def raises(**kwargs):
            raise ClientError({"Error": {"Code": code, "Message": "secret detail"},
                               "ResponseMetadata": {"HTTPHeaders": {"x-secret": "value"}}})
        setattr(self.aws, method, raises)

    def handle(self):
        result = release.lambda_handler(EVENT, self.context)
        self.assertNotIn("secret", json.dumps(result))
        return result

    def test_each_failing_step_is_named_with_its_code(self):
        cases = [("assume_role", "credentials"), ("put_item", "lock"),
                 ("describe_task_definition", "inspect"), ("register_task_definition", "register"),
                 ("delete_item", "unlock")]
        for method, step in cases:
            with self.subTest(step=step):
                self.setUp()
                self.break_call(method)
                self.assertEqual(self.handle(), {"status": "failed", "errorType": "ClientError",
                                                 "step": step, "code": "AccessDeniedException"})

    def test_a_failed_service_update_is_named_after_its_rollback(self):
        real = self.aws.update_service
        def forward_update_fails(**kwargs):
            real(**kwargs)
            if kwargs["taskDefinition"] == NEW:
                raise ClientError({"Error": {"Code": "InvalidParameterException", "Message": "secret detail"}})
        self.aws.update_service = forward_update_fails
        self.assertEqual(self.handle(), {"status": "failed", "errorType": "ClientError",
                                         "step": "update", "code": "InvalidParameterException"})
        self.assertEqual(self.aws.arn, OLD)

    def test_the_failed_pointer_write_is_named(self):
        real = self.aws.put_parameter
        def second_write_fails(**kwargs):
            if kwargs["Value"] == REPO + "@" + DIGEST:
                raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "secret detail"}})
            real(**kwargs)
        self.aws.put_parameter = second_write_fails
        self.assertEqual(self.handle(), {"status": "failed", "errorType": "ClientError",
                                         "step": "pointer", "code": "ThrottlingException"})

    def test_a_failed_rollout_is_named_after_a_successful_rollback(self):
        self.aws.fail_rollout = True
        self.assertEqual(self.handle(), {"status": "failed", "errorType": "Refused",
                                         "step": "rollout", "code": "rollout-failed"})
        self.assertEqual(self.aws.arn, OLD)

    def test_a_failed_rollback_is_named(self):
        self.aws.fail_rollout = True
        real = self.aws.update_service
        def rollback_fails(**kwargs):
            if kwargs["taskDefinition"] == OLD:
                raise ClientError({"Error": {"Code": "ServiceNotActiveException"}})
            real(**kwargs)
        self.aws.update_service = rollback_fails
        self.assertEqual(self.handle(), {"status": "failed", "errorType": "ClientError",
                                         "step": "rollback", "code": "ServiceNotActiveException"})

    def test_an_error_without_a_code_names_only_its_step(self):
        self.aws.describe_task_definition = lambda **kwargs: (_ for _ in ()).throw(TimeoutError("secret detail"))
        self.assertEqual(self.handle(), {"status": "failed", "errorType": "TimeoutError", "step": "inspect"})

    def test_a_completed_release_reports_deployed(self):
        self.assertEqual(self.handle()["status"], "deployed")


if __name__ == "__main__":
    unittest.main(buffer=True)
