#!/usr/bin/env python3
"""Production TLS seed tests using Secrets Manager's documented first-version staging semantics."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock

SCRIPT = Path(__file__).with_name("fcvm-prod-tls-seed.py")
spec = importlib.util.spec_from_file_location("prod_tls", SCRIPT)
prod_tls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prod_tls)
ARN = "arn:aws:secretsmanager:us-east-2:000000000000:secret:fcvm-fleet/prod/tls-AbCdEf"


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.versions = {}
        self.client = Mock(spec_set=["list_secret_version_ids", "put_secret_value"])
        self.client.list_secret_version_ids.side_effect = self.page
        self.client.put_secret_value.side_effect = self.put

    def page(self, SecretId, IncludeDeprecated, NextToken="0"):
        self.assertEqual(SecretId, ARN)
        self.assertTrue(IncludeDeprecated)
        # Separate pages cover older/unlabelled versions and a current version on a later page.
        index = int(NextToken)
        versions = [{"VersionId": key, "VersionStages": stages} for key, stages in self.versions.items()]
        page = {"Versions": versions[index:index + 1]}
        if index + 1 < len(versions):
            page["NextToken"] = str(index + 1)
        return page

    def put(self, SecretId, ClientRequestToken, SecretString, VersionStages):
        self.assertEqual((SecretId, SecretString, VersionStages), (ARN, "{}", [prod_tls.SEED_STAGE]))
        if ClientRequestToken not in self.versions:
            self.versions[ClientRequestToken] = VersionStages + ([] if self.versions else ["AWSCURRENT"])
        return {"VersionId": ClientRequestToken, "VersionStages": self.versions[ClientRequestToken]}

    def test_pristine_secret_and_retry_after_lost_response(self):
        def lost_reply(**kwargs):
            self.put(**kwargs)
            raise TimeoutError("response lost")
        self.client.put_secret_value.side_effect = lost_reply
        with self.assertRaises(TimeoutError):
            prod_tls.bootstrap(self.client, ARN)
        prod_tls.bootstrap(self.client, ARN)
        self.client.put_secret_value.assert_called_once()
        self.assertEqual(list(self.versions.values()), [[prod_tls.SEED_STAGE, "AWSCURRENT"]])

    def test_existing_or_renewed_certificate_remains_current(self):
        self.versions.update({"seed": [prod_tls.SEED_STAGE], "previous": [], "certificate": ["AWSCURRENT"]})
        prod_tls.bootstrap(self.client, ARN)
        self.client.put_secret_value.assert_not_called()
        self.assertEqual(self.versions["certificate"], ["AWSCURRENT"])

    def test_another_writer_wins_between_inventory_and_put(self):
        def writer_first(**kwargs):
            self.versions["new-certificate"] = ["AWSCURRENT"]
            return self.put(**kwargs)
        self.client.put_secret_value.side_effect = writer_first
        prod_tls.bootstrap(self.client, ARN)
        self.assertEqual(self.versions["new-certificate"], ["AWSCURRENT"])
        self.assertEqual(sum("AWSCURRENT" in stages for stages in self.versions.values()), 1)

    def test_concurrent_bootstraps_use_one_version(self):
        def second_bootstrap_first(**kwargs):
            self.client.put_secret_value.side_effect = self.put
            prod_tls.bootstrap(self.client, ARN)
            return self.put(**kwargs)
        self.client.put_secret_value.side_effect = second_bootstrap_first
        prod_tls.bootstrap(self.client, ARN)
        self.assertEqual(self.client.put_secret_value.call_count, 2)
        self.assertEqual(list(self.versions.values()), [[prod_tls.SEED_STAGE, "AWSCURRENT"]])

    def test_any_history_without_current_requires_recovery(self):
        for stages in ([], ["AWSPENDING"], [prod_tls.SEED_STAGE]):
            with self.subTest(stages=stages):
                self.versions["orphan"] = stages
                with self.assertRaisesRegex(RuntimeError, "versions but no AWSCURRENT"):
                    prod_tls.bootstrap(self.client, ARN)
                self.client.put_secret_value.assert_not_called()

    def test_new_orphan_during_bootstrap_is_not_promoted(self):
        def orphan_first(**kwargs):
            self.versions["orphan"] = []
            return self.put(**kwargs)
        self.client.put_secret_value.side_effect = orphan_first
        with self.assertRaisesRegex(RuntimeError, "No AWSCURRENT"):
            prod_tls.bootstrap(self.client, ARN)
        self.assertFalse(any("AWSCURRENT" in stages for stages in self.versions.values()))

    def test_terraform_waits_for_seed_before_first_ecs_task(self):
        tf = (SCRIPT.parent.parent / "fcvm-fleet-prod.tf").read_text()
        start = tf.index('resource "terraform_data" "fcvm_fleet_prod_tls_bootstrap"')
        resource = tf[start:tf.index("\n}\n", start)]
        self.assertIn('triggers_replace = aws_secretsmanager_secret.fcvm_fleet_prod_tls[0].arn', resource)
        self.assertIn('scripts/fcvm-prod-tls-seed.py', resource)
        start = tf.index('resource "aws_ecs_service" "fcvm_fleet_prod_coordinator"')
        service = tf[start:tf.index("\n}\n", start)]
        deps = service.split("depends_on = [", 1)[1].split("]", 1)[0]
        self.assertIn("terraform_data.fcvm_fleet_prod_tls_bootstrap,", deps)


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)
