#!/usr/bin/env python3
"""Run the real bootstrap with fake IMDS, SSM, and runner binaries."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = (ROOT / "runner-app" / "bootstrap.sh").read_text()
INSTANCE = "i-0123456789abcdef0"
CREDENTIAL = "fixture-bootstrap-credential"

COMMANDS = f"""
import os
from pathlib import Path
import sys

command = Path(sys.argv[0]).name
args = sys.argv[1:]
journal = Path(os.environ["JOURNAL"])
credential = {CREDENTIAL!r}
assert all(credential not in arg for arg in args), "credential was passed in argv"

def record(event):
    with journal.open("a") as handle:
        handle.write(event + "\\n")

def env_setup():
    Path(".env").touch()
    Path(".path").write_text("/usr/bin:/bin\\n")

if command == "curl":
    if args[-1].endswith("/meta-data/instance-id"):
        print({INSTANCE!r})
    else:
        assert args[1:3] == ["-X", "PUT"] and args[3].endswith("/api/token"), args
        print("imds-fixture")
elif command == "dpkg-query":
    print("install ok installed")
elif command == "install":
    Path(args[-1]).mkdir(parents=True, exist_ok=True)
elif command in ("id", "sync"):
    pass
elif command == "shutdown":
    assert args == ["-h", "now"]
    record("shutdown")
elif command == "sudo":
    assert args[:3] == ["-u", "runner", "-H"]
    assert all(not name.startswith("ACTIONS_RUNNER_INPUT_") for name in os.environ)
    os.execvp(args[3], args[3:])
elif command == "config.sh":
    assert os.environ.pop("ACTIONS_RUNNER_INPUT_TOKEN") == credential
    assert args == ["--unattended", "--url", "https://github.com/dolphin-labs-hq/dolphin-labs",
                    "--name", {INSTANCE!r}, "--labels", "dolphin,s", "--ephemeral",
                    "--disableupdate", "--work", os.environ["WORKSPACE"]]
    env_setup()
    record("configure repository runner")
elif command in ("run.sh", "Runner.Listener"):
    if command == "Runner.Listener":
        assert args == ["run"]
        assert os.environ.pop("ACTIONS_RUNNER_INPUT_JITCONFIG") == credential
        assert os.umask(0o077) == 0o077
        record("run JIT listener")
    else:
        assert args == []
        record("run repository listener")
    assert all(not name.startswith("ACTIONS_RUNNER_INPUT_") for name in os.environ)
    configured = Path(".env").read_text()
    assert credential not in configured
    assert "PATH=/opt/dev-tools/bin:/usr/bin:/bin" in configured
    assert "AGENT_TOOLSDIRECTORY=" + os.environ["TOOL_CACHE_PATH"] in configured
    assert "RUNNER_TOOL_CACHE=" + os.environ["TOOL_CACHE_PATH"] in configured
    assert Path(os.environ["WORKSPACE"]).is_dir()
else:
    raise AssertionError(f"unexpected system command {{command}}")
"""

SSM = f"""
import os
from pathlib import Path

class SSM:
    def record(self, event):
        with Path(os.environ["JOURNAL"]).open("a") as handle:
            handle.write(event + "\\n")

    def get_parameter(self, Name, WithDecryption):
        assert Name == "/github-runner/bootstrap/{INSTANCE}" and WithDecryption is True
        self.record("read broker")
        return {{"Parameter": {{"Value": {CREDENTIAL!r}}}}}

    def delete_parameter(self, Name):
        assert Name == "/github-runner/bootstrap/{INSTANCE}"
        self.record("delete broker credential")
        if os.environ.get("DELETE_FAILS") == "1":
            raise RuntimeError("mock SSM refused deletion")

def client(service):
    assert service == "ssm"
    return SSM()
"""


class BootstrapTests(unittest.TestCase):
    def bootstrap(self, registration, *, prepare_only=False, delete_fails=False):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            runner, commands, modules, etc = (root / name for name in ("runner", "bin", "modules", "etc"))
            for directory in (runner / "bin", commands, modules, etc / "sudoers.d", etc / "apt" / "apt.conf.d",
                              etc / "github-app-runner"):
                directory.mkdir(parents=True)
            (runner / ".runner-version").write_text("2.337.0\n")
            (etc / "github-app-runner" / "path").write_text("/opt/dev-tools/bin\n")
            (modules / "boto3.py").write_text(SSM)
            (modules / "botocore").mkdir()
            (modules / "botocore" / "exceptions.py").touch()
            stub = root / "stub"
            stub.write_text(f"#!{sys.executable}\n" + COMMANDS)
            stub.chmod(0o755)
            for name in ("curl", "dpkg-query", "id", "install", "sudo", "sync", "shutdown", "apt-get",
                         "useradd", "tar"):
                (commands / name).symlink_to(stub)
            for name in ("config.sh", "run.sh", "bin/Runner.Listener"):
                (runner / name).symlink_to(stub)
            (runner / "env.sh").write_text('touch .env\nprintf "%s\\n" /usr/bin:/bin >.path\n')
            for name in ("journal", "bootstrap.log"):
                (root / name).touch()

            script = BOOTSTRAP
            paths = {"/var/log/github-app-runner.log": root / "bootstrap.log", "/etc/": f"{etc}/",
                     "/home/runner/actions-runner": runner, "/home/runner/work": root / "work",
                     "/opt/hostedtoolcache": root / "toolcache"}
            for before, after in paths.items():
                script = script.replace(before, str(after))
            script = script.replace("@@REGISTRATION@@", registration)
            script = script.replace("@@REPO@@", "dolphin-labs-hq/dolphin-labs").replace("@@LABELS@@", "dolphin,s")
            result = subprocess.run(["bash", "-x", "-s"], input=script, text=True, capture_output=True, timeout=10, env={
                "PATH": f"{commands}:{os.defpath}", "PYTHONPATH": str(modules), "PYTHONDONTWRITEBYTECODE": "1",
                "GITHUB_APP_RUNNER_PREPARE_ONLY": str(int(prepare_only)), "DELETE_FAILS": str(int(delete_fails)),
                "JOURNAL": str(root / "journal"), "WORKSPACE": str(root / "work"),
                "TOOL_CACHE_PATH": str(root / "toolcache"),
            })
            output = result.stdout + result.stderr + (root / "bootstrap.log").read_text()
            self.assertNotIn(CREDENTIAL, output)
            return result.returncode, (root / "journal").read_text().splitlines(), output

    def test_repository_runner_keeps_existing_configuration(self):
        code, journal, output = self.bootstrap("repository")
        self.assertEqual(code, 0, output)
        self.assertEqual(journal, ["read broker", "delete broker credential", "configure repository runner",
                                   "run repository listener", "shutdown"])

    def test_jit_runner_starts_only_after_its_brokered_credential_is_deleted(self):
        code, journal, output = self.bootstrap("jit")
        self.assertEqual(code, 0, output)
        self.assertEqual(journal, ["read broker", "delete broker credential", "run JIT listener", "shutdown"])

    def test_prepare_only_does_not_register_or_shutdown(self):
        code, journal, output = self.bootstrap("@@REGISTRATION@@", prepare_only=True)
        self.assertEqual(code, 0, output)
        self.assertEqual(journal, [])
        self.assertIn("not registering", output)

    def test_run_without_a_rendered_mode_fails_before_reading_the_broker(self):
        code, journal, output = self.bootstrap("@@REGISTRATION@@")
        self.assertNotEqual(code, 0, output)
        self.assertEqual(journal, ["shutdown"])

    def test_jit_runner_cannot_start_when_credential_could_not_be_deleted(self):
        code, journal, output = self.bootstrap("jit", delete_fails=True)
        self.assertNotEqual(code, 0, output)
        self.assertEqual(journal, ["read broker", "delete broker credential", "shutdown"])


if __name__ == "__main__":
    unittest.main()
