#!/usr/bin/env python3
"""scripts/claude-master-cert-renew.py, scripts/ssm/claude-master-cert-*.sh and claude-master-cert-renew.tf:
no claude-master client certificate expires, and nothing else is possible through the machinery that renews it.

Offline. The four SSM scripts run for real (bash + openssl, a throwaway CA, real keys and certificates, home directories under a
temp dir); the Lambda runs against fake AWS clients; the Terraform is read as text."""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIENTS = [
    {"name": "nextjs-colton", "instance": "nextjs-dev", "account": "colton"},
    {"name": "fcvm-arm", "instance": "fcvm-metal-arm", "account": "ubuntu"},
]
os.environ.update({"CLIENTS": json.dumps(CLIENTS), "SERVER_NAME": "claude-master-server", "RENEW_BEFORE_DAYS": "14",
                   "CERT_DAYS": "30", "MIN_DAYS_ON_INSTALL": "20", "SNS_TOPIC_ARN": "arn:topic", "AWS_REGION": "us-west-1"})
sys.modules.setdefault("boto3", types.ModuleType("boto3"))
SPEC = importlib.util.spec_from_file_location("cr", ROOT / "scripts" / "claude-master-cert-renew.py")
cr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cr)
TF = (ROOT / "claude-master-cert-renew.tf").read_text()
SSM_DIR = ROOT / "scripts" / "ssm"
SCRIPTS = {n: (SSM_DIR / ("claude-master-cert-%s.sh" % n)).read_text() for n in ("status", "request", "sign", "install")}


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, **kw)


def openssl(*args, **kw):
    r = sh(["openssl", *args], **kw)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout


class Pki:
    """A throwaway CA that signs like the server does (EC P-256), plus helpers to make client keys and requests."""

    def __init__(self, root, cn="claude-master process CA"):
        self.root = Path(root)
        self.key, self.pem = self.root / "ca.key", self.root / "ca.pem"
        openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(self.key))
        openssl("req", "-x509", "-new", "-key", str(self.key), "-subj", "/CN=" + cn, "-days", "3650", "-out", str(self.pem))

    def client(self, name, days=30, directory=None):
        d = Path(directory or tempfile.mkdtemp(dir=self.root)); d.mkdir(parents=True, exist_ok=True)
        key, csr, pem = d / "client.key", d / "client.csr", d / "client.pem"
        openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key))
        openssl("req", "-new", "-key", str(key), "-subj", "/CN=" + name, "-out", str(csr))
        self.sign(csr, pem, days)
        return d

    def sign(self, csr, out, days):
        # -days 0 is not accepted by openssl; callers wanting "about to expire" pass 5.
        openssl("x509", "-req", "-in", str(csr), "-CA", str(self.pem), "-CAkey", str(self.key), "-CAcreateserial",
                "-days", str(days), "-out", str(out))


def render(script, **params):
    """What SSM does: substitute {{ Name }} placeholders. A placeholder left over is a bug."""
    out = script
    for k, v in params.items():
        out = out.replace("{{ %s }}" % k, str(v))
    left = re.findall(r"\{\{ *\w+ *\}\}", out)
    assert not left, left
    return out


def run_script(script, env=None, **params):
    return sh(["bash", "-c", render(script, **params)], env=dict(os.environ, **(env or {})))


def b64(path):
    return base64.b64encode(Path(path).read_bytes()).decode()


def kv(stdout):
    return cr.parse_pairs(stdout)


# ------------------------------------------------------------------------------------------------ the four SSM scripts
class ScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.pki = Pki(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def home(self, name="colton", days=30):
        """A fake home whose live ~/.config/claude-master holds a certificate for NAME with DAYS left."""
        home = Path(tempfile.mkdtemp(dir=self.tmp))
        if days is not None:
            self.pki.client("nextjs-" + name, days=days, directory=home / ".config" / "claude-master")
            shutil.copy(self.pki.pem, home / ".config" / "claude-master" / "ca.pem")
        return home

    # status ------------------------------------------------------------------------------------------------------
    def test_status_reports_the_end_date_the_subject_and_a_missing_certificate(self):
        home = self.home(days=30)
        r = run_script(SCRIPTS["status"], {"CM_HOME_OVERRIDE": str(home)}, Account="colton")
        out = kv(r.stdout)
        self.assertEqual((r.returncode, out["STATUS"], out["SUBJECT"]), (0, "ok", "CN=nextjs-colton"), r.stderr)
        self.assertAlmostEqual(int(out["NOTAFTER"]) - time.time(), 30 * 86400, delta=3 * 3600)
        bare = self.home(days=None)
        self.assertEqual(kv(run_script(SCRIPTS["status"], {"CM_HOME_OVERRIDE": str(bare)}, Account="colton").stdout)["STATUS"], "missing")

    def test_status_refuses_an_account_that_does_not_exist(self):
        r = run_script(SCRIPTS["status"], Account="no_such_account_xyz")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(kv(r.stdout)["STATUS"], "no-such-account")

    # request -----------------------------------------------------------------------------------------------------
    def stub_client_init(self, ok=True):
        stub = self.tmp / ("claude-master-stub-%s" % ok)
        stub.write_text('#!/bin/bash\n[ "$1" = client-init ] || exit 2\n'
                        + ('d=$3; n=$5; mkdir -p "$d"; openssl ecparam -name prime256v1 -genkey -noout -out "$d/client.key"; '
                           'openssl req -new -key "$d/client.key" -subj "/CN=$n" -out "$d/client.csr"\n' if ok else "exit 1\n"))
        stub.chmod(0o755)
        return stub

    def test_request_makes_a_pending_key_and_request_and_leaves_the_live_identity_alone(self):
        home = self.home(days=30)
        live = (home / ".config/claude-master/client.pem").read_bytes()
        r = run_script(SCRIPTS["request"], {"CM_HOME_OVERRIDE": str(home), "CM_BIN": str(self.stub_client_init())}, Account="colton", Name="nextjs-colton")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        csr = base64.b64decode(kv(r.stdout)["CSR"])
        subj = openssl("req", "-noout", "-subject", "-nameopt", "RFC2253", input=csr.decode())
        self.assertEqual(subj.strip(), "subject=CN=nextjs-colton")
        self.assertTrue((home / ".config/claude-master.new/client.key").is_file(), "the key is made on the client")
        self.assertEqual((home / ".config/claude-master/client.pem").read_bytes(), live)
        self.assertNotIn("PRIVATE", r.stdout, "the key must never be printed")

    def test_request_refuses_when_the_client_binary_is_missing_or_fails(self):
        home = self.home(days=None)
        for bin_, why in (("/nonexistent/claude-master", "missing"), (str(self.stub_client_init(ok=False)), "failing")):
            r = run_script(SCRIPTS["request"], {"CM_HOME_OVERRIDE": str(home), "CM_BIN": bin_}, Account="colton", Name="nextjs-colton")
            self.assertNotEqual(r.returncode, 0, why)
            self.assertNotIn("CSR=", r.stdout, why)

    # sign --------------------------------------------------------------------------------------------------------
    def sign_env(self):
        sentinel = self.tmp / ("signed-%s" % time.time_ns())
        stub = self.tmp / ("sign-stub-%s" % time.time_ns())
        stub.write_text('#!/bin/bash\ncat > %s\nprintf "CLIENT-PEM\\nCA-PEM\\n"\n' % sentinel)
        stub.chmod(0o755)
        return {"CM_SIGN": str(stub)}, sentinel

    def csr_b64(self, name):
        d = Path(tempfile.mkdtemp(dir=self.tmp))
        openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(d / "k"))
        openssl("req", "-new", "-key", str(d / "k"), "-subj", "/CN=" + name, "-out", str(d / "r"))
        return b64(d / "r")

    def test_sign_signs_only_for_the_exact_name_asked_for(self):
        env, sentinel = self.sign_env()
        csr = self.csr_b64("nextjs-colton")
        ok = run_script(SCRIPTS["sign"], env, Name="nextjs-colton", Csr=csr, Days="30")
        self.assertEqual((ok.returncode, ok.stdout), (0, "CLIENT-PEM\nCA-PEM\n"), ok.stderr)
        self.assertEqual(sentinel.read_text(), csr, "the request reaches the signer unchanged")
        env2, sentinel2 = self.sign_env()
        for asked, why in (("nextjs-connor", "another client"), ("nextjs-colto", "a prefix"), ("NEXTJS-COLTON", "another case")):
            r = run_script(SCRIPTS["sign"], env2, Name=asked, Csr=csr, Days="30")
            self.assertNotEqual(r.returncode, 0, why)
        self.assertFalse(sentinel2.exists(), "the signer must never run for a name the request does not carry")

    def test_sign_refuses_a_bad_encoding_a_tampered_request_and_a_bad_lifetime(self):
        env, sentinel = self.sign_env()
        good = self.csr_b64("nextjs-colton")
        raw = bytearray(base64.b64decode(good)); raw[-20] ^= 0xFF               # corrupt the signature inside the request
        tampered = base64.b64encode(bytes(raw)).decode()
        for csr, days, why in (("!!!not-base64!!!", "30", "encoding"), (tampered, "30", "tampered"), (good, "0", "zero days"), (good, "91", "too long"), (good, "abc", "not a number")):
            r = run_script(SCRIPTS["sign"], env, Name="nextjs-colton", Csr=csr, Days=days)
            self.assertNotEqual(r.returncode, 0, why)
        self.assertFalse(sentinel.exists())

    # install -----------------------------------------------------------------------------------------------------
    def pending(self, home, name="nextjs-colton", days=30, pki=None):
        """A pending ~/.config/claude-master.new with a key and request, and a certificate for it signed with DAYS left."""
        pki = pki or self.pki
        new = home / ".config" / "claude-master.new"
        d = pki.client(name, days=days, directory=new)
        return new, d / "client.pem"

    def install(self, home, cert, ca=None, name="nextjs-colton", min_days=20):
        return run_script(SCRIPTS["install"], {"CM_HOME_OVERRIDE": str(home)}, Account="colton", Name=name,
                          Cert=b64(cert), Ca=b64(ca or self.pki.pem), MinDays=min_days)

    def test_install_swaps_a_verified_certificate_in_and_leaves_no_litter(self):
        home = self.home(days=3)                                               # the old one is nearly expired
        old = (home / ".config/claude-master/client.pem").read_bytes()
        new, cert = self.pending(home, days=30)
        key = (new / "client.key").read_bytes()
        r = self.install(home, cert)
        self.assertEqual((r.returncode, kv(r.stdout)["INSTALL"]), (0, "ok"), r.stdout + r.stderr)
        live = home / ".config/claude-master"
        self.assertNotEqual((live / "client.pem").read_bytes(), old)
        self.assertEqual((live / "client.key").read_bytes(), key, "the live key is the one made by the request")
        enddate = openssl("x509", "-noout", "-enddate", "-in", str(live / "client.pem")).strip().split("=", 1)[1]
        self.assertEqual(kv(r.stdout)["NOTAFTER"], sh(["date", "-u", "-d", enddate, "+%s"]).stdout.strip())
        self.assertFalse((home / ".config/claude-master.new").exists())
        self.assertFalse((home / ".config/claude-master.old").exists())
        self.assertEqual(oct((live / "client.key").stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct((live / "client.pem").stat().st_mode & 0o777), "0o644")

    def test_install_works_when_there_is_no_live_directory_yet(self):
        home = self.home(days=None)
        _, cert = self.pending(home)
        r = self.install(home, cert)
        self.assertEqual(kv(r.stdout).get("INSTALL"), "ok", r.stdout + r.stderr)
        self.assertTrue((home / ".config/claude-master/client.pem").is_file())

    def refuses(self, why, home, cert, **kw):
        before = {p: p.read_bytes() for p in (home / ".config/claude-master").glob("*") if p.is_file()}
        r = self.install(home, cert, **kw)
        self.assertNotEqual(r.returncode, 0, why)
        self.assertNotEqual(kv(r.stdout).get("INSTALL"), "ok", why)
        after = {p: p.read_bytes() for p in (home / ".config/claude-master").glob("*") if p.is_file()}
        self.assertEqual(before, after, why + ": the live identity must be untouched")

    def test_install_refuses_everything_it_cannot_vouch_for_and_keeps_the_old_identity(self):
        home = self.home(days=20)
        new, cert = self.pending(home, days=30)
        self.refuses("wrong name", home, cert, name="nextjs-connor")
        other = Pki(tempfile.mkdtemp(dir=self.tmp), cn="some other CA")
        self.refuses("signed by a different CA", home, cert, ca=other.pem)
        # a certificate for a DIFFERENT key than the pending one
        home2 = self.home(days=20)
        new2, _ = self.pending(home2, days=30)
        stranger = self.pki.client("nextjs-colton", days=30)
        self.refuses("key mismatch", home2, stranger / "client.pem")
        # lasts too few days
        home3 = self.home(days=20)
        _, short = self.pending(home3, days=5)
        self.refuses("expires too soon", home3, short)
        # nothing pending
        home4 = self.home(days=20)
        self.refuses("no pending request", home4, cert)
        # bad encoding
        home5 = self.home(days=20)
        self.pending(home5)
        r = run_script(SCRIPTS["install"], {"CM_HOME_OVERRIDE": str(home5)}, Account="colton", Name="nextjs-colton",
                       Cert="@@@", Ca=b64(self.pki.pem), MinDays=20)
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue((home5 / ".config/claude-master/client.pem").is_file())
        self.assertTrue((home5 / ".config/claude-master.new").is_dir(), "a refused install leaves the pending request in place")


# ---------------------------------------------------------------------------------------------------- the Lambda
class InvocationDoesNotExist(Exception):
    pass


class FakeWorld:
    """EC2 + SSM + CloudWatch + SNS in one object. `responders` maps (document, instance_id) -> callable(params) -> (status, stdout)."""

    def __init__(self, days_left, now, instances=None, sign_fails=False, install_fails=False, missing=(), renewed_days=30):
        self.now, self.log, self.metrics, self.published = now, [], [], []
        self.days = dict(days_left)
        self.instances = instances if instances is not None else {"claude-master-server": "i-srv", "nextjs-dev": "i-nx", "fcvm-metal-arm": "i-arm"}
        self.by_id = {v: k for k, v in self.instances.items()}
        self.sign_fails, self.install_fails, self.missing, self.renewed_days = sign_fails, install_fails, set(missing), renewed_days
        self.results, self.polls = {}, {}

    # ec2
    def describe_instances(self, Filters):
        name = [f for f in Filters if f["Name"] == "tag:Name"][0]["Values"][0]
        iid = self.instances.get(name)
        return {"Reservations": [{"Instances": [{"InstanceId": iid}]}] if iid else []}

    # ssm
    def send_command(self, InstanceIds, DocumentName, Parameters, TimeoutSeconds):
        params = {k: v[0] for k, v in Parameters.items()}
        self.log.append((DocumentName, InstanceIds[0], params))
        cid = "cmd-%d" % len(self.log)
        self.results[cid] = self.respond(DocumentName, InstanceIds[0], params)
        return {"Command": {"CommandId": cid}}

    def get_command_invocation(self, CommandId, InstanceId):
        self.polls[CommandId] = self.polls.get(CommandId, 0) + 1
        if self.polls[CommandId] == 1:
            raise InvocationDoesNotExist("InvocationDoesNotExist")
        status, out = self.results[CommandId]
        return {"Status": status, "StandardOutputContent": out, "StandardErrorContent": ""}

    def respond(self, doc, iid, p):
        if doc == cr.DOC_STATUS:
            key = (self.by_id[iid], p["Account"])
            if key in self.missing:
                return "Success", "STATUS=missing\n"
            return "Success", "STATUS=ok\nNOTAFTER=%d\nSUBJECT=CN=x\n" % (self.now + self.days[key] * 86400)
        if doc == cr.DOC_REQUEST:
            return "Success", "CSR=" + base64.b64encode(b"x" * 120).decode() + "\n"
        if doc == cr.DOC_SIGN:
            if self.sign_fails:
                return "Failed", "SIGN=name-mismatch"
            return "Success", "-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----\n-----BEGIN CERTIFICATE-----\nBBB\n-----END CERTIFICATE-----\n"
        if doc == cr.DOC_INSTALL:
            if self.install_fails:
                return "Failed", "INSTALL=key-mismatch"
            self.days[(self.by_id[iid], p["Account"])] = self.renewed_days
            return "Success", "INSTALL=ok\nNOTAFTER=0\n"
        raise AssertionError("an unknown document was sent: " + doc)

    # cloudwatch / sns
    def put_metric_data(self, Namespace, MetricData):
        self.metrics.append((Namespace, MetricData[0]["Dimensions"][0]["Value"], MetricData[0]["Value"]))

    def publish(self, TopicArn, Subject, Message):
        self.published.append((Subject, Message))

    def clients(self):
        return {"ec2": self, "ssm": self, "cw": self, "sns": self}


NOW = 1_800_000_000
NO_SLEEP = lambda s: None


def invoke(world, event=None):
    return cr.lambda_handler(event or {}, None, clients=world.clients(), now=NOW, sleep=NO_SLEEP)


def days(colton, arm):
    return {("nextjs-dev", "colton"): colton, ("fcvm-metal-arm", "ubuntu"): arm}


class LambdaTests(unittest.TestCase):
    def test_healthy_certificates_are_read_and_left_alone(self):
        w = FakeWorld(days(25, 19), NOW)
        invoke(w)
        self.assertEqual({d for d, _, _ in w.log}, {cr.DOC_STATUS})
        self.assertEqual(sorted((c, round(v)) for _, c, v in w.metrics), [("fcvm-arm", 19), ("nextjs-colton", 25)])
        self.assertTrue(all(ns == "ClaudeMasterCerts" for ns, _, _ in w.metrics))
        self.assertEqual(w.published, [])

    def test_a_certificate_inside_the_window_is_renewed_in_the_right_order_with_the_right_names(self):
        w = FakeWorld(days(10, 25), NOW)
        out = invoke(w)
        docs = [(d, i) for d, i, _ in w.log]
        # one client at a time: read, request, sign (on the server), install, read again; then the next client
        self.assertEqual(docs, [(cr.DOC_STATUS, "i-nx"), (cr.DOC_REQUEST, "i-nx"), (cr.DOC_SIGN, "i-srv"), (cr.DOC_INSTALL, "i-nx"),
                                (cr.DOC_STATUS, "i-nx"), (cr.DOC_STATUS, "i-arm")])
        req = [p for d, _, p in w.log if d == cr.DOC_REQUEST][0]
        self.assertEqual(req, {"Account": "colton", "Name": "nextjs-colton"})
        sign = [p for d, _, p in w.log if d == cr.DOC_SIGN][0]
        self.assertEqual((sign["Name"], sign["Days"]), ("nextjs-colton", "30"))
        inst = [p for d, _, p in w.log if d == cr.DOC_INSTALL][0]
        self.assertEqual((inst["Account"], inst["Name"], inst["MinDays"]), ("colton", "nextjs-colton", "20"))
        self.assertEqual(base64.b64decode(inst["Cert"]).decode(), "-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----\n")
        self.assertEqual(base64.b64decode(inst["Ca"]).decode(), "-----BEGIN CERTIFICATE-----\nBBB\n-----END CERTIFICATE-----\n")
        self.assertTrue(any("RENEWED" in line for line in out["report"]))
        self.assertEqual(w.published, [])
        self.assertEqual(round([v for _, c, v in w.metrics if c == "nextjs-colton"][-1]), 30, "the metric shows the new date")

    def test_an_already_expired_certificate_is_renewed_too(self):
        w = FakeWorld(days(-12, 25), NOW)
        invoke(w)
        self.assertIn(cr.DOC_INSTALL, [d for d, _, _ in w.log])

    def test_dry_run_changes_nothing_and_says_what_it_would_do(self):
        w = FakeWorld(days(3, 25), NOW)
        out = invoke(w, {"dry_run": True})
        self.assertEqual({d for d, _, _ in w.log}, {cr.DOC_STATUS})
        self.assertTrue(any("WOULD renew" in line for line in out["report"]))

    def test_renew_before_days_can_be_raised_to_force_a_renewal(self):
        w = FakeWorld(days(25, 25), NOW)
        invoke(w, {"renew_before_days": 60})
        self.assertEqual([d for d, _, _ in w.log].count(cr.DOC_INSTALL), 2)

    def test_a_box_that_is_not_running_is_skipped_not_failed(self):
        w = FakeWorld(days(3, 25), NOW, instances={"claude-master-server": "i-srv", "nextjs-dev": "i-nx"})
        out = invoke(w)
        self.assertTrue(any("fcvm-metal-arm is not running" in line for line in out["report"]))
        self.assertEqual(w.published, [])

    def test_an_account_with_no_certificate_is_not_enrolled_and_is_never_created(self):
        w = FakeWorld(days(3, 25), NOW, missing=[("nextjs-dev", "colton")])
        out = invoke(w)
        self.assertTrue(any("not enrolled" in line for line in out["report"]))
        self.assertNotIn(cr.DOC_REQUEST, [d for d, _, _ in w.log])

    def test_one_failure_does_not_stop_the_others_and_is_reported_loudly(self):
        w = FakeWorld(days(3, 4), NOW, sign_fails=True)
        with self.assertRaises(RuntimeError) as ctx:
            invoke(w)
        self.assertIn("2 client(s) failed", str(ctx.exception))
        self.assertEqual([d for d, _, _ in w.log].count(cr.DOC_SIGN), 2, "the second client was still tried")
        self.assertNotIn(cr.DOC_INSTALL, [d for d, _, _ in w.log], "nothing is installed after a failed signing")
        self.assertEqual(len(w.published), 1)
        self.assertIn("FAILED", w.published[0][0])

    def test_a_failed_install_is_a_failure_even_though_everything_before_it_worked(self):
        w = FakeWorld(days(3, 25), NOW, install_fails=True)
        with self.assertRaises(RuntimeError):
            invoke(w)

    def test_a_renewal_that_leaves_a_short_certificate_is_a_failure(self):
        w = FakeWorld(days(3, 25), NOW, renewed_days=5)
        with self.assertRaises(RuntimeError):
            invoke(w)

    def test_with_the_server_down_the_client_that_needs_renewing_fails_and_the_healthy_one_does_not(self):
        w = FakeWorld(days(3, 25), NOW, instances={"nextjs-dev": "i-nx", "fcvm-metal-arm": "i-arm"})
        with self.assertRaises(RuntimeError) as ctx:
            invoke(w)
        self.assertIn("1 client(s) failed", str(ctx.exception))
        self.assertIn("server is not running", str(ctx.exception))

    def test_only_the_four_fixed_documents_are_ever_sent(self):
        w = FakeWorld(days(3, 4), NOW)
        invoke(w)
        self.assertTrue({d for d, _, _ in w.log} <= {cr.DOC_STATUS, cr.DOC_REQUEST, cr.DOC_SIGN, cr.DOC_INSTALL})
        self.assertNotIn("AWS-RunShellScript", {d for d, _, _ in w.log})

    def test_the_signing_step_only_ever_goes_to_the_server(self):
        w = FakeWorld(days(3, 4), NOW)
        invoke(w)
        self.assertEqual({i for d, i, _ in w.log if d == cr.DOC_SIGN}, {"i-srv"})
        self.assertNotIn("i-srv", {i for d, i, _ in w.log if d != cr.DOC_SIGN})

    def test_a_step_that_never_finishes_is_an_error_not_a_hang(self):
        class Stuck(FakeWorld):
            def get_command_invocation(self, CommandId, InstanceId):
                return {"Status": "InProgress", "StandardOutputContent": ""}
        w = Stuck(days(3, 25), NOW)
        with self.assertRaises(RuntimeError) as ctx:
            invoke(w)
        self.assertIn("did not finish", str(ctx.exception))


# ------------------------------------------------------------------------------------------------------ the Terraform
class TerraformTests(unittest.TestCase):
    def test_every_script_placeholder_is_a_declared_parameter_and_every_parameter_is_used(self):
        for n, script in SCRIPTS.items():
            used = set(re.findall(r"\{\{ *(\w+) *\}\}", script))
            block = TF.split('"claude-master-cert-%s" = {' % n, 1)[1].split("\n    }\n", 1)[0]
            declared = set(re.findall(r"^\s{8}(\w+)\s+= \{ type", block, re.M))
            self.assertEqual(used, declared, n)

    def test_every_parameter_is_pinned_by_a_pattern(self):
        params = re.findall(r"^\s{8}\w+\s+= \{ type = \"String\"[^\n]*", TF, re.M)
        self.assertEqual(len(params), 1 + 2 + 3 + 5)
        self.assertTrue(all("allowedPattern = local.cert_" in p for p in params), [p for p in params if "allowedPattern" not in p])

    def test_the_patterns_reject_what_could_become_a_command(self):
        pat = {k: re.compile(v) for k, v in re.findall(r'(cert_\w+_pattern)\s+= "([^"]+)"', TF)}
        for bad in ("a;b", "a b", "a$(id)", "../x", "A", "", "-x" * 40):
            self.assertIsNone(pat["cert_account_pattern"].match(bad), bad)
        for bad in ("a;b", "a b", "NAME", "-x", "a" * 64, ""):
            self.assertIsNone(pat["cert_name_pattern"].match(bad), bad)
        for bad in ("x" * 99, "a b" * 40, "a;b" + "x" * 100, "$(id)" + "x" * 100, "x" * 4097):
            self.assertIsNone(pat["cert_b64_csr_pattern"].match(bad), bad[:20])
        for bad in ("", "100", "1 2", "9;", "-1", "ab"):
            self.assertIsNone(pat["cert_days_pattern"].match(bad), bad)
        self.assertTrue(pat["cert_name_pattern"].match("nextjs-colton") and pat["cert_account_pattern"].match("ubuntu"))

    def test_the_role_can_send_only_the_four_documents_to_only_the_named_boxes(self):
        policy = TF.split('resource "aws_iam_role_policy" "claude_master_cert_renew"', 1)[1].split("\nresource ", 1)[0]
        policy = "\n".join(re.sub(r"\s+#.*$", "", l) for l in policy.splitlines() if not l.strip().startswith("#"))   # comments grant nothing
        self.assertNotIn("AWS-RunShellScript", policy)
        self.assertNotIn('"ssm:*"', policy)
        self.assertNotRegex(policy, r'Action\s*=\s*"\*"')
        self.assertIn('for name in keys(local.claude_master_cert_documents) : "arn:aws:ssm:', policy)
        self.assertIn('"ssm:resourceTag/Name" = local.claude_master_cert_instance_names', policy)
        self.assertEqual(sorted(re.findall(r'"(claude-master-cert-\w+)" = \{\n\s+script', TF)),
                         ["claude-master-cert-install", "claude-master-cert-request", "claude-master-cert-sign", "claude-master-cert-status"])
        for action in re.findall(r'Action\s*=\s*(\[[^\]]*\]|"[^"]+")', policy):
            for a in re.findall(r'"([^"]+)"', action):
                self.assertIn(a, {"ec2:DescribeInstances", "ssm:SendCommand", "ssm:GetCommandInvocation", "ssm:ListCommandInvocations",
                                  "cloudwatch:PutMetricData", "sns:Publish"}, a)

    def test_the_clients_are_the_enrolled_ones_and_feed_the_function(self):
        for name, instance, account in (("nextjs-colton", "nextjs-dev", "colton"), ("nextjs-connor", "nextjs-dev", "connor"),
                                        ("nextjs-ejc3", "nextjs-dev", "ejc3"), ("fcvm-arm", "fcvm-metal-arm", "ubuntu")):
            self.assertRegex(TF, r'"%s"\s+= \{ instance = "%s", account = "%s" \}' % (name, instance, account))
        self.assertIn("CLIENTS             = jsonencode([for name, c in local.claude_master_clients", TF)

    def test_it_runs_daily_one_at_a_time_and_is_watched(self):
        self.assertIn('schedule_expression = "cron(0 8 * * ? *)"', TF)
        self.assertIn("reserved_concurrent_executions = 1", TF)
        self.assertIn("maximum_retry_attempts = 0", TF)
        for alarm in ("claude_master_cert_renew_errors", "claude_master_cert_renew_not_running", "claude_master_cert_expiring"):
            self.assertIn('resource "aws_cloudwatch_metric_alarm" "%s"' % alarm, TF)
        self.assertIn("for_each = local.claude_master_clients", TF)
        self.assertRegex(TF, r'RENEW_BEFORE_DAYS\s+= "14"')

    def test_the_scripts_never_touch_services_or_run_anything_downloaded(self):
        for n, s in SCRIPTS.items():
            self.assertNotRegex(s, r"systemctl|service |kill|curl|wget|eval |sh -c|pkill", n)


if __name__ == "__main__":
    unittest.main()
