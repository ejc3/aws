#!/usr/bin/env python3
"""Offline release tests: image-only changes, backend-lock ownership and rollback."""
import copy
import importlib.util
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class ClientError(Exception):
    def __init__(self, response, operation="test"):
        self.response = response


stubs = {"boto3": types.ModuleType("boto3"), "botocore": types.ModuleType("botocore"),
         "botocore.config": types.SimpleNamespace(Config=object),
         "botocore.exceptions": types.SimpleNamespace(ClientError=ClientError)}
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
       "IMAGE_PARAMETER": "/fleet/stage/image"}


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
        return {"services": [{"taskDefinition": self.arn, "desiredCount": 1, "runningCount": 1,
                              "pendingCount": 0, "deployments": [{"rolloutState":
                                  "FAILED" if self.fail_rollout and self.arn == NEW else "COMPLETED"}]}]}

    def describe_task_definition(self, **kwargs):
        return copy.deepcopy(self.old)

    def get_parameter(self, **kwargs):
        return {"Parameter": {"Value": self.image, "Version": self.version}}

    def put_parameter(self, **kwargs):
        assert self.lock, "pointer write outside the Terraform backend lock"
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


class Session:
    def __init__(self, aws):
        self.aws = aws

    def client(self, name, config=None):
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
