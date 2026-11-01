"""Root-to-nonroot bootstrap test; all packages/CLI are owned local stubs."""
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest


HERE = Path(__file__).resolve().parent


def commands(section):
    source = (HERE.parent / 'patchdoctor.tf').read_text()
    match = re.search(r'\b' + section + r'\s*=\s*\{\s*commands\s*=\s*\[(.*?)\]\s*\}', source, re.S)
    if not match:
        raise AssertionError('actual buildspec command list missing')
    return [json.loads(line.strip().rstrip(',')) for line in match[1].splitlines() if line.strip()]


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(os.geteuid(), 0, 'run this isolated UID-drop fixture with sudo')
        self.user = pwd.getpwnam('nobody')
        self.temp = tempfile.TemporaryDirectory(prefix='patchdoctor-bootstrap-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o755)
        self.app = self.root / 'opt' / 'patchdoctor'
        self.app.mkdir(parents=True)
        self.app.parent.chmod(0o755)
        self.app.chmod(0o755)
        self.bin = self.root / 'tools'
        self.bin.mkdir(mode=0o755)
        self.bin.chmod(0o755)
        self.root_sdk = self.root / 'root-only-sdk'
        self.root_sdk.mkdir(mode=0o700)
        self.home = self.root / 'doctor-home'
        self.home.mkdir(mode=0o700)
        os.chown(self.home, self.user.pw_uid, self.user.pw_gid)
        for name in ('host.py', 'worker.py'):
            shutil.copyfile(HERE / name, self.app / name)
            (self.app / name).chmod(0o644)
        stub = '''#!/usr/bin/python3 -S
import os, pathlib, sys
args = sys.argv[1:]
if args[:3] == ['-m', 'pip', 'install']:
    assert 'boto3==1.43.123' in args
    target = pathlib.Path(args[args.index('--target') + 1]) if '--target' in args else pathlib.Path(ROOT_SDK)
    target.relative_to(pathlib.Path(FIXTURE_ROOT))
    target.mkdir(parents=True, exist_ok=True, mode=0o755)
    for name in ('boto3', 'botocore'):
        (target / (name + '.py')).write_text('__version__ = "1.43.123"\\n')
    sys.exit(0)
os.execv('/usr/bin/python3', ['/usr/bin/python3', '-S', *args])
'''.replace('ROOT_SDK', repr(str(self.root_sdk))).replace('FIXTURE_ROOT', repr(str(self.root)))
        (self.bin / 'python3').write_text(stub)
        (self.bin / 'python3').chmod(0o755)
        (self.app / 'bin').mkdir(mode=0o755)
        (self.app / 'bin').chmod(0o755)
        self.cli = self.app / 'bin' / 'claude'
        self.cli.write_text('#!/bin/sh\n[ "$#" = 1 ] && [ "$1" = --version ] || exit 91\nprintf "2.1.316 (fixture)\\n"\n')
        self.cli.chmod(0o755)
        self.env = {'PATH': str(self.bin) + ':/usr/bin:/bin', 'HOME': str(self.home),
            'AWS_EC2_METADATA_DISABLED': 'true', 'AWS_CONFIG_FILE': '/dev/null',
            'AWS_SHARED_CREDENTIALS_FILE': '/dev/null'}
        build = shlex.split(commands('build')[0])
        for item in build[build.index('env') + 1:]:
            if '=' not in item:
                break
            key, value = item.split('=', 1)
            self.env[key] = self.relocate(value)
        self.install = commands('install')

    def relocate(self, text):
        return text.replace('/opt/patchdoctor', str(self.app)).replace('/tmp/doctor-home', str(self.home))

    def install_sdk(self):
        sdk = next(command for command in self.install if ' -m pip install ' in command)
        self.assertRegex(sdk, r'^\(umask 022; python3 -m pip install [A-Za-z0-9 _./=\x27:-]+ >/dev/null\)$')
        subprocess.run(['/bin/bash', '-c', self.relocate(sdk)], env=self.env, check=True, timeout=10)

    def nonroot(self, command):
        if command.startswith('runuser -u doctor -- '):
            command = command.removeprefix('runuser -u doctor -- ')
        def drop():
            os.setgroups([])
            os.setgid(self.user.pw_gid)
            os.setuid(self.user.pw_uid)
        return subprocess.run(['/bin/bash', '-c', self.relocate(command)], env=self.env,
            cwd=self.app, preexec_fn=drop, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)

    def import_probe(self):
        # The fallback reproduces the old build's SDK visibility before it had a smoke step.
        return next((command for command in self.install if 'import boto3, botocore, host, worker' in command),
            'python3 -c ' + shlex.quote('import boto3, botocore, host, worker'))

    def test_actual_sdk_install_is_importable_by_nonroot_worker_and_cli_is_executable(self):
        self.install_sdk()
        result = self.nonroot(self.import_probe())
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        cli_probe = next(command for command in self.install if '/bin/claude --version' in command)
        result = self.nonroot(cli_probe)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(b'2.1.316', result.stdout)
        self.assertEqual(os.stat(self.app / 'python').st_uid, 0)

    def test_root_only_sdk_is_rejected_by_actual_nonroot_import_probe(self):
        self.install_sdk()
        (self.app / 'python').chmod(0o700)
        result = self.nonroot(self.import_probe())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'ModuleNotFoundError', result.stderr)


if __name__ == '__main__':
    unittest.main(buffer=True)
