#!/usr/bin/env python3
"""Revoke the legacy parent-zone certificates against a local ACME server that checks each request
as RFC 8555 section 7.6 requires: a JWS signed by the certificate's own key, carrying that key."""
import base64
import contextlib
import http.server
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

spec = importlib.util.spec_from_file_location("revoke", Path(__file__).with_name("fcvm-revoke-parent-wildcards.py"))
revoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(revoke)


def unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def der_signature(raw):
    half = len(raw) // 2

    def integer(b):
        b = b.lstrip(b"\0") or b"\0"
        if b[0] & 0x80:
            b = b"\0" + b
        return b"\x02" + bytes([len(b)]) + b

    body = integer(raw[:half]) + integer(raw[half:])
    return b"\x30" + bytes([len(body)]) + body


class Acme:
    """A local ACME server: directory, nonces, and revokeCert that verifies the JWS."""

    def __init__(self, directory):
        self.directory, self.nonces, self.revoked, self.posts = directory, set(), set(), 0
        acme = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body=b"", headers=()):
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                base = f"http://127.0.0.1:{self.server.server_port}"
                self.reply(200, json.dumps({"newNonce": base + "/nonce", "revokeCert": base + "/revoke"}).encode())

            def do_HEAD(self):
                nonce = f"n{len(acme.nonces)}"
                acme.nonces.add(nonce)
                self.reply(200, headers=[("Replay-Nonce", nonce)])

            def do_POST(self):
                acme.posts += 1
                status, problem = acme.check(json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                                             f"http://127.0.0.1:{self.server.server_port}/revoke")
                self.reply(status, json.dumps(problem).encode() if problem else b"")

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/directory"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def check(self, jws, url):
        header = json.loads(unb64(jws["protected"]))
        payload = json.loads(unb64(jws["payload"]))
        if header["url"] != url or header["nonce"] not in self.nonces or payload["reason"] != revoke.SUPERSEDED:
            return 400, {"type": "urn:ietf:params:acme:error:malformed"}
        self.nonces.discard(header["nonce"])
        der = unb64(payload["certificate"])
        cert_pem = subprocess.run(["openssl", "x509", "-inform", "DER"], input=der, capture_output=True,
                                  check=True).stdout
        public = self.directory / "verify-pub.pem"
        public.write_bytes(subprocess.run(["openssl", "x509", "-noout", "-pubkey"], input=cert_pem,
                                          capture_output=True, check=True).stdout)
        # The JWK must be the certificate's key, and the signature must verify against it.
        jwk = header["jwk"]
        spki = subprocess.run(["openssl", "pkey", "-pubin", "-in", str(public), "-outform", "DER"],
                              capture_output=True, check=True).stdout
        if jwk["kty"] == "EC":
            if not spki.endswith(b"\x04" + unb64(jwk["x"]) + unb64(jwk["y"])):
                return 403, {"type": "urn:ietf:params:acme:error:unauthorized"}
        elif unb64(jwk["n"]) not in spki:
            return 403, {"type": "urn:ietf:params:acme:error:unauthorized"}
        signature = unb64(jws["signature"])
        if header["alg"].startswith("ES"):
            signature = der_signature(signature)
        sig = self.directory / "sig.der"
        sig.write_bytes(signature)
        digest = {"ES256": "-sha256", "ES384": "-sha384", "RS256": "-sha256"}[header["alg"]]
        ok = subprocess.run(["openssl", "dgst", digest, "-verify", str(public), "-signature", str(sig)],
                            input=f"{jws['protected']}.{jws['payload']}".encode(), capture_output=True).returncode == 0
        if not ok:
            return 403, {"type": "urn:ietf:params:acme:error:unauthorized"}
        serial = subprocess.run(["openssl", "x509", "-noout", "-serial"], input=cert_pem, capture_output=True,
                                check=True).stdout
        if serial in self.revoked:
            return 400, {"type": "urn:ietf:params:acme:error:alreadyRevoked"}
        self.revoked.add(serial)
        return 200, None


class Secrets:
    """list_secret_version_ids and get_secret_value over fixed versions, paged two at a time."""

    def __init__(self, versions):
        self.versions = versions

    def list_secret_version_ids(self, SecretId, IncludeDeprecated, MaxResults, NextToken=None):
        assert SecretId == revoke.LEGACY and IncludeDeprecated
        start = int(NextToken or 0)
        page = {"Versions": [{"VersionId": v} for v in list(self.versions)[start:start + 2]]}
        if start + 2 < len(self.versions):
            page["NextToken"] = str(start + 2)
        return page

    def get_secret_value(self, SecretId, VersionId):
        return {"SecretString": json.dumps(self.versions[VersionId])}


class RevokeTests(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(contextlib.ExitStack())
        self.directory = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "30",
                     "-subj", "/CN=Fixture CA", "-keyout", "ca.key", "-out", "ca.crt")
        self.acme = Acme(self.directory)
        stack.callback(self.acme.httpd.server_close)
        stack.callback(self.acme.httpd.shutdown)

    def openssl(self, *args):
        subprocess.run(["openssl", *args], cwd=self.directory, check=True, capture_output=True)

    def issued(self, name, sans, key=("ec", "ec_paramgen_curve:P-256")):
        self.openssl("req", "-newkey", key[0], "-pkeyopt", key[1], "-nodes", "-subj", "/CN=Test",
                     "-addext", "subjectAltName=" + ",".join("DNS:" + s for s in sans),
                     "-keyout", name + ".key", "-out", name + ".csr")
        self.openssl("x509", "-req", "-in", name + ".csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-days", "20",
                     "-copy_extensions", "copy", "-out", name + ".crt")
        cert = (self.directory / (name + ".crt")).read_text()
        return {"cert": cert + (self.directory / "ca.crt").read_text(),
                "key": (self.directory / (name + ".key")).read_text(), "scene_token": "t"}

    def run_quiet(self, secrets, mode):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            found = revoke.run(secrets, self.directory, mode, self.acme.url)
        return found, out.getvalue()

    def test_revokes_each_parent_zone_certificate_once_with_its_own_key(self):
        parent = self.issued("parent", ["*.fcvm.dev", "*.tiles-stage.fcvm.dev", "fcvm.dev"])
        rsa = self.issued("rsa", ["*.fcvm.dev", "fcvm.dev"], key=("rsa", "rsa_keygen_bits:2048"))
        p384 = self.issued("p384", ["fcvm.dev"], key=("ec", "ec_paramgen_curve:P-384"))
        subzone = self.issued("subzone", ["*.tiles-stage.fcvm.dev"])
        secrets = Secrets({"v1": parent, "v2": parent, "v3": rsa, "v4": subzone, "v5": p384})
        found, out = self.run_quiet(secrets, "revoke")
        self.assertEqual(len(found), 3, out)
        self.assertEqual(len(self.acme.revoked), 3)
        self.assertEqual(self.acme.posts, 3, "one request per distinct certificate")
        self.assertEqual(out.count("\nrevoked "), 3, out)
        self.assertEqual(sorted(len(c["versions"]) for c in found), [1, 1, 2])
        self.assertNotIn("tiles-stage.fcvm.dev versions 1\n", out.replace("*.fcvm.dev ", ""))

    def test_a_second_run_finds_them_already_revoked(self):
        secrets = Secrets({"v1": self.issued("parent", ["*.fcvm.dev", "fcvm.dev"])})
        self.run_quiet(secrets, "revoke")
        _, out = self.run_quiet(secrets, "revoke")
        self.assertIn("already revoked serial", out)

    def test_listing_sends_nothing(self):
        _, out = self.run_quiet(Secrets({"v1": self.issued("parent", ["*.fcvm.dev"])}), "list")
        self.assertIn("would revoke serial", out)
        self.assertEqual(self.acme.posts, 0)

    def test_a_key_that_is_not_the_certificates_is_refused_before_any_request(self):
        parent = self.issued("parent", ["*.fcvm.dev"])
        other = self.issued("other", ["*.fcvm.dev"])
        with self.assertRaisesRegex(ValueError, "not the key in its secret version"):
            self.run_quiet(Secrets({"v1": dict(parent, key=other["key"])}), "revoke")
        self.assertEqual(self.acme.posts, 0)

    def test_a_refused_revocation_raises(self):
        parent = self.issued("parent", ["*.fcvm.dev"])
        self.acme.check = lambda jws, url: (403, {"type": "urn:ietf:params:acme:error:unauthorized", "detail": "no"})
        with self.assertRaisesRegex(RuntimeError, "unauthorized"):
            self.run_quiet(Secrets({"v1": parent}), "revoke")

    def test_check_reads_the_crl_the_certificate_names(self):
        # A CA whose certificates name a CRL served by a local server, and a CRL listing one serial.
        crl_path = self.directory / "ca.crl"
        server = http.server.HTTPServer(("127.0.0.1", 0), type("H", (http.server.BaseHTTPRequestHandler,), {
            "log_message": lambda *a: None,
            "do_GET": lambda h: (h.send_response(200), h.end_headers(), h.wfile.write(crl_path.read_bytes()))}))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        (self.directory / "ext.cnf").write_text(
            f"crlDistributionPoints=URI:http://127.0.0.1:{server.server_port}/ca.crl\n"
            "subjectAltName=DNS:*.fcvm.dev\n")
        (self.directory / "index.txt").write_text("")
        (self.directory / "ca.cnf").write_text(
            "[ca]\ndefault_ca=d\n[d]\ndatabase=index.txt\ncrlnumber=crlnumber\ndefault_md=sha256\n"
            "default_crl_days=1\nnew_certs_dir=.\ncertificate=ca.crt\nprivate_key=ca.key\n")
        (self.directory / "crlnumber").write_text("01\n")
        certs = {}
        for name in ("gone", "live"):
            self.openssl("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-subj", "/CN=T",
                         "-keyout", name + ".key", "-out", name + ".csr")
            self.openssl("x509", "-req", "-in", name + ".csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-days", "20",
                         "-extfile", "ext.cnf", "-out", name + ".crt")
            certs[name] = (self.directory / (name + ".crt")).read_text()
        self.openssl("ca", "-config", "ca.cnf", "-revoke", "gone.crt", "-crl_reason", "superseded")
        self.openssl("ca", "-config", "ca.cnf", "-gencrl", "-out", "ca.pem.crl")
        self.openssl("crl", "-in", "ca.pem.crl", "-outform", "DER", "-out", "ca.crl")
        self.assertIs(revoke.in_crl(certs["gone"], self.directory), True)
        self.assertIs(revoke.in_crl(certs["live"], self.directory), False)
        self.assertIsNone(revoke.in_crl(self.issued("plain", ["*.fcvm.dev"])["cert"], self.directory))

    def test_names_outside_the_subzones(self):
        for name, outside in [("*.fcvm.dev", True), ("fcvm.dev", True), ("fleet-api.fcvm.dev", True),
                              ("*.tiles-stage.fcvm.dev", False), ("*.tiles.fcvm.dev", False),
                              ("x.tiles.fcvm.dev", False), ("*.example.com", False), ("evilfcvm.dev", False)]:
            self.assertEqual(revoke.outside_subzones(name), outside, name)


if __name__ == "__main__":
    unittest.main()
