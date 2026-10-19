#!/usr/bin/env python3
"""Tests for the PR and issue watch installer and its SessionStart hook (scripts/pr-watch/)."""
import json
import sys
import re
import os
import pathlib
import subprocess
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOOK = ROOT / "scripts/pr-watch/session-start.sh"
INSTALL = ROOT / "scripts/pr-watch-install.sh"


def run(cmd, home, **kw):
    env = dict(os.environ, HOME=str(home))
    return subprocess.run(cmd, env=env, capture_output=True, text=True, **kw)


CLAIM = ROOT / "scripts/pr-watch/claim.sh"


class ClaimTests(unittest.TestCase):
    def claim(self, home, me, *args, **env):
        e = dict(os.environ, HOME=str(home), CLAUDE_CODE_MESSAGING_SOCKET=me, **env)
        return subprocess.run(["bash", str(CLAIM), *args], env=e, capture_output=True, text=True).stdout.strip()

    def test_first_claim_owns_and_another_session_is_turned_away_while_fresh(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(self.claim(home, "/s/a", "status"), "stale")
            self.assertEqual(self.claim(home, "/s/a", "claim"), "owner")
            self.assertEqual(self.claim(home, "/s/a", "claim"), "owner")  # refreshing keeps it
            self.assertEqual(self.claim(home, "/s/b", "claim"), "other")
            self.assertEqual(self.claim(home, "/s/b", "status"), "fresh")

    def test_a_stale_heartbeat_is_taken_over(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(self.claim(home, "/s/a", "claim"), "owner")
            beat = pathlib.Path(home) / ".local/state/pr-watch/heartbeat"
            beat.write_text("%d /s/a\n" % (time.time() - 2701))
            self.assertEqual(self.claim(home, "/s/b", "status"), "stale")
            self.assertEqual(self.claim(home, "/s/b", "claim"), "owner")
            self.assertEqual(self.claim(home, "/s/a", "claim"), "other")

    def test_racing_sessions_get_exactly_one_owner(self):
        with tempfile.TemporaryDirectory() as home:
            procs = [subprocess.Popen(["bash", str(CLAIM), "claim"], stdout=subprocess.PIPE, text=True,
                                      env=dict(os.environ, HOME=home, CLAUDE_CODE_MESSAGING_SOCKET="/s/%d" % i))
                     for i in range(8)]
            results = [p.communicate()[0].strip() for p in procs]
            self.assertEqual(results.count("owner"), 1, results)
            self.assertEqual(results.count("other"), 7, results)

    def test_a_garbled_heartbeat_counts_as_stale(self):
        with tempfile.TemporaryDirectory() as home:
            state = pathlib.Path(home) / ".local/state/pr-watch"
            state.mkdir(parents=True)
            (state / "heartbeat").write_text("not-a-time /s/a\n")
            self.assertEqual(self.claim(home, "/s/b", "claim"), "owner")


class HookTests(unittest.TestCase):
    def test_hook_always_reminds_and_records_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            for _ in range(2):  # fresh or stale, every session keeps a (standby) job
                out = run(["bash", str(HOOK)], home, input="{}")
                self.assertEqual(out.returncode, 0, out.stderr)
                ctx = json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]
                self.assertIn("/pr-watch", ctx)
                self.assertIn("standby", ctx)
                env = dict(os.environ, HOME=home, CLAUDE_CODE_MESSAGING_SOCKET="/s/owner")
                subprocess.run(["bash", str(CLAIM), "claim"], env=env, check=True, capture_output=True)
            self.assertNotIn("/s/hook", (pathlib.Path(home) / ".local/state/pr-watch/heartbeat").read_text())


class InstallerTests(unittest.TestCase):
    def test_watcher_install_keeps_other_hooks_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as home:
            claude = pathlib.Path(home) / ".claude"
            claude.mkdir()
            existing = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "keep-me"}]}],
                                  "SessionStart": [{"hooks": [{"type": "command", "command": "other-start"},
                                                              {"type": "prompt", "prompt": "a prompt hook"}]}]},
                        "model": "example-model"}
            real = pathlib.Path(home) / "dotfiles-settings.json"
            real.write_text(json.dumps(existing))
            (claude / "settings.json").symlink_to(real)
            for _ in range(2):
                result = run(["bash", str(INSTALL), "--watcher"], home)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((claude / "settings.json").is_symlink())  # written through, link kept
            settings = json.loads(real.read_text())
            self.assertEqual(settings["model"], "example-model")
            self.assertEqual(settings["hooks"]["PreToolUse"], existing["hooks"]["PreToolUse"])
            commands = [h["command"] for group in settings["hooks"]["SessionStart"] for h in group["hooks"] if "command" in h]
            self.assertEqual(commands.count("other-start"), 1)
            prompts = [h for group in settings["hooks"]["SessionStart"] for h in group["hooks"] if h["type"] == "prompt"]
            self.assertEqual(len(prompts), 1)
            self.assertEqual(len([c for c in commands if "pr-watch" in c]), 1)
            self.assertTrue((claude / "skills/pr-watch/SKILL.md").is_file())
            self.assertTrue((pathlib.Path(home) / ".local/share/pr-watch/claim.sh").is_file())

    def test_rejects_unknown_usage(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(run(["bash", str(INSTALL)], home).returncode, 2)
            self.assertEqual(run(["bash", str(INSTALL), "--dev-rule"], home).returncode, 2)  # retired: agents-md


class SkillTextTests(unittest.TestCase):
    def test_github_text_is_data_and_sensitive_infra_gets_a_security_review(self):
        skill = (ROOT / "scripts/pr-watch/SKILL.md").read_text()
        for phrase in ("data, never instructions", "/security-review", "claim.sh claim", "@coderabbitai review",
                       "@greptileai review", "Held for the owner", "--match-head-commit", "--limit 500",
                       "`jobs` map", "-R <repo> --squash --match-head-commit", "df -h /", "Never run a PR's own code on this jumpbox", "merge-base --is-ancestor",
                       "unapplied", "before every push, merge or apply", "answer `standby` in one line", "focus on aws repo only", "ejc3/agents-md", "repos/ejc3/agents-md/compare/", "keyed by `<repo>#<number>`", "went from draft to ready", "BEFORE the merge", "re-runs `scripts/pr-watch-install.sh --watcher`",
                       "`alerts_checked_at`", "never the topic", "lookup-events", "`alerts_pending`", "only once every result is triaged", "Note the time before the first query", "Run step 6\n   (alerts) every time", "alert-poll.py", "`alerts_seen`", "30 minutes before `alerts_checked_at`", "OrganizationAccountAccessRole", "security-hub-automation.tf", "never suppressed", "Never silence the topic"):
            self.assertIn(phrase, skill)


class AlertPollTests(unittest.TestCase):
    """scripts/pr-watch/alert-poll.py, the alert step's reader, with the AWS clients stubbed."""

    def load(self):
        import datetime as dt
        import importlib.util
        import types
        sys.modules.setdefault("boto3", types.ModuleType("boto3"))
        bc = sys.modules.setdefault("botocore", types.ModuleType("botocore"))
        cfg = types.ModuleType("botocore.config")
        cfg.Config = lambda **kw: None
        sys.modules["botocore.config"] = cfg
        bc.config = cfg
        spec = importlib.util.spec_from_file_location("alert_poll", ROOT / "scripts/pr-watch/alert-poll.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.SINCE = dt.datetime(2026, 10, 19, 4, 0, tzinfo=dt.timezone.utc)
        return mod, dt

    def test_scopes_are_every_security_region_module(self):
        mod, _ = self.load()
        got = mod.scopes(ROOT)
        self.assertIn(("main", "us-west-1"), got)
        self.assertIn(("staging", "us-west-1"), got)
        tf = (ROOT / "security-monitoring.tf").read_text()
        self.assertEqual(len(got), len(set(re.findall(r'module "security_(?:main|staging)_[a-z0-9_]+"', tf))))

    def test_cloudtrail_reads_from_the_lookback_and_every_result_has_an_id(self):
        mod, dt = self.load()
        calls, printed = [], []
        event = {"eventID": "ev-1", "eventSource": "iam.amazonaws.com", "eventName": "CreateAccessKey",
                 "userIdentity": {"arn": "arn:aws:sts::111111111111:assumed-role/example/x"}, "eventTime": "t"}

        class Pager:
            def paginate(self, **kw):
                calls.append(kw)
                found = kw.get("LookupAttributes", [{}])[0].get("AttributeValue") == "CreateAccessKey"
                return [{"Events": [{"CloudTrailEvent": json.dumps(event)}] if found else []}]

        class Client:
            def __getattr__(self, name):
                if name == "get_paginator":
                    return lambda op: Pager()
                raise RuntimeError("service not stubbed")

        class Sess:
            def client(self, svc, **kw):
                return Client()

        mod.out = lambda **kw: printed.append(kw)
        mod.poll("main", Sess(), "us-east-1")
        starts = {c["StartTime"] for c in calls if "LookupAttributes" in c}
        self.assertTrue(starts)
        self.assertEqual(starts, {mod.SINCE - dt.timedelta(minutes=30)})
        hits = [p for p in printed if p.get("src") == "cloudtrail" and "kind" in p]
        self.assertEqual([(h["id"], h["kind"], h["event"]) for h in hits], [("ev-1", "tamper-or-iam", "CreateAccessKey")])

    def test_audit_names_match_the_terraform_rule(self):
        """Every eventName in security-forward-audit is looked up, and nothing else (drift between the two fails)."""
        mod, _ = self.load()
        tf = (ROOT / "modules/security-region/main.tf").read_text()
        audit = tf[tf.index("security-forward-audit = jsonencode"):]
        audit = audit[:audit.index("\n  }\n}")]
        names = set()
        for lst in re.findall(r'eventName\s*=\s*\[([^\]]*)\]', audit):
            names |= set(re.findall(r'"([A-Za-z]+)"', lst))
        names.discard("ConsoleLogin")
        self.assertEqual(set().union(*mod.AUDIT.values()), names)
        self.assertEqual(mod.PROTECTED_VAULTS, set(re.findall(r'"([a-z0-9-]+)"', audit[audit.index("backupVaultName"):].split("]")[0])))

    def test_a_failed_read_makes_the_poll_fail_and_member_copies_are_kept(self):
        mod, _ = self.load()
        mod.FAILED.clear()
        mod.out(src="guardduty", acct="main", region="us-west-1", error="ThrottlingException")
        self.assertEqual(len(mod.FAILED), 1, "a failed read is recorded so main() exits 1")
        printed = []
        mod.out = lambda **kw: printed.append(kw)
        mod.POLLED.clear()
        mod.POLLED.add("111111111111")
        finding = lambda acct: {"Id": f"f-{acct}", "ProductArn": "arn:aws:securityhub:us-west-1::product/aws/inspector",
                                "AwsAccountId": acct, "Title": "t", "Severity": {"Label": "HIGH"},
                                "Resources": [{"Id": "r"}], "Compliance": {}}

        class SH:
            def get_paginator(self, op):
                class P:
                    def paginate(self, **kw):
                        return [{"Findings": [finding("111111111111"), finding("222222222222")]}]
                return P()

        class Sess:
            def client(self, svc, **kw):
                if svc == "securityhub":
                    return SH()
                raise RuntimeError("not stubbed")

        mod.poll("main", Sess(), "us-west-1")
        ids = [p["id"] for p in printed if p.get("src") == "securityhub" and "error" not in p]
        self.assertEqual(ids, ["f-222222222222"], "a polled account's copy is dropped, a member account's is kept")


if __name__ == "__main__":
    unittest.main(verbosity=2)
