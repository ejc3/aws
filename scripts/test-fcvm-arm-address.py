#!/usr/bin/env python3
"""fcvm-metal-arm has one public address, on its primary private address, and nothing
references the address it replaced (released, so whoever AWS gives it next gets no access)."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent


class ArmAddress(unittest.TestCase):
    def test_the_replaced_address_is_released_and_unreferenced(self):
        for tf in sorted(ROOT.glob('*.tf')):
            text = tf.read_text()
            self.assertNotIn('resource "aws_eip" "firecracker_dev" {', text, tf.name)
            self.assertNotRegex(text, r'aws_eip\.firecracker_dev(\[|\s*:)', tf.name)

    def test_the_address_is_on_the_primary_private_address(self):
        tf = (ROOT / 'firecracker-dev.tf').read_text()
        assoc = re.search(r'resource "aws_eip_association" "firecracker_dev_next" \{.*?\n\}', tf, re.S).group()
        self.assertIn('private_ip_address   = aws_network_interface.firecracker_dev[0].private_ip\n', assoc)
        self.assertIn('allow_reassociation = true', assoc)
        self.assertIn('firecracker_dev_public_ips = [for e in aws_eip.firecracker_dev_next : e.public_ip]', tf)


if __name__ == '__main__':
    unittest.main()
