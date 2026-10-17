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
    and Repo tag, and a host never outlives its job. The cap is a number of hosts (`max`) and,
    where a repo sets one, a number of vCPUs (`max_vcpus`) as well: see ensure_runner().
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
import base64
import contextlib
import hashlib
import json
import math
import os
import time
import uuid
import urllib.error
import urllib.parse
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
BOUNDED = Config(connect_timeout=3, read_timeout=8, retries={'total_max_attempts': 1})
ec2 = boto3.client('ec2', region_name=REGION, config=BOUNDED)
# RunInstances only, with botocore's own retries off, as in the metal controller
# (runner-autoscale.tf): a pool with no spot capacity answers InsufficientInstanceCapacity, and
# the default client retried that same pool with backoff for 7-15 seconds before launch() could
# move on. launch() walks the pools itself, and each attempt has its own ClientToken.
launch_ec2 = boto3.client('ec2', region_name=REGION, config=Config(
    connect_timeout=3, read_timeout=10, retries={'total_max_attempts': 1}))
# RunInstances at its worst (connect + read above). An attempt starts only if this much of the
# repo's share is left; RESERVE_SECONDS covers what may follow it past the deadline (below).
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
                  'InsufficientCapacityOnHost', 'UnfulfillableCapacity', 'InsufficientFreeAddressesInSubnet'}
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
    if not HANDOFF[0] and out_of_time():
        raise OutOfTime(f'GitHub {method} {path}')
    req = urllib.request.Request(
        'https://api.github.com' + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization': f'Bearer {pat}', 'Accept': 'application/vnd.github+json',
                 'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=8) as resp:
        raw = resp.read(4_000_000)
    return json.loads(raw) if raw else {}


# ---------------------------------------------------------------- the runners GitHub App
# A repo whose config names `app_repo` takes an installation token from the runners GitHub App (Secrets Manager
# RUNNER_APP_SECRET, JSON: app_id, private_key, installations {owner: id}) instead of a year-long PAT: minted here, narrowed
# to that one repository and to administration:write + actions:read, and expiring within the hour. The app's JWT is signed
# in pure Python (RSA PKCS#1 v1.5 over SHA-256, standard library only): the Lambda runtime has no openssl and no
# `cryptography`. Until the secret has a value, or when minting fails, the repo's own PAT is used and the reason logged.
RUNNER_APP_SECRET = os.environ.get('RUNNER_APP_SECRET', '')
APP_TOKEN_PERMISSIONS = {'administration': 'write', 'actions': 'read', 'metadata': 'read'}
_APP_TOKENS = {}  # repo -> (token, expiry epoch seconds), reused while a warm Lambda keeps the module
_APP_SCOPE_OK = {}  # (installation id, owner) -> (expiry epoch seconds, refusal message or None) of its last scope check
APP_SCOPE_PASS_SECONDS = 2700
_SHA256_DIGEST_INFO = bytes.fromhex('3031300d060960864801650304020105000420')


def _der(buf, i):
    """One DER element at buf[i]: (tag, contents, index after it)."""
    tag, length = buf[i], buf[i + 1]
    i += 2
    if length & 0x80:
        width = length & 0x7f
        length, i = int.from_bytes(buf[i:i + width], 'big'), i + width
    return tag, buf[i:i + length], i + length


def rsa_private_numbers(pem):
    """(n, d) from a PEM RSA private key, PKCS#1 ("RSA PRIVATE KEY", what GitHub issues) or PKCS#8 ("PRIVATE KEY")."""
    der = base64.b64decode(''.join(l for l in pem.strip().splitlines() if not l.startswith('-----')))
    _, seq, _ = _der(der, 0)
    if 'BEGIN PRIVATE KEY' in pem:
        _, _, i = _der(seq, 0)          # version
        _, _, i = _der(seq, i)          # algorithm
        _, inner, _ = _der(seq, i)      # OCTET STRING holding the PKCS#1 key
        _, seq, _ = _der(inner, 0)
    ints, i = [], 0
    while len(ints) < 4:
        _, value, i = _der(seq, i)
        ints.append(int.from_bytes(value, 'big'))
    _version, n, _e, d = ints
    return n, d


def rs256(message, pem):
    n, d = rsa_private_numbers(pem)
    k = (n.bit_length() + 7) // 8
    digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    em = b'\x00\x01' + b'\xff' * (k - len(digest_info) - 3) + b'\x00' + digest_info
    return pow(int.from_bytes(em, 'big'), d, n).to_bytes(k, 'big')


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def app_jwt(app_id, pem, at=None):
    at = int(time.time() if at is None else at)
    head = _b64url(json.dumps({'alg': 'RS256', 'typ': 'JWT'}, separators=(',', ':')).encode())
    body = _b64url(json.dumps({'iat': at - 60, 'exp': at + 540, 'iss': str(app_id)}, separators=(',', ':')).encode())
    return f'{head}.{body}.{_b64url(rs256(f"{head}.{body}".encode(), pem))}'


def app_token(repo):
    """An installation token for `repo` from the runners app, or None when the app is not set up for it."""
    cached = _APP_TOKENS.get(repo)
    if cached and cached[1] - time.time() > 600:
        return cached[0]
    try:
        value = secrets.get_secret_value(SecretId=RUNNER_APP_SECRET)['SecretString']
    except Exception as error:
        if ((getattr(error, 'response', None) or {}).get('Error', {}).get('Code')) == 'ResourceNotFoundException':
            return None
        raise
    creds = json.loads(value) if value.strip() else {}
    owner, name = repo.split('/', 1)
    installation = (creds.get('installations') or {}).get(owner)
    if not (creds.get('app_id') and creds.get('private_key') and installation):
        return None
    jwt = app_jwt(creds['app_id'], creds['private_key'])
    check_installation_scope(installation, owner, jwt)
    out = github('POST', f'/app/installations/{installation}/access_tokens', jwt,
                 body={'repositories': [name], 'permissions': APP_TOKEN_PERMISSIONS})
    token = out.get('token')
    if not isinstance(token, str) or not token.startswith('ghs_'):
        raise RuntimeError('GitHub returned no installation token')
    try:
        expires = datetime.fromisoformat(out['expires_at'].replace('Z', '+00:00')).timestamp()
    except Exception:
        expires = time.time() + 3000
    _APP_TOKENS[repo] = (token, expires)
    return token


def check_installation_scope(installation, owner, jwt):
    """Refuse an installation that is not `owner`'s or covers more than the repos the app is meant to cover there.

    The app's key can mint administration tokens for every repository its installation covers, and this Lambda handles
    webhook payloads, so an installation on "All repositories" (GitHub's default choice), one that also selects another
    repository, or another account's installation recorded by mistake must not be minted from. Checked against the
    installation itself, with a metadata-only token for the listing (revoked afterwards). A pass is remembered for an hour,
    a refusal for ten minutes; a refusal raises, and repo_pat logs it and uses the repo's own token. The allowed set is
    APP_INSTALLATION_REPOS (every repo the app is meant to cover), not REPOS, which leaves out repos whose gate is off.
    """
    key = (installation, owner.lower())  # a check is valid only for the owner it was made for
    until, refusal = _APP_SCOPE_OK.get(key, (0, None))
    if until > time.time():
        if refusal:
            raise RuntimeError(refusal)
        return

    def refuse(reason):
        message = f'the runners app installation {installation} {reason}; refusing it'
        _APP_SCOPE_OK[key] = (time.time() + 600, message)
        raise RuntimeError(message)

    info = github('GET', f'/app/installations/{installation}', jwt)
    account = ((info.get('account') or {}).get('login') or '').lower()
    if account != owner.lower():
        refuse(f'belongs to {account or "no account"}, not {owner}')
    if info.get('repository_selection') != 'selected':
        refuse(f'on {owner} covers all repositories (install it with "Only select repositories" and the served ones)')
    allowed = {repo.lower() for repo in json.loads(os.environ.get('APP_INSTALLATION_REPOS') or '[]')
               if repo.split('/', 1)[0].lower() == owner.lower()}
    probe = github('POST', f'/app/installations/{installation}/access_tokens', jwt, body={'permissions': {'metadata': 'read'}})
    token = probe.get('token') or ''
    if not token:
        refuse('gave no listing token')
    try:
        listing = github('GET', '/installation/repositories?per_page=100', token)
    finally:
        try:
            github('DELETE', '/installation/token', token)
        except OutOfTime:
            raise
        except Exception:
            pass  # it expires within the hour on its own and can only read metadata
    names = {(r.get('full_name') or '').lower() for r in listing.get('repositories') or []}
    if listing.get('total_count') != len(names):
        refuse('covers more repositories than one listing page')
    if not names <= allowed:
        refuse(f'on {owner} also covers {sorted(names - allowed)} (select only the served repositories)')
    # Shorter than a token's reuse (minted for an hour, reused until ten minutes are left), so every mint re-checks: a
    # broadened installation is noticed by the next token, not up to two tokens later.
    _APP_SCOPE_OK[key] = (time.time() + APP_SCOPE_PASS_SECONDS, None)


def repo_pat(cfg):
    """This repo's controller token, or None while it has none yet.

    With RUNNER_APP_SECRET set and the repo named in `app_repo`: an installation token from the runners app. Otherwise,
    or when the app is not set up or minting fails (logged), the repo's own PAT: a Terraform-managed CONTAINER with no
    Terraform-managed version (runner-repos.tf), so the token never enters state; its owner puts the value in by hand.
    """
    if RUNNER_APP_SECRET and cfg.get('app_repo'):
        try:
            token = app_token(cfg['app_repo'])
            if token:
                return token
            print(f"{cfg['app_repo']}: the runners app is not set up for it yet; using the repo's own token")
        except OutOfTime:
            raise
        except Exception as error:
            print(f"{cfg['app_repo']}: runners app token failed ({type(error).__name__}: {str(error)[:160]}); using the repo's own token")
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


# ---------------------------------------------------------------- vCPUs
# runner-app.tf's runner_app_size_vcpus, passed in every repo's config as `vcpus`: the vCPUs of one host of each size.
# Every instance type listed under a size has that many (runner-app.tf checks it), so a size is enough to count a host.
def size_vcpus(cfg, size):
    """vCPUs of one `size` host. A size the map lacks is a configuration error, raised so the error alarm fires."""
    vcpus = cfg.get('vcpus') or {}
    if size not in vcpus:
        raise RuntimeError(f"no vCPU count for size {size!r} in the repo's config")
    return int(vcpus[size])


def largest_vcpus(cfg):
    """The vCPUs of the largest size: what a host or claim of unknown size counts as, the safe side of the cap."""
    vcpus = cfg.get('vcpus') or {}
    if not vcpus:
        raise RuntimeError("no vCPU counts in the repo's config")
    return max(int(v) for v in vcpus.values())


def known_vcpus(cfg, size):
    """vCPUs of one `size` host when the config has them, else None (a config from before `vcpus`)."""
    value = (cfg.get('vcpus') or {}).get(size)
    return None if value is None else int(value)


def host_vcpus(instance, cfg):
    """vCPUs of a host: its Vcpus tag (set at launch, so a later change to the sizes cannot miscount it), else its Size
    tag through the map (hosts launched before the Vcpus tag existed), else the largest size."""
    try:
        value = int(tag(instance, 'Vcpus') or 0)
        if value > 0:
            return value
    except ValueError:
        pass
    size = tag(instance, 'Size')
    if size in (cfg.get('vcpus') or {}):
        return int(cfg['vcpus'][size])
    return largest_vcpus(cfg)


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
RESERVE_SECONDS = 45


def out_of_time(need=0.0):
    """True once the repo's share is spent, or has less than `need` seconds left."""
    return time.monotonic() + need >= DEADLINE[0]


# The deadline is enforced at the call, not only where each caller remembers to check: once a
# repo's share is spent, no AWS call (botocore's before-call event) and no GitHub request may
# start, and the reconcile stops with what it has. Every call is bounded (BOUNDED, github()'s
# timeout), so at most one call is in flight past the deadline. The exception is HANDOFF: the
# credential handoff and cleanup after EC2 accepted a launch, and releasing a claim, which must
# finish. RESERVE_SECONDS covers the longest of those past the deadline -- a handoff whose
# credential write fails, whose host is then terminated and whose claim is released (3 bounded
# calls; the parameter's tidy-up is skipped when late) -- plus publishing the counts: 4 x 11 s.
class OutOfTime(Exception):
    pass


HANDOFF = [False]


@contextlib.contextmanager
def handoff():
    HANDOFF[0] = True
    try:
        yield
    finally:
        HANDOFF[0] = False


def refuse_after_deadline(event_name=None, **_):
    if not HANDOFF[0] and out_of_time():
        raise OutOfTime(event_name or 'an AWS call')


for _client in (ec2, launch_ec2, ssm, secrets, dynamodb):
    _client.meta.events.register('before-call', refuse_after_deadline)


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


def registered_now(repo, name, pat):
    """True if a runner called `name` is registered right now (GitHub filters by exact name)."""
    body = github('GET', f'/repos/{repo}/actions/runners?name={urllib.parse.quote(name)}', pat)
    return any(r.get('name') == name for r in body.get('runners') or [])


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
            # Fleet: the only parameters this controller may delete (runner-app.tf), never fcvm's.
            {'Key': 'Fleet', 'Value': 'github-app-runner'},
            {'Key': 'InstanceArn', 'Value': f'arn:aws:ec2:{REGION}:{account}:instance/{instance_id}'},
            {'Key': 'CredentialExpiresAt', 'Value': expires.astimezone(timezone.utc).isoformat()},
        ])


def launch(repo, cfg, subnets, size, job_id, token, nonce):
    """One spot VM for `size`, trying each subnet and instance type in order."""
    ami = ssm.get_parameter(Name=CANONICAL_AMI_PARAM)['Parameter']['Value']
    labels = f"{cfg['label']},{size}"
    data = user_data(repo, labels)
    vcpus = known_vcpus(cfg, size)
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
                    # InstanceMetadataTags: Inspector's SSM plugin reads InspectorEc2Exclusion from
                    # instance metadata. Without it the tag only hides findings, and the plugin still
                    # apt-installs its scanner mid-boot (GITHUB-RUNNERS.md, Inspector).
                    MetadataOptions={'HttpTokens': 'required', 'HttpEndpoint': 'enabled', 'HttpPutResponseHopLimit': 1,
                                     'InstanceMetadataTags': 'enabled'},
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
                        ] + ([{'Key': 'Vcpus', 'Value': str(vcpus)}] if vcpus is not None else [])},
                        {'ResourceType': 'volume', 'Tags': [{'Key': 'Role', 'Value': ROLE}]},
                        {'ResourceType': 'network-interface', 'Tags': [{'Key': 'Role', 'Value': ROLE}]},
                    ])
            except OutOfTime:
                raise
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
                # str(error) carries AWS's encoded authorization message for a denial; an
                # administrator decodes it with `aws sts decode-authorization-message`.
                raise LaunchRefused(f'{repo}: RunInstances refused job {job_id}: {code}: {error}') from error
            instance_id = response['Instances'][0]['InstanceId']
            with handoff():
                return settle(repo, instance_id, instance_type, subnet, job_id, labels, token)
    print(f'{repo}: every pool refused a {size} runner for job {job_id}: {last_error}')
    return None, True


def settle(repo, instance_id, instance_type, subnet, job_id, labels, token):
    """After EC2 accepted a launch: hand the host its credential, or terminate it."""
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
        # Tidy-up only: the parameter is readable by that (now terminated) host alone, and
        # fcvm's cleanup sweeps it once expired. Past the deadline, leave it.
        if not out_of_time():
            try:
                ssm.delete_parameter(Name=f'/github-runner/bootstrap/{instance_id}')
            except Exception:
                pass
        return None, True
    print(f"{repo}: launched {instance_id} ({instance_type}, {subnet['availability_zone']}) for job {job_id} [{labels}]")
    return instance_id, False


# ---------------------------------------------------------------- policy
def claim(repo, job_id, vcpus):
    """The claim's nonce if this invocation may launch for the job (see CLAIM_SECONDS), else None.

    The claim records the vCPUs of the host it launches, so a launch EC2 does not list yet counts its own size against
    the repo's vCPU cap (ensure_runner)."""
    t, nonce = int(now().timestamp()), uuid.uuid4().hex
    try:
        dynamodb.put_item(
            TableName=os.environ['CLAIMS_TABLE'],
            Item={'repo': {'S': repo}, 'job': {'S': job_id}, 'nonce': {'S': nonce},
                  'expires_at': {'N': str(t + CLAIM_SECONDS)}, 'ttl': {'N': str(t + 86400)},
                  **({'vcpus': {'N': str(vcpus)}} if vcpus is not None else {})},
            ConditionExpression='attribute_not_exists(job) OR expires_at < :now',
            ExpressionAttributeValues={':now': {'N': str(t)}})
        return nonce
    except dynamodb.exceptions.ConditionalCheckFailedException:
        return None


def active_claims(repo):
    """{job: (nonce, vcpus)} for the jobs this repo launched for (or is launching for) within
    CLAIM_SECONDS; vcpus is None for a claim made before claims recorded it. A consistent read:
    together with the serialized controller (reserved concurrency 1) it makes the repo cap hold
    even when DescribeInstances has not caught up with a burst of launches."""
    t, found, page = int(now().timestamp()), {}, {}
    while True:
        response = dynamodb.query(
            TableName=os.environ['CLAIMS_TABLE'], ConsistentRead=True,
            KeyConditionExpression='repo = :repo', ExpressionAttributeValues={':repo': {'S': repo}}, **page)
        found.update({item['job']['S']: (item.get('nonce', {}).get('S', ''),
                                         int(item['vcpus']['N']) if 'vcpus' in item else None)
                      for item in response.get('Items', []) if int(item['expires_at']['N']) >= t})
        if 'LastEvaluatedKey' not in response:
            return found
        page = {'ExclusiveStartKey': response['LastEvaluatedKey']}


def release(repo, job_id):
    try:
        with handoff():
            dynamodb.delete_item(TableName=os.environ['CLAIMS_TABLE'], Key={'repo': {'S': repo}, 'job': {'S': job_id}})
    except Exception as error:  # the claim then just expires
        print(f'{repo}: could not release the claim for job {job_id}: {type(error).__name__}')


def ensure_runner(repo, cfg, subnets, pat, job_id, size, live, token=None, busy=frozenset()):
    """Launch for `job_id` unless it already has a host or the repo is at its cap.

    The cap is `max` hosts and, where the repo sets `max_vcpus`, that many vCPUs too: the job waits when the vCPUs of
    the repo's hosts in use, plus those of the launches not listed yet (each claim's own), plus the vCPUs of the size
    this job asks for, would pass it. A repo without `max_vcpus` is capped by hosts alone.

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
    for job, (nonce, _) in list(claimed.items()):
        if nonce and any(t.startswith(nonce + '-') for t in tokens):
            release(repo, job)
            del claimed[job]
    if job_id in claimed:
        return 'claimed', token
    # Hosts by instance (two hosts can carry one JobId, above), plus claims for launches not in
    # `running` at all. A launch this invocation made is in `live` (with its ClientToken) but not
    # yet listed: its claim must stay, but it is one slot, not two.
    held = {i.get('ClientToken') or '' for i in running}
    unseen = {job for job, (nonce, _) in claimed.items() if not (nonce and any(t.startswith(nonce + '-') for t in held))}
    in_use = {i['InstanceId'] for i in running} | unseen
    if len(in_use) >= int(cfg['max']):
        print(f"{repo}: at its cap of {cfg['max']} runners; job {job_id} waits")
        return 'cap', token
    capped = cfg.get('max_vcpus') is not None
    need = size_vcpus(cfg, size) if capped else known_vcpus(cfg, size)
    if capped:
        # Each host once (`running` can list one twice: `live` and the fresh listing), by its own vCPUs; each unseen
        # launch by its claim's. A claim made before claims recorded vCPUs counts as the largest size: too high only
        # holds a job back until that claim's host is listed or the claim expires, where too low could pass the cap.
        hosts = {i['InstanceId']: i for i in running}
        used = (sum(host_vcpus(i, cfg) for i in hosts.values())
                + sum(claimed[job][1] if claimed[job][1] is not None else largest_vcpus(cfg) for job in unseen))
        if used + need > int(cfg['max_vcpus']):
            print(f"{repo}: at its cap of {cfg['max_vcpus']} vCPUs ({used} in use, an {size} runner needs {need}); "
                  f"job {job_id} waits")
            return 'cap', token
    nonce = claim(repo, job_id, need)
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
        live.append({'InstanceId': instance_id, 'Tags': [{'Key': 'JobId', 'Value': job_id}, {'Key': 'Size', 'Value': size}]
                     + ([{'Key': 'Vcpus', 'Value': str(need)}] if need is not None else []),
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
        elif runner is None and complete and age > BOOT_GRACE and not registered_now(repo, instance_id, pat):
            # The listing is from the start of the round: a slow boot may have registered since,
            # and could be starting a job. Recheck this one name before killing it.
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
    result = {'repo': repo}
    try:
        return reconcile_repo(repo, cfg, subnets, result)
    except OutOfTime as stop:
        print(f'{repo}: reconcile stopped at its time share, before {stop}')
        result['stopped'] = 'no time left'
        return result


def reconcile_repo(repo, cfg, subnets, result):
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
    # From here on, a stop at the deadline still reports these (live grows with each launch).
    result['outcomes'] = outcomes
    result['live'] = len(live)
    if cfg.get('vcpus'):
        result['vcpus'] = sum(host_vcpus(i, cfg) for i in live)
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
        # Under a vCPU cap a later, smaller job might still fit, but it waits as well: the jobs come
        # oldest first, and the one that waits keeps its turn for the room the next host frees.
        if out_of_time():
            outcomes['deferred'] = outcomes.get('deferred', 0) + 1
            break
        outcome, token = ensure_runner(repo, cfg, subnets, pat, job_id, size, live, token, busy)
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        result['live'] = len(live)
        if cfg.get('vcpus'):
            result['vcpus'] = sum(host_vcpus(i, cfg) for i in live)
        if outcome == 'cap':
            break
    print(f'{repo}: reconcile {outcomes or "nothing queued"}; {len(live)} live')
    return result


def publish_counts(results):
    """LiveRunners per repo, and in total when every repo was counted. The too-many and
    reconcile-silent alarms read this: AWS/EC2 has no per-tag instance count to alarm on. A total
    that silently counted a skipped or failed repo as zero would hide its hosts from the
    too-many alarm, so a round without every count publishes no total; rounds like that in a
    row trip reconcile-silent (missing = breaching), which is what they are.

    LiveVcpus, per repo, is the vCPUs of those same hosts: the per-repo vCPU alarm and Imagine's
    spend alarm read it (runner-app.tf). Nothing reads a vCPU total, so none is published."""
    data = [{'MetricName': 'LiveRunners', 'Dimensions': [{'Name': 'Repo', 'Value': r['repo']}],
             'Value': r['live'], 'Unit': 'Count'} for r in results if 'live' in r]
    data += [{'MetricName': 'LiveVcpus', 'Dimensions': [{'Name': 'Repo', 'Value': r['repo']}],
              'Value': r['vcpus'], 'Unit': 'Count'} for r in results if 'vcpus' in r]
    if all('live' in r for r in results):
        data.append({'MetricName': 'LiveRunners', 'Dimensions': [{'Name': 'Repo', 'Value': 'ALL'}],
                     'Value': sum(r['live'] for r in results), 'Unit': 'Count'})
    if data:
        cloudwatch.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=data)


def rotated(order, round_no):
    """The repos for this round, starting one further along each round (the reconcile runs every two minutes), so every
    repo goes first in turn: a slow one cannot always take the time the others need, and with more than two repos a
    reversal alone would never put the middle one first."""
    if not order:
        return order
    start = round_no % len(order)
    return order[start:] + order[:start]


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
        order = rotated(order, int(time.time() // 120))
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
    # A delivery is bounded by the invocation too: stopping at a deadline releases its claim,
    # where a Lambda timeout would leave the job blocked until the claim expired.
    DEADLINE[0] = started + (context.get_remaining_time_in_millis() / 1000 if context else 110) - RESERVE_SECONDS
    try:
        return deliver(repo, cfg, subnets, job, size)
    except OutOfTime as stop:
        print(f"{repo}: delivery for job {job['id']} stopped at the invocation's deadline, before {stop}")
        return {'repo': repo, 'job': job['id'], 'size': size, 'outcome': 'out of time'}
    finally:
        DEADLINE[0] = float('inf')


def deliver(repo, cfg, subnets, job, size):
    pat = repo_pat(cfg)
    if not pat:
        return {'skipped': 'no controller token'}
    # A delivery can wait in Lambda's async queue while the reconcile launches for its job, and a
    # short job can finish before the delivery runs: its event still says "queued". Ask GitHub.
    status = github('GET', f"/repos/{repo}/actions/jobs/{job['id']}", pat).get('status')
    if status != 'queued':
        print(f"{repo}: delivery for job {job['id']} is stale (now {status}); nothing to launch")
        return {'ignored': 'no longer queued'}
    outcome, _ = ensure_runner(repo, cfg, subnets, pat, str(job['id']), size, app_instances(repo))
    return {'repo': repo, 'job': job['id'], 'size': size, 'outcome': outcome}
