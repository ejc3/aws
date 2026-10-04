#!/usr/bin/env python3
"""The parallel boxes' move to us-east-2 (ohio-pbox.tf): both sites wired, IAM follows the sites, the data and the
NFS path are protected, and `pbox` reads its region from Terraform rather than a hardcoded line."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OHIO = (ROOT / "ohio-pbox.tf").read_text()
LAUNCH = (ROOT / "parallel-box-launch.tf").read_text()
SCRIPT = (ROOT / "scripts" / "parallel-box.sh").read_text()
IO = (ROOT / "io-box.tf").read_text()
STATUS = (ROOT / "applied-status.tf").read_text()


def block(text, header):
    m = re.search(re.escape(header) + r" \{\n.*?\n\}\n", text, re.S)
    assert m, header
    return m.group(0)


class PointerTests(unittest.TestCase):
    def test_the_region_is_one_of_the_two_sites(self):
        self.assertRegex(OHIO, r'\n  parallel_box_region = "(us-west-2|us-east-2)"\n')

    def test_the_pointer_is_published_for_the_dev_boxes_and_they_may_read_it(self):
        self.assertIn('name        = "/infra/parallel-box"', OHIO)
        self.assertIn("jsonencode({ region = local.parallel_box_region })", OHIO)
        self.assertIn("aws_ssm_parameter.parallel_box_region.arn", STATUS)
        # read only, and only that one parameter
        stmt = re.search(r'Sid\s+= "ReadParallelBoxRegion".*?\n    \},', STATUS, re.S).group(0)
        self.assertIn('Action   = ["ssm:GetParameter"]', stmt)

    def test_the_script_reads_the_pointer_and_hardcodes_no_region(self):
        self.assertNotRegex(SCRIPT, r'(?m)^REGION="us-')
        self.assertIn("--name /infra/parallel-box", SCRIPT)
        self.assertIn('PARALLEL_BOX_REGION', SCRIPT)
        # fail closed: no region is an error, never a silent default
        self.assertRegex(SCRIPT, r'if \[ -z "\$REGION" \]; then\n\s+say "FATAL')
        self.assertLess(SCRIPT.index("say() {"), SCRIPT.index("--name /infra/parallel-box"))


class SiteTests(unittest.TestCase):
    def test_both_sites_carry_everything_regional(self):
        sites = re.search(r"parallel_box_sites = \{.*?\n  \}\n", LAUNCH, re.S).group(0)
        for region in ("us-west-2", "us-east-2"):
            self.assertIn('"%s" = {' % region, sites)
        for field in ("ami", "subnet_id", "security_group_id", "key_name", "volumes"):
            self.assertEqual(sites.count(field + " "), 2, field)

    def test_no_launch_policy_pins_a_literal_region(self):
        policy = re.search(r'data "aws_iam_policy_document" "parallel_box_control" \{.*?\n\}\n', LAUNCH, re.S).group(0)
        self.assertNotIn("arn:aws:ec2:us-west-2", policy)
        self.assertNotIn("arn:aws:ec2:us-east-2", policy)
        for local in ("parallel_box_instance_arns", "parallel_box_eni_arns", "parallel_box_new_volume_arns",
                      "parallel_box_referenced_arns", "parallel_box_template_arns", "parallel_box_volume_arns"):
            self.assertIn("local." + local, policy, local)

    def test_both_regions_templates_are_pinned_in_the_policy(self):
        m = re.search(r"parallel_box_template_arns = concat\((.*?)\n  \)", LAUNCH, re.S).group(1)
        self.assertIn("aws_launch_template.parallel_box :", m)
        self.assertIn("aws_launch_template.parallel_box_ohio :", m)
        self.assertEqual(LAUNCH.count("values   = local.parallel_box_template_arns"), 2)

    def test_the_ohio_template_is_the_west_one_with_only_regional_values_changed(self):
        west = block(LAUNCH, 'resource "aws_launch_template" "parallel_box"')
        ohio = block(LAUNCH, 'resource "aws_launch_template" "parallel_box_ohio"')
        norm = [
            ('"parallel_box_ohio"', '"parallel_box"'),
            ("provider = aws.ohio", "provider = aws.west2"),
            ("var.parallel_box_ami_ohio", "var.parallel_box_ami"),
            ("aws_key_pair.ohio_pbox.key_name", "aws_key_pair.parallel_box.key_name"),
            ("aws_security_group.ohio_pbox.id", "aws_security_group.parallel_box.id"),
            ('us-east-2/', "us-west-2/"),
        ]
        for a, b in norm:
            ohio = ohio.replace(a, b)
        ohio = re.sub(r"subnet_id\s+= aws_subnet\.ohio_pbox\.id[^\n]*", "SUBNET", ohio)
        west = re.sub(r'subnet_id\s+= "subnet-[0-9a-f]+"[^\n]*', "SUBNET", west)
        ohio = re.sub(r"# Ubuntu 24.04 arm64, us-east-2", "# Ubuntu 24.04 arm64, us-west-2", ohio)
        self.assertEqual(west, ohio)

    def test_one_boot_script_serves_both_regions_and_never_formats_a_disk_with_data(self):
        self.assertEqual(LAUNCH.count("mkfs.ext4"), 1)
        self.assertIn("mkfs.ext4 -L parallel-work", LAUNCH)
        self.assertIn('user_data = local.parallel_box_user_data["us-west-2/${each.key}"]', LAUNCH)
        self.assertIn('user_data = local.parallel_box_user_data["us-east-2/${each.key}"]', LAUNCH)
        self.assertNotIn("each.value.volume_id", LAUNCH)


class DataTests(unittest.TestCase):
    def test_each_volume_is_restored_from_a_copied_snapshot_and_protected(self):
        vol = block(OHIO, 'resource "aws_ebs_volume" "ohio_parallel_work"')
        self.assertIn("snapshot_id       = aws_ebs_snapshot_copy.pbox_move[each.key].id", vol)
        self.assertIn("prevent_destroy = true", vol)
        self.assertIn("ignore_changes  = [snapshot_id]", vol)
        self.assertIn("encrypted         = true", vol)
        self.assertIn("availability_zone = local.ohio_pbox_az", vol)
        copy = block(OHIO, 'resource "aws_ebs_snapshot_copy" "pbox_move"')
        self.assertIn('source_region      = "us-west-2"', copy)
        self.assertIn("encrypted          = true", copy)

    def test_names_and_sizes_match_the_originals_so_the_script_finds_them_by_tag(self):
        self.assertIn('size = 300, name = "parallel-box-work"', OHIO)
        self.assertIn('size = 100, name = "parallel-box-2-work"', OHIO)
        self.assertIn("size              = 300", (ROOT / "parallel-box.tf").read_text())
        self.assertIn("size              = 100", (ROOT / "parallel-box2.tf").read_text())
        self.assertIn('Name    = "parallel-box-work"', (ROOT / "parallel-box.tf").read_text())

    def test_the_old_volumes_keep_their_protection(self):
        for f in ("parallel-box.tf", "parallel-box2.tf"):
            self.assertIn("prevent_destroy = true", (ROOT / f).read_text(), f)


class LiveCopyTests(unittest.TestCase):
    """The Ohio volumes are copies: only one region's is live, and the script refuses the other (a flip back must not
    silently fork the work disk onto stale data)."""

    def test_each_volume_carries_a_live_tag_that_follows_the_pointer(self):
        west1 = block((ROOT / "parallel-box.tf").read_text(), 'resource "aws_ebs_volume" "parallel_work"')
        west2 = block((ROOT / "parallel-box2.tf").read_text(), 'resource "aws_ebs_volume" "parallel_work_2"')
        for west in (west1, west2):
            self.assertIn('Live = local.parallel_box_region == "us-west-2" ? "true" : "false"', west)
        ohio = block(OHIO, 'resource "aws_ebs_volume" "ohio_parallel_work"')
        self.assertIn('Live = local.parallel_box_region == "us-east-2" ? "true" : "false"', ohio)

    def test_pbox_up_refuses_a_stale_volume_unless_told_otherwise(self):
        up = SCRIPT[SCRIPT.index("  up)"):SCRIPT.index("  down)")]
        self.assertIn("Key==`Live`", up)
        self.assertIn('[ "$LIVE" = "false" ] && [ "${PARALLEL_BOX_ALLOW_STALE:-}" != "1" ]', up)
        refuse = up[up.index('[ "$LIVE" = "false" ]'):]
        self.assertLess(refuse.index("exit 1"), refuse.index("Spot capacity"))
        # the check happens before any instance is launched
        self.assertLess(up.index("Key==`Live`"), up.index("run-instances"))


class NetworkTests(unittest.TestCase):
    def test_the_nfs_path_is_one_subnet_never_a_vpc(self):
        route = block(OHIO, 'resource "aws_route" "ohio_pbox_to_io_box"')
        self.assertIn("data.aws_subnet.io_box.cidr_block", route)
        self.assertNotIn("172.31.0.0/16", route)
        self.assertIn("[aws_subnet.ohio_pbox.cidr_block]", IO)
        self.assertNotIn("aws_vpc.ohio_pbox.cidr_block", re.search(r"io_box_nfs_client_cidrs = concat\(.*?\n  \)", IO, re.S).group(0))
        back = block(OHIO, 'resource "aws_route" "west2_to_ohio_pbox"')
        self.assertIn("aws_vpc.ohio_pbox.cidr_block", back)

    def test_the_route_table_does_not_fight_its_separate_peering_route(self):
        table = block(OHIO, 'resource "aws_route_table" "ohio_pbox"')
        self.assertIn("ignore_changes = [route]", table)
        self.assertNotIn("vpc_peering_connection_id", table)

    def test_peering_is_accepted_before_a_route_uses_it(self):
        for name in ("ohio_pbox_to_io_box", "west2_to_ohio_pbox"):
            self.assertIn("depends_on = [aws_vpc_peering_connection_accepter.ohio_pbox_io]",
                          block(OHIO, 'resource "aws_route" "%s"' % name))

    def test_the_vpc_cidr_is_used_nowhere_else_and_is_logged(self):
        self.assertIn('cidr_block                       = "10.12.0.0/16"', OHIO)
        hits = [p.name for p in ROOT.glob("*.tf") if "10.12.0.0/16" in p.read_text() and p.name != "ohio-pbox.tf"]
        self.assertEqual(hits, [])
        self.assertIn('resource "aws_flow_log" "security_ohio_pbox"', OHIO)

    def test_ssh_only_and_ipv6_egress_like_the_original(self):
        sg = block(OHIO, 'resource "aws_security_group" "ohio_pbox"')
        self.assertEqual(sg.count("ingress {"), 1)
        self.assertIn("from_port   = 22", sg)
        self.assertIn('ipv6_cidr_blocks = ["::/0"]', sg)


class WatchdogTests(unittest.TestCase):
    def test_ohio_has_the_same_function_watching_only_the_parallel_boxes(self):
        fn = block(OHIO, 'resource "aws_lambda_function" "parallel_watchdog_ohio"')
        self.assertIn("aws_iam_role.parallel_watchdog.arn", fn)
        self.assertIn("data.archive_file.parallel_watchdog.output_path", fn)
        self.assertIn('TAG_NAMES     = "parallel-box,parallel-box-2"', fn)
        self.assertNotIn("gpu-box", fn)
        self.assertIn('schedule_expression = "rate(5 minutes)"', OHIO)
        self.assertIn("aws_lambda_permission", OHIO)


if __name__ == "__main__":
    unittest.main()
