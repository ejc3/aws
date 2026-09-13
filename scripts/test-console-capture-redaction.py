#!/usr/bin/env python3
"""dev-console-capture must never match, archive or email a private key.

dev-diagnostics.tf copies an instance's live console into CloudWatch Logs and an SNS email
when a status check fails. Boot scripts that traced a key under `set -x` (dev-hop-key.tf
before f641900, jumpbox2-user-data.tf before 48373c0) left it on the console, so a capture
would have copied it into both. These tests exec the real Lambda heredoc with boto3 replaced
by fakes and feed it fake keys: no AWS, no network, no real key material.

Run from the repo root:  python3 -S -B scripts/test-console-capture-redaction.py
"""
import contextlib
import io
import json
import sys
import textwrap
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "dev-diagnostics.tf").read_text()
# `<<-PY` strips the common indentation, which is what dedent does; the body sits at column 0.
SOURCE = textwrap.dedent(TF.split("  console_capture_code = <<-PY\n", 1)[1].split("\n  PY\n", 1)[0]) + "\n"

CANARY = "CANARYFAKEKEY"
FILLER = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ"
# Base64-alphabet body lines with the canary at both ends, so any fragment a cut window
# leaves behind still carries it.
BODY = [f"{CANARY}{i:02d}{FILLER}{CANARY}" for i in range(6)]
REDACTED = "[redacted private key]"


def pem(label="OPENSSH PRIVATE KEY", sep="\n", headers=()):
    return sep.join([f"-----BEGIN {label}-----", *headers, *BODY, f"-----END {label}-----"])


HOP_JSON = json.dumps({"private": pem() + "\n", "public": "ssh-ed25519 AAAAC3NzaFAKEPUBLIC dev-hop"})
# What `set -x` printed on every run before the dev-hop fix: the assignment, the test, two printfs.
XTRACE = [
    f"+ HOPJSON='{HOP_JSON}'",
    f"+ '[' -n '{HOP_JSON}' ']'",
    f"+ printf %s '{HOP_JSON}'",
    f"+ printf %s '{HOP_JSON}'",
]
BOOT = [f"[   {i}.000000] systemd[1]: Started fake-unit-{i}.service." for i in range(1, 30)]
PANIC = [
    "[ 4242.000001] kernel BUG at arch/arm64/kvm/nested.c:754!",
    "[ 4242.000002] Internal error: Oops - BUG: 00000000f2000800 [#1] SMP",
    "[ 4242.000003] Kernel panic - not syncing: Oops - BUG: Fatal exception",
]


class FakeLogs:
    class exceptions:
        class ResourceAlreadyExistsException(Exception):
            pass

    def __init__(self):
        self.messages = []

    def create_log_stream(self, logGroupName, logStreamName):
        pass

    def put_log_events(self, logGroupName, logStreamName, logEvents):
        self.messages.extend(event["message"] for event in logEvents)


class FakeSNS:
    def __init__(self):
        self.published = []

    def publish(self, TopicArn, Subject, Message):
        self.published.append(Subject + "\n" + Message)


class FakeEC2:
    def __init__(self, console):
        self.console = console

    def get_console_output(self, InstanceId, Latest=False):
        assert Latest is True, "the capture must read the live buffer"
        return {"Output": self.console, "Timestamp": "2026-09-13T00:00:00Z"}


def load(console=""):
    clients = {"ec2": FakeEC2(console), "logs": FakeLogs(), "sns": FakeSNS()}
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda service, **kw: clients[service]
    sys.modules["boto3"] = fake_boto3
    namespace = {"__name__": "index"}
    exec(compile(SOURCE, "<index.py>", "exec"), namespace)
    namespace["SNS_TOPIC"] = "arn:aws:sns:us-west-1:000000000000:fake-topic"
    return namespace, clients


REDACT = load()[0]["redact"]


def run_handler(namespace):
    msg = {"NewStateValue": "ALARM", "AlarmName": "dev-server-status-check",
           "Trigger": {"Dimensions": [{"name": "InstanceId", "value": "i-0fake"}]}}
    with contextlib.redirect_stdout(io.StringIO()):
        return namespace["lambda_handler"]({"Records": [{"Sns": {"Message": json.dumps(msg)}}]}, None)


class RedactTests(unittest.TestCase):
    def assertNoKey(self, text):
        self.assertNotIn(CANARY, text)
        for i in range(0, len(FILLER) - 12, 6):
            self.assertNotIn(FILLER[i:i + 12], text)

    def assertKept(self, lines, text):
        for line in lines:
            self.assertIn(line, text)

    def test_source_is_the_text_terraform_zips(self):
        self.assertNotIn("${", SOURCE)
        self.assertNotIn("%{", SOURCE)
        self.assertIn('output = redact(resp.get("Output") or "")', SOURCE)

    def test_a_complete_multiline_key_is_removed_and_the_console_kept(self):
        out = REDACT("\n".join(BOOT + ["+ FCVM_KEY='" + pem() + "'"] + PANIC))
        self.assertNoKey(out)
        self.assertIn("+ FCVM_KEY='" + REDACTED + "'", out)
        self.assertKept(BOOT + PANIC, out)

    def test_json_escaped_one_line_keys_from_xtrace_are_removed(self):
        out = REDACT("\n".join(BOOT + XTRACE + PANIC))
        self.assertNoKey(out)
        self.assertEqual(out.count(REDACTED), 4)
        self.assertEqual(out.count("ssh-ed25519 AAAAC3NzaFAKEPUBLIC dev-hop"), 4)
        self.assertKept(BOOT + PANIC, out)

    def test_a_buffer_that_starts_anywhere_inside_a_key(self):
        for key_line, after in (("+ FCVM_KEY='" + pem() + "'", BOOT[3:] + PANIC),
                                (XTRACE[0], BOOT[3:] + PANIC)):
            text = "\n".join(BOOT[:3] + [key_line] + after)
            begin = text.index("-----BEGIN ")
            end = text.index("-----END ")
            for cut in range(begin + 1, end + 1):
                with self.subTest(key=key_line[:12], cut=cut - begin):
                    out = REDACT(text[cut:])
                    self.assertNoKey(out)
                    self.assertKept(after, out)

    def test_a_buffer_that_ends_anywhere_inside_a_key(self):
        for key_line in ("+ FCVM_KEY='" + pem() + "'", XTRACE[0]):
            text = "\n".join(BOOT + PANIC + [key_line])
            begin = text.index("-----BEGIN ")
            end = text.index("PRIVATE KEY-----'") if key_line.startswith("+ FCVM") else text.index("-----END ")
            for cut in range(begin + 1, end + 1):
                with self.subTest(key=key_line[:12], cut=cut - begin):
                    out = REDACT(text[:cut])
                    self.assertNoKey(out)
                    self.assertKept(BOOT + PANIC, out)

    def test_other_key_formats_and_line_breaks(self):
        rsa_headers = ("Proc-Type: 4,ENCRYPTED", "DEK-Info: AES-128-CBC,0123456789ABCDEF0123456789ABCDEF", "")
        for label, headers in (("RSA PRIVATE KEY", rsa_headers), ("EC PRIVATE KEY", ()), ("PRIVATE KEY", ()),
                               ("ENCRYPTED PRIVATE KEY", ()), ("PGP PRIVATE KEY BLOCK", ())):
            # Real line breaks, CRLF from the serial console, `echo $KEY` unquoted, JSON, double JSON.
            for sep in ("\n", "\r\n", " ", "\\n", "\\\\n"):
                key = pem(label, sep, headers)
                with self.subTest(label=label, sep=repr(sep)):
                    whole = "\r\n".join(BOOT + ["+ KEY='" + key + "'"] + PANIC)
                    out = REDACT(whole)
                    self.assertNoKey(out)
                    self.assertKept(BOOT + PANIC, out)
                    middle = len(key) // 2
                    self.assertNoKey(REDACT(key[middle:] + "\n" + "\n".join(PANIC)))
                    self.assertNoKey(REDACT("\n".join(PANIC) + "\n" + key[:middle]))

    def test_quoted_marker_text_in_traced_commands_leaves_the_console_alone(self):
        for text in (
            "\n".join(BOOT + ["+ grep -c '-----BEGIN OPENSSH PRIVATE KEY-----' /var/log/x.log", "0"] + PANIC),
            "\n".join(["+ grep -c \"-----END RSA PRIVATE KEY-----\" /var/log/x.log", "0"] + BOOT + PANIC),
        ):
            self.assertEqual(REDACT(text), text)

    def test_every_shape_in_one_buffer(self):
        cut_json = XTRACE[1][XTRACE[1].index("-----BEGIN ") + 40:]
        text = "\n".join([cut_json] + BOOT + ["+ FCVM_KEY='" + pem() + "'"] + XTRACE + PANIC
                         + [XTRACE[2][:XTRACE[2].index("-----BEGIN ") + 90]])
        out = REDACT(text)
        self.assertNoKey(out)
        self.assertKept(BOOT + PANIC, out)


class HandlerTests(unittest.TestCase):
    CONSOLE = "\n".join(BOOT + XTRACE + ["+ FCVM_KEY='" + pem() + "'"] + PANIC
                        + [XTRACE[3][:XTRACE[3].index("-----BEGIN ") + 90]])

    def test_nothing_matched_logged_or_published_holds_a_key(self):
        namespace, clients = load(self.CONSOLE)
        result = run_handler(namespace)
        logged = "\n".join(clients["logs"].messages)
        published = "\n".join(clients["sns"].published)
        for text in (logged, published, json.dumps(result)):
            RedactTests.assertNoKey(self, text)
        self.assertIn(REDACTED, logged)
        self.assertIn(REDACTED, published)
        self.assertIn("PANIC SIGNATURE FOUND", published)
        for line in PANIC:
            self.assertIn(line, published)
        self.assertEqual(result["captured"][0]["signatures"], len(PANIC))

    def test_the_harness_catches_an_unredacted_capture(self):
        # With redaction bypassed the same run must expose the fake key in both sinks, or
        # the test above would pass without proving anything.
        namespace, clients = load(self.CONSOLE)
        namespace["redact"] = lambda text: text
        run_handler(namespace)
        self.assertIn(CANARY, "\n".join(clients["logs"].messages))
        self.assertIn(CANARY, "\n".join(clients["sns"].published))


if __name__ == "__main__":
    unittest.main(verbosity=2)
