#!/usr/bin/env python3
"""Verify the current staging certificate or restore a verified staging backup to an empty secret.

Use the staging fleet administration role. The coordinator renews the wildcard using its
delegated DNS authority. Restore accepts its staging secret JSON or a renderer’s protected tls.json.
"""

import argparse
import datetime as dt
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

REGION = "us-east-2"
ZONE = "tiles-stage.fcvm.dev"
STAGE = "fcvm-fleet/stage/tls"
LEGACY = "fcvm-fleet/tls-wildcard"
LABEL = "FLEET_STAGE_TLS_BOOTSTRAPPED"


def checked(condition, message):
    if not condition:
        raise ValueError(message)


def openssl(arguments, pem):
    return subprocess.run(["openssl", *arguments], input=pem, text=True, capture_output=True, check=True).stdout.strip()


def cert_date(cert, date="enddate"):
    value = openssl(["x509", "-noout", "-" + date], cert).split("=", 1)[1]
    return dt.datetime.strptime(value, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=dt.timezone.utc)


def verify(doc, legacy, directory, *, recovery=False):
    cert = doc["cert"]
    chain = re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", cert, re.S)
    checked(len(chain) >= 2 and "".join(cert.split()) == "".join("".join(chain).split()),
            "staging certificate requires its intermediate chain")
    sans = openssl(["x509", "-noout", "-ext", "subjectAltName"], chain[0]).splitlines()[1:]
    checked("".join(sans).strip() == "DNS:*." + ZONE and doc["domains"] == ["*." + ZONE],
            "unexpected certificate SANs")
    leaf_key = openssl(["x509", "-noout", "-pubkey"], chain[0])
    checked(openssl(["pkey", "-pubout", "-passin", "pass:"], doc["key"]) == leaf_key, "staging certificate/key mismatch")
    checked(openssl(["x509", "-noout", "-pubkey"], legacy["cert"]) != leaf_key, "legacy wildcard key reused")
    checked(bool(legacy["scene_token"]) and hmac.compare_digest(doc["scene_token"], legacy["scene_token"]),
            "staging scene token differs from the existing fleet")
    deadline = cert_date(chain[0])
    checked(dt.datetime.fromisoformat(doc["not_after"].replace("Z", "+00:00")) == deadline,
            "staging certificate expiry disagrees with the signed leaf")
    remaining = deadline - dt.datetime.now(dt.timezone.utc)
    checked(remaining > dt.timedelta(), "staging certificate has expired")
    if not recovery:
        checked(remaining > dt.timedelta(days=30), "staging certificate expiry is inside the renewal window")
    intermediates = directory / "chain.pem"
    intermediates.write_text("\n".join(chain[1:]) + "\n")
    openssl(["verify", "-purpose", "sslserver", "-verify_hostname", "probe." + ZONE,
             "-untrusted", str(intermediates)], chain[0])


def bootstrap(secrets, directory, restore_file=None):
    metadata = secrets.describe_secret(SecretId=STAGE)
    versions = metadata.get("VersionIdsToStages", {})
    legacy = json.loads(secrets.get_secret_value(SecretId=LEGACY)["SecretString"])
    legacy = {field: legacy[field] for field in ("cert", "scene_token")}
    current = [version for version, stages in versions.items() if "AWSCURRENT" in stages]
    if current:
        doc = json.loads(secrets.get_secret_value(SecretId=STAGE, VersionStage="AWSCURRENT")["SecretString"])
        verify(doc, legacy, directory, recovery=restore_file is not None)
        checked(any(LABEL in stages for stages in versions.values()), "missing verified staging bootstrap label")
        print("Verified staging certificate:", *doc["domains"], "expires", doc["not_after"])
        return
    checked(restore_file and not versions, "no current staging certificate; --restore requires an empty secret")
    backup = json.loads(restore_file.read_text())
    # Renderer tls.json keeps the signed chain, key and scene token but omits issuance metadata.
    cert = backup["cert"]
    doc = {field: backup[field] for field in ("cert", "key", "scene_token")}
    doc.update(domains=["*." + ZONE], not_after=cert_date(cert).isoformat(),
               issued_at=backup.get("issued_at") or cert_date(cert, "startdate").isoformat(),
               issuer=openssl(["x509", "-noout", "-issuer"], cert).removeprefix("issuer=").strip())
    verify(doc, legacy, directory, recovery=True)
    checked(not secrets.describe_secret(SecretId=STAGE).get("VersionIdsToStages"),
            "another writer populated the staging secret during certificate recovery")
    # Concurrent bootstraps cannot overwrite one another with different private keys.
    request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, metadata["ARN"] + "/" + LABEL))
    secrets.put_secret_value(SecretId=STAGE, ClientRequestToken=request_id, SecretString=json.dumps(doc),
                             VersionStages=["AWSCURRENT", LABEL])
    bootstrap(secrets, directory, restore_file)


if __name__ == "__main__":
    import boto3

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore", type=Path, metavar="STAGE_BACKUP_JSON",
                        help="restore a verified stage TLS backup to an empty secret")
    arguments = parser.parse_args()
    os.umask(0o077)
    with tempfile.TemporaryDirectory(dir=os.getenv("XDG_RUNTIME_DIR") or "/dev/shm") as workspace:
        bootstrap(boto3.client("secretsmanager", region_name=REGION), Path(workspace), arguments.restore)
