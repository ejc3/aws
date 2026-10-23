#!/usr/bin/env python3
"""pbox flavors (pbox-flavors.tf, scripts/parallel-box.sh): `up` launches only a flavor the caller names, each flavor
tries only its own instance types in order, nothing is under 24 vCPU, and IAM allows exactly the flavors' types.

Offline: reads the Terraform and the script, and runs the real script against a fake `aws` and `ssh` on PATH. Live proof
is `aws ec2 describe-instance-type-offerings` in us-east-2c (every type but g5g.metal offered, checked 2026-10-19; g5g.metal
in us-west-2a/b/c, checked 2026-10-22; g4dn.metal in us-east-2a/b/c, checked 2026-10-22), `aws ec2 describe-images` for the
x86_64 Ubuntu build in each site's region, and `aws iam simulate-principal-policy` against nextjs-dev-role after the apply."""
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "parallel-box.sh"
PBOX = SCRIPT.read_text()
FLAVORS_TF = (ROOT / "pbox-flavors.tf").read_text()
LAUNCH = (ROOT / "parallel-box-launch.tf").read_text()
MEMBER = (ROOT / "member-pbox.tf").read_text()
FALLBACK = (ROOT / "pbox-fallback.tf").read_text()
OHIO = (ROOT / "ohio-pbox.tf").read_text()
MODULE = (ROOT / "modules" / "pbox-fallback-site" / "main.tf").read_text()

# vCPU of each size suffix the flavors use; a plain .metal is the whole host, so it is per type
SIZE_VCPU = {"8xlarge": 32, "12xlarge": 48, "24xlarge": 96, "48xlarge": 192, "metal-24xl": 96, "metal-48xl": 192}
METAL_VCPU = {"g5g.metal": 64, "g4dn.metal": 96}
X86_TYPES = {"g4dn.metal"}  # the one x86_64 type; every other is Graviton


def type_vcpu(t):
    return METAL_VCPU[t] if t.endswith(".metal") else SIZE_VCPU[t.split(".", 1)[1]]


def script_type_arch(types):
    """The script's own type_arch() for each type."""
    fn = re.search(r"^type_arch\(\) \{\n.*?\n\}\n", PBOX, re.S | re.M).group(0)
    out = subprocess.run(["bash", "-c", fn + 'for t in "$@"; do echo "$t $(type_arch "$t")"; done', "x", *types],
                         capture_output=True, text=True, check=True).stdout
    return dict(line.split() for line in out.splitlines())


def script_menu():
    """name -> (vcpu, mem, nvme, price, [types]) from the script's FLAVORS block, in order."""
    body = re.search(r"^FLAVORS='\n(.*?)\n'\n", PBOX, re.S | re.M).group(1)
    menu = {}
    for line in body.splitlines():
        f = line.split()
        menu[f[0]] = (f[1], f[2], f[3], f[4], f[5:])
    return menu


def tf_flavors():
    body = re.search(r"pbox_flavors = \{\n(.*?)\n  \}\n", FLAVORS_TF, re.S).group(1)
    return {m.group(1): re.findall(r'"([^"]+)"', m.group(2))
            for m in re.finditer(r'^\s+"([a-z]+(?:-[a-z]+)?-\d+)"\s+= \[(.*?)\]$', body, re.M)}


def block(text, header):
    m = re.search(re.escape(header) + r" \{\n.*?\n\}\n", text, re.S)
    assert m, header
    return m.group(0)


def statement(sid, text):
    m = re.search(r'statement \{\s*sid\s*=\s*"' + re.escape(sid) + r'".*?\n  \}\n', text, re.S)
    assert m, sid
    return m.group(0)


class Menu(unittest.TestCase):
    def test_the_script_and_terraform_list_the_same_flavors_and_types_in_the_same_order(self):
        menu = script_menu()
        self.assertEqual(list(menu), list(tf_flavors()))
        self.assertEqual({f: v[4] for f, v in menu.items()}, tf_flavors())

    def test_the_shapes_and_sizes(self):
        names = list(script_menu())
        self.assertEqual(sorted({n.split("-")[0] for n in names}), ["compute", "general", "kvm", "memory", "storage"])
        for shape in ("compute", "general", "memory", "storage"):
            self.assertEqual([n for n in names if n.startswith(shape + "-")],
                             [f"{shape}-{s}" for s in (32, 48, 96, 192)])
        self.assertEqual([n for n in names if n.startswith("kvm-")], ["kvm-96", "kvm-192", "kvm-gpu-64", "kvm-gpu-96"])

    def test_no_flavor_is_under_24_vcpu_and_every_type_has_the_flavors_size(self):
        for f, (vcpu, mem, _, _, types) in script_menu().items():
            with self.subTest(flavor=f):
                self.assertGreaterEqual(int(vcpu), 24)
                self.assertEqual(int(f.rsplit("-", 1)[1]), int(vcpu))  # the name is the size
                for t in types:
                    self.assertEqual(type_vcpu(t), int(vcpu), t)

    def test_every_type_but_the_x86_gpu_one_is_graviton_and_each_is_in_exactly_one_flavor(self):
        seen = {}
        for f, (_, _, _, _, types) in script_menu().items():
            # spot capacity varies by type: never one alone, but for the GPU metal types, one per flavor
            if not f.startswith("kvm-gpu-"):
                self.assertGreaterEqual(len(types), 2, f)
            for t in types:
                # Graviton names: family letter(s), generation, "g", options -- the templates' AMI is arm64
                if t in X86_TYPES:
                    self.assertNotRegex(t, r"^[a-z]+\d+g[a-z]*\.", t)
                else:
                    self.assertRegex(t, r"^[a-z]+\d+g[a-z]*\.", t)
                self.assertNotIn(t, seen, "%s is in %s and %s" % (t, seen.get(t), f))
                seen[t] = f
        self.assertNotIn("i7ie", " ".join(seen))  # x86
        # the only x86 type is the x86 GPU flavor, alone
        self.assertEqual({t: seen[t] for t in X86_TYPES}, {"g4dn.metal": "kvm-gpu-96"})

    def test_the_scripts_type_arch_says_x86_64_only_for_the_x86_type(self):
        types = [t for v in script_menu().values() for t in v[4]]
        arch = script_type_arch(types)
        self.assertEqual({t for t, a in arch.items() if a == "x86_64"}, X86_TYPES)
        self.assertEqual({a for t, a in arch.items() if t not in X86_TYPES}, {"arm64"})
        # every flavor is one architecture
        for f, v in script_menu().items():
            self.assertEqual(len({arch[t] for t in v[4]}), 1, f)

    def test_metal_only_in_kvm_and_kvm_only_metal(self):
        for f, (_, _, _, _, types) in script_menu().items():
            self.assertEqual(all(".metal" in t for t in types), f.startswith("kvm-"), f)
            self.assertEqual(any(".metal" in t for t in types), f.startswith("kvm-"), f)

    def test_the_shapes_memory_and_disk(self):
        per_vcpu = {"compute": 2, "general": 4, "memory": 8, "storage": 8}
        for f, (vcpu, mem, nvme, price, _) in script_menu().items():
            shape = f.split("-")[0]
            if shape in per_vcpu:
                self.assertEqual(int(mem), int(vcpu) * per_vcpu[shape], f)
            self.assertEqual(nvme != "-", shape == "storage", f)
            self.assertRegex(price, r"^\d+\.\d\d$", f)

    def test_the_gpu_flavors_are_g5g_metal_and_g4dn_metal_and_the_owners_only(self):
        self.assertEqual(script_menu()["kvm-gpu-64"], ("64", "128", "-", "0.60", ["g5g.metal"]))
        self.assertEqual(script_menu()["kvm-gpu-96"], ("96", "384", "-", "4.12", ["g4dn.metal"]))
        gpu_types = {t for f, v in script_menu().items() if f.startswith("kvm-gpu-") for t in v[4]}
        self.assertEqual(gpu_types, {"g5g.metal", "g4dn.metal"})
        self.assertIn('kvm-gpu) echo "GPU passthrough into fcvm', PBOX)
        self.assertIn('case "$FLAVOR" in kvm-gpu-*) [ -z "$MEMBER" ] && [ -z "$OPEN" ] && [ "$CMD" = "up" ] && FALLBACK=1 ;; esac', PBOX)
        self.assertEqual(tf_flavors()["kvm-gpu-96"], ["g4dn.metal"])
        self.assertNotIn("g4dn.metal", (ROOT / "member-pbox.tf").read_text())

    def test_no_type_override_is_left(self):
        self.assertNotIn("PARALLEL_BOX_TYPES", PBOX)
        self.assertNotIn("PARALLEL_BOX_KVM_TYPES", PBOX)
        self.assertNotRegex(PBOX, r'(?m)^TYPES="\$\{')


class Iam(unittest.TestCase):
    def test_the_owners_and_members_allowed_types_are_the_union_of_the_flavors(self):
        self.assertIn("pbox_flavor_types = distinct(flatten(values(local.pbox_flavors)))", FLAVORS_TF)
        self.assertIn('member_pbox_types = distinct(flatten([for f, types in local.pbox_flavors : types if !startswith(f, "kvm-")]))',
                      FLAVORS_TF)
        doc = block(FLAVORS_TF, 'data "aws_iam_policy_document" "pbox_flavor_types"')
        for sid, names, types in (("OwnerPboxFlavorTypesOnly", "local.parallel_box_names", "local.pbox_flavor_types"),
                                  ("MemberPboxFlavorTypesOnly", "local.member_pbox_names", "local.member_pbox_types")):
            with self.subTest(sid=sid):
                s = statement(sid, doc)
                self.assertIn('effect    = "Deny"', s)
                self.assertIn('actions   = ["ec2:RunInstances"]', s)
                self.assertIn(':instance/*"]', s)
                self.assertRegex(s, r'test     = "StringEquals"\n\s+variable = "aws:RequestTag/Name"\n\s+values   = %s\n' % re.escape(names))
                self.assertRegex(s, r'test     = "StringNotEquals"\n\s+variable = "ec2:InstanceType"\n\s+values   = %s\n' % re.escape(types))
        self.assertEqual(len(re.findall(r"^  statement \{", doc, re.M)), 5)
        # --open's own IPv6 address on the owner's parallel boxes' interfaces only (by their Name tag).
        s = statement("AssignIpv6ToOpenParallelBoxes", doc)
        self.assertIn('actions   = ["ec2:AssignIpv6Addresses"]', s)
        self.assertIn("resources = local.parallel_box_eni_arns", s)
        self.assertIn('variable = "ec2:ResourceTag/Name"', s)
        # Stale spot requests: pbox may cancel any of the account's, never a persistent dev box's (their own ids).
        s = statement("CancelStaleSpotRequests", doc)
        self.assertIn('actions   = ["ec2:CancelSpotInstanceRequests"]', s)
        self.assertIn(':spot-instances-request/*"]', s)
        self.assertIn('sid       = "NeverThePersistentBoxesRequests"', doc)
        deny = statement("NeverThePersistentBoxesRequests", doc)
        self.assertIn('effect    = "Deny"', deny)
        self.assertIn('actions   = ["ec2:CancelSpotInstanceRequests"]', deny)
        # Unconditional, never a dynamic block: a replacement's unknown request id cannot make the statement unknown.
        self.assertIn("resources = concat(local.pbox_persistent_spot_requests, [", deny)
        self.assertNotIn('dynamic "statement"', doc)
        for ref in ("aws_instance.firecracker_dev[*].spot_instance_request_id", "aws_instance.x86_dev[*].spot_instance_request_id",
                    "[aws_instance.io_box.spot_instance_request_id]"):
            self.assertIn(ref, FLAVORS_TF, ref)
        # the same union, computed here from the script's menu
        menu = script_menu()
        owner = {t for v in menu.values() for t in v[4]}
        member = {t for f, v in menu.items() if not f.startswith("kvm-") for t in v[4]}
        self.assertEqual(owner - member, {t for t in owner if ".metal" in t})
        self.assertEqual(owner, {t for ts in tf_flavors().values() for t in ts})

    def test_no_wildcard_type_anywhere(self):
        for text in (FLAVORS_TF, LAUNCH, MEMBER, FALLBACK):
            self.assertNotRegex(text, r'"[a-z0-9]+\.\*"')
            self.assertNotIn('"ec2:InstanceType"\n      values   = ["*"]', text)

    def test_attached_to_both_roles_that_launch_pboxes(self):
        self.assertIn('resource "aws_iam_policy" "pbox_flavor_types"', FLAVORS_TF)
        for name, role in (("dev_server_pbox_flavor_types", "aws_iam_role.dev_server.name"),
                           ("nextjs_dev_pbox_flavor_types", "aws_iam_role.nextjs_dev.name")):
            att = block(FLAVORS_TF, 'resource "aws_iam_role_policy_attachment" "%s"' % name)
            self.assertIn(role, att)
            self.assertIn("aws_iam_policy.pbox_flavor_types.arn", att)
        # the launch grants it narrows are on exactly these two roles: parallel-box-control, parallel-box-fallback and
        # (nextjs-dev only) member-pbox-control
        for text, policy in ((LAUNCH, "parallel_box_control"), (FALLBACK, "parallel_box_fallback")):
            roles = re.findall(r"role       = (aws_iam_role\.\w+)\.name\n  policy_arn = aws_iam_policy\.%s\.arn" % policy, text)
            self.assertEqual(sorted(roles), ["aws_iam_role.dev_server", "aws_iam_role.nextjs_dev"], policy)
        self.assertIn("role       = aws_iam_role.nextjs_dev.name\n  policy_arn = aws_iam_policy.member_pbox_control.arn", MEMBER)

    def test_the_policy_fits_iams_managed_policy_limit(self):
        # rendered with ARN-length placeholders: about 3,700 of the 6,144 characters
        menu = script_menu()
        owner = list(dict.fromkeys(t for v in menu.values() for t in v[4]))
        member = list(dict.fromkeys(t for f, v in menu.items() if not f.startswith("kvm-") for t in v[4]))
        arn = "arn:aws:ec2:*:123456789012:instance/*"
        doc = {"Version": "2012-10-17", "Statement": [
            {"Sid": "OwnerPboxFlavorTypesOnly", "Effect": "Deny", "Action": "ec2:RunInstances", "Resource": arn,
             "Condition": {"StringEquals": {"aws:RequestTag/Name": ["parallel-box"] + ["parallel-box-%d" % n for n in range(2, 9)]},
                           "StringNotEquals": {"ec2:InstanceType": owner}}},
            {"Sid": "MemberPboxFlavorTypesOnly", "Effect": "Deny", "Action": "ec2:RunInstances", "Resource": arn,
             "Condition": {"StringEquals": {"aws:RequestTag/Name": ["pbox-colton", "pbox-connor", "pbox-colin", "pbox-skevh"]},
                           "StringNotEquals": {"ec2:InstanceType": member}}},
            {"Sid": "CancelStaleSpotRequests", "Effect": "Allow", "Action": "ec2:CancelSpotInstanceRequests",
             "Resource": "arn:aws:ec2:*:123456789012:spot-instances-request/*"},
            {"Sid": "NeverThePersistentBoxesRequests", "Effect": "Deny", "Action": "ec2:CancelSpotInstanceRequests",
             "Resource": ["arn:aws:ec2:*:123456789012:spot-instances-request/sir-%08d" % n for n in range(3)]}]}
        self.assertLess(len(json.dumps(doc, separators=(",", ":"))), 5000)

    def test_the_spot_quota_is_still_sized_for_the_largest_flavor(self):
        self.assertEqual(max(int(v[0]) for v in script_menu().values()), 192)
        self.assertIn("parallel_box_max_vcpus = 192", OHIO)


class X86Image(unittest.TestCase):
    """The x86 flavor boots the x86_64 build of the arm64 image, passed by `pbox` with --image-id, and every image a dev box
    may launch is pinned by exact ARN in both launch grants."""

    def test_the_home_x86_image_is_the_same_ubuntu_build_and_pinned_by_arn(self):
        arm = re.search(r'variable "parallel_box_ami_ohio" \{.*?default\s+= "(ami-[0-9a-f]+)" # (\S+)\n', OHIO, re.S)
        x86 = re.search(r'variable "parallel_box_ami_ohio_x86" \{.*?default\s+= "(ami-[0-9a-f]+)" # (\S+)\n', OHIO, re.S)
        self.assertIsNotNone(arm)
        self.assertIsNotNone(x86)
        self.assertNotEqual(arm.group(1), x86.group(1))
        self.assertEqual(x86.group(2), arm.group(2).replace("-arm64-", "-amd64-"))
        sites = re.search(r"parallel_box_sites = \{.*?\n  \}\n", LAUNCH, re.S).group(0)
        self.assertRegex(sites, r"\n      ami_x86 += var\.parallel_box_ami_ohio_x86\n")
        referenced = re.search(r"parallel_box_referenced_arns = flatten\(\[.*?\n  \]\)", LAUNCH, re.S).group(0)
        self.assertIn('"arn:aws:ec2:${r}::image/${site.ami}",', referenced)
        self.assertIn('"arn:aws:ec2:${r}::image/${site.ami_x86}",', referenced)
        # the referenced statement lists exactly those ARNs, still pinned to the templates
        stmt = statement("RunInstancesReferencedResources", LAUNCH)
        self.assertIn("resources = concat(local.parallel_box_referenced_arns, local.parallel_box_template_arns)", stmt)
        self.assertIn("values   = local.parallel_box_template_arns", stmt)

    def test_each_fallback_site_has_the_x86_build_by_name_and_it_is_pinned_by_arn(self):
        arm = re.search(r'pbox_fallback_ami_name\s+= "([^"]+)"', FALLBACK).group(1)
        x86 = re.search(r'pbox_fallback_ami_name_x86 = "([^"]+)"', FALLBACK).group(1)
        self.assertEqual(x86, arm.replace("-arm64-", "-amd64-"))
        self.assertEqual(FALLBACK.count("ami_name_x86          = local.pbox_fallback_common.ami_name_x86"), 4)
        ami = block(MODULE, 'data "aws_ami" "ubuntu_x86"')
        self.assertIn('owners = ["099720109477"]', ami)
        self.assertIn("values = [var.ami_name_x86]", ami)
        self.assertIn('values = ["x86_64"]', ami)
        self.assertIn("value       = data.aws_ami.ubuntu_x86.id", block(MODULE, 'output "ami_id_x86"'))
        self.assertNotIn("ubuntu_x86", block(MODULE, 'resource "aws_launch_template" "box"'))  # passed by pbox, not booted by default
        referenced = re.search(r"pbox_fallback_referenced_arns = flatten\(\[.*?\n  \]\)", FALLBACK, re.S).group(0)
        self.assertIn('"arn:aws:ec2:${r}::image/${s.ami_id}",', referenced)
        self.assertIn('"arn:aws:ec2:${r}::image/${s.ami_id_x86}",', referenced)

    def test_no_launch_grant_wildcards_an_image(self):
        for text in (LAUNCH, FALLBACK, MEMBER):
            self.assertNotRegex(text, r"image/\*")
            self.assertNotRegex(text, r"::image/\$\{[^}]*\}\*")

    def test_the_images_are_published_for_pbox(self):
        self.assertIn("x86_image = local.parallel_box_sites[local.parallel_box_region].ami_x86", OHIO)
        self.assertIn("r => { subnets = s.subnets, x86_image = s.ami_id_x86 }", FALLBACK)

    def test_the_members_box_never_gets_the_x86_image(self):
        for word in ("ami_x86", "parallel_box_ami_ohio_x86", "ami_id_x86", "--image-id"):
            self.assertNotIn(word, MEMBER)

    def test_the_script_passes_the_image_only_for_an_x86_type(self):
        self.assertEqual(len(re.findall(r'IMG_OUT="\$\(image_args "\$T" ', PBOX)), 2)  # home and fallback
        fn = re.search(r"^image_args\(\) \{\n.*?\n\}\n", PBOX, re.S | re.M).group(0)
        self.assertIn('[ "$(type_arch "$1")" = x86_64 ] || return 0', fn)
        self.assertIn("printf '%s\\n' --image-id \"$2\"", fn)


class GpuQuota(unittest.TestCase):
    def test_the_g_spot_quota_where_the_gpu_flavors_launch_covers_the_largest_and_never_asks_below_the_current_value(self):
        # one box of the largest GPU flavor, derived from the flavor names (kvm-gpu-96 = 96)
        self.assertIn('pbox_gpu_spot_vcpus = max([for f in keys(local.pbox_flavors) : tonumber(trimprefix(f, "kvm-gpu-")) '
                      'if startswith(f, "kvm-gpu-")]...)', FLAVORS_TF)
        self.assertEqual(max(int(v[0]) for f, v in script_menu().items() if f.startswith("kvm-gpu-")), 96)
        # us-east-2: the home site, where kvm-gpu-96 launches; us-west-2: kvm-gpu-64, which us-east-2 does not offer;
        # us-east-1: a kvm-gpu-96 fallback site whose default 64 vCPU fits no g4dn.metal (#630)
        for name, provider in (("ohio_spot_g", "aws.ohio"), ("west2_spot_g", "aws.west2"), ("east1_spot_g", "aws.dr")):
            with self.subTest(quota=name):
                for kind in ("data", "resource"):
                    b = block(FLAVORS_TF, '%s "aws_servicequotas_service_quota" "%s"' % (kind, name))
                    self.assertIn("provider     = %s" % provider, b)
                    self.assertIn('quota_code   = "L-3819A6DF"', b)
                b = block(FLAVORS_TF, 'resource "aws_servicequotas_service_quota" "%s"' % name)
                self.assertIn("value        = max(local.pbox_gpu_spot_vcpus, data.aws_servicequotas_service_quota.%s.value)" % name, b)
        self.assertIn('parallel_box_region = "us-east-2"', OHIO)
        # nothing else in this account owns the G spot quota (fcvm-fleet-account.tf sets its own account's, #605)
        tf = "".join(p.read_text() for p in ROOT.glob("*.tf") if p.name != "fcvm-fleet-account.tf")
        self.assertEqual(len(re.findall(r'resource "aws_servicequotas_service_quota" "\w+" \{\n[^}]*"L-3819A6DF"', tf)), 3)


class GpuCheckRuns(unittest.TestCase):
    """The script's GPU_CHECK, run against a fake sysfs: ready only when every GPU is vfio-pci's with its group node, and
    its whole IOMMU group is unbound or vfio-pci, pci-stub or pcieport."""

    def run_check(self, members, arch="aarch64", cmdline="ro console=ttyS0", marker=None, boot_id="boot-2"):
        script = (Path(__file__).resolve().parent / "parallel-box.sh").read_text()
        check = re.search(r"^GPU_CHECK='(.*?)'$", script, re.S | re.M).group(1)
        with tempfile.TemporaryDirectory() as root:
            r = Path(root)
            (r / "bin").mkdir()
            for tool, body in (("timeout", "exit 0"), ("cloud-init", "exit 0"), ("uname", "echo " + arch)):
                (r / "bin" / tool).write_text("#!/bin/sh\n" + body + "\n")
                (r / "bin" / tool).chmod(0o755)
            (r / "proc/sys/kernel/random").mkdir(parents=True)
            (r / "proc/cmdline").write_text(cmdline + "\n")
            (r / "proc/sys/kernel/random/boot_id").write_text(boot_id + "\n")
            (r / "var/lib").mkdir(parents=True)
            if marker is not None:
                (r / "var/lib/pbox-iommu-reboot").write_text(marker + "\n")
            (r / "dev/vfio").mkdir(parents=True)
            (r / "dev/vfio/7").write_text("")
            grp = r / "sys/kernel/iommu_groups/7/devices"
            grp.mkdir(parents=True)
            for addr, vendor, driver in members:
                d = r / "sys/bus/pci/devices" / addr
                d.mkdir(parents=True)
                (d / "vendor").write_text(vendor + "\n")
                if driver:
                    (r / "sys/bus/pci/drivers" / driver).mkdir(parents=True, exist_ok=True)
                    (d / "driver").symlink_to(r / "sys/bus/pci/drivers" / driver)
                (d / "iommu_group").symlink_to(r / "sys/kernel/iommu_groups/7")
                (grp / addr).symlink_to(d)
            body = re.sub(r"(?<!/proc)/sys/", root + "/sys/", check)
            body = body.replace("/proc/", root + "/proc/").replace("/var/lib/pbox-iommu-reboot", root + "/var/lib/pbox-iommu-reboot")
            body = body.replace('[ -c "/dev/vfio/', '[ -e "' + root + "/dev/vfio/")
            env = dict(os.environ, PATH=str(r / "bin") + ":/usr/bin:/bin")
            return subprocess.run(["bash", "-c", body], env=env, capture_output=True, text=True).returncode

    def test_an_x86_box_waiting_for_its_iommu_reboot_says_so_with_75(self):
        # the boot script asked for the reboot in THIS boot (the marker names it): the caller waits and asks again
        gpu = [("0000:00:1f.0", "0x10de", "nvidia")]
        self.assertEqual(self.run_check(gpu, arch="x86_64", marker="boot-2", boot_id="boot-2"), 75)

    def test_an_x86_box_that_rebooted_without_the_iommu_parameter_fails(self):
        on_vfio = [("0000:00:1f.0", "0x10de", "vfio-pci")]
        for marker in (None, "boot-1"):  # never asked, or asked in an earlier boot and it did not take
            with self.subTest(marker=marker):
                self.assertEqual(self.run_check(on_vfio, arch="x86_64", marker=marker, boot_id="boot-2"), 1)

    def test_an_x86_box_booted_with_intel_iommu_on_is_checked_like_any_other(self):
        on_vfio = [("0000:00:1f.0", "0x10de", "vfio-pci")]
        cmd = "ro console=ttyS0 intel_iommu=on iommu=pt"
        self.assertEqual(self.run_check(on_vfio, arch="x86_64", cmdline=cmd, marker="boot-1"), 0)
        self.assertNotEqual(self.run_check([("0000:00:1f.0", "0x10de", "nvidia")], arch="x86_64", cmdline=cmd), 0)

    def test_an_arm64_box_needs_no_kernel_parameter(self):
        self.assertEqual(self.run_check([("0000:00:1f.0", "0x10de", "vfio-pci")], arch="aarch64", marker="boot-2"), 0)

    def test_a_gpu_alone_in_its_group_on_vfio_is_ready(self):
        self.assertEqual(self.run_check([("0000:00:1f.0", "0x10de", "vfio-pci")]), 0)

    def test_a_bridge_or_an_unbound_device_in_the_group_is_fine(self):
        self.assertEqual(self.run_check([("0000:00:1f.0", "0x10de", "vfio-pci"), ("0000:00:1c.0", "0x1d0f", "pcieport"),
                                         ("0000:00:1d.0", "0x1d0f", None)]), 0)

    def test_a_group_shared_with_a_bound_nic_is_not_ready(self):
        self.assertNotEqual(self.run_check([("0000:00:1f.0", "0x10de", "vfio-pci"), ("0000:00:05.0", "0x1d0f", "ena")]), 0)

    def test_a_gpu_still_on_nvidia_is_not_ready(self):
        self.assertNotEqual(self.run_check([("0000:00:1f.0", "0x10de", "nvidia")]), 0)


class GpuVfio(unittest.TestCase):
    def test_every_nvidia_function_goes_to_vfio_pci_and_nothing_happens_without_one(self):
        m = re.search(r"pbox_gpu_vfio_setup = <<-GPU\n(.*?)\n  GPU\n", FLAVORS_TF, re.S)
        self.assertIsNotNone(m, "pbox_gpu_vfio_setup")
        g = m.group(1)
        self.assertIn('[ "$(cat "$d/vendor")" = 0x10de ]', g)
        self.assertIn('if [ -n "$GPU_DEVS" ]; then', g)
        # everything that changes the box is inside the guard
        self.assertLess(g.index('if [ -n "$GPU_DEVS" ]; then'), g.index("modprobe vfio-pci"))
        self.assertTrue(g.rstrip().endswith("fi"))
        self.assertIn("echo vfio-pci > /etc/modules-load.d/vfio-pci.conf", g)
        self.assertIn('for dev in $GPU_DEVS; do driverctl set-override "$dev" vfio-pci; done', g)
        self.assertIn('SUBSYSTEM=="vfio", GROUP="kvm", MODE="0660"', g)
        self.assertIn("ubuntu hard memlock unlimited", g)
        self.assertNotRegex(g, r"nvidia-(driver|utils|dkms)|cuda")  # the host never drives the GPUs

    def test_every_apt_call_in_a_pbox_boot_waits_for_the_dpkg_lock(self):
        # ejc3/aws#609: the boot-time package run held the dpkg lock, driverctl failed to install, every GPU stayed on nouveau.
        import re as _re
        for f in ("parallel-box-launch.tf", "pbox-fallback.tf", "member-pbox.tf", "pbox-flavors.tf"):
            checked = 0
            for line in (ROOT / f).read_text().splitlines():
                # Options may sit between apt-get and the subcommand (-o ..., -y), so match any words before it.
                if _re.search(r"\bapt-get(?:\s+\S+)*?\s+(update|install)\b", line) and not line.lstrip().startswith("#"):
                    checked += 1
                    self.assertIn("-o DPkg::Lock::Timeout=300", line, f"{f}: {line.strip()}")
            self.assertGreater(checked, 0, f"{f}: no apt-get update/install command found")

    def test_a_missing_driverctl_stops_the_gpu_step_loudly(self):
        gpu = (ROOT / "pbox-flavors.tf").read_text().split("pbox_gpu_vfio_setup = <<-GPU", 1)[1].split("\n  GPU\n", 1)[0]
        self.assertIn("if ! command -v driverctl >/dev/null; then", gpu)
        self.assertIn("pbox GPU setup FAILED: driverctl is not installed", gpu)
        self.assertLess(gpu.index("command -v driverctl"), gpu.index("driverctl set-override"))

    def test_the_home_and_fallback_boots_run_it_once_after_apt_and_the_members_do_not(self):
        for text in (LAUNCH, FALLBACK):
            self.assertEqual(text.count("${local.pbox_gpu_vfio_setup}"), 1)
            self.assertLess(text.index("apt-get -o DPkg::Lock::Timeout=300 update"), text.index("${local.pbox_gpu_vfio_setup}"))
        self.assertNotIn("pbox_gpu_vfio_setup", MEMBER)

    def test_up_checks_the_gpus_are_vfio_pcis_on_a_gpu_type_after_the_boot_script(self):
        m = re.search(r"^GPU_CHECK='(.*?)'$", PBOX, re.S | re.M)
        self.assertIsNotNone(m, "GPU_CHECK")
        check = m.group(1)
        self.assertTrue(check.startswith("timeout 900 cloud-init status --wait"))
        self.assertIn('[ "${drv##*/}" = vfio-pci ] && [ -n "$grp" ] && [ -c "/dev/vfio/${grp##*/}" ] || bad=1', check)
        self.assertIn('[ "$n" -gt 0 ] && [ "$bad" -eq 0 ]', check)
        wait = re.search(r"^wait_ready\(\) \{\n(.*?)\n\}\n", PBOX, re.S | re.M).group(1)
        self.assertIn('case "$(type_flavor "$CHOSEN")" in kvm-gpu-*)', wait)
        self.assertIn('if on_box "$GPU_CHECK"; then', wait)
        self.assertIn("if on_box 'test -c /dev/kvm && sudo usermod -aG kvm ubuntu'; then", wait)
        self.assertLess(wait.index("test -c /dev/kvm"), wait.index("$GPU_CHECK"))
        # on_box asks again while the box reboots (ssh 255, or GPU_CHECK's 75), and gives up after 20 minutes
        on_box = re.search(r"^on_box\(\) \{\n(.*?)\n\}\n", PBOX, re.S | re.M).group(1)
        self.assertIn("for tries in $(seq 1 120); do", on_box)
        self.assertIn('case "$rc" in 255|75) ;; *) return "$rc" ;; esac', on_box)
        self.assertIn("-o ConnectTimeout=5", on_box)


def iommu_snippet():
    m = re.search(r"pbox_gpu_iommu_reboot = <<-IOMMU\n(.*?)\n  IOMMU\n", FLAVORS_TF, re.S)
    assert m, "pbox_gpu_iommu_reboot"
    return m.group(1)


class GpuIommuReboot(unittest.TestCase):
    """The x86 GPU box's one reboot for intel_iommu=on (pbox_gpu_iommu_reboot): x86 with an NVIDIA function only, once,
    and first in the boot scripts, so the boot script that runs again after it has done nothing twice."""

    def test_everything_is_inside_one_guard_x86_nvidia_not_yet_on_and_no_marker(self):
        s = iommu_snippet()
        guard = s.splitlines()[0]
        for cond in ('[ "$(uname -m)" = x86_64 ]', "grep -qx 0x10de /sys/bus/pci/devices/*/vendor",
                     "! grep -qw intel_iommu=on /proc/cmdline", "[ ! -e /var/lib/pbox-iommu-reboot ]"):
            self.assertIn(cond, guard)
        self.assertTrue(guard.startswith("if ") and guard.endswith("; then"), guard)
        self.assertEqual(s.rstrip().splitlines()[-1], "fi")
        self.assertEqual(s.count("\nfi"), 1)
        body = s[s.index("then\n"):]
        self.assertIn('"$GRUB_CMDLINE_LINUX_DEFAULT intel_iommu=on iommu=pt"', body)
        self.assertIn("> /etc/default/grub.d/90-pbox-iommu.cfg", body)
        # the marker (this boot's id) and the re-run of the boot script are in place before the reboot
        for step in ("update-grub", "cat /proc/sys/kernel/random/boot_id > /var/lib/pbox-iommu-reboot",
                     "rm -f /var/lib/cloud/instance/sem/config_scripts_user"):
            self.assertLess(body.index(step), body.index("systemctl reboot"), step)
        self.assertLess(body.index("systemctl reboot"), body.index("exit 0"))
        self.assertEqual(s.count("systemctl reboot"), 1)

    def run_snippet(self, arch, vendors, cmdline="ro console=ttyS0", marker=False):
        with tempfile.TemporaryDirectory() as root:
            r = Path(root)
            (r / "bin").mkdir()
            for tool, body in (("uname", "echo " + arch), ("update-grub", 'echo update-grub >> "$LOG"'),
                               ("systemctl", 'echo "systemctl $*" >> "$LOG"'), ("sleep", "exit 0")):
                (r / "bin" / tool).write_text("#!/bin/sh\n" + body + "\n")
                (r / "bin" / tool).chmod(0o755)
            for i, v in enumerate(vendors):
                d = r / "sys/bus/pci/devices" / ("0000:00:%02x.0" % i)
                d.mkdir(parents=True)
                (d / "vendor").write_text(v + "\n")
            (r / "proc/sys/kernel/random").mkdir(parents=True)
            (r / "proc/cmdline").write_text(cmdline + "\n")
            (r / "proc/sys/kernel/random/boot_id").write_text("boot-1\n")
            (r / "etc/default/grub.d").mkdir(parents=True)
            (r / "var/lib/cloud/instance/sem").mkdir(parents=True)
            (r / "var/lib/cloud/instance/sem/config_scripts_user").write_text("")
            if marker:
                (r / "var/lib/pbox-iommu-reboot").write_text("boot-0\n")
            body = iommu_snippet()
            for p in ("/sys/bus/", "/proc/", "/etc/default/grub.d/", "/var/lib/"):
                body = body.replace(p, root + p)
            script = body + "\necho after-the-step\n"
            log = r / "log"
            env = dict(os.environ, PATH=str(r / "bin") + ":/usr/bin:/bin", LOG=str(log))
            out = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
            dropin = r / "etc/default/grub.d/90-pbox-iommu.cfg"
            return {"rc": out.returncode, "continued": "after-the-step" in out.stdout,
                    "calls": log.read_text().splitlines() if log.exists() else [],
                    "dropin": dropin.read_text() if dropin.exists() else None,
                    "marker": (r / "var/lib/pbox-iommu-reboot").read_text().strip() if (r / "var/lib/pbox-iommu-reboot").exists() else None,
                    "sem": (r / "var/lib/cloud/instance/sem/config_scripts_user").exists()}

    def test_an_x86_gpu_box_writes_the_drop_in_marks_the_boot_rearms_the_script_and_reboots(self):
        res = self.run_snippet("x86_64", ["0x1d0f", "0x10de"])
        self.assertEqual(res["rc"], 0)
        self.assertFalse(res["continued"])  # nothing after the step runs in this boot
        self.assertEqual(res["calls"], ["update-grub", "systemctl reboot"])
        self.assertIn("intel_iommu=on iommu=pt", res["dropin"])
        self.assertEqual(res["marker"], "boot-1")
        self.assertFalse(res["sem"])

    def test_it_is_inert_on_arm64_without_a_gpu_after_the_reboot_and_once_marked(self):
        for name, kw in (("arm64 with a GPU", dict(arch="aarch64", vendors=["0x10de"])),
                         ("x86 without a GPU", dict(arch="x86_64", vendors=["0x1d0f", "0x8086"])),
                         ("x86 booted with it", dict(arch="x86_64", vendors=["0x10de"], cmdline="ro intel_iommu=on iommu=pt")),
                         ("x86 already rebooted once", dict(arch="x86_64", vendors=["0x10de"], marker=True))):
            with self.subTest(name):
                res = self.run_snippet(**kw)
                self.assertEqual(res["rc"], 0)
                self.assertTrue(res["continued"])
                self.assertEqual(res["calls"], [])
                self.assertIsNone(res["dropin"])
                self.assertTrue(res["sem"])

    def test_the_home_and_fallback_boots_run_it_first_and_the_members_do_not(self):
        for text in (LAUNCH, FALLBACK):
            self.assertEqual(text.count("${local.pbox_gpu_iommu_reboot}"), 1)
            at = text.index("${local.pbox_gpu_iommu_reboot}")
            # after the dev-hop key (idempotent), before anything that is not: the work volume, apt, NVMe, vfio
            self.assertLess(text.index("chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys"), at)
            for later in ('mount "$DEV" /mnt/work', "mkfs.ext4", "apt-get -o DPkg::Lock::Timeout=300 update", "${local.pbox_local_nvme_setup}",
                          "${local.pbox_gpu_vfio_setup}"):
                self.assertLess(at, text.index(later), later)
        self.assertNotIn("pbox_gpu_iommu_reboot", MEMBER)


class LocalNvme(unittest.TestCase):
    def test_every_pbox_boot_mounts_local_nvme_found_by_model_and_never_formats_a_used_disk(self):
        nvme = re.search(r"pbox_local_nvme_setup = <<-NVME\n(.*?)\n  NVME\n", FLAVORS_TF, re.S).group(1)
        self.assertIn("/Amazon EC2 NVMe Instance Storage/", nvme)
        self.assertNotRegex(nvme, r"/dev/nvme\d")  # never a guessed device name
        self.assertIn('blkid "$d" >/dev/null 2>&1 && NVME_USED=', nvme)
        self.assertIn("NOT formatting", nvme)
        self.assertEqual(nvme.count("mkfs.ext4"), 1)
        self.assertNotIn("fstab", nvme)
        self.assertNotIn("Elastic Block Store", nvme)
        for text in (LAUNCH, MEMBER, FALLBACK):
            self.assertEqual(text.count("${local.pbox_local_nvme_setup}"), 1)
            # after apt-get update (mdadm), after the work volume is mounted
            self.assertLess(text.index("apt-get -o DPkg::Lock::Timeout=300 update"), text.index("${local.pbox_local_nvme_setup}"))
            self.assertLess(text.index("mount \"$DEV\" /mnt/work"), text.index("${local.pbox_local_nvme_setup}"))


# The script against a fake aws: every call is logged; run-instances always finds no capacity.
FAKE_AWS = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
a = " ".join(args)
open(os.environ["FAKE_LOG"], "a").write(json.dumps(args) + "\n")
if "get-parameter" in a:
    print(os.environ.get("FAKE_POINTER") or '{"region": "us-east-2", "x86_image": "ami-0x86home", "fallback": {}}')
elif "describe-launch-template-versions" in a:
    print("spot")
elif "describe-instances" in a and "PublicIpAddress" in a:
    print("203.0.113.9" if os.environ.get("FAKE_RUNNING") else "None")
elif "describe-instances" in a and "InstanceType" in a:
    print(os.environ.get("FAKE_RUNNING") or "None")
elif "describe-instances" in a and "InstanceId" in a:
    print("i-0running" if os.environ.get("FAKE_RUNNING") else "None")
elif "describe-instances" in a and "InstanceLifecycle" in a:
    print("spot")
elif "describe-instances" in a:
    print("None")
elif "describe-volumes" in a and "[State," in a:
    print("available\tNone")
elif "describe-volumes" in a and "AvailabilityZone" in a:
    print("us-east-2c")
elif "describe-volumes" in a:
    print("vol-1\t100\tavailable")
elif "describe-instance-types" in a:
    print("96")
elif "describe-spot-price-history" in a:
    print("0.5")
elif "run-instances" in a:
    print("An error occurred (InsufficientInstanceCapacity) when calling the RunInstances operation")
    sys.exit(254)
else:
    print("None")
'''


class ScriptRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name, body in (("aws", FAKE_AWS), ("ssh", "#!/bin/sh\nexit 255\n"), ("sleep", "#!/bin/sh\nexit 0\n")):
            p = self.tmp / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IXUSR)
        self.log = self.tmp / "calls"

    def pbox(self, *args, user="ejc3", running="", pointer=""):
        if self.log.exists():
            self.log.unlink()
        env = dict(os.environ, PATH=f"{self.tmp}:{os.environ['PATH']}", HOME=str(self.tmp), FAKE_LOG=str(self.log),
                   MEMBER_BOX_USER=user, FAKE_RUNNING=running, FAKE_POINTER=pointer)
        env.pop("PARALLEL_BOX_REGION", None)
        r = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=120)
        return r.returncode, r.stdout, r.stderr

    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def launched_types(self):
        return [c[c.index("--instance-type") + 1] for c in self.calls() if "run-instances" in c]

    def test_up_without_a_flavor_fails_prints_the_menu_and_calls_nothing(self):
        for args in (("up",), ("up", "3"), ("up", "3", "--fallback"), ("capacity",)):
            with self.subTest(args=args):
                rc, out, err = self.pbox(*args)
                self.assertEqual(rc, 2, err)
                self.assertEqual(self.calls(), [], "not one AWS call, so nothing is launched")
                self.assertEqual(out, "")
                self.assertIn("needs a FLAVOR", err)
                for f in script_menu():
                    self.assertIn(f, err)
                self.assertIn("pick the SMALLEST that fits", err)
                self.assertIn("CPU-bound", err)
                self.assertIn("~$0.35/hr spot", err)

    def test_an_unknown_flavor_and_the_old_kvm_word_are_refused(self):
        for args in (("up", "2", "compute-24"), ("up", "--flavor", "huge-192"), ("up", "kvm"), ("up", "--flavor")):
            with self.subTest(args=args):
                rc, _, err = self.pbox(*args)
                self.assertEqual(rc, 2, err)
                self.assertEqual(self.launched_types(), [])
        _, _, err = self.pbox("up", "compute-24")
        self.assertIn("unknown flavor 'compute-24'", err)

    def test_each_flavor_launches_only_its_own_types_in_order(self):
        for f, v in script_menu().items():
            with self.subTest(flavor=f):
                rc, _, err = self.pbox("up", "5", f)
                # no capacity anywhere in the fake; a GPU flavor goes on to the fallback sites, none published here
                self.assertEqual(rc, 4 if f.startswith("kvm-gpu-") else 3, err)
                self.assertEqual(self.launched_types(), v[4])
                runs = [c for c in self.calls() if "run-instances" in c]
                self.assertTrue(all("LaunchTemplateName=parallel-box-5,Version=$Latest" in c for c in runs))

    def test_only_an_x86_type_gets_the_published_x86_image(self):
        rc, _, err = self.pbox("up", "2", "kvm-gpu-96")
        self.assertEqual(rc, 4, err)  # home, then the (unpublished) fallback sites
        runs = [c for c in self.calls() if "run-instances" in c]
        self.assertEqual([c[c.index("--instance-type") + 1] for c in runs], ["g4dn.metal"])
        self.assertEqual(runs[0][runs[0].index("--image-id") + 1], "ami-0x86home")
        self.assertIn("LaunchTemplateName=parallel-box-2,Version=$Latest", runs[0])
        for f in ("kvm-gpu-64", "kvm-96", "compute-32"):
            with self.subTest(flavor=f):
                self.pbox("up", "2", f)
                runs = [c for c in self.calls() if "run-instances" in c]
                self.assertTrue(runs)
                self.assertFalse(any("--image-id" in c for c in runs))

    def test_an_x86_type_with_no_published_image_is_refused_not_launched_on_the_arm64_image(self):
        for pointer in ('{"region": "us-east-2", "fallback": {}}', '{"region": "us-east-2", "x86_image": "", "fallback": {}}'):
            with self.subTest(pointer=pointer):
                rc, _, err = self.pbox("up", "2", "kvm-gpu-96", pointer=pointer)
                self.assertEqual(rc, 1, err)  # a real error, not "no capacity"
                self.assertEqual(self.launched_types(), [])
                self.assertIn("g4dn.metal is x86_64 and /infra/parallel-box names no x86_64 image for us-east-2", err)
        # a region override is not the published home site: its image is not used there
        env = dict(os.environ, PATH=f"{self.tmp}:{os.environ['PATH']}", HOME=str(self.tmp), FAKE_LOG=str(self.log),
                   MEMBER_BOX_USER="ejc3", FAKE_RUNNING="", FAKE_POINTER="", PARALLEL_BOX_REGION="us-west-2")
        r = subprocess.run(["bash", str(SCRIPT), "up", "2", "kvm-gpu-96"], capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("no x86_64 image for us-west-2", r.stderr)

    def test_the_flavor_option_and_the_positional_flavor_are_the_same(self):
        self.pbox("up", "2", "--flavor", "storage-48")
        a = self.launched_types()
        self.pbox("up", "2", "--flavor=storage-48")
        b = self.launched_types()
        self.pbox("up", "storage-48", "2")
        c = self.launched_types()
        self.assertEqual(a, ["i8g.12xlarge", "i8ge.12xlarge"])
        self.assertEqual(a, b)
        self.assertEqual(a, c)

    def test_a_member_picks_a_flavor_too_and_never_kvm(self):
        rc, _, err = self.pbox("up", user="colin")
        self.assertEqual(rc, 2, err)
        self.assertEqual(self.calls(), [])
        self.assertNotIn("kvm-", err)
        for f in ("kvm-96", "kvm-gpu-64"):
            rc, _, err = self.pbox("up", f, user="colin")
            self.assertEqual(rc, 2, err)
            self.assertEqual(self.launched_types(), [])
        rc, _, err = self.pbox("up", "memory-32", user="colin")
        self.assertEqual(rc, 3, err)
        self.assertEqual(self.launched_types(), script_menu()["memory-32"][4])

    def test_flavors_lists_the_menu_with_types_and_needs_no_aws(self):
        rc, _, err = self.pbox("flavors")
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls(), [])
        self.assertIn("types, in order: c8g.48xlarge c9g.48xlarge", err)
        self.assertIn("kvm-192", err)
        _, _, err = self.pbox("flavors", user="skevh")
        self.assertNotIn("kvm-", err)

    def test_status_reports_the_flavor_of_a_running_box(self):
        _, out, _ = self.pbox("status", "3", running="r8g.24xlarge")
        self.assertIn("(r8g.24xlarge, flavor memory-96, spot)", out)

    def test_up_of_a_running_box_of_another_flavor_keeps_it_and_says_so(self):
        _, _, err = self.pbox("up", "3", "compute-32", running="r8g.24xlarge")
        self.assertIn("already running as r8g.24xlarge (flavor memory-96), not compute-32; it is kept", err)
        self.assertEqual(self.launched_types(), [])

    def test_a_flavor_is_only_for_up_and_capacity(self):
        rc, _, err = self.pbox("status", "compute-32")
        self.assertEqual(rc, 1)
        self.assertIn("only applies to 'up' and 'capacity'", err)


if __name__ == "__main__":
    unittest.main(buffer=True)
