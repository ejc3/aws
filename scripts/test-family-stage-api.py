#!/usr/bin/env python3
"""Exercise stage releases with persisted receipts and simulated SDK failures."""
import copy
import importlib.util
import re
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release", ROOT / "family-stage-api/release.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
REPO = "example.dkr.ecr.us-west-1.amazonaws.com/family/api"
COMMIT, OLD = "a" * 40, "b" * 40
DIGEST, OLD_DIGEST = "sha256:" + "d" * 64, "sha256:" + "b" * 64


class StageRelease(unittest.TestCase):
    def setUp(self):
        self.tags = {COMMIT: DIGEST, OLD: OLD_DIGEST, "family-stage": OLD_DIGEST}
        self.record = {}
        self.deployment = "ecs-svc/100"
        self.rollout = "COMPLETED"
        self.running_image = REPO + ":family-stage"
        self.now = 1000
        self.delays = []
        self.event = {"commit": COMMIT, "digest": DIGEST, "run_number": 20}
        self.ecr, self.ecs, self.table = Mock(), Mock(), Mock()
        self.table.get_item.side_effect = self.read
        self.table.put_item.side_effect = self.write
        self.ecr.batch_get_image.side_effect = self.image
        self.ecr.put_image.side_effect = self.promote
        self.ecs.describe_services.side_effect = self.service
        self.ecs.describe_task_definition.side_effect = self.task
        self.ecs.update_service.side_effect = self.update

    def read(self, **args):
        self.assertEqual(args, {"Key": {"stage": "family"}, "ConsistentRead": True})
        return {"Item": copy.deepcopy(self.record)}

    def write(self, Item):
        self.record = copy.deepcopy(Item)
        return {}

    def image(self, repositoryName, imageIds):
        self.assertEqual(repositoryName, "family/api")
        tag = imageIds[0]["imageTag"]
        if tag not in self.tags:
            return {"images": [], "failures": [{"failureCode": "ImageNotFound"}]}
        return {"images": [{"imageId": {"imageTag": tag, "imageDigest": self.tags[tag]}, "imageManifest": tag,
                            "imageManifestMediaType": "application/vnd.oci.image.manifest.v1+json"}]}

    def promote(self, **args):
        self.assertEqual(args, {"repositoryName": "family/api", "imageTag": "family-stage", "imageDigest": DIGEST,
                                "imageManifest": COMMIT, "imageManifestMediaType": "application/vnd.oci.image.manifest.v1+json"})
        self.tags["family-stage"] = args["imageDigest"]
        return {}

    def service(self, cluster, services):
        self.assertEqual((cluster, services), ("family", ["family"]))
        return {"services": [{"taskDefinition": "family:7", "status": "ACTIVE", "desiredCount": 1,
                              "deployments": [{"id": self.deployment, "status": "PRIMARY", "rolloutState": self.rollout}]}]}

    def task(self, taskDefinition):
        self.assertEqual(taskDefinition, "family:7")
        return {"taskDefinition": {"family": "family", "containerDefinitions": [{"name": "api", "image": self.running_image}]}}

    def update(self, **args):
        self.assertEqual(args, {"cluster": "family", "service": "family", "forceNewDeployment": True})
        self.assertEqual(self.tags["family-stage"], DIGEST)
        self.assertEqual(self.record["baseline"], self.deployment)
        self.deployment = "ecs-svc/" + str(int(self.deployment.split("/")[1]) + 1)
        self.rollout = "IN_PROGRESS"
        return {"service": self.service("family", ["family"])["services"][0]}

    def wait(self, seconds):
        self.delays.append(seconds)
        self.now += seconds

    def invoke(self, event=None):
        return module.release(self.event if event is None else event, self.ecr, self.ecs, self.table, REPO,
                              now=lambda: self.now, sleep=self.wait)

    def test_commit_promoted_and_only_the_single_service_rolled(self):
        result = self.invoke()
        self.assertEqual(result, {**self.event, "deployment_id": self.deployment})
        self.assertEqual(self.record, {**result, "stage": "family"})
        self.assertEqual(self.invoke(), result)
        # Manual dispatch of the already-deployed commit still fences older runs.
        self.assertEqual(self.invoke({**self.event, "run_number": 22})["deployment_id"], self.deployment)
        with self.assertRaisesRegex(ValueError, "newer workflow"):
            self.invoke({**self.event, "run_number": 21})
        self.ecs.update_service.assert_called_once()
        self.ecr.put_image.assert_called_once()

    def test_crash_before_ecs_call_retries_and_crash_after_ecs_call_adopts_the_deployment(self):
        self.ecs.update_service.side_effect = TimeoutError("before ECS")
        with self.assertRaises(TimeoutError):
            self.invoke()
        self.assertEqual(self.record["baseline"], "ecs-svc/100")
        self.now += 301

        def lost_result(**args):
            self.update(**args)
            raise TimeoutError("lost acknowledgement")
        self.ecs.update_service.side_effect = lost_result
        with self.assertRaises(TimeoutError):
            self.invoke()
        self.ecs.update_service.reset_mock()
        self.assertEqual(self.invoke()["deployment_id"], "ecs-svc/101")
        self.ecs.update_service.assert_not_called()
        self.ecr.put_image.assert_called_once()

    def test_lost_response_and_a_stale_primary_are_reconciled_without_another_roll(self):
        stale = self.service("family", ["family"])
        def lost_reply(**args):
            self.update(**args)
            raise TimeoutError("accepted, response lost")
        self.ecs.update_service.side_effect = lost_reply
        with self.assertRaises(TimeoutError):
            self.invoke()
        fresh = self.service("family", ["family"])
        self.ecs.describe_services.side_effect = [stale, stale, fresh]
        self.assertEqual(self.invoke()["deployment_id"], "ecs-svc/101")
        self.ecs.update_service.assert_called_once()
        self.assertEqual(len(self.delays), 2)

    def test_ambiguous_roll_is_not_sent_again_on_stale_reads_inside_the_grace_period(self):
        self.ecs.update_service.side_effect = TimeoutError("no response")
        with self.assertRaises(TimeoutError):
            self.invoke()
        # A newer manual run for the same image must also reconcile the old submission.
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            self.invoke({**self.event, "run_number": 21})
        self.ecs.update_service.assert_called_once()
        self.assertEqual(self.record["run_number"], 21)
        self.assertTrue(self.delays)
        self.assertLess(sum(self.delays), 30)

    def test_retag_failure_does_not_roll_and_can_be_retried(self):
        self.ecr.put_image.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.invoke()
        self.ecs.update_service.assert_not_called()
        self.assertNotIn("deployment_id", self.record)
        self.ecr.put_image.side_effect = self.promote
        self.assertEqual(self.invoke()["deployment_id"], self.deployment)

    def test_wrong_digest_missing_source_and_missing_seed_do_not_roll_or_move_tag(self):
        with self.assertRaisesRegex(ValueError, "immutable commit tag"):
            self.invoke({**self.event, "digest": OLD_DIGEST})
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.invoke({**self.event, "commit": "e" * 40})
        del self.tags["family-stage"]
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.invoke()
        self.ecr.put_image.assert_not_called()
        self.ecs.update_service.assert_not_called()
        self.table.put_item.assert_not_called()

    def test_partial_cutover_or_disabled_service_fails_before_any_mutation(self):
        self.running_image = REPO + ":" + OLD
        with self.assertRaisesRegex(ValueError, "rolling tag"):
            self.invoke()
        self.ecs.describe_services.side_effect = None
        self.ecs.describe_services.return_value = {"services": [{"status": "ACTIVE", "desiredCount": 0}]}
        with self.assertRaisesRegex(ValueError, "one desired task"):
            self.invoke()
        self.ecr.put_image.assert_not_called()
        self.ecs.update_service.assert_not_called()
        self.table.put_item.assert_not_called()

    def test_receipt_rejects_reused_number_with_changed_image(self):
        self.invoke()
        with self.assertRaisesRegex(ValueError, "same workflow run"):
            self.invoke({**self.event, "commit": OLD, "digest": OLD_DIGEST})
        self.ecs.update_service.assert_called_once()

    def test_replay_of_an_acknowledged_run_cannot_roll_over_a_different_primary(self):
        initial = self.invoke()
        # ECS can briefly show the old primary after accepting UpdateService.
        self.deployment = "ecs-svc/100"
        self.assertEqual(self.invoke(), initial)
        self.ecs.update_service.assert_called_once()
        # If an operator moved the tag, a replay must not move it back.
        self.tags["family-stage"] = OLD_DIGEST
        with self.assertRaisesRegex(ValueError, "staging tag moved"):
            self.invoke()
        self.ecr.put_image.assert_called_once()

    def test_failed_rollout_can_be_retried_even_when_tag_matches(self):
        first = self.invoke()
        self.rollout = "FAILED"
        self.assertNotEqual(self.invoke()["deployment_id"], first["deployment_id"])
        self.assertEqual(self.ecs.update_service.call_count, 2)

    def test_invalid_contract_has_no_aws_calls(self):
        for override in ({"commit": "main"}, {"digest": "latest"}, {"run_number": -1}, {"run_number": True}, {"cluster": "prod"}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.invoke({**self.event, **override})
        self.table.get_item.assert_not_called()
        self.ecs.describe_services.assert_not_called()


class PermissionBoundary(unittest.TestCase):
    def test_ci_cannot_control_tasks_or_read_state_and_secrets(self):
        text = (ROOT / "family-stage-api.tf").read_text()
        trust = text.split('resource "aws_iam_role" "family_stage_api_ci" {')[1].split('\n}')[0]
        self.assertIn('aws_iam_openid_connect_provider.family_github.arn', trust)
        code = "\n".join(l for l in trust.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("StringLike", code)
        self.assertNotIn('"*"', code)
        # One statement per repository: its subject is admitted only with its own workflow file, on main.
        statements = code.split("Sid       = ")[1:]
        self.assertEqual(len(statements), 2)
        expected = {
            '"ConstellationDeployFamilyApi"': (
                "repo:ejc3@1694850/constellation@1427792887:environment:family-stage",
                "ejc3/constellation/.github/workflows/deploy-family-api.yml@refs/heads/main"),
            '"DolphinLabsFamilyDeployApi"': (
                "repo:dolphin-labs-hq@316189183/dolphin-labs@1330391842:environment:family-stage",
                "dolphin-labs-hq/dolphin-labs/.github/workflows/family-deploy-api.yml@refs/heads/main"),
        }
        for statement in statements:
            sid = statement.split("\n")[0].strip()
            sub, workflow = expected.pop(sid)
            conditions = dict(re.findall(r'"token\.actions\.githubusercontent\.com:(\w+)"\s*=\s*"([^"]+)"', statement))
            self.assertEqual(conditions, {"aud": "sts.amazonaws.com", "sub": sub, "ref": "refs/heads/main",
                                          "job_workflow_ref": workflow}, sid)
            self.assertEqual(statement.count("StringEquals"), 1)
        self.assertEqual(expected, {})
        policy = text.split('resource "aws_iam_role_policy" "family_stage_api_ci" {')[1].split('\n}')[0]
        self.assertEqual(set(re.findall(r'"([a-zA-Z0-9]+:[A-Za-z*]+)"', policy)), {
            "ecr:GetAuthorizationToken", "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload",
            "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "lambda:InvokeFunction"})
        self.assertIn('Resource = local.family_api_repository', policy)
        self.assertIn('Resource = aws_lambda_function.family_stage_api_release.arn', policy)
        self.assertRegex(policy, r'Action = "ecr:GetAuthorizationToken", Resource = "\*"')
        self.assertEqual(policy.count('Resource = "*"'), 1)
        self.assertRegex(text, r'reserved_concurrent_executions\s*= 1\b')


class GitHubEnvironments(unittest.TestCase):
    def test_family_stage_is_main_only_in_both_repositories_and_holds_the_role(self):
        text = (ROOT / "family-stage-github.tf").read_text()
        for name, provider, repository in (
            ("family_stage_api", "github.agents_md", '"constellation"'),
            ("dolphin_labs_family_stage_api", "github.dolphin_labs_ci", "local.dolphin_labs_ci_repository"),
        ):
            with self.subTest(environment=name):
                env = text.split(f'resource "github_repository_environment" "{name}" {{')[1].split("\n}")[0]
                self.assertRegex(env, rf"provider\s*= {re.escape(provider)}\n")
                self.assertRegex(env, rf"repository\s*= {re.escape(repository)}\n")
                self.assertRegex(env, r'environment\s*= "family-stage"')
                self.assertRegex(env, r"protected_branches\s*= false")
                self.assertRegex(env, r"custom_branch_policies\s*= true")
                policy = text.split(f'resource "github_repository_environment_deployment_policy" "{name}_main" {{')[1].split("\n}")[0]
                self.assertIn(f"github_repository_environment.{name}.environment", policy)
                self.assertRegex(policy, r'branch_pattern\s*= "main"')
                var = text.split(f'resource "github_actions_environment_variable" "{name}_role" {{')[1].split("\n}")[0]
                self.assertIn(f"github_repository_environment.{name}.environment", var)
                self.assertRegex(var, r'variable_name\s*= "FAMILY_STAGE_DEPLOY_ROLE_ARN"')
                self.assertRegex(var, r"value\s*= aws_iam_role\.family_stage_api_ci\.arn")

if __name__ == "__main__":
    unittest.main(buffer=True)
