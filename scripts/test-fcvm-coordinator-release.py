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

    def put_item(self, **kwargs):
        if self.lock and kwargs.get("ConditionExpression") == "attribute_not_exists(LockID)":
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}})
        self.lock = kwargs["Item"]["Info"]["S"]
        self.calls.append("lock")

    def delete_item(self, **kwargs):
        if (kwargs.get("ConditionExpression") == "Info = :owner"
                and kwargs["ExpressionAttributeValues"][":owner"]["S"] != self.lock):
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}})
        self.lock = None
        self.calls.append("unlock")

    def describe_services(self, **kwargs):
        return {"services": [{"taskDefinition": self.arn, "desiredCount": 1, "runningCount": 1,
                              "pendingCount": 0, "deployments": [{"rolloutState":
                                  "FAILED" if self.fail_rollout and self.arn == NEW else "COMPLETED"}]}]}

    def describe_task_definition(self, **kwargs):
        return copy.deepcopy(self.old)

    def get_parameter(self, **kwargs):
        return {"Parameter": {"Value": self.image}}

    def put_parameter(self, **kwargs):
        assert self.lock, "pointer write outside the Terraform backend lock"
        self.image = kwargs["Value"]
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
        self.assertEqual(self.aws.calls, ["lock", "register", NEW, "pointer", "unlock"])
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

    def test_a_failed_rollout_restores_the_previous_task_and_pointer(self):
        self.aws.fail_rollout = True
        old = self.aws.image
        with self.assertRaisesRegex(RuntimeError, "rollout failed"):
            self.run_release()
        self.assertEqual(self.aws.arn, OLD)
        self.assertEqual(self.aws.image, old)
        self.assertEqual(self.aws.calls, ["lock", "register", NEW, OLD, "pointer", "deregister:" + NEW, "unlock"])

    def test_the_lock_delete_checks_the_unique_owner(self):
        real = self.aws.update_service
        def changed(**kwargs):
            real(**kwargs)
            self.aws.lock = "new owner"
        self.aws.update_service = changed
        with self.assertRaises(ClientError):
            self.run_release()
        self.assertEqual(self.aws.lock, "new owner")


if __name__ == "__main__":
    unittest.main(buffer=True)
