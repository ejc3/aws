#!/usr/bin/env python3
"""scripts/github-app-token.py: a JWT GitHub accepts (verified with openssl against a throwaway key), the installation
token request, retries only on transient failures, and nothing but the token on stdout. Offline."""
import base64
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("gat", ROOT / "scripts" / "github-app-token.py")
gat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gat)
TF = (ROOT / "github-admin-tokens.tf").read_text()


def unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Keys:
    def __init__(self):
        self.dir = tempfile.mkdtemp()
        self.key = os.path.join(self.dir, "k.pem")
        self.pub = os.path.join(self.dir, "k.pub")
        subprocess.run(["openssl", "genrsa", "-out", self.key, "2048"], check=True, capture_output=True)
        subprocess.run(["openssl", "rsa", "-in", self.key, "-pubout", "-out", self.pub], check=True, capture_output=True)
        self.pem = Path(self.key).read_text()


KEYS = Keys()


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, message):
    return urllib.error.HTTPError("https://api.github.com/x", code, "err", {}, io.BytesIO(json.dumps({"message": message}).encode()))


class JwtTests(unittest.TestCase):
    def test_the_jwt_is_rs256_signed_by_the_key_and_valid_nine_minutes(self):
        jwt = gat.make_jwt(12345, KEYS.pem, now=1_800_000_000)
        header, payload, sig = jwt.split(".")
        self.assertEqual(json.loads(unb64(header)), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(json.loads(unb64(payload)), {"iat": 1_800_000_000 - 60, "exp": 1_800_000_000 + 540, "iss": "12345"})
        with tempfile.NamedTemporaryFile(delete=False) as s:
            s.write(unb64(sig))
        r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", KEYS.pub, "-signature", s.name],
                           input=f"{header}.{payload}".encode(), capture_output=True)
        os.unlink(s.name)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_bad_key_fails_clearly(self):
        with self.assertRaisesRegex(RuntimeError, "could not sign"):
            gat.make_jwt(1, "not a key")

    def test_the_key_does_not_outlive_the_signature(self):
        before = set(os.listdir(tempfile.gettempdir()))
        gat.make_jwt(1, KEYS.pem)
        after = set(os.listdir(tempfile.gettempdir()))
        self.assertEqual(after - before, set(), "the temporary key directory must be gone")


class TokenTests(unittest.TestCase):
    creds = {"app_id": 7, "private_key": KEYS.pem, "installations": {"ejc3": 111, "dolphin-labs-hq": 222}}

    def test_it_asks_for_the_owners_installation_and_narrows_to_repos(self):
        seen = []

        def opener(req, timeout):
            seen.append((req.get_method(), req.full_url, req.data))
            return Resp(json.dumps({"token": "ghs_abc", "expires_at": "x"}).encode())
        tok = gat.installation_token(self.creds, "dolphin-labs-hq", ["dolphin-labs-hq/dolphin-maps"], opener=opener)
        self.assertEqual(tok, "ghs_abc")
        method, url, data = seen[0]
        self.assertEqual((method, url), ("POST", "https://api.github.com/app/installations/222/access_tokens"))
        self.assertEqual(json.loads(data), {"repositories": ["dolphin-maps"]})

    def test_an_owner_without_the_app_is_refused_before_any_call(self):
        def opener(req, timeout):
            raise AssertionError("no call expected")
        with self.assertRaisesRegex(RuntimeError, "not installed on CoderColton"):
            gat.installation_token(self.creds, "CoderColton", opener=opener)

    def test_transient_failures_are_retried_and_then_succeed(self):
        calls, slept = [], []

        def opener(req, timeout):
            calls.append(1)
            if len(calls) < 3:
                raise http_error(502, "bad gateway") if len(calls) == 1 else urllib.error.URLError("reset")
            return Resp(json.dumps({"token": "ghs_ok"}).encode())
        self.assertEqual(gat.installation_token(self.creds, "ejc3", opener=opener, sleep=slept.append), "ghs_ok")
        self.assertEqual((len(calls), slept), (3, [1, 2]))

    def test_a_refusal_is_not_retried_and_says_why(self):
        calls = []

        def opener(req, timeout):
            calls.append(1)
            raise http_error(401, "A JSON web token could not be decoded")
        with self.assertRaisesRegex(RuntimeError, "401 .*could not be decoded"):
            gat.installation_token(self.creds, "ejc3", opener=opener, sleep=lambda s: None)
        self.assertEqual(len(calls), 1)

    def test_it_gives_up_after_the_attempts(self):
        def opener(req, timeout):
            raise http_error(503, "unavailable")
        with self.assertRaisesRegex(RuntimeError, "kept failing"):
            gat.installation_token(self.creds, "ejc3", opener=opener, sleep=lambda s: None)

    def test_only_the_token_reaches_stdout(self):
        import contextlib
        from unittest import mock
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gat, "load_creds", lambda: self.creds), \
                mock.patch.object(gat, "installation_token", lambda creds, owner, repos=None: "ghs_only"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = gat.main(["ejc3"])
        self.assertEqual((rc, out.getvalue()), (0, "ghs_only"))
        with mock.patch.object(gat, "load_creds", lambda: self.creds), \
                mock.patch.object(gat, "installation_token", mock.Mock(side_effect=RuntimeError("boom"))), \
                contextlib.redirect_stdout(out := io.StringIO()), contextlib.redirect_stderr(err := io.StringIO()):
            rc = gat.main(["ejc3"])
        self.assertEqual((rc, out.getvalue()), (1, ""), "a failure prints nothing on stdout")
        self.assertIn("boom", err.getvalue())


class ContainerTests(unittest.TestCase):
    def test_only_the_jumpbox_role_reads_the_app_credentials(self):
        block = TF.split('resource "aws_secretsmanager_secret_policy" "github_app_admin"', 1)[1].split("\n}\n", 1)[0]
        self.assertIn('"aws:PrincipalArn" = local.github_admin_token_readers', block)
        self.assertIn("github_admin_token_readers = [aws_iam_role.jumpbox_admin[0].arn]", TF)
        self.assertNotIn("games_mp_admin_principals", block)
        self.assertEqual(gat.SECRET_ID, "github-app/aws-admin")


if __name__ == "__main__":
    unittest.main()
