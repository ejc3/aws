#!/usr/bin/env python3
"""Offline guards for the on-demand GPU test box (gpu-box.tf + scripts/gpu-box.sh).

Live proof is `aws iam simulate-principal-policy` against nextjs-dev-role (allow for a
tagged g4dn.xlarge from the template, deny for an untagged or larger type) plus one
`gbox up` / `gbox down`; these tests keep the source from drifting out of that shape.
"""

from pathlib import Path
import datetime
import json
import os
import re
import sys
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
                self.assertRegex(run, r'"ec2:LaunchTemplate"\s*\n\s*values\s*=\s*\[aws_launch_template\.gpu_box\.arn\]')
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
        self.assertIn('aws_launch_template.gpu_box.arn', ref)
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
            self.assertRegex(self.tf, r'resource_type = "' + kind + r'"\s*\n\s*tags = \{\s*\n\s*Name\s*=\s*local\.gpu_box_name')

    def test_ssh_only_from_nextjs_dev_and_no_fleet_egress(self):
        sg = re.search(r'resource "aws_security_group" "gpu_box" \{.*?\n\}', self.tf, re.S).group()
        ingress, egress = sg.split('dynamic "egress"')
        self.assertNotIn('0.0.0.0/0', ingress)
        self.assertIn('aws_eip.nextjs_dev', ingress)
        # web, DNS and NTP only: io-box exports NFS (2049) read-write to 172.31.0.0/16
        self.assertNotRegex(egress, r'protocol\s*=\s*"-1"')
        self.assertNotIn('2049', egress)
        self.assertEqual(sorted(re.findall(r'\["(?:tcp|udp)", (\d+)\]', egress)), sorted(['443', '80', '53', '53', '123']))

    def test_watchdog_reaps_the_gpu_box_and_enforces_its_lifetime(self):
        watchdog = source('parallel-box-watchdog.tf')
        self.assertRegex(watchdog, r'TAG_NAMES\s*=\s*"[^"]*\bgpu-box\b')
        self.assertRegex(watchdog, r'"ec2:ResourceTag/Name"\s*=\s*\[[^\]]*"gpu-box"')
        self.assertRegex(watchdog, r'MAX_AGE_MINUTES\s*=\s*jsonencode\(\{\s*"gpu-box"\s*=\s*var\.gpu_box_max_hours \* 60')
        self.assertIn('terminated_lifetime', watchdog)

    def test_tag_keys_are_per_resource(self):
        self.assertIn('local.gpu_box_tag_keys.instance', statement(self.tf, 'RunGpuBoxInstanceOnly'))
        self.assertIn('local.gpu_box_tag_keys.volume', statement(self.tf, 'RunGpuBoxRootVolume'))
        self.assertIn('local.gpu_box_tag_keys.eni', statement(self.tf, 'RunGpuBoxEni'))

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

        fake = types.ModuleType('boto3')
        fake.client = lambda name, region_name=None: {'ec2': EC2(), 'cloudwatch': CW(), 'sns': SNS()}[name]
        saved_mod, saved_env = sys.modules.get('boto3'), dict(os.environ)
        sys.modules['boto3'] = fake
        os.environ.update(TAG_NAMES='parallel-box,parallel-box-2,gpu-box', IDLE_MINUTES='30',
                          MAX_AGE_MINUTES=json.dumps({'gpu-box': 240}), SNS_TOPIC_ARN='arn:aws:sns:test')
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

    def test_failed_terminate_alerts(self):
        actions, calls = self.run_watchdog([('i-old', 'gpu-box', 250), ('i-idle', 'gpu-box', 45)],
                                           {'i-old': 90.0, 'i-idle': 1.0}, terminate_error='protected')
        self.assertEqual(set(actions.values()), {'terminate_failed'})
        self.assertEqual(len([s for s in calls['sns'] if 'FAILED' in s]), 2)


if __name__ == '__main__':
    unittest.main()
