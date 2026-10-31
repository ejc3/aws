#!/usr/bin/env python3
"""Offline contracts for release ownership, stale saved plans and image admission."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


def block(source, kind, name):
    match = re.search(r'\b' + re.escape(kind) + r'\s+"' + re.escape(name) + r'"\s*\{', source)
    if match is None:
        raise AssertionError("missing " + kind + " " + name)
    start, depth = match.end(), 1
    for index in range(start, len(source)):
        depth += (source[index] == "{") - (source[index] == "}")
        if depth == 0:
            return source[start:index]
    raise AssertionError("unterminated configuration block")


class ReleaseContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = (ROOT / "modules/fcvm-fleet-region/main.tf").read_text()
        cls.release = (ROOT / "fcvm-fleet-ami-release.tf").read_text()
        cls.fleet = (ROOT / "fcvm-fleet.tf").read_text()

    def test_each_template_rechecks_the_release_at_apply_time(self):
        guard = block(self.module, 'ephemeral "aws_ssm_parameter"', "ami_release_guard")
        self.assertIn("self.version == var.ami_release_parameter_version", guard)
        self.assertIn("with_decryption = false", guard)
        for name in ("host", "tile", "tile_on_demand", "tile_pool"):
            with self.subTest(template=name):
                template = block(self.module, 'resource "aws_launch_template"', name)
                self.assertRegex(template, r"depends_on\s*=\s*\[ephemeral\.aws_ssm_parameter\.ami_release_guard\]")
                self.assertRegex(template, r"update_default_version\s*=\s*true")
                self.assertNotIn("ignore_changes", template)

    def test_release_dependency_does_not_defer_region_topology(self):
        for module in ("fcvm_fleet_east1", "fcvm_fleet_east2", "fcvm_fleet_west2"):
            body = block(self.fleet, "module", module)
            self.assertNotIn("depends_on", body)
        east = block(self.fleet, "module", "fcvm_fleet_east1")
        self.assertIn("aws_ssm_parameter.fcvm_ami_release.arn", east)
        self.assertIn("data.aws_ssm_parameter.fcvm_ami_release[0].version", east)

    def test_bootstrap_pointer_has_no_launch_template_dependency_cycle(self):
        pointer = block(self.release, 'resource "aws_ssm_parameter"', "fcvm_ami_release")
        self.assertIn("provider", pointer)
        self.assertIn("aws.fcvm_fleet_east1", pointer)
        self.assertIn("var.fcvm_fleet_host_amis", pointer)
        self.assertIn("var.fcvm_fleet_tile_amis", pointer)
        self.assertNotIn("module.", pointer)
        self.assertRegex(pointer, r"ignore_changes\s*=\s*\[value\]")
        self.assertRegex(pointer, r"prevent_destroy\s*=\s*true")

    def test_active_catalog_requires_schema_and_reviewed_guard(self):
        data = block(self.release, 'data "aws_ssm_parameter"', "fcvm_ami_release")
        self.assertIn("try(jsondecode(self.value).version, 0) == 1", data)
        self.assertIn("try(jsondecode(self.value).guard_sha256", data)
        self.assertIn("cve-2026-98432-mitigation.py", data)
        self.assertIn("tag:FleetImageValidated", self.module)
        self.assertIn("owners   = [\"self\"]", self.module)

    def test_launch_permission_requires_every_validation_tag(self):
        policy = block(self.release, 'resource "aws_iam_policy"', "fcvm_ami_validated_images")
        for field in ("ec2:Owner", "ec2:Public", "ec2:ResourceTag/Purpose",
                      "ec2:ResourceTag/FleetImageKind", "ec2:ResourceTag/FleetImageValidated",
                      "ec2:ResourceTag/GuardHelperSHA256"):
            self.assertEqual(policy.count('"' + field + '"'), 2, field)
        self.assertNotIn("StringEqualsIfExists", policy)
        self.assertNotIn("AdministratorAccess", policy)
        self.assertIn('"ec2:RunInstances", "ec2:CreateFleet"', policy)
        self.assertIn("local.fcvm_fleet_host_template_condition", policy)
        self.assertIn("local.fcvm_fleet_tile_template_condition", policy)

    def test_automatic_mode_removes_the_old_exact_image_bypass(self):
        self.assertEqual(self.fleet.count("var.fcvm_ami_pipeline_enabled ? [] : flatten([for r, amis"), 2)
        self.assertEqual(len(re.findall(r"lookup\(local\.fcvm_active_(?:host|tile)_amis,", self.fleet)), 6)
        # The image policy is managed separately: the existing inline policy was
        # already within five bytes of IAM's 10 KiB aggregate role limit.
        self.assertIn('resource "aws_iam_role_policy_attachment" "fcvm_ami_validated_images"', self.release)

    def test_legacy_verified_images_are_never_claimed_sanitized(self):
        for name in ("fcvm_ami_bootstrap_kind", "fcvm_ami_bootstrap_validated"):
            body = block(self.release, 'resource "aws_ec2_tag"', name)
            self.assertNotIn('key         = "PipelineSanitized"', body)
        self.assertNotRegex(self.release, r'key\s*=\s*"PipelineSanitized"')


if __name__ == "__main__":
    unittest.main(buffer=True)
