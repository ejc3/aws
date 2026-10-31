#!/usr/bin/env python3
"""Offline Mac CLI tests: no real AWS calls, SSH connections, or allocations."""
import copy
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
import types
import re

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("macpbox", ROOT / "scripts/macpbox.py")
mac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mac)
ADMIN = {"Account": "123456789012", "Arn": "arn:aws:sts::123456789012:assumed-role/jumpbox/i-0123456789abcdef0"}
DEV = {"Account": ADMIN["Account"], "Arn": "arn:aws:sts::123456789012:assumed-role/dev/example"}
DATA = {"version": 1, "region": mac.REGION, "type": mac.TYPE, "name": "macpbox", "purpose": "macpbox",
        "lease_hours": 24, "launch_template_id": "lt-1234567890abcdef0", "ssh_user": "ec2-user",
        "admin_role_arns": ["arn:aws:iam::123456789012:role/jumpbox"],
        "admin_identity_file": "~/.ssh/fcvm-ec2", "dev_identity_file": "~/.ssh/dev_hop"}


class Cloud:
    def __init__(self):
        self.calls, self.hosts, self.instances = [], [], []
        self.caller, self.primary_name = ADMIN, "jumpbox"
        self.price, self.fail, self.hidden = "1.9700000000", None, False
        self.architecture = "arm64_mac"
        self.pricing_family = "Dedicated Host"
        self.pricing_attributes = {"instanceType": "mac-m4pro", "tenancy": "Host",
                                   "operatingSystem": "Linux", "location": "US West (Oregon)",
                                   "regionCode": "us-west-2"}

    def __call__(self, service, operation, parameters=None, region=mac.REGION, **kwargs):
        p = parameters or {}
        self.calls.append((service, operation, copy.deepcopy(p), region, kwargs))
        if operation == "get-caller-identity":
            return self.caller
        if operation == "get-products":
            filters = {item["Field"]: item["Value"] for item in p.get("Filters", [])}
            # Match the catalog as AWS does, including the hardware SKU's OS placeholder.
            fields = {**self.pricing_attributes, "productFamily": self.pricing_family}
            if any(fields.get(key) != value for key, value in filters.items()):
                return {"PriceList": []}
            return {"PriceList": [json.dumps({"product": {"productFamily": self.pricing_family,
                "attributes": self.pricing_attributes},
                "terms": {"OnDemand": {"example": {"priceDimensions": {"example": {
                    "unit": "Hrs", "pricePerUnit": {"USD": self.price}}}}}}})]}
        if operation == "describe-hosts":
            if "HostIds" in p:
                return {"Hosts": [h for h in self.hosts if h["HostId"] in p["HostIds"]]}
            return {"Hosts": [] if self.hidden else self.hosts}
        if operation == "describe-instances":
            if "InstanceIds" in p:
                return {"Reservations": [{"Instances": [{"InstanceId": p["InstanceIds"][0],
                    "State": {"Name": "running"}, "Tags": [{"Key": "Name", "Value": self.primary_name}]}]}]}
            return {"Reservations": [{"Instances": [] if self.hidden else self.instances}]}
        if operation == "describe-launch-template-versions":
            return {"LaunchTemplateVersions": [{"VersionNumber": 3, "LaunchTemplateData": {
                "ImageId": "ami-1234567890abcdef0", "InstanceType": mac.TYPE, "Placement": {"Tenancy": "host"},
                "NetworkInterfaces": [{"DeviceIndex": 0, "SubnetId": "subnet-example", "Groups": ["sg-example"],
                    "AssociatePublicIpAddress": True, "DeleteOnTermination": True}]}}]}
        if operation == "describe-images":
            return {"Images": [{"State": "available", "Architecture": self.architecture}]}
        if operation == "describe-subnets":
            return {"Subnets": [{"AvailabilityZone": "us-west-2b"}]}
        if operation == "describe-instance-type-offerings":
            return {"InstanceTypeOfferings": [{"InstanceType": mac.TYPE, "Location": "us-west-2b"}]}
        if operation == "list-service-quotas":
            return {"Quotas": [{"QuotaName": "Running Dedicated mac-m4pro Hosts", "Value": 1}]}
        if operation == "allocate-hosts":
            self.hosts = [{"HostId": "h-example", "State": "available", "AllocationTime": "2026-01-01T12:00:00Z",
                "HostProperties": {"InstanceType": mac.TYPE}, "Tags": p["TagSpecifications"][0]["Tags"]}]
            if self.fail == "allocate-hosts":
                self.hidden = True
                raise mac.Error("synthetic lost allocation reply")
            return {"HostIds": ["h-example"]}
        if operation == "run-instances":
            self.instances = [{"InstanceId": "i-abcdef01234567890", "InstanceType": mac.TYPE,
                "Placement": {"Tenancy": "host", "HostId": "h-example"}, "State": {"Name": "pending"},
                "Tags": p["TagSpecifications"][0]["Tags"]}]
            if self.fail == "run-instances":
                self.hidden = True
                raise mac.Error("synthetic lost instance reply")
            return {"Instances": self.instances}
        raise AssertionError("Unexpected AWS operation " + operation)

    def writes(self):
        return [c for c in self.calls if c[1] in ("allocate-hosts", "run-instances")]


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.cloud = Cloud()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "lease"

    def up(self, caller=ADMIN, approved=False, cap=None):
        return mac.up(DATA, caller, approved, cap, self.cloud, self.directory)

    def test_unapproved_up_quotes_nonzero_host_minimum_without_writes(self):
        result = self.up()
        self.assertTrue(result["approval_required"])
        self.assertEqual(mac.Decimal(result["minimum_24h_host_usd"]), mac.Decimal("47.28"))
        self.assertIn("EBS", result["additional_charges"])
        self.assertEqual(self.cloud.writes(), [])
        filters = next(c[2]["Filters"] for c in self.cloud.calls if c[1] == "get-products")
        self.assertIn({"Type": "TERM_MATCH", "Field": "instanceType", "Value": "mac-m4pro"}, filters)
        for approval, cap in ((False, "100"), (True, None)):
            self.assertTrue(self.up(ADMIN, approval, cap)["approval_required"])
        self.assertEqual(self.cloud.writes(), [])

    def test_developer_cannot_allocate_even_with_flags(self):
        with self.assertRaises(mac.Error):
            self.up(DEV, True, "100")
        self.assertEqual(self.cloud.writes(), [])

    def test_backup_and_noninstance_sessions_cannot_allocate(self):
        self.cloud.primary_name = "jumpbox-2"
        with self.assertRaises(mac.Error): self.up(ADMIN, True, "100")
        self.assertEqual(self.cloud.writes(), [])
        self.cloud.caller = {**ADMIN, "Arn": ADMIN["Arn"].rsplit("/", 1)[0] + "/federated-session"}
        with self.assertRaises(mac.Error): self.up(self.cloud.caller, True, "100")
        self.assertEqual(self.cloud.writes(), [])

    def test_price_cap_and_zero_instance_price_refuse_without_billing(self):
        with self.assertRaises(mac.Error): self.up(ADMIN, True, "1.96")
        self.cloud.price = "0"
        with self.assertRaises(mac.Error): self.up(ADMIN, True, "100")
        self.assertEqual(self.cloud.writes(), [])

    def test_quote_accepts_the_real_linux_labeled_dedicated_host_catalog(self):
        try:
            result = mac.quote(self.cloud)
        except mac.Error as error:
            self.fail("The real Dedicated Host catalog must produce a quote: " + str(error))
        self.assertEqual(mac.Decimal(result["hourly_host_usd"]), mac.Decimal("1.97"))
        self.assertEqual(mac.Decimal(result["minimum_24h_host_usd"]), mac.Decimal("47.28"))
        filters = self.cloud.calls[-1][2]["Filters"]
        self.assertIn({"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Dedicated Host"}, filters)
        self.assertIn({"Type": "TERM_MATCH", "Field": "regionCode", "Value": "us-west-2"}, filters)
        self.assertNotIn("operatingSystem", {item["Field"] for item in filters})
        self.assertEqual(self.cloud.writes(), [])

    def test_quote_rejects_compute_zero_or_mismatched_catalog_even_if_api_returns_it(self):
        valid = self.cloud("pricing", "get-products")["PriceList"][0]
        original = json.loads(valid)
        changes = [("productFamily", "Compute Instance"), ("regionCode", "us-east-1"),
                   ("location", "US East (N. Virginia)"), ("tenancy", "Shared"),
                   ("instanceType", "mac-m4"), ("price", "0"), ("price", "-1"),
                   ("price", "Infinity"), ("price", "NaN")]
        for field, value in changes:
            with self.subTest(field=field, value=value):
                product = copy.deepcopy(original)
                if field == "productFamily":
                    product["product"][field] = value
                elif field == "price":
                    product["terms"]["OnDemand"]["example"]["priceDimensions"]["example"]["pricePerUnit"]["USD"] = value
                else:
                    product["product"]["attributes"][field] = value
                with self.assertRaises(mac.Error):
                    mac.quote(lambda *args, **kwargs: {"PriceList": [json.dumps(product)]})
        self.assertEqual(self.cloud.writes(), [])

    def test_real_mac_architecture_and_fixed_lt_network_are_used(self):
        result = self.up(ADMIN, True, "2")
        self.assertTrue(result["launched"])
        self.assertEqual(result["hosts"][0]["earliest_release"], "2026-01-02T12:00:00+00:00")
        allocation, launch = self.cloud.writes()
        self.assertEqual(allocation[2]["AvailabilityZone"], "us-west-2b")
        self.assertEqual(launch[2]["LaunchTemplate"], {"LaunchTemplateId": DATA["launch_template_id"], "Version": "3"})
        self.assertEqual(launch[2]["Placement"], {"Tenancy": "host", "HostId": "h-example"})
        self.assertNotIn("NetworkInterfaces", launch[2])
        self.assertTrue(all(c[4].get("imds") is True for c in self.cloud.writes()))
        primary_lookup = next(c for c in self.cloud.calls if c[1] == "describe-instances" and "InstanceIds" in c[2])
        self.assertEqual(primary_lookup[3], "us-west-1")
        journal = json.loads((self.directory / "pending.json").read_text())
        self.assertEqual(allocation[2]["ClientToken"], "macpbox-host-" + journal["lease"])
        self.assertEqual(launch[2]["ClientToken"], "macpbox-run-" + journal["lease"])
        self.assertEqual(journal["earliest_release"], result["hosts"][0]["earliest_release"])

    def test_warm_running_or_stopped_host_reuse_never_writes(self):
        self.up(ADMIN, True, "2")
        self.cloud.instances[0]["State"]["Name"] = "stopped"
        self.cloud.calls.clear()
        self.assertTrue(self.up(DEV)["reused_without_mutation"])
        self.assertEqual(self.cloud.writes(), [])

    def test_uncertain_allocate_or_run_never_allocates_a_second_host(self):
        for phase in ("allocate-hosts", "run-instances"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as location:
                self.directory = Path(location) / "lease"
                self.cloud = Cloud()
                self.cloud.fail = phase
                with self.assertRaises(mac.Error): self.up(ADMIN, True, "2")
                before = len(self.cloud.writes())
                with self.assertRaises(mac.Error): self.up(ADMIN, True, "2")
                self.assertEqual(len(self.cloud.writes()), before)

    def test_even_successful_but_hidden_host_does_not_allow_second_allocation(self):
        self.up(ADMIN, True, "2")
        self.cloud.hidden = True
        before = len(self.cloud.writes())
        with self.assertRaises(mac.Error): self.up(ADMIN, True, "2")
        self.assertEqual(len(self.cloud.writes()), before)

    def test_observed_release_is_retained_when_aws_history_later_disappears(self):
        self.up(ADMIN, True, "2")
        self.cloud.instances.clear()
        self.cloud.hosts[0]["State"] = "released"
        with mac.LeaseLock(self.directory) as lock:
            self.assertTrue(mac.confirm_previous_release(lock, ADMIN["Account"], self.cloud))
        journal = json.loads((self.directory / "pending.json").read_text())
        self.assertEqual(journal["release_confirmation"]["host_id"], journal["host_id"])
        self.cloud.hosts.clear()
        before = len(self.cloud.writes())
        self.assertTrue(self.up()["approval_required"])
        self.assertEqual(len(self.cloud.writes()), before)

    def test_expired_or_missing_history_without_recorded_proof_never_allocates(self):
        self.up(ADMIN, True, "2")
        self.cloud.hosts.clear()
        self.cloud.instances.clear()
        before = len(self.cloud.writes())
        with self.assertRaisesRegex(mac.Error, "CloudTrail"):
            self.up(ADMIN, True, "2")
        self.assertEqual(len(self.cloud.writes()), before)
        with mac.LeaseLock(self.directory) as lock:
            self.assertFalse(mac.confirm_previous_release(lock, ADMIN["Account"],
                lambda *a, **k: (_ for _ in ()).throw(mac.Error("InvalidHostID.NotFound"))))
        self.assertNotIn("release_confirmation", json.loads((self.directory / "pending.json").read_text()))

    def test_cached_release_proof_cannot_cross_account_region_or_host(self):
        self.up(ADMIN, True, "2")
        self.cloud.instances.clear()
        self.cloud.hosts[0]["State"] = "released"
        with mac.LeaseLock(self.directory) as lock:
            self.assertTrue(mac.confirm_previous_release(lock, ADMIN["Account"], self.cloud))
            original = lock.read()
            self.cloud.hosts.clear()
            for key, value in (("account", "000000000000"), ("region", "us-east-1"), ("host_id", "h-other")):
                changed = copy.deepcopy(original)
                changed["release_confirmation"][key] = value
                lock.write(changed)
                self.assertFalse(mac.confirm_previous_release(lock, ADMIN["Account"], self.cloud))

    def test_paid_identity_cannot_come_from_inherited_profiles_or_endpoints(self):
        response = types.SimpleNamespace(returncode=0, stdout="{}", stderr="")
        with patch.dict(os.environ, {"AWS_PROFILE": "example", "AWS_ACCESS_KEY_ID": "not-a-key",
                                   "AWS_ENDPOINT_URL": "https://example.invalid"}), \
                patch.object(mac.subprocess, "run", return_value=response) as run:
            mac.aws("sts", "get-caller-identity", imds=True)
        environment = run.call_args.kwargs["env"]
        self.assertNotIn("AWS_PROFILE", environment)
        self.assertNotIn("AWS_ACCESS_KEY_ID", environment)
        self.assertNotIn("AWS_ENDPOINT_URL", environment)
        self.assertEqual(environment["AWS_CONFIG_FILE"], "/dev/null")
        self.assertEqual(environment["AWS_SHARED_CREDENTIALS_FILE"], "/dev/null")
        self.assertFalse(run.call_args.kwargs.get("shell", False))


class WorkspaceTests(unittest.TestCase):
    def test_invalid_id_cannot_reach_ssh_or_aws(self):
        for value in ("../x", "x/y", ".", "-option", "x;touch", "x\nnext", "a" * 49):
            with self.subTest(value=value), self.assertRaises(mac.Error):
                mac.workspace(value, DATA, DEV, lambda *a, **k: self.fail("AWS called"),
                              lambda *a, **k: self.fail("SSH called"))

    def run_workspace(self, root, identifier="example-agent"):
        script = mac.WORKSPACE.replace("/Users/ec2-user", str(root))
        return subprocess.run([sys.executable, "-", identifier], input=script, text=True, capture_output=True)

    def test_unique_workspace_keeps_build_and_cache_settings_local(self):
        with tempfile.TemporaryDirectory() as location:
            root = Path(location)
            self.assertEqual(self.run_workspace(root).returncode, 0)
            base = root / "work/example-agent"
            for name in ("repos", "build", "cache", "tmp", "bin"):
                self.assertTrue((base / name).is_dir())
            self.assertIn(str(base / "cache/cargo"), (base / "env").read_text())
            self.assertIn("-derivedDataPath", (base / "bin/xcodebuild").read_text())
            self.assertNotEqual(self.run_workspace(root).returncode, 0)

    def test_workspace_rejects_symlinked_parent_or_agent(self):
        with tempfile.TemporaryDirectory() as location:
            root = Path(location)
            target = root / "untouched"
            target.mkdir()
            (root / "work").symlink_to(target, target_is_directory=True)
            self.assertNotEqual(self.run_workspace(root).returncode, 0)
            self.assertEqual(list(target.iterdir()), [])
            (root / "work").unlink()
            (root / "work").mkdir()
            (root / "work/example-agent").symlink_to(target, target_is_directory=True)
            self.assertNotEqual(self.run_workspace(root).returncode, 0)
            self.assertEqual(list(target.iterdir()), [])


class InstallationTests(unittest.TestCase):
    def test_installer_follows_replacement_names_and_excludes_disabled_hosts(self):
        source = (ROOT / "mac-pbox-tools.tf").read_text()
        association = source.split('resource "aws_ssm_association" "macpbox_tools" {', 1)[1]
        self.assertIn('key    = "tag:Name"', association)
        self.assertIn('values = local.macpbox_tool_names', association)
        self.assertIn('count = length(local.macpbox_tool_names) > 0 ? 1 : 0', association)
        self.assertNotIn('InstanceIds', association)
        names = source.split('macpbox_tool_names = concat(', 1)[1].split('\n  )', 1)[0]
        for gate, name in (("enable_jumpbox", "jumpbox"), ("enable_jumpbox_2", "jumpbox-2"),
                           ("enable_firecracker_instance", "fcvm-metal-arm"),
                           ("enable_x86_dev_instance", "fcvm-metal-x86")):
            self.assertIn(f'var.{gate} ? ["{name}"] : []', names)
        self.assertIn('var.enable_nextjs_ct || var.enable_nextjs_dev ? ["nextjs-dev"] : []', names)
        self.assertNotIn('.id', names)

    def test_developer_discovery_policy_grants_no_write_or_broad_parameter_read(self):
        source = (ROOT / "mac-pbox-tools.tf").read_text()
        actions = set(re.findall(r'"((?:ec2|ssm|pricing):[A-Za-z]+)"', source))
        self.assertEqual(actions, {"ec2:DescribeHosts", "ssm:GetParameter", "pricing:GetProducts",
                                   "ssm:DescribeInstanceInformation"})
        self.assertRegex(source, r'Action\s*=\s*"ssm:GetParameter"\s+Resource\s*=\s*aws_ssm_parameter\.macpbox\.arn')
        self.assertIn('systemd-run --machine="$user"', source)
        self.assertIn('/bin/ln -sfn /opt/nextjs-ct/macpbox /usr/local/bin/macpbox', source)

    def test_real_installer_checks_hash_before_atomic_replacement_and_cleans_failure(self):
        source = (ROOT / "mac-pbox-tools.tf").read_text()
        script = textwrap.dedent(re.search(r'macpbox_tool_install = <<-SH\n(.*?)\n  SH', source, re.S)[1])
        payload = (ROOT / "scripts/macpbox.py").read_bytes()
        for correct in (False, True):
            with self.subTest(correct=correct), tempfile.TemporaryDirectory() as location:
                root = Path(location)
                binary = root / "bin"
                binary.mkdir()
                target = binary / "macpbox"
                target.write_text("previous executable\n")
                fake = root / "fake"
                fake.mkdir()
                (fake / "chown").write_text("#!/bin/sh\nexit 0\n")
                (fake / "chown").chmod(0o755)
                digest = hashlib.sha256(payload).hexdigest() if correct else "0" * 64
                rendered = script.replace('${base64encode(file("${path.module}/scripts/macpbox.py"))}', base64.b64encode(payload).decode())
                rendered = rendered.replace('${substr(filesha256("${path.module}/scripts/macpbox.py"), 0, 12)}', digest[:12])
                rendered = rendered.replace('${filesha256("${path.module}/scripts/macpbox.py")}', digest)
                rendered = rendered.replace('${join(" ", local.nextjs_users)}', "")
                rendered = rendered.replace("/usr/local/bin", str(binary)).replace("/var/lib/nextjs-ct/shared", str(root / "absent-shared"))
                self.assertNotIn("${", rendered)
                environment = dict(os.environ, PATH=str(fake) + os.pathsep + os.environ["PATH"])
                run = subprocess.run(["/bin/sh", "-s"], input=rendered, text=True, capture_output=True, env=environment)
                self.assertEqual(run.returncode == 0, correct)
                self.assertEqual(target.read_bytes(), payload if correct else b"previous executable\n")
                self.assertEqual(list(binary.glob(".macpbox.*")), [])


if __name__ == "__main__":
    unittest.main(buffer=True)
