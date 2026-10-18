#!/usr/bin/env python3
"""Offline checks for the containerized nextjs box (nextjs-ct.tf, nextjs-ct-user-data.tf, scripts/nextjs-ct/)."""

from pathlib import Path
import os
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
CT_TF = (ROOT / "nextjs-ct.tf").read_text()
CT_USER_DATA = (ROOT / "nextjs-ct-user-data.tf").read_text()
NEXTJS_DEV_TF = (ROOT / "nextjs-dev.tf").read_text()
SCRIPTS = ROOT / "scripts" / "nextjs-ct"


def block(text, header):
    """The body of the first top-level block whose header line starts with `header`."""
    start = text.index(header)
    depth = 0
    for i in range(text.index("{", start), len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise AssertionError(f"unterminated block {header}")


class BoxTests(unittest.TestCase):
    def setUp(self):
        self.instance = block(CT_TF, 'resource "aws_instance" "nextjs_ct"')

    def test_box_serves_nothing_before_the_cutover(self):
        # No Elastic IP of its own, and a Name tag that nothing keys on (auto-reboot, alarms, hop aliases and
        # claw-master renewal all look for "nextjs-dev").
        self.assertNotIn('resource "aws_eip"', CT_TF)
        self.assertNotIn('resource "aws_eip_association"', CT_TF)
        self.assertRegex(self.instance, r'Name\s+=\s+"nextjs-ct"')

    def test_box_shares_the_role_and_security_group(self):
        self.assertIn("aws_iam_instance_profile.nextjs_dev.name", self.instance)
        self.assertIn("aws_security_group.nextjs_dev.id", self.instance)

    def test_containers_cannot_reach_the_real_metadata_service(self):
        self.assertRegex(self.instance, r'http_tokens\s+=\s+"required"')
        self.assertRegex(self.instance, r"http_put_response_hop_limit\s+=\s+1\b")

    def test_root_volume_survives_termination(self):
        self.assertRegex(self.instance, r"delete_on_termination\s+=\s+false")
        self.assertRegex(self.instance, r"encrypted\s+=\s+true")

    def test_bootstrap_fetches_its_own_script(self):
        self.assertIn("s3://ejc3-dev-scripts/user-data/nextjs-ct.sh", self.instance)
        self.assertIn('key          = "user-data/nextjs-ct.sh"', CT_USER_DATA)
        read = block(NEXTJS_DEV_TF, 'resource "aws_iam_role_policy" "nextjs_dev"')
        self.assertIn("/user-data/nextjs-ct.sh", read)
        self.assertIn("/user-data/nextjs.sh", read)


class SelfAssumeTests(unittest.TestCase):
    def test_role_trusts_itself_and_nothing_new(self):
        role = block(NEXTJS_DEV_TF, 'resource "aws_iam_role" "nextjs_dev"')
        self.assertIn('Principal = { Service = "ec2.amazonaws.com" }', role)
        self.assertIn("Principal = { AWS = local.nextjs_dev_role_arn }", role)
        self.assertEqual(role.count("Principal"), 2)
        self.assertIn('role/nextjs-dev-role"', CT_TF)

    def test_identity_grant_is_this_role_only(self):
        grant = block(CT_TF, 'resource "aws_iam_role_policy" "nextjs_ct_self_assume"')
        self.assertIn('Action   = "sts:AssumeRole"', grant)
        self.assertIn("Resource = local.nextjs_dev_role_arn", grant)
        self.assertNotIn('"*"', grant)


class EmbeddedFilesTests(unittest.TestCase):
    def test_every_embedded_file_exists_and_parses(self):
        files = re.findall(r'"(/[^"]+)"\s+=\s+"(scripts/nextjs-ct/[^"]+)"', CT_USER_DATA)
        self.assertTrue(files)
        for dest, src in files:
            path = ROOT / src
            self.assertTrue(path.is_file(), src)
            first = path.read_text().splitlines()[0]
            if "bash" in first:
                subprocess.run(["bash", "-n", str(path)], check=True)
            elif "python3" in first:
                subprocess.run(["python3", "-m", "py_compile", str(path)], check=True)

    def test_files_are_embedded_as_base64_not_templates(self):
        self.assertIn('base64encode(file("${path.module}/${src}"))', CT_USER_DATA)


class SetupSyncTests(unittest.TestCase):
    """The real setup-sync, against a fake aws CLI serving a published script."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-nextjs-ct.")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "aws").write_text(f"""#!/bin/bash
case "$1 $2" in
  "s3api head-object") echo '"etag-1"' ;;
  "s3 cp") cp {self.root}/published.sh "$4" ;;
esac
""")
        (self.bin / "aws").chmod(0o755)
        # The log's group is adm on the box; the test user cannot chgrp to it.
        for tool in ("chgrp",):
            (self.bin / tool).write_text("#!/bin/sh\nexit 0\n")
            (self.bin / tool).chmod(0o755)
        sync = (SCRIPTS / "setup-sync").read_text()
        for original, replacement in (("/tmp/", f"{self.root}/"),
                                      ("/var/lib/nextjs-setup", str(self.root / "state")),
                                      ("/var/log/setup-sync.log", str(self.root / "setup.log")),
                                      ("/usr/local/sbin/nextjs-ct-health", str(self.root / "health"))):
            sync = sync.replace(original, replacement)
        sync = sync.replace("install -m 0640 -o root -g adm", "install -m 0640")
        self.sync = self.root / "setup-sync"
        self.sync.write_text(sync)
        self.sync.chmod(0o755)
        self.etag = self.root / "state" / "applied-etag"

    def run_sync(self, published, health=None):
        (self.root / "published.sh").write_text(published)
        if health is not None:
            (self.root / "health").write_text(f"#!/bin/sh\nexit {health}\n")
            (self.root / "health").chmod(0o755)
        env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin")
        return subprocess.run([str(self.sync)], env=env, capture_output=True, text=True, timeout=30)

    def test_records_a_healthy_run(self):
        r = self.run_sync("#!/bin/bash\nexit 0\n", health=0)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.etag.read_text(), '"etag-1"')

    def test_failed_setup_is_retried(self):
        r = self.run_sync("#!/bin/bash\nexit 3\n")
        self.assertEqual(r.returncode, 1)
        self.assertFalse(self.etag.exists())

    def test_failed_health_is_retried(self):
        r = self.run_sync("#!/bin/bash\nexit 0\n", health=1)
        self.assertEqual(r.returncode, 1)
        self.assertIn("health check failed", r.stdout)
        self.assertFalse(self.etag.exists())

    def test_refuses_a_script_that_does_not_parse(self):
        r = self.run_sync("#!/bin/bash\nif then\n")
        self.assertEqual(r.returncode, 0)
        self.assertIn("REFUSING", r.stdout)
        self.assertFalse(self.etag.exists())


if __name__ == "__main__":
    unittest.main()
