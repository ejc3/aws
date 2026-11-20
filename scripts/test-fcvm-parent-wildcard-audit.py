#!/usr/bin/env python3
"""The parent-zone wildcard audit against a local CA and CRL server: the listing, and a CRL proof that
counts only a CRL signed by the certificate's issuer, naming it, current, and for the URL it came from."""
import contextlib
import datetime as dt
import functools
import http.server
import importlib.util
import io
import json
from pathlib import Path
import re
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
    def setUp(self):
        stack = self.enterContext(contextlib.ExitStack())
        self.directory = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        quiet = type("Quiet", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *a: None})
        handler = functools.partial(quiet, directory=str(self.directory))
        self.server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        stack.callback(self.server.server_close)
        stack.callback(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.ca("ca", "/CN=Fixture Intermediate")

    def openssl(self, *args):
        subprocess.run(["openssl", *args], cwd=self.directory, check=True, capture_output=True)

    def ca(self, name, subject, idp=None):
        """A CA directory `name` with its own key, database and CRL settings."""
        self.openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "30",
                     "-subj", subject, "-addext", "basicConstraints=critical,CA:TRUE",
                     "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-keyout", name + ".key", "-out", name + ".crt")
        (self.directory / (name + ".idx")).write_text("")
        (self.directory / (name + ".num")).write_text("01\n")
        ext = f"crl_extensions=crl_ext\n[crl_ext]\nissuingDistributionPoint=@idp\n[idp]\nfullname=URI:{idp}\n" if idp else ""
        (self.directory / (name + ".cnf")).write_text(
            f"[ca]\ndefault_ca=d\n[d]\ndatabase={name}.idx\ncrlnumber={name}.num\ndefault_md=sha256\n"
            f"default_crl_days=7\nnew_certs_dir=.\ncertificate={name}.crt\nprivate_key={name}.key\n{ext}")

    def leaf(self, name, sans, crl="ca.crl", ca="ca"):
        (self.directory / "ext.cnf").write_text(
            (f"crlDistributionPoints=URI:{self.base}/{crl}\n" if crl else "")
            + "subjectAltName=" + ",".join("DNS:" + s for s in sans) + "\n")
        self.openssl("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-subj", "/CN=T",
                     "-keyout", name + ".key", "-out", name + ".csr")
        self.openssl("x509", "-req", "-in", name + ".csr", "-CA", ca + ".crt", "-CAkey", ca + ".key", "-days", "20",
                     "-extfile", "ext.cnf", "-out", name + ".crt")
        cert = (self.directory / (name + ".crt")).read_text()
        return {"cert": cert + (self.directory / (ca + ".crt")).read_text(),
                "key": (self.directory / (name + ".key")).read_text(), "scene_token": "t"}

    def crl(self, ca="ca", out="ca.crl", revoke=()):
        for name in revoke:
            self.openssl("ca", "-config", ca + ".cnf", "-revoke", name + ".crt", "-crl_reason", "superseded")
        self.openssl("ca", "-config", ca + ".cnf", "-gencrl", "-out", out + ".pem")
        self.openssl("crl", "-in", out + ".pem", "-outform", "DER", "-out", out)

    def check(self, versions, now=None):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            passed = audit.run(Secrets(versions), self.directory, check=True, now=now)
        return passed, out.getvalue()

    def test_lists_each_parent_zone_certificate_once_and_says_whether_its_key_is_stored(self):
        parent = self.leaf("parent", ["*.fcvm.dev", "*.tiles-stage.fcvm.dev", "fcvm.dev"])
        other = self.leaf("other", ["fcvm.dev"])
        subzone = self.leaf("subzone", ["*.tiles-stage.fcvm.dev"])
        versions = {"v1": parent, "v2": parent, "v3": dict(other, key=subzone["key"]), "v4": subzone}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertTrue(audit.run(Secrets(versions), self.directory))
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], f"2 certificate(s) for names outside the fleet subzones in {audit.LEGACY}")
        self.assertIn("versions 2 key stored", out.getvalue())
        self.assertIn("names fcvm.dev versions 1 key MISSING", out.getvalue())
        self.assertNotIn("PRIVATE KEY", out.getvalue())

    def test_a_revoked_certificate_passes_and_one_not_listed_fails(self):
        gone, live = self.leaf("gone", ["*.fcvm.dev"]), self.leaf("live", ["fcvm.dev"])
        self.crl(revoke=["gone"])
        passed, out = self.check({"v1": gone})
        self.assertTrue(passed, out)
        self.assertIn("revoked: serial", out)
        passed, out = self.check({"v1": gone, "v2": live})
        self.assertFalse(passed)
        self.assertIn("not revoked: serial", out)

    def no_status(self, versions, reason, now=None):
        passed, out = self.check(versions, now)
        self.assertFalse(passed, out)
        self.assertRegex(out, r"NO STATUS \(" + re.escape(reason))

    def test_a_crl_signed_by_another_key_under_the_same_name_is_no_status(self):
        cert = self.leaf("gone", ["*.fcvm.dev"])
        self.ca("rogue", "/CN=Fixture Intermediate")
        (self.directory / "rogue.idx").write_text((self.directory / "ca.idx").read_text())
        self.openssl("ca", "-config", "ca.cnf", "-revoke", "gone.crt")
        (self.directory / "rogue.idx").write_text((self.directory / "ca.idx").read_text())
        self.crl(ca="rogue")
        self.no_status({"v1": cert}, "the CRL is not signed by the certificate's issuer")

    def test_a_crl_from_another_issuer_is_no_status(self):
        cert = self.leaf("gone", ["*.fcvm.dev"])
        self.ca("other", "/CN=Another CA")
        self.crl(ca="other")
        self.no_status({"v1": cert}, "the CRL is not signed by the certificate's issuer")

    def test_a_stale_or_future_crl_is_no_status(self):
        cert = self.leaf("gone", ["*.fcvm.dev"])
        self.crl(revoke=["gone"])
        now = dt.datetime.now(dt.timezone.utc)
        self.no_status({"v1": cert}, "the CRL is stale", now=now + dt.timedelta(days=8))
        self.no_status({"v1": cert}, "the CRL's thisUpdate is in the future", now=now - dt.timedelta(hours=1))

    def test_a_crl_for_another_distribution_point_is_no_status(self):
        self.ca("ca", "/CN=Fixture Intermediate", idp=f"{self.base}/shard-2.crl")
        cert = self.leaf("gone", ["*.fcvm.dev"])
        self.crl(revoke=["gone"])
        self.no_status({"v1": cert}, "the CRL covers another distribution point")
        self.ca("ca", "/CN=Fixture Intermediate", idp=f"{self.base}/ca.crl")
        cert = self.leaf("gone", ["*.fcvm.dev"])
        self.crl(revoke=["gone"])
        self.assertTrue(self.check({"v1": cert})[0])

    def test_no_crl_no_issuer_a_wrong_intermediate_or_a_missing_crl_is_no_status(self):
        self.no_status({"v1": self.leaf("plain", ["*.fcvm.dev"], crl=None)}, "the certificate names no CRL")
        lone = self.leaf("lone", ["*.fcvm.dev"])
        self.no_status({"v1": dict(lone, cert=audit.chain(lone["cert"])[0])}, "the secret version stores no issuer")
        self.ca("other", "/CN=Another CA")
        wrong = self.leaf("wrong", ["*.fcvm.dev"])
        swapped = audit.chain(wrong["cert"])[0] + (self.directory / "other.crt").read_text()
        self.no_status({"v1": dict(wrong, cert=swapped)}, "the stored intermediate did not sign the certificate")
        self.no_status({"v1": self.leaf("missing", ["*.fcvm.dev"], crl="absent.crl")}, "the CRL did not download")

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
    unittest.main()
