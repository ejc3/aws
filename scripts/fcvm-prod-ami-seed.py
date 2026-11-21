#!/usr/bin/env python3
"""Read-only Terraform external checks before the first production fleet AMI pointer is created."""
import json
import os
import re
import subprocess
import sys


# The fleet account service control policy permits EC2 only in these regions.
FLEET_REGIONS = ("us-east-1", "us-east-2", "us-west-2")
IMAGE_ID = re.compile(r"ami-[0-9a-f]{17}")


def require(ok, message):
    if not ok:
        raise ValueError(message)


def validate_image_id(value):
    require(isinstance(value, str) and IMAGE_ID.fullmatch(value), "invalid fleet AMI ID")


def release_catalogs(release, region):
    previous = release["previous"]
    require(previous is not None or release["generation"] == 0, "the release has no rollback manifest")
    catalogs = []
    for manifest, field in ((release, "images"), (previous, "previous_images")):
        if manifest is None:
            continue
        for kind in ("host", "tile"):
            catalog = manifest[kind + "_amis"]
            require(isinstance(catalog, dict) and region in catalog, "missing fleet release region")
            for where, arches in catalog.items():
                require(where in FLEET_REGIONS, "unsupported fleet AMI region")
                require(isinstance(arches, dict) and arches, "missing regional AMI catalog")
                for image_id in arches.values():
                    validate_image_id(image_id)
            require("x86_64" in catalog[region], "missing x86_64 release AMI")
            catalogs.append((field, kind, catalog))
    return catalogs


def validate_copies(copies, catalogs, region, regions):
    for field, kind, catalog in catalogs:
        expected = copies[field]
        require(isinstance(expected, dict) and set(expected) == {region, *regions},
                f"{field} does not cover all release copy regions")
        for pair in expected.values():
            require(isinstance(pair, dict) and set(pair) == {"host", "tile"}, f"{field} needs both host and tile AMIs")
            validate_image_id(pair[kind])
        require(expected[region][kind] == catalog[region]["x86_64"], f"{field} does not match the {kind} release in {region}")


def check_images(client, account, region, catalogs, regions, copies):
    """Describe every named image in its own region; copied pairs must connect to the matching release."""
    images = {}

    def image(where, image_id):
        key = where, image_id
        if key not in images:
            rows = client("ec2", where).describe_images(ImageIds=[image_id], Owners=[account])["Images"]
            require(len(rows) == 1 and rows[0]["ImageId"] == image_id and rows[0]["OwnerId"] == account
                    and rows[0]["State"] == "available" and rows[0]["Public"] is False,
                    f"fleet AMI {image_id} is missing, public, or unavailable in {where}")
            images[key] = rows[0]
        return images[key]

    for field, kind, catalog in catalogs:
        for where, arches in catalog.items():
            for arch, image_id in arches.items():
                require(image(where, image_id)["Architecture"] == arch,
                        f"{kind} AMI {image_id} is not {arch} in {where}")
        if copies is None:
            continue
        east = catalog[region]["x86_64"]
        source = image(region, east)
        for where in regions:
            target_id = copies[field][where][kind]
            target = image(where, target_id)
            require(target["Architecture"] == "x86_64" and (
                (target.get("SourceImageRegion"), target.get("SourceImageId")) == (region, east) or
                (source.get("SourceImageRegion"), source.get("SourceImageId")) == (where, target_id)),
                f"{field} {kind} in {where} is not linked to the release AMI in {region}")


def run(mode, query, client):
    region = query["region"]
    require(region in FLEET_REGIONS, "unsupported fleet AMI region")
    if mode == "present":
        ssm = client("ssm", region)
        try:
            ssm.get_parameter(Name=query["prod"], WithDecryption=False)
        except ssm.exceptions.ParameterNotFound:
            return {"present": "false"}
        return {"present": "true"}
    require(mode == "verify", "unknown AMI seed check")
    if query["when"] == "existing":
        return {"verified": "true"}  # Terraform ignores value after creation and forbids replacement.
    release = json.loads(query["release"])
    regions = json.loads(query["copy_regions"])
    require(isinstance(regions, list) and all(r in FLEET_REGIONS and r != region for r in regions)
            and len(regions) == len(set(regions)), "invalid fleet AMI copy regions")
    catalogs = release_catalogs(release, region)
    copies = None
    if query["stage"]:
        ssm = client("ssm", region)
        stage = ssm.get_parameter(Name=query["stage"], WithDecryption=False)["Parameter"]
        require(str(stage["Version"]) == query["version"] and json.loads(stage["Value"]) == release,
                "staging AMI release changed after planning; create a fresh plan")
        copies = json.loads(ssm.get_parameter(Name=query["stage"] + "-copies", WithDecryption=False)["Parameter"]["Value"])
        require(copies["version"] == 1 and all(copies[k] == release[k] for k in ("generation", "source_sha", "build_id")),
                "regional AMI copies do not match the planned staging release")
        validate_copies(copies, catalogs, region, regions)
    check_images(client, query["role"].split(":")[4], region, catalogs, regions, copies)
    return {"verified": "true"}


def main():
    import boto3

    query = json.load(sys.stdin)
    mode = sys.argv[1]
    session = None
    class EC2:
        # The admin hosts' system boto3 model discards SourceImageId/SourceImageRegion.
        # Their AWS CLI v2 returns the actual EC2 copy lineage.
        def __init__(self, region):
            self.region = region

        def describe_images(self, *, ImageIds, Owners):
            creds = session.get_credentials().get_frozen_credentials()
            env = dict(os.environ, AWS_ACCESS_KEY_ID=creds.access_key, AWS_SECRET_ACCESS_KEY=creds.secret_key,
                       AWS_SESSION_TOKEN=creds.token, AWS_CLI_AUTO_PROMPT="off")
            response = subprocess.run(["aws", "ec2", "describe-images", "--region", self.region,
                                       "--owners", *Owners, "--image-ids", *ImageIds, "--output", "json",
                                       "--no-cli-pager"], env=env, capture_output=True, text=True, check=True, timeout=60)
            return json.loads(response.stdout)

    def client(service, region):
        nonlocal session
        if session is None:
            account = query["role"].split(":")[4]
            names = [query["prod"]] if mode == "present" else []
            statements = []
            if mode == "verify":
                statements.append({"Effect": "Allow", "Action": "ec2:DescribeImages", "Resource": "*"})
                if query["stage"]:
                    names.extend([query["stage"], query["stage"] + "-copies"])
            if names:
                statements.append({"Effect": "Allow", "Action": "ssm:GetParameter",
                                   "Resource": [f"arn:aws:ssm:{region}:{account}:parameter{name}" for name in names]})
            creds = boto3.client("sts", region_name=region).assume_role(
                RoleArn=query["role"], RoleSessionName="fcvm-prod-ami-seed-check",
                Policy=json.dumps({"Version": "2012-10-17", "Statement": statements}))["Credentials"]
            session = boto3.Session(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                                    aws_session_token=creds["SessionToken"])
        return EC2(region) if service == "ec2" else session.client(service, region_name=region)
    try:
        verdict = run(mode, query, client)
    except KeyError as error:
        sys.exit(f"Production seed: required release, copy or image field is missing: {error}")
    except ValueError as error:
        sys.exit(f"Production seed: {error}")
    print(json.dumps(verdict))


if __name__ == "__main__":
    main()
