"""PatchDoctor's durable admission controller and worker-only command broker."""
import datetime
import hashlib
import json
import os
import re
import time

MODEL = 'us.anthropic.claude-opus-5-5'
CVE = re.compile(r'CVE-[0-9]{4}-[0-9]{4,10}\Z')
IID = re.compile(r'i-[0-9a-f]{8}(?:[0-9a-f]{9})?\Z')
DIGEST = re.compile(r'[0-9a-f]{64}\Z')
BUILD_TERMINAL = {'SUCCEEDED', 'FAILED', 'FAULT', 'STOPPED', 'TIMED_OUT'}
COMMAND_TERMINAL = {'Success', 'Failed', 'Cancelled', 'TimedOut', 'DeliveryTimedOut', 'ExecutionTimedOut', 'Undeliverable', 'Terminated'}
OUTCOMES = {'patched', 'mitigated', 'blocked', 'self-test-passed'}
START_REJECTED = {'InvalidInputException', 'ResourceNotFoundException', 'AccountLimitExceededException',
    'AccessDeniedException', 'NotAuthorizedException'}
_CLIENTS = {}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def require(value):
    if not value:
        raise ValueError('outside PatchDoctor contract')


def error_code(error):
    return getattr(error, 'response', {}).get('Error', {}).get('Code', '')


def client(service, region):
    key = service, region
    if key not in _CLIENTS:
        import boto3
        from botocore.config import Config
        _CLIENTS[key] = boto3.client(service, region_name=region, config=Config(
            connect_timeout=5, read_timeout=30, retries={'total_max_attempts': 1}))
    return _CLIENTS[key]


def summary(value):
    # Summaries, unlike command output, may enter notifications. Never log raw SDK errors.
    text = str(value)[:2000]
    text = re.sub(r'(?:AKIA|ASIA)[A-Z0-9]{16}|(?:sk-ant-|github_pat_|ghp_)[A-Za-z0-9_-]+', '[redacted]', text)
    text = re.sub(r'(?i)(?:bearer\s+|(?:secret|password|token|accesskey)[\w _-]*[=:]\s*)\S+', '[redacted]', text)
    return '[redacted private key]' if 'PRIVATE KEY' in text else text


class Store:
    def __init__(self, ddb, table):
        self.ddb, self.table = ddb, table

    def get(self, pk):
        item = self.ddb.get_item(TableName=self.table, Key={'pk': {'S': pk}}, ConsistentRead=True).get('Item')
        if not item:
            return None
        value = json.loads(item['body']['S'])
        value['_revision'] = int(item['revision']['N'])
        return value

    def put(self, pk, value):
        revision = value.get('_revision', 0)
        body = {k: v for k, v in value.items() if k != '_revision'}
        require(len(encoded(body).encode()) < 350000)
        kwargs = {'TableName': self.table, 'Item': {'pk': {'S': pk}, 'body': {'S': encoded(body)},
            'revision': {'N': str(revision + 1)}}}
        if revision:
            kwargs.update(ConditionExpression='revision = :old', ExpressionAttributeValues={':old': {'N': str(revision)}})
        else:
            kwargs['ConditionExpression'] = 'attribute_not_exists(pk)'
        self.ddb.put_item(**kwargs)
        value['_revision'] = revision + 1
        return value

    def cases(self):
        result, start = [], None
        for _ in range(20):
            kwargs = {'TableName': self.table, 'ConsistentRead': True, 'Limit': 100,
                'FilterExpression': 'begins_with(pk, :prefix)', 'ExpressionAttributeValues': {':prefix': {'S': 'case#'}}}
            if start:
                kwargs['ExclusiveStartKey'] = start
            page = self.ddb.scan(**kwargs)
            for item in page.get('Items', []):
                value = json.loads(item['body']['S'])
                value['_revision'] = int(item['revision']['N'])
                result.append(value)
            start = page.get('LastEvaluatedKey')
            if not start:
                return result
        raise RuntimeError('incident ledger scan bound exceeded')


class Doctor:
    def __init__(self, env=None, store=None, clients=client, clock=time.time):
        self.env = os.environ if env is None else env
        self.now, self.clients = clock, clients
        self.region = self.env.get('AWS_REGION', 'us-west-1')
        self.account = self.env['ACCOUNT_ID']
        require(re.fullmatch(r'[0-9]{12}', self.account))
        self.regions = set(json.loads(self.env['ALLOWED_REGIONS_JSON']))
        self.names = set(json.loads(self.env['TARGET_NAMES_JSON']))
        self.store = store or Store(clients('dynamodb', self.region), self.env['TABLE_NAME'])

    def api(self, service, region=None):
        return self.clients(service, region or self.region)

    def save(self, case):
        case['updated_at'] = int(self.now())
        return self.store.put('case#' + case['case_id'], case)

    def notify(self, kind, case):
        self.api('sns').publish(TopicArn=self.env['SNS_TOPIC_ARN'], Subject='PatchDoctor ' + kind,
            Message=encoded({'kind': kind, 'case_id': case['case_id'], 'cve': case['cve'],
                'region': case['region'], 'status': case['status'], 'targets': len(case.get('targets', []))}))

    def notify_once(self, kind, case, marker):
        if not case.get(marker):
            self.notify(kind, case)
            case[marker] = True
            self.save(case)

    def config(self):
        return self.store.get('config') or {'armed': False}

    def describe_target(self, instance_id, region):
        require(region in self.regions and isinstance(instance_id, str) and IID.fullmatch(instance_id))
        page = self.api('ec2', region).describe_instances(Filters=[{'Name': 'instance-id', 'Values': [instance_id]}])
        rows = [(r, i) for r in page.get('Reservations', []) for i in r.get('Instances', [])]
        require(len(rows) == 1)
        reservation, instance = rows[0]
        tags = {t['Key']: t['Value'] for t in instance.get('Tags', [])}
        require(reservation.get('OwnerId') == self.account and instance['InstanceId'] == instance_id
            and tags.get('Name') in self.names and instance.get('State', {}).get('Name') == 'running')
        return {'instance_id': instance_id, 'region': region, 'name': tags['Name']}

    def critical_targets(self, cve, region):
        require(CVE.fullmatch(cve) and region in self.regions)
        filters = {'vulnerabilityId': [{'comparison': 'EQUALS', 'value': cve}],
            'severity': [{'comparison': 'EQUALS', 'value': 'CRITICAL'}],
            'findingStatus': [{'comparison': 'EQUALS', 'value': 'ACTIVE'}],
            'resourceType': [{'comparison': 'EQUALS', 'value': 'AWS_EC2_INSTANCE'}]}
        targets, findings, token = {}, [], None
        for _ in range(20):
            args = {'filterCriteria': filters, 'maxResults': 100}
            if token:
                args['nextToken'] = token
            page = self.api('inspector2', region).list_findings(**args)
            for finding in page.get('findings', []):
                if (finding.get('awsAccountId') != self.account or finding.get('severity') != 'CRITICAL'
                        or finding.get('status') != 'ACTIVE'
                        or finding.get('packageVulnerabilityDetails', {}).get('vulnerabilityId') != cve):
                    continue
                for resource in finding.get('resources', []):
                    if resource.get('type') != 'AWS_EC2_INSTANCE' or resource.get('region', region) != region:
                        continue
                    instance_id = resource.get('id', '')
                    if not IID.fullmatch(instance_id):
                        continue
                    try:
                        target = self.describe_target(instance_id, region)
                    except ValueError:
                        continue
                    targets[instance_id] = target
                    advisory = finding.get('packageVulnerabilityDetails', {})
                    packages = [{key: str(package.get(key, ''))[:128] for key in
                        ('name', 'fixedInVersion', 'version', 'packageManager')}
                        for package in advisory.get('vulnerablePackages', [])[:8]]
                    urls = [str(url)[:512] for url in [advisory.get('sourceUrl', ''),
                        *advisory.get('referenceUrls', [])[:2]] if str(url).startswith('https://')]
                    findings.append({'finding_arn': finding['findingArn'], 'instance_id': instance_id,
                        'fix_available': finding.get('fixAvailable', 'UNKNOWN'),
                        'exploit_available': finding.get('exploitAvailable', 'UNKNOWN'),
                        'title': str(finding.get('title', ''))[:200],
                        'description': str(finding.get('description', ''))[:800],
                        'packages': packages, 'source_urls': urls})
            token = page.get('nextToken')
            if not token:
                return list(targets.values()), findings[:50]
        raise RuntimeError('finding pagination bound exceeded')

    def admit(self, event):
        require(isinstance(event, dict) and len(encoded(event).encode()) <= 256 * 1024)
        require(event.get('account') == self.account and event.get('region') in self.regions)
        region, detail = event['region'], event.get('detail', {})
        cves, critical_ids = set(), set()
        if event.get('source') == 'aws.inspector2' and event.get('detail-type') == 'Inspector2 Finding':
            arn = detail.get('findingArn', '')
            require(isinstance(arn, str) and len(arn) < 512)
            page = self.api('inspector2', region).list_findings(filterCriteria={
                'findingArn': [{'comparison': 'EQUALS', 'value': arn}]}, maxResults=100)
            for finding in page.get('findings', []):
                if (finding.get('findingArn') == arn and finding.get('awsAccountId') == self.account
                        and finding.get('status') == 'ACTIVE' and finding.get('severity') == 'CRITICAL'):
                    critical_ids.add(finding['findingArn'])
                    cves.add(finding.get('packageVulnerabilityDetails', {}).get('vulnerabilityId', ''))
        elif event.get('source') == 'aws.securityhub' and event.get('detail-type') == 'Security Hub Findings - Imported':
            for hint in detail.get('findings', [])[:100]:
                page = self.api('securityhub', region).get_findings(Filters={
                    'Id': [{'Value': hint.get('Id', ''), 'Comparison': 'EQUALS'}],
                    'AwsAccountId': [{'Value': self.account, 'Comparison': 'EQUALS'}]}, MaxResults=100)
                for finding in page.get('Findings', []):
                    if (finding.get('AwsAccountId') == self.account and finding.get('RecordState') == 'ACTIVE'
                            and finding.get('Severity', {}).get('Label') == 'CRITICAL'
                            and finding.get('Workflow', {}).get('Status') not in {'SUPPRESSED', 'RESOLVED'}):
                        critical_ids.add(finding['Id'])
                        cves.update(v.get('Id', '') for v in finding.get('Vulnerabilities', []))
        else:
            return []
        valid = sorted(c for c in cves if isinstance(c, str) and CVE.fullmatch(c))
        if not valid:
            if critical_ids:
                case_id = digest('no-cve/' + encoded({'account': self.account, 'region': region,
                    'source': event['source'], 'findings': sorted(critical_ids)}))
                case = self.store.get('case#' + case_id)
                if not case:
                    case = {'case_id': case_id, 'account': self.account, 'region': region, 'cve': 'NO_CVE',
                        'mode': 'repair', 'status': 'blocked-no-CVE', 'created_at': int(self.now()),
                        'attempts': 0, 'reserved_usd': 0, 'operations': {}, 'targets': [], 'findings': []}
                    self.save(case)
                    self.notify('blocked-no-CVE', case)
                return [case_id]
            return []  # Lowered/resolved findings never create a repair case.
        admitted = []
        for cve in valid:
            targets, findings = self.critical_targets(cve, region)
            if not targets:
                continue
            revision = digest(encoded({'targets': sorted(t['instance_id'] for t in targets),
                'fixes': sorted((f['instance_id'], f['fix_available'],
                    sorted((p['name'], p['fixedInVersion']) for p in f['packages'])) for f in findings)}))
            case_id = digest('/'.join((self.account, region, cve, revision)))
            case = self.store.get('case#' + case_id)
            if not case:
                case = {'case_id': case_id, 'account': self.account, 'region': region, 'cve': cve,
                    'mode': 'repair', 'revision': revision, 'status': 'pending', 'created_at': int(self.now()),
                    'attempts': 0, 'reserved_usd': 0, 'operations': {}}
            case.update(targets=targets, findings=findings, finding_arns=sorted(set(case.get('finding_arns', [])) | {f['finding_arn'] for f in findings}))
            self.save(case)
            if not case.get('notice_sent'):
                self.notify('CRITICAL', case)
                case['notice_sent'] = True
                self.save(case)
            admitted.append(case_id)
        return admitted

    def build(self, build_id):
        require(isinstance(build_id, str) and build_id.startswith(self.env['PROJECT_NAME'] + ':'))
        rows = self.api('codebuild').batch_get_builds(ids=[build_id]).get('builds', [])
        require(len(rows) == 1 and rows[0].get('id') == build_id
            and rows[0].get('projectName') == self.env['PROJECT_NAME'])
        return rows[0]

    def recover_start(self, case):
        ids = self.api('codebuild').list_builds_for_project(projectName=self.env['PROJECT_NAME'], sortOrder='DESCENDING').get('ids', [])
        matches = []
        for build_id in ids[:100]:
            build = self.build(build_id)
            variables = {v['name']: v.get('value') for v in build.get('environment', {}).get('environmentVariables', [])}
            if variables.get('PATCHDOCTOR_CASE_ID') == case['case_id'] and variables.get('PATCHDOCTOR_ATTEMPT') == str(case['attempts']):
                matches.append(build_id)
        require(len(matches) <= 1)
        if matches:
            case['build_id'] = matches[0]
            case['status'] = 'running'
            self.save(case)
        # No found build is NOT permission to replay an uncertain StartBuild.
        return bool(matches)

    def start(self, case):
        require(case['attempts'] < 2 and case['reserved_usd'] + 25 <= 50)
        today = datetime.datetime.fromtimestamp(self.now(), datetime.timezone.utc).date().isoformat()
        daily = self.store.get('daily#' + today) or {'starts': 0}
        if daily['starts'] >= 4:
            case['status'] = 'daily-budget-wait'
            case['retry_after'] = int(datetime.datetime.combine(
                datetime.date.fromisoformat(today) + datetime.timedelta(days=1),
                datetime.time(), datetime.timezone.utc).timestamp())
            self.save(case)
            self.notify('daily-budget-exhausted', case)
            return
        # Reserve before any StartBuild. A crash consumes the reservation conservatively.
        daily['starts'] += 1
        self.store.put('daily#' + today, daily)
        case['attempts'] += 1
        case['reserved_usd'] += 25
        case.update(status='starting', started_at=int(self.now()), deadline_unix=int(self.now()) + 3600,
            start_token=digest(case['case_id'] + '/' + str(case['attempts'])), start_attempted=False)
        case.pop('outcome', None)
        self.save(case)
        self.submit(case)

    def submit(self, case):
        require(case['status'] == 'starting' and case.get('start_attempted') is False)
        case['start_attempted'] = True
        self.save(case)
        try:
            result = self.api('codebuild').start_build(projectName=self.env['PROJECT_NAME'],
                idempotencyToken=case['start_token'], environmentVariablesOverride=[
                    {'name': 'PATCHDOCTOR_CASE_ID', 'value': case['case_id'], 'type': 'PLAINTEXT'},
                    {'name': 'PATCHDOCTOR_MODE', 'value': case['mode'], 'type': 'PLAINTEXT'},
                    {'name': 'PATCHDOCTOR_ATTEMPT', 'value': str(case['attempts']), 'type': 'PLAINTEXT'}])
        except Exception as error:
            code = error_code(error)
            if code in START_REJECTED:
                case.update(status='blocked-start-rejected', no_build=True, rejection_code=code)
                self.save(case)
                self.notify_once('start-rejected', case, 'start_notice_sent')
            else:
                case['status'] = 'start-uncertain'
                self.save(case)
                self.notify_once('start-uncertain', case, 'start_notice_sent')
            return
        require(result['build'].get('projectName') == self.env['PROJECT_NAME'])
        case.update(build_id=result['build']['id'], status='running')
        self.save(case)

    def reconcile(self):
        cases = self.store.cases()
        busy = False
        for case in cases:
            if case['status'] == 'daily-budget-wait' and self.now() >= case['retry_after']:
                case['status'] = 'pending'
                self.save(case)
            if case['status'] in {'starting', 'start-uncertain'}:
                if case['status'] == 'starting' and case.get('start_attempted') is False:
                    self.submit(case)  # Proven pre-send intent, not an uncertain replay.
                else:
                    self.notify_once('start-uncertain', case, 'start_notice_sent')
                    self.recover_start(case)
                busy = True
            if case.get('status') == 'running':
                build = self.build(case['build_id'])
                if build['buildStatus'] not in BUILD_TERMINAL:
                    busy = True
                    if self.now() >= case['deadline_unix']:
                        self.api('codebuild').stop_build(id=case['build_id'])
                    continue
                case['build_status'] = build['buildStatus']
                # A completed agent outcome is evidence, never a claim that AWS closed the finding.
                if case.get('outcome'):
                    case['status'] = 'awaiting-native-closure' if case['outcome'] in {'patched', 'mitigated'} else case['outcome']
                else:
                    case['status'] = 'pending' if case['attempts'] < 2 else 'budget-exhausted'
                self.save(case)
                if case.get('outcome'):
                    self.notify_once(case['outcome'], case, 'outcome_notice_sent')
                if case['status'] == 'budget-exhausted':
                    self.notify('budget-exhausted', case)
        for case in cases:
            if case.get('status') == 'awaiting-native-closure':
                targets, _ = self.critical_targets(case['cve'], case['region'])
                if not targets and self.native_closed(case):
                    case['status'] = 'resolved'
                    self.save(case)
        if busy:
            return
        for case in sorted(cases, key=lambda c: c['created_at']):
            if case['status'] == 'pending' and (case['mode'] == 'self-test' or self.config().get('armed') is True):
                if case['mode'] != 'self-test':
                    targets, findings = self.critical_targets(case['cve'], case['region'])
                    if not targets:
                        case['status'] = 'blocked-no-current-critical-target'
                        self.save(case)
                        continue
                    case.update(targets=targets, findings=findings)
                self.start(case)
                break

    def native_closed(self, case):
        arns = case.get('finding_arns', [])
        if not arns:
            return False
        for arn in arns:
            page = self.api('inspector2', case['region']).list_findings(filterCriteria={
                'findingArn': [{'comparison': 'EQUALS', 'value': arn}]}, maxResults=100)
            matches = [f for f in page.get('findings', []) if f.get('findingArn') == arn
                and f.get('awsAccountId') == self.account]
            if len(matches) != 1 or matches[0].get('status') != 'CLOSED':
                return False
        return True

    def authorize(self, event):
        require(isinstance(event.get('case_id'), str) and DIGEST.fullmatch(event['case_id']))
        case = self.store.get('case#' + event['case_id'])
        require(case and case.get('status') == 'running' and case.get('build_id') == event.get('build_id')
            and self.now() < case['deadline_unix'])
        build = self.build(event['build_id'])
        variables = {v['name']: v.get('value') for v in build.get('environment', {}).get('environmentVariables', [])}
        require(build.get('buildStatus') == 'IN_PROGRESS' and variables.get('PATCHDOCTOR_CASE_ID') == case['case_id']
            and variables.get('PATCHDOCTOR_MODE') == case['mode'])
        return case

    def operation(self, case, operation_id):
        require(isinstance(operation_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', operation_id))
        return 'op#' + case['case_id'] + '#' + digest(operation_id)

    def command(self, case, event):
        require(case['mode'] == 'repair')
        target, script = event.get('target', {}), event.get('script', '')
        require(target in [{k: t[k] for k in ('instance_id', 'region')} for t in case['targets']])
        inspect = event.get('inspect') is True
        require(isinstance(script, str) and (inspect or bool(script)) and len(script.encode()) <= 16384 and '\x00' not in script)
        key = self.operation(case, event.get('operation_id'))
        body_hash = digest(encoded({'target': target, 'script': script, 'inspect': inspect}))
        existing = self.store.get(key)
        if existing:
            require(existing['body_hash'] == body_hash)
            if existing['state'] != 'prepared':
                return self.op_receipt(existing)
        targets, _ = self.critical_targets(case['cve'], target['region'])
        require(any(t['instance_id'] == target['instance_id'] for t in targets))
        self.describe_target(target['instance_id'], target['region'])
        op = existing or {'operation_id': event['operation_id'], 'body_hash': body_hash, 'script_sha256': digest(script),
            'target': target, 'state': 'prepared', 'comment': 'PatchDoctor-' + digest(key)[:48],
            'document': self.env['INSPECT_DOCUMENT_NAME'] if inspect else self.env['COMMAND_DOCUMENT_NAME'],
            'created_at': int(self.now()), 'build_id': case['build_id']}
        if not existing:
            self.store.put(key, op)
        case['operations'][event['operation_id']] = key
        self.save(case)
        if not op.get('request_version_id'):
            authored = encoded({'case_id': case['case_id'], 'operation_id': op['operation_id'],
                'target': target, 'document': op['document'], 'script': script}).encode()
            request_key = 'commands/' + case['case_id'] + '/' + digest(key) + '/request.json'
            archived = self.api('s3').put_object(Bucket=self.env['BUCKET_NAME'], Key=request_key,
                Body=authored, ContentType='application/json', ExpectedBucketOwner=self.account,
                Metadata={'sha256': hashlib.sha256(authored).hexdigest()})
            require(isinstance(archived.get('VersionId'), str) and archived['VersionId'] not in {'', 'null'})
            op.update(request_key=request_key, request_version_id=archived['VersionId'],
                request_sha256=hashlib.sha256(authored).hexdigest())
            self.store.put(key, op)
        # The durable request archive and send intent precede every SSM send.
        op['state'] = 'send-uncertain'
        self.store.put(key, op)
        parameters = {} if inspect else {'commands': [script], 'executionTimeout': [str(min(900, max(1, int(case['deadline_unix'] - self.now()))))]}
        try:
            result = self.api('ssm', target['region']).send_command(InstanceIds=[target['instance_id']],
                DocumentName=op['document'], Parameters=parameters, Comment=op['comment'], TimeoutSeconds=60)
        except Exception:
            return self.op_receipt(op)  # Reconcile by the recorded comment; never resend an unknown outcome.
        command_id = result['Command']['CommandId']
        require(re.fullmatch(r'[0-9a-f-]{36}', command_id))
        op.update(command_id=command_id, state='submitted')
        self.store.put(key, op)
        return self.op_receipt(op)

    @staticmethod
    def op_receipt(op):
        return {k: op[k] for k in ('operation_id', 'state', 'command_id', 'target', 'script_sha256', 'status',
            'request_key', 'request_version_id', 'request_sha256') if k in op}

    def poll(self, case, event):
        require(case['mode'] == 'repair')
        key = self.operation(case, event.get('operation_id'))
        op = self.store.get(key)
        require(op and case['operations'].get(event['operation_id']) == key)
        ssm = self.api('ssm', op['target']['region'])
        if not op.get('command_id'):
            commands, token = [], None
            for _ in range(10):
                args = {'Filters': [{'key': 'InvokedAfter', 'value': datetime.datetime.fromtimestamp(
                    op['created_at'] - 60, datetime.timezone.utc).isoformat()}], 'MaxResults': 50}
                if token:
                    args['NextToken'] = token
                page = ssm.list_commands(**args)
                commands.extend(c for c in page.get('Commands', []) if c.get('Comment') == op['comment']
                    and c.get('InstanceIds') == [op['target']['instance_id']]
                    and c.get('DocumentName') == op['document'])
                token = page.get('NextToken')
                if not token:
                    break
            require(not token and len(commands) <= 1)
            if not commands:
                return self.op_receipt(op)
            op.update(command_id=commands[0]['CommandId'], state='submitted')
            self.store.put(key, op)
        try:
            result = ssm.get_command_invocation(CommandId=op['command_id'], InstanceId=op['target']['instance_id'])
        except Exception as error:
            if error_code(error) == 'InvocationDoesNotExist':
                return {**self.op_receipt(op), 'status': 'Pending'}
            raise
        status = result['Status']
        receipt = {**self.op_receipt(op), 'status': status, 'response_code': result.get('ResponseCode', -1),
            'stdout': str(result.get('StandardOutputContent', ''))[:24576],
            'stderr': str(result.get('StandardErrorContent', ''))[:8192]}
        if status in COMMAND_TERMINAL:
            receipt_key = 'commands/' + case['case_id'] + '/' + digest(key) + '.json'
            # Host roles never write into the audit bucket. Only this broker records results.
            self.api('s3').put_object(Bucket=self.env['BUCKET_NAME'], Key=receipt_key,
                Body=encoded(receipt).encode(), ContentType='application/json', ExpectedBucketOwner=self.account)
            op.update(state='terminal', status=status, receipt_key=receipt_key)
            self.store.put(key, op)
            receipt['state'] = 'terminal'
        return receipt

    def checkpoint(self, case, event):
        prefix = 'cases/' + case['case_id'] + '/builds/' + digest(case['build_id']) + '/checkpoints/'
        key, version = event.get('key', ''), event.get('version_id', '')
        require(isinstance(key, str) and key.startswith(prefix) and len(key) < 1024 and '..' not in key
            and isinstance(version, str) and version not in {'', 'null'} and len(version) < 1024)
        obj = self.api('s3').head_object(Bucket=self.env['BUCKET_NAME'], Key=key,
            VersionId=version, ExpectedBucketOwner=self.account)
        require(obj.get('VersionId') == version and 0 < obj['ContentLength'] <= 256 * 1024 * 1024
            and obj.get('ServerSideEncryption') == 'aws:kms')
        case['checkpoint'] = {'key': key, 'version_id': version}
        self.save(case)
        return {'acknowledged': True, 'checkpoint': case['checkpoint']}

    def broker(self, event):
        require(len(encoded(event).encode()) <= 24000)
        case = self.authorize(event)
        action = event.get('action')
        if action == 'get':
            return {**{k: case[k] for k in ('case_id', 'mode', 'targets', 'findings', 'cve', 'deadline_unix')},
                'model': MODEL, 'budget_usd': 25, 'attempt': case['attempts'],
                'reservation_id': case['start_token'], 'case_reserved_usd': case['reserved_usd'],
                'checkpoint': case.get('checkpoint'),
                'operations': [self.op_receipt(self.store.get(key)) for key in case['operations'].values()]}
        if action == 'command':
            return self.command(case, event)
        if action == 'poll':
            return self.poll(case, event)
        if action == 'checkpoint':
            return self.checkpoint(case, event)
        if action == 'finish':
            require(event.get('outcome') in OUTCOMES and (event['outcome'] == 'self-test-passed') == (case['mode'] == 'self-test'))
            case.update(outcome=event['outcome'], summary=summary(event.get('summary', '')))
            self.save(case)
            return {'acknowledged': True, 'outcome': case['outcome'], 'status': 'awaiting-build-completion'}
        raise ValueError('unsupported action')

    def handler(self, event):
        if 'Records' in event:
            failures, admitted = [], []
            for message in event['Records']:
                try:
                    body = json.loads(message['body'])
                    # Untrusted SQS bodies can ONLY enter native finding admission.
                    admitted.extend(self.admit(body))
                except Exception:
                    failures.append({'itemIdentifier': message['messageId']})
            self.reconcile()
            return {'batchItemFailures': failures, 'admitted': admitted}
        action = event.get('action') if not event.get('source') and not event.get('detail-type') else None
        if action == 'self_test':
            case_id = digest('PatchDoctor/SELF_TEST')
            case = self.store.get('case#' + case_id)
            if not case:
                case = {'case_id': case_id, 'account': self.account, 'region': self.region, 'cve': 'SELF_TEST',
                    'mode': 'self-test', 'status': 'pending', 'created_at': int(self.now()), 'attempts': 0,
                    'reserved_usd': 0, 'operations': {}, 'targets': [], 'findings': []}
                self.save(case)
            self.reconcile()
            return {'case_id': case_id, 'armed': self.config().get('armed') is True}
        if action == 'arm':
            case = self.store.get('case#' + digest('PatchDoctor/SELF_TEST'))
            require(case and case.get('status') == 'self-test-passed' and case.get('outcome') == 'self-test-passed'
                and self.build(case['build_id'])['buildStatus'] == 'SUCCEEDED')
            cfg = self.config()
            cfg['armed'] = True
            self.store.put('config', cfg)
            self.reconcile()
            return {'armed': True}
        admitted = self.admit(event) if event.get('source') in {'aws.inspector2', 'aws.securityhub'} else []
        self.reconcile()
        return {'admitted': admitted, 'armed': self.config().get('armed') is True}


def handler(event, context):
    return Doctor().handler(event)


def broker(event, context):
    try:
        return Doctor().broker(event)
    except Exception as error:
        # Raw command data is returned only on the authenticated worker path, never errors/logs.
        return {'refused': True, 'error_type': type(error).__name__}
