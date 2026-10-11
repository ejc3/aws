#!/usr/bin/env python3
"""Colton Games production on Cloudflare (cloudflare-production.tf, workers-prod-secrets.tf, scripts/workers-prod-secrets.sh):
the cutover is one switch that starts off and moves only the apex; the production Worker's workers.dev address is behind a
hostname Access application (never a Worker-native one, which would also gate production); the cc-games.org protections
fit the Free plan and leave Bot Fight Mode off; the Worker's AWS keys can do one thing each; its secrets never mix with
staging's; and the loader is run here for real against stub `aws` and `npx`."""
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROD = (ROOT / "cloudflare-production.tf").read_text()
SECRETS = (ROOT / "workers-prod-secrets.tf").read_text()
VERCEL = (ROOT / "vercel-cc-games.tf").read_text()
MODERATION = (ROOT / "colton-games-moderation.tf").read_text()
SCRIPT = ROOT / "scripts" / "workers-prod-secrets.sh"
ALL_TF = "\n".join(p.read_text() for p in ROOT.glob("*.tf"))


def code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def block(text, kind, *labels):
    head = "%s %s {" % (kind, " ".join('"%s"' % l for l in labels))
    body = code(text)
    body = body[body.index(head):]
    depth, seen = 0, False
    for i, ch in enumerate(body):
        depth += ch == "{"
        depth -= ch == "}"
        seen = seen or depth > 0
        if seen and depth == 0:
            return body[:i + 1]


class CutoverTests(unittest.TestCase):
    def test_the_switch_exists_once_and_is_off(self):
        self.assertEqual(len(re.findall(r"^\s*colton_games_production_on_cloudflare\s*=", code(ALL_TF), re.M)), 1)
        self.assertRegex(code(PROD), r"colton_games_production_on_cloudflare\s*=\s*false\b")

    def test_the_apex_record_follows_the_switch_and_keeps_vercels_address(self):
        rec = block(VERCEL, "resource", "cloudflare_dns_record", "cc_games_org_apex")
        self.assertIn("proxied = local.colton_games_production_on_cloudflare", rec)
        self.assertIn('content = "76.76.21.21"', rec)
        self.assertIn('name    = "cc-games.org"', rec)

    def test_no_other_production_record_is_tied_to_it(self):
        for name in ("cc_games_org_www", "cc_games_net_apex", "cc_games_app_apex", "ccgames_app_apex",
                     "cc_games_net_www", "cc_games_app_www", "ccgames_app_www"):
            rec = block(VERCEL, "resource", "cloudflare_dns_record", name)
            self.assertIn("proxied = false", rec, name)
            self.assertNotIn("colton_games_production_on_cloudflare", rec, name)

    def test_the_route_exists_only_with_the_switch_and_after_the_protections(self):
        route = block(PROD, "resource", "cloudflare_workers_route", "colton_games_prod")
        self.assertIn("count   = local.colton_games_production_on_cloudflare ? 1 : 0", route)
        self.assertIn('pattern = "cc-games.org/*"', route)
        self.assertIn("script  = local.colton_games_prod_worker_name", route)
        for dep in ("cloudflare_ruleset.cc_games_org_waf", "cloudflare_ruleset.cc_games_org_rate_limit",
                    "cloudflare_zone_setting.cc_games_org"):
            self.assertIn(dep, route)

    def test_no_custom_domain_and_nothing_shells_out(self):
        # A Custom Domain cannot coexist with the record, which would make the switch two steps with an outage between.
        self.assertNotIn("cloudflare_workers_custom_domain", code(PROD))
        self.assertNotRegex(code(PROD + SECRETS), r"local-exec|provisioner|curl|api\.cloudflare\.com")
        self.assertIn('colton_games_prod_worker_name = "colton-games-prod"', PROD)


class AccessTests(unittest.TestCase):
    ACCESS = block(PROD, "resource", "cloudflare_zero_trust_access_application", "colton_games_prod_workers_dev")

    def test_it_is_a_hostname_application_on_the_workers_dev_address_only(self):
        self.assertIn('colton_games_prod_workers_dev = "${local.colton_games_prod_worker_name}.cc-games.workers.dev"', PROD)
        self.assertIn("domain           = local.colton_games_prod_workers_dev", self.ACCESS)
        self.assertIn('{ type = "public", uri = local.colton_games_prod_workers_dev }', self.ACCESS)
        # A worker destination would follow the Worker onto cc-games.org and put Access in front of production.
        self.assertNotRegex(self.ACCESS, r'type\s*=\s*"(worker|preview_worker)"|worker_id')

    def test_the_url_switches_are_adopted_after_access_with_previews_off(self):
        sub = block(PROD, "resource", "cloudflare_workers_script_subdomain", "colton_games_prod")
        self.assertIn("script_name      = local.colton_games_prod_worker_name", sub)
        self.assertIn("enabled          = true", sub)
        self.assertIn("previews_enabled = false", sub)
        self.assertIn("cloudflare_zero_trust_access_application.colton_games_prod_workers_dev", sub)
        self.assertIn('id = "${var.cloudflare_account_id}/${local.colton_games_prod_worker_name}"', PROD)
        self.assertNotRegex(code(PROD), r'resource\s+"cloudflare_worker"\s')

    def test_the_family_allowlist_and_service_token_and_no_destroy(self):
        self.assertIn("cloudflare_zero_trust_access_policy.cc_games_allowed.id", self.ACCESS)
        self.assertIn("cloudflare_zero_trust_access_policy.cc_games_service.id", self.ACCESS)
        self.assertIn("prevent_destroy = true", self.ACCESS)


class ZoneProtectionTests(unittest.TestCase):
    def test_every_protection_is_on_cc_games_org(self):
        for kind, name in (("cloudflare_zone_setting", "cc_games_org"), ("cloudflare_zone_setting", "cc_games_org_hsts"),
                           ("cloudflare_bot_management", "cc_games_org"), ("cloudflare_ruleset", "cc_games_org_waf"),
                           ("cloudflare_ruleset", "cc_games_org_rate_limit")):
            self.assertRegex(block(PROD, "resource", kind, name), r"zone_id\s+= var\.cc_games_org_zone_id", name)

    def test_https_tls_and_strict_origin(self):
        self.assertRegex(PROD, r'always_use_https\s*=\s*"on"')
        self.assertRegex(PROD, r'min_tls_version\s*=\s*"1\.2"')
        self.assertRegex(PROD, r'ssl\s*=\s*"strict"')
        hsts = block(PROD, "resource", "cloudflare_zone_setting", "cc_games_org_hsts")
        self.assertIn("max_age            = 63072000", hsts)
        self.assertIn("include_subdomains = false", hsts)
        self.assertIn("preload            = false", hsts)

    def test_ai_bots_blocked_and_bot_fight_mode_off(self):
        bots = block(PROD, "resource", "cloudflare_bot_management", "cc_games_org")
        self.assertIn('ai_bots_protection = "block"', bots)
        self.assertIn("fight_mode         = false", bots)
        self.assertNotRegex(code(ALL_TF), r"fight_mode\s*=\s*true")

    def test_custom_rules_fit_the_free_plan_and_engines_skip_browser_checks_first(self):
        waf = block(PROD, "resource", "cloudflare_ruleset", "cc_games_org_waf")
        self.assertIn('phase       = "http_request_firewall_custom"', waf)
        actions = re.findall(r'action\s+= "([a-z_]+)"', waf)
        self.assertLessEqual(len(actions), 5)
        self.assertEqual(actions[0], "skip")
        self.assertNotIn("challenge", actions)
        self.assertIn('starts_with(http.request.uri.path, \\"/api/mp/engine/\\")', PROD)

    def test_one_rate_limit_rule_on_path_only_lobby_excluded(self):
        rl = block(PROD, "resource", "cloudflare_ruleset", "cc_games_org_rate_limit")
        self.assertIn('phase       = "http_ratelimit"', rl)
        self.assertEqual(rl.count("ratelimit = {"), 1)
        self.assertIn("period              = 10", rl)
        self.assertIn("mitigation_timeout  = 10", rl)
        expr = re.search(r'cc_games_org_rate_limited = "(.*)"', PROD).group(1)
        # Free plan: the expression may use the path only.
        self.assertEqual(set(re.findall(r"\b(?:http|cf|ip)\.[a-z_.]+", expr)), {"http.request.uri.path"})
        self.assertIn('not starts_with(http.request.uri.path, \\"/api/mp/\\")', expr)


class IdentityTests(unittest.TestCase):
    def test_the_launcher_user_may_only_invoke_the_production_launch_function(self):
        pol = block(PROD, "resource", "aws_iam_user_policy", "games_mp_launcher_worker")
        self.assertEqual(re.findall(r'Action\s+= "([^"]+)"', pol), ["lambda:InvokeFunction"])
        self.assertIn('Resource = aws_lambda_function.games_mp_launch["production"].arn', pol)
        self.assertNotIn("preview", pol)

    def test_the_production_worker_has_its_own_moderation_user(self):
        self.assertIn('name = "colton-games-moderation-worker-prod"', MODERATION)
        pol = block(MODERATION, "resource", "aws_iam_user_policy", "colton_games_moderation_worker_prod")
        self.assertIn("policy = data.aws_iam_policy_document.moderation.json", pol)
        # Its key goes to the production container only, never to the staging key's secret.
        stage = block(MODERATION, "resource", "aws_secretsmanager_secret_version", "colton_games_moderation_worker_key")
        self.assertNotIn("worker_prod", stage)


class SecretsTests(unittest.TestCase):
    def test_both_containers_are_administration_only(self):
        pol = block(SECRETS, "resource", "aws_secretsmanager_secret_policy", "workers_prod")
        self.assertIn('Effect    = "Deny"', pol)
        self.assertIn('"aws:PrincipalArn" = local.games_mp_admin_principals', pol)
        self.assertNotRegex(code(SECRETS), r"dev_server|nextjs_dev")
        self.assertIn('"colton-games"           =', SECRETS)
        self.assertIn('"colton-games/terraform" =', SECRETS)

    def test_terraform_writes_only_the_managed_container(self):
        versions = re.findall(r'resource "aws_secretsmanager_secret_version" "([a-z_]+)" \{\s*secret_id\s*= ([^\n]+)', code(SECRETS))
        self.assertEqual(versions, [("workers_prod_colton_games", 'aws_secretsmanager_secret.workers_prod["colton-games/terraform"].id')])
        self.assertNotRegex(code(ALL_TF), r'(data|ephemeral)\s+"aws_secretsmanager_secret_version"\s+"[^"]*workers_prod')

    def test_the_managed_set_is_vercel_productions_with_the_worker_differences(self):
        env = re.search(r"colton_games_prod_worker_env = merge\((.*?)\n  \)\n", SECRETS, re.S).group(1)
        self.assertIn('if entry.target == "production"', env)
        self.assertIn('if contains(entry.targets, "production")', env)
        self.assertIn('contains(var.colton_games_account_saves_targets, "production")', env)
        for name in ("AUTH_URL", "SKYHOOK_LEADERBOARD_ENVIRONMENT", "MP_LAUNCH_AWS_ACCESS_KEY_ID", "MP_LAUNCH_AWS_SECRET_ACCESS_KEY",
                     "MODERATION_AWS_ACCESS_KEY_ID", "MODERATION_AWS_SECRET_ACCESS_KEY", "MODERATION_AWS_REGION"):
            self.assertRegex(env, r"\b%s\s+=" % name)
        self.assertIn('AUTH_URL = "https://cc-games.org"', env)
        self.assertRegex(env, r'SKYHOOK_LEADERBOARD_ENVIRONMENT\s+= "production"')
        self.assertIn('colton_games_prod_worker_vercel_only = ["MP_LAUNCH_ROLE_ARN", "AWS_REGION"]', SECRETS)
        # Never a preview or staging value.
        self.assertNotRegex(env, r'"preview"|nonprod|workers_stage|colton_games_moderation_worker\.')

    def test_the_two_containers_never_share_a_name(self):
        self.assertIn('colton_games_prod_worker_out_of_band = ["SUPABASE_URL", "SUPABASE_SECRET_KEY", "SKYHOOK_LEADERBOARD_SECRET"]', SECRETS)
        self.assertIn("setintersection(keys(local.colton_games_prod_worker_env), local.colton_games_prod_worker_out_of_band)", SECRETS)


FAKE_AWS = r'''#!/usr/bin/env python3
import os, sys
a = " ".join(sys.argv[1:])
def answer(var):
    p = os.environ.get(var)
    if p is None:
        sys.stderr.write("An error occurred (ResourceNotFoundException)\n"); sys.exit(254)
    print(p)
if "workers-prod/" in a and a.split("--secret-id ")[1].split()[0].endswith("/terraform"):
    answer("FAKE_MANAGED")
elif "workers-prod/" in a:
    answer("FAKE_OOB")
elif "cloudflare-workers-deploy-token" in a:
    print("cfat_fake-deploy-token-value-for-the-test-0000000000")
else:
    sys.exit(3)
'''
FAKE_NPX = r'''#!/usr/bin/env python3
import os, sys
open(os.environ["NPX_LOG"], "a").write("ARGV " + " ".join(sys.argv[1:]) + "\n")
open(os.environ["NPX_STDIN"], "w").write(sys.stdin.read())
print("Successfully created secret for key: X")
'''


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name, body in (("aws", FAKE_AWS), ("npx", FAKE_NPX)):
            p = self.tmp / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IEXEC)
        self.log, self.stdin = self.tmp / "npx.log", self.tmp / "npx.stdin"

    def run_loader(self, *args, oob=None, managed=None):
        env = dict(os.environ, PATH="%s:%s" % (self.tmp, os.environ["PATH"]), NPX_LOG=str(self.log), NPX_STDIN=str(self.stdin))
        for var, value in (("FAKE_OOB", oob), ("FAKE_MANAGED", managed)):
            if value is None:
                env.pop(var, None)
            else:
                env[var] = json.dumps(value)
        r = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60)
        return r, r.stdout + r.stderr

    OOB = {"SUPABASE_URL": "https://project.example.invalid", "SUPABASE_SECRET_KEY": "sb_secret_oob-VALUE-111",
           "SKYHOOK_LEADERBOARD_SECRET": "skyhook-oob-VALUE-333-abcdefghijklmnopqrstuvwxyz"}
    MANAGED = {"AUTH_SECRET": "managed-VALUE-222-abcdefghijklmnop", "MP_ENV": "production"}

    def test_it_loads_the_merge_of_both_into_the_prod_worker(self):
        r, out = self.run_loader("colton-games", oob=self.OOB, managed=self.MANAGED)
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(json.loads(self.stdin.read_text()), {**self.OOB, **self.MANAGED})
        self.assertIn("secret bulk --name colton-games-prod", self.log.read_text())
        self.assertNotIn("-stage", self.log.read_text())

    def test_it_prints_names_never_values_or_the_token(self):
        r, out = self.run_loader("colton-games", oob=self.OOB, managed=self.MANAGED)
        self.assertIn("SUPABASE_SECRET_KEY", out)
        for secret in ("sb_secret_oob-VALUE-111", "skyhook-oob-VALUE-333", "managed-VALUE-222", "cfat_fake-deploy-token"):
            self.assertNotIn(secret, out)
            self.assertNotIn(secret, self.log.read_text())

    def test_a_name_in_both_containers_is_refused_before_cloudflare(self):
        r, out = self.run_loader("colton-games", oob=self.OOB, managed={**self.MANAGED, "SUPABASE_URL": "https://other.invalid"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("SUPABASE_URL is in both containers", out)
        self.assertFalse(self.log.exists())

    def test_placeholders_empties_bad_names_and_missing_containers_are_refused(self):
        for oob, managed in (({"SUPABASE_URL": "[SENSITIVE]"}, self.MANAGED), ({"SUPABASE_URL": " "}, self.MANAGED),
                             ({"supabase_url": "x"}, self.MANAGED), ({}, self.MANAGED), (self.OOB, ["x"]),
                             (None, self.MANAGED), (self.OOB, None)):
            r, out = self.run_loader("colton-games", oob=oob, managed=managed)
            self.assertNotEqual(r.returncode, 0, (oob, managed))
        self.assertFalse(self.log.exists())

    def test_an_incomplete_or_extra_out_of_band_set_is_refused(self):
        missing = {k: v for k, v in self.OOB.items() if k != "SKYHOOK_LEADERBOARD_SECRET"}
        for oob in (missing, {**self.OOB, "EXTRA_NAME": "x"}):
            r, out = self.run_loader("colton-games", oob=oob, managed=self.MANAGED)
            self.assertNotEqual(r.returncode, 0, oob)
            self.assertIn("must hold exactly", out)
        self.assertFalse(self.log.exists())

    def test_the_loader_and_terraform_agree_on_the_out_of_band_names(self):
        tf = re.search(r"colton_games_prod_worker_out_of_band = \[(.*?)\]", SECRETS).group(1)
        sh = re.search(r'colton-games\) OUT_OF_BAND_NAMES="([^"]+)"', SCRIPT.read_text()).group(1)
        self.assertEqual(sorted(re.findall(r'"([A-Z_]+)"', tf)), sorted(sh.split()))

    def test_a_site_without_production_secrets_is_refused(self):
        r, out = self.run_loader("imagine", oob=self.OOB, managed=self.MANAGED)
        self.assertEqual(r.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_the_site_argument_is_validated(self):
        for bad in ("../x", "Colton", "a b", "x;rm", ""):
            r, out = self.run_loader(bad, oob=self.OOB, managed=self.MANAGED)
            self.assertNotEqual(r.returncode, 0, bad)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
