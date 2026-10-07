#!/usr/bin/env python3
"""The staging Workers' secrets kept in AWS (workers-stage-secrets.tf and scripts/workers-stage-secrets.sh): one
administrators-only container per site, Terraform never holding a value, and a loader that is run here for real against
stub `aws` and `npx` so it passes the JSON through untouched, refuses a placeholder before calling Cloudflare, and prints
names, never values."""
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "workers-stage-secrets.tf").read_text()
SCRIPT = ROOT / "scripts" / "workers-stage-secrets.sh"
SITES = ["colton-games", "dolphin-films", "dolphin-labs", "imagine", "nest-step", "remote-claw"]
ALL_TF = "\n".join(p.read_text() for p in ROOT.glob("*.tf"))


def code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


class ContainerTests(unittest.TestCase):
    def test_there_is_one_container_per_staging_site_and_no_other(self):
        sites = re.search(r'workers_stage_secret_sites = toset\(\[(.*?)\]\)', TF, re.S).group(1)
        self.assertEqual(sorted(re.findall(r'"([a-z-]+)"', sites)), SITES)
        self.assertIn('name                    = "workers-stage/${each.key}"', TF)

    def test_every_site_has_a_staging_worker_somewhere_in_the_stack(self):
        for site in SITES:
            self.assertRegex(ALL_TF, r'"%s-stage"|name\s*=\s*"%s-stage"|%s-stage' % (site, site, site), site)

    def test_only_administration_may_read_a_container(self):
        c = code(TF)
        self.assertIn('Effect    = "Deny"', c)
        self.assertIn('Action    = "secretsmanager:GetSecretValue"', c)
        self.assertIn('"aws:PrincipalArn" = local.games_mp_admin_principals', c)
        self.assertNotRegex(c, r"dev_server|nextjs_dev")

    def test_terraform_never_holds_a_value(self):
        self.assertNotRegex(ALL_TF, r'(data|ephemeral)\s+"aws_secretsmanager_secret_version"\s+"[^"]*workers_stage')
        self.assertNotRegex(ALL_TF, r'resource\s+"aws_secretsmanager_secret_version"\s+"workers_stage')
        self.assertNotIn("secret_string", code(TF))


FAKE_AWS = r'''#!/usr/bin/env python3
import os, sys
a = " ".join(sys.argv[1:])
if "workers-stage/" in a:
    p = os.environ.get("FAKE_STAGE_JSON")
    if p is None:
        sys.stderr.write("An error occurred (ResourceNotFoundException)\n"); sys.exit(254)
    print(p)
elif "cloudflare-workers-deploy-token" in a:
    print("cfat_fake-deploy-token-value-for-the-test-0000000000")
else:
    sys.exit(0)
'''
FAKE_NPX = r'''#!/usr/bin/env python3
import os, sys
open(os.environ["NPX_LOG"], "a").write("ARGV " + " ".join(sys.argv[1:]) + "\n")
open(os.environ["NPX_STDIN"], "w").write(sys.stdin.read())
open(os.environ["NPX_ENV"], "w").write(os.environ.get("CLOUDFLARE_ACCOUNT_ID", "") + "|" + ("token-present" if os.environ.get("CLOUDFLARE_API_TOKEN") else "no-token"))
print("Successfully created secret for key: X")
'''


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name, body in (("aws", FAKE_AWS), ("npx", FAKE_NPX)):
            p = self.tmp / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IEXEC)
        self.log, self.stdin, self.env = self.tmp / "npx.log", self.tmp / "npx.stdin", self.tmp / "npx.env"

    def run_loader(self, *args, stage_json=None):
        env = dict(os.environ, PATH="%s:%s" % (self.tmp, os.environ["PATH"]), NPX_LOG=str(self.log),
                   NPX_STDIN=str(self.stdin), NPX_ENV=str(self.env))
        if stage_json is not None:
            env["FAKE_STAGE_JSON"] = stage_json
        else:
            env.pop("FAKE_STAGE_JSON", None)
        r = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60)
        return r, r.stdout + r.stderr

    def test_it_loads_exactly_the_container_into_the_matching_worker(self):
        payload = {"AUTH_SECRET": "value-one-0123456789", "IMAGINE_DB_URL": "libsql://example.invalid"}
        r, out = self.run_loader("imagine", stage_json=json.dumps(payload))
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(json.loads(self.stdin.read_text()), payload)
        self.assertIn("secret bulk --name imagine-stage", self.log.read_text())
        self.assertEqual(self.env.read_text(), "12ea67fb7ced068de03f35c22688e436|token-present")

    def test_it_prints_names_and_never_a_value_or_the_token(self):
        payload = {"AUTH_SECRET": "super-secret-value-ABC123", "TURSO_AUTH_TOKEN": "tok-DEF456-xyz"}
        r, out = self.run_loader("dolphin-films", stage_json=json.dumps(payload))
        self.assertIn("AUTH_SECRET", out)
        for secret in ("super-secret-value-ABC123", "tok-DEF456-xyz", "cfat_fake-deploy-token"):
            self.assertNotIn(secret, out)
            self.assertNotIn(secret, self.log.read_text())  # nothing secret on the wrangler command line either

    def test_a_placeholder_is_refused_before_cloudflare_is_called(self):
        r, out = self.run_loader("colton-games", stage_json=json.dumps({"AUTH_SECRET": "[SENSITIVE]"}))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("placeholder", out)
        self.assertFalse(self.log.exists(), "wrangler must not run")

    def test_empty_values_bad_names_and_non_objects_are_refused(self):
        for bad in ({"AUTH_SECRET": ""}, {"auth_secret": "x"}, {"A B": "x"}, {}, ["x"]):
            r, out = self.run_loader("nest-step", stage_json=json.dumps(bad))
            self.assertNotEqual(r.returncode, 0, bad)
        self.assertFalse(self.log.exists())

    def test_a_missing_container_value_says_so_and_does_nothing(self):
        r, out = self.run_loader("remote-claw")
        self.assertEqual(r.returncode, 1)
        self.assertIn("has no value yet", out)
        self.assertFalse(self.log.exists())

    def test_the_site_argument_is_validated(self):
        for bad in ("../x", "Imagine", "a b", "x;rm", ""):
            r, out = self.run_loader(bad, stage_json=json.dumps({"A": "b"}))
            self.assertNotEqual(r.returncode, 0, bad)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
