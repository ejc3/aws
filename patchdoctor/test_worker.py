#!/usr/bin/env python3
"""Credential-free worker regressions; only the owned fake CLI is a subprocess."""
import asyncio
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

import host
import worker

CASE = 'a' * 64
BUILD = 'patchdoctor:00000000-0000-0000-0000-000000000001'
ENV = {'PATCHDOCTOR_MODE': 'self-test', 'PATCHDOCTOR_BUCKET_NAME': 'private-test-artifacts',
       'PATCHDOCTOR_CASE_ID': CASE, 'CODEBUILD_BUILD_ID': BUILD,
       'PATCHDOCTOR_BROKER_ARN': 'arn:aws:lambda:us-west-1:111111111111:function:patchdoctor:broker',
       'AWS_EC2_METADATA_DISABLED': 'true', 'PATH': os.defpath}
CANARY = 'DO_NOT_PRINT_SECRET_8e27cc'
CAPABILITY = 'e' * 64


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, **kw):
        assert 'ServerSideEncryption' not in kw  # Bucket-default customer KMS, never SSE-S3 override.
        version = str(len(self.objects) + 1)
        self.objects[(kw['Key'], version)] = (bytes(kw['Body']), kw['Metadata'])
        return {'VersionId': version, 'ServerSideEncryption': 'aws:kms'}

    def get_object(self, **kw):
        raw, metadata = self.objects[(kw['Key'], kw['VersionId'])]
        return {'VersionId': kw['VersionId'], 'Body': io.BytesIO(raw), 'Metadata': metadata,
                'ServerSideEncryption': 'aws:kms'}


class FakeBroker:
    case_id, build_id = CASE, BUILD

    def __init__(self):
        self.case = {'case_id': CASE, 'mode': 'self-test', 'deadline_unix': time.time() + 120,
                     'budget_usd': 25, 'model': worker.MODEL, 'reservation_id': 'reservation-one',
                     'attempt': 1, 'operations': []}
        self.calls = []
        self.claimed_build = None

    def claim_worker(self):
        if self.claimed_build == self.build_id:
            raise host.BrokerError('worker already claimed')
        self.calls.append(('worker_claim', {}))
        self.claimed_build = self.build_id

    def call(self, action, **kw):
        self.calls.append((action, kw))
        if action == 'get':
            return dict(self.case)
        if action == 'checkpoint':
            self.case['checkpoint'] = dict(kw)
            return {'acknowledged': True, 'checkpoint': dict(kw)}
        if action == 'finish':
            assert self.case.get('checkpoint')
            return {'acknowledged': True}
        raise AssertionError('unexpected broker action')


class TestWorker(worker.Worker):
    __test__ = False

    async def fixture_test(self):
        # Same deterministic fixture assertions, in process; no host command or SDK.
        namespace = {}
        exec((self.paths['workspace'] / 'buggy.py').read_text(), namespace)
        ok = all(namespace['total'](xs) == answer for xs, answer in [([2, 3], 5), ([], 0), ([-2, 2], 0)])
        return (0 if ok else 1), b'fixture test result'


class WorkerTests(unittest.TestCase):
    def setUp(self):
        original_socket = socket.socket
        class OfflineSocket(original_socket):
            def __init__(self, family=socket.AF_INET, *args, **kwargs):
                if family != socket.AF_UNIX:
                    raise AssertionError('network forbidden')
                super().__init__(family, *args, **kwargs)
        self.sockets = patch.object(socket, 'socket', OfflineSocket)
        self.sockets.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.broker, self.s3 = FakeBroker(), FakeS3()

    def tearDown(self):
        self.temp.cleanup()
        self.sockets.stop()

    def make_worker(self, cli=None, env=None):
        path = self.root / str(len(list(self.root.iterdir())))
        return TestWorker(self.broker, self.s3, env=env or ENV, root=path, cli=cli)

    def fake_cli(self, fixed=True, returncode=0):
        path = self.root / ('fake_cli_' + str(len(list(self.root.iterdir()))) + '.py')
        path.write_text('import pathlib,json,sys,os\n'
            'assert "--max-turns" in sys.argv and sys.argv[sys.argv.index("--max-turns")+1]=="100"\n'
            'assert sys.argv[sys.argv.index("--model")+1]=="us.anthropic.claude-opus-5-5"\n'
            'assert os.environ["AWS_REGION"]=="us-east-1"\n'
            'assert os.environ["CLAUDE_CODE_USE_BEDROCK"]=="1"\n'
            'assert all(os.environ["ANTHROPIC_DEFAULT_"+n+"_MODEL"]=="us.anthropic.claude-opus-5-5" for n in ["OPUS","SONNET","HAIKU"])\n'
            'assert "ANTHROPIC_API_KEY" not in os.environ\n'
            'prompt=sys.stdin.read()\n'
            'assert ' + repr(CAPABILITY) + ' not in prompt\n'
            'assert all(' + repr(CAPABILITY) + ' not in value for value in os.environ.values())\n'
            + ('pathlib.Path("buggy.py").write_text("def total(values):\\n    return sum(values)\\n")\n' if fixed else '')
            + 'print(json.dumps({"type":"assistant","message":{"content":[{"type":"tool_use","name":n,"input":{"secret":' + repr(CANARY) + '}} for n in ["Read","Edit","Bash"]]}}),flush=True)\n'
            'print(json.dumps({"type":"result","subtype":"success","is_error":False,"total_cost_usd":0.01,"result":' + repr(CANARY) + ',"usage":{"input_tokens":2,"output_tokens":3}}),flush=True)\n'
            'print(' + repr(CANARY) + ',file=sys.stderr)\n'
            f'sys.exit({returncode})\n')
        return [sys.executable, '-I', '-S', str(path)]

    def test_real_stream_is_private_and_success_requires_actual_fixture_change(self):
        w = self.make_worker(cli=self.fake_cli())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            outcome = asyncio.run(w.run())
        self.assertEqual(outcome, 'self-test-passed')
        self.assertNotIn(CANARY, output.getvalue())
        self.assertIn('tool_use', output.getvalue())
        raw = (w.paths['artifacts'] / 'claude.ndjson').read_text()
        self.assertIn(CANARY, raw)
        self.assertIn(CANARY, (w.paths['artifacts'] / 'claude.stderr').read_text())
        self.assertEqual(self.broker.calls[-1][0], 'finish')
        self.assertEqual(self.broker.calls[-2][0], 'checkpoint')

    def test_worker_claim_precedes_get_cli_and_durable_artifacts(self):
        backend = self.broker
        class Client:
            def invoke(self, **kw):
                request = json.loads(kw['Payload'])
                action = request.pop('action')
                self_case, self_build = request.pop('case_id'), request.pop('build_id')
                assert (self_case, self_build) == (CASE, BUILD)
                capability = request.pop('worker_capability', None)
                if action == 'worker_claim':
                    backend.claim_worker()
                    result = {'worker_capability': CAPABILITY}
                else:
                    if action in {'checkpoint', 'finish'}:
                        assert capability == CAPABILITY
                    else:
                        assert capability is None
                    result = backend.call(action, **request)
                return {'Payload': io.BytesIO(json.dumps(result).encode())}
        parent = host.Broker(client=Client(), env=ENV)
        w = TestWorker(parent, self.s3, env=ENV, root=self.root / 'actual-parent', cli=self.fake_cli())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(w.run()), 'self-test-passed')
        self.assertEqual(parent._worker_capability, CAPABILITY)
        self.assertEqual(self.broker.calls[0][0], 'worker_claim')
        self.assertEqual(self.broker.calls[1][0], 'get')
        for raw, _ in self.s3.objects.values():
            with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as tar:
                for member in tar:
                    if member.isfile():
                        self.assertNotIn(CAPABILITY.encode(), tar.extractfile(member).read())

    def test_model_broker_cannot_checkpoint_finish_or_repeat_parent_claim(self):
        class Client:
            def __init__(self):
                self.calls, self.claimed = [], False
            def invoke(self, **kw):
                payload = json.loads(kw['Payload'])
                self.calls.append(payload)
                action = payload['action']
                if action == 'worker_claim':
                    if self.claimed:
                        result = {'refused': True, 'error_type': 'AlreadyClaimed'}
                    else:
                        self.claimed = True
                        result = {'worker_capability': CAPABILITY}
                elif action in {'checkpoint', 'finish'}:
                    result = {'acknowledged': payload.get('worker_capability') == CAPABILITY}
                else:
                    result = {'case_id': CASE}
                return {'Payload': io.BytesIO(json.dumps(result).encode())}
        client = Client()
        model = host.Broker(client=client, env=ENV)
        for action in ('checkpoint', 'finish'):
            with self.subTest(action=action), self.assertRaises(host.BrokerError):
                model.call(action)
        self.assertEqual(client.calls, [])
        parent = host.Broker(client=client, env=ENV)
        self.assertTrue(callable(getattr(parent, 'claim_worker', None)))
        parent.claim_worker()
        self.assertTrue(parent.call('checkpoint', key='own', version_id='one')['acknowledged'])
        self.assertTrue(parent.call('finish', outcome='self-test-passed', summary='proof')['acknowledged'])
        parent.call('get')
        self.assertNotIn('worker_capability', client.calls[-1])
        with self.assertRaises(host.BrokerError):
            host.Broker(client=client, env=ENV).claim_worker()
        self.assertNotIn(CAPABILITY, json.dumps(ENV))

    def test_model_cli_exposes_no_checkpoint_or_finish_subcommands(self):
        for argv in (['checkpoint', '--key', 'own', '--version-id', 'one'],
                     ['finish', '--json-file', str(self.root / 'result.json')]):
            with self.subTest(action=argv[0]), contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    host.main(argv)
                self.assertEqual(raised.exception.code, 2)

    def test_claimed_success_without_fix_never_finishes(self):
        w = self.make_worker(cli=self.fake_cli(fixed=False))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'proof failed'):
            asyncio.run(w.run())
        self.assertFalse(any(action == 'finish' for action, _ in self.broker.calls))

    def test_failed_cli_never_reports_pass(self):
        w = self.make_worker(cli=self.fake_cli(returncode=1))
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'Claude Code failed'):
            asyncio.run(w.run())
        self.assertFalse(any(action == 'finish' for action, _ in self.broker.calls))

    def test_restart_recovers_acknowledged_version_not_unacknowledged_local_edits(self):
        w = self.make_worker()
        (w.paths['workspace'] / 'evidence.txt').write_text('acknowledged')
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(w.checkpoint())
        checkpoint = dict(self.broker.case['checkpoint'])
        (w.paths['workspace'] / 'evidence.txt').write_text('unacknowledged crash state')
        restored = self.make_worker()
        with contextlib.redirect_stdout(io.StringIO()):
            restored.recover({'checkpoint': checkpoint})
        self.assertEqual((restored.paths['workspace'] / 'evidence.txt').read_text(), 'acknowledged')

    def test_checkpoint_refusal_is_not_acknowledged(self):
        w = self.make_worker()
        self.broker.call = lambda *a, **k: {'acknowledged': False}
        with self.assertRaisesRegex(RuntimeError, 'not acknowledged'):
            asyncio.run(w.checkpoint())

    def test_recovery_checks_version_content_digest_and_kms(self):
        w = self.make_worker()
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(w.checkpoint())
        checkpoint = self.broker.case['checkpoint']
        key = checkpoint['key'], checkpoint['version_id']
        raw, metadata = self.s3.objects[key]
        self.s3.objects[key] = (raw + b'tampered', metadata)
        with self.assertRaisesRegex(RuntimeError, 'version mismatch'):
            self.make_worker().recover({'checkpoint': checkpoint})

    def test_archive_size_limit_uses_actual_file_bytes(self):
        w = self.make_worker()
        (w.paths['workspace'] / 'large').write_bytes(b'12345')
        with patch.object(worker, 'MAX_EXPANDED', 4), self.assertRaisesRegex(RuntimeError, 'expanded limit'):
            worker.archive(w.paths)

    def test_uncertain_same_build_reservation_cannot_refill_budget(self):
        w = self.make_worker()
        (w.paths['artifacts'] / 'model-reservation.json').write_text(json.dumps({
            'build_id': BUILD, 'reservation_id': 'reservation-one', 'state': 'started'}))
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(w.checkpoint())
        recovered = self.make_worker(cli=['must-not-run'])
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'cannot refill'):
            asyncio.run(recovered.run())

    def test_new_separately_reserved_build_recovers_old_workspace_and_can_run(self):
        w = self.make_worker()
        (w.paths['workspace'] / 'evidence.txt').write_text('old acknowledged evidence')
        (w.paths['artifacts'] / 'model-reservation.json').write_text(json.dumps({
            'build_id': BUILD, 'reservation_id': 'reservation-one', 'state': 'started'}))
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(w.checkpoint())
        self.broker.build_id = 'patchdoctor:00000000-0000-0000-0000-000000000002'
        self.broker.case.update(reservation_id='reservation-two', attempt=2)
        recovered = self.make_worker(cli=self.fake_cli())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(recovered.run()), 'self-test-passed')
        self.assertEqual((recovered.paths['workspace'] / 'evidence.txt').read_text(), 'old acknowledged evidence')

    def test_persister_failure_after_reader_eof_terminates_owned_cli(self):
        path = self.root / 'waiting_cli.py'
        path.write_text('import os,sys,time\nsys.stdin.read()\nos.close(1)\nos.close(2)\ntime.sleep(30)\n')
        w = self.make_worker(cli=[sys.executable, '-I', '-S', str(path)])
        async def broken_persister():
            await asyncio.sleep(0.05)
            raise RuntimeError('checkpoint storage failed')
        w.persist_loop = broken_persister
        started = time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'checkpoint storage failed'):
            asyncio.run(w.run())
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNotNone(w.process.returncode)
        self.assertFalse(any(action == 'finish' for action, _ in self.broker.calls))

    def test_cleanup_cancels_and_awaits_readers_that_do_not_reach_eof(self):
        w = self.make_worker(cli=self.fake_cli())
        cli_path = Path(w.cli[-1])
        cli_path.write_text(cli_path.read_text().replace('sys.exit(0)', 'import time\ntime.sleep(30)\nsys.exit(0)'))
        cancelled = []
        async def retained_pipe(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        async def broken_persister():
            await asyncio.sleep(0.02)
            raise RuntimeError('checkpoint storage failed')
        w.collect, w.persist_loop = retained_pipe, broken_persister
        with patch.object(worker, 'DRAIN_SECONDS', 0.03), contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'checkpoint storage failed'):
            asyncio.run(w.run())
        self.assertEqual(len(cancelled), 2)

    def test_error_or_budget_result_cannot_finish_using_stale_repair_result(self):
        for old, replacement in [('"subtype":"success"', '"subtype":"error_max_budget_usd"'),
                                 ('"is_error":False', '"is_error":True')]:
            with self.subTest(replacement=replacement):
                self.broker = FakeBroker()
                cli = self.fake_cli()
                path = Path(cli[-1])
                source = path.read_text()
                self.assertIn(old, source)
                path.write_text(source.replace(old, replacement))
                env = dict(ENV, PATCHDOCTOR_MODE='repair')
                self.broker.case.update(mode='repair')
                self.broker.case.pop('checkpoint', None)
                w = self.make_worker(cli=cli, env=env)
                (w.paths['workspace'] / 'result.json').write_text('{"outcome":"patched","summary":"stale success"}')
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'result or budget proof failed'):
                    asyncio.run(w.run())
        self.assertFalse(any(action == 'finish' for action, _ in self.broker.calls))

    def test_checkpoint_cancellation_joins_ack_before_final_checkpoint(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        original = self.broker.call
        def slow_ack(action, **fields):
            if action == 'checkpoint':
                entered.set()
                if not release.wait(2):
                    raise AssertionError('test release missing')
            return original(action, **fields)
        self.broker.call = slow_ack
        w = self.make_worker()
        async def scenario():
            task = asyncio.create_task(w.checkpoint(), name='cancelled-test-checkpoint')
            await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await w.checkpoint()
        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(scenario())
        self.assertEqual(self.broker.case['checkpoint']['version_id'], '2')

    def test_deployed_cli_layout_and_installer_ownership(self):
        env = dict(ENV, CLAUDE_BINARY='/opt/patchdoctor/bin/claude')
        self.assertEqual(self.make_worker(env=env).cli, ['/opt/patchdoctor/bin/claude'])
        installer = Path(__file__).with_name('install-claude.sh').read_text()
        self.assertIn('destination=${1:', installer)
        self.assertIn('install -m 0755 "$tmp" "$destination"', installer)
        self.assertNotIn('host.py', installer)
        self.assertNotIn('/usr/local/bin/claude', installer)

    def test_checkpoint_rejects_escape_but_roundtrips_owned_symlink(self):
        w = self.make_worker()
        f = w.paths['workspace'] / 'file'
        f.write_text('owned')
        (w.paths['workspace'] / 'link').symlink_to(f)
        data = worker.archive(w.paths)
        restored = self.make_worker()
        worker.restore(data, restored.paths)
        self.assertEqual((restored.paths['workspace'] / 'link').read_text(), 'owned')
        (w.paths['workspace'] / 'escape').symlink_to('/etc/passwd')
        with self.assertRaises(ValueError):
            worker.archive(w.paths)

    def test_malicious_restore_member_is_refused(self):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as tar:
            member = tarfile.TarInfo('workspace/../../outside')
            member.size = 1
            tar.addfile(member, io.BytesIO(b'x'))
        with self.assertRaisesRegex(RuntimeError, 'unsafe checkpoint'):
            worker.restore(data.getvalue(), self.make_worker().paths)

    def test_broker_region_identity_and_uncertain_command_no_retry(self):
        class Client:
            calls = []
            def invoke(self, **kw):
                self.calls.append(kw)
                raise OSError(CANARY)
        c = Client()
        b = host.Broker(client=c, env=ENV)
        with self.assertRaisesRegex(host.BrokerError, 'uncertain'):
            b.call('command', operation_id='stable', target={'instance_id': 'i-test', 'region': 'us-west-1'}, script='true')
        self.assertEqual(len(c.calls), 1)
        payload = json.loads(c.calls[0]['Payload'])
        self.assertEqual(payload['case_id'], CASE)
        self.assertEqual(payload['build_id'], BUILD)
        self.assertEqual(c.calls[0]['FunctionName'], ENV['PATCHDOCTOR_BROKER_ARN'])

    def test_broker_refused_dto_is_failure(self):
        class Client:
            def invoke(self, **kw):
                return {'Payload': io.BytesIO(b'{"refused":true,"error_type":"Refused"}')}
        with self.assertRaises(host.BrokerError):
            host.Broker(client=Client(), env=ENV).call('get')

    def test_expired_case_never_starts_cli(self):
        self.broker.case['deadline_unix'] = 1
        with self.assertRaisesRegex(RuntimeError, 'exhausted'):
            asyncio.run(self.make_worker(cli=['must-not-run']).run())


if __name__ == '__main__':
    unittest.main(buffer=True)
