#!/usr/bin/env python3
"""Prebuilt app runner images (runner-app-image.tf, runner-app/image_builder.py, image-build.sh, images/).

Runs the real image_builder.py against a fake EC2 and SSM, the image script's own functions against a fake curl and
local archives, and reads the Terraform for the boundaries. Nothing here touches AWS or the network.
"""
import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILDER_PY = ROOT / "runner-app" / "image_builder.py"
IMAGE_BUILD = ROOT / "runner-app" / "image-build.sh"
IMAGINE_SCRIPT = ROOT / "runner-app" / "images" / "imagine-runner.sh"
NODE_WEB_SCRIPT = ROOT / "runner-app" / "images" / "node-web-runner.sh"
# Read where the tests use them, and empty when missing, so each test fails on its own on a tree without the pipeline.
IMAGE_TF = (ROOT / "runner-app-image.tf").read_text() if (ROOT / "runner-app-image.tf").exists() else ""
APP_TF = (ROOT / "runner-app.tf").read_text()

ACCOUNT, OTHER = "123456789012", "210987654321"
PURPOSE, BUILDER = "github-app-runner-image", "github-app-runner-image-builder"
NOW = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)
VERSIONS = {"RUST_TOOLCHAIN": "1.93.0", "RUST_COMPONENTS": "rustfmt,clippy", "RUST_TARGETS": "wasm32-unknown-unknown",
            "NODE_VERSION": "22.23.3", "PNPM_VERSION": "10.29.1", "OTP_VERSION": "29.1.1", "ELIXIR_VERSION": "1.20.4",
            "PLAYWRIGHT_VERSION": "1.63.0", "LIBSQL_IMAGE": "ghcr.io/tursodatabase/libsql-server:v0.24.32"}
IMAGES = {"imagine-runner": {"repo": "ejc3/imagine", "script": "imagine-runner.sh", "versions": VERSIONS,
                             "builder_types": ["c7a.2xlarge", "c7i.2xlarge"]}}
SUBNETS = [{"subnet_id": "subnet-c", "availability_zone": "us-west-1c"},
           {"subnet_id": "subnet-a", "availability_zone": "us-west-1a"}]
ENV = {"IMAGES": json.dumps(IMAGES), "LAUNCH_SUBNETS": json.dumps(SUBNETS), "SECURITY_GROUP_ID": "sg-app",
       "INSTANCE_PROFILE": "github-app-runner-image-builder-profile", "ACCOUNT_ID": ACCOUNT, "KEEP_IMAGES": "3"}


class Refused(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def tags(**kw):
    return [{"Key": k, "Value": v} for k, v in kw.items() if v is not None]


def has(resource, name, values):
    if name.startswith("tag:"):
        return any(t["Key"] == name[4:] and t["Value"] in values for t in resource.get("Tags", []))
    return {"state": resource.get("State"), "instance-state-name": resource.get("State", {}).get("Name")
            if isinstance(resource.get("State"), dict) else None}[name] in values


class FakeEC2:
    def __init__(self):
        self.instances, self.images, self.snapshots = [], [], []
        self.launched, self.attempts, self.created, self.terminated = [], [], [], []
        self.deregistered, self.deleted_snapshots, self.refuse = [], [], set()
        self.next = 1

    def get_paginator(self, name):
        assert name == "describe_instances"
        fake = self

        class P:
            def paginate(self, Filters):
                out = [i for i in fake.instances if all(has(i, f["Name"], f["Values"]) for f in Filters)]
                return [{"Reservations": [{"Instances": out}]}]
        return P()

    def describe_images(self, Owners, Filters):
        return {"Images": [i for i in self.images if i["OwnerId"] in Owners
                           and all(has(i, f["Name"], f["Values"]) for f in Filters)]}

    def describe_snapshots(self, OwnerIds, Filters):
        return {"Snapshots": [s for s in self.snapshots if s["OwnerId"] in OwnerIds
                              and all(has(s, f["Name"], f["Values"]) for f in Filters)]}

    def run_instances(self, **kw):
        self.attempts.append(kw)
        if kw["InstanceType"] in self.refuse:
            raise Refused("InsufficientInstanceCapacity")
        iid = "i-%017x" % self.next
        self.next += 1
        self.instances.append({"InstanceId": iid, "State": {"Name": "pending"}, "LaunchTime": NOW,
                               "Tags": kw["TagSpecifications"][0]["Tags"]})
        self.launched.append(kw)
        return {"Instances": [{"InstanceId": iid}]}

    def create_image(self, **kw):
        self.created.append(kw)
        return {"ImageId": "ami-made%d" % len(self.created)}

    def terminate_instances(self, InstanceIds):
        self.terminated += InstanceIds

    def get_console_output(self, InstanceId, Latest):
        return {"Output": f"console of {InstanceId}: github-app-runner-image: FAILED (100)"}

    def deregister_image(self, ImageId):
        self.deregistered.append(ImageId)

    def delete_snapshot(self, SnapshotId):
        self.deleted_snapshots.append(SnapshotId)


class FakeSSM:
    def get_parameter(self, Name):
        assert Name == "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id", Name
        return {"Parameter": {"Value": "ami-canonical"}}


def load_builder():
    ec2, ssm = FakeEC2(), FakeSSM()
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda name, region_name=None, config=None: {"ec2": ec2, "ssm": ssm}[name]
    fake_botocore, fake_config = types.ModuleType("botocore"), types.ModuleType("botocore.config")
    fake_config.Config = lambda **kw: types.SimpleNamespace(**kw)
    fake_botocore.config = fake_config
    sys.modules["boto3"], sys.modules["botocore"], sys.modules["botocore.config"] = fake_boto3, fake_botocore, fake_config
    os.environ.update(ENV)
    spec = importlib.util.spec_from_file_location("image_builder_under_test", BUILDER_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.now = lambda: NOW
    return mod, ec2


def builder(iid, state, status, age_min=20, name="imagine-runner"):
    return {"InstanceId": iid, "State": {"Name": state}, "LaunchTime": NOW - timedelta(minutes=age_min),
            "Tags": tags(Name=BUILDER, Image=name, Repo="ejc3/imagine", BuildStatus=status)}


def image(image_id, created, state="available", owner=ACCOUNT, purpose=PURPOSE, name="imagine-runner", built_from=None):
    return {"ImageId": image_id, "OwnerId": owner, "CreationDate": created, "State": state,
            "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"SnapshotId": "snap-" + image_id[4:]}}],
            "Tags": tags(Purpose=purpose, Image=name, Repo="ejc3/imagine", BuiltFrom=built_from)}


def call(mod, event):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            return mod.handler(event, None), out.getvalue(), None
        except Exception as error:  # noqa: BLE001 - the test inspects it
            return None, out.getvalue(), error


class BuildTests(unittest.TestCase):
    def test_a_build_launches_one_on_demand_builder_from_canonicals_image(self):
        mod, ec2 = load_builder()
        result, log, error = call(mod, {"build": True})
        self.assertIsNone(error)
        self.assertEqual(result, {"imagine-runner": "started"})
        (kw,) = ec2.launched
        self.assertEqual(kw["ImageId"], "ami-canonical")
        self.assertEqual(kw["InstanceType"], "c7a.2xlarge")
        self.assertNotIn("InstanceMarketOptions", kw, "on demand: a one-time spot host cannot be stopped and imaged")
        self.assertEqual(kw["InstanceInitiatedShutdownBehavior"], "stop")
        self.assertEqual(kw["IamInstanceProfile"], {"Name": "github-app-runner-image-builder-profile"})
        self.assertEqual(kw["NetworkInterfaces"][0]["Groups"], ["sg-app"])
        self.assertEqual(kw["MetadataOptions"]["HttpTokens"], "required")
        ebs = kw["BlockDeviceMappings"][0]["Ebs"]
        self.assertEqual((ebs["Encrypted"], ebs["VolumeType"], ebs["VolumeSize"]), (True, "gp3", 30))
        self.assertEqual({t["Key"]: t["Value"] for t in kw["TagSpecifications"][0]["Tags"]},
                         {"Name": BUILDER, "Image": "imagine-runner", "Repo": "ejc3/imagine", "BuildStatus": "starting"})
        self.assertEqual([s["Tags"] for s in kw["TagSpecifications"][1:]], [[{"Key": "Name", "Value": BUILDER}]] * 2)

    def test_the_builders_user_data_is_the_bootstrap_prepare_only_then_the_image_script_at_the_pins(self):
        mod, ec2 = load_builder()
        call(mod, {"build": ["imagine-runner"]})
        raw = ec2.launched[0]["UserData"]
        self.assertLess(len(raw), 16384, "EC2's user data limit")
        script = gzip.decompress(raw).decode()
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)
        for key, value in VERSIONS.items():
            self.assertIn(f"export {key}='{value}'\n", script)
        self.assertNotIn("@@", script.replace("'@@REPO@@'", ""), "every placeholder is filled")
        bootstrap = (ROOT / "runner-app" / "bootstrap.sh").read_text()
        self.assertIn(bootstrap.replace("@@REPO@@", "ejc3/imagine").replace("@@LABELS@@", "").rstrip("\n"), script,
                      "the image installs exactly what a job host's bootstrap installs, the runner pin included")
        self.assertIn(IMAGINE_SCRIPT.read_text().rstrip("\n"), script)
        order = [script.index(s) for s in ("GITHUB_APP_RUNNER_PREPARE_ONLY=1 bash /root/github-app-runner-bootstrap.sh",
                                           "bash /root/github-app-runner-image.sh", "truncate -s 0 /etc/machine-id",
                                           "Tags=[{'Key': 'BuildStatus', 'Value': 'complete'}]")]
        self.assertEqual(order, sorted(order), "bootstrap, toolchains, scrub, and only then complete")
        self.assertIn("sync; shutdown -h now' EXIT", script, "every outcome powers off")

    def test_a_build_already_in_flight_is_not_started_twice(self):
        mod, ec2 = load_builder()
        for state in ("pending", "running", "stopping", "stopped"):
            ec2.instances = [builder("i-flight", state, "starting" if state != "stopped" else "complete")]
            result, _, error = call(mod, {"build": True})
            self.assertIsNone(error)
            self.assertEqual(ec2.launched, [], state)

    def test_a_refused_pool_falls_through_to_the_next(self):
        mod, ec2 = load_builder()
        ec2.refuse = {"c7a.2xlarge"}
        call(mod, {"build": True})
        self.assertEqual([a["InstanceType"] for a in ec2.attempts], ["c7a.2xlarge", "c7i.2xlarge"])
        self.assertEqual(len(ec2.launched), 1)

    def test_an_unknown_image_or_a_pin_that_could_reach_the_shell_is_refused(self):
        mod, ec2 = load_builder()
        _, _, error = call(mod, {"build": ["dolphin-runner"]})
        self.assertIsInstance(error, ValueError)
        for bad in ("1.93.0'; curl x | sh; '", "1.93 0", "$(id)"):
            os.environ["IMAGES"] = json.dumps({"imagine-runner": dict(IMAGES["imagine-runner"],
                                                                      versions=dict(VERSIONS, RUST_TOOLCHAIN=bad))})
            _, _, error = call(mod, {"build": True})
            self.assertIsInstance(error, ValueError, bad)
        os.environ["IMAGES"] = ENV["IMAGES"]
        self.assertEqual(ec2.launched, [])

    def test_the_schedule_alone_starts_nothing(self):
        mod, ec2 = load_builder()
        result, _, error = call(mod, {})
        self.assertIsNone(error)
        self.assertEqual((result, ec2.launched), ({"imagine-runner": "advanced"}, []))


class AdvanceTests(unittest.TestCase):
    def test_a_stopped_complete_builder_is_imaged_with_the_pipelines_tags(self):
        mod, ec2 = load_builder()
        ec2.instances = [builder("i-done", "stopped", "complete")]
        _, _, error = call(mod, {})
        self.assertIsNone(error)
        (kw,) = ec2.created
        self.assertEqual(kw["InstanceId"], "i-done")
        self.assertEqual(kw["Name"], "imagine-runner-20261019-0900")
        image_tags = {t["Key"]: t["Value"] for t in kw["TagSpecifications"][0]["Tags"]}
        snapshot_tags = {t["Key"]: t["Value"] for t in kw["TagSpecifications"][1]["Tags"]}
        self.assertEqual([s["ResourceType"] for s in kw["TagSpecifications"]], ["image", "snapshot"])
        for t in (image_tags, snapshot_tags):
            self.assertEqual((t["Purpose"], t["Image"], t["Repo"]), (PURPOSE, "imagine-runner", "ejc3/imagine"))
        self.assertEqual(image_tags["BuiltFrom"], "i-done")
        self.assertNotEqual(image_tags["Purpose"], "github-runner", "fcvm's launcher and replicator pick that tag")
        self.assertLessEqual(len(kw["Description"]), 255)
        self.assertEqual(ec2.terminated, [], "the builder stays until its image is available")

    def test_an_image_being_made_is_waited_for_and_not_made_again(self):
        mod, ec2 = load_builder()
        ec2.instances = [builder("i-done", "stopped", "complete")]
        ec2.images = [image("ami-made1", "2026-10-19T08:55:00.000Z", state="pending", built_from="i-done")]
        _, _, error = call(mod, {})
        self.assertIsNone(error)
        self.assertEqual((ec2.created, ec2.terminated), ([], []))

    def test_an_available_image_ends_its_build_and_the_newest_three_are_kept(self):
        mod, ec2 = load_builder()
        ec2.instances = [builder("i-done", "stopped", "complete")]
        ec2.images = [image("ami-w1", "2026-09-28T09:20:00.000Z"), image("ami-w2", "2026-10-05T09:20:00.000Z"),
                      image("ami-w3", "2026-10-12T09:20:00.000Z"), image("ami-w4", "2026-10-15T09:20:00.000Z"),
                      image("ami-new", "2026-10-19T08:40:00.000Z", built_from="i-done"),
                      image("ami-broken", "2026-10-01T09:20:00.000Z", state="failed"),
                      # Never this pipeline's to retire: another name, no Purpose tag, fcvm's tag, another account.
                      image("ami-dolphin", "2026-01-01T00:00:00.000Z", name="dolphin-runner"),
                      image("ami-untagged", "2026-01-01T00:00:00.000Z", purpose=None),
                      image("ami-fcvm", "2026-01-01T00:00:00.000Z", purpose="github-runner"),
                      image("ami-foreign", "2026-01-01T00:00:00.000Z", owner=OTHER)]
        _, log, error = call(mod, {})
        self.assertIsNone(error)
        self.assertEqual(ec2.terminated, ["i-done"])
        self.assertEqual(sorted(ec2.deregistered), ["ami-broken", "ami-w1", "ami-w2"])
        self.assertEqual(sorted(ec2.deleted_snapshots), ["snap-broken", "snap-w1", "snap-w2"])

    def test_a_snapshot_no_image_names_is_deleted_once_a_day_old(self):
        mod, ec2 = load_builder()
        ec2.images = [image("ami-w4", "2026-10-15T09:20:00.000Z")]
        snap = lambda sid, age, **kw: {"SnapshotId": sid, "OwnerId": kw.get("owner", ACCOUNT), "State": "completed",
                                       "StartTime": NOW - age, "Tags": tags(Purpose=kw.get("purpose", PURPOSE),
                                                                            Image="imagine-runner")}
        ec2.snapshots = [snap("snap-w4", timedelta(days=4)), snap("snap-orphan", timedelta(days=3)),
                         snap("snap-young", timedelta(hours=2)),
                         snap("snap-foreign", timedelta(days=3), owner=OTHER),
                         snap("snap-untagged", timedelta(days=3), purpose="github-runner")]
        _, _, error = call(mod, {})
        self.assertIsNone(error)
        self.assertEqual(ec2.deleted_snapshots, ["snap-orphan"])

    def test_a_builder_that_stopped_without_completing_is_terminated_and_fails_loudly(self):
        mod, ec2 = load_builder()
        ec2.instances = [builder("i-broke", "stopped", "starting")]
        _, log, error = call(mod, {})
        self.assertEqual(type(error).__name__, "BuildFailed")
        self.assertIn("stopped without completing (BuildStatus=starting)", str(error))
        self.assertIn("console of i-broke: github-app-runner-image: FAILED", log)
        self.assertEqual((ec2.terminated, ec2.created), (["i-broke"], []))

    def test_a_builder_past_its_time_is_terminated(self):
        mod, ec2 = load_builder()
        ec2.instances = [builder("i-slow", "running", "starting", age_min=61), builder("i-ok", "running", "starting", age_min=30)]
        _, _, error = call(mod, {})
        self.assertIn("i-slow is still running", str(error))
        self.assertEqual(ec2.terminated, ["i-slow"])

    def test_an_image_that_failed_terminates_its_builder_and_fails_loudly(self):
        mod, ec2 = load_builder()
        ec2.instances = [builder("i-done", "stopped", "complete")]
        ec2.images = [image("ami-made1", "2026-10-19T08:40:00.000Z", state="failed", built_from="i-done")]
        _, _, error = call(mod, {})
        self.assertIn("which failed", str(error))
        self.assertEqual(ec2.terminated, ["i-done"])
        self.assertEqual(ec2.deregistered, ["ami-made1"], "and the failed image is retired")

    def test_another_hosts_name_or_image_is_never_touched(self):
        mod, ec2 = load_builder()
        other = builder("i-dolphin", "stopped", "starting", name="dolphin-runner")
        ec2.instances = [other]
        _, _, error = call(mod, {})
        self.assertIsNone(error)
        self.assertEqual((ec2.terminated, ec2.created), ([], []))


class ImageScriptTests(unittest.TestCase):
    """runner-app/images/imagine-runner.sh's BEAM step, run for real against local archives and a fake curl."""

    def fixture(self, elixir_majors=(27, 28, 29), corrupt=False):
        tmp = Path(tempfile.mkdtemp(prefix="image-script."))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        served = tmp / "served"
        (served / "builds/otp/amd64/ubuntu-24.04").mkdir(parents=True)
        (served / "builds/elixir").mkdir(parents=True)
        src = tmp / "otp-src" / "OTP-29.1.1"
        (src / "bin").mkdir(parents=True)
        (src / "Install").write_text("#!/bin/sh\n")
        (src / "bin" / "erl").write_text("#!/bin/sh\n")
        otp = served / "builds/otp/amd64/ubuntu-24.04/OTP-29.1.1.tar.gz"
        with tarfile.open(otp, "w:gz") as t:
            t.add(src, arcname="OTP-29.1.1")
        listing = [f"OTP-29.1 abc 2026-09-16T11:12:00Z {'0' * 64}",
                   f"OTP-29.1.1 def 2026-09-22T08:11:48Z {hashlib.sha256(otp.read_bytes()).hexdigest()}"]
        (served / "builds/otp/amd64/ubuntu-24.04/builds.txt").write_text("\n".join(listing) + "\n")
        lines = []
        for m in elixir_majors:
            z = served / f"builds/elixir/v1.20.4-otp-{m}.zip"
            with zipfile.ZipFile(z, "w") as f:
                f.writestr("bin/elixir", f"built for {m}\n")
                f.writestr("lib/elixir/ebin/elixir.app", "x\n")
            digest = "f" * 64 if corrupt else hashlib.sha256(z.read_bytes()).hexdigest()
            lines.append(f"v1.20.4-otp-{m} 759443e 2026-08-28T10:07:51Z {digest} ")
        (served / "builds/elixir/builds.txt").write_text("v1.20.4 759443e 2026-08-28T10:07:51Z " + "e" * 64 + " \n"
                                                         + "\n".join(lines) + "\n")
        (tmp / "bin").mkdir()
        # curl -fsSL --retry 3 --retry-all-errors [-o FILE] URL: serve URL from the fixture tree.
        (tmp / "bin" / "curl").write_text(
            '#!/bin/bash\nout=; url=\nwhile [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift 2;; --retry) shift 2;; '
            '-*) shift;; *) url=$1; shift;; esac; done\n'
            f'file={served}/${{url#https://mirror.example}}\n[ -f "$file" ] || exit 22\n'
            'if [ -n "$out" ]; then cp "$file" "$out"; else cat "$file"; fi\n')
        (tmp / "bin" / "chown").write_text("#!/bin/bash\nexit 0\n")
        for f in ("curl", "chown"):
            (tmp / "bin" / f).chmod(0o755)
        return tmp

    def run_beam(self, tmp):
        script = (f"set -euo pipefail\nexport IMAGE_SCRIPT_FUNCTIONS_ONLY=1 TOOL_CACHE={tmp}/toolcache "
                  f"HEX_MIRROR=https://mirror.example\nsource {IMAGINE_SCRIPT}\n"
                  "OTP_VERSION=29.1.1 ELIXIR_VERSION=1.20.4 install_beam\n")
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                              env={"PATH": f"{tmp / 'bin'}:/usr/bin:/bin", "TMPDIR": str(tmp)})

    def test_otp_and_elixir_land_where_setup_beam_caches_them(self):
        tmp = self.fixture()
        out = self.run_beam(tmp)
        self.assertEqual(out.returncode, 0, out.stderr)
        cache = tmp / "toolcache"
        # @actions/tool-cache cacheDir(dir, tool, versionSpec) writes <cache>/<tool>/<semver.clean(spec) or spec>/<arch>
        # and <arch>.complete; setup-beam's specs are "ubuntu-24.04/OTP-29.1.1" and "v1.20.4-otp-29".
        self.assertTrue((cache / "otp/ubuntu-24.04/OTP-29.1.1/x64/bin/erl").is_file())
        self.assertTrue((cache / "otp/ubuntu-24.04/OTP-29.1.1/x64/Install").is_file(), "the tarball's top dir is stripped")
        self.assertTrue((cache / "otp/ubuntu-24.04/OTP-29.1.1/x64.complete").is_file())
        self.assertEqual((cache / "elixir/1.20.4-otp-29/x64/bin/elixir").read_text(), "built for 29\n")
        self.assertTrue((cache / "elixir/1.20.4-otp-29/x64.complete").is_file())

    def test_elixir_takes_the_build_for_the_newest_otp_major_listed_as_setup_beam_does(self):
        tmp = self.fixture(elixir_majors=(27, 28))
        out = self.run_beam(tmp)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual((tmp / "toolcache/elixir/1.20.4-otp-28/x64/bin/elixir").read_text(), "built for 28\n")
        self.assertFalse((tmp / "toolcache/elixir/1.20.4-otp-29").exists())

    def test_an_archive_that_does_not_match_its_listed_sha256_fails_the_build(self):
        tmp = self.fixture(corrupt=True)
        out = self.run_beam(tmp)
        self.assertNotEqual(out.returncode, 0)
        self.assertTrue((tmp / "toolcache/otp/ubuntu-24.04/OTP-29.1.1/x64.complete").exists(), "OTP's own sha matched")
        self.assertIn("FAILED", out.stdout + out.stderr, "sha256sum refused the Elixir archive")
        self.assertFalse((tmp / "toolcache/elixir/1.20.4-otp-29/x64.complete").exists())

    def test_node_is_the_pinned_version_not_the_newest_of_its_major(self):
        # ejc3/imagine's setup action uses an installed Node only at the version it pins, so the image installs exactly
        # that one, whatever nodejs.org lists as newer.
        tmp = Path(tempfile.mkdtemp(prefix="image-node."))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        served = tmp / "served" / "dist"
        served.mkdir(parents=True)
        for version in ("v22.99.0", "v22.23.3"):
            src = tmp / "node-src" / f"node-{version}-linux-x64"
            (src / "bin").mkdir(parents=True)
            (src / "bin" / "node").write_text(f"#!/bin/sh\necho {version}\n")
            (src / "bin" / "node").chmod(0o755)
            (served / version).mkdir()
            archive = served / version / f"node-{version}-linux-x64.tar.gz"
            with tarfile.open(archive, "w:gz") as tf:
                tf.add(src, arcname=src.name)
            (served / version / "SHASUMS256.txt").write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n")
        (served / "index.json").write_text(json.dumps([{"version": "v22.99.0"}, {"version": "v22.23.3"}]))
        (tmp / "bin").mkdir()
        (tmp / "bin" / "curl").write_text(
            '#!/bin/bash\nout=; url=\nwhile [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift 2;; --retry) shift 2;; '
            '-*) shift;; *) url=$1; shift;; esac; done\n'
            f'file={tmp / "served"}/${{url#https://nodejs.org/}}\n[ -f "$file" ] || exit 22\n'
            'if [ -n "$out" ]; then cp "$file" "$out"; else cat "$file"; fi\n')
        (tmp / "bin" / "npm").write_text(f'#!/bin/bash\necho "$*" > {tmp}/npm\n')
        for f in ("curl", "npm"):
            (tmp / "bin" / f).chmod(0o755)
        script = (f"set -euo pipefail\nexport IMAGE_SCRIPT_FUNCTIONS_ONLY=1 NODE_DIR={tmp}/opt-node\nsource {IMAGINE_SCRIPT}\n"
                  "NODE_VERSION=22.23.3 NODE_MAJOR=22 PNPM_VERSION=10.29.1 install_node\n")
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                             env={"PATH": f"{tmp / 'bin'}:/usr/bin:/bin", "TMPDIR": str(tmp)})
        self.assertEqual(out.returncode, 0, out.stderr)
        node = subprocess.run([str(tmp / "opt-node/bin/node")], capture_output=True, text=True).stdout.strip()
        self.assertEqual(node, "v22.23.3", "the pin, not v22.99.0, the newest 22 listed")
        self.assertIn("pnpm@10.29.1", (tmp / "npm").read_text())

    def test_the_script_is_valid_and_needs_every_pin_runner_app_image_tf_gives_it(self):
        subprocess.run(["bash", "-n", str(IMAGINE_SCRIPT)], check=True)
        subprocess.run(["bash", "-n", str(IMAGE_BUILD)], check=True)
        text = IMAGINE_SCRIPT.read_text()
        needed = set(re.findall(r'"\$\{([A-Z_]+):\?\}"', text))
        pinned = set(re.findall(r"^\s+([A-Z_]+)\s+= \"", IMAGE_TF.split("versions = {", 1)[1].split("}", 1)[0], re.M))
        self.assertEqual(needed, pinned)
        self.assertEqual(pinned, set(VERSIONS))
        self.assertIn("printf '%s\\n' /home/runner/.cargo/bin \"$NODE_DIR/bin\" > /etc/github-app-runner/path", text,
                      "bootstrap.sh puts these first on a job's PATH")
        self.assertIn("usermod -aG docker runner", text, "a job reaches docker's socket without sudo")


class NodeWebImageScriptTests(unittest.TestCase):
    """runner-app/images/node-web-runner.sh's Node and Python steps, run for real against local archives and a fake curl."""

    def fixture(self, corrupt_node=False):
        tmp = Path(tempfile.mkdtemp(prefix="node-web-script."))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        served = tmp / "served"
        # Node: an index newest first, one tarball per version, and its SHASUMS256.txt.
        (served / "node").mkdir(parents=True)
        index = []
        for version in ("v24.11.0", "v22.21.1", "v22.21.0"):
            src = tmp / "node-src" / f"node-{version}-linux-x64"
            (src / "bin").mkdir(parents=True)
            (src / "bin" / "node").write_text(f"#!/bin/sh\necho {version}\n")
            (served / "node" / version).mkdir()
            archive = served / "node" / version / f"node-{version}-linux-x64.tar.gz"
            with tarfile.open(archive, "w:gz") as tf:
                tf.add(src, arcname=src.name)
            digest = "0" * 64 if corrupt_node else hashlib.sha256(archive.read_bytes()).hexdigest()
            (served / "node" / version / "SHASUMS256.txt").write_text(
                f"{'1' * 64}  node-{version}-darwin-x64.tar.gz\n{digest}  {archive.name}\n")
            index.append({"version": version})
        (served / "node" / "index.json").write_text(json.dumps(index))
        # Python: actions/python-versions' manifest, newest first; each build carries its own setup.sh.
        manifest = []
        for version, stable in (("3.12.16", False), ("3.12.15", True), ("3.12.14", True), ("3.11.17", True)):
            files = []
            for platform_version, arch in (("22.04", "x64"), ("24.04", "arm64"), ("24.04", "x64")):
                name = f"python-{version}-linux-{platform_version}-{arch}.tar.gz"
                src = tmp / "py-src" / name
                src.mkdir(parents=True)
                (src / "setup.sh").write_text(
                    f'echo "{version} {platform_version} {arch} $RUNNER_TOOL_CACHE ${{AGENT_TOOLSDIRECTORY-unset}}"'
                    f' >> "$RUNNER_TOOL_CACHE/setup.log"\n'
                    f'mkdir -p "$RUNNER_TOOL_CACHE/Python/{version}/x64" && touch "$RUNNER_TOOL_CACHE/Python/{version}/x64.complete"\n')
                with tarfile.open(served / name, "w:gz") as tf:
                    tf.add(src / "setup.sh", arcname="./setup.sh")
                files.append({"filename": name, "arch": arch, "platform": "linux", "platform_version": platform_version,
                              "download_url": f"https://mirror.example/{name}"})
            manifest.append({"version": version, "stable": stable, "files": files})
        (served / "manifest.json").write_text(json.dumps(manifest))
        (tmp / "bin").mkdir()
        (tmp / "bin" / "curl").write_text(
            '#!/bin/bash\nout=; url=\nwhile [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift 2;; --retry) shift 2;; '
            '-*) shift;; *) url=$1; shift;; esac; done\n'
            f'file={served}/${{url#https://mirror.example/}}\n[ -f "$file" ] || exit 22\n'
            'if [ -n "$out" ]; then cp "$file" "$out"; else cat "$file"; fi\n')
        (tmp / "bin" / "curl").chmod(0o755)
        (tmp / "toolcache").mkdir()
        return tmp

    def run_steps(self, tmp, steps, **pins):
        exports = " ".join(f"{k}={v}" for k, v in pins.items())
        script = (f"set -euo pipefail\nexport IMAGE_SCRIPT_FUNCTIONS_ONLY=1 TOOL_CACHE={tmp}/toolcache TMPDIR={tmp} "
                  f"NODE_MIRROR=https://mirror.example/node PYTHON_MANIFEST=https://mirror.example/manifest.json "
                  f"AGENT_TOOLSDIRECTORY=/somewhere/else {exports}\nsource {NODE_WEB_SCRIPT}\n{steps}\n")
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                              env={"PATH": f"{tmp / 'bin'}:/usr/bin:/bin"})

    def test_node_lands_where_setup_node_finds_it(self):
        tmp = self.fixture()
        out = self.run_steps(tmp, 'install_node; echo "bin=$NODE_BIN"', NODE_MAJOR="22")
        self.assertEqual(out.returncode, 0, out.stderr)
        cache = tmp / "toolcache"
        # @actions/tool-cache cacheDir(dir, 'node', version): <cache>/node/<version without v>/<arch> and <arch>.complete.
        self.assertTrue((cache / "node/22.21.1/x64/bin/node").is_file(), "the newest of the major, its top dir stripped")
        self.assertTrue((cache / "node/22.21.1/x64.complete").is_file())
        self.assertFalse((cache / "node/24.11.0").exists())
        self.assertIn(f"bin={cache}/node/22.21.1/x64/bin", out.stdout, "Playwright's npx runs on that Node")

    def test_a_node_archive_that_does_not_match_its_shasums_fails_the_build(self):
        tmp = self.fixture(corrupt_node=True)
        out = self.run_steps(tmp, "install_node", NODE_MAJOR="22")
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse((tmp / "toolcache/node/22.21.1/x64.complete").exists())

    def test_each_python_minor_is_its_newest_stable_build_for_this_release_installed_by_its_setup(self):
        tmp = self.fixture()
        out = self.run_steps(tmp, "install_python", PYTHON_VERSIONS="3.11,3.12")
        self.assertEqual(out.returncode, 0, out.stderr)
        log = (tmp / "toolcache/setup.log").read_text().splitlines()
        # setup.sh prefers AGENT_TOOLSDIRECTORY when it is set at all, so it is unset for it.
        self.assertEqual(log, [f"3.11.17 24.04 x64 {tmp}/toolcache unset", f"3.12.15 24.04 x64 {tmp}/toolcache unset"])
        self.assertTrue((tmp / "toolcache/Python/3.12.15/x64.complete").is_file())

    def test_a_minor_with_no_build_fails_the_build(self):
        tmp = self.fixture()
        out = self.run_steps(tmp, "install_python", PYTHON_VERSIONS="3.9")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("no Python 3.9 build", out.stderr)

    def test_playwright_needs_both_its_pins(self):
        tmp = self.fixture()
        for pins in ({"PLAYWRIGHT_VERSION": "1.62.1"}, {"PLAYWRIGHT_BROWSERS": "chromium"}):
            out = self.run_steps(tmp, "main", NODE_MAJOR="22", **pins)
            self.assertNotEqual(out.returncode, 0, pins)
            self.assertIn("set with PLAYWRIGHT_", out.stderr)

    def test_the_script_is_valid_and_every_image_using_it_pins_only_what_it_reads(self):
        subprocess.run(["bash", "-n", str(NODE_WEB_SCRIPT)], check=True)
        text = NODE_WEB_SCRIPT.read_text()
        known = {"NODE_MAJOR", "PYTHON_VERSIONS", "PLAYWRIGHT_VERSION", "PLAYWRIGHT_BROWSERS", "APT_PACKAGES"}
        for pin in known:
            self.assertIn("${" + pin, text, "the script reads every pin an image may set")
        entries = re.findall(r'^    "([a-z0-9-]+)" = \{\n      repo\s+= "([^"]+)"\n      script = "node-web-runner\.sh"\n'
                             r'(.*?)\n      versions = \{\n(.*?)\n      \}', IMAGE_TF, re.M | re.S)
        self.assertEqual({e[0] for e in entries},
                         {"dolphin-labs-runner", "dolphin-films-runner", "dolphin-maps-runner", "colton-games-runner"})
        repos = set(re.findall(r'^    "([^"]+/[^"]+)"\s+= \{ label', APP_TF, re.M))
        for name, repo, _, block in entries:
            pins = dict(re.findall(r'^\s+([A-Z_]+)\s+= "([^"]*)"', block, re.M))
            self.assertIn("NODE_MAJOR", pins, name)
            self.assertLessEqual(set(pins), known, name)
            self.assertEqual("PLAYWRIGHT_VERSION" in pins, "PLAYWRIGHT_BROWSERS" in pins, name)
            self.assertIn(repo, repos, f"{name} is for an app runner repo")
            for value in pins.values():
                self.assertRegex(value, r"^[A-Za-z0-9][A-Za-z0-9._:/@+,-]{0,199}$", "image_builder.py's VERSION_VALUE")

    def test_a_script_several_images_share_is_packaged_once(self):
        self.assertIn("for_each = toset([for image in local.runner_app_images : image.script])", IMAGE_TF)


class TerraformTests(unittest.TestCase):
    def block(self, kind, name, text=IMAGE_TF):
        return re.search(r'^resource "' + kind + r'" "' + name + r'" \{\n.*?^\}', text, re.M | re.S).group()

    def statement(self, policy, sid):
        return re.search(r'Sid\s+= "' + sid + r'"(.*?)\n      \}', policy, re.S).group(1)

    def test_the_pins_say_what_they_must_match(self):
        pins = IMAGE_TF[IMAGE_TF.index('"imagine-runner" = {'):IMAGE_TF.index("versions = {")]
        for source in ("rust-toolchain.toml", ".github/workflows/ci.yml", "web/package.json", "private"):
            self.assertIn(source, pins)
        self.assertIn('RUST_TOOLCHAIN     = "1.93.0"', IMAGE_TF)

    def test_weekly_and_on_demand_builds_and_a_five_minute_advance_one_at_a_time(self):
        weekly = self.block("aws_cloudwatch_event_rule", "runner_app_image_weekly")
        self.assertIn('schedule_expression = "cron(0 9 ? * MON *)"', weekly)
        self.assertIn("input = jsonencode({ build = true })", self.block("aws_cloudwatch_event_target", "runner_app_image_weekly"))
        self.assertIn('schedule_expression = "rate(5 minutes)"', self.block("aws_cloudwatch_event_rule", "runner_app_image_advance"))
        self.assertIn("input = jsonencode({})", self.block("aws_cloudwatch_event_target", "runner_app_image_advance"))
        fn = self.block("aws_lambda_function", "runner_app_image")
        self.assertIn("reserved_concurrent_executions = 1", fn)
        self.assertIn('KEEP_IMAGES       = "3"', fn)
        self.assertIn('alarm_name          = "github-app-runner-image-errors"', self.block("aws_cloudwatch_metric_alarm", "runner_app_image_errors"))
        self.assertIn('--payload \'{"build": ["imagine-runner"]}\'', IMAGE_TF, "the on-demand call is written down")

    def test_the_functions_describe_calls_stay_in_us_west_1(self):
        # Describe calls take no resource ARN, so without the region condition the role could list every region.
        policy = self.block("aws_iam_role_policy", "runner_app_image_lambda")
        describe = self.statement(policy, "Describe")
        self.assertIn('"aws:RequestedRegion" = "us-west-1"', describe)

    def test_the_builder_host_can_only_report_complete(self):
        policy = self.block("aws_iam_role_policy", "runner_app_image_builder")
        allows = re.findall(r'Effect\s*=\s*"Allow"\s*\n\s*Action\s*=\s*("[^"]*"|\[[^\]]*\])', policy)
        self.assertEqual(allows, ['"ec2:CreateTags"'])
        self.assertIn('"aws:RequestTag/BuildStatus" = "complete"', policy)
        self.assertIn('"aws:TagKeys" = ["BuildStatus"]', policy)
        self.assertRegex(policy, r'"ec2:ResourceTag/Name"\s+= local.runner_app_image_builder')
        self.assertNotRegex(IMAGE_TF, r'aws_iam_role_policy_attachment"\s+"runner_app_image_builder', "no managed policies")

    def test_the_function_builds_only_from_canonical_and_retires_only_its_own_images(self):
        policy = self.block("aws_iam_role_policy", "runner_app_image_lambda")
        launch_image = self.statement(policy, "BuildFromCanonicalImagesOnly")
        self.assertIn('"ec2:Owner" = [local.runner_app_ami_owner, "amazon"]', launch_image)
        self.assertEqual(len(re.findall(r'Action\s+= "ec2:RunInstances"\n\s+Resource\s+= "arn:aws:ec2:us-west-1::image/\*"', policy)), 1)
        builder_ = self.statement(policy, "LaunchNamedBuilder")
        for needed in ('"aws:RequestTag/Name"    = local.runner_app_image_builder', '"ec2:InstanceType"       = local.runner_app_image_builder_types',
                       '"ec2:MetadataHttpTokens" = "required"', '"ec2:InstanceProfile" = aws_iam_instance_profile.runner_app_image_builder[0].arn'):
            self.assertIn(needed, builder_)
        self.assertIn('"aws:RequestTag/Purpose" = local.runner_app_image_purpose', self.statement(policy, "CreateTaggedImage"))
        retire = self.statement(policy, "RetireOwnImagesOnly")
        self.assertIn('["ec2:DeregisterImage", "ec2:DeleteSnapshot"]', retire)
        self.assertIn('"aws:ResourceTag/Purpose" = local.runner_app_image_purpose', retire)
        self.assertIn('"aws:ResourceTag/Name" = local.runner_app_image_builder', self.statement(policy, "ImageTerminateAndReadBuildersOnly"))
        self.assertIn("NotResource = aws_iam_role.runner_app_image_builder[0].arn", policy)
        self.assertIn('"ec2:CreateAction" = ["RunInstances", "CreateImage"]', self.statement(policy, "DenyTagChangesOutsideCreation"))
        for broad in ('"ec2:*"', '"iam:*"', "secretsmanager", "AdministratorAccess"):
            self.assertNotIn(broad, policy)

    def test_one_purpose_tag_everywhere_and_never_fcvms(self):
        self.assertIn('runner_app_image_purpose           = "github-app-runner-image"', IMAGE_TF)
        builder_src = BUILDER_PY.read_text()
        app_src = (ROOT / "runner-app" / "app_runner.py").read_text()
        self.assertIn("PURPOSE = 'github-app-runner-image'", builder_src)
        self.assertIn("IMAGE_PURPOSE = 'github-app-runner-image'", app_src)
        self.assertIn('runner_app_image_builder           = "github-app-runner-image-builder"', IMAGE_TF)
        self.assertIn("BUILDER = 'github-app-runner-image-builder'", builder_src)
        # fcvm's builder reaps stray ami-builder-temp hosts and its launcher and the Ohio replicator take Purpose=github-runner.
        self.assertNotIn("ami-builder-temp", IMAGE_TF + builder_src)


if __name__ == "__main__":
    unittest.main()
