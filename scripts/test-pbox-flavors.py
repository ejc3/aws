#!/usr/bin/env python3
"""pbox flavors (pbox-flavors.tf, scripts/parallel-box.sh): `up` launches only a flavor the caller names, each flavor
tries only its own instance types in order, nothing is under 24 vCPU, and IAM allows exactly the flavors' types.

Offline: reads the Terraform and the script, and runs the real script against a fake `aws` and `ssh` on PATH. Live proof
is `aws ec2 describe-instance-type-offerings` in us-east-2c (every type but g5g.metal offered, checked 2026-10-19; g5g.metal
in us-west-2a/b/c, checked 2026-10-22) and `aws iam simulate-principal-policy` against nextjs-dev-role after the apply."""
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

# vCPU of each size suffix the flavors use
SIZE_VCPU = {"8xlarge": 32, "12xlarge": 48, "24xlarge": 96, "48xlarge": 192, "metal-24xl": 96, "metal-48xl": 192, "metal": 64}


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
        self.assertEqual([n for n in names if n.startswith("kvm-")], ["kvm-96", "kvm-192", "kvm-gpu-64"])

    def test_no_flavor_is_under_24_vcpu_and_every_type_has_the_flavors_size(self):
        for f, (vcpu, mem, _, _, types) in script_menu().items():
            with self.subTest(flavor=f):
                self.assertGreaterEqual(int(vcpu), 24)
                self.assertEqual(int(f.rsplit("-", 1)[1]), int(vcpu))  # the name is the size
                for t in types:
                    self.assertEqual(SIZE_VCPU[t.split(".", 1)[1]], int(vcpu), t)

    def test_every_type_is_graviton_and_in_exactly_one_flavor(self):
        seen = {}
        for f, (_, _, _, _, types) in script_menu().items():
            # spot capacity varies by type: never one alone, but for g5g.metal, the only Graviton metal type with a GPU
            if f != "kvm-gpu-64":
                self.assertGreaterEqual(len(types), 2, f)
            for t in types:
                # Graviton names: family letter(s), generation, "g", options -- the AMI is arm64
                self.assertRegex(t, r"^[a-z]+\d+g[a-z]*\.", t)
                self.assertNotIn(t, seen, "%s is in %s and %s" % (t, seen.get(t), f))
                seen[t] = f
        self.assertNotIn("i7ie", " ".join(seen))  # x86

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

    def test_the_gpu_flavor_is_g5g_metal_and_the_owners_only(self):
        self.assertEqual(script_menu()["kvm-gpu-64"], ("64", "128", "-", "0.60", ["g5g.metal"]))
        gpu_types = {t for f, v in script_menu().items() if f.startswith("kvm-gpu-") for t in v[4]}
        self.assertEqual(gpu_types, {"g5g.metal"})
        self.assertIn('kvm-gpu) echo "GPU passthrough into fcvm', PBOX)
        self.assertIn("--regions us-west-2", PBOX)

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
        self.assertEqual(len(re.findall(r"^  statement \{", doc, re.M)), 2)
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
        # rendered with ARN-length placeholders: about 3,300 of the 6,144 characters
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
                           "StringNotEquals": {"ec2:InstanceType": member}}}]}
        self.assertLess(len(json.dumps(doc, separators=(",", ":"))), 5000)

    def test_the_spot_quota_is_still_sized_for_the_largest_flavor(self):
        self.assertEqual(max(int(v[0]) for v in script_menu().values()), 192)
        self.assertIn("parallel_box_max_vcpus = 192", OHIO)


class GpuQuota(unittest.TestCase):
    def test_the_g_spot_quota_in_us_west_2_covers_one_gpu_box_and_never_asks_below_the_current_value(self):
        self.assertIn("pbox_gpu_spot_vcpus = 64", FLAVORS_TF)
        self.assertEqual(int(script_menu()["kvm-gpu-64"][0]), 64)
        for kind in ("data", "resource"):
            b = block(FLAVORS_TF, '%s "aws_servicequotas_service_quota" "west2_spot_g"' % kind)
            self.assertIn("provider     = aws.west2", b)
            self.assertIn('quota_code   = "L-3819A6DF"', b)
        b = block(FLAVORS_TF, 'resource "aws_servicequotas_service_quota" "west2_spot_g"')
        self.assertIn("value        = max(local.pbox_gpu_spot_vcpus, data.aws_servicequotas_service_quota.west2_spot_g.value)", b)


class GpuCheckRuns(unittest.TestCase):
    """The script's GPU_CHECK, run against a fake sysfs: ready only when every GPU is vfio-pci's with its group node, and
    its whole IOMMU group is unbound or vfio-pci, pci-stub or pcieport."""

    def run_check(self, members):
        import os, subprocess, tempfile
        script = (Path(__file__).resolve().parent / "parallel-box.sh").read_text()
        check = re.search(r"^GPU_CHECK='(.*?)'$", script, re.S | re.M).group(1)
        with tempfile.TemporaryDirectory() as root:
            r = Path(root)
            (r / "bin").mkdir()
            for tool in ("timeout", "cloud-init"):
                (r / "bin" / tool).write_text("#!/bin/sh\nexit 0\n")
                (r / "bin" / tool).chmod(0o755)
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
            body = check.replace("/sys/", root + "/sys/").replace('[ -c "/dev/vfio/', '[ -e "' + root + "/dev/vfio/")
            env = dict(os.environ, PATH=str(r / "bin") + ":/usr/bin:/bin")
            return subprocess.run(["bash", "-c", body], env=env, capture_output=True, text=True).returncode

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

    def test_the_home_and_fallback_boots_run_it_once_after_apt_and_the_members_do_not(self):
        for text in (LAUNCH, FALLBACK):
            self.assertEqual(text.count("${local.pbox_gpu_vfio_setup}"), 1)
            self.assertLess(text.index("apt-get update"), text.index("${local.pbox_gpu_vfio_setup}"))
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
        self.assertIn('"ubuntu@$IP" "$GPU_CHECK" >&2; then', wait)
        self.assertLess(wait.index("test -c /dev/kvm"), wait.index("$GPU_CHECK"))


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
            self.assertLess(text.index("apt-get update"), text.index("${local.pbox_local_nvme_setup}"))
            self.assertLess(text.index("mount \"$DEV\" /mnt/work"), text.index("${local.pbox_local_nvme_setup}"))


# The script against a fake aws: every call is logged; run-instances always finds no capacity.
FAKE_AWS = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
a = " ".join(args)
open(os.environ["FAKE_LOG"], "a").write(json.dumps(args) + "\n")
if "get-parameter" in a:
    print('{"region": "us-east-2", "fallback": {}}')
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

    def pbox(self, *args, user="ejc3", running=""):
        if self.log.exists():
            self.log.unlink()
        env = dict(os.environ, PATH=f"{self.tmp}:{os.environ['PATH']}", HOME=str(self.tmp), FAKE_LOG=str(self.log),
                   MEMBER_BOX_USER=user, FAKE_RUNNING=running)
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
                self.assertEqual(rc, 3, err)  # no capacity anywhere in the fake
                self.assertEqual(self.launched_types(), v[4])
                runs = [c for c in self.calls() if "run-instances" in c]
                self.assertTrue(all("LaunchTemplateName=parallel-box-5,Version=$Latest" in c for c in runs))

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
