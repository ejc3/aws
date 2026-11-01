#!/usr/bin/env python3
"""Offline guards for the on-demand GPU test box (gpu-box.tf + scripts/gpu-box.sh).

Live proof is `aws iam simulate-principal-policy` against nextjs-dev-role (allow for a
tagged g4dn.xlarge from a slot's template, deny for an untagged or larger type) plus one
`gbox up` / `gbox down`; these tests keep the source from drifting out of that shape. The
script itself also runs here, against a fake `aws` on PATH, so a slot's commands are shown
to touch only that slot's box.
"""

from pathlib import Path
import base64
import datetime
import gzip
import hashlib
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
        self.assertEqual(re.search(r'GPU_BOX_METAL_TYPES:-([^}"]+)', self.script).group(1).split(),
                         hcl_list(self.tf, 'gpu_box_metal_types'))
        self.assertEqual(re.search(r'^SUBNETS="([^"]+)"', self.script, re.M).group(1).split(), subnets)

    RUN_SIDS = ('RunGpuBoxInstanceOnly', 'RunGpuBoxRootVolume', 'RunGpuBoxEni', 'RunGpuBoxReferencedResources')

    def test_every_launch_statement_requires_this_template(self):
        # the role also holds the parallel-box grant; without the pin the two policies'
        # AMIs, subnets, tags and PassRole mix (IAM checks each resource against the union)
        for sid in self.RUN_SIDS:
            with self.subTest(sid=sid):
                run = statement(self.tf, sid)
                templates = 'gpu_box_standard_template_arns' if sid == 'RunGpuBoxInstanceOnly' else 'gpu_box_template_arns'
                self.assertRegex(run, r'"ec2:LaunchTemplate"\s*\n\s*values\s*=\s*local\.' + templates + r'\n')
        pbox = source('parallel-box-launch.tf')
        for sid in ('RunTaggedParallelBoxOnly', 'RunInstancesReferencedResources'):
            with self.subTest(sid=sid):
                self.assertIn('"ec2:LaunchTemplate"', statement(pbox, sid))

    def test_the_metal_type_launches_only_from_the_metal_slot(self):
        metal = statement(self.tf, 'RunGpuBoxMetalInstanceOnly')
        self.assertRegex(metal, r'"ec2:LaunchTemplate"\s*\n\s*values\s*=\s*\[aws_launch_template\.gpu_box\["metal"\]\.arn\]\n')
        self.assertRegex(metal, r'"aws:RequestTag/Name"\s*\n\s*values\s*=\s*\[local\.gpu_box_slots\["metal"\]\]\n')
        self.assertRegex(metal, r'"ec2:InstanceType"\s*\n\s*values\s*=\s*local\.gpu_box_metal_types\n')
        for key in ('"aws:TagKeys"', '"ec2:Tenancy"', '"ec2:MetadataHttpTokens"', '"ec2:InstanceProfile"'):
            self.assertIn(key, metal)
        self.assertEqual(hcl_list(self.tf, 'gpu_box_metal_types'), ['g4dn.metal'])
        # the small slots' statement never names a metal type
        self.assertFalse(any(t.endswith('.metal') for t in hcl_list(self.tf, 'gpu_box_types')))
        self.assertNotIn('gpu_box_metal_types', statement(self.tf, 'RunGpuBoxInstanceOnly'))

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
        self.assertIn('image/${var.gpu_box_ami}', statement(self.tf, 'RunGpuBoxBrowserImage'))
        self.assertIn('local.gpu_box_template_arns', ref)
        self.assertIn('security-group/${aws_security_group.gpu_box.id}', ref)
        self.assertNotRegex(ref, r':(subnet|image|security-group)/\*')
        self.assertIn(':launch-template/*', ref)
        self.assertRegex(ref, r'test\s*=\s*"ArnEquals"\s*\n\s*variable\s*=\s*"ec2:LaunchTemplate"\s*\n\s*values\s*=\s*local\.gpu_box_template_arns')
        policy = self.tf.split('resource "aws_iam_policy" "gpu_box_control" {', 1)[1].split('\n}', 1)[0]
        self.assertIn('for k, v in s : k => v if k != "Sid"', policy)

    def test_no_role_passing_and_terminate_only(self):
        self.assertFalse('iam:PassRole' in self.tf, 'the GPU box needs no role, so none may be passed')
        self.assertFalse('iam_instance_profile' in self.tf, 'the GPU box runs without an instance profile')
        down = statement(self.tf, 'TerminateTaggedGpuBoxOnly')
        self.assertIn('"ec2:ResourceTag/Name"', down)
        self.assertFalse('StartInstances' in self.tf, 'down means terminate; nothing to restart')

    def test_grant_goes_to_nextjs_dev_and_the_metal_boxes_only(self):
        attachments = re.findall(r'role\s*=\s*(aws_iam_role\.[a-z_]+)\.(?:id|name)', self.tf)
        self.assertEqual(attachments, ['aws_iam_role.nextjs_dev', 'aws_iam_role.dev_server'])
        self.assertIn('resource "aws_iam_policy" "gpu_box_control"', self.tf,
                      'managed, not inline: the role\'s inline policies share one 10,240-char limit')

    def test_describe_is_region_scoped(self):
        self.assertIn('"aws:RequestedRegion"', statement(self.tf, 'DescribeForGbox'))

    def test_box_ends_itself_and_tags_everything_it_creates(self):
        self.assertRegex(self.tf, r'instance_initiated_shutdown_behavior\s*=\s*"terminate"')
        self.assertIn('shutdown -h +${var.gpu_box_max_hours * 60}', self.tf)
        for kind in ('instance', 'volume', 'network-interface'):
            self.assertRegex(self.tf, r'resource_type = "' + kind + r'"\s*\n\s*tags = \{\s*\n\s*Name\s*=\s*each\.value')

    def test_ssh_only_from_the_granted_boxes_and_no_fleet_egress(self):
        sg = re.search(r'resource "aws_security_group" "gpu_box" \{.*?\n\}', self.tf, re.S).group()
        ingress, egress = sg.split('dynamic "egress"')
        self.assertNotIn('0.0.0.0/0', ingress)
        # exactly the boxes holding the grant, each a /32 of its own Elastic IP
        self.assertEqual(re.findall(r'aws_eip\.(\w+) :', ingress), ['nextjs_dev', 'x86_dev'])
        self.assertIn('for i, ip in local.firecracker_dev_public_ips', ingress)
        self.assertIn('cidr_blocks = ["${ingress.value}/32"]', ingress)
        # the group's description is immutable (a change replaces the group under any running gbox)
        self.assertIn('description = "On-demand GPU test box: SSH from nextjs-dev only"', sg)
        # web, DNS and NTP only: io-box exports NFS (2049) read-write to us-west-2d, a gbox subnet
        self.assertNotRegex(egress, r'protocol\s*=\s*"-1"')
        self.assertNotIn('2049', egress)
        self.assertEqual(sorted(re.findall(r'\["(?:tcp|udp)", (\d+)\]', egress)), sorted(['443', '80', '53', '53', '123']))

    def test_watchdog_reaps_every_slot_and_enforces_its_lifetime(self):
        watchdog = source('parallel-box-watchdog.tf')
        # what it finds and what it may terminate are one list, and that list holds every slot
        # the members' boxes join the same list (member-*.tf, tested in test-member-boxes.py)
        self.assertRegex(watchdog, r'parallel_watchdog_tag_names\s*=\s*concat\(local\.parallel_box_names, local\.gpu_box_names[,)]')
        self.assertRegex(watchdog, r'TAG_NAMES\s*=\s*join\(",", local\.parallel_watchdog_tag_names\)')
        self.assertRegex(watchdog, r'"ec2:ResourceTag/Name"\s*=\s*local\.parallel_watchdog_tag_names\s*\}')
        self.assertRegex(watchdog, r'MAX_AGE_MINUTES\s*=\s*jsonencode\(\{\s*for n in concat\(local\.gpu_box_names, local\.member_gbox_names\)\s*:\s*n\s*=>\s*n == local\.gpu_box_slots\["native"\] \? 30 : var\.gpu_box_max_hours \* 60')
        self.assertIn('terminated_lifetime', watchdog)

    def test_tag_keys_are_per_resource(self):
        self.assertIn('local.gpu_box_tag_keys.instance', statement(self.tf, 'RunGpuBoxInstanceOnly'))
        self.assertIn('local.gpu_box_tag_keys.volume', statement(self.tf, 'RunGpuBoxRootVolume'))
        self.assertIn('local.gpu_box_tag_keys.eni', statement(self.tf, 'RunGpuBoxEni'))

    SLOT_NAMES = ['gpu-box', 'gpu-box-2', 'gpu-box-3', 'gpu-box-4', 'gpu-box-metal', 'gpu-box-native']

    def test_four_slots_and_slot_one_keeps_its_name(self):
        self.assertRegex(self.tf, r'gpu_box_name\s*=\s*"gpu-box"\n')
        self.assertIn('{ for n in [1, 2, 3, 4] : tostring(n) => n == 1 ? local.gpu_box_name : "${local.gpu_box_name}-${n}" },', self.tf)
        self.assertIn('{ metal = "${local.gpu_box_name}-metal" },', self.tf)
        self.assertEqual(re.search(r'^SLOTS="([^"]+)"', self.script, re.M).group(1).split(), ['1', '2', '3', '4', 'metal', 'native'])
        lt = re.search(r'resource "aws_launch_template" "gpu_box" \{.*?\n\}', self.tf, re.S).group()
        self.assertRegex(lt, r'for_each\s*=\s*local\.gpu_box_slots')
        self.assertRegex(lt, r'\n  name\s*=\s*each\.value')
        # the template that existed before slots becomes slot 1 in place, never a replacement
        self.assertRegex(self.tf, r'moved \{\s*from = aws_launch_template\.gpu_box\s*to\s*= aws_launch_template\.gpu_box\["1"\]\s*\}')

    def test_every_tag_condition_covers_exactly_the_slots(self):
        for sid in ('RunGpuBoxInstanceOnly', 'RunGpuBoxRootVolume', 'RunGpuBoxEni'):
            with self.subTest(sid=sid):
                names = 'gpu_box_standard_names' if sid == 'RunGpuBoxInstanceOnly' else 'gpu_box_names'
                self.assertRegex(statement(self.tf, sid), r'"aws:RequestTag/Name"\s*\n\s*values\s*=\s*local\.' + names + r'\n')
        self.assertRegex(statement(self.tf, 'TerminateTaggedGpuBoxOnly'),
                         r'"ec2:ResourceTag/Name"\s*\n\s*values\s*=\s*local\.gpu_box_names\n')

    def test_gbox_is_installed_on_nextjs_dev(self):
        self.assertIn('scripts/gpu-box.sh', source('dev-selfupdate.tf'))
        self.assertIn('${local.gbox_setup}', source('nextjs-user-data.tf'))

    def test_native_has_exact_stock_image_type_and_no_cross_slot_image_grant(self):
        image = re.search(r'data "aws_ami" "gpu_box_native" \{.*?\n\}', self.tf, re.S).group()
        self.assertIn('provider    = aws.west2', image)
        self.assertIn('owners      = ["099720109477"]', image)
        self.assertIn('ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*', image)
        run = statement(self.tf, 'RunGpuBoxNativeInstanceOnly')
        for key, values in [('ec2:LaunchTemplate', '[aws_launch_template.gpu_box["native"].arn]'),
                            ('aws:RequestTag/Name', '[local.gpu_box_slots["native"]]'),
                            ('ec2:InstanceType', '["g4dn.xlarge"]'),
                            ('ec2:MetadataHttpTokens', '["required"]'), ('ec2:InstanceProfile', '["true"]')]:
            self.assertRegex(run, r'variable\s*=\s*"' + re.escape(key) + r'"\s*\n\s*values\s*=\s*' + re.escape(values))
        self.assertIn('if k != "native"', self.tf)
        browser = statement(self.tf, 'RunGpuBoxBrowserImage')
        native = statement(self.tf, 'RunGpuBoxNativeImage')
        self.assertIn('local.gpu_box_standard_template_arns', browser)
        self.assertNotIn('gpu_box_native.id', browser)
        self.assertIn('image/${data.aws_ami.gpu_box_native.id}', native)
        self.assertIn('[aws_launch_template.gpu_box["native"].arn]', native)
        self.assertNotIn('image/', statement(self.tf, 'RunGpuBoxReferencedResources'))

    def test_native_arms_lifetime_before_packages_or_ssh_and_keeps_browser_userdata(self):
        native = self.tf.split('base64gzip(<<-NATIVE\n', 1)[1].split('\n  NATIVE', 1)[0]
        timer = native.index('/usr/local/sbin/gpu-box-native-bootstrap --install')
        self.assertLess(timer, native.index('${local.linux_ip_options_bootstrap}'))
        self.assertLess(timer, native.index('/home/ubuntu/.ssh'))
        self.assertIn('file("${path.module}/scripts/gpu-box-native-bootstrap.sh")', native)
        self.assertNotIn('gpu-box-ready', native)
        self.assertNotIn('shutdown -h +', native)
        self.assertIn('shutdown -h +${var.gpu_box_max_hours * 60}', self.tf.split('base64encode(<<-INIT', 1)[1])

    def test_native_userdata_compresses_below_ec2_limit_and_roundtrips_exactly(self):
        native = self.tf.split('base64gzip(<<-NATIVE\n', 1)[1].split('\n  NATIVE', 1)[0]
        bootstrap = (ROOT / 'scripts/gpu-box-native-bootstrap.sh').read_bytes()
        guard = (ROOT / 'scripts/cve-2026-98432-mitigation.py').read_bytes()
        common = source('linux-ip-options.tf')
        setup = common.split('linux_ip_options_setup = <<-SH\n', 1)[1].split('\n  SH', 1)[0]
        setup = setup.replace('${base64gzip(file("${path.module}/scripts/cve-2026-98432-mitigation.py"))}',
                              base64.b64encode(gzip.compress(guard, mtime=0)).decode())
        setup = setup.replace('${filesha256("${path.module}/scripts/cve-2026-98432-mitigation.py")}',
                              hashlib.sha256(guard).hexdigest())
        cold = common.split('linux_ip_options_bootstrap = <<-SH\n', 1)[1].split('\n  SH', 1)[0]
        cold = cold.replace('${local.linux_ip_options_setup}', setup)
        native = native.replace('${base64encode(file("${path.module}/scripts/gpu-box-native-bootstrap.sh"))}',
                                base64.b64encode(bootstrap).decode())
        native = native.replace('${local.linux_ip_options_bootstrap}', cold)
        native = native.replace('${trimspace(tls_private_key.dev_hop.public_key_openssh)}',
                                'ssh-ed25519 ' + base64.b64encode(bytes(51)).decode())
        self.assertNotIn('${', native, 'an unrendered input would hide part of EC2 userdata')
        original = textwrap.dedent(native).encode()
        delivered = gzip.compress(original, mtime=0)
        self.assertLessEqual(len(delivered), 16384)
        self.assertEqual(gzip.decompress(delivered), original)


class WatchdogHarness(unittest.TestCase):
    """The real parallel-box-watchdog.tf Lambda body against fake boto3: the LaunchTime lifetime
    is the one bound on the GPU box that nothing on the box can switch off."""

    def run_watchdog(self, instances, cpu, terminate_error=None, io=None, io_thresholds=None, volumes=None):
        code = re.search(r'parallel_watchdog_code = <<-PY\n(.*?)\n  PY', source('parallel-box-watchdog.tf'), re.S).group(1)
        code = textwrap.dedent(code)
        now = datetime.datetime.now(datetime.timezone.utc)
        calls = {'terminate': [], 'sns': [], 'metrics': []}
        io = io or {}
        volumes = volumes or {}

        class EC2:
            def describe_instances(self, Filters):
                return {'Reservations': [{'Instances': [
                    {'InstanceId': iid, 'LaunchTime': now - datetime.timedelta(minutes=age),
                     'Tags': [{'Key': 'Name', 'Value': name}] + [{'Key': k, 'Value': v} for k, v in extra],
                     'BlockDeviceMappings': [{'DeviceName': '/dev/sd%s' % chr(97 + n), 'Ebs': {'VolumeId': vol}}
                                             for n, vol in enumerate(volumes.get(iid, []))]}
                    for iid, name, age, *more in instances for extra in [more[0] if more else {}.items()]]}]}

            def terminate_instances(self, InstanceIds):
                if terminate_error:
                    raise RuntimeError(terminate_error)
                calls['terminate'].extend(InstanceIds)

        class CW:
            def get_metric_statistics(self, **kwargs):
                iid, metric = kwargs['Dimensions'][0]['Value'], kwargs['MetricName']
                calls['metrics'].append((iid, metric, tuple(kwargs['Statistics'])))
                if kwargs['Namespace'] == 'AWS/EBS':
                    # A volume's series: windows are keyed by timestamp so the code can sum volumes per window.
                    assert kwargs['Dimensions'][0]['Name'] == 'VolumeId' and kwargs['Period'] == 300
                    return {'Datapoints': [{'Timestamp': n, 'Sum': v} for n, v in enumerate(io.get(iid, {}).get(metric, []))]}
                assert kwargs['Namespace'] == 'AWS/EC2' and kwargs['Dimensions'][0]['Name'] == 'InstanceId'
                if metric == 'CPUUtilization':
                    peak = cpu[iid]
                    return {'Datapoints': [{'Maximum': peak}, {'Maximum': peak}]}
                # Five-minute byte sums per window; a metric not given has no series at all.
                return {'Datapoints': [{'Sum': v} for v in io.get(iid, {}).get(metric, [])]}

        class SNS:
            def publish(self, **kwargs):
                calls['sns'].append(kwargs['Subject'])
                calls.setdefault('body', []).append(kwargs['Message'])

        fake = types.ModuleType('boto3')
        fake.client = lambda name, region_name=None: {'ec2': EC2(), 'cloudwatch': CW(), 'sns': SNS()}[name]
        saved_mod, saved_env = sys.modules.get('boto3'), dict(os.environ)
        sys.modules['boto3'] = fake
        slots = GpuBoxTests.SLOT_NAMES
        os.environ.update(TAG_NAMES=','.join(['parallel-box'] + ['parallel-box-%d' % n for n in range(2, 9)] + ['pbox-colton'] + slots), IDLE_MINUTES='30',
                          MAX_AGE_MINUTES=json.dumps({n: 30 if n == 'gpu-box-native' else 240 for n in slots}),
                          SINGLE_NAMES='gpu-box-native', SNS_TOPIC_ARN='arn:aws:sns:test')
        if io_thresholds is not None:
            os.environ['IO_THRESHOLDS'] = json.dumps(io_thresholds)
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

    def test_native_busy_lifetime_and_duplicate_do_not_affect_existing_slots(self):
        actions, calls = self.run_watchdog(
            [('i-native', 'gpu-box-native', 30), ('i-browser', 'gpu-box', 31), ('i-metal', 'gpu-box-metal', 31)],
            {'i-native': 100.0, 'i-browser': 90.0, 'i-metal': 90.0})
        self.assertEqual(actions, {'i-native': 'terminated_lifetime', 'i-browser': 'busy', 'i-metal': 'busy'})
        self.assertEqual(calls['terminate'], ['i-native'])
        self.assertNotIn('i-native', [iid for iid, _, _ in calls['metrics']])
        actions, calls = self.run_watchdog(
            [('i-older', 'gpu-box-native', 10), ('i-newer', 'gpu-box-native', 5)], {})
        self.assertEqual(actions, {'i-older': 'too_young', 'i-newer': 'terminated_duplicate'})
        self.assertEqual(calls['terminate'], ['i-newer'])
        self.assertIn('concat(local.member_gbox_names, [local.gpu_box_slots["native"]])', source('parallel-box-watchdog.tf'))

    def test_every_slot_is_lifetime_capped_and_idled_out(self):
        actions, calls = self.run_watchdog(
            [('i-2', 'gpu-box-2', 250), ('i-3', 'gpu-box-3', 45), ('i-4', 'gpu-box-4', 45), ('i-p2', 'parallel-box-2', 45),
             ('i-p3', 'parallel-box-3', 45), ('i-p1', 'parallel-box', 45), ('i-p8', 'parallel-box-8', 45),
             ('i-mc', 'pbox-colton', 45)],
            {'i-2': 90.0, 'i-3': 1.0, 'i-4': 90.0, 'i-p2': 1.0, 'i-p3': 1.0, 'i-p1': 1.0, 'i-p8': 1.0, 'i-mc': 1.0})
        self.assertEqual(actions, {'i-2': 'terminated_lifetime', 'i-3': 'terminated', 'i-4': 'busy', 'i-p2': 'terminated',
                                   'i-p3': 'terminated', 'i-p1': 'terminated', 'i-p8': 'terminated', 'i-mc': 'terminated'})
        idle = [b for b in calls['body'] if 'below' in b]
        self.assertTrue(any('gbox up 3\n' in b for b in idle), idle)
        # each parallel box's alert names ITS slot: the restart command selects that slot's work volume
        for cmd in ('pbox up 2 FLAVOR\n', 'pbox up 3 FLAVOR\n', 'pbox up 8 FLAVOR\n', 'pbox up FLAVOR\n'):
            self.assertTrue(any(cmd in b for b in idle), (cmd, idle))
        # a member's pbox-<person> is not a slot: its notice names the person, never a slot number
        member = [b for b in idle if 'i-mc' in b][0]
        self.assertIn('pbox up FLAVOR   (run by colton)\n', member)

    def test_a_fallback_boxs_notice_says_its_fresh_volume_is_gone(self):
        # pbox-fallback.tf's boxes run on a fresh volume deleted with them: "untouched" would send the owner looking.
        _, calls = self.run_watchdog([('i-fb', 'parallel-box-2', 45, {'Fallback': 'true'}.items()),
                                      ('i-home', 'parallel-box-3', 45)], {'i-fb': 1.0, 'i-home': 1.0})
        fb = [b for b in calls['body'] if 'i-fb' in b][0]
        home = [b for b in calls['body'] if 'i-home' in b][0]
        self.assertIn('fresh work volume was deleted with it', fb)
        self.assertNotIn('Its work volume is untouched', fb)
        self.assertIn('Its work volume is untouched', home)

    # The thresholds Terraform passes, decoded from parallel-box-watchdog.tf's jsonencode (#532).
    @staticmethod
    def io_thresholds():
        body = re.search(r'parallel_watchdog_io_thresholds = jsonencode\(\{\n(.*?)\n  \}\)', source('parallel-box-watchdog.tf'), re.S).group(1)
        out = {}
        for line in body.splitlines():
            name, expr = [x.strip() for x in line.split('=')]
            a, b = [int(x) for x in expr.split('*')]
            out[name] = a * b
        return out

    MIB = 1048576
    # Measured 2026-10-19 on Ohio parallel boxes the old watchdog reaped as idle (MiB per five minutes, the 30-minute
    # window before each reap): the first two were idle, the rest were working under 5% CPU.
    IDLE_EBS = [64.4, 62.9, 68.4, 77.5, 74.6, 80.2]           # i-03ca1cbd42d5ba817, 0.0% CPU
    IDLE_EBS_AFTER_JOB = [164.7, 39.7, 69.4, 47.8, 9.6, 8.0]  # i-0bc2905f612dcf33c, a job ended just before the window
    STORAGE_JOB = [10688.9, 10810.8, 10902.9, 10854.6, 8124.3, 8346.3]  # i-0ce383ccaec76e537 instance store, 5-9% CPU
    EBS_JOB = [1684.9, 25.9, 43.2, 41.7, 41.5, 19.1]          # i-05b6ecccaa04b9fa8, 1.3% CPU
    NET_JOB = [181.6, 0.0, 0.1, 0.0, 0.0, 0.0]                # i-04e12eff452c3ff63 NetworkIn, 2.6% CPU

    def test_the_thresholds_sit_between_the_measured_idle_floor_and_real_work(self):
        t = self.io_thresholds()
        self.assertEqual(sorted(t), ['DiskReadBytes', 'DiskWriteBytes', 'EBSReadBytes', 'EBSWriteBytes', 'NetworkIn', 'NetworkOut'])
        for disk in ('DiskReadBytes', 'DiskWriteBytes', 'EBSReadBytes', 'EBSWriteBytes'):
            self.assertGreater(t[disk], max(self.IDLE_EBS + self.IDLE_EBS_AFTER_JOB) * self.MIB)
            self.assertLess(t[disk], min(max(self.STORAGE_JOB), max(self.EBS_JOB)) * self.MIB)
        for net in ('NetworkIn', 'NetworkOut'):
            self.assertGreater(t[net], 3 * self.MIB)
            self.assertLess(t[net], max(self.NET_JOB) * self.MIB)

    def test_disk_or_network_work_under_the_cpu_bar_keeps_the_box(self):
        mib = lambda xs: [x * self.MIB for x in xs]
        actions, calls = self.run_watchdog(
            [('i-idle', 'parallel-box', 45), ('i-after', 'parallel-box-2', 45), ('i-nvme', 'parallel-box-3', 45),
             ('i-ebs', 'parallel-box-4', 45), ('i-net', 'pbox-colton', 45), ('i-gpu', 'gpu-box-2', 45),
             ('i-cpu', 'parallel-box-5', 45)],
            {'i-idle': 0.0, 'i-after': 1.4, 'i-nvme': 4.9, 'i-ebs': 1.3, 'i-net': 2.6, 'i-gpu': 0.5, 'i-cpu': 50.0},
            io={'i-idle': {'EBSWriteBytes': mib(self.IDLE_EBS), 'NetworkIn': mib([0.1] * 6)},
                'i-after': {'EBSWriteBytes': mib(self.IDLE_EBS_AFTER_JOB)},
                'i-nvme': {'DiskWriteBytes': mib(self.STORAGE_JOB), 'EBSWriteBytes': mib(self.IDLE_EBS)},
                'i-ebs': {'EBSWriteBytes': mib(self.EBS_JOB)},
                'i-net': {'NetworkIn': mib(self.NET_JOB)},
                'i-gpu': {'NetworkOut': mib([70.0, 0, 0, 0, 0, 0])}},
            io_thresholds=self.io_thresholds())
        self.assertEqual(actions, {'i-idle': 'terminated', 'i-after': 'terminated', 'i-nvme': 'busy_io', 'i-ebs': 'busy_io',
                                   'i-net': 'busy_io', 'i-gpu': 'busy_io', 'i-cpu': 'busy'})
        self.assertEqual(calls['terminate'], ['i-idle', 'i-after'])
        idle = [b for b in calls['body'] if 'i-idle' in b][0]
        self.assertIn('and under its disk and network thresholds and was terminated', idle)
        # Byte metrics are five-minute sums; a box over the CPU bar is never asked about I/O.
        self.assertTrue(all(st == ('Sum',) for _, m, st in calls['metrics'] if m != 'CPUUtilization'))
        self.assertEqual([m for i, m, _ in calls['metrics'] if i == 'i-cpu'], ['CPUUtilization'])

    def test_missing_io_series_are_no_activity_and_missing_cpu_still_never_reaps(self):
        # An EBS-only type has no DiskRead/WriteBytes series at all: that must not keep it alive or crash the check.
        actions, calls = self.run_watchdog([('i-1', 'parallel-box', 45)], {'i-1': 0.0}, io={},
                                           io_thresholds=self.io_thresholds())
        self.assertEqual(actions, {'i-1': 'terminated'})
        self.assertEqual(len([m for _, m, _ in calls['metrics'] if m != 'CPUUtilization']), 6)

    def test_a_bare_metal_box_with_no_instance_ebs_series_reads_its_volumes(self):
        # kvm-96/kvm-192 are .metal types: AWS documents EBSRead/WriteBytes only for Nitro instances that are not bare
        # metal. The attached volumes' AWS/EBS series stand in, summed per window across the volumes.
        mib = lambda xs: [x * self.MIB for x in xs]
        actions, calls = self.run_watchdog(
            [('i-kvm', 'parallel-box', 45), ('i-split', 'parallel-box-2', 45), ('i-quiet', 'parallel-box-3', 45),
             ('i-novol', 'parallel-box-4', 45), ('i-nitro', 'parallel-box-5', 45)],
            {'i-kvm': 1.0, 'i-split': 1.0, 'i-quiet': 0.0, 'i-novol': 0.0, 'i-nitro': 0.0},
            io={'vol-root': {'VolumeWriteBytes': mib([70.0] * 6)}, 'vol-work': {'VolumeWriteBytes': mib(self.EBS_JOB)},
                # 300 + 300 MiB in one window on two volumes: over 512 only when summed
                'vol-a': {'VolumeReadBytes': mib([300.0, 10.0])}, 'vol-b': {'VolumeReadBytes': mib([300.0, 10.0])},
                'vol-q': {'VolumeWriteBytes': mib(self.IDLE_EBS)},
                # a non-metal box publishes the instance series (idle here): its volumes are never asked
                'i-nitro': {'EBSWriteBytes': mib(self.IDLE_EBS), 'EBSReadBytes': mib([0.0] * 6)},
                'vol-n': {'VolumeWriteBytes': mib(self.EBS_JOB)}},
            io_thresholds=self.io_thresholds(),
            volumes={'i-kvm': ['vol-root', 'vol-work'], 'i-split': ['vol-a', 'vol-b'], 'i-quiet': ['vol-q'],
                     'i-nitro': ['vol-n']})
        self.assertEqual(actions, {'i-kvm': 'busy_io', 'i-split': 'busy_io', 'i-quiet': 'terminated',
                                   'i-novol': 'terminated', 'i-nitro': 'terminated'})
        self.assertNotIn('vol-n', [i for i, _, _ in calls['metrics']])
        self.assertTrue(all(st == ('Sum',) for i, _, st in calls['metrics'] if i.startswith('vol-')))

    def test_without_thresholds_it_is_cpu_alone(self):
        # The members' Windows boxes pass none (member-wbox.tf): their behaviour and their metric calls are unchanged.
        actions, calls = self.run_watchdog([('i-1', 'parallel-box', 45)], {'i-1': 0.0},
                                           io={'i-1': {'EBSWriteBytes': [10 ** 12]}})
        self.assertEqual(actions, {'i-1': 'terminated'})
        self.assertEqual([m for _, m, _ in calls['metrics']], ['CPUUtilization'])
        self.assertNotIn('thresholds', calls['body'][0])

    def test_every_parallel_watchdog_passes_the_thresholds_and_the_windows_one_does_not(self):
        threshold = 'IO_THRESHOLDS = local.parallel_watchdog_io_thresholds'
        fn = re.search(r'resource "aws_lambda_function" "parallel_watchdog" \{.*?\n\}', source('parallel-box-watchdog.tf'), re.S).group(0)
        ohio = re.search(r'resource "aws_lambda_function" "parallel_watchdog_ohio" \{.*?\n\}', source('ohio-pbox.tf'), re.S).group(0)
        fallback = re.search(r'pbox_fallback_watchdog = \{.*?\n  \}', source('pbox-fallback.tf'), re.S).group(0)
        for body in (fn, ohio, fallback):
            self.assertRegex(body, r'IO_THRESHOLDS\s+= local\.parallel_watchdog_io_thresholds')
        self.assertNotIn('IO_THRESHOLDS', source('member-wbox.tf'))

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
    for s in [a for a in args if a.startswith('subnet-')]:
        print('%s\tus-west-2a' % s)
elif op == 'describe-instance-type-offerings':
    print('g4dn.xlarge\tus-west-2a')
    print('g4dn.metal\tus-west-2a')
elif op == 'run-instances':
    name = opt('--launch-template').split(',')[0].split('=', 1)[1]
    if name == 'gpu-box-native' and os.environ.get('FAKE_NATIVE_NO_CAPACITY') == '1':
        print('InsufficientInstanceCapacity')
        sys.exit(255)
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

    def test_up_metal_launches_the_metal_template_as_g4dn_metal(self):
        r, calls = self.gbox('up', 'metal')
        self.assertEqual(r.returncode, 0, r.stderr)
        runs = [c for c in calls if c.startswith('ec2 run-instances')]
        self.assertEqual(len(runs), 1, calls)
        self.assertIn('LaunchTemplateName=gpu-box-metal,Version=$Latest', runs[0])
        self.assertIn('--instance-type g4dn.metal', runs[0])
        self.assertNotIn('xlarge', runs[0])
        r, calls = self.gbox('down', 'metal')
        self.assertEqual([c for c in calls if c.startswith('ec2 terminate-instances')],
                         ['ec2 terminate-instances --region us-west-2 --instance-ids i-new-gpu-box-metal'])

    def test_native_ignores_type_overrides_and_uses_only_its_own_template(self):
        self.env['GPU_BOX_TYPES'] = 'g6.xlarge g4dn.metal'
        self.env['GPU_BOX_METAL_TYPES'] = 'g5.xlarge'
        result, calls = self.gbox('up', 'native')
        self.assertEqual(result.returncode, 0, result.stderr)
        runs = [call for call in calls if call.startswith('ec2 run-instances')]
        self.assertEqual(len(runs), 1)
        self.assertIn('LaunchTemplateName=gpu-box-native,Version=$Latest', runs[0])
        self.assertIn('--instance-type g4dn.xlarge ', runs[0])
        self.assertNotIn('--iam-instance-profile', runs[0])
        result, calls = self.gbox('down', 'native')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call for call in calls if call.startswith('ec2 terminate-instances')],
                         ['ec2 terminate-instances --region us-west-2 --instance-ids i-new-gpu-box-native'])

    def test_member_cannot_select_native_before_any_aws_call(self):
        self.env['MEMBER_BOX_USER'] = 'colton'
        result, calls = self.gbox('up', 'native')
        self.assertEqual(result.returncode, 1)
        self.assertIn('with no slot', result.stderr)
        self.assertEqual(calls, [])

    def test_native_capacity_failure_never_falls_back_to_another_type(self):
        self.env.update(GPU_BOX_TYPES='g5.xlarge g6.xlarge', FAKE_NATIVE_NO_CAPACITY='1')
        result, calls = self.gbox('up', 'native')
        self.assertEqual(result.returncode, 1)
        runs = [call for call in calls if call.startswith('ec2 run-instances')]
        self.assertEqual(len(runs), 4)
        self.assertTrue(all('--instance-type g4dn.xlarge ' in call for call in runs))
        self.assertTrue(all('LaunchTemplateName=gpu-box-native,Version=$Latest' in call for call in runs))

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
                                 'gpu-box-3: down ($0)', 'gpu-box-4: down ($0)', 'gpu-box-metal: down ($0)',
                                 'gpu-box-native: down ($0)'])

    def test_unknown_slot_is_refused_before_any_aws_call(self):
        for args in (('up', '5'), ('down', '0'), ('ssh', 'all'), ('down', 'all')):
            with self.subTest(args=args):
                r, calls = self.gbox(*args)
                self.assertEqual(r.returncode, 1)
                self.assertIn('unknown slot', r.stderr)
                self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main(buffer=True)
