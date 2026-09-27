"""github-app-runner: ephemeral x86 spot runners for repos other than ejc3/fcvm.

ejc3/fcvm keeps its own controller (runner-autoscale.tf): bare metal, warm reuse, leases.
The repos served here need none of that. Their jobs are ordinary Node/Playwright/pytest work
on a stock Ubuntu VM that boots in about a minute, so the policy is the simplest one that is
correct:

  * ONE runner per queued job. A `queued` delivery launches a spot VM tagged with that job's
    id; a redelivered event finds the tag and launches nothing. The runner registers
    --ephemeral, takes one job (not necessarily that one; the count is what matters), and
    powers off, and InstanceInitiatedShutdownBehavior=terminate deletes it.
  * NO reuse, ever, and nothing shared between repos: each repo has its own label, cap, PAT
    and Repo tag, and a host never outlives its job.
  * A reconcile every two minutes (EventBridge) re-derives the truth from GitHub and EC2: it
    launches for queued jobs that have no runner (a lost delivery, a spot refusal), and
    terminates hosts that never registered (BOOT_GRACE), sat idle (IDLE_LIMIT) or outlived
    any job (MAX_LIFETIME). Offline runner records left by dead hosts are deleted.

The function runs one execution at a time (reserved concurrency 1), so two deliveries can
never both decide a job is uncovered and launch twice.

The registration token is brokered exactly as for fcvm: the controller launches first, then
writes /github-runner/bootstrap/<instance-id> tagged with that instance's ARN, and the runner
instance role can read and delete only the parameter tagged with its own ARN. The token never
appears in user data.
"""
import json
import os
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3

REGION = 'us-west-1'
ROLE = 'github-app-runner'
CANONICAL_AMI_PARAM = '/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id'
LIVE_STATES = ['pending', 'running']

BOOT_GRACE = timedelta(minutes=int(os.environ.get('BOOT_GRACE_MINUTES', '10')))
IDLE_LIMIT = timedelta(minutes=int(os.environ.get('IDLE_MINUTES', '10')))
MAX_LIFETIME = timedelta(minutes=int(os.environ.get('MAX_LIFETIME_MINUTES', '180')))
VOLUME_GB = int(os.environ.get('VOLUME_GB', '80'))
RUNS_PER_STATUS = 30

ec2 = boto3.client('ec2', region_name=REGION)
ssm = boto3.client('ssm', region_name=REGION)
secrets = boto3.client('secretsmanager', region_name=REGION)
dynamodb = boto3.client('dynamodb', region_name=REGION)
cloudwatch = boto3.client('cloudwatch', region_name=REGION)

# A launch claim per job (table CLAIMS_TABLE) makes "one host per job" hold across
# invocations. DescribeInstances is eventually consistent: a redelivery seconds after a launch
# may not see the new host by its JobId tag yet. The claim is a conditional write, strongly
# consistent, so exactly one invocation launches. A definite failure releases it so the next
# round retries at once; an ambiguous one keeps it until CLAIM_SECONDS pass, by which time the
# tag is visible if a host did start.
CLAIM_SECONDS = 15 * 60
METRIC_NAMESPACE = 'GitHubAppRunner'

HERE = os.path.dirname(os.path.abspath(__file__))



# RunInstances errors that mean nothing was created (see launch()).
CAPACITY_CODES = {'InsufficientInstanceCapacity', 'Unsupported', 'SpotMaxPriceTooLow',
                  'InsufficientCapacityOnHost', 'UnfulfillableCapacity'}

def now():
    return datetime.now(timezone.utc)


def config():
    return json.loads(os.environ['REPOS']), json.loads(os.environ['LAUNCH_SUBNETS'])


# ---------------------------------------------------------------- GitHub
def github(method, path, pat, body=None):
    req = urllib.request.Request(
        'https://api.github.com' + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization': f'Bearer {pat}', 'Accept': 'application/vnd.github+json',
                 'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=8) as resp:
        raw = resp.read(4_000_000)
    return json.loads(raw) if raw else {}


def repo_pat(cfg):
    """This repo's controller token, or None while its secret has no value yet.

    The secret is a Terraform-managed CONTAINER with no Terraform-managed version (runner-repos.tf),
    so the token never enters state; its owner puts the value in by hand.
    """
    try:
        value = secrets.get_secret_value(SecretId=cfg['pat_secret'])['SecretString']
    except Exception as error:
        print(f"no controller token in {cfg['pat_secret']}: {type(error).__name__}")
        return None
    return value.strip() or None


def job_size(cfg, labels):
    """The size label a job asks for when the job is this repo's, else None.

    A job is ours only if EVERY label it requests is one our runners carry: self-hosted,
    linux, x64, the repo label and exactly one size label. So `[self-hosted, ARM64]` (fcvm's
    metal) and a size label alone never match, and a typo'd label is not silently served by
    a runner that lacks it.
    """
    want = {str(label).lower() for label in (labels or [])}
    sizes = {size.lower() for size in cfg['sizes']}
    if cfg['label'].lower() not in want:
        return None
    requested = want & sizes
    if len(requested) != 1:
        return None
    size = requested.pop()
    if not want <= {'self-hosted', 'linux', 'x64', cfg['label'].lower(), size}:
        return None
    return size


def queued_jobs(repo, cfg, pat):
    """[(job_id, size)] for this repo's queued jobs that ask for our labels."""
    found, seen = [], set()
    for status in ('queued', 'in_progress'):
        runs = github('GET', f'/repos/{repo}/actions/runs?status={status}&per_page={RUNS_PER_STATUS}', pat)
        for run in (runs.get('workflow_runs') or [])[:RUNS_PER_STATUS]:
            jobs = github('GET', f"/repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100", pat)
            for job in jobs.get('jobs') or []:
                if job.get('id') in seen or job.get('status') != 'queued':
                    continue
                seen.add(job.get('id'))
                size = job_size(cfg, job.get('labels'))
                if size:
                    found.append((str(job['id']), size))
    return found


def list_runners(repo, pat):
    """{runner name: runner record} for this repo's self-hosted runners."""
    out = {}
    for page in range(1, 6):
        body = github('GET', f'/repos/{repo}/actions/runners?per_page=100&page={page}', pat)
        runners = body.get('runners') or []
        for runner in runners:
            out[runner.get('name')] = runner
        if len(runners) < 100:
            break
    return out


def registration_token(repo, pat):
    body = github('POST', f'/repos/{repo}/actions/runners/registration-token', pat, body={})
    token = body.get('token')
    expires = datetime.fromisoformat(str(body.get('expires_at', '')).replace('Z', '+00:00'))
    if not isinstance(token, str) or not token.strip() or len(token) > 4096:
        raise RuntimeError('GitHub returned no usable registration token')
    return token, expires


# ---------------------------------------------------------------- EC2
def tag(instance, key):
    return next((t['Value'] for t in instance.get('Tags', []) if t['Key'] == key), None)


def app_instances(repo):
    pages = ec2.get_paginator('describe_instances').paginate(Filters=[
        {'Name': 'tag:Role', 'Values': [ROLE]},
        {'Name': 'tag:Repo', 'Values': [repo]},
        {'Name': 'instance-state-name', 'Values': LIVE_STATES},
    ])
    return [i for page in pages for r in page['Reservations'] for i in r['Instances']]


def user_data(repo, labels):
    with open(os.path.join(HERE, 'bootstrap.sh')) as f:
        script = f.read()
    return script.replace('@@REPO@@', repo).replace('@@LABELS@@', labels)


def broker(instance_id, token, expires):
    account = os.environ['RUNNER_ACCOUNT_ID']
    ssm.put_parameter(
        Name=f'/github-runner/bootstrap/{instance_id}',
        Description='One-host GitHub registration credential; expires at GitHub after one hour',
        Type='SecureString', Value=token, Overwrite=False,
        Tags=[
            # Role=github-runner so fcvm's cleanup also sweeps this parameter once it expires.
            {'Key': 'Role', 'Value': 'github-runner'},
            {'Key': 'InstanceArn', 'Value': f'arn:aws:ec2:{REGION}:{account}:instance/{instance_id}'},
            {'Key': 'CredentialExpiresAt', 'Value': expires.astimezone(timezone.utc).isoformat()},
        ])


def launch(repo, cfg, subnets, size, job_id, token):
    """One spot VM for `size`, trying each subnet and instance type in order."""
    ami = ssm.get_parameter(Name=CANONICAL_AMI_PARAM)['Parameter']['Value']
    labels = f"{cfg['label']},{size}"
    data = user_data(repo, labels)
    last_error = None
    # Only these mean EC2 created nothing, so another pool is safe to try. Anything else -- a
    # timeout, a throttle, a 5xx -- may have launched an instance after all, so stop: the next
    # reconcile sees any host that did start (its JobId tag) and launches only if none did.
    for subnet in subnets:
        for instance_type in cfg['sizes'][size]:
            try:
                response = ec2.run_instances(
                    # botocore's own retries resend these exact arguments, so one token per
                    # attempt makes a retried request return the instance it already created.
                    ClientToken=str(uuid.uuid4()),
                    MinCount=1, MaxCount=1, ImageId=ami, InstanceType=instance_type,
                    NetworkInterfaces=[{
                        'DeviceIndex': 0, 'SubnetId': subnet['subnet_id'],
                        'Groups': [os.environ['SECURITY_GROUP_ID']],
                        'AssociatePublicIpAddress': True, 'Ipv6AddressCount': 1,
                    }],
                    IamInstanceProfile={'Name': os.environ['INSTANCE_PROFILE']},
                    BlockDeviceMappings=[{'DeviceName': '/dev/sda1', 'Ebs': {
                        'VolumeSize': VOLUME_GB, 'VolumeType': 'gp3', 'DeleteOnTermination': True, 'Encrypted': True}}],
                    UserData=data,
                    MetadataOptions={'HttpTokens': 'required', 'HttpEndpoint': 'enabled', 'HttpPutResponseHopLimit': 1},
                    InstanceInitiatedShutdownBehavior='terminate',
                    InstanceMarketOptions={'MarketType': 'spot', 'SpotOptions': {'SpotInstanceType': 'one-time'}},
                    TagSpecifications=[
                        {'ResourceType': 'instance', 'Tags': [
                            {'Key': 'Name', 'Value': f"{ROLE}-{cfg['label']}-{size}"},
                            {'Key': 'Role', 'Value': ROLE},
                            {'Key': 'Repo', 'Value': repo},
                            {'Key': 'Size', 'Value': size},
                            {'Key': 'JobId', 'Value': job_id},
                            {'Key': 'InspectorEc2Exclusion', 'Value': 'true'},
                        ]},
                        {'ResourceType': 'volume', 'Tags': [{'Key': 'Role', 'Value': ROLE}]},
                        {'ResourceType': 'network-interface', 'Tags': [{'Key': 'Role', 'Value': ROLE}]},
                    ])
            except Exception as error:
                last_error = error
                code = (getattr(error, 'response', None) or {}).get('Error', {}).get('Code')
                if code not in CAPACITY_CODES:
                    print(f"{repo}: launch for job {job_id} ended ambiguously ({type(error).__name__} {code}); "
                          'not trying another pool this round')
                    return None, False
                print(f"{repo}: {instance_type} in {subnet['availability_zone']} refused: {code}")
                continue
            instance_id = response['Instances'][0]['InstanceId']
            try:
                broker(instance_id, *token)
            except Exception as error:
                # EC2 accepted it; without its credential it can only idle. Never launch another
                # type for the same job: the reconcile retries once this one is gone.
                print(f'{repo}: bootstrap credential for {instance_id} failed ({type(error).__name__}); terminating')
                ec2.terminate_instances(InstanceIds=[instance_id])
                try:
                    ssm.delete_parameter(Name=f'/github-runner/bootstrap/{instance_id}')
                except Exception:
                    pass
                return None, True
            print(f"{repo}: launched {instance_id} ({instance_type}, {subnet['availability_zone']}) for job {job_id} [{labels}]")
            return instance_id, False
    print(f'{repo}: every pool refused a {size} runner for job {job_id}: {last_error}')
    return None, True


# ---------------------------------------------------------------- policy
def claim(repo, job_id):
    """True if this invocation may launch for the job (see CLAIM_SECONDS)."""
    t = int(now().timestamp())
    try:
        dynamodb.put_item(
            TableName=os.environ['CLAIMS_TABLE'],
            Item={'pk': {'S': f'{repo}#{job_id}'}, 'expires_at': {'N': str(t + CLAIM_SECONDS)},
                  'ttl': {'N': str(t + 86400)}},
            ConditionExpression='attribute_not_exists(pk) OR expires_at < :now',
            ExpressionAttributeValues={':now': {'N': str(t)}})
        return True
    except dynamodb.exceptions.ConditionalCheckFailedException:
        return False


def release(repo, job_id):
    try:
        dynamodb.delete_item(TableName=os.environ['CLAIMS_TABLE'], Key={'pk': {'S': f'{repo}#{job_id}'}})
    except Exception as error:  # the claim then just expires
        print(f'{repo}: could not release the claim for job {job_id}: {type(error).__name__}')


def ensure_runner(repo, cfg, subnets, pat, job_id, size, live, token=None):
    """Launch for `job_id` unless it already has a host or the repo is at its cap."""
    if any(tag(i, 'JobId') == job_id for i in live):
        return 'exists', token
    if len(live) >= int(cfg['max']):
        print(f"{repo}: at its cap of {cfg['max']} runners; job {job_id} waits")
        return 'cap', token
    if not claim(repo, job_id):
        return 'claimed', token
    token = token or registration_token(repo, pat)
    instance_id, definite = launch(repo, cfg, subnets, size, job_id, token)
    if instance_id:
        live.append({'InstanceId': instance_id, 'Tags': [{'Key': 'JobId', 'Value': job_id}],
                     'LaunchTime': now(), 'State': {'Name': 'pending'}})
        return 'launched', token
    if definite:
        release(repo, job_id)
    return 'failed', token


def reap(repo, cfg, pat, live, runners):
    """Terminate hosts that never registered, sat idle or outlived any job; return survivors."""
    keep, t = [], now()
    for instance in live:
        instance_id = instance['InstanceId']
        age = t - instance['LaunchTime']
        runner = runners.get(instance_id)
        reason = None
        if age > MAX_LIFETIME:
            reason = f'older than {MAX_LIFETIME}'
        elif runner is None and age > BOOT_GRACE:
            reason = f'not registered after {BOOT_GRACE}'
        elif runner is not None and not runner.get('busy') and age > IDLE_LIMIT:
            reason = f'idle after {IDLE_LIMIT}'
        if reason:
            print(f'{repo}: terminating {instance_id}: {reason}')
            ec2.terminate_instances(InstanceIds=[instance_id])
            if runner is not None:
                try:
                    github('DELETE', f"/repos/{repo}/actions/runners/{runner['id']}", pat)
                except urllib.error.HTTPError as error:
                    print(f'{repo}: could not remove runner {instance_id}: {error.code}')
        else:
            keep.append(instance)
    live_ids = {i['InstanceId'] for i in keep}
    for name, runner in runners.items():
        ours = cfg['label'].lower() in {str(l.get('name', '')).lower() for l in runner.get('labels') or []}
        if ours and str(name).startswith('i-') and name not in live_ids and runner.get('status') == 'offline':
            print(f'{repo}: removing offline runner {name} (its host is gone)')
            try:
                github('DELETE', f"/repos/{repo}/actions/runners/{runner['id']}", pat)
            except urllib.error.HTTPError as error:
                print(f'{repo}: could not remove runner {name}: {error.code}')
    return keep


def reconcile(repo, cfg, subnets):
    pat = repo_pat(cfg)
    if not pat:
        return {'repo': repo, 'skipped': 'no controller token'}
    live = reap(repo, cfg, pat, app_instances(repo), list_runners(repo, pat))
    outcomes, token = {}, None
    for job_id, size in queued_jobs(repo, cfg, pat):
        outcome, token = ensure_runner(repo, cfg, subnets, pat, job_id, size, live, token)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    print(f'{repo}: reconcile {outcomes or "nothing queued"}; {len(live)} live')
    return {'repo': repo, 'outcomes': outcomes, 'live': len(live)}


def publish_counts(results):
    """LiveRunners per repo and in total, every reconcile. The too-many and not-running alarms
    read this: AWS/EC2 has no per-tag instance count to alarm on."""
    data = [{'MetricName': 'LiveRunners', 'Dimensions': [{'Name': 'Repo', 'Value': r['repo']}],
             'Value': r['live'], 'Unit': 'Count'} for r in results if 'live' in r]
    data.append({'MetricName': 'LiveRunners', 'Dimensions': [{'Name': 'Repo', 'Value': 'ALL'}],
                 'Value': sum(r.get('live', 0) for r in results), 'Unit': 'Count'})
    cloudwatch.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=data)


def handler(event, context):
    repos, subnets = config()
    if event.get('reconcile') or event.get('source') == 'aws.events':
        # Each repo on its own: one repo's revoked token or GitHub timeout must not stop the
        # other's launches and reaping. The invocation still fails afterwards, for the alarm.
        results, failed = [], []
        for repo, cfg in repos.items():
            try:
                results.append(reconcile(repo, cfg, subnets))
            except Exception as error:
                failed.append(repo)
                print(f'{repo}: reconcile failed: {type(error).__name__}: {error}')
                results.append({'repo': repo, 'error': type(error).__name__})
        publish_counts(results)
        if failed:
            raise RuntimeError(f'reconcile failed for {", ".join(failed)}')
        return results
    repo = event.get('repo')
    if repo not in repos:
        print(f'ignoring a delivery for {repo!r}: not a served repo')
        return {'ignored': 'repo'}
    if event.get('action') != 'queued':
        # Ephemeral hosts power off after their job; a completion needs no work here.
        return {'ignored': 'action'}
    cfg, job = repos[repo], event.get('workflow_job') or {}
    size = job_size(cfg, job.get('labels'))
    if not size or job.get('id') is None:
        return {'ignored': 'labels'}
    pat = repo_pat(cfg)
    if not pat:
        return {'skipped': 'no controller token'}
    outcome, _ = ensure_runner(repo, cfg, subnets, pat, str(job['id']), size, app_instances(repo))
    return {'repo': repo, 'job': job['id'], 'size': size, 'outcome': outcome}
