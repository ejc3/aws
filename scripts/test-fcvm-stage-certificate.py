#!/usr/bin/env python3
"""Exercise the first staging wildcard issue and read-only proof with isolated keys and a local CA."""
import contextlib
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("stagecert", Path(__file__).with_name("fcvm-stage-certificate.py"))
cert = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cert)


class CertificateTests(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(contextlib.ExitStack())
        self.directory = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.run = subprocess.run
        self.command("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                     "-days", "120", "-subj", "/CN=Fixture", "-keyout", "ca.key", "-out", "ca.crt")
        stack.enter_context(mock.patch.dict(os.environ, {"SSL_CERT_FILE": str(self.directory / "ca.crt")}))
        self.leaf = self.signed_leaf("stage", "*." + cert.ZONE)
        self.legacy = {"cert": (self.directory / "ca.crt").read_text(), "scene_token": "original-scene-token"}

    def command(self, *args):
        self.run(["openssl", *args], cwd=self.directory, check=True, capture_output=True)

    def signed_leaf(self, name, names):
        self.command("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-subj", "/CN=Test",
                     "-addext", "subjectAltName=DNS:" + names, "-keyout", name + ".key", "-out", name + ".csr")
        self.command("x509", "-req", "-in", name + ".csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-days", "90",
                     "-copy_extensions", "copy", "-out", name + ".crt")
        pem = (self.directory / (name + ".crt")).read_text() + (self.directory / "ca.crt").read_text()
        return {"cert": pem, "key": (self.directory / (name + ".key")).read_text(), "domains": ["*." + cert.ZONE],
                "not_after": cert.expiry(pem).isoformat(), "scene_token": "original-scene-token"}

    def test_rejects_extra_san_mismatched_key_old_key_changed_token_and_invalid_dates(self):
        broad = self.signed_leaf("broad", "*." + cert.ZONE + ",DNS:*.fcvm.dev")
        for error, doc, old in [
            ("SANs", broad, self.legacy),
            ("key mismatch", dict(self.leaf, key=broad["key"]), self.legacy),
            ("key reused", self.leaf, dict(self.legacy, cert=self.leaf["cert"])),
            ("token differs", dict(self.leaf, scene_token="different"), self.legacy),
            ("expiry", dict(self.leaf, not_after=dt.datetime.now(dt.timezone.utc).isoformat()), self.legacy),
        ]:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                cert.verify(doc, old, self.directory)

    def test_empty_secret_requires_issue_and_first_issue_is_verified_then_read_only(self):
        secrets = mock.Mock()
        metadata = {"ARN": "arn:test:secret:stage", "VersionIdsToStages": {}}
        secrets.describe_secret.side_effect = lambda **_: metadata
        values = {cert.LEGACY: json.dumps(dict(self.legacy, key="legacy private key never sent")),
                  "fcvm-fleet/cloudflare-dns-token": "test-token"}
        secrets.get_secret_value.side_effect = lambda SecretId, **_: {"SecretString": values[SecretId]}

        def put(SecretId, SecretString, VersionStages, ClientRequestToken):
            values[SecretId] = SecretString
            metadata["VersionIdsToStages"][ClientRequestToken] = VersionStages

        def run(args, **kw):
            if args[0] != "lego":
                return self.run(args, **kw)
            self.assertEqual(args[args.index("--domains"):], ["--domains", "*." + cert.ZONE, "run"])
            output = self.directory / "certificates"
            output.mkdir()
            for suffix, field in [("key", "key"), ("crt", "cert")]:
                (output / ("_." + cert.ZONE + "." + suffix)).write_text(self.leaf[field])

        secrets.put_secret_value.side_effect = put
        with self.assertRaisesRegex(ValueError, "empty secret"):
            cert.bootstrap(secrets, self.directory, False)
        with mock.patch.object(cert.subprocess, "check_output", return_value="_acme-challenge.fcvm.dev."), \
                mock.patch.object(cert.subprocess, "run", side_effect=run):
            cert.bootstrap(secrets, self.directory, True)
            cert.bootstrap(secrets, self.directory, True)
        self.assertEqual(secrets.put_secret_value.call_count, 1)
        self.assertEqual(list(metadata["VersionIdsToStages"].values()), [["AWSCURRENT", cert.LABEL]])
        self.assertEqual(json.loads(values[cert.STAGE])["key"], self.leaf["key"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
