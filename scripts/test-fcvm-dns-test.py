#!/usr/bin/env python3
"""Test the isolated propagation probe and its IAM and DNS wiring."""
import contextlib
import importlib.util
import io
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("probe", ROOT / "scripts/fcvm-dns-probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class PropagationTests(unittest.TestCase):
    def test_child_zone_is_delegated_and_writer_is_confined(self):
        dns = (ROOT / "fcvm-dns-test.tf").read_text()
        self.assertIn('fcvm_dns_probe_zone = "dnstest.fcvm.dev"', dns)
        self.assertIn('resource "aws_route53_zone" "fcvm_dns_probe"', dns)
        delegation = dns.split('resource "cloudflare_dns_record" "fcvm_dns_probe_ns"')[1].split("\n}")[0]
        for field in ["4", "var.fcvm_dev_zone_id", "local.fcvm_dns_probe_zone", '"NS"',
                      "aws_route53_zone.fcvm_dns_probe.name_servers[count.index]"]:
            self.assertIn(field, delegation)
        grant = (ROOT / "constellation-account.tf").read_text().split('Sid      = "ChangeFcvmProbeRecords"')[1].split("}, {")[0]
        for field in ['"route53:ChangeResourceRecordSets"', 'aws_route53_zone.fcvm_dns_probe.arn', '"aws:SourceIp"',
                      'local.fcvm_dns_probe_source_cidrs', '["probe-*.${local.fcvm_dns_probe_zone}"]',
                      '["A", "AAAA", "TXT"]', '["CREATE", "DELETE"]', 'Null = {']:
            self.assertIn(field, grant)
        for key in ["NormalizedRecordNames", "RecordTypes", "Actions"]:
            self.assertEqual(grant.count("route53:ChangeResourceRecordSets" + key), 2)
        self.assertNotIn('"*"', grant)

    def simulate(self, timeout):
        client = mock.Mock()
        def answers(name, kind, server=None):
            if kind == "NS":
                return [f"ns{i}.example." for i in range(4)]
            if server is None:
                return ['"Z1234"']
            return ['"' + name.split(".")[0].removeprefix("probe-") + '"']
        with mock.patch.object(probe, "lookup", side_effect=answers), contextlib.redirect_stdout(io.StringIO()):
            if timeout:
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    probe.probe(client, timeout=0)
            else:
                probe.probe(client)
        first, second = [call.kwargs for call in client.change_resource_record_sets.call_args_list]
        self.assertEqual(first["HostedZoneId"], "Z1234")
        created = first["ChangeBatch"]["Changes"][0]
        self.assertRegex(created["ResourceRecordSet"]["Name"], r"^probe-[0-9a-f]{32}\.dnstest\.fcvm\.dev$")
        self.assertEqual(created["Action"], "CREATE")
        self.assertEqual(second["ChangeBatch"]["Changes"], [dict(created, Action="DELETE")])

    def test_created_record_is_deleted_after_observation(self):
        self.simulate(False)

    def test_created_record_is_deleted_after_timeout(self):
        self.simulate(True)

    def test_missing_delegation_prevents_write(self):
        client = mock.Mock()
        with mock.patch.object(probe, "lookup", return_value=[]), self.assertRaisesRegex(RuntimeError, "cannot resolve"):
            probe.probe(client)
        client.change_resource_record_sets.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)
