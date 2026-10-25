#!/usr/bin/env python3
"""The added address survives boot and never restarts the live primary interface."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('secondary_ipv4', ROOT / 'scripts/fcvm-secondary-ipv4.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SecondaryAddress(unittest.TestCase):
    def test_persists_and_applies_without_replacing_dhcp_or_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / '61-fcvm-secondary-ipv4.yaml'
            calls = []
            def run(args, **kwargs):
                calls.append(args)
                return subprocess.CompletedProcess(args, 0, json.dumps([{'dev': 'ens5', 'metric': 100}]))
            module.configure('10.0.1.42', config, run)
            first = config.read_bytes()
            self.assertIn(b'ens5:\n      addresses:\n        - 10.0.1.42/32\n', first)
            self.assertNotIn(b'dhcp4:', first)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertIn(['netplan', 'generate'], calls)
            self.assertEqual(calls[-1], ['ip', '-4', 'address', 'replace', '10.0.1.42/32', 'dev', 'ens5'])
            module.configure('10.0.1.42', config, run)
            self.assertEqual(config.read_bytes(), first)
            self.assertNotIn(['netplan', 'apply'], calls)
            self.assertFalse(any('restart' in call for call in calls))

    def test_failed_boot_configuration_rolls_back_before_any_live_change(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / '61-fcvm-secondary-ipv4.yaml'
            original = 'network:\n  version: 2\n'
            config.write_text(original)
            calls = []
            def run(args, **kwargs):
                calls.append(args)
                if args == ['netplan', 'generate'] and calls.count(args) == 1:
                    raise subprocess.CalledProcessError(1, args)
                return subprocess.CompletedProcess(args, 0, '[{"dev":"ens5"}]')
            with self.assertRaises(subprocess.CalledProcessError):
                module.configure('10.0.1.42', config, run)
            self.assertEqual(config.read_text(), original)
            self.assertFalse(any('replace' in call for call in calls))

    def test_no_address_can_be_written_without_an_interface_or_valid_ip(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'netplan.yaml'
            def run(args, **kwargs):
                return subprocess.CompletedProcess(args, 0, '[]')
            with self.assertRaises(RuntimeError):
                module.configure('10.0.1.42', config, run)
            with self.assertRaises(ValueError):
                module.configure('10.0.1.42; reboot', config, run)
            self.assertFalse(config.exists())

    def test_managed_association_runs_the_script_on_the_arm_host(self):
        tf = (ROOT / 'firecracker-dev.tf').read_text()
        self.assertIn('resource "aws_ssm_association" "firecracker_dev_secondary_ipv4"', tf)
        association = tf.split('resource "aws_ssm_association" "firecracker_dev_secondary_ipv4"', 1)[1].split('\n}\n', 1)[0]
        self.assertIn('file("${path.module}/scripts/fcvm-secondary-ipv4.py")', association)
        self.assertIn('aws_instance.firecracker_dev[0].id', association)
        self.assertIn('wait_for_success_timeout_seconds', association)


if __name__ == '__main__':
    unittest.main(buffer=True)
