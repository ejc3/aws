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
        self.assertRegex(self.instance, r'Name\s+=\s+local\.nextjs_ct_serving \? "nextjs-dev" : "nextjs-ct"')

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
        self.assertIn("--bucket ejc3-dev-scripts --key user-data/nextjs-ct.sh", self.instance)
        self.assertIn('key          = "user-data/nextjs-ct.sh"', CT_USER_DATA)
        read = block(NEXTJS_DEV_TF, 'resource "aws_iam_role_policy" "nextjs_dev"')
        self.assertIn("/user-data/nextjs-ct.sh", read)
        self.assertIn("/user-data/nextjs.sh", read)


class CutoverSwitchTests(unittest.TestCase):
    """One value, local.nextjs_ct_serving, moves everything keyed to the nextjs box; on since the cutover (2026-10-20)."""

    def test_switch_is_on(self):
        self.assertIn("nextjs_ct_serving = true", (ROOT / "nextjs-ct-user-data.tf").read_text())

    def test_everything_keyed_to_the_box_reads_the_live_instance(self):
        self.assertIn("nextjs_live_instance = local.nextjs_ct_serving ? one(aws_instance.nextjs_ct) : one(aws_instance.nextjs_dev)", CT_TF)
        assoc = block(NEXTJS_DEV_TF, 'resource "aws_eip_association" "nextjs_dev"')
        self.assertIn("instance_id   = local.nextjs_live_instance.id", assoc)
        self.assertIn("volume/${local.nextjs_live_instance.root_block_device[0].volume_id}", NEXTJS_DEV_TF)
        alarms = (ROOT / "nextjs-alarms.tf").read_text()
        self.assertNotIn("aws_instance.nextjs_dev", alarms)
        self.assertEqual(alarms.count("local.nextjs_live_instance"), 6)
        self.assertIn("[local.nextjs_live_instance.id]", (ROOT / "agents-md.tf").read_text())
        self.assertIn('Name    = local.nextjs_ct_serving ? "nextjs-dev-retired" : "nextjs-dev"', NEXTJS_DEV_TF)

    def test_address_and_old_box_move_only_after_the_new_box_applied_the_switch(self):
        # Review of #490 (Codex P1): otherwise the EIP moves to a box whose tunnels are still off (cutover), or the
        # old box starts while the new one still holds the tunnels (rollback).
        conv = block(CT_TF, 'resource "terraform_data" "nextjs_ct_converge"')
        self.assertIn("serving  = local.nextjs_ct_serving", conv)
        self.assertIn("scripts/ssm-nextjs-ct-converge.sh", conv)
        self.assertIn("depends_on = [aws_s3_object.nextjs_ct_user_data]", conv)
        self.assertIn("depends_on = [terraform_data.nextjs_ct_converge]", block(CT_TF, 'resource "aws_ec2_instance_state" "nextjs_dev"'))
        self.assertIn("depends_on = [terraform_data.nextjs_ct_converge]", block(NEXTJS_DEV_TF, 'resource "aws_eip_association" "nextjs_dev"'))
        helper = (ROOT / "scripts" / "ssm-nextjs-ct-converge.sh").read_text()
        self.assertIn("flock -w 3600 /run/nextjs-ct-setup.lock true", helper)
        self.assertIn("systemctl start setup-sync.service", helper)
        self.assertIn('jq -r .serving /etc/nextjs-ct/config.json', helper)
        self.assertIn("/usr/local/sbin/nextjs-ct-health || exit 1", helper)
        self.assertIn('cloudflared@$z.service', helper)
        subprocess.run(["bash", "-n", str(ROOT / "scripts" / "ssm-nextjs-ct-converge.sh")], check=True)

    def test_old_box_is_stopped_never_terminated(self):
        state = block(CT_TF, 'resource "aws_ec2_instance_state" "nextjs_dev"')
        self.assertIn('state       = local.nextjs_ct_serving ? "stopped" : "running"', state)
        self.assertRegex(block(NEXTJS_DEV_TF, 'resource "aws_instance" "nextjs_dev"'), r"delete_on_termination\s+=\s+false")


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


class FirstBootTests(unittest.TestCase):
    """Review of #487: the first boot must not strand the box, rerun itself, or record a half-done setup."""

    def setUp(self):
        self.instance = block(CT_TF, 'resource "aws_instance" "nextjs_ct"')
        self.script = CT_USER_DATA.split("nextjs_ct_user_data = <<-SCRIPT", 1)[1].split("\nSCRIPT\n", 1)[0]

    def test_bootstrap_retries_the_download_and_passes_its_etag(self):
        self.assertRegex(self.instance, r"for i in \$\(seq 1 120\); do\n\s+if META=\$\(aws s3api get-object")
        self.assertIn('export NEXTJS_CT_ETAG=', self.instance)
        self.assertNotIn("aws s3 cp s3://ejc3-dev-scripts/user-data/nextjs-ct.sh", self.instance)

    def test_setup_sync_is_installed_before_anything_that_can_fail(self):
        timer = self.script.index("systemctl enable --now setup-sync.timer")
        self.assertLess(timer, self.script.index("apt-get"))
        self.assertLess(self.script.index("${local.nextjs_ct_install_files}"), timer)

    def test_required_packages_fail_the_run(self):
        install = next(l for l in self.script.splitlines() if "systemd-container" in l)
        follow = self.script.split(install, 1)[1].splitlines()[1]
        self.assertIn("exit 1", follow)
        self.assertNotIn("WARNING", follow)
        self.assertIn('ERROR: the AWS CLI did not install"; exit 1', self.script)

    def test_etag_is_recorded_only_after_health(self):
        health = self.script.index("/usr/local/sbin/nextjs-ct-health || {")
        self.assertLess(health, self.script.index("/var/lib/nextjs-setup/applied-etag"))
        self.assertRegex(CT_USER_DATA, r'"/usr/local/sbin/nextjs-ct-health"\s+=\s+"scripts/nextjs-ct/nextjs-ct-health"')

    def test_a_run_outside_setup_sync_waits_for_the_lock(self):
        self.assertIn('if [ -z "$${NEXTJS_CT_SETUP_LOCK_HELD:-}" ]; then\n  exec 9>/run/nextjs-ct-setup.lock', self.script)


class HealthTests(unittest.TestCase):
    def run_health(self, missing=(), timer=True, active=True):
        with tempfile.TemporaryDirectory() as d:
            bin_ = Path(d)
            for cmd in ("aws", "jq", "nft", "systemd-nspawn", "machinectl", "debootstrap", "rsync"):
                if cmd not in missing:
                    (bin_ / cmd).write_text("#!/bin/sh\nexit 0\n")
                    (bin_ / cmd).chmod(0o755)
            (bin_ / "systemctl").write_text(f"#!/bin/sh\ncase \"$1 $3\" in 'is-enabled setup-sync.timer') exit {0 if timer else 1} ;; 'is-active setup-sync.timer') exit {0 if active else 1} ;; esac\nexit 0\n")
            (bin_ / "systemctl").chmod(0o755)
            sync = bin_ / "setup-sync"
            sync.write_text("#!/bin/sh\n")
            sync.chmod(0o755)
            text = (SCRIPTS / "nextjs-ct-health").read_text().replace("/usr/local/bin/setup-sync", str(sync))
            (bin_ / "health").write_text(text)
            (bin_ / "health").chmod(0o755)
            env = dict(os.environ, PATH=f"{bin_}:/usr/bin:/bin")
            # The real tools in /usr/bin must not stand in for a missing fake.
            env["PATH"] = str(bin_)
            return subprocess.run(["/bin/bash", str(bin_ / "health")], env=env, capture_output=True, text=True)

    def test_healthy(self):
        r = self.run_health()
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_missing_tool_is_unhealthy(self):
        r = self.run_health(missing=("nft",))
        self.assertEqual(r.returncode, 1)
        self.assertIn("missing:nft", r.stdout)

    def test_disabled_timer_is_unhealthy(self):
        r = self.run_health(timer=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("disabled:setup-sync.timer", r.stdout)


    def test_enabled_but_stopped_timer_is_unhealthy(self):
        r = self.run_health(active=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("inactive:setup-sync.timer", r.stdout)


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
                                      ("/usr/local/sbin/nextjs-ct-health", str(self.root / "health")),
                                      ("/run/nextjs-ct-setup.lock", str(self.root / "setup.lock"))):
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

    def test_hands_the_script_its_etag_and_the_held_lock(self):
        r = self.run_sync('#!/bin/bash\necho "etag=$NEXTJS_CT_ETAG held=$NEXTJS_CT_SETUP_LOCK_HELD"\n', health=0)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('etag="etag-1" held=1', (self.root / "setup.log").read_text())

    def test_a_running_setup_makes_the_tick_wait_for_the_next(self):
        import fcntl
        with open(self.root / "setup.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            r = self.run_sync("#!/bin/bash\nexit 0\n", health=0)
        self.assertEqual(r.returncode, 0)
        self.assertIn("already running", r.stdout)
        self.assertFalse(self.etag.exists())

    def test_refuses_a_script_that_does_not_parse(self):
        r = self.run_sync("#!/bin/bash\nif then\n")
        self.assertEqual(r.returncode, 0)
        self.assertIn("REFUSING", r.stdout)
        self.assertFalse(self.etag.exists())


class AdminKeyTests(unittest.TestCase):
    """nextjs-ct-machine strips the instance key pair (the owner's key) from every account that does not
    declare it; the owner's phone logs in to ejc3 with that key, so ejc3 must declare it and only ejc3."""

    def test_only_ejc3_declares_the_owner_key(self):
        user_data = (ROOT / "nextjs-user-data.tf").read_text()
        key = re.search(r'echo "(ssh-ed25519 \S+ fcvm-ec2)" >> /home/ubuntu/.ssh/authorized_keys',
                        (ROOT / "runner-autoscale.tf").read_text()).group(1)
        keys = block(user_data, "nextjs_user_keys = {")
        ejc3 = keys[keys.index("ejc3 = ["):]
        ejc3 = ejc3[:ejc3.index("\n    ]")]
        self.assertIn(f'"{key}"', ejc3)
        self.assertEqual(keys.count(key), 1)
        machine = (SCRIPTS / "host" / "nextjs-ct-machine").read_text()
        self.assertIn("removed the admin key", machine)


class ProvisionMetadataTimeoutTests(unittest.TestCase):
    """setup restarts nextjs-ct-imds right before provision runs; the CLI's default one-second, one-try metadata
    timeout then misses the first credentials, so provision raises it before its first AWS call."""

    def test_timeout_is_raised_before_the_first_aws_call(self):
        provision = (SCRIPTS / "ct" / "provision").read_text()
        export = provision.index("export AWS_METADATA_SERVICE_TIMEOUT=5 AWS_METADATA_SERVICE_NUM_ATTEMPTS=3")
        first_call = re.search(r"\$\(aws |^\s*aws ", provision, re.M).start()
        self.assertLess(export, first_call)


class BoxAgentNotesTests(unittest.TestCase):
    """Every account's Claude Code gets the box-wide notes (/etc/claude-code/CLAUDE.md): the claw-master proxy is not a
    firewall, the host-side limits are, and a cause is named only once seen. A member's agent once unset HTTPS_PROXY and
    blamed it for a failure it never showed."""

    def test_provision_installs_the_notes_for_claude_code(self):
        provision = (SCRIPTS / "ct" / "provision").read_text()
        self.assertIn('install -m 644 "$SHARED/ct/box-agents.md" /etc/claude-code/CLAUDE.md', provision)

    def test_the_notes_say_what_the_proxy_is_and_what_limits_you(self):
        notes = (SCRIPTS / "ct" / "box-agents.md").read_text()
        for phrase in ("`HTTPS_PROXY` is not a firewall", "127.0.0.1", "Do not unset it",
                       "nothing you run inside can change them", "Name a cause only after you have seen it"):
            self.assertIn(phrase, notes)


if __name__ == "__main__":
    unittest.main()
