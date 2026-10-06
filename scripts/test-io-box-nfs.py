#!/usr/bin/env python3
"""Pins who can reach the I/O box's read-write NFS export (io-box.tf).

The export crosses an inter-region VPC peer, where a security group cannot reference another
group, so the only filter is the source CIDR. That is safe only while the CIDRs name exactly
the subnets NFS clients live in, and those subnets hold nothing else: the games router and
match engines (games-multiplayer.tf) share the us-west-1 VPC and must never be admitted.

This checks the rule and the exports line, and that every host whose user_data installs the
NFS client lives in one of the admitted subnets. Offline; reads the Terraform source.

Run from the repo root:  python3 -S -B scripts/test-io-box-nfs.py
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IO = (ROOT / "io-box.tf").read_text()
MAIN = (ROOT / "main.tf").read_text()
GAMES = (ROOT / "games-multiplayer.tf").read_text()


def source(name):
    return (ROOT / name).read_text()


def block(text, kind, name):
    m = re.search(r'^resource "%s" "%s" \{\n.*?^\}' % (re.escape(kind), re.escape(name)), text, re.S | re.M)
    if m is None:
        raise AssertionError("missing %s.%s" % (kind, name))
    return m.group()


def ingress(sg, port):
    for body in re.findall(r"^  ingress \{\n(.*?)\n  \}", sg, re.S | re.M):
        if re.search(r"from_port\s*=\s*%d\n" % port, body):
            return body
    raise AssertionError("no ingress for port %d" % port)


class NfsRuleTests(unittest.TestCase):
    def test_2049_admits_exactly_the_client_subnets(self):
        rule = ingress(block(IO, "aws_security_group", "io_box"), 2049)
        self.assertRegex(rule, r"to_port\s*=\s*2049\n")
        self.assertRegex(rule, r"cidr_blocks = local\.io_box_nfs_client_cidrs$")
        # Never a whole VPC again, by expression or by literal.
        for whole in ("data.aws_vpc.selected.cidr_block", "data.aws_vpc.west2_default.cidr_block",
                      "10.0.0.0/16", "172.31.0.0/16", "0.0.0.0/0", "ipv6_cidr_blocks"):
            self.assertNotIn(whole, rule)

    def test_client_cidrs_are_the_dev_fleet_and_the_parallel_box_subnet(self):
        m = re.search(r"io_box_nfs_client_cidrs = concat\(\n(.*?)\n  \)", IO, re.S)
        self.assertIsNotNone(m)
        # Comments dropped: the Ohio line carries one.
        self.assertEqual([line.split("#")[0].strip() for line in m.group(1).splitlines()],
                         ["[for s in local.dev_fleet_subnets : s.cidr_block],",
                          "[data.aws_subnet.io_box.cidr_block],",
                          # the parallel boxes in us-east-2 (ohio-pbox.tf): their one SUBNET, never the VPC
                          "[aws_subnet.ohio_pbox.cidr_block],"])
        self.assertIn("dev_fleet_subnets = [aws_subnet.subnet_a, aws_subnet.subnet_b]", MAIN)
        # The dev fleet list must never pick up a games subnet.
        fleet = re.search(r"dev_fleet_subnets = \[(.*?)\]", MAIN).group(1)
        self.assertNotIn("games", fleet)
        self.assertNotIn("dev_fleet_subnets", re.sub(r"mp_alb_subnets = local\.dev_fleet_subnets", "", GAMES))

    def test_exports_line_uses_the_same_list_and_no_whole_vpc(self):
        exports = re.search(r"<<'EXPORTS'\n(.*?)\n\s*EXPORTS", IO, re.S).group(1)
        self.assertIn('/srv/io ${join(" ", [for c in local.io_box_nfs_client_cidrs : '
                      '"${c}(rw,async,no_subtree_check,root_squash,fsid=0)"])}', exports)
        self.assertNotRegex(exports, r"\d+\.\d+\.\d+\.\d+/\d+")

    def test_ssh_is_unchanged_and_separate_from_nfs(self):
        # SSH keeps its own rule; narrowing NFS must not have merged the two.
        rule = ingress(block(IO, "aws_security_group", "io_box"), 22)
        self.assertNotIn("2049", rule)


class AutomountOrderingTests(unittest.TestCase):
    """The /mnt/io automount must not sit in an ordering cycle with the network. On 2026-10-06 systemd broke such a cycle
    by deleting the start jobs of systemd-networkd, systemd-resolved and cloud-init, and a new fcvm-metal-arm booted with
    no network at all; a boot before it had happened to drop the automount instead. The victim is random per boot."""

    SETUP = re.search(r"io_box_client_setup = <<-CLIENT\n(.*?)\n  CLIENT\n", IO, re.S).group(1)

    def unit(self, name, tag):
        return re.search(r"cat > /etc/systemd/system/%s <<'%s'\n(.*?)\n%s\n" % (re.escape(name), tag, tag), self.SETUP, re.S).group(1)

    def test_the_automount_has_no_default_dependencies_and_no_network_ordering(self):
        unit = self.unit("mnt-io.automount", "AUTOMOUNT")
        directives = [l for l in unit.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        self.assertIn("DefaultDependencies=no", directives)
        self.assertEqual([l for l in directives if "network" in l or l.startswith(("After=", "Requires=", "Wants="))], [])

    def test_the_automount_still_unmounts_cleanly_at_shutdown(self):
        unit = self.unit("mnt-io.automount", "AUTOMOUNT")
        self.assertIn("Before=umount.target", unit)
        self.assertIn("Conflicts=umount.target", unit)
        self.assertIn("WantedBy=multi-user.target", unit)

    def test_the_network_ordering_lives_on_the_mount_unit_the_automount_triggers(self):
        unit = self.unit("mnt-io.mount", "MOUNTUNIT")
        self.assertIn("After=network-online.target", unit)
        self.assertIn("Wants=network-online.target", unit)


class MounterTests(unittest.TestCase):
    """Every host that installs the /mnt/io automount sits in an admitted subnet."""

    # Files that splice local.io_box_client_setup into a host's boot script.
    MOUNTERS = (
        "dev-user-data.tf",        # via local.shell_setup: the ARM and x86 metal boxes
        "nextjs-user-data.tf",     # nextjs-dev
        "parallel-box-launch.tf",  # the parallel boxes
    )

    def test_the_mounters_are_exactly_the_known_ones(self):
        found = sorted(p.name for p in ROOT.glob("*.tf") if "${local.io_box_client_setup}" in p.read_text())
        self.assertEqual(found, sorted(self.MOUNTERS),
                         "a new NFS client: add its subnet to io_box_nfs_client_cidrs and here")
        dev = source("dev-user-data.tf")
        self.assertEqual(dev.count("${local.io_box_client_setup}"), 1)
        self.assertEqual(dev.count("${local.shell_setup}"), 2)  # arm_user_data and x86_user_data

    def test_metal_boxes_and_nextjs_are_in_dev_fleet_subnets(self):
        self.assertIn("subnet_id         = local.subnet_ids_by_az[var.firecracker_availability_zone]",
                      source("firecracker-dev.tf"))
        self.assertIn("subnet_ids_by_az  = { for s in local.dev_fleet_subnets : s.availability_zone => s.id }", MAIN)
        self.assertIn("subnet_id         = aws_subnet.subnet_a.id", block(source("x86-dev.tf"), "aws_network_interface", "x86_dev"))
        self.assertIn("subnet_id              = aws_subnet.subnet_a.id",
                      block(source("nextjs-dev.tf"), "aws_instance", "nextjs_dev"))

    def test_parallel_boxes_launch_in_the_subnet_the_nfs_rules_admit(self):
        # The boxes moved to us-east-2 (ohio-pbox.tf): they launch in that VPC's one subnet, which is a client of the export
        # (a subnet, never the VPC) and reaches the I/O box over a peering route to ITS subnet only.
        launch = source("parallel-box-launch.tf")
        self.assertIn("subnet_id                   = aws_subnet.ohio_pbox.id", launch)
        self.assertNotRegex(launch, r'subnet_id\s*=\s*"subnet-[0-9a-f]+"')
        self.assertIn("[aws_subnet.ohio_pbox.cidr_block]", IO)


class ExportsAssociationTests(unittest.TestCase):
    """The server's export file follows the security group on every start: user_data is ignored after creation, so an SSM
    association rewrites it from the same list (a client added later was admitted by the security group and refused by the server)."""

    ASSOC = re.search(r'resource "aws_ssm_association" "io_box_exports" \{.*?\n\}\n', IO, re.S).group(0)

    def test_it_targets_only_the_io_box_in_us_west_2(self):
        self.assertIn("provider            = aws.west2", self.ASSOC)
        self.assertIn('name                = "AWS-RunShellScript"', self.ASSOC)
        self.assertRegex(self.ASSOC, r'targets \{\s+key\s+= "tag:Name"\s+values = \["io-box"\]')

    def test_it_renders_the_same_line_from_the_same_list_as_the_boot_script(self):
        line = '/srv/io ${join(" ", [for c in local.io_box_nfs_client_cidrs : "${c}(rw,async,no_subtree_check,root_squash,fsid=0)"])}'
        self.assertEqual(IO.count(line), 2, "the boot script and the association must render the identical export line")
        for whole in ("data.aws_vpc", "0.0.0.0/0", "172.31.0.0/16", "10.0.0.0/16"):
            self.assertNotIn(whole, self.ASSOC)

    def test_it_replaces_the_file_only_when_it_differs_and_then_reloads(self):
        self.assertIn('if cmp -s "$new" "$f"; then', self.ASSOC)
        self.assertIn('install -m 644 "$new" "$f"', self.ASSOC)
        self.assertLess(self.ASSOC.index('install -m 644'), self.ASSOC.index("exportfs -ra"))
        self.assertIn('rate(1 day)', self.ASSOC)


if __name__ == "__main__":
    unittest.main(verbosity=2)
