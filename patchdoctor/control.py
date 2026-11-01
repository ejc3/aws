"""PatchDoctor's durable admission controller and worker-only command broker."""
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from urllib.parse import quote

import triage

MODEL = 'us.anthropic.claude-opus-5-5'
CVE = re.compile(r'CVE-[0-9]{4}-[0-9]{4,10}\Z')
IID = re.compile(r'i-[0-9a-f]{8}(?:[0-9a-f]{9})?\Z')
DIGEST = re.compile(r'[0-9a-f]{64}\Z')
BUILD_TERMINAL = {'SUCCEEDED', 'FAILED', 'FAULT', 'STOPPED', 'TIMED_OUT'}
COMMAND_TERMINAL = {'Success', 'Failed', 'Cancelled', 'TimedOut', 'DeliveryTimedOut', 'ExecutionTimedOut', 'Undeliverable', 'Terminated'}
OUTCOMES = {'patched', 'mitigated', 'blocked', 'self-test-passed'}
START_REJECTED = {'InvalidInputException', 'ResourceNotFoundException', 'AccountLimitExceededException',
    'AccessDeniedException', 'NotAuthorizedException'}
ACTIVE_CASES = {'pending', 'running', 'starting', 'start-uncertain', 'daily-budget-wait', 'awaiting-native-closure', 'deferred'}
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
        if pk.startswith('case#') and (body.get('status') in ACTIVE_CASES or body.get('outstanding_operations')):
            kwargs['Item'].update(record_kind={'S': 'active-case'}, created_at={'N': str(body['created_at'])})
        if revision:
            kwargs.update(ConditionExpression='revision = :old', ExpressionAttributeValues={':old': {'N': str(revision)}})
        else:
            kwargs['ConditionExpression'] = 'attribute_not_exists(pk)'
        self.ddb.put_item(**kwargs)
        value['_revision'] = revision + 1
        return value

    def cases(self):
        result, start = [], None
        while True:
            kwargs = {'TableName': self.table, 'IndexName': 'active-cases', 'Limit': 100,
                'KeyConditionExpression': 'record_kind = :kind',
                'ExpressionAttributeValues': {':kind': {'S': 'active-case'}}}
            if start:
                kwargs['ExclusiveStartKey'] = start
            page = self.ddb.query(**kwargs)
            for item in page.get('Items', []):
                value = self.get(item['pk']['S'])
                if value and (value.get('status') in ACTIVE_CASES or value.get('outstanding_operations')):
                    result.append(value)
            start = page.get('LastEvaluatedKey')
            if not start:
                return result


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

    def active_cases(self):
        # Strong pointers cover GSI propagation delay for active builds and commands.
        result = {}
        for key in ('active-case', 'host-operation'):
            pointer = self.store.get(key)
            if pointer and pointer.get('case_id'):
                case = self.store.get('case#' + pointer['case_id'])
                require(case)
                result[case['case_id']] = case
        for case in self.store.cases():
            if case['case_id'] not in result:
                result[case['case_id']] = case
        return list(result.values())

    def release(self, key, case_id, operation_id=None):
        pointer = self.store.get(key)
        if pointer and pointer.get('case_id') == case_id and (operation_id is None or pointer.get('operation_id') == operation_id):
            pointer.update(case_id=None, operation_id=None)
            self.store.put(key, pointer)

    def operation_index(self, case, operation_id, key, terminal=False):
        for _ in range(4):
            current = self.store.get('case#' + case['case_id'])
            require(current)
            current.setdefault('operations', {})[operation_id] = key
            pending = set(current.get('outstanding_operations', []))
            pending.discard(operation_id) if terminal else pending.add(operation_id)
            current['outstanding_operations'] = sorted(pending)
            try:
                self.save(current)
                case.clear()
                case.update(current)
                return
            except Exception as error:
                if error_code(error) != 'ConditionalCheckFailedException':
                    raise
        raise RuntimeError('case operation index conflict')

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
        value = self.store.get('config') or {}
        # An older armed record cannot bypass the new two-model commissioning gate.
        return {**value, 'armed': value.get('armed') is True and value.get('gate_version') == 2}

    def triage_input(self, case):
        """Only re-read native Inspector/EC2 facts can establish Ubuntu package identity."""
        facts = {'cve': case['cve'], 'targets': sorted(case['targets'], key=lambda t: t['instance_id']), 'packages': [],
            'known_exploit': None, 'fix_available': None}
        findings = case.get('findings', [])
        if any(f.get('exploit_available') == 'YES' for f in findings):
            facts['known_exploit'] = True
        if any(f.get('fix_available') in {'YES', 'PARTIAL'} for f in findings):
            facts['fix_available'] = True
        try:
            require(findings and all(f.get('triage_complete') is True for f in findings))
            targets = {t['instance_id']: t for t in case['targets']}
            releases = {'UBUNTU_20_04': 'focal', 'UBUNTU_22_04': 'jammy',
                'UBUNTU_24_04': 'noble', 'UBUNTU_26_04': 'resolute'}
            architectures = {'ARM64': 'arm64', 'arm64': 'arm64', 'X86_64': 'amd64', 'x86_64': 'amd64', 'amd64': 'amd64'}
            packages = []
            for finding in findings:
                require(finding.get('source') == 'UBUNTU_CVE' and finding.get('platform') in releases)
                target = targets[finding['instance_id']]
                for package in finding['packages']:
                    name, version, release = package['name'], package['version'], package['release']
                    arch = architectures[package['arch']]
                    require(package['packageManager'] == 'OS' and architectures[target['architecture']] == arch)
                    require(re.fullmatch(r'[a-z0-9][a-z0-9.+-]{0,127}', name)
                        and re.fullmatch(r'[A-Za-z0-9.+:~_-]{1,128}', version)
                        and (not release or re.fullmatch(r'[A-Za-z0-9.+~_-]{1,128}', release)))
                    require('fcvm' not in version.lower() and not (
                        target['name'].startswith('fcvm-metal-') and name.startswith('linux')))
                    if release and not version.endswith('-' + release):
                        require('-' not in version)
                        version += '-' + release
                    epoch = package.get('epoch')
                    require(type(epoch) is int and 0 <= epoch < 100000)
                    if ':' in version:
                        require(version.split(':', 1)[0] == str(epoch))
                    elif epoch:
                        version = str(epoch) + ':' + version
                    purl = 'pkg:deb/ubuntu/' + name + '@' + quote(version, safe='.+~_-')
                    purl += '?arch=' + arch + '&distro=' + releases[finding['platform']]
                    packages.append({'instance_id': target['instance_id'], 'origin': 'ubuntu_archive', 'purl': purl})
            require({p['instance_id'] for p in packages} == set(targets) and len(packages) <= 32)
            facts['packages'] = sorted(packages, key=lambda p: (p['instance_id'], p['purl']))
            if all(f.get('exploit_available') == 'NO' for f in findings):
                facts['known_exploit'] = False
            if all(f.get('fix_available') == 'NO' for f in findings):
                facts['fix_available'] = False
        except (KeyError, TypeError, ValueError):
            pass  # Missing, truncated, custom or ambiguous facts always go to Opus.
        return facts

    def assess(self, case):
        facts = self.triage_input(case)
        revision = digest(encoded(facts))
        if (case.get('triage_revision') == revision and case.get('triage')
                and (case['triage'].get('decision') != 'defer' or self.now() < case.get('recheck_at', 0))):
            return case['triage']['decision'], False
        verdict = triage.classify(facts)
        case.update(triage=verdict, triage_revision=revision, triage_checked_at=int(self.now()))
        if verdict['decision'] == 'defer':
            # The scheduler observes this before one hour, not after it.
            case.update(status='deferred', recheck_at=int(self.now()) + min(3300, verdict['recheck_after_seconds']))
        self.save(case)
        if verdict['decision'] == 'defer':
            self.notify('deferred-recheck', case)
            case['triage_notice_at'] = case['triage_checked_at']
            self.save(case)
        return verdict['decision'], True

    def status(self):
        cfg, cases = self.config(), self.active_cases()
        return {'connected': True, 'armed': cfg['armed'], 'paused': cfg.get('paused_new_runs') is True,
            'models': {'doctor': MODEL, 'triage': triage.MODEL},
            'limits': {'worker_minutes': 60, 'attempts_per_case': 2, 'starts_per_day': 4,
                'model_allowance_usd': 25, 'case_allowance_usd': 50},
            'setup': {'tenant_connected': False, 'phone_verified': False, 'mac_verified': False},
            'state': {'active_case_id': (self.store.get('active-case') or {}).get('case_id'),
                'pending': sum(c['status'] == 'pending' for c in cases),
                'deferred': sum(c['status'] == 'deferred' for c in cases)},
            'cases': [{k: c[k] for k in ('case_id', 'cve', 'status', 'created_at', 'updated_at', 'attempts') if k in c}
                for c in sorted(cases, key=lambda c: c['created_at'])[:50]]}

    def admin(self, event):
        action, mutation = event['action'], event.get('mutationId')
        require(isinstance(mutation, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', mutation))
        require(set(event) <= {'action', 'mutationId', 'paused', 'case_id'})
        if action == 'pause_new_runs':
            require(type(event.get('paused')) is bool)
        if event.get('case_id') is not None:
            require(action == 'recheck' and DIGEST.fullmatch(event['case_id']))
        request_hash = digest(encoded(event))
        key = 'admin#' + digest(mutation)
        receipt = self.store.get(key)
        if receipt:
            require(receipt['request_sha256'] == request_hash)
            if receipt.get('state') == 'completed':
                return receipt['result']
        else:
            receipt = {'request_sha256': request_hash, 'state': 'prepared', 'action': action,
                'mutationId': mutation, 'created_at': int(self.now())}
            self.store.put(key, receipt)
        result = self.admin_effect(event, key, receipt)
        if result.get('state') != 'uncertain':
            receipt.update(state='completed', result=result)
            self.store.put(key, receipt)  # Durable exact replay receipt BEFORE acknowledging.
        return result

    def admin_effect(self, event, key, receipt):
        action, mutation = event['action'], event['mutationId']
        result = {'action': action, 'mutationId': mutation, 'state': 'completed'}
        cfg = self.config()
        previous = cfg.get('last_admin_effect', {})
        if previous.get('mutationId') == mutation:
            return previous
        if action in {'self_test', 'haiku_self_test', 'triage_self_test'}:
            if 'haiku_result' not in receipt:
                if receipt.get('haiku_attempted'):
                    return {**result, 'state': 'uncertain'}
                receipt['haiku_attempted'] = True
                self.store.put(key, receipt)
                try:
                    proof = triage.self_test()
                except Exception as error:
                    proof = {'ok': False, 'model': triage.MODEL, 'error_type': type(error).__name__}
                receipt['haiku_result'] = {**proof, 'checked_at': int(self.now())}
                self.store.put(key, receipt)
            cfg['haiku_self_test'] = receipt['haiku_result']
            self.store.put('config', cfg)
            result['haiku'] = receipt['haiku_result']
            if action != 'self_test' or receipt['haiku_result'].get('ok') is not True:
                return result
            case_id = digest('PatchDoctor/SELF_TEST')
            case = self.store.get('case#' + case_id)
            if not case:
                case = {'case_id': case_id, 'account': self.account, 'region': self.region, 'cve': 'SELF_TEST',
                    'mode': 'self-test', 'status': 'pending', 'created_at': int(self.now()), 'attempts': 0,
                    'reserved_usd': 0, 'operations': {}, 'targets': [], 'findings': []}
                self.save(case)
            self.reconcile()
            return {**result, 'case_id': case_id, 'armed': self.config()['armed']}
        if action == 'recheck':
            if 'case_ids' not in receipt:
                cases = [self.store.get('case#' + event['case_id'])] if event.get('case_id') else self.active_cases()
                require(all(cases))
                receipt['case_ids'] = [c['case_id'] for c in cases if c['mode'] == 'repair'
                    and c['status'] in {'pending', 'deferred', 'blocked-no-current-critical-target'}]
                self.store.put(key, receipt)
            changed = 0
            for case_id in receipt['case_ids']:
                case = self.store.get('case#' + case_id)
                effect_id = digest(mutation)
                if case.get('recheck_effects', {}).get(effect_id) is True:
                    continue
                require(case['status'] in {'pending', 'deferred', 'blocked-no-current-critical-target'})
                case.update(status='pending', recheck_at=0)
                case.setdefault('recheck_effects', {})[effect_id] = True
                case.pop('triage', None)
                self.save(case)
                changed += 1
            if changed:
                self.reconcile()
            return {**result, 'case_count': len(receipt['case_ids'])}
        require(action in {'arm', 'pause_new_runs'})
        if receipt.get('config_attempted'):
            return {**result, 'state': 'uncertain'}  # A later admin action must never be overwritten.
        if action == 'arm':
            case = self.store.get('case#' + digest('PatchDoctor/SELF_TEST'))
            require(case and case.get('status') == 'self-test-passed' and case.get('outcome') == 'self-test-passed'
                and self.build(case['build_id'])['buildStatus'] == 'SUCCEEDED'
                and cfg.get('haiku_self_test', {}).get('ok') is True
                and cfg['haiku_self_test'].get('model') == triage.MODEL)
            cfg.update(armed=True, gate_version=2, paused_new_runs=False)
        else:
            cfg['paused_new_runs'] = event['paused']
        receipt['config_attempted'] = True
        self.store.put(key, receipt)
        result.update(armed=cfg.get('armed') is True and cfg.get('gate_version') == 2,
            paused=cfg.get('paused_new_runs') is True)
        cfg['last_admin_effect'] = result
        self.store.put('config', cfg)  # Effect and its recovery marker are ONE atomic item write.
        if action == 'arm':
            self.reconcile()
        return result

    def describe_target(self, instance_id, region):
        require(region in self.regions and isinstance(instance_id, str) and IID.fullmatch(instance_id))
        page = self.api('ec2', region).describe_instances(Filters=[{'Name': 'instance-id', 'Values': [instance_id]}])
        rows = [(r, i) for r in page.get('Reservations', []) for i in r.get('Instances', [])]
        require(len(rows) == 1)
        reservation, instance = rows[0]
        tags = {t['Key']: t['Value'] for t in instance.get('Tags', [])}
        require(reservation.get('OwnerId') == self.account and instance['InstanceId'] == instance_id
            and tags.get('Name') in self.names and instance.get('State', {}).get('Name') == 'running')
        return {'instance_id': instance_id, 'region': region, 'name': tags['Name'],
            'architecture': instance.get('Architecture', '')}

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
                    raw_packages = advisory.get('vulnerablePackages', [])
                    packages = [{**{key: str(package.get(key, ''))[:128] for key in
                        ('name', 'fixedInVersion', 'version', 'packageManager', 'arch', 'release')},
                        'epoch': package.get('epoch')}
                        for package in raw_packages[:8]]
                    urls = [str(url)[:512] for url in [advisory.get('sourceUrl', ''),
                        *advisory.get('referenceUrls', [])[:2]] if str(url).startswith('https://')]
                    findings.append({'finding_arn': finding['findingArn'], 'instance_id': instance_id,
                        'fix_available': finding.get('fixAvailable', 'UNKNOWN'),
                        'exploit_available': finding.get('exploitAvailable', 'UNKNOWN'),
                        'title': str(finding.get('title', ''))[:200],
                        'description': str(finding.get('description', ''))[:800],
                        'packages': packages, 'source_urls': urls,
                        'source': advisory.get('source', ''),
                        'platform': resource.get('details', {}).get('awsEc2Instance', {}).get('platform', ''),
                        'triage_complete': 0 < len(raw_packages) <= 8 and all(
                            len(str(p.get(k, ''))) <= 128 for p in raw_packages
                            for k in ('name', 'version', 'packageManager', 'arch', 'release'))})
            token = page.get('nextToken')
            if not token:
                if len(findings) > 50:
                    for finding in findings[:50]:
                        finding['triage_complete'] = False
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
            revision_id = digest('/'.join((self.account, region, cve, revision)))
            pointer_key = 'revision#' + revision_id
            pointer = self.store.get(pointer_key) or {'case_id': revision_id, 'generation': 0}
            case_id = pointer['case_id']
            case = self.store.get('case#' + case_id)
            if case and case['status'] == 'resolved':
                # Native ACTIVE was re-read above: this is a new occurrence, not an old delivery.
                pointer.update(generation=pointer['generation'] + 1)
                pointer['case_id'] = digest(revision_id + '/' + str(pointer['generation']))
                case_id = pointer['case_id']
                case = self.store.get('case#' + case_id)
            if not case:
                case = {'case_id': case_id, 'account': self.account, 'region': region, 'cve': cve,
                    'mode': 'repair', 'revision': revision, 'generation': pointer['generation'],
                    'status': 'pending', 'created_at': int(self.now()),
                    'attempts': 0, 'reserved_usd': 0, 'operations': {}}
            case.update(targets=targets, findings=findings, finding_arns=sorted(set(case.get('finding_arns', [])) | {f['finding_arn'] for f in findings}))
            if case['status'] == 'blocked-no-current-critical-target':
                case['status'] = 'pending'
            if case['status'] == 'deferred' and case.get('triage_revision') != digest(encoded(self.triage_input(case))):
                case['status'] = 'pending'  # A new exploit/fix/native package revision bypasses an old deferral.
            self.save(case)
            # Case first, then deterministic generation pointer: a crash can only rediscover it.
            self.store.put(pointer_key, pointer)
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
        active = self.store.get('active-case') or {'case_id': None}
        if active.get('case_id') not in {None, case['case_id']}:
            return
        active['case_id'] = case['case_id']
        self.store.put('active-case', active)
        today = datetime.datetime.fromtimestamp(self.now(), datetime.timezone.utc).date().isoformat()
        daily = self.store.get('daily#' + today) or {'starts': 0}
        if daily['starts'] >= 4:
            case['status'] = 'daily-budget-wait'
            case['retry_after'] = int(datetime.datetime.combine(
                datetime.date.fromisoformat(today) + datetime.timedelta(days=1),
                datetime.time(), datetime.timezone.utc).timestamp())
            self.save(case)
            self.release('active-case', case['case_id'])
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
        case.pop('build_status', None)
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
                self.release('active-case', case['case_id'])
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
        cases = self.active_cases()
        observed = []
        pointer = self.store.get('active-case') or {}
        for case in cases:
            if (case['case_id'] == pointer.get('case_id') and case['status'] not in {'running', 'starting', 'start-uncertain'} and
                    (case.get('build_status') in BUILD_TERMINAL or case.get('no_build') is True)):
                self.release('active-case', case['case_id'])
        busy = False
        for case in cases:
            try:
                self.reconcile_operations(case)
            except Exception as error:
                self.observer_failure(case, error)
                continue
            observed.append(case)
            if case['status'] == 'deferred':
                if self.now() >= case['recheck_at']:
                    try:
                        # Evidence checks do not acquire the repair/command lease.
                        targets, findings = self.critical_targets(case['cve'], case['region'])
                        if not targets:
                            case['status'] = 'blocked-no-current-critical-target'
                            self.save(case)
                        else:
                            case.update(status='pending', targets=targets, findings=findings)
                            self.assess(case)
                    except Exception as error:
                        self.observer_failure(case, error)
                        continue
                elif case.get('triage_notice_at') != case.get('triage_checked_at'):
                    self.notify('deferred-recheck', case)
                    case['triage_notice_at'] = case['triage_checked_at']
                    self.save(case)
            if case['status'] == 'daily-budget-wait' and self.now() >= case['retry_after']:
                case['status'] = 'pending'
                self.save(case)
                self.release('active-case', case['case_id'])
            if case['status'] in {'starting', 'start-uncertain'}:
                if case['status'] == 'starting' and case.get('start_attempted') is False:
                    self.submit(case)  # Proven pre-send intent, not an uncertain replay.
                else:
                    self.notify_once('start-uncertain', case, 'start_notice_sent')
                    self.recover_start(case)
                busy = True
            if case.get('status') == 'running':
                try:
                    build = self.build(case['build_id'])
                except Exception as error:
                    self.observer_failure(case, error)
                    continue
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
                self.release('active-case', case['case_id'])
                if case.get('outcome'):
                    self.notify_once(case['outcome'], case, 'outcome_notice_sent')
                if case['status'] == 'budget-exhausted':
                    self.notify('budget-exhausted', case)
        for case in observed:
            if case.get('status') == 'awaiting-native-closure':
                try:
                    targets, _ = self.critical_targets(case['cve'], case['region'])
                    if not targets and not case.get('outstanding_operations') and self.native_closed(case):
                        case['status'] = 'resolved'
                        self.save(case)
                except Exception as error:
                    self.observer_failure(case, error)
        if busy or (self.store.get('host-operation') or {}).get('case_id'):
            return
        pointer = self.store.get('active-case') or {}
        for case in sorted(observed, key=lambda c: (c['case_id'] != pointer.get('case_id'), c['created_at'])):
            if (case['status'] == 'pending' and not case.get('outstanding_operations')
                    and (case['mode'] == 'self-test' or self.config().get('armed') is True)):
                if case['mode'] != 'self-test':
                    try:
                        targets, findings = self.critical_targets(case['cve'], case['region'])
                    except Exception as error:
                        self.observer_failure(case, error)
                        continue
                    if not targets:
                        case['status'] = 'blocked-no-current-critical-target'
                        self.save(case)
                        self.release('active-case', case['case_id'])
                        continue
                    case.update(targets=targets, findings=findings)
                    decision, assessed = self.assess(case)
                    if decision == 'defer':
                        return  # A deferred case cannot start a repair worker.
                    if self.config().get('paused_new_runs') is True:
                        if assessed:
                            return
                        continue  # Pausing new repairs preserves native/triage rechecks.
                self.start(case)
                break

    def observer_failure(self, case, error):
        for key in ('active-case', 'host-operation'):
            if (self.store.get(key) or {}).get('case_id') == case['case_id']:
                raise error  # Safety observations for an active build/write must fail closed.
        # An unrelated historical observer must not stop admission of other critical repairs.
        try:
            self.notify_once('observation-attention', case, 'observer_notice_sent')
        except Exception:
            pass  # The case remains indexed; next scheduled observation retries its notice.

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

    def reconcile_operations(self, case):
        # Existing command observation is independent of the worker/model lease.
        operations = set(case.get('outstanding_operations', []))
        pointer = self.store.get('host-operation') or {}
        if pointer.get('case_id') == case['case_id']:
            operations.add(pointer['operation_id'])
        for operation_id in sorted(operations):
            key = self.operation(case, operation_id)
            op = self.store.get(key)
            require(op and case['operations'].get(operation_id) == key)
            if op['state'] == 'terminal':
                self.archive_terminal(case, key, op, {'status': op['status']})
            elif op['state'] == 'prepared':
                if self.build(op['build_id'])['buildStatus'] not in BUILD_TERMINAL:
                    continue
                # Prepared proves SendCommand was never attempted. Do not replay it.
                self.archive_terminal(case, key, op, {'status': 'Cancelled', 'response_code': -1,
                    'stdout': '', 'stderr': '', 'reason': 'worker-ended-before-send'})
            else:
                self.poll(case, {'operation_id': operation_id})

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

    def require_worker(self, case, event):
        token = event.get('worker_capability', '')
        require(isinstance(token, str) and DIGEST.fullmatch(token)
            and case.get('worker_capability_build_id') == case['build_id']
            and hmac.compare_digest(case.get('worker_capability_hash', ''), digest(token)))

    def worker_claim(self, event):
        token = secrets.token_hex(32)
        for _ in range(4):
            current = self.authorize(event)
            require(current.get('worker_capability_build_id') != current['build_id'])
            current.update(worker_capability_build_id=current['build_id'], worker_capability_hash=digest(token))
            try:
                self.save(current)
                return {'worker_capability': token}
            except Exception as error:
                if error_code(error) != 'ConditionalCheckFailedException':
                    raise
        raise RuntimeError('worker claim revision conflict')

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
        if not inspect:
            targets, _ = self.critical_targets(case['cve'], target['region'])
            require(any(t['instance_id'] == target['instance_id'] for t in targets))
        self.describe_target(target['instance_id'], target['region'])
        op = existing or {'operation_id': event['operation_id'], 'body_hash': body_hash, 'script_sha256': digest(script),
            'target': target, 'state': 'prepared', 'comment': 'PatchDoctor-' + digest(key)[:48],
            'document': self.env['INSPECT_DOCUMENT_NAME'] if inspect else self.env['COMMAND_DOCUMENT_NAME'],
            'created_at': int(self.now()), 'build_id': case['build_id']}
        if not existing:
            self.store.put(key, op)
        self.operation_index(case, event['operation_id'], key)
        if not inspect:
            active = self.store.get('host-operation') or {'case_id': None}
            require(not active.get('case_id') or (active['case_id'] == case['case_id']
                and active.get('operation_id') == event['operation_id']))
            active.update(case_id=case['case_id'], operation_id=event['operation_id'])
            self.store.put('host-operation', active)
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
                args = {'InstanceId': op['target']['instance_id'], 'Filters': [
                    {'key': 'InvokedAfter', 'value': datetime.datetime.fromtimestamp(
                        op['created_at'] - 60, datetime.timezone.utc).isoformat()},
                    {'key': 'DocumentName', 'value': op['document']}], 'MaxResults': 50}
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
                if not op.get('attention_sent'):
                    self.notify('command-delivery-uncertain', case)
                    op['attention_sent'] = True
                    self.store.put(key, op)
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
            self.archive_terminal(case, key, op, receipt)
            receipt['state'] = 'terminal'
        return receipt

    def archive_terminal(self, case, key, op, receipt):
        if op['state'] != 'terminal':
            receipt_key = 'commands/' + case['case_id'] + '/' + digest(key) + '.json'
            # No host role writes into the audit bucket; terminal ACK follows its archive.
            archived = self.api('s3').put_object(Bucket=self.env['BUCKET_NAME'], Key=receipt_key,
                Body=encoded(receipt).encode(), ContentType='application/json', ExpectedBucketOwner=self.account)
            require(isinstance(archived.get('VersionId'), str) and archived['VersionId'] not in {'', 'null'})
            op.update(state='terminal', status=receipt['status'], receipt_key=receipt_key,
                receipt_version_id=archived['VersionId'])
            self.store.put(key, op)
        self.operation_index(case, op['operation_id'], key, terminal=True)
        self.release('host-operation', case['case_id'], op['operation_id'])

    def checkpoint(self, case, event):
        prefix = 'cases/' + case['case_id'] + '/builds/' + digest(case['build_id']) + '/checkpoints/'
        key, version = event.get('key', ''), event.get('version_id', '')
        require(isinstance(key, str) and key.startswith(prefix) and len(key) < 1024 and '..' not in key
            and isinstance(version, str) and version not in {'', 'null'} and len(version) < 1024)
        obj = self.api('s3').head_object(Bucket=self.env['BUCKET_NAME'], Key=key,
            VersionId=version, ExpectedBucketOwner=self.account)
        require(obj.get('VersionId') == version and 0 < obj['ContentLength'] <= 256 * 1024 * 1024
            and obj.get('ServerSideEncryption') == 'aws:kms')
        checkpoint = {'key': key, 'version_id': version}
        for _ in range(4):
            current = self.authorize({'case_id': case['case_id'], 'build_id': case['build_id']})
            self.require_worker(current, event)
            current['checkpoint'] = checkpoint
            try:
                self.save(current)
                return {'acknowledged': True, 'checkpoint': checkpoint}
            except Exception as error:
                if error_code(error) != 'ConditionalCheckFailedException':
                    raise
        raise RuntimeError('checkpoint revision conflict')

    def broker(self, event):
        require(len(encoded(event).encode()) <= 24000)
        case = self.authorize(event)
        action = event.get('action')
        if action == 'worker_claim':
            return self.worker_claim(event)
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
            self.require_worker(case, event)
            return self.checkpoint(case, event)
        if action == 'finish':
            require(event.get('outcome') in OUTCOMES and (event['outcome'] == 'self-test-passed') == (case['mode'] == 'self-test'))
            text = summary(event.get('summary', ''))
            for _ in range(4):
                current = self.authorize(event)
                self.require_worker(current, event)
                current.update(outcome=event['outcome'], summary=text)
                try:
                    self.save(current)
                    return {'acknowledged': True, 'outcome': current['outcome'], 'status': 'awaiting-build-completion'}
                except Exception as error:
                    if error_code(error) != 'ConditionalCheckFailedException':
                        raise
            raise RuntimeError('finish revision conflict')
        raise ValueError('unsupported action')

    def admit_records(self, event):
        require(isinstance(event.get('Records'), list))
        failures, admitted, completed = [], [], []
        for message in event['Records']:
            try:
                body = json.loads(message['body'])
                # Untrusted SQS bodies can ONLY enter native finding admission.
                admitted.extend(self.admit(body))
                completed.append(message['messageId'])
            except Exception:
                failures.append({'itemIdentifier': message['messageId']})
        return {'batchItemFailures': failures, 'admitted': admitted}, completed

    def admission(self, event):
        result, completed = self.admit_records(event)
        if result['admitted']:
            try:
                reply = self.api('lambda').invoke(FunctionName=self.env['CONTROL_ARN'], InvocationType='Event',
                    Payload=encoded({'source': 'patchdoctor.admission', 'detail-type': 'Pending critical cases'}).encode())
                require(reply.get('StatusCode') == 202)
            except Exception:
                # Case is durable; redelivery retries only the fixed controller wakeup.
                result['batchItemFailures'].extend({'itemIdentifier': message_id} for message_id in completed)
        return result

    def handler(self, event):
        if 'Records' in event:
            result, _ = self.admit_records(event)
            self.reconcile()
            return result
        action = event.get('action') if not event.get('source') and not event.get('detail-type') else None
        if action == 'status':
            return self.status()
        if action in {'self_test', 'haiku_self_test', 'triage_self_test', 'arm', 'pause_new_runs', 'recheck'}:
            return self.admin(event)
        require(action is None)
        admitted = self.admit(event) if event.get('source') in {'aws.inspector2', 'aws.securityhub'} else []
        self.reconcile()
        return {'admitted': admitted, 'armed': self.config().get('armed') is True}


def handler(event, context):
    return Doctor().handler(event)


def admission(event, context):
    return Doctor().admission(event)


def broker(event, context):
    try:
        return Doctor().broker(event)
    except Exception as error:
        # Raw command data is returned only on the authenticated worker path, never errors/logs.
        return {'refused': True, 'error_type': type(error).__name__}
