#!/usr/bin/env python3
"""fcvm-metal-arm has one public address, on its primary private address, and nothing
references the address it replaced (released, so whoever AWS gives it next gets no access)."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
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


IPV4 = re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b')
# The scripts that reach fcvm-metal-arm by address, with arguments that get each one past its argument checks.
HOST_SCRIPTS = {
    'claw-master-login.sh': ['claude-test'],
    'claw-master-mac-setup.sh': ['mac'],
    'mac-ssh-connector-setup.sh': ['mac'],
    'windows-pc-setup.sh': ['colin-pc'],
    'windows-pc-bootstrap.sh': ['print', 'test-pc', '21899'],
}


class HostComesFromTheOperator(unittest.TestCase):
    """The box's address is not in the repository: FCVM_HOST, else the `fcvm-arm` ssh alias, else an error.
    A default literal would point a script at a stranger once the address is released."""

    def run_script(self, home, name, alias_host=None, **env):
        """Run a script with stub ssh and aws on PATH. `ssh -G fcvm-arm` answers with alias_host as its hostname,
        or with the alias itself, which is what ssh prints for a name that has no config entry."""
        stubs = Path(home) / 'stubs'
        stubs.mkdir(exist_ok=True)
        (stubs / 'ssh').write_text(
            '#!/bin/sh\n'
            'printf "%s\\n" "$*" >> "$HOME/ssh.log"\n'
            f'if [ "$1" = -G ]; then echo "hostname {alias_host or "fcvm-arm"}"; exit 0; fi\n'
            'exit 0\n')
        (stubs / 'aws').write_text('#!/bin/sh\necho aws >> "$HOME/aws.log"\nexit 99\n')
        for stub in stubs.iterdir():
            stub.chmod(0o755)
        ssh = Path(home) / '.ssh'
        ssh.mkdir(exist_ok=True)
        if not (ssh / 'to-test-pc.pub').exists():
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(ssh / 'to-test-pc')], check=True)
        base = {k: v for k, v in os.environ.items() if k != 'FCVM_HOST'}
        return subprocess.run(['bash', str(ROOT / 'scripts' / name), *HOST_SCRIPTS[name]], capture_output=True, text=True,
                              timeout=60, stdin=subprocess.DEVNULL,
                              env=dict(base, HOME=home, PATH=f'{stubs}:/usr/bin:/bin', FCVM_KEY='/nonexistent', **env))

    def test_no_script_names_an_address_for_the_host(self):
        for name in HOST_SCRIPTS:
            for n, line in enumerate((ROOT / 'scripts' / name).read_text().splitlines(), 1):
                if 'FCVM_HOST' in line or re.match(r'\s*(FCVM|HOST|ip)=', line):
                    public = [a for a in IPV4.findall(line) if not a.startswith(('10.', '127.'))]
                    self.assertEqual(public, [], f'{name}:{n}')

    def test_without_FCVM_HOST_or_the_alias_a_script_stops_and_names_both(self):
        for name in HOST_SCRIPTS:
            with tempfile.TemporaryDirectory() as home:
                r = self.run_script(home, name)
                self.assertEqual(r.returncode, 2, f'{name}: {r.stdout}{r.stderr}')
                self.assertIn('FCVM_HOST', r.stderr, name)
                if name != 'windows-pc-bootstrap.sh':  # it runs on the box itself, where no alias points at the box
                    self.assertIn('fcvm-arm', r.stderr, name)
                self.assertFalse((Path(home) / 'aws.log').exists(), f'{name} reached aws without a host')
                ssh_log = Path(home) / 'ssh.log'
                self.assertNotIn('ubuntu@', ssh_log.read_text() if ssh_log.exists() else '', name)

    def test_the_alias_hostname_is_used_and_FCVM_HOST_wins_over_it(self):
        with tempfile.TemporaryDirectory() as home:
            r = self.run_script(home, 'claw-master-login.sh', alias_host='203.0.113.9')
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn('ubuntu@203.0.113.9', (Path(home) / 'ssh.log').read_text())
        with tempfile.TemporaryDirectory() as home:
            r = self.run_script(home, 'claw-master-login.sh', alias_host='203.0.113.9', FCVM_HOST='203.0.113.20')
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            log = (Path(home) / 'ssh.log').read_text()
            self.assertIn('ubuntu@203.0.113.20', log)
            self.assertNotIn('203.0.113.9', log)

    def test_the_bootstrap_paste_uses_FCVM_HOST(self):
        with tempfile.TemporaryDirectory() as home:
            r = self.run_script(home, 'windows-pc-bootstrap.sh', FCVM_HOST='203.0.113.7')
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn('pcboot@203.0.113.7', r.stdout)


if __name__ == '__main__':
    unittest.main(buffer=True)
