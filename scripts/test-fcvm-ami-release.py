#!/usr/bin/env python3
"""Offline contracts for release ownership, stale saved plans and image admission."""
from pathlib import Path
import re
import sys
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
                ignored = re.findall(r"\bignore_changes\s*=\s*([^\n]+)", template)
                self.assertEqual([value.strip() for value in ignored], ["[description]"])

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

    def test_only_nonsecret_image_maps_are_declassified_for_resource_keys(self):
        for kind in ("host", "tile"):
            self.assertIn("nonsensitive(jsondecode(data.aws_ssm_parameter.fcvm_ami_release[0].value)."
                          + kind + "_amis)", self.release)
        self.assertNotIn("nonsensitive(data.aws_ssm_parameter.fcvm_ami_release[0].value)", self.release)

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

    def test_legacy_image_authorization_survives_default_pair_updates(self):
        catalog = re.search(r"fcvm_ami_bootstrap\s*=\s*\{(.*?)\n  \}", self.release, re.S)
        self.assertIsNotNone(catalog, "legacy image tags must have an explicit additive catalog")
        self.assertNotIn("var.", catalog.group(1))
        for key, image, kind in (
            ("host/x86_64", "ami-00fb7a2e8b89e2446", "host"),
            ("tile/x86_64", "ami-0edfc91ca2e651db9", "tile"),
            ("host/x86_64/38d63412", "ami-0d94bb60fbf180b7f", "host"),
            ("tile/x86_64/2f861183", "ami-0a9804d3fc86cd87e", "tile"),
            ("host/x86_64/2414e101", "ami-05234f17eb73f6a34", "host"),
            ("tile/x86_64/2414e101", "ami-0a76a813745a28952", "tile"),
            ("host/x86_64/46f2d8bc", "ami-0653ffbb5ec723d2f", "host"),
            ("tile/x86_64/46f2d8bc", "ami-00578e249371ea9fe", "tile"),
            ("host/x86_64/a70ad25c", "ami-0129863cf5498d43b", "host"),
            ("tile/x86_64/a70ad25c", "ami-05dd36badf188758b", "tile"),
            ("host/x86_64/a5bae67b", "ami-051db4be56ae7f6b0", "host"),
            ("tile/x86_64/a5bae67b", "ami-0ce935fe1f66fb10f", "tile"),
            ("host/x86_64/04915961", "ami-0e7a6e72f1dc8f4f4", "host"),
            ("host/x86_64/3b597cf9", "ami-017c2fffe2dd0c176", "host"),
            ("tile/x86_64/3b597cf9", "ami-058b6cec0b53ddf4d", "tile"),
            ("host/x86_64/95217caa", "ami-0620224a0a4dd168e", "host"),
            ("host/x86_64/a4a0bd3e", "ami-04c3294f21cb88942", "host"),
        ):
            with self.subTest(resource_key=key):
                self.assertRegex(catalog.group(1), r'"' + re.escape(key)
                                 + r'"\s*=\s*\{\s*image\s*=\s*"' + image
                                 + r'",\s*kind\s*=\s*"' + kind + r'"\s*\}')

    def test_published_images_are_copied_out_of_the_staging_region_only_as_a_record(self):
        pipeline = (ROOT / "fcvm-fleet-ami-pipeline.tf").read_text()
        regions = re.search(r'fcvm_ami_copy_regions\s*=\s*\[([^\]]*)\]', self.release)
        self.assertIsNotNone(regions)
        self.assertEqual(re.findall(r'"([a-z0-9-]+)"', regions.group(1)), ["us-west-2"])
        record = block(self.release, 'resource "aws_ssm_parameter"', "fcvm_ami_copies")
        self.assertIn('"${aws_ssm_parameter.fcvm_ami_release.name}-copies"', record)
        self.assertRegex(record, r"ignore_changes\s*=\s*\[value\]")
        # The copies never enter the release's image maps, which make launch templates.
        pointer = block(self.release, 'resource "aws_ssm_parameter"', "fcvm_ami_release")
        self.assertNotIn("copy", pointer)
        function = block(pipeline, 'resource "aws_lambda_function"', "fcvm_ami_publish")
        self.assertIn('COPY_REGIONS = join(",", local.fcvm_ami_copy_regions)', function)
        policy = block(pipeline, 'resource "aws_iam_role_policy"', "fcvm_ami_publish_fleet")
        self.assertIn("aws_ssm_parameter.fcvm_ami_copies.arn", policy)
        copy = policy[policy.index('"CopyPublishedImagesOnly"'):policy.index('"CopyImageTagsOnCreate"')]
        self.assertIn('"aws:RequestedRegion" = local.fcvm_ami_release_regions', copy)
        tags = policy[policy.index('"CopyImageTagsOnCreate"'):]
        tags = tags[:tags.index("} },")]
        self.assertIn('"ec2:CreateAction" = "CopyImage"', tags)
    def test_the_factory_builds_in_us_west_2_and_releases_in_us_east_1(self):
        pipeline = (ROOT / "fcvm-fleet-ami-pipeline.tf").read_text()
        self.assertRegex(pipeline, r'fcvm_ami_region\s*=\s*"us-east-1"')
        self.assertRegex(pipeline, r'fcvm_ami_build_region\s*=\s*"us-west-2"')
        for kind, name in (('data "aws_ami"', "fcvm_ami_canonical"), ('resource "aws_security_group"', "fcvm_ami_builder"),
                           ('resource "aws_ssm_document"', "fcvm_ami_build"),
                           ('data "aws_ec2_instance_type_offerings"', "fcvm_ami_builder")):
            with self.subTest(resource=name):
                resource = block(pipeline, kind, name)
                self.assertIn("aws.fcvm_fleet_west2", resource)
                if kind.startswith("resource"):
                    self.assertRegex(resource, r'(?m)^  region\s*=\s*local\.fcvm_ami_build_region\s*$')
        dispatch = block(pipeline, 'resource "aws_lambda_function"', "fcvm_ami_dispatch")
        self.assertRegex(dispatch, r"COMPUTE_REGION\s*=\s*local\.fcvm_ami_build_region")
        self.assertRegex(dispatch, r"SUBNET_ID\s*=\s*local\.fcvm_ami_builder_subnet")
        self.assertRegex(dispatch, r"RELEASE_REGION\s*=\s*local\.fcvm_ami_region\b")
        self.assertIn("module.fcvm_fleet_west2.subnet_ids_by_az[local.fcvm_ami_build_az]", pipeline)
        publish = block(pipeline, 'resource "aws_lambda_function"', "fcvm_ami_publish")
        self.assertRegex(publish, r"COMPUTE_REGION\s*=\s*local\.fcvm_ami_region\b")
        self.assertRegex(publish, r"BUILD_REGION\s*=\s*local\.fcvm_ami_build_region")
        project = block(pipeline, 'resource "aws_codebuild_project"', "fcvm_ami")
        self.assertRegex(project, r'"FLEET_AMI_REGION"\s*value\s*=\s*local\.fcvm_ami_build_region')
        # The TLS secrets stay in us-east-1, where they are.
        self.assertRegex(project, r'"FLEET_AMI_SECRET_REGION"\s*value\s*=\s*local\.fcvm_ami_region\b')
        self.assertIn("aws.fcvm_fleet_east1", block(pipeline, 'resource "aws_secretsmanager_secret"', "fcvm_ami_tls"))
        # Retirement acts on the staging fleet's own hosts, in the release region.
        fleet = block(pipeline, 'resource "aws_iam_role_policy"', "fcvm_ami_publish_fleet")
        self.assertIn('"ec2:TerminateInstances", Resource = "arn:aws:ec2:${local.fcvm_ami_region}:', fleet)
    def test_publisher_environment_stays_far_below_lambdas_limit(self):
        # Lambda refuses an environment over 4 KB (its JSON form). The release configuration grew
        # it to an estimated 4,020 bytes, one variable away from a failed apply; it now ships in
        # the publisher's bundle. Every remaining value is bounded here, and the worst case must
        # stay within half the limit. An expression this table does not know fails the test.
        sources = "\n".join(path.read_text() for path in ROOT.glob("*.tf"))
        def literal(name):
            found = re.findall(r"^\s*" + re.escape(name) + r'\s*=\s*"([^"$]*)"\s*$', sources, re.M)
            self.assertEqual(len(found), 1, name)
            return len(found[0])
        pipeline = (ROOT / "fcvm-fleet-ami-pipeline.tf").read_text()
        function = block(pipeline, 'resource "aws_lambda_function"', "fcvm_ami_publish")
        variables = function[function.index("variables = {"):]
        pairs = re.findall(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(.+?)\s*$", variables, re.M)
        copy_regions = re.search(r"fcvm_ami_copy_regions\s*=\s*\[([^\]]*)\]", self.release).group(1).count('"') // 2
        bounds = [
            (r'"[^"$]*"', lambda v: len(v) - 2),
            (r"local\.fcvm_fleet_account_id", lambda v: 12),
            (r"local\.fcvm_ami_guard_sha", lambda v: 64),
            (r"local\.(fcvm_[a-z0-9_]+)", lambda v: literal(v.split(".", 1)[1])),
            (r"var\.aws_region", lambda v: 14),
            (r"data\.aws_caller_identity\.current\.account_id", lambda v: 12),
            (r"aws_[a-z0-9_]+\.[a-z0-9_]+\.arn", lambda v: 256),
            (r"aws_s3_bucket\.[a-z0-9_]+\.id", lambda v: 63),
            (r"aws_ssm_parameter\.[a-z0-9_]+\.name", lambda v: 128),
            (r"module\.[a-z0-9_]+\.host_user_data_sha256", lambda v: 64),
            (r'join\(",", local\.fcvm_ami_copy_regions\)', lambda v: 15 * copy_regions),
        ]
        total = 2
        for key, value in pairs:
            with self.subTest(variable=key):
                self.assertNotIn("jsonencode", value, "bulky configuration belongs in the bundle")
                for pattern, bound in bounds:
                    if re.fullmatch(pattern, value):
                        total += len(key) + 6 + bound(value)
                        break
                else:
                    self.fail(f"no size bound for {key} = {value}")
        self.assertGreater(len(pairs), 15)
        print(f"worst-case publisher environment: {total} bytes", file=sys.stderr)
        self.assertLessEqual(total, 2048, f"worst-case publisher environment {total} bytes")
        bundle = block(pipeline, 'data "archive_file"', "fcvm_ami_publish")
        for key in ("TEMPLATES_JSON", "SEED_RELEASE_JSON", "PHONE_AMI_JSON", "RENDERER_AMI_JSON", "POTATO_AMI_JSON", "HOST_FIX_AMI_JSON"):
            self.assertIn(key + " ", bundle)
        self.assertIn('filename = "publisher-config.json"', bundle)

if __name__ == "__main__":
    unittest.main(buffer=True)
