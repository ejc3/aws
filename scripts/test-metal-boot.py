#!/usr/bin/env python3
"""The metal boxes' boot scripts (dev-user-data.tf): the NVMe setup reuses an existing filesystem on a reboot and formats blank
disks, earlyoom guards memory, and the system dnsmasq service never races systemd-resolved. The NVMe script is RENDERED the way
Terraform renders it and RUN against stub blkid/btrfs/mount/mkfs, so the decision is tested, not just the text."""
import os
import re
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "dev-user-data.tf").read_text()


def rendered_nvme_script():
    start = TF.index("cat > /usr/local/bin/nvme-btrfs-setup.sh << 'SCRIPT'\n") + len("cat > /usr/local/bin/nvme-btrfs-setup.sh << 'SCRIPT'\n")
    end = TF.index("\nSCRIPT\n", start)
    return TF[start:end].replace("$${", "${") + "\n"


STUBS = {
    # root disk is nvme2n1; the instance store is nvme0n1 and nvme1n1 (model "Amazon EC2 NVMe Instance Storage")
    "lsblk": r'''#!/bin/bash
case "$*" in
  *PKNAME*) echo "${FAKE_ROOT_DEV-nvme2n1}" ;;
  *) printf 'nvme0n1 disk Amazon EC2 NVMe Instance Storage\nnvme1n1 disk Amazon EC2 NVMe Instance Storage\nnvme2n1 disk Amazon Elastic Block Store\n' ;;
esac
''',
    "findmnt": '#!/bin/bash\necho /dev/nvme2n1p1\n',
    "mountpoint": '#!/bin/bash\n[ -e "$T/mounted" ]\n',
    "blkid": r'''#!/bin/bash
# blkid -s TYPE|UUID -o value /dev/<disk>
disk=$(basename "${@: -1}"); want=$2
f="$T/scn/$disk.$want"
[ -f "$f" ] && cat "$f"
exit 0
''',
    "btrfs": r'''#!/bin/bash
case "$1" in
  device) exit 0 ;;
  filesystem) cat "$T/scn/fs_show" 2>/dev/null ;;
esac
''',
    "mount": '#!/bin/bash\necho "mount $*" >> "$T/calls"\n[ -e "$T/scn/mount_fails" ] && exit 32\ntouch "$T/mounted"\nexit 0\n',
    "mkfs.btrfs": '#!/bin/bash\necho "mkfs.btrfs $*" >> "$T/calls"\n',
    "chown": '#!/bin/bash\necho "chown $*" >> "$T/calls"\n',
}


def run_script(scenario, mounted=False, root_dev=None):
    """scenario: {filename: content} under scn/. Returns (returncode, calls, stdout)."""
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        (t / "bin").mkdir()
        (t / "scn").mkdir()
        (t / "mnt").mkdir()
        (t / "home").mkdir()
        for name, body in STUBS.items():
            p = t / "bin" / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IEXEC)
        for name, content in scenario.items():
            (t / "scn" / name).write_text(content)
        if mounted:
            (t / "mounted").write_text("")
        script = rendered_nvme_script().replace("/mnt/fcvm-btrfs", str(t / "mnt")).replace("/home/ubuntu", str(t / "home"))
        (t / "run.sh").write_text(script)
        env = dict(os.environ, PATH="%s:%s" % (t / "bin", os.environ["PATH"]), T=str(t))
        if root_dev is not None:
            env["FAKE_ROOT_DEV"] = root_dev
        r = subprocess.run(["bash", str(t / "run.sh")], capture_output=True, text=True, env=env)
        calls = (t / "calls").read_text() if (t / "calls").exists() else ""
        return r.returncode, calls, r.stdout + r.stderr


def btrfs_disks(uuid="aaaaaaaa-1111-2222-3333-444444444444", only=None):
    out = {}
    for d in ("nvme0n1", "nvme1n1"):
        if only is not None and d not in only:
            continue
        out[d + ".TYPE"] = "btrfs\n"
        out[d + ".UUID"] = uuid + "\n"
    return out


def fs_show(devices=2, missing=False):
    lines = ["Label: none  uuid: aaaaaaaa-1111-2222-3333-444444444444", "\tTotal devices %d FS bytes used 1.00GiB" % devices]
    lines += ["\tdevid    %d size 1.73TiB used 1.00GiB path /dev/nvme%dn1" % (i + 1, i) for i in range(devices)]
    if missing:
        lines.append("\t*** Some devices missing")
    return "\n".join(lines) + "\n"


class NvmeSetupTests(unittest.TestCase):
    def test_the_rendered_script_is_valid_bash(self):
        with tempfile.NamedTemporaryFile("w", suffix=".sh") as f:
            f.write(rendered_nvme_script())
            f.flush()
            self.assertEqual(subprocess.run(["bash", "-n", f.name], capture_output=True).returncode, 0)

    def test_blank_disks_are_formatted_as_before(self):
        rc, calls, out = run_script({})
        self.assertEqual(rc, 0, out)
        self.assertIn("mkfs.btrfs -f -d raid0 -m raid0 /dev/nvme0n1 /dev/nvme1n1", calls)
        self.assertIn("Setting up btrfs RAID0", out)

    def test_a_reboot_reuses_the_filesystem_both_disks_already_carry_and_formats_nothing(self):
        rc, calls, out = run_script(dict(btrfs_disks(), fs_show=fs_show(2)))
        self.assertEqual(rc, 0, out)
        self.assertNotIn("mkfs.btrfs", calls)
        self.assertIn("mount /dev/nvme0n1", calls)
        self.assertIn("Reusing the existing btrfs filesystem", out)
        self.assertIn("data kept", out)

    def test_two_different_filesystems_are_not_reused(self):
        d = btrfs_disks()
        d["nvme1n1.UUID"] = "bbbbbbbb-1111-2222-3333-444444444444\n"
        rc, calls, out = run_script(dict(d, fs_show=fs_show(2)))
        self.assertIn("mkfs.btrfs", calls, out)

    def test_one_blank_disk_is_not_reused(self):
        rc, calls, out = run_script(dict(btrfs_disks(only=["nvme0n1"]), fs_show=fs_show(2)))
        self.assertIn("mkfs.btrfs", calls, out)

    def test_a_filesystem_that_lists_fewer_devices_than_the_disks_is_not_reused(self):
        rc, calls, out = run_script(dict(btrfs_disks(), fs_show=fs_show(1)))
        self.assertIn("mkfs.btrfs", calls, out)

    def test_a_filesystem_with_a_missing_device_is_not_reused(self):
        rc, calls, out = run_script(dict(btrfs_disks(), fs_show=fs_show(2, missing=True)))
        self.assertIn("mkfs.btrfs", calls, out)

    def test_a_filesystem_that_will_not_mount_falls_through_to_a_format(self):
        rc, calls, out = run_script(dict(btrfs_disks(), fs_show=fs_show(2), mount_fails="1"))
        self.assertEqual(calls.count("mkfs.btrfs"), 1, out)

    def test_an_already_mounted_store_is_left_alone(self):
        rc, calls, out = run_script(btrfs_disks(), mounted=True)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, "")
        self.assertIn("already mounted", out)

    def test_an_unknown_root_disk_formats_nothing(self):
        rc, calls, out = run_script({}, root_dev="")
        self.assertNotEqual(rc, 0)
        self.assertNotIn("mkfs", calls)

    def test_ownership_is_not_recursive_over_a_reused_tree(self):
        rc, calls, out = run_script(dict(btrfs_disks(), fs_show=fs_show(2)))
        # The container-store symlink dir under ~/.local is small and still recursive; the NVMe tree must never be.
        self.assertFalse([c for c in calls.splitlines() if c.startswith("chown -R") and "/mnt" in c], calls)
        self.assertNotRegex(rendered_nvme_script(), r"chown -R[^\n]*/mnt/fcvm-btrfs")


class HardeningTests(unittest.TestCase):
    HARDEN = re.search(r"metal_boot_hardening = <<-HARDEN\n(.*?)\nHARDEN\n", TF, re.S).group(1)

    def test_both_metal_scripts_include_it_after_the_nvme_setup(self):
        self.assertEqual(TF.count("${local.nvme_btrfs_setup}\n\n${local.metal_boot_hardening}\n"), 2)

    def test_earlyoom_is_installed_enabled_and_kills_before_the_box_wedges(self):
        self.assertIn("apt-get install -y earlyoom", self.HARDEN)
        self.assertIn("systemctl enable earlyoom.service", self.HARDEN)
        self.assertIn("-m 4,2", self.HARDEN)  # SIGTERM at 4% available, SIGKILL at 2%

    def test_the_infrastructure_that_keeps_the_box_reachable_is_never_a_candidate(self):
        avoid = re.search(r"--avoid '([^']*)'", self.HARDEN).group(1)
        names = set(re.findall(r"[A-Za-z0-9_.-]+", avoid.replace("^|/", "")))
        for name in ("systemd", "sshd", "cloudflared", "tmux", "tmux-scroll", "amazon-ssm-agent", "dbus-daemon"):
            self.assertIn(name, names, name)

    def test_the_system_dnsmasq_service_is_masked_and_only_the_base_package_is_installed(self):
        self.assertIn("systemctl disable --now dnsmasq.service", self.HARDEN)
        self.assertIn("systemctl mask dnsmasq.service", self.HARDEN)
        self.assertEqual(len(re.findall(r"iproute2 dnsmasq-base ", TF)), 2)
        self.assertNotRegex(TF, r"iproute2 dnsmasq cmake")

    def test_it_changes_nothing_on_the_nextjs_or_jumpbox_scripts(self):
        for name in ("nextjs-user-data.tf", "jumpbox2-user-data.tf"):
            self.assertNotIn("metal_boot_hardening", (ROOT / name).read_text(), name)


class ArmNetworkInterfaceTests(unittest.TestCase):
    """CreateNetworkInterface accepts only ONE of ipv6 address count / addresses / prefix count / prefixes. Asking for the
    prefix AND an address count failed every create (the 2026-10-06 move to r8gd), so the host address is assigned afterwards."""

    FC = (ROOT / "firecracker-dev.tf").read_text()
    ENI = re.search(r'resource "aws_network_interface" "firecracker_dev" \{(.*?)\n\}\n', FC, re.S).group(1)

    def test_the_eni_asks_for_the_prefix_only(self):
        self.assertRegex(self.ENI, r"(?m)^\s*ipv6_prefix_count\s*=\s*1")
        self.assertNotRegex(self.ENI, r"(?m)^\s*(ipv6_address_count|ipv6_addresses|ipv6_prefixes)\s*=")

    def test_the_host_address_is_assigned_after_the_eni_and_only_when_it_has_none(self):
        block = re.search(r'resource "terraform_data" "firecracker_dev_host_ipv6" \{(.*?)\n\}\n', self.FC, re.S).group(1)
        self.assertIn("triggers_replace = aws_network_interface.firecracker_dev[0].id", block)
        self.assertIn("assign-ipv6-addresses", block)
        self.assertIn('if [ "$N" = "0" ]', block)

    def test_the_instance_waits_for_the_host_address(self):
        inst = re.search(r'resource "aws_instance" "firecracker_dev" \{(.*?)\n\}\n', self.FC, re.S).group(1)
        self.assertIn("depends_on = [terraform_data.firecracker_dev_host_ipv6]", inst)


if __name__ == "__main__":
    unittest.main()
