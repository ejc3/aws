"""Credential-free tests of real controller/broker paths and durable crash seams."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import triage as T

SPEC = importlib.util.spec_from_file_location('patchdoctor_control', Path(__file__).with_name('control.py'))
C = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(C)
ACCOUNT = '111111111111'
REGION = 'us-west-1'
INSTANCE = 'i-' + '1' * 17
COMMAND = '11111111-1111-1111-1111-111111111111'
ENV = {'ACCOUNT_ID': ACCOUNT, 'TABLE_NAME': 'ledger', 'PROJECT_NAME': 'patchdoctor', 'BUCKET_NAME': 'private-audit',
    'SNS_TOPIC_ARN': 'test-topic', 'ALLOWED_REGIONS_JSON': json.dumps([REGION, 'us-west-2', 'us-east-1']),
    'TARGET_NAMES_JSON': json.dumps(['jumpbox', 'nextjs-ct']), 'COMMAND_DOCUMENT_NAME': 'PatchDoctor-RunCommand',
    'INSPECT_DOCUMENT_NAME': 'PatchDoctor-Inspect',
    'CONTROL_ARN': 'arn:aws:lambda:us-west-1:111111111111:function:patchdoctor'}


class Memory:
    def __init__(self):
        self.rows = {}

    def get(self, key):
        return copy.deepcopy(self.rows.get(key))

    def put(self, key, value):
        old = self.rows.get(key, {})
        if old.get('_revision', 0) != value.get('_revision', 0):
            error = RuntimeError('conditional write conflict')
            error.response = {'Error': {'Code': 'ConditionalCheckFailedException'}}
            raise error
        value['_revision'] = value.get('_revision', 0) + 1
        self.rows[key] = copy.deepcopy(value)
        return value

    def cases(self):
        return [copy.deepcopy(v) for k, v in self.rows.items() if k.startswith('case#')
            and (v.get('status') in C.ACTIVE_CASES or v.get('outstanding_operations'))]


class Cloud:
    def __init__(self):
        self.finding = {'findingArn': 'arn:aws:inspector2:' + REGION + ':' + ACCOUNT + ':finding/one',
            'awsAccountId': ACCOUNT, 'severity': 'CRITICAL', 'status': 'ACTIVE', 'fixAvailable': 'YES',
            'packageVulnerabilityDetails': {'vulnerabilityId': 'CVE-2026-12345',
                'sourceUrl': 'https://vendor.example/CVE-2026-12345',
                'vulnerablePackages': [{'name': 'example-package', 'fixedInVersion': '2.0', 'version': '1.0'}]},
            'resources': [{'type': 'AWS_EC2_INSTANCE', 'id': INSTANCE, 'region': REGION}]}
        self.instance = {'InstanceId': INSTANCE, 'State': {'Name': 'running'}, 'Tags': [{'Key': 'Name', 'Value': 'jumpbox'}]}
        self.builds, self.starts, self.stops, self.sends, self.messages, self.objects = {}, [], [], [], [], {}
        self.lose_start = self.lose_send = self.fail_audit = False
        self.reject_start = False
        self.invocation_status = 'Success'
        self.shub = None
        self.extra_findings, self.extra_instances = [], []
        self.invokes, self.fail_invoke = [], False

    def client(self, service, region):
        if service not in {'inspector2', 'securityhub', 'ec2', 'codebuild', 'sns', 'ssm', 's3', 'lambda'}:
            raise AssertionError('unmocked service: ' + service)
        return self

    def list_findings(self, **kw):
        filters = kw['filterCriteria']
        rows = copy.deepcopy(([self.finding] if self.finding else []) + self.extra_findings)
        for key, rows_filter in filters.items():
            wanted = rows_filter[0]['value']
            if key == 'findingArn':
                rows = [f for f in rows if f['findingArn'] == wanted]
            elif key == 'findingStatus':
                rows = [f for f in rows if f['status'] == wanted]
            elif key == 'severity':
                rows = [f for f in rows if f['severity'] == wanted]
            elif key == 'vulnerabilityId':
                rows = [f for f in rows if f.get('packageVulnerabilityDetails', {}).get('vulnerabilityId') == wanted]
        return {'findings': rows}

    def get_findings(self, **kw):
        return {'Findings': [copy.deepcopy(self.shub)] if self.shub else []}

    def describe_instances(self, **kw):
        wanted = kw['Filters'][0]['Values']
        return {'Reservations': [{'OwnerId': ACCOUNT, 'Instances': copy.deepcopy(
            [i for i in [self.instance, *self.extra_instances] if i['InstanceId'] in wanted])}]}

    def publish(self, **kw):
        self.messages.append(kw)
        return {'MessageId': 'one'}

    def invoke(self, **kw):
        self.invokes.append(kw)
        if self.fail_invoke:
            raise TimeoutError('controller wake delivery unknown')
        return {'StatusCode': 202}

    def start_build(self, **kw):
        self.starts.append(kw)
        number = len(self.starts)
        if self.reject_start:
            error = RuntimeError('definitive rejection with private SDK detail')
            error.response = {'Error': {'Code': 'AccessDeniedException'}}
            raise error
        value = {'id': 'patchdoctor:' + str(number), 'projectName': 'patchdoctor', 'buildStatus': 'IN_PROGRESS',
            'environment': {'environmentVariables': copy.deepcopy(kw['environmentVariablesOverride'])}}
        if getattr(self, 'worker_revision', None):
            value['environment']['environmentVariables'].append({'name': 'PATCHDOCTOR_SELF_TEST_REVISION',
                'value': self.worker_revision, 'type': 'PLAINTEXT'})
        self.builds[value['id']] = value
        if self.lose_start:
            self.lose_start = False
            raise TimeoutError('lost StartBuild reply')
        return {'build': copy.deepcopy(value)}

    def batch_get_builds(self, **kw):
        return {'builds': [copy.deepcopy(self.builds[b]) for b in kw['ids'] if b in self.builds]}

    def list_builds_for_project(self, **kw):
        return {'ids': list(self.builds)}

    def stop_build(self, **kw):
        self.stops.append(kw)
        return {}

    def send_command(self, **kw):
        self.sends.append(kw)
        if self.lose_send:
            self.lose_send = False
            raise TimeoutError('lost SendCommand reply')
        return {'Command': {'CommandId': COMMAND}}

    def list_commands(self, **kw):
        return {'Commands': [{'CommandId': COMMAND, 'Comment': c['Comment'], 'InstanceIds': c['InstanceIds'],
            'DocumentName': c['DocumentName']} for c in self.sends]}

    def get_command_invocation(self, **kw):
        assert kw == {'CommandId': COMMAND, 'InstanceId': INSTANCE}
        return {'Status': self.invocation_status, 'ResponseCode': 0, 'StandardOutputContent': 'patched', 'StandardErrorContent': ''}

    def put_object(self, **kw):
        if self.fail_audit:
            raise TimeoutError('audit unavailable')
        self.objects[kw['Key']] = kw['Body']
        return {'VersionId': 'audit-version'}

    def head_object(self, **kw):
        assert kw['ExpectedBucketOwner'] == ACCOUNT
        return {'VersionId': kw['VersionId'], 'ContentLength': 10, 'ServerSideEncryption': 'aws:kms'}


def durable_json(path, value):
    temporary = path.with_suffix('.new')
    with temporary.open('w') as output:
        json.dump(value, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


class FileMemory(Memory):
    def __init__(self, directory):
        super().__init__()
        self.path = directory / 'ddb.json'
        self.rows = json.loads(self.path.read_text()) if self.path.exists() else {}

    def put(self, key, value):
        super().put(key, value)
        durable_json(self.path, self.rows)
        return value


class FileCloud(Cloud):
    def __init__(self, directory):
        super().__init__()
        self.path = directory / 'cloud.json'
        if self.path.exists():
            value = json.loads(self.path.read_text())
            for key in ('builds', 'starts', 'sends'):
                setattr(self, key, value[key])
            self.objects = {k: v.encode() for k, v in value['objects'].items()}

    def persist(self):
        durable_json(self.path, {**{k: getattr(self, k) for k in ('builds', 'starts', 'sends')},
            'objects': {k: v.decode() for k, v in self.objects.items()}})

    def start_build(self, **kw):
        value = super().start_build(**kw)
        self.persist()
        return value

    def send_command(self, **kw):
        value = super().send_command(**kw)
        self.persist()
        return value

    def put_object(self, **kw):
        value = super().put_object(**kw)
        self.persist()
        return value


def crash_child(mode, directory):
    # This branch only invokes actual control methods with file-backed fake APIs.
    import socket
    socket.socket = lambda *a, **kw: (_ for _ in ()).throw(AssertionError('network forbidden'))
    os.umask(0o077)
    memory, cloud = FileMemory(directory), FileCloud(directory)
    doctor = C.Doctor(ENV, memory, cloud.client, lambda: 1793538000)
    if mode != 'recover':
        memory.put('config', {'armed': True, 'gate_version': 2})
    event = {'source': 'aws.inspector2', 'detail-type': 'Inspector2 Finding', 'account': ACCOUNT,
        'region': REGION, 'detail': {'findingArn': cloud.finding['findingArn']}}
    case_id = doctor.handler(event)['admitted'][0]
    case = memory.get('case#' + case_id)
    command = {'action': 'command', 'case_id': case_id, 'build_id': case['build_id'],
        'target': {'instance_id': INSTANCE, 'region': REGION}, 'operation_id': 'durable-patch', 'script': 'true'}
    reply = doctor.broker(command)
    if mode == 'recover':
        assert len(cloud.starts) == len(cloud.sends) == 1
        assert reply['command_id'] == COMMAND
        poll = doctor.broker({**command, 'action': 'poll'})
        assert poll['state'] == 'terminal' and len(cloud.objects) == 2
        print(json.dumps({'recovered': True, 'starts': 1, 'commands': 1}), flush=True)
        return
    assert reply['command_id'] == COMMAND
    # Actual handler/broker have ACKed; all fake service state is already fsynced.
    print('ACK', flush=True)
    if mode == 'hard':
        os._exit(23)  # Deliberately bypasses every shutdown/atexit handler.
    signal.pause()  # Parent sends SIGTERM only to this owned child.


class Tests(unittest.TestCase):
    def setUp(self):
        self.store, self.cloud = Memory(), Cloud()
        self.clock = 1793538000
        self.doctor = C.Doctor(ENV, self.store, self.cloud.client, lambda: self.clock)
        self.capabilities = {}
        self.event = {'source': 'aws.inspector2', 'detail-type': 'Inspector2 Finding', 'account': ACCOUNT,
            'region': REGION, 'detail': {'findingArn': self.cloud.finding['findingArn'], 'severity': 'CRITICAL'}}
        class Model:
            def __init__(self):
                self.calls, self.decision, self.category = [], 'run_doctor', 'needs_investigation'
            def converse(self, **kwargs):
                self.calls.append(kwargs)
                return {'stopReason': 'end_turn', 'usage': {'inputTokens': 10, 'outputTokens': 12},
                    'output': {'message': {'role': 'assistant', 'content': [{'text': json.dumps({
                        'decision': self.decision, 'category': self.category, 'confidence': 0.99, 'reason': 'fake-only'})}]}}}
        self.model = Model()
        self.model_patch = patch.object(T, '_client', return_value=self.model)
        self.model_patch.start()
        self.addCleanup(self.model_patch.stop)
        self.vendor_patch = patch.object(T, 'fetch_vendor', side_effect=AssertionError('real vendor network forbidden'))
        self.vendor_patch.start()
        self.addCleanup(self.vendor_patch.stop)

    def admit(self, armed=True):
        if armed:
            self.store.put('config', {'armed': True, 'gate_version': 2})
        result = self.doctor.handler(self.event)
        return self.store.get('case#' + result['admitted'][0])

    def request(self, case, action, **fields):
        if action in {'checkpoint', 'finish'} and hasattr(self.doctor, 'worker_claim'):
            identity = case['case_id'], case['build_id']
            if identity not in self.capabilities:
                self.capabilities[identity] = self.doctor.broker({
                    'case_id': identity[0], 'build_id': identity[1], 'action': 'worker_claim'})['worker_capability']
            fields.setdefault('worker_capability', self.capabilities[identity])
        return {'case_id': case['case_id'], 'build_id': case['build_id'], 'action': action, **fields}

    def command(self, case, **fields):
        return self.request(case, 'command', target={'instance_id': INSTANCE, 'region': REGION},
            operation_id='patch-version-1', script=fields.pop('script', 'true'), **fields)

    def native_no_fix(self):
        self.cloud.instance['Architecture'] = 'arm64'
        finding = self.cloud.finding
        finding.update(fixAvailable='NO', exploitAvailable='NO')
        finding['resources'][0]['details'] = {'awsEc2Instance': {'platform': 'UBUNTU_24_04'}}
        finding['packageVulnerabilityDetails'].update(source='UBUNTU_CVE', vulnerablePackages=[{
            'name': 'example-package', 'version': '1.0', 'release': '0ubuntu1', 'epoch': 0,
            'arch': 'ARM64', 'packageManager': 'OS', 'fixedInVersion': ''}])
        purl = 'pkg:deb/ubuntu/example-package@1.0-0ubuntu1?arch=arm64&distro=noble'
        body = {'author': 'Canonical Ltd.', '@id': 'https://github.com/canonical/ubuntu-security-notices/blob/main/vex/cve/2026/CVE-2026-12345',
            'statements': [{'vulnerability': {'name': 'CVE-2026-12345'}, 'products': [{'@id': purl}],
                'status': 'not_affected', 'justification': 'vulnerable_code_not_present'}]}
        self.model.decision, self.model.category = 'defer', 'not_applicable'
        return T.VendorEvidence('CVE-2026-12345', __import__('time').time(), json.dumps(body).encode())

    def test_native_purl_deferral_is_durable_cached_and_hourly_rechecked(self):
        vendor = self.native_no_fix()
        with patch.object(T, 'fetch_vendor', return_value=vendor):
            case = self.admit()
            self.assertEqual(case['status'], 'deferred')
            self.assertEqual(self.cloud.starts, [])
            self.assertEqual(self.doctor.triage_input(case)['packages'][0]['purl'],
                'pkg:deb/ubuntu/example-package@1.0-0ubuntu1?arch=arm64&distro=noble')
            self.doctor = C.Doctor(ENV, self.store, self.cloud.client, lambda: self.clock)
            self.doctor.reconcile()
            self.assertEqual(len(self.model.calls), 1)
            self.clock += 3300
            self.doctor.reconcile()
            self.assertEqual(len(self.model.calls), 2)
        self.assertEqual(sum(m['Subject'] == 'PatchDoctor deferred-recheck' for m in self.cloud.messages), 2)
        self.assertEqual(self.cloud.finding['status'], 'ACTIVE')
        self.assertEqual(self.cloud.sends, [])

    def test_due_evidence_recheck_runs_before_busy_and_unknown_host_operation_fences(self):
        for fence in ('busy-build', 'unknown-host-operation'):
            with self.subTest(fence=fence):
                self.store, self.cloud = Memory(), Cloud()
                self.doctor = C.Doctor(ENV, self.store, self.cloud.client, lambda: self.clock)
                self.model.calls.clear()
                vendor = self.native_no_fix()
                with patch.object(T, 'fetch_vendor', return_value=vendor):
                    deferred = self.admit()
                    other = copy.deepcopy(self.cloud.finding)
                    other['findingArn'] += '-other'
                    other['fixAvailable'] = 'YES'
                    other['packageVulnerabilityDetails']['vulnerabilityId'] = 'CVE-2026-54321'
                    self.cloud.extra_findings = [other]
                    other_id = self.doctor.handler({**self.event, 'detail': {'findingArn': other['findingArn']}})['admitted'][0]
                    repair = self.store.get('case#' + other_id)
                    self.assertEqual(len(self.cloud.starts), 1)
                    if fence == 'unknown-host-operation':
                        self.cloud.lose_send = True
                        self.doctor.broker(self.command(repair))
                        repair = self.store.get('case#' + other_id)
                        repair.update(attempts=2, reserved_usd=50)
                        self.doctor.save(repair)
                        self.cloud.builds[repair['build_id']]['buildStatus'] = 'FAILED'
                    sends = len(self.cloud.sends)
                    self.clock += 3300
                    with patch.object(self.cloud, 'list_commands', return_value={'Commands': []}):
                        self.doctor.reconcile()
                checked = self.store.get('case#' + deferred['case_id'])
                self.assertEqual(len(self.model.calls), 2, 'due evidence must be checked even while another repair is fenced')
                self.assertEqual(checked['triage_checked_at'], self.clock)
                self.assertEqual(checked['status'], 'deferred')
                self.assertEqual(len(self.cloud.starts), 1)
                self.assertEqual(len(self.cloud.sends), sends)
                if fence == 'unknown-host-operation':
                    self.assertEqual(self.store.get('host-operation')['case_id'], repair['case_id'])

    def test_recheck_effect_survives_lost_ack_and_later_recheck_even_after_worker_start(self):
        for start_after_second in (False, True):
            with self.subTest(start_after_second=start_after_second):
                self.store, self.cloud = Memory(), Cloud()
                self.doctor = C.Doctor(ENV, self.store, self.cloud.client, lambda: self.clock)
                self.model.calls.clear()
                vendor = self.native_no_fix()
                with patch.object(T, 'fetch_vendor', return_value=vendor):
                    case = self.admit()
                    self.doctor.handler({'action': 'pause_new_runs', 'paused': True, 'mutationId': 'hold'})
                    first = {'action': 'recheck', 'case_id': case['case_id'], 'mutationId': 'recheck-A'}
                    original, failed = self.store.put, []
                    def lose_final_ack(key, value):
                        if key == 'admin#' + C.digest(first['mutationId']) and value.get('state') == 'completed' and not failed:
                            failed.append(True)
                            raise OSError('lost first final acknowledgement')
                        return original(key, value)
                    self.store.put = lose_final_ack
                    with self.assertRaises(OSError):
                        self.doctor.handler(first)
                    if start_after_second:
                        self.model.decision, self.model.category = 'run_doctor', 'needs_investigation'
                        self.doctor.handler({'action': 'pause_new_runs', 'paused': False, 'mutationId': 'resume'})
                    self.doctor.handler({**first, 'mutationId': 'recheck-B'})
                    before = self.store.get('case#' + case['case_id'])
                    calls, starts = len(self.model.calls), len(self.cloud.starts)
                    try:
                        replay = self.doctor.handler(first)
                    except ValueError:
                        replay = {'state': 'refused'}
                    self.assertEqual(replay, {'action': 'recheck', 'mutationId': 'recheck-A', 'state': 'completed', 'case_count': 1})
                    self.assertEqual(self.store.get('case#' + case['case_id']), before)
                    self.assertEqual((len(self.model.calls), len(self.cloud.starts)), (calls, starts))
                    self.assertEqual(starts, int(start_after_second))
                    for mutation in ('recheck-A', 'recheck-B'):
                        self.assertTrue(before['recheck_effects'][C.digest(mutation)])

    def test_changed_native_exploit_bypasses_cached_deferral_immediately(self):
        vendor = self.native_no_fix()
        with patch.object(T, 'fetch_vendor', return_value=vendor):
            case = self.admit()
            self.assertEqual(case['status'], 'deferred')
            self.cloud.finding['exploitAvailable'] = 'YES'
            self.doctor.handler(self.event)
        current = self.store.get('case#' + case['case_id'])
        self.assertEqual(current['status'], 'running')
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(len(self.cloud.starts), 1)

    def test_native_cache_is_stable_when_inventory_order_changes(self):
        vendor = self.native_no_fix()
        other = copy.deepcopy(self.cloud.finding)
        other['findingArn'] += '-other'
        other['packageVulnerabilityDetails']['vulnerablePackages'][0]['name'] = 'other-package'
        self.cloud.extra_findings = [other]
        document = json.loads(vendor.body)
        document['statements'][0]['products'].append({'@id':
            document['statements'][0]['products'][0]['@id'].replace('example-package', 'other-package')})
        vendor = T.VendorEvidence(vendor.cve, vendor.fetched_at, json.dumps(document).encode())
        with patch.object(T, 'fetch_vendor', return_value=vendor):
            first = self.admit()
            self.cloud.finding, self.cloud.extra_findings[0] = self.cloud.extra_findings[0], self.cloud.finding
            second = self.doctor.handler({**self.event, 'detail': {'findingArn': self.cloud.finding['findingArn']}})
        self.assertEqual(second['admitted'], [first['case_id']])
        self.assertEqual(len(self.model.calls), 1)

    def test_unknown_custom_truncated_or_wrong_arch_native_facts_start_opus(self):
        for variant in ['source', 'platform', 'arch', 'custom', 'truncated', 'missing']:
            with self.subTest(variant=variant):
                self.store, self.cloud = Memory(), Cloud()
                self.doctor = C.Doctor(ENV, self.store, self.cloud.client, lambda: self.clock)
                self.model.calls.clear()
                vendor = self.native_no_fix()
                package = self.cloud.finding['packageVulnerabilityDetails']['vulnerablePackages'][0]
                if variant == 'source': self.cloud.finding['packageVulnerabilityDetails']['source'] = 'OTHER'
                if variant == 'platform': self.cloud.finding['resources'][0]['details']['awsEc2Instance']['platform'] = 'UNKNOWN'
                if variant == 'arch': package['arch'] = 'X86_64'
                if variant == 'custom': package['version'] = '7.0-fcvm-custom'
                if variant == 'truncated': self.cloud.finding['packageVulnerabilityDetails']['vulnerablePackages'] *= 9
                if variant == 'missing': package.pop('epoch')
                with patch.object(T, 'fetch_vendor', side_effect=AssertionError('unknown facts must not fetch')):
                    case = self.admit()
                self.assertEqual(case['status'], 'running')
                self.assertEqual(len(self.model.calls), 0)

    def test_old_armed_config_cannot_bypass_two_model_commissioning_gate(self):
        self.store.put('config', {'armed': True})
        self.doctor.handler(self.event)
        self.assertFalse(self.doctor.config()['armed'])
        self.assertEqual(self.cloud.starts, [])

    def test_admin_self_test_is_real_haiku_then_opus_and_arm_unpauses(self):
        result = self.doctor.handler({'action': 'self_test', 'mutationId': 'commission-once'})
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.model.calls[0]['modelId'], T.MODEL)
        self.assertEqual(self.store.get('config')['haiku_self_test']['model'], T.MODEL)
        case = self.store.get('case#' + result['case_id'])
        with self.assertRaises(ValueError):
            self.doctor.handler({'action': 'arm', 'mutationId': 'not-ready'})
        self.doctor.broker(self.request(case, 'finish', outcome='self-test-passed', summary='actual fixture'))
        self.cloud.builds[case['build_id']]['buildStatus'] = 'SUCCEEDED'
        self.doctor.reconcile()
        self.doctor.handler({'action': 'pause_new_runs', 'paused': True, 'mutationId': 'paused'})
        armed = self.doctor.handler({'action': 'arm', 'mutationId': 'enable'})
        self.assertTrue(armed['armed'])
        self.assertFalse(armed['paused'])
        self.assertEqual(self.cloud.sends, [])

    def test_worker_revision_gets_fresh_commissioning_without_resetting_old_budget(self):
        old_revision, new_revision = 'a' * 64, 'b' * 64
        self.cloud.worker_revision = old_revision
        self.doctor = C.Doctor({**ENV, 'SELF_TEST_REVISION': old_revision}, self.store, self.cloud.client, lambda: self.clock)
        old = self.doctor.handler({'action': 'self_test', 'mutationId': 'runtime-A'})
        self.assertEqual(old['case_id'], C.digest('PatchDoctor/SELF_TEST/' + old_revision))
        for _ in range(2):
            case = self.store.get('case#' + old['case_id'])
            self.cloud.builds[case['build_id']]['buildStatus'] = 'FAILED'
            self.doctor.reconcile()
        previous = self.store.get('case#' + old['case_id'])
        self.assertEqual((previous['status'], previous['attempts'], previous['reserved_usd']), ('budget-exhausted', 2, 50))
        config = self.store.get('config')
        config.update(armed=True, gate_version=2, commissioned_worker_revision=old_revision)
        self.store.put('config', config)
        self.cloud.worker_revision = new_revision
        self.doctor = C.Doctor({**ENV, 'SELF_TEST_REVISION': new_revision}, self.store, self.cloud.client, lambda: self.clock)
        self.assertFalse(self.doctor.config()['armed'])
        result = self.doctor.handler({'action': 'self_test', 'mutationId': 'runtime-B'})
        self.assertNotEqual(result['case_id'], old['case_id'])
        self.assertEqual(self.doctor.status()['self_test_case_id'], result['case_id'])
        case = self.store.get('case#' + result['case_id'])
        self.assertEqual((case['worker_revision'], case['attempts'], case['reserved_usd']), (new_revision, 1, 25))
        self.assertEqual(self.store.get('case#' + old['case_id']), previous)
        with self.assertRaises(ValueError):
            self.doctor.handler({'action': 'arm', 'mutationId': 'old-proof-cannot-arm-B'})
        self.doctor.broker(self.request(case, 'finish', outcome='self-test-passed', summary='current runtime proof'))
        self.cloud.builds[case['build_id']]['buildStatus'] = 'SUCCEEDED'
        self.doctor.reconcile()
        self.assertTrue(self.doctor.handler({'action': 'arm', 'mutationId': 'arm-B'})['armed'])
        self.assertEqual(self.store.get('config')['commissioned_worker_revision'], new_revision)
        self.doctor.handler({'action': 'self_test', 'mutationId': 'same-runtime-B'})
        daily = self.store.get('daily#2026-11-01')
        self.assertEqual((len(self.cloud.starts), daily['starts']), (3, 3))

    def test_worker_revision_keeps_daily_limit_and_does_not_start_old_pending_self_test(self):
        old_revision, current = 'a' * 64, 'b' * 64
        old_id = C.digest('PatchDoctor/SELF_TEST/' + old_revision)
        old = {'case_id': old_id, 'account': ACCOUNT, 'region': REGION, 'cve': 'SELF_TEST', 'mode': 'self-test',
            'status': 'pending', 'created_at': self.clock - 1, 'attempts': 1, 'reserved_usd': 25,
            'operations': {}, 'targets': [], 'findings': [], 'worker_revision': old_revision}
        self.doctor.save(old)
        # Actual start() crash seam: pointer is saved before pending becomes starting.
        self.store.put('active-case', {'case_id': old_id})
        self.store.put('daily#2026-11-01', {'starts': 4})
        self.cloud.worker_revision = current
        self.doctor = C.Doctor({**ENV, 'SELF_TEST_REVISION': current}, self.store, self.cloud.client, lambda: self.clock)
        result = self.doctor.handler({'action': 'self_test', 'mutationId': 'budget-preserved'})
        self.assertEqual(result['case_id'], C.digest('PatchDoctor/SELF_TEST/' + current))
        fresh = self.store.get('case#' + result['case_id'])
        self.assertEqual((fresh['status'], fresh['attempts'], fresh['reserved_usd']), ('daily-budget-wait', 0, 0))
        self.assertEqual(self.store.get('case#' + old_id), old)
        self.assertEqual((self.store.get('daily#2026-11-01')['starts'], self.cloud.starts), (4, []))
        self.assertIsNone(self.store.get('active-case')['case_id'])
        self.clock += 86400
        self.doctor.reconcile()
        self.assertEqual(self.store.get('case#' + result['case_id'])['status'], 'running')
        self.assertEqual(len(self.cloud.starts), 1)
        self.assertEqual(self.store.get('active-case')['case_id'], result['case_id'])
        self.assertEqual(self.store.get('case#' + old_id), old)

    def test_previous_revision_success_cannot_leave_new_revision_armed(self):
        old_revision = 'a' * 64
        self.cloud.worker_revision = old_revision
        self.doctor = C.Doctor({**ENV, 'SELF_TEST_REVISION': old_revision}, self.store, self.cloud.client, lambda: self.clock)
        result = self.doctor.handler({'action': 'self_test', 'mutationId': 'passed-runtime-A'})
        case = self.store.get('case#' + result['case_id'])
        self.doctor.broker(self.request(case, 'finish', outcome='self-test-passed', summary='runtime A proof'))
        self.cloud.builds[case['build_id']]['buildStatus'] = 'SUCCEEDED'
        self.doctor.reconcile()
        self.doctor.handler({'action': 'arm', 'mutationId': 'arm-runtime-A'})
        self.doctor = C.Doctor({**ENV, 'SELF_TEST_REVISION': 'b' * 64}, self.store, self.cloud.client, lambda: self.clock)
        self.assertFalse(self.doctor.config()['armed'])
        with self.assertRaises(ValueError):
            self.doctor.handler({'action': 'arm', 'mutationId': 'cannot-arm-runtime-B'})
        self.assertEqual(self.store.get('case#' + case['case_id'])['status'], 'self-test-passed')
        self.assertEqual((len(self.cloud.starts), self.store.get('daily#2026-11-01')['starts']), (1, 1))

    def test_revisioned_self_test_broker_requires_its_actual_build_revision_not_current_revision(self):
        revision = 'a' * 64
        self.cloud.worker_revision = revision
        result = self.doctor.handler({'action': 'self_test', 'mutationId': 'broker-revision'})
        case = self.store.get('case#' + result['case_id'])
        case['worker_revision'] = revision
        self.doctor.save(case)
        variables = self.cloud.builds[case['build_id']]['environment']['environmentVariables']
        variable = next(v for v in variables if v['name'] == 'PATCHDOCTOR_SELF_TEST_REVISION')
        variable['value'] = 'b' * 64
        event = self.request(case, 'get')
        with self.assertRaises(ValueError):
            self.doctor.broker(event)
        variable['value'] = revision
        self.doctor = C.Doctor({**ENV, 'SELF_TEST_REVISION': 'b' * 64}, self.store, self.cloud.client, lambda: self.clock)
        self.assertEqual(self.doctor.broker(event)['case_id'], case['case_id'])

    def test_failed_haiku_self_test_cannot_enqueue_opus_or_arm(self):
        with patch.object(T, '_client', side_effect=TimeoutError('private detail')):
            result = self.doctor.handler({'action': 'self_test', 'mutationId': 'bad-model'})
        self.assertFalse(result['haiku']['ok'])
        self.assertEqual(self.cloud.starts, [])
        self.assertNotIn('private detail', json.dumps(result))
        with self.assertRaises(ValueError):
            self.doctor.handler({'action': 'arm', 'mutationId': 'bad-arm'})

    def test_admin_mutation_replay_conflict_and_lost_ack_do_not_repeat_model_or_build(self):
        event = {'action': 'self_test', 'mutationId': 'same-intent'}
        original, failed = self.store.put, []
        def lost_ack(key, value):
            if key.startswith('admin#') and value.get('state') == 'completed' and not failed:
                failed.append(True)
                raise OSError('lost final receipt')
            return original(key, value)
        self.store.put = lost_ack
        with self.assertRaises(OSError):
            self.doctor.handler(event)
        replay = self.doctor.handler(event)
        self.assertEqual(replay['state'], 'completed')
        self.assertEqual(self.doctor.handler(event), replay)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(len(self.cloud.starts), 1)
        with self.assertRaises(ValueError):
            self.doctor.handler({'action': 'pause_new_runs', 'paused': True, 'mutationId': 'same-intent'})

    def test_pause_effect_marker_recovers_before_ack_without_reapplying(self):
        original, failed = self.store.put, []
        def lost_ack(key, value):
            if key.startswith('admin#') and value.get('state') == 'completed' and not failed:
                failed.append(True); raise OSError('lost pause ACK')
            return original(key, value)
        self.store.put = lost_ack
        event = {'action': 'pause_new_runs', 'paused': True, 'mutationId': 'pause-once'}
        with self.assertRaises(OSError): self.doctor.handler(event)
        revision = self.store.get('config')['_revision']
        replay = self.doctor.handler(event)
        self.assertTrue(replay['paused'])
        self.assertEqual(self.store.get('config')['_revision'], revision)
        self.doctor.handler(self.event)
        self.assertEqual(self.cloud.starts, [])
        with self.assertRaises(ValueError): self.doctor.handler({**event, 'paused': False})

    def test_manual_recheck_is_durable_idempotent_and_respects_pause(self):
        vendor = self.native_no_fix()
        with patch.object(T, 'fetch_vendor', return_value=vendor): case = self.admit()
        self.doctor.handler({'action': 'pause_new_runs', 'paused': True, 'mutationId': 'hold'})
        event = {'action': 'recheck', 'case_id': case['case_id'], 'mutationId': 'check-once'}
        first = self.doctor.handler(event)
        self.assertEqual(first['case_count'], 1)
        revision = self.store.get('case#' + case['case_id'])['_revision']
        self.assertEqual(self.doctor.handler(event), first)
        self.assertEqual(self.store.get('case#' + case['case_id'])['_revision'], revision)
        self.assertEqual(self.cloud.starts, [])

    def test_status_whitelist_never_exposes_capability_or_private_receipts(self):
        case = self.admit()
        case.update(worker_capability_hash='SECRET-CAPABILITY', summary='PRIVATE OUTPUT')
        self.doctor.save(case)
        result = self.doctor.handler({'action': 'status'})
        raw = json.dumps(result)
        self.assertNotIn('SECRET', raw)
        self.assertNotIn('PRIVATE OUTPUT', raw)
        self.assertTrue(result['connected'])
        self.assertFalse(any(result['setup'].values()))
        self.assertEqual(result['models']['triage'], T.MODEL)

    def test_finish_conflict_reloads_only_outcome_and_never_retries_other_errors(self):
        case = self.admit()
        event = self.request(case, 'finish', outcome='patched', summary='checked installed package')
        original = self.store.put
        conflicts = []
        def concurrent_write(key, value):
            if key == 'case#' + case['case_id'] and value.get('outcome') and not conflicts:
                other = self.store.get(key)
                other['operations']['concurrent'] = 'op#preserved'
                original(key, other)
                conflicts.append(True)
            return original(key, value)
        self.store.put = concurrent_write
        try:
            result = self.doctor.broker(event)
        except Exception:
            result = {'acknowledged': False}
        self.assertIs(result.get('acknowledged'), True)
        current = self.store.get('case#' + case['case_id'])
        self.assertEqual(current['outcome'], 'patched')
        self.assertEqual(current['operations']['concurrent'], 'op#preserved')
        with patch.object(self.store, 'put', side_effect=OSError('storage unavailable')) as writes:
            with self.assertRaises(OSError):
                self.doctor.broker(event)
            self.assertEqual(writes.call_count, 1)

    def test_parent_capability_is_one_time_build_bound_and_not_in_model_get(self):
        case = self.admit()
        raw = {'case_id': case['case_id'], 'build_id': case['build_id']}
        key = 'cases/' + case['case_id'] + '/builds/' + C.digest(case['build_id']) + '/checkpoints/one.tar.gz'
        for fields in ({'action': 'checkpoint', 'key': key, 'version_id': 'one'},
                {'action': 'finish', 'outcome': 'patched', 'summary': 'bypass'}):
            denied = False
            try:
                self.doctor.broker({**raw, **fields})
            except ValueError:
                denied = True
            self.assertTrue(denied, 'model identity alone must not bypass parent proof')
        first = self.doctor.broker({**raw, 'action': 'worker_claim'})['worker_capability']
        self.assertRegex(first, r'^[a-f0-9]{64}$')
        stored = self.store.get('case#' + case['case_id'])
        self.assertNotIn(first, json.dumps(stored))
        self.assertEqual(stored['worker_capability_hash'], C.digest(first))
        exposed = self.doctor.broker({**raw, 'action': 'get'})
        self.assertNotIn(first, json.dumps(exposed))
        self.assertNotIn(C.digest(first), json.dumps(exposed))
        with self.assertRaises(ValueError):
            self.doctor.broker({**raw, 'action': 'worker_claim'})
        with self.assertRaises(ValueError):
            self.doctor.broker({**raw, 'action': 'finish', 'outcome': 'patched', 'worker_capability': '0' * 64})
        self.cloud.builds[case['build_id']]['buildStatus'] = 'FAILED'
        self.doctor.reconcile()
        current = self.store.get('case#' + case['case_id'])
        newer = {'case_id': current['case_id'], 'build_id': current['build_id']}
        with self.assertRaises(ValueError):
            self.doctor.broker({**newer, 'action': 'finish', 'outcome': 'patched', 'worker_capability': first})
        second = self.doctor.broker({**newer, 'action': 'worker_claim'})['worker_capability']
        self.assertNotEqual(first, second)
        self.assertTrue(self.doctor.broker({**newer, 'action': 'finish', 'outcome': 'patched',
            'worker_capability': second})['acknowledged'])

    def test_unknown_send_search_is_scoped_and_never_released_by_age(self):
        case = self.admit()
        self.cloud.lose_send = True
        self.doctor.broker(self.command(case))
        self.cloud.builds[case['build_id']]['buildStatus'] = 'FAILED'
        self.clock += 7 * 86400
        with patch.object(self.cloud, 'list_commands', return_value={'Commands': []}) as search:
            self.doctor.reconcile()
            self.doctor.reconcile()
            args = search.call_args.kwargs
            self.assertEqual(args.get('InstanceId'), INSTANCE)
            self.assertIn({'key': 'DocumentName', 'value': 'PatchDoctor-RunCommand'}, args['Filters'])
        op = self.store.get(self.doctor.operation(case, 'patch-version-1'))
        self.assertEqual(op['state'], 'send-uncertain')
        self.assertNotIn('receipt_key', op)
        self.assertEqual(self.store.get('host-operation')['case_id'], case['case_id'])
        self.assertEqual(sum(m['Subject'] == 'PatchDoctor command-delivery-uncertain' for m in self.cloud.messages), 1)
        with patch.object(self.cloud, 'list_commands', side_effect=OSError('observer unavailable')):
            with self.assertRaises(OSError):
                self.doctor.reconcile()  # An active unknown write still fails closed.

    def test_unrelated_historical_observer_failure_does_not_block_new_critical_case(self):
        case = self.admit()
        self.doctor.broker(self.command(case, inspect=True, script=''))
        old = self.store.get('case#' + case['case_id'])
        old.update(status='budget-exhausted', attempts=2, reserved_usd=50)
        self.doctor.save(old)
        self.cloud.builds[case['build_id']]['buildStatus'] = 'FAILED'
        self.doctor.release('active-case', case['case_id'])
        self.cloud.finding['packageVulnerabilityDetails']['vulnerablePackages'][0]['fixedInVersion'] = '9.0'
        reply = None
        with patch.object(self.cloud, 'get_command_invocation', side_effect=OSError('PRIVATE_OBSERVER_CANARY')):
            try:
                reply = self.doctor.handler(self.event)
            except OSError:
                pass
        self.assertIsNotNone(reply, 'an unrelated old observer must not stop new critical admission')
        new = self.store.get('case#' + reply['admitted'][0])
        self.assertEqual(new['status'], 'running')
        self.assertEqual(len(self.cloud.starts), 2)
        self.assertTrue(self.store.get('case#' + case['case_id'])['outstanding_operations'])
        self.assertNotIn('PRIVATE_OBSERVER_CANARY', json.dumps(self.cloud.messages))

    def test_separate_admission_durable_ack_only_wakes_fixed_controller(self):
        records = {'Records': [{'messageId': 'native', 'body': json.dumps({**self.event, 'action': 'arm'})},
            {'messageId': 'forged-admin', 'body': json.dumps({'action': 'arm'})}]}
        try:
            with patch.object(self.doctor, 'reconcile', side_effect=AssertionError('admission must not schedule builds')):
                reply = self.doctor.admission(records)
        except AttributeError:
            reply = {'admitted': []}
        self.assertEqual(len(reply['admitted']), 1)
        self.assertEqual(reply['batchItemFailures'], [{'itemIdentifier': 'forged-admin'}])
        self.assertFalse(self.doctor.config().get('armed'))
        self.assertEqual(self.cloud.starts, [])
        self.assertEqual(self.cloud.sends, [])
        call = self.cloud.invokes[0]
        self.assertEqual((call['FunctionName'], call['InvocationType']), (ENV['CONTROL_ARN'], 'Event'))
        self.assertEqual(json.loads(call['Payload']), {'source': 'patchdoctor.admission', 'detail-type': 'Pending critical cases'})
        self.assertEqual(self.store.get('case#' + reply['admitted'][0])['status'], 'pending')
        self.cloud.fail_invoke = True
        retried = self.doctor.admission({'Records': [records['Records'][0]]})
        self.assertEqual(retried['batchItemFailures'], [{'itemIdentifier': 'native'}])
        self.assertEqual(len([k for k in self.store.rows if k.startswith('case#')]), 1)

    def test_checkpoint_conflict_reloads_only_checkpoint_and_preserves_command_index(self):
        case = self.admit()
        event = self.request(case, 'checkpoint', key='cases/' + case['case_id'] + '/builds/'
            + C.digest(case['build_id']) + '/checkpoints/immutable.tar.gz', version_id='version-one')
        original = self.store.put
        conflicts = []
        def concurrent_write(key, value):
            if key == 'case#' + case['case_id'] and value.get('checkpoint') and not conflicts:
                other = self.store.get(key)
                other['operations']['concurrent'] = 'op#durably-registered'
                other['outstanding_operations'] = ['concurrent']
                other['outcome'] = 'patched'
                original(key, other)
                conflicts.append(True)
            return original(key, value)
        self.store.put = concurrent_write
        with patch.object(self.cloud, 'head_object', wraps=self.cloud.head_object) as head:
            try:
                result = self.doctor.broker(event)
            except Exception:
                result = {'acknowledged': False}
            self.assertIs(result.get('acknowledged'), True)
            self.assertEqual(head.call_count, 1)
        current = self.store.get('case#' + case['case_id'])
        self.assertEqual(current['operations']['concurrent'], 'op#durably-registered')
        self.assertEqual(current['outcome'], 'patched')
        self.assertEqual(current['checkpoint']['version_id'], 'version-one')

    def test_terminal_worker_operations_reconcile_outside_deadline_and_fence_new_mutations(self):
        case = self.admit()
        self.cloud.lose_send = True
        self.cloud.invocation_status = 'InProgress'
        self.doctor.broker(self.command(case))
        with self.assertRaises(ValueError):
            self.doctor.broker(self.request(case, 'command', target={'instance_id': INSTANCE, 'region': REGION},
                operation_id='second-mutation', script='true'))
        latest = self.store.get('case#' + case['case_id'])
        latest.update(attempts=2, reserved_usd=50)
        self.doctor.save(latest)
        self.cloud.builds[case['build_id']]['buildStatus'] = 'FAILED'
        self.clock += 4000
        self.doctor.reconcile()
        key = C.Doctor.operation(self.doctor, case, 'patch-version-1')
        self.assertNotEqual(self.store.get(key)['state'], 'terminal')
        self.cloud.finding['packageVulnerabilityDetails']['vulnerablePackages'][0]['fixedInVersion'] = '3.0'
        other = self.store.get('case#' + self.doctor.handler(self.event)['admitted'][0])
        self.assertEqual(other['status'], 'pending')
        self.assertNotIn('build_id', other)
        self.assertEqual(len(self.cloud.starts), 1, 'no new model reservation while the old command is uncertain')
        self.assertEqual(len(self.cloud.sends), 1)
        self.cloud.invocation_status = 'Success'
        self.doctor.reconcile()
        op = self.store.get(key)
        self.assertEqual(op['state'], 'terminal')
        self.assertIn(op['receipt_key'], self.cloud.objects)
        self.assertEqual(self.store.get('case#' + case['case_id']).get('outstanding_operations'), [])
        self.assertFalse((self.store.get('host-operation') or {}).get('case_id'))
        other = self.store.get('case#' + other['case_id'])
        self.assertEqual(len(self.cloud.starts), 2)
        self.assertEqual(other['status'], 'running')
        self.doctor.broker(self.command(other))
        self.assertEqual(len(self.cloud.sends), 2)

    def test_terminal_cleanup_recovers_both_crash_cuts_without_ssm_or_rearchive(self):
        class Cut(BaseException):
            pass
        for cut in ('after-terminal-write', 'after-index-clear'):
            with self.subTest(cut=cut):
                memory, cloud = Memory(), Cloud()
                doctor = C.Doctor(ENV, memory, cloud.client, lambda: self.clock)
                memory.put('config', {'armed': True, 'gate_version': 2})
                case_id = doctor.handler(self.event)['admitted'][0]
                case = memory.get('case#' + case_id)
                cloud.fail_audit = True
                with self.assertRaises(TimeoutError):
                    doctor.broker(self.command(case))  # Prepared, but never sent.
                cloud.fail_audit = False
                case = memory.get('case#' + case_id)
                case.update(attempts=2, reserved_usd=50)
                doctor.save(case)
                cloud.builds[case['build_id']]['buildStatus'] = 'FAILED'
                op_key = doctor.operation(case, 'patch-version-1')
                original = memory.put
                def crash_after_durable_write(key, value):
                    result = original(key, value)
                    if ((cut == 'after-terminal-write' and key == op_key and value.get('state') == 'terminal')
                            or (cut == 'after-index-clear' and key == 'case#' + case_id
                                and value.get('outstanding_operations') == [])):
                        raise Cut()
                    return result
                memory.put = crash_after_durable_write
                with self.assertRaises(Cut):
                    doctor.reconcile()
                memory.put = original
                terminal = memory.get(op_key)
                self.assertEqual(terminal['state'], 'terminal')
                self.assertIn(terminal['receipt_key'], cloud.objects)
                self.assertNotIn('command_id', terminal)
                self.assertEqual(memory.get('host-operation')['case_id'], case_id)
                with patch.object(cloud, 'list_commands', wraps=cloud.list_commands) as listing, \
                        patch.object(cloud, 'get_command_invocation', wraps=cloud.get_command_invocation) as invocation, \
                        patch.object(cloud, 'put_object', wraps=cloud.put_object) as archive:
                    doctor.reconcile()
                    self.assertFalse((memory.get('host-operation') or {}).get('case_id'))
                    self.assertEqual(memory.get('case#' + case_id).get('outstanding_operations'), [])
                    self.assertEqual((listing.call_count, invocation.call_count, archive.call_count), (0, 0, 0))
                self.assertEqual(memory.get(op_key), terminal)
                self.assertEqual(len(cloud.starts), 1)
                self.assertEqual(cloud.sends, [])

    def test_fixed_inspect_still_checks_host_after_native_closure_without_new_mutation(self):
        case = self.admit()
        self.cloud.finding['status'] = 'CLOSED'
        failure = None
        try:
            result = self.doctor.broker(self.command(case, script='', inspect=True))
        except Exception as error:
            failure = type(error).__name__
        self.assertIsNone(failure, 'native closure must not prevent admitted read-only health verification')
        self.assertEqual(result['command_id'], COMMAND)
        self.assertEqual(self.cloud.sends[-1]['DocumentName'], 'PatchDoctor-Inspect')
        self.assertEqual(self.cloud.sends[-1]['Parameters'], {})
        with self.assertRaises(ValueError):
            self.doctor.broker(self.command(case))
        self.cloud.instance['Tags'][0]['Value'] = 'foreign'
        with self.assertRaises(ValueError):
            self.doctor.broker(self.request(case, 'command', target={'instance_id': INSTANCE, 'region': REGION},
                operation_id='foreign-inspect', inspect=True, script=''))

    def test_native_recurrence_creates_one_new_generation_after_resolved_case(self):
        case = self.admit()
        current = self.store.get('case#' + case['case_id'])
        current['status'] = 'resolved'
        self.doctor.save(current)
        self.cloud.builds[case['build_id']]['buildStatus'] = 'SUCCEEDED'
        self.doctor.release('active-case', case['case_id']) if hasattr(self.doctor, 'release') else None
        # Simulate a crash after the deterministic new case save but before pointer ACK.
        original = self.store.put
        failed = []
        def lose_pointer(key, value):
            if key.startswith('revision#') and value.get('generation') == 1 and not failed:
                failed.append(True)
                raise TimeoutError('pointer write never delivered')
            return original(key, value)
        self.store.put = lose_pointer
        try:
            self.doctor.admit(self.event)
        except TimeoutError:
            pass
        self.store.put = original
        second = self.doctor.handler(self.event)['admitted'][0]
        self.assertNotEqual(second, case['case_id'])
        self.assertEqual(self.doctor.handler(self.event)['admitted'], [second])
        self.assertEqual(len([k for k in self.store.rows if k.startswith('case#')]), 2)
        self.assertEqual(self.store.get('case#' + case['case_id'])['status'], 'resolved')
        self.assertEqual(self.store.get('case#' + second)['generation'], 1)
        self.assertEqual(len(self.cloud.starts), 2)

    def test_sparse_index_paginates_keys_and_strong_active_pointer_survives_index_lag(self):
        class DDB:
            def __init__(self):
                self.rows, self.pages = {}, 0
            def put_item(self, **kw):
                self.rows[kw['Item']['pk']['S']] = copy.deepcopy(kw['Item'])
            def get_item(self, **kw):
                assert kw['ConsistentRead'] is True
                return {'Item': copy.deepcopy(self.rows[kw['Key']['pk']['S']])}
            def query(self, **kw):
                assert kw['IndexName'] == 'active-cases'
                assert kw['KeyConditionExpression'] == 'record_kind = :kind'
                self.pages += 1
                index = int(kw.get('ExclusiveStartKey', {}).get('pk', {}).get('S', '0'))
                result = {'Items': [{'pk': {'S': 'case#' + str(index)}}]}
                if index < 20:
                    result['LastEvaluatedKey'] = {'pk': {'S': str(index + 1)}}
                return result
            def scan(self, **kw):
                # 2,100 old daily/op records precede actionable cases: old global cap fails.
                return {'Items': [], 'LastEvaluatedKey': {'pk': {'S': 'old-audit'}}}
        ddb = DDB()
        actual = C.Store(ddb, 'ledger')
        for i in range(21):
            value = {'case_id': str(i), 'status': 'pending', 'created_at': i}
            actual.put('case#' + str(i), value)
        try:
            cases = actual.cases()
        except RuntimeError:
            cases = []
        self.assertEqual(len(cases), 21)
        self.assertEqual(ddb.pages, 21)
        for i in range(21):
            self.assertEqual(ddb.rows['case#' + str(i)].get('record_kind'), {'S': 'active-case'})
        value = actual.get('case#0')
        value['status'] = 'resolved'
        actual.put('case#0', value)
        self.assertNotIn('record_kind', ddb.rows['case#0'])
        value['outstanding_operations'] = ['still-running']
        actual.put('case#0', value)
        self.assertEqual(ddb.rows['case#0']['record_kind'], {'S': 'active-case'})
        case = self.admit()
        self.cloud.finding['packageVulnerabilityDetails']['vulnerablePackages'][0]['fixedInVersion'] = '4.0'
        self.doctor.admit(self.event)
        self.store.cases = lambda: []  # GSI has not propagated either case.
        self.doctor.reconcile()
        self.assertEqual(len(self.cloud.starts), 1)
        self.assertEqual(self.store.get('active-case')['case_id'], case['case_id'])
        self.cloud.builds[case['build_id']]['buildStatus'] = 'FAILED'
        self.doctor.reconcile()
        self.assertEqual(len(self.cloud.starts), 2)
        self.doctor.reconcile()
        self.assertEqual(len(self.cloud.starts), 2)
        self.assertEqual(self.store.get('active-case')['case_id'], case['case_id'])
        fresh_store, fresh_cloud = Memory(), Cloud()
        fresh = C.Doctor(ENV, fresh_store, fresh_cloud.client, lambda: self.clock)
        fresh_store.put('config', {'armed': True, 'gate_version': 2})
        interrupted = fresh.admit(self.event)[0]
        fresh_store.put('active-case', {'case_id': interrupted})  # Killed before start intent.
        fresh_cloud.finding['status'] = 'CLOSED'
        other_finding = copy.deepcopy(fresh_cloud.finding)
        other_finding.update(status='ACTIVE', findingArn=other_finding['findingArn'] + '-second')
        other_finding['packageVulnerabilityDetails']['vulnerabilityId'] = 'CVE-2026-54321'
        fresh_cloud.extra_findings.append(other_finding)
        following = fresh.admit({**self.event, 'detail': {'findingArn': other_finding['findingArn']}})[0]
        fresh.reconcile()
        self.assertEqual(fresh_store.get('case#' + interrupted)['status'], 'blocked-no-current-critical-target')
        self.assertEqual(len(fresh_cloud.starts), 1)
        self.assertEqual(fresh_store.get('active-case')['case_id'], following)

    def test_duplicate_native_delivery_does_not_start_or_notify_twice(self):
        case = self.admit()
        self.doctor.handler(self.event)
        self.assertEqual(len(self.cloud.starts), 1)
        self.assertEqual(len(self.cloud.messages), 1)
        self.assertEqual(case['reserved_usd'], 25)
        grant = self.doctor.broker(self.request(case, 'get'))
        self.assertEqual((grant['budget_usd'], grant['attempt'], grant['reservation_id']),
            (25, 1, case['start_token']))
        self.assertEqual(len(self.store.cases()), 1)

    def test_native_readmission_reopens_same_case_when_stopped_target_returns(self):
        self.store.put('config', {'armed': True, 'gate_version': 2, 'paused_new_runs': True})
        first = self.doctor.handler(self.event)['admitted'][0]
        original = self.store.get('case#' + first)
        self.assertEqual(original['status'], 'pending')
        self.assertEqual(self.cloud.starts, [])
        self.cloud.instance['State']['Name'] = 'stopped'
        self.doctor.reconcile()
        blocked = self.store.get('case#' + first)
        self.assertEqual(blocked['status'], 'blocked-no-current-critical-target')
        self.assertNotIn(first, [case['case_id'] for case in self.store.cases()])
        self.cloud.instance['State']['Name'] = 'running'
        admitted = self.doctor.handler(self.event)['admitted']
        reopened = self.store.get('case#' + first)
        self.assertEqual(admitted, [first])
        self.assertEqual(reopened['status'], 'pending')
        for key in ('revision', 'generation', 'attempts', 'reserved_usd', 'operations', 'outstanding_operations'):
            self.assertEqual(reopened.get(key), original.get(key))
        self.assertEqual(self.cloud.starts, [])
        self.doctor.handler({'action': 'pause_new_runs', 'paused': False, 'mutationId': 'resume-returned-target'})
        self.doctor.reconcile()
        self.assertEqual(self.store.get('case#' + first)['status'], 'running')
        self.assertEqual(len(self.cloud.starts), 1)

    def test_material_fixed_version_or_target_revision_reopens_but_reimport_deduplicates(self):
        first = self.admit(armed=False)
        first['status'] = 'blocked'
        self.doctor.save(first)
        self.cloud.finding['updatedAt'] = 'changed-rescan'
        self.cloud.finding['description'] = 'updated description'
        self.cloud.finding['packageVulnerabilityDetails']['vulnerablePackages'][0]['version'] = '1.1'
        duplicate = self.doctor.handler(self.event)['admitted'][0]
        self.assertEqual(duplicate, first['case_id'])
        self.cloud.finding['packageVulnerabilityDetails']['vulnerablePackages'][0]['fixedInVersion'] = '2.1'
        fresh = self.doctor.handler(self.event)['admitted'][0]
        self.assertNotEqual(fresh, first['case_id'])
        self.assertEqual(self.store.get('case#' + fresh)['status'], 'pending')
        self.assertEqual(len([k for k in self.store.rows if k.startswith('case#')]), 2)
        self.assertEqual(self.store.get('case#' + first['case_id'])['status'], 'blocked')
        facts = self.store.get('case#' + fresh)['findings'][0]
        self.assertEqual(facts['packages'][0]['fixedInVersion'], '2.1')
        self.assertEqual(facts['source_urls'], ['https://vendor.example/CVE-2026-12345'])
        other = 'i-' + '2' * 17
        finding = copy.deepcopy(self.cloud.finding)
        finding['findingArn'] += '-new-target'
        finding['resources'][0]['id'] = other
        self.cloud.extra_findings.append(finding)
        self.cloud.extra_instances.append({'InstanceId': other, 'State': {'Name': 'running'},
            'Tags': [{'Key': 'Name', 'Value': 'nextjs-ct'}]})
        new_target = self.doctor.handler(self.event)['admitted'][0]
        self.assertNotEqual(new_target, fresh)
        self.assertEqual(len(self.store.get('case#' + new_target)['targets']), 2)

    def test_reread_lowered_finding_and_foreign_scope_refused(self):
        self.cloud.finding['severity'] = 'HIGH'
        self.assertEqual(self.doctor.handler(self.event)['admitted'], [])
        self.assertEqual(self.cloud.starts, [])
        for field, value in [('account', '222222222222'), ('region', 'eu-west-1')]:
            event = {**self.event, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.doctor.handler(event)
        self.cloud.finding['severity'] = 'CRITICAL'
        self.cloud.instance['Tags'][0]['Value'] = 'unknown-host'
        self.assertEqual(self.doctor.handler(self.event)['admitted'], [])
        self.assertEqual(self.cloud.starts, [])

    def test_securityhub_current_state_not_event_severity(self):
        event = {**self.event, 'source': 'aws.securityhub', 'detail-type': 'Security Hub Findings - Imported',
            'detail': {'findings': [{'Id': 'finding-one', 'Severity': {'Label': 'CRITICAL'}}]}}
        self.cloud.shub = {'Id': 'finding-one', 'AwsAccountId': ACCOUNT, 'RecordState': 'ACTIVE',
            'Severity': {'Label': 'HIGH'}, 'Workflow': {'Status': 'NEW'}, 'Vulnerabilities': [{'Id': 'CVE-2026-12345'}]}
        self.assertEqual(self.doctor.handler(event)['admitted'], [])
        self.cloud.shub['Severity']['Label'] = 'CRITICAL'
        self.assertEqual(len(self.doctor.handler(event)['admitted']), 1)
        self.assertEqual(self.cloud.starts, [])  # unarmed

    def test_gate_selftest_no_host_actions_and_sqs_cannot_arm(self):
        case = self.admit(armed=False)
        self.assertEqual(case['status'], 'pending')
        self.assertEqual(self.cloud.starts, [])
        reply = self.doctor.handler({'action': 'self_test', 'mutationId': 'existing-selftest'})
        test = self.store.get('case#' + reply['case_id'])
        with self.assertRaises(ValueError):
            self.doctor.broker(self.command(test))
        self.doctor.broker(self.request(test, 'finish', outcome='self-test-passed', summary='real fixture proof'))
        self.cloud.builds[test['build_id']]['buildStatus'] = 'SUCCEEDED'
        self.doctor.reconcile()
        message = {'messageId': 'forged', 'body': json.dumps({'action': 'arm'})}
        reply = self.doctor.handler({'Records': [message]})
        self.assertEqual(reply['batchItemFailures'], [{'itemIdentifier': 'forged'}])
        self.assertFalse(self.doctor.config()['armed'])
        self.assertTrue(self.doctor.handler({'action': 'arm', 'mutationId': 'existing-arm'})['armed'])
        self.assertEqual(len(self.cloud.sends), 0)

    def test_lost_start_reply_recovers_same_build_without_paid_replay(self):
        self.cloud.lose_start = True
        case = self.admit()
        self.assertEqual(case['status'], 'start-uncertain')
        self.doctor.reconcile()
        recovered = self.store.get('case#' + case['case_id'])
        self.assertEqual(recovered['build_id'], 'patchdoctor:1')
        self.assertEqual(recovered['status'], 'running')
        self.assertEqual(len(self.cloud.starts), 1)
        self.clock += 301
        self.doctor.reconcile()
        self.assertEqual(len(self.cloud.starts), 1)
        self.assertEqual(sum(m['Subject'] == 'PatchDoctor start-uncertain' for m in self.cloud.messages), 1)

    def test_definitive_start_rejection_records_no_build_and_does_not_hold_queue(self):
        self.cloud.reject_start = True
        case = self.admit()
        self.assertEqual(case['status'], 'blocked-start-rejected')
        self.assertIs(case['no_build'], True)
        self.assertEqual(case['rejection_code'], 'AccessDeniedException')
        self.assertEqual(self.cloud.builds, {})
        self.doctor.reconcile()
        self.doctor.reconcile()
        self.assertEqual(len(self.cloud.starts), 1)
        self.assertEqual(sum(m['Subject'] == 'PatchDoctor start-rejected' for m in self.cloud.messages), 1)
        self.cloud.reject_start = False
        self.cloud.finding['packageVulnerabilityDetails']['vulnerablePackages'][0]['fixedInVersion'] = '3.0'
        fresh = self.doctor.handler(self.event)['admitted'][0]
        self.assertEqual(self.store.get('case#' + fresh)['status'], 'running')
        self.assertEqual(len(self.cloud.builds), 1)

    def test_pre_send_crash_intent_resumes_but_uncertain_absence_does_not(self):
        case = self.admit(armed=False)
        case.update(status='starting', attempts=1, reserved_usd=25, started_at=self.clock,
            deadline_unix=self.clock + 3600, start_token='fixed-token', start_attempted=False)
        self.doctor.save(case)
        self.doctor.reconcile()
        self.assertEqual(len(self.cloud.starts), 1)
        case = self.store.get('case#' + case['case_id'])
        case.update(status='start-uncertain')
        case.pop('build_id')
        self.doctor.save(case)
        self.cloud.builds = {}
        self.clock += 601
        self.doctor.reconcile()
        self.assertEqual(len(self.cloud.starts), 1)
        self.assertEqual(self.store.get('case#' + case['case_id'])['status'], 'start-uncertain')

    def test_lost_command_reply_reconciles_without_resend_and_audits_before_ack(self):
        case = self.admit()
        self.cloud.lose_send = True
        event = self.command(case)
        self.cloud.fail_audit = True
        with self.assertRaises(TimeoutError):
            self.doctor.broker(event)
        self.assertEqual(self.cloud.sends, [], 'authored request must be durable before any host mutation')
        self.cloud.fail_audit = False
        receipt = self.doctor.broker(event)
        self.assertEqual(receipt['request_version_id'], 'audit-version')
        authored = json.loads(self.cloud.objects[receipt['request_key']])
        self.assertEqual(authored['script'], 'true')
        self.assertEqual(receipt['state'], 'send-uncertain')
        self.assertEqual(self.doctor.broker(event)['state'], 'send-uncertain')
        self.assertEqual(len(self.cloud.sends), 1)
        self.cloud.fail_audit = True
        poll = self.request(case, 'poll', operation_id=event['operation_id'])
        with self.assertRaises(TimeoutError):
            self.doctor.broker(poll)
        opkey = self.doctor.operation(case, event['operation_id'])
        self.assertNotEqual(self.store.get(opkey)['state'], 'terminal')
        self.cloud.fail_audit = False
        receipt = self.doctor.broker(poll)
        self.assertEqual(receipt['state'], 'terminal')
        self.assertEqual(receipt['stdout'], 'patched')
        self.assertEqual(len(self.cloud.objects), 2)
        self.assertEqual(len(self.cloud.sends), 1)
        self.assertNotIn('OutputS3BucketName', self.cloud.sends[0])
        self.assertEqual(self.cloud.sends[0]['DocumentName'], 'PatchDoctor-RunCommand')

    def test_operation_body_reuse_and_build_forgery_rejected(self):
        case = self.admit()
        event = self.command(case)
        self.doctor.broker(event)
        with self.assertRaises(ValueError):
            self.doctor.broker({**event, 'script': 'false'})
        for forged in [{**event, 'build_id': 'patchdoctor:foreign'}, {**event, 'case_id': 'a' * 64}]:
            with self.assertRaises(ValueError):
                self.doctor.broker(forged)
        build = self.cloud.builds[case['build_id']]
        build['environment']['environmentVariables'][0]['value'] = 'foreign-case'
        with self.assertRaises(ValueError):
            self.doctor.broker(event)
        self.assertEqual(len(self.cloud.sends), 1)

    def test_command_rereads_native_critical_target(self):
        case = self.admit()
        self.cloud.finding['severity'] = 'HIGH'
        with self.assertRaises(ValueError):
            self.doctor.broker(self.command(case))
        self.assertEqual(self.cloud.sends, [])

    def test_checkpoint_bound_version_acknowledged_after_head(self):
        case = self.admit()
        key = 'cases/' + case['case_id'] + '/builds/' + C.digest(case['build_id']) + '/checkpoints/one.tar.gz'
        reply = self.doctor.broker(self.request(case, 'checkpoint', key=key, version_id='version-one'))
        self.assertTrue(reply['acknowledged'])
        self.assertEqual(self.store.get('case#' + case['case_id'])['checkpoint'], {'key': key, 'version_id': 'version-one'})
        with patch.object(self.cloud, 'head_object', return_value={
                'VersionId': 'version-one', 'ContentLength': 10, 'ServerSideEncryption': 'AES256'}):
            with self.assertRaises(ValueError):
                self.doctor.broker(self.request(case, 'checkpoint', key=key, version_id='version-one'))
        for wrong in ['cases/foreign/checkpoints/x', key + '/../x']:
            with self.assertRaises(ValueError):
                self.doctor.broker(self.request(case, 'checkpoint', key=wrong, version_id='version-one'))

    def test_worker_finish_never_resolves_until_actual_native_closed(self):
        case = self.admit()
        self.doctor.broker(self.request(case, 'finish', outcome='patched', summary='verified installed version'))
        self.cloud.builds[case['build_id']]['buildStatus'] = 'SUCCEEDED'
        self.cloud.instance['State']['Name'] = 'stopped'
        self.doctor.reconcile()
        self.assertEqual(self.store.get('case#' + case['case_id'])['status'], 'awaiting-native-closure')
        self.doctor.reconcile()
        self.assertEqual(sum(m['Subject'] == 'PatchDoctor patched' for m in self.cloud.messages), 1)
        self.assertNotIn('verified installed version', json.dumps(self.cloud.messages))
        self.cloud.finding['status'] = 'CLOSED'
        self.doctor.reconcile()
        self.assertEqual(self.store.get('case#' + case['case_id'])['status'], 'resolved')

    def test_two_attempt_case_budget_and_four_daily_start_budget(self):
        case = self.admit()
        self.cloud.builds[case['build_id']]['buildStatus'] = 'FAILED'
        self.doctor.reconcile()
        case = self.store.get('case#' + case['case_id'])
        self.assertEqual(case['attempts'], 2)
        self.assertEqual(case['reserved_usd'], 50)
        self.cloud.builds[case['build_id']]['buildStatus'] = 'FAILED'
        self.doctor.reconcile()
        self.assertEqual(self.store.get('case#' + case['case_id'])['status'], 'budget-exhausted')
        self.assertEqual(len(self.cloud.starts), 2)
        today = C.datetime.datetime.fromtimestamp(self.clock, C.datetime.timezone.utc).date().isoformat()
        daily = self.store.get('daily#' + today)
        daily['starts'] = 4
        self.store.put('daily#' + today, daily)
        case = self.store.get('case#' + case['case_id'])
        case.update(attempts=0, reserved_usd=0, status='pending')
        self.doctor.save(case)
        self.doctor.reconcile()
        self.assertEqual(len(self.cloud.starts), 2)
        waiting = self.store.get('case#' + case['case_id'])
        self.assertEqual(waiting['status'], 'daily-budget-wait')
        self.clock = waiting['retry_after']
        self.doctor.reconcile()
        self.assertEqual(len(self.cloud.starts), 3)
        self.assertEqual(self.store.get('case#' + case['case_id'])['status'], 'running')

    def test_credential_canaries_never_escape_errors_or_notifications(self):
        canary = 'sk-ant-CREDENTIAL_CANARY_DO_NOT_PRINT'
        with patch.object(C, 'Doctor') as factory, patch('sys.stdout', new_callable=io.StringIO) as output:
            factory.return_value.broker.side_effect = RuntimeError(canary)
            reply = C.broker({}, None)
            self.assertEqual(reply, {'refused': True, 'error_type': 'RuntimeError'})
            self.assertNotIn(canary, output.getvalue() + json.dumps(reply))
        self.assertNotIn(canary, C.summary('token=' + canary))
        self.event['detail']['title'] = canary
        self.admit()
        self.assertNotIn(canary, json.dumps(self.cloud.messages))

    def test_acknowledged_state_survives_hard_exit_and_sigterm_process_restart(self):
        for mode in ('hard', 'term'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix='patchdoctor-restart-') as raw:
                directory = Path(raw)
                argv = [sys.executable, '-S', '-B', str(Path(__file__).resolve()), '--crash-child']
                env = {'PATH': '/usr/bin:/bin', 'HOME': raw, 'AWS_EC2_METADATA_DISABLED': 'true'}
                child = subprocess.Popen([*argv, mode, raw], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                try:
                    self.assertTrue(select.select([child.stdout], [], [], 10)[0], 'child failed to acknowledge')
                    self.assertEqual(child.stdout.readline(), b'ACK\n')
                    if mode == 'term':
                        child.terminate()
                    _, stderr = child.communicate(timeout=10)
                    self.assertEqual(child.returncode, 23 if mode == 'hard' else -signal.SIGTERM, stderr.decode())
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.communicate(timeout=5)
                recovered = subprocess.run([*argv, 'recover', raw], capture_output=True, env=env, timeout=10)
                self.assertEqual(recovered.returncode, 0, recovered.stderr.decode())
                self.assertEqual(json.loads(recovered.stdout), {'recovered': True, 'starts': 1, 'commands': 1})

    def test_non_cve_critical_recorded_blocked_without_host_actions(self):
        self.cloud.finding['packageVulnerabilityDetails'] = {}
        reply = self.doctor.handler(self.event)
        self.assertEqual(len(reply['admitted']), 1)
        case = self.store.get('case#' + reply['admitted'][0])
        self.assertEqual(case['status'], 'blocked-no-CVE')
        self.assertEqual(self.cloud.starts, [])
        self.assertEqual(len(self.cloud.messages), 1)


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--crash-child':
        crash_child(sys.argv[2], Path(sys.argv[3]))
    else:
        unittest.main(buffer=True)
