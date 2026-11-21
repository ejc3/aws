#!/usr/bin/env python3
"""Give a pristine production TLS secret the empty JSON document required by the coordinator."""
import json
import os
import uuid

SEED_STAGE = "FLEET_PROD_TLS_BOOTSTRAPPED"


def versions(client, arn):
    request = {"SecretId": arn, "IncludeDeprecated": True}
    stages = []
    while True:
        page = client.list_secret_version_ids(**request)
        stages.extend(v.get("VersionStages", []) for v in page["Versions"])
        if not page.get("NextToken"):
            return stages
        request["NextToken"] = page["NextToken"]


def bootstrap(client, arn):
    stages = versions(client, arn)
    if any("AWSCURRENT" in labels for labels in stages):
        return
    if stages:
        raise RuntimeError("Production TLS secret has versions but no AWSCURRENT. Inspect its version labels before recovery.")
    # AWS adds AWSCURRENT automatically to the first version. If another writer was first,
    # only our custom label is attached; never request or move AWSCURRENT ourselves.
    result = client.put_secret_value(
        SecretId=arn, ClientRequestToken=str(uuid.uuid5(uuid.NAMESPACE_URL, arn + "/" + SEED_STAGE)),
        SecretString="{}", VersionStages=[SEED_STAGE],
    )
    if "AWSCURRENT" not in result.get("VersionStages", []) and not any(
        "AWSCURRENT" in labels for labels in versions(client, arn)
    ):
        raise RuntimeError("No AWSCURRENT visible after bootstrapping production TLS. Retry the Terraform apply.")


def main():
    import boto3

    arn = os.environ["PROD_TLS_ARN"]
    region = os.environ["FLEET_REGION"]
    policy = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow",
        "Action": ["secretsmanager:ListSecretVersionIds", "secretsmanager:PutSecretValue"],
        "Resource": arn,
    }]}
    creds = boto3.client("sts", region_name=region).assume_role(
        RoleArn=os.environ["FLEET_ROLE"], RoleSessionName="fcvm-prod-tls-bootstrap",
        DurationSeconds=900, Policy=json.dumps(policy),
    )["Credentials"]
    client = boto3.client("secretsmanager", region_name=region, aws_access_key_id=creds["AccessKeyId"],
                          aws_secret_access_key=creds["SecretAccessKey"], aws_session_token=creds["SessionToken"])
    bootstrap(client, arn)


if __name__ == "__main__":
    main()
