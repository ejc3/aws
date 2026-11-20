#!/usr/bin/env python3
"""The parent-zone wildcard audit against a local CA hierarchy and CRL server: the listing, and a
revocation proof that trusts nothing from the secret until it chains to a trusted root, fetches only
approved CRL URLs, and passes only on OpenSSL's "certificate revoked" at the leaf."""
import contextlib
import datetime as dt
import http.server
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

HERE = Path(__file__).with_name("fcvm-parent-wildcard-audit.py")
spec = importlib.util.spec_from_file_location("audit", HERE)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class Secrets:
    """list_secret_version_ids and get_secret_value over fixed versions, paged two at a time; no writes."""

    def __init__(self, versions):
        self.versions = versions

    def list_secret_version_ids(self, SecretId, IncludeDeprecated, MaxResults, NextToken=None):
        assert SecretId == audit.LEGACY and IncludeDeprecated
        start = int(NextToken or 0)
        page = {"Versions": [{"VersionId": v} for v in list(self.versions)[start:start + 2]]}
        if start + 2 < len(self.versions):
            page["NextToken"] = str(start + 2)
        return page

    def get_secret_value(self, SecretId, VersionId):
        return {"SecretString": json.dumps(self.versions[VersionId])}


class AuditTests(unittest.TestCase):
    """root (trusted) -> int1 -> int2 -> leaf, with int2 signing the leaf's CRL."""

    def setUp(self):
        stack = self.enterContext(contextlib.ExitStack())
        self.directory = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.requests = []
        directory, requests = self.directory, self.requests

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append(self.path)
                if self.path == "/redirect.crl":
                    self.send_response(302)
                    self.send_header("Location", "/ca.crl")
                    self.end_headers()
                    return
                path = directory / self.path.lstrip("/")
                if not path.is_file():
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(path.read_bytes())

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        stack.callback(self.server.server_close)
        stack.callback(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.proof = {"trust": self.directory / "root.crt", "crl_url": rf"http://127\.0\.0\.1:{self.server.server_port}/[a-z0-9-]+\.crl"}
        self.openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "60",
                     "-subj", "/CN=Fixture Root", "-addext", "basicConstraints=critical,CA:TRUE",
                     "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-keyout", "root.key", "-out", "root.crt")
        self.intermediate("int1", "/CN=Fixture Intermediate 1", "root")
        self.intermediate("int2", "/CN=Fixture Intermediate 2", "int1")

    def openssl(self, *args):
        subprocess.run(["openssl", *args], cwd=self.directory, check=True, capture_output=True)

    def intermediate(self, name, subject, signer, idp=None):
        """A CA `name` signed by `signer`, with its own database and CRL settings."""
        (self.directory / "ca-ext.cnf").write_text("basicConstraints=critical,CA:TRUE\n"
                                                   "keyUsage=critical,keyCertSign,cRLSign\n")
        self.openssl("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-subj", subject,
                     "-keyout", name + ".key", "-out", name + ".csr")
        self.openssl("x509", "-req", "-in", name + ".csr", "-CA", signer + ".crt", "-CAkey", signer + ".key",
                     "-days", "45", "-extfile", "ca-ext.cnf", "-out", name + ".crt")
        self.ca_config(name, idp)

    def ca_config(self, name, idp=None):
        (self.directory / (name + ".idx")).write_text("")
        (self.directory / (name + ".num")).write_text("01\n")
        ext = (f"crl_extensions=crl_ext\n[crl_ext]\nissuingDistributionPoint=critical,@idp\n[idp]\nfullname=URI:{idp}\n"
               if idp else "")
        (self.directory / (name + ".cnf")).write_text(
            f"[ca]\ndefault_ca=d\n[d]\ndatabase={name}.idx\ncrlnumber={name}.num\ndefault_md=sha256\n"
            f"default_crl_days=7\nnew_certs_dir=.\ncertificate={name}.crt\nprivate_key={name}.key\n{ext}")

    def leaf(self, name, sans, crl="ca.crl", signer="int2", chain=("int2", "int1")):
        (self.directory / "ext.cnf").write_text(
            (f"crlDistributionPoints=URI:{self.base}/{crl}\n" if crl else "")
            + "subjectAltName=" + ",".join("DNS:" + s for s in sans) + "\n")
        self.openssl("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-subj", "/CN=T",
                     "-keyout", name + ".key", "-out", name + ".csr")
        self.openssl("x509", "-req", "-in", name + ".csr", "-CA", signer + ".crt", "-CAkey", signer + ".key",
                     "-days", "20", "-extfile", "ext.cnf", "-out", name + ".crt")
        pem = "".join((self.directory / (n + ".crt")).read_text() for n in (name, *chain))
        return {"cert": pem, "key": (self.directory / (name + ".key")).read_text(), "scene_token": "t"}

    def crl(self, ca="int2", out="ca.crl", revoke=(), dates=()):
        for name in revoke:
            self.openssl("ca", "-config", ca + ".cnf", "-revoke", name + ".crt", "-crl_reason", "superseded")
        self.openssl("ca", "-config", ca + ".cnf", "-gencrl", *dates, "-out", out + ".pem")
        self.openssl("crl", "-in", out + ".pem", "-outform", "DER", "-out", out)

    def check(self, versions, now=None, **proof):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            passed = audit.run(Secrets(versions), self.directory, check=True, now=now, **{**self.proof, **proof})
        return passed, out.getvalue()

    def no_status(self, versions, reason, now=None, **proof):
        passed, out = self.check(versions, now, **proof)
        self.assertFalse(passed, out)
        self.assertIn("NO STATUS (" + reason, out)
        return out

    def test_lists_each_parent_zone_certificate_once_and_says_whether_its_key_is_stored(self):
        parent = self.leaf("parent", ["*.fcvm.dev", "*.tiles-stage.fcvm.dev", "fcvm.dev"])
        other = self.leaf("other", ["fcvm.dev"])
        subzone = self.leaf("subzone", ["*.tiles-stage.fcvm.dev"])
        versions = {"v1": parent, "v2": parent, "v3": dict(other, key=subzone["key"]), "v4": subzone}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertTrue(audit.run(Secrets(versions), self.directory))
        text = out.getvalue()
        self.assertEqual(text.splitlines()[0], f"2 certificate(s) for names outside the fleet subzones in {audit.LEGACY}")
        self.assertIn("versions 2 key stored", text)
        self.assertIn("names fcvm.dev versions 1 key MISSING", text)
        self.assertNotIn("PRIVATE KEY", text)
        self.assertEqual(self.requests, [], "listing fetches nothing")

    def test_an_empty_inventory_lists_but_never_checks(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(audit.run(Secrets({}), self.directory))
        passed, out = self.check({"v1": self.leaf("subzone", ["*.tiles-stage.fcvm.dev"])})
        self.assertFalse(passed)
        self.assertIn("NO STATUS: fcvm-fleet/tls-wildcard holds no certificate", out)
        self.assertFalse(self.check({})[0])

    def test_revoked_at_the_leaf_passes_and_not_listed_fails(self):
        gone, live = self.leaf("gone", ["*.fcvm.dev"]), self.leaf("live", ["fcvm.dev"])
        self.crl(revoke=["gone"])
        passed, out = self.check({"v1": gone})
        self.assertTrue(passed, out)
        self.assertIn("revoked: serial", out)
        passed, out = self.check({"v1": gone, "v2": live})
        self.assertFalse(passed)
        self.assertIn("not revoked: serial", out)

    def test_a_chain_that_does_not_reach_the_trusted_root_fetches_nothing(self):
        # A writable secret can carry any intermediates: a self-made root and CA under the same names.
        self.openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "60",
                     "-subj", "/CN=Fixture Root", "-addext", "basicConstraints=critical,CA:TRUE",
                     "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-keyout", "fake.key", "-out", "fake.crt")
        self.intermediate("rogue", "/CN=Fixture Intermediate 2", "fake")
        cert = self.leaf("gone", ["*.fcvm.dev"], signer="rogue", chain=("rogue", "fake"))
        self.crl(ca="rogue", revoke=["gone"])
        self.no_status({"v1": cert}, "the certificate does not verify through its stored intermediates")
        self.no_status({"v1": dict(cert, cert=audit.chain(cert["cert"])[0])}, "the secret version stores no intermediate")
        # An intermediate openssl cannot load: it exits non-zero without an "error N at depth" line.
        good = self.leaf("good", ["*.fcvm.dev"])
        corrupt = audit.chain(good["cert"])[0] + "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
        self.no_status({"v1": dict(good, cert=corrupt)}, "the certificate does not verify through its stored intermediates")
        self.assertEqual(self.requests, [])

    def test_a_crl_signed_by_another_key_under_the_issuers_name_is_no_status(self):
        cert = self.leaf("gone", ["*.fcvm.dev"])
        self.openssl("ca", "-config", "int2.cnf", "-revoke", "gone.crt")
        # Same issuer name and database as int2, another key.
        self.openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "30",
                     "-subj", "/CN=Fixture Intermediate 2", "-keyout", "forged.key", "-out", "forged.crt")
        (self.directory / "forged.cnf").write_text((self.directory / "int2.cnf").read_text()
                                                   .replace("int2.key", "forged.key").replace("int2.crt", "forged.crt"))
        self.crl(ca="forged")
        self.no_status({"v1": cert}, "openssl verify -crl_check")

    def test_a_crl_from_another_issuer_is_no_status(self):
        cert = self.leaf("gone", ["*.fcvm.dev"])
        self.intermediate("other", "/CN=Another CA", "int1")
        self.openssl("ca", "-config", "other.cnf", "-revoke", "gone.crt")
        self.crl(ca="other")
        self.no_status({"v1": cert}, "openssl verify -crl_check")

    def test_a_stale_or_future_crl_is_no_status(self):
        cert = self.leaf("gone", ["*.fcvm.dev"])
        self.crl(revoke=["gone"])
        now = dt.datetime.now(dt.timezone.utc)
        self.no_status({"v1": cert}, "openssl verify -crl_check", now=now + dt.timedelta(days=8))
        stamp = lambda t: t.strftime("%Y%m%d%H%M%SZ")
        self.crl(dates=("-crl_lastupdate", stamp(now + dt.timedelta(days=1)),
                        "-crl_nextupdate", stamp(now + dt.timedelta(days=8))))
        self.no_status({"v1": cert}, "openssl verify -crl_check", now=now)

    def test_a_crl_scoped_to_another_distribution_point_is_no_status(self):
        cert = self.leaf("gone", ["*.fcvm.dev"])
        self.ca_config("int2", idp=f"{self.base}/shard-2.crl")
        self.openssl("ca", "-config", "int2.cnf", "-revoke", "gone.crt")
        self.crl()
        self.no_status({"v1": cert}, "openssl verify -crl_check")
        self.ca_config("int2", idp=f"{self.base}/ca.crl")
        self.openssl("ca", "-config", "int2.cnf", "-revoke", "gone.crt")
        self.crl()
        self.assertTrue(self.check({"v1": cert})[0])

    def test_fetches_are_restricted_to_approved_endpoints_without_redirects_and_bounded(self):
        self.no_status({"v1": self.leaf("plain", ["*.fcvm.dev"], crl=None)}, "the certificate names no CRL")
        elsewhere = self.leaf("elsewhere", ["*.fcvm.dev"])
        self.no_status({"v1": elsewhere}, "the certificate's CRL is not at an approved CRL endpoint",
                       crl_url=r"http://[a-z0-9]+\.c\.lencr\.org/[0-9]+\.crl")
        self.assertEqual(self.requests, [], "an unapproved URL is never contacted")
        moved = self.leaf("moved", ["*.fcvm.dev"], crl="redirect.crl")
        self.crl(revoke=["moved"])
        self.no_status({"v1": moved}, "the CRL did not download")
        self.assertEqual(self.requests, ["/redirect.crl"], "the redirect is not followed")
        self.no_status({"v1": elsewhere}, "the CRL is larger than the fetch bound", max_bytes=16)
        self.no_status({"v1": self.leaf("missing", ["*.fcvm.dev"], crl="absent.crl")}, "the CRL did not download")

    def test_the_production_endpoint_pattern(self):
        for url, ok in [("http://ye1.c.lencr.org/12.crl", True), ("http://r10.c.lencr.org/0.crl", True),
                        ("https://ye1.c.lencr.org/12.crl", False), ("http://ye1.c.lencr.org.evil/1.crl", False),
                        ("http://ye1.c.lencr.org:8080/1.crl", False), ("http://ye1.c.lencr.org/1.crl?x", False),
                        ("http://evil.example/ye1.c.lencr.org/1.crl", False)]:
            self.assertEqual(bool(__import__("re").fullmatch(audit.LETS_ENCRYPT_CRL, url)), ok, url)

    def test_names_outside_the_subzones(self):
        for name, outside in [("*.fcvm.dev", True), ("fcvm.dev", True), ("fleet-api.fcvm.dev", True),
                              ("*.tiles-stage.fcvm.dev", False), ("*.tiles.fcvm.dev", False),
                              ("x.tiles.fcvm.dev", False), ("*.example.com", False), ("evilfcvm.dev", False)]:
            self.assertEqual(audit.outside_subzones(name), outside, name)

    def test_the_audit_only_reads(self):
        # #929 ships the audit only: revocation and token retirement are a separate approved step (#922).
        source = HERE.read_text()
        for forbidden in ("revokeCert", "newNonce", "method=", "data=", "put_secret_value", "update_secret",
                          "delete_secret", "tag_resource", "POST"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main(buffer=True)
