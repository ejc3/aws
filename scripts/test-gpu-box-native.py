"""Offline native GPU lifetime and boot-state tests. Operational callbacks are fakes."""
import contextlib
import datetime
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.request
from unittest.mock import patch

SOURCE = (Path(__file__).resolve().parent / "gpu-box-native-bootstrap.sh").read_text()
STATE = SOURCE.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]


class Bootstrap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='native-bootstrap-test-')
        self.root = Path(self.tmp.name)
        self.now = 1700000100
        self.boot = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        self.calls = []
        self.versions = {'nvidia-smi': '580.196.07, Tesla T4', 'podman': 'podman version 4.9.3',
                         'nvidia-ctk': 'NVIDIA Container Toolkit CLI version 1.20.1\ncommit: fixture'}
        (self.root / 'boot').write_text(self.boot)
        (self.root / 'cdi').write_text('deviceNodes:\n  - path: /dev/nvidia-uvm\n    major: 511\n  - path: /dev/nvidia-uvm-tools\n    major: 511\n    minor: 1\n')
        (self.root / 'devices').write_text('Character devices:\n509 nvidia-uvm\n195 nvidia-frontend\nBlock devices:\n8 sd\n')
        self.real_stat = os.stat

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, value):
        result = Path({'/proc/sys/kernel/random/boot_id': str(self.root / 'boot'),
                     '/proc/devices': str(self.root / 'devices'),
                     '/etc/cdi/nvidia.yaml': str(self.root / 'cdi'),
                     '/etc/gpu-box-ready': str(self.root / 'ready')}.get(str(value), value))
        self.assertTrue(result.is_relative_to(self.root), 'unexpected fixture path')
        return result

    def fake_stat(self, path, *args, **kwargs):
        if str(path) in {'/dev/nvidia-uvm', '/dev/nvidia-uvm-tools'}:
            return types.SimpleNamespace(st_mode=stat.S_IFCHR | 0o666,
                st_rdev=os.makedev(509, int(str(path).endswith('-tools'))))
        value = self.real_stat(path, *args, **kwargs)
        if str(path) == str(self.root / 'state.json'):
            values = list(value); values[4] = 0
            return os.stat_result(values)
        return value

    def fake_lstat(self, path, *args, **kwargs):
        return self.fake_stat(path, *args, follow_symlinks=False, **kwargs)

    def url(self, request, timeout):
        self.calls.append(request.full_url)
        if request.full_url == 'http://169.254.169.254/latest/api/token':
            self.assertEqual(request.method, 'PUT')
            return io.BytesIO(b'fixture-token')
        self.assertEqual(request.full_url, 'http://169.254.169.254/latest/dynamic/instance-identity/document')
        return io.BytesIO(json.dumps({'instanceId': 'i-0123456789abcdef0', 'instanceType': 'g4dn.xlarge',
            'pendingTime': '2023-11-14T22:13:20Z'}).encode())

    def command(self, argv, **kwargs):
        self.assertIn(argv[0], self.versions)
        return types.SimpleNamespace(stdout=self.versions[argv[0]])

    def operation(self, *args):
        # Imported Path is replaced only in the state block's pathlib module;
        # every absolute runtime path it reads is redirected into our own TMP.
        module = types.SimpleNamespace(Path=self.path)
        # Path.lstat uses stat(follow_symlinks=False) or lstat across Python versions.
        with patch.dict(sys.modules, {'pathlib': module}), patch('os.stat', self.fake_stat), \
             patch('os.lstat', self.fake_lstat), \
             patch('urllib.request.urlopen', self.url), patch('time.time', lambda: self.now), \
             patch('subprocess.run', self.command), patch.object(sys, 'argv', ['state', str(self.root), *args]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            try:
                exec(compile(STATE, '<actual-bootstrap-state>', 'exec'), {})
            except SystemExit as error:
                if error.code not in {None, 0}:
                    raise
            return output.getvalue().strip()

    def newboot(self, letter):
        self.boot = letter * 8 + '-' + letter * 4 + '-4' + letter * 3 + '-8' + letter * 3 + '-' + letter * 12
        (self.root / 'boot').write_text(self.boot)

    def test_original_launch_deadline_never_renews(self):
        self.operation('init')
        self.assertEqual(int(self.operation('get', 'deadline_unix')), 1700001800)
        self.now += 900
        self.operation('init')
        self.assertEqual(int(self.operation('get', 'deadline_unix')), 1700001800)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.operation('calendar'), '2023-11-14 22:43:20 UTC')

    def test_two_real_boot_transitions_before_ready_and_static_cdi_evidence(self):
        self.operation('init'); self.operation('phase', 'upgrade_reboot_pending')
        with self.assertRaises(AssertionError):
            self.operation('newboot')
        self.assertFalse((self.root / 'ready').exists())
        self.newboot('b'); self.operation('newboot'); self.operation('phase', 'prep_running')
        self.operation('snapshot', 'before'); self.operation('phase', 'prep_reboot_pending')
        self.operation('snapshot', 'after')
        with self.assertRaises(AssertionError):
            self.operation('ready')
        self.assertFalse((self.root / 'ready').exists())
        self.newboot('c'); self.operation('newboot'); self.operation('phase', 'checking')
        self.operation('snapshot', 'after'); self.operation('ready')
        ready = json.loads((self.root / 'ready').read_text())
        self.assertEqual(len(set(ready['boot_ids'])), 3)
        self.assertTrue(ready['cdi_unchanged'])
        evidence = json.loads((self.root / 'after.json').read_text())
        self.assertEqual(evidence['cdi_device_nodes'][0]['major'], 511)
        self.assertEqual(evidence['actual_device_nodes']['/dev/nvidia-uvm']['major'], 509)
        self.assertEqual(evidence['character_devices']['nvidia-uvm'], 509)

    def shell_block(self, condition, sentinel):
        start = SOURCE.index(condition)
        return SOURCE[start:SOURCE.index(sentinel, start)]

    def test_real_install_block_rearms_same_absolute_timer_before_bootstrap(self):
        block = self.shell_block('if [[ ${1:-} = --install ]]; then', '\n[[ $# = 0 ]]')
        unitdir = self.root / 'units'; unitdir.mkdir()
        self.assertEqual(block.count('/etc/systemd/system/'), 3)
        block = block.replace('/etc/systemd/system/', str(unitdir) + '/')
        prelude = 'state() { if [[ $1 = calendar ]]; then echo "2023-11-14 22:43:20 UTC"; fi; }; systemctl() { printf "%s\\n" "$*" >> "$EVENTS"; }; SCRIPT=/usr/local/sbin/gpu-box-native-bootstrap; '
        for _ in range(3):
            env = {'PATH': '/usr/bin:/bin', 'EVENTS': str(self.root / 'events')}
            subprocess.run(['/bin/bash', '-eu', '-c', prelude + block, 'fixture', '--install'], env=env, check=True)
            timer = (unitdir / 'gpu-box-native-expiry.timer').read_text()
            self.assertIn('OnCalendar=2023-11-14 22:43:20 UTC', timer)
            self.assertIn('Persistent=true', timer)
        lines = (self.root / 'events').read_text().splitlines()
        self.assertLess(lines.index('enable --now gpu-box-native-expiry.timer'),
                        lines.index('start --no-block gpu-box-native-bootstrap.service'))

    def test_generated_bootstrap_job_has_no_cloud_final_ordering_cycle(self):
        service = SOURCE.split('cat > /etc/systemd/system/gpu-box-native-bootstrap.service <<EOF\n', 1)[1].split('\nEOF', 1)[0]
        service = service.replace('$SCRIPT', '/usr/bin/true')
        wanted = next(line.split('=', 1)[1] for line in service.splitlines() if line.startswith('WantedBy='))
        self.assertIn(wanted, {'multi-user.target', 'cloud-init.target'})
        with tempfile.TemporaryDirectory(prefix='native-boot-graph-') as directory:
            root = Path(directory)
            units = root / 'etc/systemd/system'; units.mkdir(parents=True)
            binary = root / 'usr/bin/true'; binary.parent.mkdir(parents=True)
            binary.write_text('#!/bin/sh\nexit 0\n'); binary.chmod(0o755)
            for name in ('sysinit.target', 'basic.target', 'shutdown.target', 'network-online.target'):
                (units / name).write_text('[Unit]\nDescription=Offline boot fixture\nDefaultDependencies=no\n')
            (units / 'multi-user.target').write_text('[Unit]\nDescription=Offline multi-user target\n')
            (units / 'default.target').write_text('[Unit]\nWants=multi-user.target cloud-init.target\n')
            (units / 'cloud-init.target').write_text('[Unit]\nWants=cloud-final.service\n')
            # Ubuntu's real cloud-final unit orders itself after multi-user.target.
            (units / 'cloud-final.service').write_text('[Unit]\nAfter=multi-user.target\n[Service]\nType=oneshot\nExecStart=/usr/bin/true\n')
            (units / 'gpu-box-native-bootstrap.service').write_text(service)
            installed = units / (wanted + '.wants'); installed.mkdir()
            (installed / 'gpu-box-native-bootstrap.service').symlink_to('../gpu-box-native-bootstrap.service')
            result = subprocess.run(['systemd-analyze', 'verify', '--man=no', '--root=' + str(root), 'default.target'],
                env={'PATH': '/usr/bin:/bin', 'SYSTEMD_LOG_COLOR': '0'}, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn('ordering cycle', result.stderr.lower())

    def test_real_expiry_branch_only_shuts_down_at_deadline(self):
        block = self.shell_block('if [[ ${1:-} = --expire ]]; then', '\n\nif [[ ${1:-} = --install ]]')
        self.assertEqual(block.count('/usr/sbin/shutdown'), 1)
        shutdown = self.root / 'shutdown'
        shutdown.write_text('#!/bin/bash\nprintf "%s\\n" "$*" > "$MARKER"\n'); shutdown.chmod(0o700)
        block = block.replace('/usr/sbin/shutdown', '"$FAKE_SHUTDOWN"')
        prelude = 'state() { echo 1800; }; date() { echo "$NOW"; }; '
        env = {'PATH': '/usr/bin:/bin', 'FAKE_SHUTDOWN': str(shutdown), 'MARKER': str(self.root / 'expired')}
        for now, code in [(1799, 1), (1800, 0), (1801, 0)]:
            (self.root / 'expired').unlink(missing_ok=True)
            value = subprocess.run(['/bin/bash', '-eu', '-c', prelude + block, 'fixture', '--expire'],
                                   env=dict(env, NOW=str(now)))
            self.assertEqual(value.returncode, code)
            self.assertEqual((self.root / 'expired').exists(), now >= 1800)


if __name__ == '__main__':
    # No network can escape even if an expected fake were omitted.
    with patch.object(socket, 'socket', side_effect=AssertionError('offline fixture network access')):
        unittest.main(buffer=True)
