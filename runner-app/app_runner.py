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
import math
import os
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config

REGION = 'us-west-1'
ROLE = 'github-app-runner'
CANONICAL_AMI_PARAM = '/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id'
LIVE_STATES = ['pending', 'running']

BOOT_GRACE = timedelta(minutes=int(os.environ.get('BOOT_GRACE_MINUTES', '10')))
IDLE_LIMIT = timedelta(minutes=int(os.environ.get('IDLE_MINUTES', '10')))
MAX_LIFETIME = timedelta(minutes=int(os.environ.get('MAX_LIFETIME_MINUTES', '180')))
VOLUME_GB = int(os.environ.get('VOLUME_GB', '80'))
# GitHub calls one reconcile may spend listing runs and jobs (queued_jobs()). Every 2 minutes
# per repo, 120 calls is at most 3,600/hour, inside the token's 5,000.
SCAN_CALL_BUDGET = 120
MAX_RUN_PAGES = 10   # GitHub lists at most 1,000 runs for a status-filtered query

# Bounded clients: botocore's defaults (60 s reads, several retries) let one stalled call eat a
# repo's time share and the publishing reserve. Every call here fails in well under that.
BOUNDED = Config(connect_timeout=3, read_timeout=8, retries={'total_max_attempts': 2, 'mode': 'standard'})
ec2 = boto3.client('ec2', region_name=REGION, config=BOUNDED)
# RunInstances only, with botocore's own retries off, as in the metal controller
# (runner-autoscale.tf): a pool with no spot capacity answers InsufficientInstanceCapacity, and
# the default client retried that same pool with backoff for 7-15 seconds before launch() could
# move on. launch() walks the pools itself, and each attempt has its own ClientToken.
launch_ec2 = boto3.client('ec2', region_name=REGION, config=Config(
    connect_timeout=3, read_timeout=10, retries={'total_max_attempts': 1}))
# RunInstances at its worst (connect + read above). An attempt starts only if this much of the
# repo's share is left; RESERVE_SECONDS covers the credential handoff after it (bounded SSM,
# at most 2 x 11 s) and publishing the counts.
LAUNCH_ATTEMPT_SECONDS = 3 + 10
ssm = boto3.client('ssm', region_name=REGION, config=BOUNDED)
secrets = boto3.client('secretsmanager', region_name=REGION, config=BOUNDED)
dynamodb = boto3.client('dynamodb', region_name=REGION, config=BOUNDED)
cloudwatch = boto3.client('cloudwatch', region_name=REGION, config=BOUNDED)

# A launch claim per job (table CLAIMS_TABLE) makes "one host per job" hold across
# invocations. DescribeInstances is eventually consistent: a redelivery seconds after a launch
# may not see the new host by its JobId tag yet. The claim is a conditional write, strongly
# consistent, so exactly one invocation launches. Each claim carries a random nonce that
# prefixes the ClientToken of every RunInstances attempt it makes, so the claim is released only
# when THAT launch is listed -- never by an older host of the same job. A definite failure
# releases it so the next round retries at once; an ambiguous one keeps it until CLAIM_SECONDS
# pass, by which time the host is listed if one did start.
CLAIM_SECONDS = 15 * 60
METRIC_NAMESPACE = 'GitHubAppRunner'

HERE = os.path.dirname(os.path.abspath(__file__))



# How launch() treats a RunInstances error:
#   capacity refusal   -> nothing created; try the next pool
#   ambiguous          -> may have created an instance (no response, throttling, 5xx); keep the
#                         job's claim and let a later round decide
#   anything else      -> definite refusal; raise LaunchRefused so the error alarm fires
CAPACITY_CODES = {'InsufficientInstanceCapacity', 'Unsupported', 'SpotMaxPriceTooLow',
                  'InsufficientCapacityOnHost', 'UnfulfillableCapacity'}
AMBIGUOUS_CODES = {'InternalError', 'InternalFailure', 'ServiceUnavailable', 'Unavailable',
                   'RequestLimitExceeded', 'Throttling', 'ThrottlingException', 'RequestTimeout',
                   'RequestTimeoutException'}


class LaunchRefused(Exception):
    pass

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
        # Only "no value yet" is expected. AccessDenied, a KMS failure or a network error must
        # propagate: returning None would skip the repo while every alarm stays green.
        code = (getattr(error, 'response', None) or {}).get('Error', {}).get('Code')
        if code == 'ResourceNotFoundException':
            print(f"no controller token in {cfg['pat_secret']} yet")
            return None
        raise
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


# Wall clock, not just call count, bounds a reconcile: github() may take up to its timeout per
# call. handler() gives each repo an equal share of the invocation's remaining time (minus a
# reserve for publishing counts), and a repo's scan stops at its share.
DEADLINE = [float('inf')]
RESERVE_SECONDS = 25


def out_of_time(need=0.0):
    """True once the repo's share is spent, or has less than `need` seconds left."""
    return time.monotonic() + need >= DEADLINE[0]


def pages(repo, path, key, pat, budget):
    """Every item under `key` across a paginated GitHub list, 100 per page, while the shared
    call budget and this repo's time share last (budget is a one-element list so callers share it)."""
    page = 1
    while budget[0] > 0 and not out_of_time():
        budget[0] -= 1
        sep = '&' if '?' in path else '?'
        items = github('GET', f'{path}{sep}per_page=100&page={page}', pat).get(key) or []
        yield from items
        if len(items) < 100:
            return
        page += 1
    print(f'{repo}: queue scan stopped at its budget of {SCAN_CALL_BUDGET} GitHub calls')


def runs_oldest_first(repo, status, pat, budget):
    """This status's runs, OLDEST first, one page at a time. GitHub lists newest first, so page 1
    (read once, for total_count) holds the newest; walking back from the last page reaches the
    oldest runs first however many newer ones there are."""
    if budget[0] <= 0 or out_of_time():
        return
    path = f'/repos/{repo}/actions/runs?status={status}&per_page=100'
    budget[0] -= 1
    first = github('GET', f'{path}&page=1', pat)
    last = min(MAX_RUN_PAGES, max(1, math.ceil(int(first.get('total_count') or 0) / 100)))
    for page in range(last, 0, -1):
        if page == 1:
            body = first
        elif budget[0] <= 0 or out_of_time():
            return
        else:
            budget[0] -= 1
            body = github('GET', f'{path}&page={page}', pat)
        yield from sorted(body.get('workflow_runs') or [], key=lambda r: (r.get('created_at') or '', r['id']))


def queued_jobs(repo, cfg, pat):
    """[(job_id, size)] for this repo's queued jobs that ask for our labels.

    Queued and in-progress runs, OLDEST first, each run's jobs read as soon as the run is: a job
    whose delivery was lost or whose first launch was refused is the one most at risk of waiting,
    and it must be reached however many newer runs there are and however slowly GitHub answers.
    Reading every run page before any job could spend the whole share listing runs. Which status
    goes first alternates by round, so neither starves the other. Bounded by SCAN_CALL_BUDGET
    calls per reconcile (well inside 5,000/hour) and the repo's deadline.
    """
    found, seen, runs_seen, budget = [], set(), set(), [SCAN_CALL_BUDGET]
    statuses = ('queued', 'in_progress') if int(time.time() // 120) % 2 == 0 else ('in_progress', 'queued')
    for status in statuses:
        for run in runs_oldest_first(repo, status, pat, budget):
            if run['id'] in runs_seen:
                continue
            runs_seen.add(run['id'])
            if budget[0] <= 0 or out_of_time():
                print(f'{repo}: queue scan stopped at its budget of {SCAN_CALL_BUDGET} GitHub calls or its time share')
                break
            for job in pages(repo, f"/repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest", 'jobs', pat, budget):
                if job.get('id') in seen or job.get('status') != 'queued':
                    continue
                seen.add(job.get('id'))
                size = job_size(cfg, job.get('labels'))
                if size:
                    found.append((str(job['id']), size))
    return found


def list_runners(repo, pat):
    """({runner name: runner record}, complete) for this repo's self-hosted runners. `complete` is
    False when the listing stopped early -- at the repo's deadline, or past 500 runners -- and a
    host's absence from it then proves nothing."""
    out = {}
    for page in range(1, 6):
        if out_of_time():
            return out, False
        body = github('GET', f'/repos/{repo}/actions/runners?per_page=100&page={page}', pat)
        runners = body.get('runners') or []
        for runner in runners:
            out[runner.get('name')] = runner
        if len(runners) < 100:
            return out, True
    return out, False


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


def listed_instances(repo):
    """Every app runner EC2 still lists for this repo, in ANY state (terminated hosts stay listed
    for about an hour). Once a claim's own launch is listed, its claim has done its job."""
    listed = ec2.get_paginator('describe_instances').paginate(Filters=[
        {'Name': 'tag:Role', 'Values': [ROLE]},
        {'Name': 'tag:Repo', 'Values': [repo]},
    ])
    return [i for page in listed for r in page['Reservations'] for i in r['Instances']]


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


def launch(repo, cfg, subnets, size, job_id, token, nonce):
    """One spot VM for `size`, trying each subnet and instance type in order."""
    ami = ssm.get_parameter(Name=CANONICAL_AMI_PARAM)['Parameter']['Value']
    labels = f"{cfg['label']},{size}"
    data = user_data(repo, labels)
    last_error, attempt = None, 0
    # Only these mean EC2 created nothing, so another pool is safe to try. Anything else -- a
    # timeout, a throttle, a 5xx -- may have launched an instance after all, so stop: the next
    # reconcile sees any host that did start (its JobId tag) and launches only if none did.
    for subnet in subnets:
        for instance_type in cfg['sizes'][size]:
            # Each attempt may take the launch client's full read timeout: none starts after
            # the repo's deadline. Every earlier attempt was a capacity refusal, so nothing exists.
            if out_of_time(LAUNCH_ATTEMPT_SECONDS):
                print(f'{repo}: out of time before a pool accepted job {job_id}; next round retries')
                return None, True
            attempt += 1
            try:
                response = launch_ec2.run_instances(
                    # botocore's own retries resend these exact arguments, so one token per
                    # attempt makes a retried request return the instance it already created.
                    # The claim's nonce prefixes it, which is how the claim finds its own host.
                    ClientToken=f'{nonce}-{attempt}',
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
                if code in CAPACITY_CODES:
                    print(f"{repo}: {instance_type} in {subnet['availability_zone']} refused: {code}")
                    continue
                if code is None or code in AMBIGUOUS_CODES:
                    print(f"{repo}: launch for job {job_id} ended ambiguously ({type(error).__name__} {code}); "
                          'not trying another pool this round')
                    return None, False
                # Definite (UnauthorizedOperation, InvalidAMIID.*, a quota ...): nothing was created,
                # and retrying other pools or later rounds will not help. Make it loud.
                raise LaunchRefused(f'{repo}: RunInstances refused job {job_id}: {code}') from error
            instance_id = response['Instances'][0]['InstanceId']
            try:
                broker(instance_id, *token)
            except Exception as error:
                # EC2 accepted it; without its credential it can only idle. Never launch another
                # type for the same job: the reconcile retries once this one is gone.
                print(f'{repo}: bootstrap credential for {instance_id} failed ({type(error).__name__}); terminating')
                try:
                    ec2.terminate_instances(InstanceIds=[instance_id])
                except Exception as cleanup:
                    # The host may still be alive: an ambiguous outcome, so the claim must stay.
                    print(f'{repo}: could not terminate {instance_id} ({type(cleanup).__name__}); keeping the claim')
                    return None, False
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
    """The claim's nonce if this invocation may launch for the job (see CLAIM_SECONDS), else None."""
    t, nonce = int(now().timestamp()), uuid.uuid4().hex
    try:
        dynamodb.put_item(
            TableName=os.environ['CLAIMS_TABLE'],
            Item={'repo': {'S': repo}, 'job': {'S': job_id}, 'nonce': {'S': nonce},
                  'expires_at': {'N': str(t + CLAIM_SECONDS)}, 'ttl': {'N': str(t + 86400)}},
            ConditionExpression='attribute_not_exists(job) OR expires_at < :now',
            ExpressionAttributeValues={':now': {'N': str(t)}})
        return nonce
    except dynamodb.exceptions.ConditionalCheckFailedException:
        return None


def active_claims(repo):
    """{job: nonce} for the jobs this repo launched for (or is launching for) within
    CLAIM_SECONDS. A consistent read:
    together with the serialized controller (reserved concurrency 1) it makes the repo cap hold
    even when DescribeInstances has not caught up with a burst of launches."""
    t, found, page = int(now().timestamp()), {}, {}
    while True:
        response = dynamodb.query(
            TableName=os.environ['CLAIMS_TABLE'], ConsistentRead=True,
            KeyConditionExpression='repo = :repo', ExpressionAttributeValues={':repo': {'S': repo}}, **page)
        found.update({item['job']['S']: item.get('nonce', {}).get('S', '')
                      for item in response.get('Items', []) if int(item['expires_at']['N']) >= t})
        if 'LastEvaluatedKey' not in response:
            return found
        page = {'ExclusiveStartKey': response['LastEvaluatedKey']}


def release(repo, job_id):
    try:
        dynamodb.delete_item(TableName=os.environ['CLAIMS_TABLE'], Key={'repo': {'S': repo}, 'job': {'S': job_id}})
    except Exception as error:  # the claim then just expires
        print(f'{repo}: could not release the claim for job {job_id}: {type(error).__name__}')


def ensure_runner(repo, cfg, subnets, pat, job_id, size, live, token=None, busy=frozenset()):
    """Launch for `job_id` unless it already has a host or the repo is at its cap.

    `busy` names hosts whose runner is running a job. A host tagged for this job cannot serve it
    if it is busy while the job is still queued: GitHub gave it another job with the same labels,
    and an ephemeral runner takes only one. The job then needs a host of its own."""
    # Claims first, then a fresh listing: every launch whose claim was read is, in that listing,
    # either not yet visible (the claim stands in for it) or visible and counted by its real
    # state. `live`, taken earlier in the invocation, is only added to that, never trusted alone.
    claimed, listed = active_claims(repo), listed_instances(repo)
    running = live + [i for i in listed if i.get('State', {}).get('Name') in LIVE_STATES]
    if any(tag(i, 'JobId') == job_id and i['InstanceId'] not in busy for i in running):
        return 'exists', token
    # A claim goes once ITS OWN launch is listed, in any state (its nonce prefixes the host's
    # ClientToken): short jobs do not hold cap slots, and a job whose host failed and terminated
    # while it stayed queued is relaunched now rather than when the claim expires. A claim with
    # no listed host of its own -- even if an older host of the same job is listed -- stays.
    # Clean up FIRST, before deciding this job is "claimed".
    tokens = {i.get('ClientToken') or '' for i in listed}
    for job, nonce in list(claimed.items()):
        if nonce and any(t.startswith(nonce + '-') for t in tokens):
            release(repo, job)
            del claimed[job]
    if job_id in claimed:
        return 'claimed', token
    # Hosts by instance (two hosts can carry one JobId, above), plus claims for launches not in
    # `running` at all. A launch this invocation made is in `live` (with its ClientToken) but not
    # yet listed: its claim must stay, but it is one slot, not two.
    held = {i.get('ClientToken') or '' for i in running}
    unseen = {job for job, nonce in claimed.items() if not (nonce and any(t.startswith(nonce + '-') for t in held))}
    in_use = {i['InstanceId'] for i in running} | unseen
    if len(in_use) >= int(cfg['max']):
        print(f"{repo}: at its cap of {cfg['max']} runners; job {job_id} waits")
        return 'cap', token
    nonce = claim(repo, job_id)
    if not nonce:
        return 'claimed', token
    try:
        token = token or registration_token(repo, pat)
        instance_id, definite = launch(repo, cfg, subnets, size, job_id, token, nonce)
    except Exception:
        # Anything raised here launched nothing: a failure before RunInstances (the token, the
        # AMI lookup) or a definite RunInstances refusal (LaunchRefused). Free the job for the
        # next round rather than holding it for CLAIM_SECONDS, and let the error reach the alarm.
        release(repo, job_id)
        raise
    if instance_id:
        live.append({'InstanceId': instance_id, 'Tags': [{'Key': 'JobId', 'Value': job_id}],
                     'LaunchTime': now(), 'State': {'Name': 'pending'}, 'ClientToken': f'{nonce}-local'})
        return 'launched', token
    if definite:
        release(repo, job_id)
    return 'failed', token


def reap(repo, cfg, pat, live, runners, complete=True):
    """Terminate hosts that never registered, sat idle or outlived any job; return survivors.

    Each terminate or DELETE is a request, so none starts after the repo's deadline: the rest
    wait for the next round and count as live until then. "Never registered" is judged only
    from a complete runner listing, or a busy host missing from a partial one would be killed."""
    keep, t = [], now()
    for index, instance in enumerate(live):
        if out_of_time():
            print(f'{repo}: reaping stopped at its time share; {len(live) - index} hosts wait for the next round')
            keep += live[index:]
            break
        instance_id = instance['InstanceId']
        age = t - instance['LaunchTime']
        runner = runners.get(instance_id)
        reason, idle = None, False
        if age > MAX_LIFETIME:
            reason = f'older than {MAX_LIFETIME}'
        elif runner is None and complete and age > BOOT_GRACE:
            reason = f'not registered after {BOOT_GRACE}'
        elif runner is not None and not runner.get('busy') and age > IDLE_LIMIT:
            reason, idle = f'idle after {IDLE_LIMIT}', True
        if reason and idle:
            # `busy` is from the listing at the start of the round: GitHub may have given this
            # runner a job since, and terminating first would kill it. Deregister FIRST --
            # GitHub refuses to remove a runner that is running a job -- and terminate only
            # once that succeeded. (Pattern B uses the same order, for the same reason.)
            try:
                github('DELETE', f"/repos/{repo}/actions/runners/{runner['id']}", pat)
            except urllib.error.HTTPError as error:
                print(f'{repo}: keeping {instance_id}: GitHub refused to deregister its runner ({error.code})')
                keep.append(instance)
                continue
            runner = None
        if reason:
            print(f'{repo}: terminating {instance_id}: {reason}')
            ec2.terminate_instances(InstanceIds=[instance_id])
            # Past the deadline now, the record waits: next round its host is gone and the
            # offline-runner sweep below removes it.
            if runner is not None and not out_of_time():
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
            if out_of_time():
                break
            print(f'{repo}: removing offline runner {name} (its host is gone)')
            try:
                github('DELETE', f"/repos/{repo}/actions/runners/{runner['id']}", pat)
            except urllib.error.HTTPError as error:
                print(f'{repo}: could not remove runner {name}: {error.code}')
    return keep


def reconcile(repo, cfg, subnets):
    # An earlier repo may have used this one's share already: start nothing.
    if out_of_time():
        print(f'{repo}: reconcile skipped; its time share was already spent')
        return {'repo': repo, 'skipped': 'no time left'}
    pat = repo_pat(cfg)
    if not pat:
        return {'repo': repo, 'skipped': 'no controller token'}
    runners, complete = list_runners(repo, pat)
    if out_of_time():
        print(f'{repo}: reconcile stopped after the runner listing; its time share is spent')
        return {'repo': repo, 'skipped': 'no time left'}
    live = reap(repo, cfg, pat, app_instances(repo), runners, complete)
    busy = frozenset(name for name, runner in runners.items() if runner.get('busy'))
    outcomes, token = {}, None
    # The scan gets half the remaining share, so jobs it finds always have time to launch: a
    # queue big or slow enough to use the whole share would otherwise be found and dropped on
    # every round.
    share_end = DEADLINE[0]
    if share_end != float('inf'):
        DEADLINE[0] = time.monotonic() + max(0.0, share_end - time.monotonic()) / 2
    try:
        jobs = queued_jobs(repo, cfg, pat)
    finally:
        DEADLINE[0] = share_end
    for job_id, size in jobs:
        # Each ensure_runner costs a DynamoDB query and an EC2 scan, so a backlog must not run
        # past this repo's time share. The cap is repo-wide: once hit, every later job waits too.
        if out_of_time():
            outcomes['deferred'] = outcomes.get('deferred', 0) + 1
            break
        outcome, token = ensure_runner(repo, cfg, subnets, pat, job_id, size, live, token, busy)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if outcome == 'cap':
            break
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
    started = time.monotonic()
    DEADLINE[0] = float('inf')
    repos, subnets = config()
    if event.get('reconcile') or event.get('source') == 'aws.events':
        # Each repo on its own: one repo's revoked token or GitHub timeout must not stop the
        # other's launches and reaping. The invocation still fails afterwards, for the alarm.
        results, failed = [], []
        remaining = (context.get_remaining_time_in_millis() / 1000 if context else 110) - RESERVE_SECONDS
        order = list(repos.items())
        # Alternate which repo goes first, so a slow one cannot always take the time the other needs.
        if int(time.time() // 120) % 2:
            order.reverse()
        for index, (repo, cfg) in enumerate(order):
            share = max(0.0, remaining - (time.monotonic() - started)) / (len(order) - index)
            DEADLINE[0] = time.monotonic() + share
            try:
                results.append(reconcile(repo, cfg, subnets))
            except Exception as error:
                failed.append(repo)
                print(f'{repo}: reconcile failed: {type(error).__name__}: {error}')
                results.append({'repo': repo, 'error': type(error).__name__})
        DEADLINE[0] = float('inf')
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
