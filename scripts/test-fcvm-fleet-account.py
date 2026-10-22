#!/usr/bin/env python3
"""Offline tests for fcvm-fleet-account.tf: the fcvm-fleet member account (ejc3/aws#604) and its G spot quotas (#605)."""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "fcvm-fleet-account.tf").read_text()
CONST = (ROOT / "constellation-account.tf").read_text()
PEOPLE = (ROOT / "people.tf").read_text()


def block(header, text=TF):
    start = text.index(header)
    return text[start:text.index("\n}\n", start) + 2]


class AccountTests(unittest.TestCase):
    def test_the_account_is_never_closed_or_replaced_by_terraform(self):
        acct = block('resource "aws_organizations_account" "fcvm_fleet"')
        self.assertIn("close_on_deletion          = false", acct)
        self.assertIn("prevent_destroy = true", acct)
        self.assertIn("email                      = local.fcvm_fleet_account_email", acct)
        self.assertIn("parent_id                  = aws_organizations_organizational_unit.fcvm_fleet.id", acct)

    def test_its_root_address_comes_from_people_addresses(self):
        self.assertIn("fcvm_fleet_account_email    = nonsensitive(local.people.fcvm_fleet_account)", PEOPLE)
        self.assertIn("can(local.people.fcvm_fleet_account)", PEOPLE)
        self.assertNotRegex(TF, r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}")

    def test_one_provider_alias_per_fleet_region_through_the_access_role(self):
        self.assertIn('fcvm_fleet_regions         = ["us-east-2", "us-west-2", "us-east-1"]', TF)
        for alias, region in (("fcvm_fleet", "us-east-2"), ("fcvm_fleet_west2", "us-west-2"), ("fcvm_fleet_east1", "us-east-1")):
            prov = re.search(r'provider "aws" \{\n  alias  = "%s"\n  region = "%s"\n  assume_role \{\n    role_arn = "([^"]+)"' % (alias, region), TF)
            self.assertIsNotNone(prov, alias)
            self.assertTrue(prov.group(1).endswith(":role/OrganizationAccountAccessRole"))


class GuardrailTests(unittest.TestCase):
    def scp(self):
        return block("fcvm_fleet_scp = {")

    def test_regions_leave_and_iam_users_are_denied_on_the_ou_only(self):
        s = self.scp()
        self.assertIn('Condition = { StringNotEquals = { "aws:RequestedRegion" = local.fcvm_fleet_regions } }', s)
        self.assertIn("NotAction = local.dolphin_region_scp_global_actions", s)
        self.assertIn('Action   = "organizations:LeaveOrganization"', s)
        self.assertIn('"iam:CreateUser", "iam:CreateAccessKey", "iam:CreateLoginProfile", "iam:UpdateLoginProfile"', s)
        self.assertNotIn('"Allow"', s)
        att = block('resource "aws_organizations_policy_attachment" "fcvm_fleet_guardrails"')
        self.assertIn("target_id = aws_organizations_organizational_unit.fcvm_fleet.id", att)

    def test_nothing_here_trusts_the_fleet_account(self):
        # No principal in this repository names the fleet account except its own roles' providers and the state bucket.
        for f in ROOT.glob("*.tf"):
            if f.name == "fcvm-fleet-account.tf":
                continue
            text = f.read_text()
            self.assertNotIn("aws_organizations_account.fcvm_fleet", text, f.name)
            self.assertNotIn("local.fcvm_fleet_account_id", text, f.name)


class ApplyRoleTests(unittest.TestCase):
    def test_the_apply_role_trusts_only_fcvm_arm(self):
        role = block('resource "aws_iam_role" "fcvm_fleet_terraform"')
        self.assertEqual(role.count("Principal"), 1)
        self.assertIn("Principal = { AWS = aws_iam_role.dev_server.arn }", role)
        self.assertIn('"ec2:SourceInstanceARN" = local.constellation_applier_instance_arn', role)
        self.assertIn("max_session_duration = 3600", role)

    def test_the_dev_box_grant_names_exactly_that_role_and_the_pointer(self):
        grant = block('resource "aws_iam_policy" "dev_server_constellation_terraform"', CONST)
        for sid, res in (("AssumeTheFcvmFleetApplyRoleOnly", "aws_iam_role.fcvm_fleet_terraform.arn"),
                         ("ReadTheFcvmFleetPointerOnly", "aws_ssm_parameter.fcvm_fleet.arn")):
            stmt = grant.split(f'Sid       = "{sid}"', 1)[1].split("}", 2)[0] + grant.split(f'Sid       = "{sid}"', 1)[1].split("}", 2)[1]
            self.assertIn(f"Resource  = {res}", stmt, sid)
            self.assertIn('"ec2:SourceInstanceARN" = local.constellation_applier_instance_arn', stmt, sid)

    def test_state_objects_are_for_the_appliers_only(self):
        pol = block('resource "aws_s3_bucket_policy" "fcvm_fleet_terraform_state"')
        self.assertIn("StateObjectsForAppliersOnly", pol)
        self.assertIn("[local.fcvm_fleet_terraform_role, local.fcvm_fleet_access_role_arn]", pol)
        self.assertIn('status = "Enabled"', block('resource "aws_s3_bucket_versioning" "fcvm_fleet_terraform_state"'))
        self.assertIn("prevent_destroy = true", block('resource "aws_s3_bucket" "fcvm_fleet_terraform_state"'))

    def test_the_pointer_holds_no_credential(self):
        param = block('resource "aws_ssm_parameter" "fcvm_fleet"')
        self.assertIn('name        = "/infra/fcvm-fleet"', param)
        self.assertIn('type        = "String"', param)
        for leak in ("secret", "token", "password", "key_id"):
            self.assertNotIn(leak, param.lower().replace("no credentials", ""))


class QuotaTests(unittest.TestCase):
    def test_g_spot_is_192_in_each_fleet_region_and_never_lowered(self):
        self.assertIn("fcvm_fleet_spot_g_vcpus = 192", TF)
        for suffix, alias in (("east2", "fcvm_fleet"), ("west2", "fcvm_fleet_west2"), ("east1", "fcvm_fleet_east1")):
            q = block(f'resource "aws_servicequotas_service_quota" "fcvm_fleet_spot_g_{suffix}"')
            self.assertIn(f"provider     = aws.{alias}", q)
            self.assertIn('quota_code   = "L-3819A6DF"', q)
            self.assertIn(f"max(local.fcvm_fleet_spot_g_vcpus, data.aws_servicequotas_service_quota.fcvm_fleet_spot_g_{suffix}.value)", q)


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)
