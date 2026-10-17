"""github-app-runner-image: builds the prebuilt images Pattern C's controller may launch (runner-app-image.tf).

The same shape as fcvm's image pipeline (github-ami-builder.tf): a named, tagged builder host provisions itself from user
data and reports only by a tag; whoever started it makes the image from it and terminates it. Here that is this function,
not a GitHub workflow (ejc3/aws's workflows carry no AWS identity), and every step is re-derived from EC2 on each call,
so nothing is stored between them:

  build (weekly, or on demand)  launch one on-demand builder per image name from Canonical's Ubuntu 24.04, unless one is
                                already in flight. Its user data is runner-app/image-build.sh: bootstrap.sh's own install
                                (prepare-only), then runner-app/images/<script> at the pinned versions. It tags itself
                                BuildStatus=complete and powers off; it stops, it is not terminated.
  advance (every 5 minutes)     a stopped, complete builder -> CreateImage, tagged Purpose/Image/Repo/BuiltFrom;
                                its image available       -> terminate the builder, keep the newest KEEP_IMAGES images of
                                                             that name, deregister the rest and delete their snapshots;
                                anything else stopped, an image that failed, or a builder past its time -> print its
                                console, terminate it, and fail the invocation (github-app-runner-image-errors).

Invoke with {"build": true} to build every image now, or {"build": ["<name>"]} for one.
"""
import gzip
import json
import os
import re
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config

REGION = 'us-west-1'
CANONICAL_AMI_PARAM = '/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id'
# The tag every image this pipeline makes carries, and the only images the controller's prebuilt-image launch statement
# admits (runner-app.tf). Not fcvm's Purpose=github-runner: fcvm's launcher and the Ohio replicator pick by that tag.
PURPOSE = 'github-app-runner-image'
BUILDER = 'github-app-runner-image-builder'
ACTIVE = ['pending', 'running', 'stopping', 'stopped']
FAILED_IMAGE_STATES = {'failed', 'error', 'invalid'}

KEEP = int(os.environ.get('KEEP_IMAGES', '3'))
BUILD_TIMEOUT = timedelta(minutes=int(os.environ.get('BUILD_TIMEOUT_MINUTES', '60')))
IMAGE_TIMEOUT = timedelta(minutes=int(os.environ.get('IMAGE_TIMEOUT_MINUTES', '120')))
VOLUME_GB = int(os.environ.get('BUILDER_VOLUME_GB', '30'))
# A snapshot no registered image names is deleted only once it is this old: CreateImage tags a snapshot before the image
# lists it.
ORPHAN_AGE = timedelta(days=1)
CAPACITY_CODES = {'InsufficientInstanceCapacity', 'Unsupported', 'InsufficientFreeAddressesInSubnet',
                  'UnfulfillableCapacity'}

NAME = re.compile(r'^[a-z0-9][a-z0-9-]{0,62}$')
VERSION_KEY = re.compile(r'^[A-Z][A-Z0-9_]{0,63}$')
# Single-quoted into a shell script: no quote, space or `$` can reach it.
VERSION_VALUE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/@+,-]{0,199}$')

HERE = os.path.dirname(os.path.abspath(__file__))
BOUNDED = Config(connect_timeout=3, read_timeout=10, retries={'total_max_attempts': 2})
ec2 = boto3.client('ec2', region_name=REGION, config=BOUNDED)
ssm = boto3.client('ssm', region_name=REGION, config=BOUNDED)


class BuildFailed(Exception):
    pass


def now():
    return datetime.now(timezone.utc)


def tag(resource, key):
    return next((t['Value'] for t in resource.get('Tags', []) if t['Key'] == key), None)


def images_config():
    images = json.loads(os.environ['IMAGES'])
    for name, cfg in images.items():
        if not NAME.match(name) or not re.match(r'^[A-Za-z0-9._-]+\.sh$', cfg['script']):
            raise ValueError(f'bad image name or script: {name!r} {cfg.get("script")!r}')
        for key, value in cfg['versions'].items():
            if not VERSION_KEY.match(key) or not VERSION_VALUE.match(value):
                raise ValueError(f'{name}: bad version pin {key}={value!r}')
    return images


def read(relative):
    with open(os.path.join(HERE, relative)) as f:
        return f.read()


def embed(script, marker, body):
    """`body` in place of `marker`, inside a quoted heredoc whose end line it must not contain."""
    end = {'@@BOOTSTRAP@@': 'GITHUB_APP_RUNNER_BOOTSTRAP', '@@IMAGE_SCRIPT@@': 'GITHUB_APP_RUNNER_IMAGE'}[marker]
    if end in body.splitlines():
        raise ValueError(f'{marker} contains its heredoc terminator')
    return script.replace(marker, body.rstrip('\n'))


def user_data(name, cfg):
    """The builder's user data, gzipped (cloud-init reads it either way; this keeps it far under EC2's 16 KB)."""
    exports = '\n'.join(f"export {k}='{v}'" for k, v in sorted(cfg['versions'].items()))
    bootstrap = read('bootstrap.sh').replace('@@REPO@@', cfg['repo']).replace('@@LABELS@@', '')
    script = (read('image-build.sh').replace('@@IMAGE@@', name).replace('@@REPO@@', cfg['repo'])
              .replace('@@VERSIONS@@', exports))
    script = embed(script, '@@BOOTSTRAP@@', bootstrap)
    script = embed(script, '@@IMAGE_SCRIPT@@', read(os.path.join('images', cfg['script'])))
    return gzip.compress(script.encode(), mtime=0)


def builders(name):
    pages = ec2.get_paginator('describe_instances').paginate(Filters=[
        {'Name': 'tag:Name', 'Values': [BUILDER]},
        {'Name': 'tag:Image', 'Values': [name]},
        {'Name': 'instance-state-name', 'Values': ACTIVE},
    ])
    return [i for page in pages for r in page['Reservations'] for i in r['Instances']]


def owned_images(name):
    """This pipeline's images of `name`: owned by this account and tagged by it, whatever the filters let through."""
    account = os.environ['ACCOUNT_ID']
    listed = ec2.describe_images(Owners=[account], Filters=[
        {'Name': 'tag:Purpose', 'Values': [PURPOSE]},
        {'Name': 'tag:Image', 'Values': [name]},
    ])['Images']
    return [i for i in listed if i.get('OwnerId') == account and tag(i, 'Purpose') == PURPOSE and tag(i, 'Image') == name]


def start(name, cfg, subnets):
    if builders(name):
        print(f'{name}: a build is already in flight')
        return 'building'
    ami = ssm.get_parameter(Name=CANONICAL_AMI_PARAM)['Parameter']['Value']
    data = user_data(name, cfg)
    last = None
    for subnet in subnets:
        for instance_type in cfg['builder_types']:
            try:
                response = ec2.run_instances(
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
                    # On demand, and stopped (not terminated) when it powers off: an image is made from a stopped host.
                    InstanceInitiatedShutdownBehavior='stop',
                    TagSpecifications=[
                        {'ResourceType': 'instance', 'Tags': [
                            {'Key': 'Name', 'Value': BUILDER}, {'Key': 'Image', 'Value': name},
                            {'Key': 'Repo', 'Value': cfg['repo']}, {'Key': 'BuildStatus', 'Value': 'starting'}]},
                        {'ResourceType': 'volume', 'Tags': [{'Key': 'Name', 'Value': BUILDER}]},
                        {'ResourceType': 'network-interface', 'Tags': [{'Key': 'Name', 'Value': BUILDER}]},
                    ])
            except Exception as error:
                code = (getattr(error, 'response', None) or {}).get('Error', {}).get('Code')
                if code in CAPACITY_CODES:
                    print(f"{name}: {instance_type} in {subnet['availability_zone']} refused: {code}")
                    last = error
                    continue
                raise
            instance_id = response['Instances'][0]['InstanceId']
            print(f'{name}: builder {instance_id} ({instance_type}) launched from {ami}')
            return 'started'
    raise BuildFailed(f'{name}: every builder pool refused: {last}')


def console(instance_id):
    try:
        return ec2.get_console_output(InstanceId=instance_id, Latest=True).get('Output', '')[-6000:]
    except Exception as error:
        return f'(no console output: {type(error).__name__})'


def give_up(name, builder, why):
    instance_id = builder['InstanceId']
    print(f'{name}: builder {instance_id} {why}; its console:\n{console(instance_id)}')
    ec2.terminate_instances(InstanceIds=[instance_id])
    return f'{name}: builder {instance_id} {why}'


def make_image(name, cfg, builder):
    instance_id = builder['InstanceId']
    stamp = now().strftime('%Y%m%d-%H%M')
    pins = ', '.join(f'{k}={v}' for k, v in sorted(cfg['versions'].items()))
    shared = [{'Key': 'Purpose', 'Value': PURPOSE}, {'Key': 'Image', 'Value': name}, {'Key': 'Repo', 'Value': cfg['repo']}]
    image_id = ec2.create_image(
        InstanceId=instance_id, Name=f'{name}-{stamp}', Description=f'{name} for {cfg["repo"]}: {pins}'[:255],
        TagSpecifications=[
            {'ResourceType': 'image', 'Tags': [{'Key': 'Name', 'Value': f'{name}-{stamp}'},
                                               {'Key': 'BuiltFrom', 'Value': instance_id}] + shared},
            {'ResourceType': 'snapshot', 'Tags': [{'Key': 'Name', 'Value': f'{name}-{stamp}'}] + shared},
        ])['ImageId']
    print(f'{name}: imaging builder {instance_id} as {image_id}')
    return image_id


def advance(name, cfg):
    """Move every in-flight build of `name` one step; return what failed."""
    problems = []
    for builder in builders(name):
        instance_id, state = builder['InstanceId'], builder['State']['Name']
        age = now() - builder['LaunchTime']
        made = [i for i in owned_images(name) if tag(i, 'BuiltFrom') == instance_id]
        if made:
            image = made[0]
            if image['State'] == 'available':
                print(f"{name}: {image['ImageId']} is available; terminating builder {instance_id}")
                ec2.terminate_instances(InstanceIds=[instance_id])
            elif image['State'] in FAILED_IMAGE_STATES:
                problems.append(give_up(name, builder, f"was imaged as {image['ImageId']}, which {image['State']}"))
            elif age > IMAGE_TIMEOUT:
                problems.append(give_up(name, builder, f"was imaged as {image['ImageId']}, still {image['State']} after {age}"))
        elif state == 'stopped' and tag(builder, 'BuildStatus') == 'complete':
            make_image(name, cfg, builder)
        elif state == 'stopped':
            problems.append(give_up(name, builder, f"stopped without completing (BuildStatus={tag(builder, 'BuildStatus')})"))
        elif age > BUILD_TIMEOUT:
            problems.append(give_up(name, builder, f'is still {state} after {age}'))
    return problems


def snapshots_of(image):
    return [m['Ebs']['SnapshotId'] for m in image.get('BlockDeviceMappings', []) if m.get('Ebs', {}).get('SnapshotId')]


def prune(name):
    """Keep the newest KEEP available images of `name`; deregister the rest, and failed ones, with their snapshots."""
    images = owned_images(name)
    available = sorted((i for i in images if i['State'] == 'available'), key=lambda i: i['CreationDate'], reverse=True)
    doomed = available[KEEP:] + [i for i in images if i['State'] in FAILED_IMAGE_STATES]
    for image in doomed:
        print(f"{name}: deregistering {image['ImageId']} ({image['CreationDate']}) and its snapshots {snapshots_of(image)}")
        ec2.deregister_image(ImageId=image['ImageId'])
        for snapshot in snapshots_of(image):
            ec2.delete_snapshot(SnapshotId=snapshot)
    # A snapshot whose delete failed in an earlier round is named by no image now. It is deleted once it is a day old:
    # CreateImage tags a snapshot before its image lists it.
    named = {s for i in images for s in snapshots_of(i)}
    account = os.environ['ACCOUNT_ID']
    listed = ec2.describe_snapshots(OwnerIds=[account], Filters=[
        {'Name': 'tag:Purpose', 'Values': [PURPOSE]}, {'Name': 'tag:Image', 'Values': [name]}])['Snapshots']
    for snapshot in listed:
        if (snapshot['SnapshotId'] not in named and snapshot.get('OwnerId') == account
                and tag(snapshot, 'Purpose') == PURPOSE and tag(snapshot, 'Image') == name
                and snapshot.get('State') == 'completed' and now() - snapshot['StartTime'] > ORPHAN_AGE):
            print(f"{name}: deleting snapshot {snapshot['SnapshotId']}, which no image names")
            ec2.delete_snapshot(SnapshotId=snapshot['SnapshotId'])
    return len(doomed)


def handler(event, context):
    images = images_config()
    subnets = json.loads(os.environ['LAUNCH_SUBNETS'])
    build = (event or {}).get('build')
    wanted = sorted(images) if build is True else list(build or [])
    unknown = [n for n in wanted if n not in images]
    if unknown:
        raise ValueError(f'no such image: {unknown}; known: {sorted(images)}')
    problems, result = [], {}
    for name in sorted(images):
        cfg = images[name]
        try:
            problems += advance(name, cfg)
            result[name] = start(name, cfg, subnets) if name in wanted else 'advanced'
            prune(name)
        except BuildFailed as error:
            problems.append(str(error))
    if problems:
        raise BuildFailed('; '.join(problems))
    return result
