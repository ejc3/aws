#!/usr/bin/env python3
"""The staging Workers' secrets kept in AWS (workers-stage-secrets.tf and scripts/workers-stage-secrets.sh): one
administrators-only container per site, Terraform never holding a value of one (dolphin-maps' second container,
workers-stage/dolphin-maps/terraform, is Terraform's own: dolphin-maps-moderation.tf), and a loader that is run here for real
against stub `aws` and `npx` so it passes the JSON through untouched (merging the two containers where a site has both),
refuses a placeholder or a name in both before calling Cloudflare, and prints names, never values."""
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
SITES = ["colton-games", "imagine", "nest-step", "remote-claw"]
# Kept in the Dolphin account (dolphin-labs-hq/dolphin-aws) and read there through OrganizationAccountAccessRole.
DOLPHIN_SITES = ["dolphin-films", "dolphin-labs", "dolphin-maps"]
DOLPHIN_ROLE = "arn:aws:iam::484888470752:role/OrganizationAccountAccessRole"
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
log = os.environ.get("AWS_LOG")
if log:
    open(log, "a").write("ARGV %s KEY=%s PROFILE=%s DEFAULT=%s\n" % (a, os.environ.get("AWS_ACCESS_KEY_ID", "-"), os.environ.get("AWS_PROFILE", "-"), os.environ.get("AWS_DEFAULT_PROFILE", "-")))
if a.startswith("sts assume-role"):
    if os.environ.get("FAKE_ASSUME_FAILS"):
        sys.stderr.write("An error occurred (AccessDenied)\n"); sys.exit(254)
    if os.environ.get("FAKE_ASSUME_MALFORMED"):
        print("None\tNone\tNone"); sys.exit(0)
    print("ASIAFAKEDOLPHIN\tfake-secret-key-0123\tfake-session-token-4567")
elif "workers-stage/" in a:
    p = os.environ.get("FAKE_MANAGED_JSON" if "/terraform " in a + " " else "FAKE_STAGE_JSON")
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
        self.aws_log = self.tmp / "aws.log"

    MANAGED = {"MODERATION_AWS_ACCESS_KEY_ID": "AKIAFAKEMODERATION01", "MODERATION_AWS_SECRET_ACCESS_KEY": "moderation-secret-0123",
               "MODERATION_AWS_REGION": "us-west-1", "MODERATION_TEXT_REGION": "us-west-2"}

    def run_loader(self, *args, stage_json=None, managed_json=json.dumps(MANAGED), **extra_env):
        env = dict(os.environ, PATH="%s:%s" % (self.tmp, os.environ["PATH"]), NPX_LOG=str(self.log),
                   NPX_STDIN=str(self.stdin), NPX_ENV=str(self.env), AWS_LOG=str(self.aws_log), AWS_PROFILE="example-profile")
        for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "FAKE_ASSUME_FAILS", "FAKE_ASSUME_MALFORMED", "AWS_DEFAULT_PROFILE"):
            env.pop(k, None)
        env.update(extra_env)
        for name, value in (("FAKE_STAGE_JSON", stage_json), ("FAKE_MANAGED_JSON", managed_json)):
            if value is not None:
                env[name] = value
            else:
                env.pop(name, None)
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
        self.assertIn("ResourceNotFoundException", out)  # AWS's own reason is shown
        self.assertFalse(self.log.exists())

    def test_the_site_argument_is_validated(self):
        for bad in ("../x", "Imagine", "a b", "x;rm", ""):
            r, out = self.run_loader(bad, stage_json=json.dumps({"A": "b"}))
            self.assertNotEqual(r.returncode, 0, bad)
        self.assertFalse(self.log.exists())


    def aws_calls(self):
        return self.aws_log.read_text().splitlines() if self.aws_log.exists() else []

    def test_dolphin_sites_are_read_in_the_dolphin_account_with_an_assumed_session(self):
        payload = {"AUTH_SECRET": "dolphin-value-0123456789"}
        for site in DOLPHIN_SITES:
            self.aws_log.unlink(missing_ok=True)
            r, out = self.run_loader(site, stage_json=json.dumps(payload))
            self.assertEqual(r.returncode, 0, out)
            calls = self.aws_calls()
            self.assertIn("sts assume-role --role-arn %s" % DOLPHIN_ROLE, calls[0])
            read = [c for c in calls if "workers-stage/%s" % site in c]
            self.assertEqual(len(read), 2 if site == "dolphin-maps" else 1, calls)
            for each in read:
                self.assertIn("KEY=ASIAFAKEDOLPHIN PROFILE=- DEFAULT=-", each)  # the assumed session, no profile
            token = [c for c in calls if "cloudflare-workers-deploy-token" in c]
            self.assertIn("KEY=- PROFILE=example-profile", token[0])  # the deploy token stays in this account
            self.assertIn("secret bulk --name %s-stage" % site, self.log.read_text())
            for secret in ("fake-secret-key-0123", "fake-session-token-4567", "dolphin-value-0123456789"):
                self.assertNotIn(secret, out)
                self.assertNotIn(secret, "\n".join(calls))  # never on a command line

    def test_dolphin_maps_merges_terraforms_container_and_no_other_site_reads_one(self):
        payload = {"AUTH_SECRET": "dolphin-value-0123456789"}
        r, out = self.run_loader("dolphin-maps", stage_json=json.dumps(payload))
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(json.loads(self.stdin.read_text()), {**payload, **self.MANAGED})
        self.assertIn("--secret-id workers-stage/dolphin-maps/terraform ", "\n".join(self.aws_calls()) + " ")
        for secret in ("moderation-secret-0123", "AKIAFAKEMODERATION01"):
            self.assertNotIn(secret, out)
        for site in ("dolphin-labs", "imagine"):
            self.aws_log.unlink(missing_ok=True)
            r, out = self.run_loader(site, stage_json=json.dumps(payload))
            self.assertEqual(r.returncode, 0, out)
            self.assertEqual(json.loads(self.stdin.read_text()), payload)
            self.assertFalse([c for c in self.aws_calls() if "/terraform" in c], site)

    def test_dolphin_maps_refuses_a_name_in_both_and_a_missing_terraform_container(self):
        both = {"AUTH_SECRET": "x-0123456789", "MODERATION_AWS_REGION": "us-west-1"}
        r, out = self.run_loader("dolphin-maps", stage_json=json.dumps(both))
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("in both containers", out)
        r, out = self.run_loader("dolphin-maps", stage_json=json.dumps({"AUTH_SECRET": "x-0123456789"}), managed_json=None)
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("workers-stage/dolphin-maps/terraform", out)
        self.assertIn("ResourceNotFoundException", out)
        self.assertFalse(self.log.exists(), "wrangler must not run")

    def test_other_sites_never_assume_a_role(self):
        r, out = self.run_loader("imagine", stage_json=json.dumps({"A": "b"}))
        self.assertEqual(r.returncode, 0, out)
        self.assertFalse([c for c in self.aws_calls() if "assume-role" in c])
        self.assertIn("KEY=- PROFILE=example-profile", [c for c in self.aws_calls() if "workers-stage/imagine" in c][0])

    def test_a_failed_assume_role_stops_before_cloudflare(self):
        r, out = self.run_loader("dolphin-labs", stage_json=json.dumps({"A": "b"}), FAKE_ASSUME_FAILS="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("account 484888470752", out)
        self.assertFalse([c for c in self.aws_calls() if "workers-stage/" in c])
        self.assertFalse(self.log.exists())

    def test_a_malformed_session_fails_closed_without_reading_this_account(self):
        r, out = self.run_loader("dolphin-films", stage_json=json.dumps({"A": "b"}), FAKE_ASSUME_MALFORMED="1")
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("no session in account 484888470752", out)
        self.assertFalse([c for c in self.aws_calls() if "workers-stage/" in c])  # not even this account's old copy
        self.assertFalse(self.log.exists())

    def test_a_default_profile_cannot_override_the_assumed_session(self):
        r, out = self.run_loader("dolphin-labs", stage_json=json.dumps({"A": "b"}), AWS_DEFAULT_PROFILE="example-profile")
        self.assertEqual(r.returncode, 0, out)
        read = [c for c in self.aws_calls() if "workers-stage/dolphin-labs" in c]
        self.assertIn("KEY=ASIAFAKEDOLPHIN PROFILE=- DEFAULT=-", read[0])

    def test_a_denied_read_shows_aws_reason(self):
        r, out = self.run_loader("dolphin-labs", stage_json=json.dumps({"A": "b"}), FAKE_ASSUME_FAILS="1")
        self.assertIn("AccessDenied", out)

    def test_this_account_sites_print_names_and_never_a_value_or_the_token(self):
        payload = {"AUTH_SECRET": "super-secret-value-ABC123"}
        r, out = self.run_loader("imagine", stage_json=json.dumps(payload))
        self.assertEqual(r.returncode, 0, out)
        for secret in ("super-secret-value-ABC123", "cfat_fake-deploy-token"):
            self.assertNotIn(secret, out)
            self.assertNotIn(secret, self.log.read_text())


if __name__ == "__main__":
    unittest.main(buffer=True)
