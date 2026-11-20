#!/usr/bin/env python3
"""Audit the staging certificates for names in fcvm.dev outside the fleet's subzones (ejc3/aws#922).

Before the staging subzone certificate (#923), the staging coordinator held certificates for
`*.fcvm.dev` and `fcvm.dev`, and copies of their keys reached staging hosts and tiles. Those names
include the production apex and API, so #922 requires each one to be revoked, or shown unusable,
before production is exposed. This script only reads: it changes no secret, sends no ACME request
and revokes nothing. Revoking them is a separate, approved step under #922, after staging has
verifiably stopped serving them.

Run with the staging fleet administration role.

- No flag: list every distinct certificate in the versions of `fcvm-fleet/tls-wildcard` that names
  something in fcvm.dev outside `tiles-stage.fcvm.dev` and `tiles.fcvm.dev`, with its serial,
  expiry, names, how many versions hold it, and whether the key stored with it is its key (a
  revocation signed with the certificate's own key, RFC 8555 section 7.6, needs it: the
  coordinator used a new ACME account for every order and kept none).
- --check: prove each one revoked from the CRL it names. The CRL counts only when it is signed by
  the certificate's issuer (the intermediate stored with it, which must itself have signed the
  certificate), is current (thisUpdate not in the future, nextUpdate not
  passed) and, when it carries an issuing distribution point, names the URL it came from.
  Anything else is no status, never a pass. Exits non-zero unless every certificate is proven
  revoked.
"""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.error
import urllib.request

REGION = "us-east-2"
LEGACY = "fcvm-fleet/tls-wildcard"
# The fleet's own subzones: a certificate naming only these is not a parent-zone certificate.
SUBZONES = ("tiles-stage.fcvm.dev", "tiles.fcvm.dev")
PARENT = "fcvm.dev"
USER_AGENT = "ejc3-aws-fcvm-wildcard-audit/1"
CLOCK_SKEW = dt.timedelta(minutes=5)


def checked(condition, message):
    if not condition:
        raise ValueError(message)


def openssl(arguments, data, text=True):
    return subprocess.run(["openssl", *arguments], input=data, capture_output=True, check=True, text=text).stdout


def chain(pem):
    found = re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", pem, re.S)
    checked(found, "secret version holds no certificate")
    return [c + "\n" for c in found]


def names(cert):
    lines = openssl(["x509", "-noout", "-ext", "subjectAltName"], cert).splitlines()[1:]
    return sorted(n.strip().removeprefix("DNS:") for n in ",".join(lines).split(",") if n.strip())


def outside_subzones(name):
    host = name.removeprefix("*.")
    return (host == PARENT or host.endswith("." + PARENT)) and not any(
        host == z or host.endswith("." + z) for z in SUBZONES)


def fields(cert):
    out = openssl(["x509", "-noout", "-serial", "-enddate", "-fingerprint", "-sha256"], cert)
    values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {"serial": values["serial"].upper(), "not_after": values["notAfter"],
            "sha256": values["sha256 Fingerprint"].replace(":", "").lower()}


def key_matches(cert, key_pem):
    """Whether `key_pem` is the certificate's private key (compared by public key, nothing printed)."""
    try:
        public = openssl(["pkey", "-pubout", "-passin", "pass:"], key_pem)
    except subprocess.CalledProcessError:
        return False
    return public == openssl(["x509", "-noout", "-pubkey"], cert)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, b""


def crl_time(text, field):
    value = re.search(field + r"=(.+)", text)
    checked(value and value.group(1).strip() != "NONE", f"the CRL has no {field}")
    return dt.datetime.strptime(value.group(1).strip(), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=dt.timezone.utc)


def crl_status(cert, issuer, directory, now=None):
    """'revoked' or 'not revoked', from the CRL the certificate names, after proving that CRL is
    signed by the certificate's issuer, is current and covers the URL it came from.
    Raises ValueError when no such proof is possible: that is no status, never a pass."""
    now = now or dt.datetime.now(dt.timezone.utc)
    url = re.search(r"URI:(\S+)", openssl(["x509", "-noout", "-ext", "crlDistributionPoints"], cert))
    checked(url, "the certificate names no CRL")
    url = url.group(1)
    leaf_pem, issuer_pem = directory / "leaf.pem", directory / "issuer.pem"
    leaf_pem.write_text(cert)
    issuer_pem.write_text(issuer)
    # The stored intermediate must be the certificate's issuer, or a CRL it signed proves nothing.
    signed = subprocess.run(["openssl", "verify", "-partial_chain", "-no_check_time", "-CAfile", str(issuer_pem),
                             str(leaf_pem)], capture_output=True, text=True)
    checked(signed.returncode == 0, "the stored intermediate did not sign the certificate")
    status, body = fetch(url)
    checked(status == 200 and body, "the CRL did not download")
    crl = directory / "crl.der"
    crl.write_bytes(body)
    # openssl finds the signer in -CAfile by the CRL's issuer name, so a CRL naming another issuer, or
    # signed by another key under the issuer's name, fails here.
    proof = subprocess.run(["openssl", "crl", "-inform", "DER", "-in", str(crl), "-CAfile", str(issuer_pem), "-noout"],
                           capture_output=True, text=True)
    checked(proof.returncode == 0 and "verify OK" in proof.stdout + proof.stderr,
            "the CRL is not signed by the certificate's issuer")
    meta = openssl(["crl", "-inform", "DER", "-in", str(crl), "-noout", "-lastupdate", "-nextupdate"], None)
    checked(crl_time(meta, "lastUpdate") <= now + CLOCK_SKEW, "the CRL's thisUpdate is in the future")
    checked(crl_time(meta, "nextUpdate") > now, "the CRL is stale (nextUpdate has passed)")
    text = openssl(["crl", "-inform", "DER", "-in", str(crl), "-noout", "-text"], None)
    idp = re.search(r"X509v3 Issuing Distribution Point:.*?URI:(\S+)", text, re.S)
    checked(not idp or idp.group(1) == url, "the CRL covers another distribution point")
    serial = fields(cert)["serial"]
    listed = any(re.sub(r"\s", "", m).upper() == serial for m in re.findall(r"Serial Number:\s*([0-9A-Fa-f]+)", text))
    return "revoked" if listed else "not revoked"


def collect(secrets):
    """The distinct parent-zone certificates in every version of the legacy secret."""
    versions, token = [], None
    while True:
        page = secrets.list_secret_version_ids(SecretId=LEGACY, IncludeDeprecated=True, MaxResults=100,
                                               **({"NextToken": token} if token else {}))
        versions += [v["VersionId"] for v in page["Versions"]]
        token = page.get("NextToken")
        if not token:
            break
    found = {}
    for version in versions:
        doc = json.loads(secrets.get_secret_value(SecretId=LEGACY, VersionId=version)["SecretString"])
        certs = chain(doc["cert"])
        cert = certs[0]
        if not any(outside_subzones(n) for n in names(cert)):
            continue
        info = fields(cert)
        entry = found.setdefault(info["sha256"], dict(info, cert=cert, issuer=certs[1] if len(certs) > 1 else None,
                                                      names=names(cert), versions=[], key=False))
        entry["versions"].append(version)
        entry["key"] |= key_matches(cert, doc.get("key", ""))
    return list(found.values())


def run(secrets, directory, check=False, now=None):
    """List, or with `check` prove each certificate revoked. Returns whether the run passed:
    a listing always does, a check only when every certificate is proven revoked."""
    certificates = collect(secrets)
    print(f"{len(certificates)} certificate(s) for names outside the fleet subzones in {LEGACY}")
    passed = True
    for c in certificates:
        label = (f"serial {c['serial']} expires {c['not_after']} names {' '.join(c['names'])} "
                 f"versions {len(c['versions'])} key {'stored' if c['key'] else 'MISSING'}")
        if not check:
            print(label)
            continue
        with tempfile.TemporaryDirectory(dir=directory) as d:
            try:
                checked(c["issuer"], "the secret version stores no issuer certificate")
                state = crl_status(c["cert"], c["issuer"], Path(d), now)
            except (ValueError, subprocess.CalledProcessError) as error:
                state = f"NO STATUS ({error})"
        passed &= state == "revoked"
        print(f"{state}: {label}")
    return passed


if __name__ == "__main__":
    import boto3

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="prove each certificate revoked from its CRL")
    arguments = parser.parse_args()
    os.umask(0o077)
    with tempfile.TemporaryDirectory(dir=os.getenv("XDG_RUNTIME_DIR") or "/dev/shm") as workspace:
        ok = run(boto3.client("secretsmanager", region_name=REGION), Path(workspace), arguments.check)
    raise SystemExit(0 if ok else 1)
