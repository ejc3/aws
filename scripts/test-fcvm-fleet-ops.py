#!/usr/bin/env python3
"""Fleet budget IAM, operator authentication and CLI commands. Offline."""

import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parent
loader = importlib.machinery.SourceFileLoader("ops", str(SCRIPTS / "fcvm-fleet-ops"))
spec = importlib.util.spec_from_loader(loader.name, loader)
ops = importlib.util.module_from_spec(spec)
loader.exec_module(ops)
ACCOUNT = "1" * 12
FLEET = {
    "account_id": ACCOUNT,
    "home_region": "us-east-2",
    "terraform_role": f"arn:aws:iam::{ACCOUNT}:role/fcvm-fleet-terraform",
    "state_bucket": f"fcvm-fleet-terraform-state-{ACCOUNT}",
}


class SessionTests(unittest.TestCase):
    def test_instance_profile_discovers_and_assumes_only_the_fleet_role(self):
        calls = []

        def aws(env, region, service, op, *args):
            calls.append((region, service, op, args))
            self.assertNotIn("AWS_PROFILE", env)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
            self.assertEqual(env["AWS_SHARED_CREDENTIALS_FILE"], os.devnull)
            if service == "ssm":
                return {"Parameter": {"Value": json.dumps(FLEET)}}
            return {"Credentials": {"AccessKeyId": "a", "SecretAccessKey": "s", "SessionToken": "t"}}

        with (
            patch.dict(os.environ, AWS_PROFILE="different", AWS_SECRET_ACCESS_KEY="other"),
            patch.object(ops, "aws", aws),
        ):
            pointer, env = ops.fleet_session()
        self.assertEqual(pointer, FLEET)
        self.assertEqual(calls[0], ("us-west-1", "ssm", "get-parameter", ("--name", "/infra/fcvm-fleet")))
        self.assertEqual(
            calls[1],
            (
                "us-east-2",
                "sts",
                "assume-role",
                (
                    "--role-arn",
                    FLEET["terraform_role"],
                    "--role-session-name",
                    "fcvm-fleet-ops",
                    "--duration-seconds",
                    "3600",
                ),
            ),
        )
        self.assertEqual(env["AWS_SESSION_TOKEN"], "t")

    def test_unexpected_role_or_bucket_is_rejected_before_assume_role(self):
        for key, value in (
            ("terraform_role", "other"),
            ("state_bucket", "other"),
            ("home_region", "us-east-1"),
        ):
            with (
                self.subTest(key=key),
                patch.object(
                    ops, "aws", return_value={"Parameter": {"Value": json.dumps(dict(FLEET, **{key: value}))}}
                ) as aws,
            ):
                with self.assertRaises(ValueError):
                    ops.fleet_session()
                aws.assert_called_once()


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.marker = b"off"
        self.writes = []
        self.race = False

    def s3(self, env, region, service, op, *args):
        self.assertEqual((region, service), ("us-east-2", "s3api"))
        self.assertEqual(
            args[:6],
            (
                "--bucket",
                f"fcvm-fleet-coordinator-state-{ACCOUNT}",
                "--key",
                "drills/prod-budget-day",
                "--expected-bucket-owner",
                ACCOUNT,
            ),
        )
        if op == "head-object":
            return {"ContentLength": len(self.marker), "ETag": '"etag"'}
        self.assertEqual(args[6:8], ("--if-match", '"etag"'))
        if op == "get-object":
            Path(args[-1]).write_bytes(self.marker)
        else:
            self.assertEqual(op, "put-object")
            self.assertEqual(args[8:11], ("--content-type", "text/plain", "--body"))
            if self.race:
                raise subprocess.CalledProcessError(254, ["aws"])
            self.marker = Path(args[-1]).read_bytes()
            self.writes.append(self.marker)
        return {}

    def test_read_and_change_the_utc_selector_idempotently(self):
        with patch.object(ops, "aws", self.s3):
            self.assertFalse(ops.budget("status", FLEET, {})["override_active_today"])
            self.assertFalse(self.writes)
            trip = ops.budget("trip", FLEET, {})
            self.assertEqual(trip["selected"], dt.datetime.now(dt.timezone.utc).date().isoformat())
            self.assertTrue(trip["override_active_today"])
            ops.budget("trip", FLEET, {})
            self.assertTrue(ops.budget("status", FLEET, {})["override_active_today"])
            self.assertEqual(ops.budget("restore", FLEET, {})["selected"], "off")
            self.assertEqual(self.writes, [trip["selected"].encode(), b"off"])

    def test_stale_and_malformed_markers_and_concurrent_update(self):
        with patch.object(ops, "aws", self.s3):
            self.marker = b"1999-12-31"
            self.assertFalse(ops.budget("status", FLEET, {})["override_active_today"])
            for bad in (b"<script>", b"2000-99-99", b"\xfe\xfe\xff"):
                self.marker = bad
                self.assertEqual(ops.budget("status", FLEET, {})["selected"], "invalid")
            self.race = True
            with self.assertRaises(subprocess.CalledProcessError):
                ops.budget("restore", FLEET, {})
            self.assertFalse(self.writes)

    def test_quota_reads_only_g_spot_in_us_east_1(self):
        with patch.object(
            ops, "aws", side_effect=[480, [{"status": "CASE_CLOSED", "requested_vcpus": 480}]]
        ) as aws:
            result = ops.quota({})
        self.assertEqual(result["applied_vcpus"], 480)
        self.assertEqual(result["requests"][0]["status"], "CASE_CLOSED")
        self.assertEqual(
            [c.args[3] for c in aws.call_args_list],
            ["get-service-quota", "list-requested-service-quota-change-history-by-quota"],
        )
        for call in aws.call_args_list:
            self.assertEqual(call.args[1:3], ("us-east-1", "service-quotas"))
            self.assertEqual(call.args[4:8], tuple(ops.G_SPOT))

    def test_terraform_uses_project_root_backend_and_only_saved_plan_for_apply(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            root = home / "fcvm" / "fleet/ops/terraform"
            root.mkdir(parents=True)
            (root / "main.tf").touch()
            (home / "bin").mkdir()
            (home / "bin" / "terraform").touch()
            with (
                patch.object(Path, "home", return_value=home),
                patch.dict(os.environ, FCVM_REPO=str(home / "fcvm")),
                patch.object(ops.subprocess, "check_output", return_value=b'{"terraform_version":"1.10.3"}'),
                patch.object(ops.subprocess, "call", return_value=0) as run,
            ):
                ops.terraform(
                    "init",
                    [],
                    FLEET,
                    {
                        "AWS_SESSION_TOKEN": "test",
                        "TF_WORKSPACE": "another",
                        "TF_CLI_ARGS_apply": "-auto-approve",
                        "TF_DATA_DIR": "/other",
                    },
                )
                args = run.call_args.args[0]
                self.assertEqual(args[:3], [str(home / "bin" / "terraform"), "-chdir=" + str(root), "init"])
                self.assertIn("-backend-config=bucket=" + FLEET["state_bucket"], args)
                self.assertFalse(any("test" in x for x in args))
                self.assertEqual(
                    run.call_args.kwargs["env"],
                    {
                        "TF_VAR_fleet_account_id": ACCOUNT,
                        "TF_WORKSPACE": "default",
                        "AWS_SESSION_TOKEN": "test",
                    },
                )
                for args in ([], ["-auto-approve"], ["missing.tfplan"]):
                    with self.assertRaises(ValueError):
                        ops.terraform("apply", args, FLEET, {})
                (root / "ops.tfplan").touch()
                ops.terraform("apply", ["ops.tfplan"], FLEET, {})
                self.assertEqual(run.call_args.args[0][-2:], ["apply", "ops.tfplan"])


class PermissionsTests(unittest.TestCase):
    def test_marker_and_role_grants_only(self):
        text = (SCRIPTS.parent / "fcvm-fleet-prod-drills.tf").read_text()

        def block(kind, name):
            return re.search(rf'^resource "{kind}" "{name}" \{{.*?^\}}', text, re.M | re.S).group()

        marker = block("aws_s3_object", "fcvm_fleet_prod_budget_day")
        self.assertIn('key          = "drills/prod-budget-day"', marker)
        self.assertIn('content      = "off"', marker)
        self.assertRegex(marker, r"ignore_changes\s*= all")
        self.assertIn("aws_s3_bucket.fcvm_fleet_coordinator_state.id", marker)
        for name, role, actions in (
            ("fcvm_fleet_operate_budget_drill", "fcvm_fleet_terraform.id", ["s3:GetObject", "s3:PutObject"]),
            ("fcvm_fleet_read_budget_drill", "fcvm_fleet_prod_coordinator_task[0].id", ["s3:GetObject"]),
        ):
            grant = block("aws_iam_role_policy", name)
            self.assertIn("role     = aws_iam_role." + role, grant)
            self.assertEqual(re.findall(r'"(s3:[^"]+)"', grant), actions)
            self.assertEqual(
                re.findall(r'Resource\s*= "(.*?)"', grant),
                [
                    "${aws_s3_bucket.fcvm_fleet_coordinator_state.arn}/${aws_s3_object.fcvm_fleet_prod_budget_day[0].key}"
                ],
            )
            self.assertIn("local.fcvm_fleet_prod", grant)


if __name__ == "__main__":
    unittest.main(buffer=True)
