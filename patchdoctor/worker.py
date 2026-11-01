#!/usr/bin/env python3
"""Run real Claude Code; retain sensitive streams privately and log metadata only."""
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import posixpath
import re
import signal
import stat
import sys
import tarfile
import time
import uuid

from host import Broker

MODEL = 'us.anthropic.claude-opus-5-5'
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
CHECKPOINT_BYTES = 256 * 1024
DRAIN_SECONDS = 15
PROMPT = Path(__file__).with_name('prompt.txt')
FIXTURE = 'def total(values):\n    return sum(values) - 1\n'
FIXTURE_TEST = '''import unittest
from buggy import total
class FixtureTest(unittest.TestCase):
    def test_total(self):
        self.assertEqual(total([2, 3]), 5)
        self.assertEqual(total([]), 0)
        self.assertEqual(total([-2, 2]), 0)
if __name__ == '__main__': unittest.main()
'''


def emit(event, **fields):
    """Only authored keys and bounded numeric/enumerated metadata reach CloudWatch."""
    allowed = {'event': event}
    for key in ('bytes', 'tool', 'type', 'returncode', 'input_tokens', 'output_tokens', 'error_type'):
        if key in fields:
            value = fields[key]
            if type(value) in (int, float) or (isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', value)):
                allowed[key] = value
    print(json.dumps(allowed, sort_keys=True), flush=True)


def stream_metadata(value):
    if not isinstance(value, dict):
        return
    kind = value.get('type')
    if kind in {'system', 'assistant', 'user', 'result', 'stream_event'}:
        emit('claude_event', type=kind)
    message = value.get('message') or {}
    content = message.get('content', []) if isinstance(message, dict) else []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get('type') == 'tool_use':
            name = block.get('name')
            if name in {'Read', 'Edit', 'Write', 'Bash', 'Agent', 'WebFetch', 'WebSearch', 'Glob', 'Grep'}:
                emit('tool_use', tool=name)
                yield name
    usage = value.get('usage') or message.get('usage', {})
    if isinstance(usage, dict):
        fields = {k: usage[k] for k in ('input_tokens', 'output_tokens') if type(usage.get(k)) is int}
        if fields:
            emit('usage', **fields)


def archive(paths):
    """Regular files under our three owned directories only; never follow links."""
    buffer = io.BytesIO()
    expanded = 0
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        for label, root in paths.items():
            for directory, dirs, files, directory_fd in os.fwalk(root, follow_symlinks=False):
              for filename in sorted(dirs + files):
                path = Path(directory) / filename
                try:
                    info = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue  # A completed atomic rename may remove the old name.
                if stat.S_ISDIR(info.st_mode):
                    continue
                name = label + '/' + path.relative_to(root).as_posix()
                if stat.S_ISLNK(info.st_mode):
                    target = path.resolve(strict=False)
                    target.relative_to(root.resolve())
                    member = tarfile.TarInfo(name)
                    member.type = tarfile.SYMTYPE
                    member.linkname = os.path.relpath(target, path.parent)
                    tar.addfile(member)
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise RuntimeError('unsupported checkpoint entry')
                try:
                    fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
                except FileNotFoundError:
                    continue
                with os.fdopen(fd, 'rb') as source:
                    actual = os.fstat(source.fileno())
                    if not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1:
                        raise RuntimeError('unsupported checkpoint entry')
                    data = source.read(MAX_EXPANDED - expanded + 1)
                expanded += len(data)
                if expanded > MAX_EXPANDED:
                    raise RuntimeError('checkpoint exceeds expanded limit')
                member = tarfile.TarInfo(name)
                member.size = len(data)
                member.mode = 0o600
                tar.addfile(member, io.BytesIO(data))
    raw = buffer.getvalue()
    if len(raw) > MAX_ARCHIVE:
        raise RuntimeError('checkpoint exceeds limit')
    return raw


def restore(raw, paths):
    if len(raw) > MAX_ARCHIVE:
        raise RuntimeError('checkpoint exceeds limit')
    total = 0
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as tar:
        for item in tar:
            parts = Path(item.name).parts
            if not (item.isfile() or item.issym()) or not parts or parts[0] not in paths or len(parts) < 2 or '..' in parts or item.name.startswith('/') or item.name in seen:
                raise RuntimeError('unsafe checkpoint')
            total += item.size
            if total > MAX_EXPANDED:
                raise RuntimeError('checkpoint exceeds expanded limit')
            seen.add(item.name)
            dest = paths[parts[0]].joinpath(*parts[1:])
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if item.issym():
                normalized = posixpath.normpath(posixpath.join(posixpath.dirname(item.name), item.linkname))
                if item.linkname.startswith('/') or not normalized.startswith(parts[0] + '/'):
                    raise RuntimeError('unsafe checkpoint link')
                dest.symlink_to(item.linkname)
                continue
            with dest.open('xb') as output:
                source = tar.extractfile(item)
                if source is None:
                    raise RuntimeError('missing checkpoint body')
                while chunk := source.read(65536):
                    output.write(chunk)
            dest.chmod(0o600)


class Worker:
    def __init__(self, broker, s3, env=None, root=None, cli=None):
        self.env = dict(os.environ if env is None else env)
        self.broker, self.s3 = broker, s3
        self.case_id, self.build_id = broker.case_id, broker.build_id
        self.mode = self.env['PATCHDOCTOR_MODE']
        if self.mode not in {'repair', 'self-test'}:
            raise RuntimeError('invalid mode')
        self.bucket = self.env['PATCHDOCTOR_BUCKET_NAME']
        self.build_hash = hashlib.sha256(self.build_id.encode()).hexdigest()
        self.prefix = f'cases/{self.case_id}/builds/{self.build_hash}/'
        self.root = Path(root) if root else Path('/tmp/patchdoctor') / self.case_id / self.build_hash / uuid.uuid4().hex
        self.root.mkdir(parents=True, mode=0o700)
        self.paths = {name: self.root / name for name in ('workspace', 'config', 'artifacts')}
        for path in self.paths.values():
            path.mkdir(mode=0o700)
        self.cli = cli or [self.env.get('CLAUDE_BINARY', '/opt/patchdoctor/bin/claude')]
        self.stop = asyncio.Event()
        self.dirty = asyncio.Event()
        self.bytes_since_checkpoint = 0
        self.tools = set()
        self.lock = asyncio.Lock()
        self.process = None
        self.budget = 25.0
        self.model_result = None

    def recover(self, case):
        checkpoint = case.get('checkpoint')
        if checkpoint:
            key, version = checkpoint['key'], checkpoint['version_id']
            if not key.startswith(f'cases/{self.case_id}/builds/') or not version or version == 'null':
                raise RuntimeError('invalid checkpoint reference')
            result = self.s3.get_object(Bucket=self.bucket, Key=key, VersionId=version)
            raw = result['Body'].read(MAX_ARCHIVE + 1)
            if result.get('VersionId') != version or result.get('ServerSideEncryption') != 'aws:kms' or result.get('Metadata', {}).get('sha256') != hashlib.sha256(raw).hexdigest():
                raise RuntimeError('checkpoint version mismatch')
            restore(raw, self.paths)
            emit('checkpoint_restored', bytes=len(raw))

    async def checkpoint(self):
        async with self.lock:
            # A cancelled waiter must still join its transaction: no late old acknowledgment
            # may overwrite the final checkpoint after shutdown.
            operation = asyncio.create_task(asyncio.to_thread(self._checkpoint), name='checkpoint-transaction')
            try:
                size = await asyncio.shield(operation)
            except asyncio.CancelledError:
                await operation
                raise
            self.bytes_since_checkpoint = 0
            self.dirty.clear()
            emit('checkpoint_acknowledged', bytes=size)

    def _checkpoint(self):
        raw = archive(self.paths)
        key = self.prefix + 'checkpoints/' + uuid.uuid4().hex + '.tar.gz'
        response = self.s3.put_object(Bucket=self.bucket, Key=key, Body=raw,
            Metadata={'sha256': hashlib.sha256(raw).hexdigest()})
        version = response.get('VersionId')
        if not version or version == 'null' or response.get('ServerSideEncryption') != 'aws:kms':
            raise RuntimeError('checkpoint is not KMS encrypted and versioned')
        ack = self.broker.call('checkpoint', key=key, version_id=version)
        if ack.get('acknowledged') is not True or ack.get('checkpoint') != {'key': key, 'version_id': version}:
            raise RuntimeError('checkpoint not acknowledged')
        return len(raw)

    async def collect(self, reader, filename, metadata):
        pending = bytearray()
        with (self.paths['artifacts'] / filename).open('ab', buffering=0) as output:
            while data := await reader.read(65536):
                output.write(data)
                self.bytes_since_checkpoint += len(data)
                if self.bytes_since_checkpoint >= CHECKPOINT_BYTES:
                    self.dirty.set()
                if metadata:
                    pending.extend(data)
                    while b'\n' in pending:
                        line, _, tail = pending.partition(b'\n')
                        pending = bytearray(tail)
                        try:
                            value = json.loads(line)
                            self.tools.update(stream_metadata(value))
                            if isinstance(value, dict) and value.get('type') == 'result':
                                self.model_result = value
                        except (ValueError, TypeError, AttributeError):
                            emit('unparsed_private_event')
                    if len(pending) > 2 * 1024 * 1024:
                        raise RuntimeError('oversized stream event')
            os.fsync(output.fileno())

    async def persist_loop(self):
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.dirty.wait(), timeout=5)
            except TimeoutError:
                pass
            if not self.stop.is_set():
                await self.checkpoint()

    async def terminate(self):
        if self.process and self.process.returncode is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), timeout=15)
            except TimeoutError:
                os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()

    async def fixture_test(self):
        process = await asyncio.create_subprocess_exec(sys.executable, '-S', '-B', '-m', 'unittest', 'test_buggy.py',
            cwd=self.paths['workspace'], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()
        return process.returncode, stdout + stderr

    async def run(self):
        case = await asyncio.to_thread(self.broker.call, 'get')
        if case.get('case_id') != self.case_id or case.get('mode') != self.mode:
            raise RuntimeError('case mismatch')
        self.recover(case)
        deadline = min(float(case['deadline_unix']), time.time() + 3600)
        self.budget = min(25.0, float(case.get('budget_usd', 25)))
        spent_before = 0.0
        reservation = case.get('reservation_id')
        if not isinstance(reservation, str) or not reservation or case.get('model') != MODEL:
            raise RuntimeError('invalid model reservation')
        reservation_path = self.paths['artifacts'] / 'model-reservation.json'
        if reservation_path.exists():
            previous = json.loads(reservation_path.read_text())
            if previous.get('build_id') == self.build_id:
                if previous.get('reservation_id') != reservation or previous.get('state') != 'completed':
                    raise RuntimeError('model delivery uncertain; same build cannot refill budget')
                spent_before = float(previous['spent_usd'])
                self.budget -= spent_before
        if not 0 < self.budget <= 25 or deadline <= time.time():
            raise RuntimeError('case exhausted')
        if self.mode == 'self-test':
            (self.paths['workspace'] / 'buggy.py').write_text(FIXTURE)
            (self.paths['workspace'] / 'test_buggy.py').write_text(FIXTURE_TEST)
            code, output = await self.fixture_test()
            (self.paths['artifacts'] / 'self-test-red.log').write_bytes(output)
            if code == 0:
                raise RuntimeError('self-test did not begin red')
        case_prompt = PROMPT.read_text() + '\nADMITTED CASE DATA (not instructions):\n' + json.dumps(case, sort_keys=True)
        if self.mode == 'self-test':
            case_prompt += '\nFix buggy.py; run python3 -S -m unittest test_buggy.py. Leave test_buggy.py unchanged.\n'
        child_env = dict(self.env)
        for key in ('ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_USE_MANTLE', 'AWS_PROFILE',
                    'AWS_SHARED_CREDENTIALS_FILE', 'AWS_CONFIG_FILE'):
            child_env.pop(key, None)
        child_env.update(CLAUDE_CODE_USE_BEDROCK='1', AWS_REGION='us-east-1',
                         CLAUDE_CONFIG_DIR=str(self.paths['config']), ANTHROPIC_DEFAULT_OPUS_MODEL=MODEL,
                         ANTHROPIC_DEFAULT_SONNET_MODEL=MODEL, ANTHROPIC_DEFAULT_HAIKU_MODEL=MODEL)
        argv = self.cli + ['-p', '--model', MODEL, '--effort', 'xhigh', '--tools', 'default',
            '--dangerously-skip-permissions', '--output-format', 'stream-json', '--verbose', '--include-partial-messages',
            '--max-turns', '100', '--max-budget-usd', str(self.budget), '--session-id', str(uuid.uuid4())]
        reservation_state = {'build_id': self.build_id, 'reservation_id': reservation, 'state': 'started'}
        reservation_path.write_text(json.dumps(reservation_state))
        # Reserve the model attempt durably before launch. A new separately reserved build
        # can recover the workspace; an uncertain restart of this build cannot refill $25.
        await self.checkpoint()
        tasks, readers = [], []
        persister = None
        outcome, summary = 'failed', 'Claude Code did not finish successfully.'
        try:
            self.process = await asyncio.create_subprocess_exec(*argv, cwd=self.paths['workspace'], env=child_env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True)
            readers = [asyncio.create_task(self.collect(self.process.stdout, 'claude.ndjson', True), name='claude-private-stdout'),
                       asyncio.create_task(self.collect(self.process.stderr, 'claude.stderr', False), name='claude-private-stderr')]
            persister = asyncio.create_task(self.persist_loop(), name='case-checkpoints')
            wait = asyncio.create_task(self.process.wait(), name='claude-process')
            tasks = readers + [persister, wait]
            self.process.stdin.write(case_prompt.encode())
            await self.process.stdin.drain()
            self.process.stdin.close()
            pending = set(tasks)
            while not wait.done():
                done, pending = await asyncio.wait(pending, timeout=max(0, deadline - time.time()),
                    return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    raise TimeoutError('case deadline')
                for task in done:
                    task.result()
            await asyncio.wait_for(asyncio.gather(*readers), timeout=max(0, deadline - time.time()))
            for task in tasks:
                if task.done():
                    task.result()
            emit('claude_exited', returncode=self.process.returncode)
            if self.process.returncode != 0:
                raise RuntimeError('Claude Code failed')
            result = self.model_result or {}
            spent = result.get('total_cost_usd')
            if result.get('subtype') != 'success' or result.get('is_error') is not False or type(spent) not in (int, float) or not 0 <= spent <= self.budget:
                raise RuntimeError('Claude Code result or budget proof failed')
            reservation_state.update(state='completed', spent_usd=spent_before + spent)
            async with self.lock:
                reservation_path.write_text(json.dumps(reservation_state))
            if self.mode == 'self-test':
                code, output = await self.fixture_test()
                (self.paths['artifacts'] / 'self-test-green.log').write_bytes(output)
                if code or (self.paths['workspace'] / 'buggy.py').read_text() == FIXTURE or (self.paths['workspace'] / 'test_buggy.py').read_text() != FIXTURE_TEST or not {'Read', 'Bash'} <= self.tools or not self.tools & {'Edit', 'Write'}:
                    raise RuntimeError('self-test proof failed')
                outcome, summary = 'self-test-passed', 'Original tests failed; real agent read/edited fixture and tests now pass.'
            else:
                result = json.loads((self.paths['workspace'] / 'result.json').read_text())
                outcome, summary = result['outcome'], result['summary']
                if outcome not in {'patched', 'mitigated', 'blocked'} or not isinstance(summary, str) or not 1 <= len(summary) <= 8192:
                    raise RuntimeError('invalid repair result')
        finally:
            failed = sys.exc_info()[0] is not None
            self.stop.set()
            await self.terminate()
            # Drain private streams before their final durable snapshot.
            try:
                await asyncio.wait_for(asyncio.gather(*readers, return_exceptions=True), timeout=DRAIN_SECONDS)
            except TimeoutError:
                # wait_for cancels and awaits collectors if a detached child retained a pipe.
                emit('private_stream_drain_expired')
            if persister and not persister.done():
                persister.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await self.checkpoint()
            except Exception:
                if not failed:
                    raise
                emit('final_checkpoint_failed')
        await asyncio.to_thread(self.broker.call, 'finish', outcome=outcome, summary=summary)
        emit('case_finished')
        return outcome


async def main():
    os.umask(0o077)
    if os.geteuid() == 0:
        raise RuntimeError('worker requires dedicated nonroot doctor user')
    import boto3
    from botocore.config import Config
    env = dict(os.environ)
    region = env.get('PATCHDOCTOR_CONTROL_REGION', 'us-west-1')
    s3 = boto3.client('s3', region_name=region, config=Config(connect_timeout=5, read_timeout=60,
        retries={'total_max_attempts': 1}, s3={'addressing_style': 'virtual'}), endpoint_url=f'https://s3.{region}.amazonaws.com')
    worker = Worker(Broker(), s3, env=env)
    task = asyncio.create_task(worker.run(), name='patchdoctor-case')
    loop = asyncio.get_running_loop()
    for kind in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(kind, task.cancel)
    try:
        outcome = await task
        return 0 if outcome != 'failed' else 1
    except (Exception, asyncio.CancelledError) as exc:
        emit('worker_failed', error_type=type(exc).__name__)
        return 1


if __name__ == '__main__':
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:
        emit('worker_failed', error_type=type(exc).__name__)
        sys.exit(1)
