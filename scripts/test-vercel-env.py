#!/usr/bin/env python3
"""vercel-env.tf and vercel-env-vars.tf: Terraform writes the hand-set Vercel environments from the vercel-env/<site>/<target>
containers, importing every existing variable by id; the generated inventory carries no value and matches its generator."""
import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "vercel-env.tf").read_text()
VARS = (ROOT / "vercel-env-vars.tf").read_text()
SECRETS = (ROOT / "vercel-env-secrets.tf").read_text()
SPEC = importlib.util.spec_from_file_location("inv", ROOT / "scripts" / "vercel-env-inventory.py")
inv = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inv)

ENTRY = re.compile(r'^    "([^"/]+)/([A-Za-z0-9]+)"\s+= \{ key = "([^"]+)", target = \[([^\]]*)\], sensitive = (true|false) \}$', re.M)


def parsed():
    out = {}
    for site, ident, key, target, sensitive in ENTRY.findall(VARS):
        out[f"{site}/{ident}"] = {"key": key, "target": [t.strip().strip('"') for t in target.split(",") if t.strip()],
                                  "sensitive": sensitive == "true"}
    return out


class InventoryTests(unittest.TestCase):
    def test_the_generated_file_round_trips_through_the_generator(self):
        variables = parsed()
        self.assertGreater(len(variables), 0)
        self.assertEqual(VARS, inv.render(variables), "vercel-env-vars.tf must be exactly what the generator writes")

    def test_it_never_holds_a_value(self):
        self.assertNotIn("value", VARS.split("locals {", 1)[1])

    def test_the_sites_match_between_the_generator_and_terraform(self):
        tf_projects = dict(re.findall(r'^    "([a-z-]+)"\s+= \{ project_id = "(prj_[A-Za-z0-9]+)", team_id = "team_[A-Za-z0-9]+" \}$', TF, re.M))
        self.assertEqual(tf_projects, {site: pid for site, (pid, _team) in inv.PROJECTS.items()})
        self.assertEqual({k.split("/")[0] for k in parsed()} - set(tf_projects), set(), "every inventoried site is a managed project")

    def test_every_target_has_a_container(self):
        targets = {}
        for m in re.finditer(r'^    "([a-z-]+)"\s+= \[([^\]]*)\]$', SECRETS, re.M):
            targets[m.group(1)] = re.findall(r'"([a-z]+)"', m.group(2))
        for ident, v in parsed().items():
            for t in v["target"]:
                self.assertIn(t, targets.get(ident.split("/")[0], []), f"{ident} needs vercel-env/{ident.split('/')[0]}/{t}")


class TerraformTests(unittest.TestCase):
    def blocks(self):
        return re.findall(r'resource "vercel_project_environment_variable" "(vercel_env_[a-z0-9_]+)" \{(.*?)\n\}\n', TF, re.S)

    def test_two_teams_each_with_checks_and_an_import(self):
        blocks = dict(self.blocks())
        self.assertEqual(sorted(blocks), ["vercel_env_coltons", "vercel_env_ejc3"])
        for name, body in blocks.items():
            self.assertEqual(body.count("precondition {"), 2, name)
            self.assertIn("value      = local.vercel_env_value[each.key]", body, name)
            self.assertIn(f"to       = vercel_project_environment_variable.{name}[each.key]", TF, name)
        self.assertIn("provider   = vercel.ejc3", blocks["vercel_env_ejc3"])
        self.assertIn("provider   = vercel.coltons", blocks["vercel_env_coltons"])
        self.assertIn("api_token = trimspace(ephemeral.aws_secretsmanager_secret_version.vercel_api_token_coltons.secret_string)", TF)

    def test_the_import_id_is_team_project_and_variable(self):
        ids = re.findall(r'id       = "(\$\{.*?\})/(\$\{.*?\})/(\$\{split\("/", each.key\)\[1\]\})"', TF)
        self.assertEqual(len(ids), 2)

    def test_the_personal_team_provider_reads_its_own_token(self):
        self.assertIn('alias     = "ejc3"', TF)
        self.assertIn("api_token = trimspace(ephemeral.aws_secretsmanager_secret_version.vercel_api_token_ejc3.secret_string)", TF)
        self.assertIn('Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }', TF)

    def test_the_retired_dolphin_labs_team_is_not_managed(self):
        # The Dolphin sites left Vercel and their team is retired (ejc3/aws #503 forgot what Terraform managed there).
        for text in (TF, VARS, (ROOT / "scripts" / "vercel-env-inventory.py").read_text()):
            self.assertNotIn("team_4TayirAcbgetrOUgYXghMXJH", text)
            self.assertNotIn("dolphin", re.sub(r"#.*", "", text))
        self.assertNotIn("dolphin-labs", inv.PROJECTS)

    def test_the_dolphin_containers_stay_but_are_not_vercel_records(self):
        # vercel-env/dolphin-labs/production is the dolphin-labs-prod Worker's source; same addresses, not capturable.
        dolphin = SECRETS.split("vercel_env_dolphin = {", 1)[1].split("}", 1)[0]
        self.assertIn('"dolphin-labs"  = ["development", "preview", "production"]', dolphin)
        self.assertIn('"dolphin-films" = ["preview", "production"]', dolphin)
        targets = SECRETS.split("vercel_env_targets = {", 1)[1].split("}", 1)[0]
        self.assertNotIn("dolphin", targets)
        self.assertIn("merge(local.vercel_env_targets, local.vercel_env_dolphin)", SECRETS)

    def test_values_come_only_from_the_containers(self):
        self.assertIn('local.vercel_env_record["${split("/", id)[0]}/${v.target[0]}"][v.key]', TF)
        self.assertIn("secret_id = aws_secretsmanager_secret.vercel_env[each.key].id", TF)


if __name__ == "__main__":
    unittest.main()
