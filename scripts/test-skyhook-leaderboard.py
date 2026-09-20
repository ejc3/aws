#!/usr/bin/env python3
"""Credential-free source guards for Skyhook's staging leaderboard infrastructure.

These inspect the checked-in configuration, not live accounts or Terraform state.
They complement provider validation; they do not prove an apply, Access enforcement,
application bindings, migrations, proxy behavior, or a working global leaderboard.
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parent.parent
CONFIG = 'skyhook-leaderboard.tf'


def source(filename):
    return (ROOT / filename).read_text()


def resource(filename, kind, name):
    match = re.search(
        r'^resource "' + re.escape(kind) + r'" "' + re.escape(name)
        + r'" \{\n.*?^\}', source(filename), re.M | re.S,
    )
    if match is None:
        raise AssertionError(f'Missing {kind}.{name} in {filename}')
    return match.group()


def without_comments(text):
    return re.sub(r'(?m)^\s*#.*$', '', text)


class SkyhookLeaderboardTests(unittest.TestCase):
    def test_database_is_staging_only_and_uses_the_managed_account(self):
        database = resource(CONFIG, 'cloudflare_d1_database', 'skyhook_leaderboard_stage')
        self.assertRegex(database, r'name\s*=\s*"skyhook-leaderboard-stage"')
        self.assertRegex(database, r'account_id\s*=\s*var\.cloudflare_account_id')
        self.assertNotRegex(database, r'(?m)^\s*(count|for_each|provider)\s*=')

    def test_scores_and_signing_material_are_protected_from_destroy(self):
        for kind, name in [
            ('cloudflare_d1_database', 'skyhook_leaderboard_stage'),
            ('random_password', 'skyhook_leaderboard_stage'),
            ('cloudflare_secrets_store', 'games'),
            ('cloudflare_secrets_store_secret', 'skyhook_leaderboard_stage'),
            ('aws_secretsmanager_secret', 'skyhook_leaderboard_stage_proxy'),
        ]:
            with self.subTest(resource=f'{kind}.{name}'):
                block = resource(CONFIG, kind, name)
                self.assertRegex(block, r'prevent_destroy\s*=\s*true')

    def test_signing_key_is_generated_and_only_available_to_workers(self):
        password = resource(CONFIG, 'random_password', 'skyhook_leaderboard_stage')
        length = re.search(r'(?m)^\s*length\s*=\s*(\d+)\s*$', password)
        self.assertIsNotNone(length)
        self.assertGreaterEqual(int(length.group(1)), 48)
        self.assertRegex(password, r'special\s*=\s*false')
        secret = resource(CONFIG, 'cloudflare_secrets_store_secret', 'skyhook_leaderboard_stage')
        self.assertRegex(secret, r'account_id\s*=\s*var\.cloudflare_account_id')
        self.assertRegex(secret, r'store_id\s*=\s*cloudflare_secrets_store\.games\.id')
        self.assertRegex(secret, r'name\s*=\s*"skyhook-leaderboard-stage"')
        self.assertRegex(secret, r'scopes\s*=\s*\[\s*"workers"\s*\]')
        self.assertRegex(secret, r'value\s*=\s*random_password\.skyhook_leaderboard_stage\.result')

    def test_dev_token_is_dedicated_finite_and_not_a_public_bypass(self):
        token = resource(CONFIG, 'cloudflare_zero_trust_access_service_token', 'skyhook_dev')
        self.assertRegex(token, r'account_id\s*=\s*var\.cloudflare_account_id')
        self.assertRegex(token, r'duration\s*=\s*"8760h"')
        policy = resource(CONFIG, 'cloudflare_zero_trust_access_policy', 'skyhook_dev')
        self.assertRegex(policy, r'decision\s*=\s*"non_identity"')
        self.assertRegex(policy, r'token_id\s*=\s*'
                         r'cloudflare_zero_trust_access_service_token\.skyhook_dev\.id')
        self.assertEqual(policy.count('token_id'), 1)
        self.assertNotRegex(without_comments(policy), r'everyone|any_valid_service_token|bypass')

    def test_dev_policy_is_attached_only_to_the_staging_worker(self):
        references = []
        for path in ROOT.glob('*.tf'):
            for match in re.finditer(
                r'^resource "cloudflare_zero_trust_access_application" "([^"]+)" '
                r'\{\n.*?^\}', path.read_text(), re.M | re.S,
            ):
                if 'cloudflare_zero_trust_access_policy.skyhook_dev.id' in match.group():
                    references.append((path.name, match.group(1)))
        self.assertEqual(references, [('cloudflare.tf', 'colton_games_stage')])

    def test_human_and_existing_automation_access_are_preserved(self):
        app = resource('cloudflare.tf', 'cloudflare_zero_trust_access_application',
                       'colton_games_stage')
        attached = re.findall(
            r'id\s*=\s*cloudflare_zero_trust_access_policy\.([a-z_]+)\.id\s*'
            r'precedence\s*=\s*(\d+)', app,
        )
        self.assertEqual(attached, [('cc_games_allowed', '1'), ('cc_games_service', '2'),
                                    ('skyhook_dev', '3')])
        self.assertRegex(app, r'type\s*=\s*"worker"')
        self.assertRegex(app, r'worker_id\s*=\s*local\.colton_games_worker_id')
        self.assertNotRegex(without_comments(app), r'(?m)^\s*domain\s*=')

    def test_proxy_credential_has_recovery_window_and_no_signing_key(self):
        container = resource(CONFIG, 'aws_secretsmanager_secret',
                             'skyhook_leaderboard_stage_proxy')
        self.assertRegex(container, r'recovery_window_in_days\s*=\s*30')
        version = resource(CONFIG, 'aws_secretsmanager_secret_version',
                           'skyhook_leaderboard_stage_proxy')
        self.assertRegex(version, r'secret_id\s*=\s*'
                         r'aws_secretsmanager_secret\.skyhook_leaderboard_stage_proxy\.id')
        self.assertRegex(version, r'secret_string\s*=\s*jsonencode\(')
        for field in ['client_id', 'client_secret']:
            self.assertRegex(version, field + r'\s*=\s*'
                             r'cloudflare_zero_trust_access_service_token\.skyhook_dev\.'
                             + field)
        self.assertRegex(version, r'upstream_url\s*=')
        self.assertRegex(version, r'upstream_url\s*=\s*local\.skyhook_leaderboard_stage_url')
        payload = version.split('jsonencode({', 1)[1].split('})', 1)[0]
        self.assertEqual(re.findall(r'(?m)^\s*([a-z_]+)\s*=', payload),
                         ['upstream_url', 'client_id', 'client_secret'])
        self.assertNotRegex(without_comments(version), r'random_password|\.value|\.result')

    def test_dev_iam_reads_only_its_exact_proxy_secret(self):
        policy = resource(CONFIG, 'aws_iam_role_policy', 'nextjs_skyhook_leaderboard')
        self.assertRegex(policy, r'count\s*=\s*var\.enable_nextjs_dev\s*\?\s*1\s*:\s*0')
        self.assertRegex(policy, r'role\s*=\s*aws_iam_role\.nextjs_dev\.id')
        self.assertEqual(re.findall(r'"([a-z0-9]+:[A-Za-z*]+)"', policy),
                         ['secretsmanager:GetSecretValue'])
        self.assertEqual(re.findall(r'Effect\s*=\s*"([^"]+)"', policy), ['Allow'])
        self.assertRegex(policy, r'Resource\s*=\s*'
                         r'aws_secretsmanager_secret\.skyhook_leaderboard_stage_proxy\.arn')
        self.assertEqual(len(re.findall(r'\bResource\s*=', policy)), 1)
        self.assertNotIn('"*"', policy)

    def test_handoff_output_contains_identifiers_not_credentials(self):
        outputs = re.findall(r'^output "([^"]+)" \{\n(.*?)^\}', source(CONFIG), re.M | re.S)
        self.assertEqual([name for name, _ in outputs], ['skyhook_leaderboard_stage'])
        output = without_comments(outputs[0][1])
        self.assertIn('SKYHOOK_LEADERBOARD_DB', output)
        self.assertIn('SKYHOOK_LEADERBOARD_SECRET', output)
        self.assertRegex(output, r'database_id\s*=\s*'
                         r'cloudflare_d1_database\.skyhook_leaderboard_stage\.id')
        self.assertRegex(output, r'migrations_dir\s*=\s*"migrations/skyhook"')
        self.assertIn('cloudflare_secrets_store.games.id', output)
        self.assertIn('aws_secretsmanager_secret.skyhook_leaderboard_stage_proxy.arn', output)
        self.assertNotRegex(output, r'random_password|client_secret|client_id|secret_string|'
                            r'\.value\b|\.result\b|nonsensitive\(')

    def test_build_token_can_attach_bindings_without_database_management(self):
        deploy = resource('cloudflare.tf', 'cloudflare_api_token', 'colton_games_build_deploy')
        groups = re.findall(
            r'one\(data\.cloudflare_api_token_permission_groups_list\.([a-z_]+)'
            r'\[0\]\.result\)\.id', deploy,
        )
        self.assertEqual(groups, ['workers_scripts_write', 'secrets_store_write'])
        permissions = re.search(r'permission_groups\s*=\s*\[(.*?)\n\s*\]', deploy, re.S)
        self.assertIsNotNone(permissions)
        self.assertEqual(len(re.findall(r'\bid\s*=', permissions.group(1))), 2)
        self.assertRegex(deploy, r'resources\s*=\s*jsonencode\(\{\s*'
                         r'"com\.cloudflare\.api\.account\.\$\{var\.cloudflare_account_id\}"'
                         r'\s*=\s*"\*"\s*\}\)')
        self.assertRegex(deploy, r'count\s*=\s*local\.colton_games_workers_builds_enabled\s*\?\s*1\s*:\s*0')
        config = source('cloudflare.tf')
        for name, permission in [('workers_scripts_write', 'Workers%20Scripts%20Write'),
                                 ('secrets_store_write', 'Secrets%20Store%20Write')]:
            with self.subTest(permission=permission):
                lookup = re.search(
                    r'^data "cloudflare_api_token_permission_groups_list" "'
                    + name + r'" \{\n.*?^\}', config, re.M | re.S,
                )
                self.assertIsNotNone(lookup)
                self.assertRegex(lookup.group(), r'name\s*=\s*"' + permission + '"')
                self.assertRegex(lookup.group(), r'count\s*=\s*'
                                 r'local\.colton_games_workers_builds_enabled\s*\?\s*1\s*:\s*0')

    def test_worker_urls_still_depend_on_existing_access_and_rollout_gate(self):
        worker = resource('cloudflare.tf', 'cloudflare_worker', 'colton_games_stage')
        self.assertRegex(worker, r'enabled\s*=\s*local\.colton_games_worker_urls_enabled')
        self.assertRegex(worker, r'previews_enabled\s*=\s*local\.colton_games_worker_urls_enabled')
        self.assertIn('cloudflare_zero_trust_access_application.colton_games_stage', worker)
        self.assertRegex(worker, r'prevent_destroy\s*=\s*true')
        self.assertRegex(worker, r'ignore_changes\s*=\s*\[observability\]')
        self.assertNotRegex(without_comments(worker), r'(?m)^\s*(bindings|script|content)\s*=')

    def test_resources_use_typed_terraform_without_runtime_bootstrap(self):
        config = without_comments(source(CONFIG))
        self.assertNotRegex(config, r'local-exec|remote-exec|\bprovisioner\b|'
                            r'\buser_data\b|data "external"|resource "null_resource"')
        self.assertNotRegex(config, r'(?m)^\s*provider\s*=\s*cloudflare\.workers_builds')

    def test_validation_runs_this_source_guard_without_cloud_credentials(self):
        workflow = source('.github/workflows/drift.yml')
        self.assertIn('python3 scripts/test-skyhook-leaderboard.py', workflow)
        self.assertIn('terraform init -backend=false -lockfile=readonly', workflow)
        for forbidden in ['id-token:', 'configure-aws-credentials', 'role-to-assume',
                          'secrets.', 'terraform plan', 'terraform apply']:
            self.assertNotIn(forbidden, workflow)


if __name__ == '__main__':
    unittest.main(verbosity=2)
