#!/usr/bin/env python3
"""Bootstrap the staging wildcard before the coordinator switches Secrets Manager paths.

Run with the staging fleet administration role. With no flag, verify the current stage
certificate without making changes. With --issue, seed a previously empty secret using
the fleet's original lego/Cloudflare DNS-01 method while the legacy CNAME still exists.
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


def expiry(cert):
    value = openssl(["x509", "-noout", "-enddate"], cert).removeprefix("notAfter=")
    return dt.datetime.strptime(value, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=dt.timezone.utc)


def verify(doc, legacy, directory):
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
    deadline = expiry(chain[0])
    checked(dt.datetime.fromisoformat(doc["not_after"].replace("Z", "+00:00")) == deadline
            and deadline > dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30),
            "staging certificate expiry does not meet the renewal window")
    intermediates = directory / "chain.pem"
    intermediates.write_text("\n".join(chain[1:]) + "\n")
    openssl(["verify", "-purpose", "sslserver", "-verify_hostname", "probe." + ZONE,
             "-untrusted", str(intermediates)], chain[0])


def bootstrap(secrets, directory, issue):
    metadata = secrets.describe_secret(SecretId=STAGE)
    versions = metadata.get("VersionIdsToStages", {})
    legacy = json.loads(secrets.get_secret_value(SecretId=LEGACY)["SecretString"])
    legacy = {field: legacy[field] for field in ("cert", "scene_token")}
    current = [version for version, stages in versions.items() if "AWSCURRENT" in stages]
    if current:
        doc = json.loads(secrets.get_secret_value(SecretId=STAGE, VersionStage="AWSCURRENT")["SecretString"])
        verify(doc, legacy, directory)
        checked(any(LABEL in stages for stages in versions.values()), "missing verified staging bootstrap label")
        print("Verified staging certificate:", *doc["domains"], "expires", doc["not_after"])
        return
    checked(issue and not versions, "staging TLS has no current version; --issue requires an empty secret")
    cname = subprocess.check_output(["dig", "+short", "CNAME", "_acme-challenge." + ZONE], text=True).strip()
    checked(cname == "_acme-challenge.fcvm.dev.", "staging ACME CNAME does not delegate to the legacy issuer")
    # Use exactly one ACME identifier. The legacy issuer also requests the parent apex.
    token = secrets.get_secret_value(SecretId="fcvm-fleet/cloudflare-dns-token")["SecretString"]
    subprocess.run(["lego", "--accept-tos", "--email", "acme@fcvm.dev", "--path", str(directory),
                    "--server", "https://acme-v02.api.letsencrypt.org/directory", "--dns", "cloudflare",
                    "--dns.resolvers", "1.1.1.1:53", "--key-type", "ec256", "--domains", "*." + ZONE, "run"],
                   env={"PATH": os.environ["PATH"], "HOME": str(directory), "CLOUDFLARE_DNS_API_TOKEN": token,
                        "CLOUDFLARE_PROPAGATION_TIMEOUT": "300"}, check=True)
    cert_dir = directory / "certificates"
    cert = (cert_dir / ("_." + ZONE + ".crt")).read_text()
    doc = {"cert": cert, "key": (cert_dir / ("_." + ZONE + ".key")).read_text(), "domains": ["*." + ZONE],
           "not_after": expiry(cert).isoformat(), "issued_at": dt.datetime.now(dt.timezone.utc).isoformat(),
           "issuer": openssl(["x509", "-noout", "-issuer"], cert).removeprefix("issuer=").strip(), "scene_token": legacy["scene_token"]}
    verify(doc, legacy, directory)
    checked(not secrets.describe_secret(SecretId=STAGE).get("VersionIdsToStages"),
            "another writer populated the staging secret during certificate issuance")
    # Concurrent bootstraps cannot overwrite one another with different private keys.
    request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, metadata["ARN"] + "/" + LABEL))
    secrets.put_secret_value(SecretId=STAGE, ClientRequestToken=request_id, SecretString=json.dumps(doc),
                             VersionStages=["AWSCURRENT", LABEL])
    bootstrap(secrets, directory, False)


if __name__ == "__main__":
    import boto3

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", action="store_true")
    arguments = parser.parse_args()
    os.umask(0o077)
    with tempfile.TemporaryDirectory(dir=os.getenv("XDG_RUNTIME_DIR") or "/dev/shm") as workspace:
        bootstrap(boto3.client("secretsmanager", region_name=REGION), Path(workspace), arguments.issue)
