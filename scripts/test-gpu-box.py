#!/usr/bin/env python3
"""Offline guards for the on-demand GPU test box (gpu-box.tf + scripts/gpu-box.sh).

Live proof is `aws iam simulate-principal-policy` against nextjs-dev-role (allow for a
tagged g4dn.xlarge from a slot's template, deny for an untagged or larger type) plus one
`gbox up` / `gbox down`; these tests keep the source from drifting out of that shape. The
script itself also runs here, against a fake `aws` on PATH, so a slot's commands are shown
to touch only that slot's box.
"""

from pathlib import Path
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest

ROOT = Path(__file__).resolve().parent.parent


def source(name):
    return (ROOT / name).read_text()


def hcl_list(text, name):
    match = re.search(r'\b' + re.escape(name) + r'\s*=\s*\[(.*?)\]', text, re.S)
    if match is None:
        raise AssertionError(f'missing list {name}')
    return re.findall(r'"([^"]+)"', match.group(1))


def statement(policy_text, sid):
    match = re.search(r'statement \{\s*sid\s*=\s*"' + re.escape(sid) + r'".*?\n  \}\n', policy_text, re.S)
    if match is None:
        raise AssertionError(f'missing statement {sid}')
    return match.group()


class GpuBoxTests(unittest.TestCase):
    tf = source('gpu-box.tf')
    script = source('scripts/gpu-box.sh')

    def test_script_lists_match_terraform(self):
        types = hcl_list(self.tf, 'gpu_box_types')
        subnets = hcl_list(self.tf, 'gpu_box_subnets')
        self.assertEqual(re.search(r'GPU_BOX_TYPES:-([^}"]+)', self.script).group(1).split(), types)
        self.assertEqual(re.search(r'^SUBNETS="([^"]+)"', self.script, re.M).group(1).split(), subnets)

    RUN_SIDS = ('RunGpuBoxInstanceOnly', 'RunGpuBoxRootVolume', 'RunGpuBoxEni', 'RunGpuBoxReferencedResources')

    def test_every_launch_statement_requires_this_template(self):
        # the role also holds the parallel-box grant; without the pin the two policies'
        # AMIs, subnets, tags and PassRole mix (IAM checks each resource against the union)
        for sid in self.RUN_SIDS:
            with self.subTest(sid=sid):
                run = statement(self.tf, sid)
                self.assertRegex(run, r'"ec2:LaunchTemplate"\s*\n\s*values\s*=\s*local\.gpu_box_template_arns\n')
        pbox = source('parallel-box-launch.tf')
        for sid in ('RunTaggedParallelBoxOnly', 'RunInstancesReferencedResources'):
            with self.subTest(sid=sid):
                self.assertIn('"ec2:LaunchTemplate"', statement(pbox, sid))

    def test_instance_launch_is_tag_type_and_shape_scoped(self):
        run = statement(self.tf, 'RunGpuBoxInstanceOnly')
        for key in ('"aws:RequestTag/Name"', '"aws:TagKeys"', '"ec2:InstanceType"', '"ec2:Tenancy"',
                    '"ec2:MetadataHttpTokens"', '"ec2:InstanceProfile"'):
            self.assertIn(key, run)
        self.assertIn('local.gpu_box_types', run)
        self.assertIn(':instance/*', run)
        self.assertNotIn(':volume/*', run, 'ec2:InstanceType never matches a volume or ENI')

    def test_root_volume_is_the_templates_shape(self):
        vol = statement(self.tf, 'RunGpuBoxRootVolume')
        for key in ('"aws:RequestTag/Name"', '"aws:TagKeys"', '"ec2:VolumeType"', '"ec2:VolumeSize"', '"ec2:Encrypted"'):
            self.assertIn(key, vol)
        self.assertIn(':volume/*', vol)
        eni = statement(self.tf, 'RunGpuBoxEni')
        self.assertIn(':network-interface/*', eni)
        self.assertIn('"aws:TagKeys"', eni)

    def test_referenced_resources_are_pinned(self):
        ref = statement(self.tf, 'RunGpuBoxReferencedResources')
        self.assertIn('image/${var.gpu_box_ami}', ref)
        self.assertIn('local.gpu_box_template_arns', ref)
        self.assertIn('security-group/${aws_security_group.gpu_box.id}', ref)
        self.assertNotRegex(ref, r':(subnet|image|security-group)/\*')

    def test_no_role_passing_and_terminate_only(self):
        self.assertFalse('iam:PassRole' in self.tf, 'the GPU box needs no role, so none may be passed')
        self.assertFalse('iam_instance_profile' in self.tf, 'the GPU box runs without an instance profile')
        down = statement(self.tf, 'TerminateTaggedGpuBoxOnly')
        self.assertIn('"ec2:ResourceTag/Name"', down)
        self.assertFalse('StartInstances' in self.tf, 'down means terminate; nothing to restart')

    def test_grant_goes_to_nextjs_dev_only(self):
        attachments = re.findall(r'role\s*=\s*(aws_iam_role\.[a-z_]+)\.(?:id|name)', self.tf)
        self.assertEqual(attachments, ['aws_iam_role.nextjs_dev'])
        self.assertIn('resource "aws_iam_policy" "gpu_box_control"', self.tf,
                      'managed, not inline: the role\'s inline policies share one 10,240-char limit')

    def test_describe_is_region_scoped(self):
        self.assertIn('"aws:RequestedRegion"', statement(self.tf, 'DescribeForGbox'))

    def test_box_ends_itself_and_tags_everything_it_creates(self):
        self.assertRegex(self.tf, r'instance_initiated_shutdown_behavior\s*=\s*"terminate"')
        self.assertIn('shutdown -h +${var.gpu_box_max_hours * 60}', self.tf)
        for kind in ('instance', 'volume', 'network-interface'):
            self.assertRegex(self.tf, r'resource_type = "' + kind + r'"\s*\n\s*tags = \{\s*\n\s*Name\s*=\s*each\.value')

    def test_ssh_only_from_nextjs_dev_and_no_fleet_egress(self):
        sg = re.search(r'resource "aws_security_group" "gpu_box" \{.*?\n\}', self.tf, re.S).group()
        ingress, egress = sg.split('dynamic "egress"')
        self.assertNotIn('0.0.0.0/0', ingress)
        self.assertIn('aws_eip.nextjs_dev', ingress)
        # web, DNS and NTP only: io-box exports NFS (2049) read-write to us-west-2d, a gbox subnet
        self.assertNotRegex(egress, r'protocol\s*=\s*"-1"')
        self.assertNotIn('2049', egress)
        self.assertEqual(sorted(re.findall(r'\["(?:tcp|udp)", (\d+)\]', egress)), sorted(['443', '80', '53', '53', '123']))

    def test_watchdog_reaps_every_slot_and_enforces_its_lifetime(self):
        watchdog = source('parallel-box-watchdog.tf')
        # what it finds and what it may terminate are one list, and that list holds every slot
        self.assertRegex(watchdog, r'parallel_watchdog_tag_names\s*=\s*concat\(\["parallel-box", "parallel-box-2"\], local\.gpu_box_names\)')
        self.assertRegex(watchdog, r'TAG_NAMES\s*=\s*join\(",", local\.parallel_watchdog_tag_names\)')
        self.assertRegex(watchdog, r'"ec2:ResourceTag/Name"\s*=\s*local\.parallel_watchdog_tag_names\s*\}')
        self.assertRegex(watchdog, r'MAX_AGE_MINUTES\s*=\s*jsonencode\(\{\s*for n in local\.gpu_box_names\s*:\s*n\s*=>\s*var\.gpu_box_max_hours \* 60')
        self.assertIn('terminated_lifetime', watchdog)

    def test_tag_keys_are_per_resource(self):
        self.assertIn('local.gpu_box_tag_keys.instance', statement(self.tf, 'RunGpuBoxInstanceOnly'))
        self.assertIn('local.gpu_box_tag_keys.volume', statement(self.tf, 'RunGpuBoxRootVolume'))
        self.assertIn('local.gpu_box_tag_keys.eni', statement(self.tf, 'RunGpuBoxEni'))

    SLOT_NAMES = ['gpu-box', 'gpu-box-2', 'gpu-box-3', 'gpu-box-4']

    def test_four_slots_and_slot_one_keeps_its_name(self):
        self.assertRegex(self.tf, r'gpu_box_name\s*=\s*"gpu-box"\n')
        self.assertIn('gpu_box_slots = { for n in [1, 2, 3, 4] : tostring(n) => n == 1 ? local.gpu_box_name : "${local.gpu_box_name}-${n}" }', self.tf)
        self.assertEqual(re.search(r'^SLOTS="([^"]+)"', self.script, re.M).group(1).split(), ['1', '2', '3', '4'])
        lt = re.search(r'resource "aws_launch_template" "gpu_box" \{.*?\n\}', self.tf, re.S).group()
        self.assertRegex(lt, r'for_each\s*=\s*local\.gpu_box_slots')
        self.assertRegex(lt, r'\n  name\s*=\s*each\.value')
        # the template that existed before slots becomes slot 1 in place, never a replacement
        self.assertRegex(self.tf, r'moved \{\s*from = aws_launch_template\.gpu_box\s*to\s*= aws_launch_template\.gpu_box\["1"\]\s*\}')

    def test_every_tag_condition_covers_exactly_the_slots(self):
        for sid in ('RunGpuBoxInstanceOnly', 'RunGpuBoxRootVolume', 'RunGpuBoxEni'):
            with self.subTest(sid=sid):
                self.assertRegex(statement(self.tf, sid), r'"aws:RequestTag/Name"\s*\n\s*values\s*=\s*local\.gpu_box_names\n')
        self.assertRegex(statement(self.tf, 'TerminateTaggedGpuBoxOnly'),
                         r'"ec2:ResourceTag/Name"\s*\n\s*values\s*=\s*local\.gpu_box_names\n')

    def test_gbox_is_installed_on_nextjs_dev(self):
        self.assertIn('scripts/gpu-box.sh', source('dev-selfupdate.tf'))
        self.assertIn('${local.gbox_setup}', source('nextjs-user-data.tf'))


class WatchdogHarness(unittest.TestCase):
    """The real parallel-box-watchdog.tf Lambda body against fake boto3: the LaunchTime lifetime
    is the one bound on the GPU box that nothing on the box can switch off."""

    def run_watchdog(self, instances, cpu, terminate_error=None):
        code = re.search(r'parallel_watchdog_code = <<-PY\n(.*?)\n  PY', source('parallel-box-watchdog.tf'), re.S).group(1)
        code = textwrap.dedent(code)
        now = datetime.datetime.now(datetime.timezone.utc)
        calls = {'terminate': [], 'sns': []}

        class EC2:
            def describe_instances(self, Filters):
                return {'Reservations': [{'Instances': [
                    {'InstanceId': iid, 'LaunchTime': now - datetime.timedelta(minutes=age),
                     'Tags': [{'Key': 'Name', 'Value': name}]} for iid, name, age in instances]}]}

            def terminate_instances(self, InstanceIds):
                if terminate_error:
                    raise RuntimeError(terminate_error)
                calls['terminate'].extend(InstanceIds)

        class CW:
            def get_metric_statistics(self, **kwargs):
                peak = cpu[kwargs['Dimensions'][0]['Value']]
                return {'Datapoints': [{'Maximum': peak}, {'Maximum': peak}]}

        class SNS:
            def publish(self, **kwargs):
                calls['sns'].append(kwargs['Subject'])
                calls.setdefault('body', []).append(kwargs['Message'])

        fake = types.ModuleType('boto3')
        fake.client = lambda name, region_name=None: {'ec2': EC2(), 'cloudwatch': CW(), 'sns': SNS()}[name]
        saved_mod, saved_env = sys.modules.get('boto3'), dict(os.environ)
        sys.modules['boto3'] = fake
        slots = GpuBoxTests.SLOT_NAMES
        os.environ.update(TAG_NAMES=','.join(['parallel-box', 'parallel-box-2'] + slots), IDLE_MINUTES='30',
                          MAX_AGE_MINUTES=json.dumps({n: 240 for n in slots}), SNS_TOPIC_ARN='arn:aws:sns:test')
        try:
            env = {}
            exec(compile(code, 'index.py', 'exec'), env)
            result = env['lambda_handler']({}, None)
        finally:
            os.environ.clear()
            os.environ.update(saved_env)
            if saved_mod is None:
                del sys.modules['boto3']
            else:
                sys.modules['boto3'] = saved_mod
        return {r['id']: r['action'] for r in result['results']}, calls

    def test_lifetime_idle_and_busy(self):
        actions, calls = self.run_watchdog(
            [('i-old', 'gpu-box', 250), ('i-idle', 'gpu-box', 45), ('i-young', 'gpu-box', 6), ('i-pbox', 'parallel-box', 600)],
            {'i-old': 90.0, 'i-idle': 1.0, 'i-young': 1.0, 'i-pbox': 90.0})
        self.assertEqual(actions, {'i-old': 'terminated_lifetime', 'i-idle': 'terminated',
                                   'i-young': 'too_young', 'i-pbox': 'busy'})
        self.assertEqual(calls['terminate'], ['i-old', 'i-idle'])

    def test_every_slot_is_lifetime_capped_and_idled_out(self):
        actions, calls = self.run_watchdog(
            [('i-2', 'gpu-box-2', 250), ('i-3', 'gpu-box-3', 45), ('i-4', 'gpu-box-4', 45), ('i-p2', 'parallel-box-2', 45)],
            {'i-2': 90.0, 'i-3': 1.0, 'i-4': 90.0, 'i-p2': 1.0})
        self.assertEqual(actions, {'i-2': 'terminated_lifetime', 'i-3': 'terminated', 'i-4': 'busy', 'i-p2': 'terminated'})
        idle = [b for b in calls['body'] if 'below' in b]
        self.assertTrue(any('gbox up 3\n' in b for b in idle), idle)
        self.assertTrue(any('pbox up 2\n' in b for b in idle), idle)

    def test_failed_terminate_alerts(self):
        actions, calls = self.run_watchdog([('i-old', 'gpu-box', 250), ('i-idle', 'gpu-box', 45)],
                                           {'i-old': 90.0, 'i-idle': 1.0}, terminate_error='protected')
        self.assertEqual(set(actions.values()), {'terminate_failed'})
        self.assertEqual(len([s for s in calls['sns'] if 'FAILED' in s]), 2)


FAKE_AWS = r'''#!PYTHON -I
# A stand-in for the aws CLI: boxes live in $FAKE_STATE/<Name> (one instance id per file),
# and every call is appended to $FAKE_STATE/calls.
import os, sys
state = os.environ['FAKE_STATE']
args = sys.argv[1:]
with open(os.path.join(state, 'calls'), 'a') as f:
    f.write(' '.join(args) + '\n')

def opt(name):
    return args[args.index(name) + 1] if name in args else ''

def boxes(name):
    p = os.path.join(state, name)
    return open(p).read().split() if os.path.exists(p) else []

op = args[1] if len(args) > 1 else ''
if op == 'describe-instances':
    name = next(a.split('Values=', 1)[1] for a in args if a.startswith('Name=tag:Name,'))
    ids, query = boxes(name), opt('--query')
    if 'PublicIpAddress' in query:
        print('198.51.100.%d' % (int(name.rsplit('-', 1)[1]) if name[-1].isdigit() else 1) if ids else 'None')
    elif 'InstanceType' in query:
        print('g4dn.xlarge' if ids else 'None')
    else:
        print('\t'.join(ids))
elif op == 'describe-subnets':
    for s in opt('--subnet-ids').split() or [a for a in args if a.startswith('subnet-')]:
        print('%s\tus-west-2a' % s)
elif op == 'describe-instance-type-offerings':
    print('g4dn.xlarge\tus-west-2a')
elif op == 'run-instances':
    name = opt('--launch-template').split(',')[0].split('=', 1)[1]
    with open(os.path.join(state, name), 'a') as f:
        f.write('i-new-%s\n' % name)
    print('i-new-%s' % name)
elif op == 'terminate-instances':
    gone = args[args.index('--instance-ids') + 1:]
    gone = [g for g in gone if g.startswith('i-')]
    for name in os.listdir(state):
        if name.startswith('gpu-box'):
            keep = [i for i in boxes(name) if i not in gone]
            open(os.path.join(state, name), 'w').write('\n'.join(keep))
'''


class GboxScript(unittest.TestCase):
    """scripts/gpu-box.sh itself, against a fake aws and ssh: each slot's commands reach only
    that slot's Name tag and launch template, and no slot means slot 1, as before."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state, bin_dir, home = root / 'state', root / 'bin', root / 'home'
        for d in (self.state, bin_dir, home / '.ssh'):
            d.mkdir(parents=True)
        (home / '.ssh' / 'dev_hop').write_text('fake key\n')
        (bin_dir / 'aws').write_text(FAKE_AWS.replace('PYTHON', sys.executable, 1))
        (bin_dir / 'ssh').write_text('#!/bin/sh\necho "Tesla T4, 550.00, 15360 MiB"\n')
        (bin_dir / 'sleep').write_text('#!/bin/sh\nexit 0\n')
        for f in ('aws', 'ssh', 'sleep'):
            (bin_dir / f).chmod(0o755)
        self.env = {'PATH': '%s:/usr/bin:/bin' % bin_dir, 'HOME': str(home), 'FAKE_STATE': str(self.state)}

    def tearDown(self):
        self.tmp.cleanup()

    def running(self, **boxes):
        for name, iid in boxes.items():
            (self.state / name.replace('_', '-')).write_text(iid + '\n')

    def gbox(self, *args):
        (self.state / 'calls').write_text('')
        r = subprocess.run(['bash', str(ROOT / 'scripts/gpu-box.sh'), *args], env=self.env,
                           capture_output=True, text=True, timeout=60)
        return r, (self.state / 'calls').read_text().splitlines()

    def test_down_terminates_only_its_own_slot(self):
        self.running(gpu_box='i-111', gpu_box_3='i-333', gpu_box_4='i-444')
        r, calls = self.gbox('down', '3')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([c for c in calls if c.startswith('ec2 terminate-instances')],
                         ['ec2 terminate-instances --region us-west-2 --instance-ids i-333'])
        self.assertIn('Terminating gpu-box-3: i-333', r.stderr)
        r, calls = self.gbox('down')
        self.assertEqual([c for c in calls if c.startswith('ec2 terminate-instances')],
                         ['ec2 terminate-instances --region us-west-2 --instance-ids i-111'])
        self.assertEqual((self.state / 'gpu-box-4').read_text().split(), ['i-444'])

    def test_up_launches_from_its_slots_template_and_passes_no_tags(self):
        r, calls = self.gbox('up', '2')
        self.assertEqual(r.returncode, 0, r.stderr)
        runs = [c for c in calls if c.startswith('ec2 run-instances')]
        self.assertEqual(len(runs), 1, calls)
        self.assertIn('LaunchTemplateName=gpu-box-2,Version=$Latest', runs[0])
        self.assertNotIn('--tag-specifications', runs[0])
        self.assertIn('Ready at 198.51.100.2', r.stderr)
        r, calls = self.gbox('up')
        self.assertIn('LaunchTemplateName=gpu-box,Version=$Latest', [c for c in calls if c.startswith('ec2 run-instances')][0])

    def test_status_ip_and_status_all(self):
        self.running(gpu_box_2='i-222')
        r, _ = self.gbox('status')
        self.assertEqual(r.stdout, 'gpu-box: down ($0)\n')
        r, _ = self.gbox('ip', '2')
        self.assertEqual(r.stdout, '198.51.100.2\n')
        r, _ = self.gbox('status', 'all')
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [l for l in r.stdout.splitlines() if l.startswith('gpu-box')]  # minus the fake ssh's detail
        self.assertEqual(lines, ['gpu-box: down ($0)', 'gpu-box-2: RUNNING at 198.51.100.2 (g4dn.xlarge); the watchdog '
                                 'terminates it at its lifetime or after 30 idle minutes',
                                 'gpu-box-3: down ($0)', 'gpu-box-4: down ($0)'])

    def test_unknown_slot_is_refused_before_any_aws_call(self):
        for args in (('up', '5'), ('down', '0'), ('ssh', 'all'), ('down', 'all')):
            with self.subTest(args=args):
                r, calls = self.gbox(*args)
                self.assertEqual(r.returncode, 1)
                self.assertIn('unknown slot', r.stderr)
                self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
