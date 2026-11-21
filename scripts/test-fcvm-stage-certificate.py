#!/usr/bin/env python3
"""Exercise the staging wildcard recovery and read-only proof with isolated keys and a local CA."""
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

    def signed_leaf(self, name, names, days=90):
        self.command("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-subj", "/CN=Test",
                     "-addext", "subjectAltName=DNS:" + names, "-keyout", name + ".key", "-out", name + ".csr")
        self.command("x509", "-req", "-in", name + ".csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-days", str(days),
                     "-copy_extensions", "copy", "-out", name + ".crt")
        pem = (self.directory / (name + ".crt")).read_text() + (self.directory / "ca.crt").read_text()
        return {"cert": pem, "key": (self.directory / (name + ".key")).read_text(), "domains": ["*." + cert.ZONE],
                "not_after": cert.cert_date(pem).isoformat(), "scene_token": "original-scene-token"}

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

    def test_explicit_recovery_accepts_near_expiry_and_rejects_expired_leaves(self):
        soon = self.signed_leaf("soon", "*." + cert.ZONE, days=20)
        backup = self.directory / "tls.json"
        backup.write_text(json.dumps(soon))
        metadata = {"ARN": "arn:test:secret:stage", "VersionIdsToStages": {}}
        values = {cert.LEGACY: self.legacy}
        secrets = mock.Mock()
        secrets.describe_secret.return_value = metadata
        secrets.get_secret_value.side_effect = lambda SecretId, **_: {"SecretString": json.dumps(values[SecretId])}
        def put(SecretId, SecretString, VersionStages, ClientRequestToken):
            values[SecretId] = json.loads(SecretString)
            metadata["VersionIdsToStages"][ClientRequestToken] = VersionStages
        secrets.put_secret_value.side_effect = put

        with self.assertRaisesRegex(ValueError, "renewal window"):
            cert.verify(soon, self.legacy, self.directory)
        cert.bootstrap(secrets, self.directory, backup)
        self.assertEqual(secrets.put_secret_value.call_count, 1)
        with self.assertRaisesRegex(ValueError, "renewal window"):
            cert.bootstrap(secrets, self.directory)

        after_expiry = dt.datetime.fromisoformat(soon["not_after"]) + dt.timedelta(seconds=1)
        class AfterExpiry(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return after_expiry.astimezone(tz)
        metadata["VersionIdsToStages"].clear()
        with mock.patch.object(cert.dt, "datetime", AfterExpiry), self.assertRaisesRegex(ValueError, "expir"):
            cert.bootstrap(secrets, self.directory, backup)
        self.assertEqual(secrets.put_secret_value.call_count, 1)

    def test_restore_requires_good_backup_then_verifies_without_writing_again(self):
        secrets = mock.Mock()
        metadata = {"ARN": "arn:test:secret:stage", "VersionIdsToStages": {}}
        secrets.describe_secret.side_effect = lambda **_: metadata
        values = {cert.LEGACY: json.dumps(dict(self.legacy, key="legacy private key never sent"))}
        secrets.get_secret_value.side_effect = lambda SecretId, **_: {"SecretString": values[SecretId]}

        def put(SecretId, SecretString, VersionStages, ClientRequestToken):
            values[SecretId] = SecretString
            metadata["VersionIdsToStages"][ClientRequestToken] = VersionStages

        secrets.put_secret_value.side_effect = put
        with self.assertRaisesRegex(ValueError, "empty secret"):
            cert.bootstrap(secrets, self.directory)
        backup = self.directory / "stage-backup.json"
        backup.write_text(json.dumps(dict(self.leaf, scene_token="wrong")))
        with self.assertRaisesRegex(ValueError, "token differs"):
            cert.bootstrap(secrets, self.directory, backup)
        secrets.put_secret_value.assert_not_called()
        backup.write_text(json.dumps({k: self.leaf[k] for k in ("cert", "key", "scene_token")}))
        cert.bootstrap(secrets, self.directory, backup)
        backup.unlink()
        cert.bootstrap(secrets, self.directory)
        self.assertEqual(secrets.put_secret_value.call_count, 1)
        self.assertEqual(list(metadata["VersionIdsToStages"].values()), [["AWSCURRENT", cert.LABEL]])
        restored = json.loads(values[cert.STAGE])
        self.assertEqual({k: restored[k] for k in self.leaf}, self.leaf)
        self.assertIn("issuer", restored)
        self.assertEqual(restored["issued_at"], cert.cert_date(self.leaf["cert"], "startdate").isoformat())


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)
