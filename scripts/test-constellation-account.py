#!/usr/bin/env python3
"""Offline checks for constellation's member account (constellation-account.tf): a dev box applies inside that one account
only, through one exact role pinned to fcvm-arm's instance, and never gains a path to the organization or another member
account."""

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "constellation-account.tf").read_text()


def block(header):
    start = TF.index(header)
    depth = 0
    for i in range(TF.index("{", start), len(TF)):
        if TF[i] == "{":
            depth += 1
        elif TF[i] == "}":
            depth -= 1
            if depth == 0:
                return TF[start:i + 1]
    raise AssertionError(header)


class ConstellationAccountTests(unittest.TestCase):
    def test_the_account_is_never_closed_or_replaced_by_terraform(self):
        account = block('resource "aws_organizations_account" "constellation"')
        self.assertIn("close_on_deletion          = false", account)
        self.assertIn("prevent_destroy = true", account)
        self.assertIn("local.constellation_account_email", account)

    def test_the_apply_role_trusts_only_fcvm_arm(self):
        role = block('resource "aws_iam_role" "constellation_terraform"')
        self.assertIn("Principal = { AWS = aws_iam_role.dev_server.arn }", role)
        self.assertIn('"ec2:SourceInstanceARN" = local.constellation_applier_instance_arn', role)

    def test_the_pin_is_fcvm_arm_or_an_arn_no_instance_has(self):
        self.assertIn("try(aws_instance.firecracker_dev[0].arn, ", TF)
        self.assertIn(":instance/fcvm-arm-not-running", TF)

    def test_the_dev_box_grant_names_exactly_that_role(self):
        grant = block('resource "aws_iam_role_policy" "dev_server_constellation_terraform"')
        self.assertIn('Action    = "sts:AssumeRole"', grant)
        self.assertIn("Resource  = aws_iam_role.constellation_terraform.arn", grant)
        self.assertIn('"ec2:SourceInstanceARN" = local.constellation_applier_instance_arn', grant)

    def test_nothing_gives_a_dev_box_the_organization_or_other_accounts(self):
        # The options refused in constellation's own notes: organizations:CreateAccount for a dev box, and AssumeRole on
        # OrganizationAccountAccessRole in any account (admin of every member account).
        for role in ("aws_iam_role.dev_server", "aws_iam_role.nextjs_dev"):
            for m in re.finditer(r'resource "aws_iam_role_policy" "(\w+)" \{', TF):
                body = block(m.group(0)[:-2])
                if f"role = {role}.id" in body.replace("  ", " ") or f"role = {role}.id" in body:
                    self.assertNotIn("organizations:", body)
                    self.assertNotIn("OrganizationAccountAccessRole", body)
                    self.assertNotIn("arn:aws:iam::*", body)
        self.assertNotIn("organizations:CreateAccount", TF)

    def test_github_holds_no_aws_credential_for_this_account(self):
        # State and configuration reads can carry secret values; a same-repository pull request could read them.
        for forbidden in ("aws_iam_openid_connect_provider", "AssumeRoleWithWebIdentity", "token.actions.githubusercontent.com",
                          "github_actions_variable", "ReadOnlyAccess"):
            self.assertNotIn(forbidden, TF)

    def test_state_objects_are_for_the_appliers_only(self):
        policy = block('resource "aws_s3_bucket_policy" "constellation_terraform_state"')
        self.assertIn("StateObjectsForAppliersOnly", policy)
        self.assertIn('"s3:GetObject"', policy)
        self.assertIn("local.constellation_terraform_role_arn", policy)


if __name__ == "__main__":
    unittest.main()
