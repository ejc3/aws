#!/usr/bin/env python3
"""The agent's fixed broker client; it has no direct SSM or administrator path."""
import argparse
import json
import os
from pathlib import Path
import re
import sys


class BrokerError(RuntimeError):
    pass


class Broker:
    def __init__(self, client=None, env=None):
        env = os.environ if env is None else env
        self.case_id = env['PATCHDOCTOR_CASE_ID']
        self.build_id = env['CODEBUILD_BUILD_ID']
        self.arn = env['PATCHDOCTOR_BROKER_ARN']
        if not re.fullmatch(r'[a-f0-9]{64}', self.case_id):
            raise BrokerError('invalid case identity')
        if not re.fullmatch(r'[A-Za-z0-9_-]+:[a-f0-9-]{36}', self.build_id):
            raise BrokerError('invalid build identity')
        if not re.fullmatch(r'arn:aws:lambda:[a-z0-9-]+:[0-9]{12}:function:[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)?', self.arn):
            raise BrokerError('invalid broker identity')
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client('lambda', region_name=self.arn.split(':')[3], config=Config(
                connect_timeout=5, read_timeout=70, retries={'total_max_attempts': 1}))
        self.client = client

    def call(self, action, **fields):
        if action not in {'get', 'command', 'poll', 'checkpoint', 'finish'} or {'action', 'case_id', 'build_id'} & fields.keys():
            raise BrokerError('invalid broker operation')
        payload = dict(fields, action=action, case_id=self.case_id, build_id=self.build_id)
        try:
            reply = self.client.invoke(FunctionName=self.arn, InvocationType='RequestResponse',
                                       Payload=json.dumps(payload).encode())
            body = reply['Payload'].read(2 * 1024 * 1024 + 1)
            if reply.get('FunctionError') or len(body) > 2 * 1024 * 1024:
                raise BrokerError('broker refused operation')
            value = json.loads(body)
            if not isinstance(value, dict) or value.get('error') or value.get('refused') is True or value.get('ok') is False:
                raise BrokerError('broker refused operation')
            return value
        except BrokerError:
            raise
        except Exception as exc:
            # Invocation delivery can be uncertain. The caller must reconcile the SAME operation ID.
            raise BrokerError('broker delivery uncertain; reconcile operation') from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('get', aliases=['case'])
    run = sub.add_parser('run')
    run.add_argument('--target', required=True)
    run.add_argument('--region', required=True)
    run.add_argument('--operation-id', required=True)
    run.add_argument('--script-file', type=Path)
    run.add_argument('--inspect', action='store_true')
    poll = sub.add_parser('poll')
    poll.add_argument('--operation-id', required=True)
    checkpoint = sub.add_parser('checkpoint')
    checkpoint.add_argument('--key', required=True)
    checkpoint.add_argument('--version-id', required=True)
    finish = sub.add_parser('finish')
    finish.add_argument('--json-file', type=Path, required=True)
    args = parser.parse_args(argv)
    fields = {}
    action = {'case': 'get', 'run': 'command'}.get(args.action, args.action)
    try:
        if action in {'command', 'poll'}:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', args.operation_id):
                raise BrokerError('invalid operation identity')
            fields['operation_id'] = args.operation_id
        if action == 'command':
            if os.environ.get('PATCHDOCTOR_MODE') == 'self-test':
                raise BrokerError('self-test cannot mutate hosts')
            if args.inspect == bool(args.script_file):
                raise BrokerError('choose fixed inspection or script file')
            script = args.script_file.read_text() if args.script_file else ''
            if len(script.encode()) > 16384:
                raise BrokerError('host script exceeds limit')
            fields.update(target={'instance_id': args.target, 'region': args.region}, script=script, inspect=args.inspect)
        elif action == 'checkpoint':
            fields.update(key=args.key, version_id=args.version_id)
        elif action == 'finish':
            value = json.loads(args.json_file.read_text())
            fields.update(outcome=value['outcome'], summary=value['summary'])
        result = Broker().call(action, **fields)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__, 'operation_id': fields.get('operation_id'),
                          'reconcile_before_resending': action == 'command'}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
