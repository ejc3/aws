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
- --check: prove each one revoked. The certificate and its intermediates come from a writable
  secret, so before any request the certificate must verify through its stored intermediates
  (untrusted) to a root in this host's trust store. The CRL it names must be a Let's Encrypt CRL
  endpoint (`http://<issuer>.c.lencr.org/<n>.crl`); the fetch follows no redirect and reads at
  most 8 MiB. OpenSSL then verifies the certificate again with that CRL (`verify -crl_check`),
  which decides the CRL's issuer, signature, freshness and issuing-distribution-point scope.
  Only a run whose one error is "certificate revoked" at depth 0 proves revocation; a clean
  verify means not revoked, and any other error is no status. Exits non-zero unless the
  inventory is not empty and every certificate is proven revoked: an empty inventory cannot
  prove the known certificates revoked.
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
# Let's Encrypt's intermediates' CRL endpoints, the only URLs --check fetches.
LETS_ENCRYPT_CRL = r"http://[a-z0-9]+\.c\.lencr\.org/[0-9]+\.crl"
MAX_CRL_BYTES = 8 << 20
# openssl verify's X509_V_ERR_CERT_REVOKED.
CERT_REVOKED = "23"


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


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(url, max_bytes):
    """GET `url` with no redirect followed and at most `max_bytes` read: (status, body or None)."""
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(req, timeout=30) as response:
            body = response.read(max_bytes + 1)
            return response.status, (body if len(body) <= max_bytes else None)
    except urllib.error.HTTPError as error:
        return error.code, b""


def verify(leaf_pem, bundle_pem, trust, at, crl_pem=None):
    """openssl verify of the leaf through the untrusted bundle: the (error, depth) pairs it reports."""
    result = subprocess.run(["openssl", "verify", "-attime", str(int(at.timestamp())),
                             *(["-CAfile", str(trust)] if trust else []), "-untrusted", str(bundle_pem),
                             *(["-crl_check", "-CRLfile", str(crl_pem)] if crl_pem else []), str(leaf_pem)],
                            capture_output=True, text=True)
    errors = re.findall(r"error (\d+) at (\d+) depth lookup", result.stdout + result.stderr)
    if result.returncode == 0 and not errors:
        return []
    return errors or [("unknown", "?")]


def crl_status(cert, intermediates, directory, now=None, trust=None, crl_url=LETS_ENCRYPT_CRL,
               max_bytes=MAX_CRL_BYTES):
    """'revoked' or 'not revoked' for `cert`, from the CRL it names. Raises ValueError when no proof
    is possible: that is no status, never a pass. `trust` is a CA file to use instead of this
    host's trust store (tests)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    checked(intermediates, "the secret version stores no intermediate")
    leaf_pem, bundle_pem = directory / "leaf.pem", directory / "bundle.pem"
    leaf_pem.write_text(cert)
    bundle_pem.write_text("".join(intermediates))
    # The stored chain is untrusted until it reaches a root this host trusts; nothing is fetched first.
    checked(not verify(leaf_pem, bundle_pem, trust, now),
            "the certificate does not verify through its stored intermediates to a trusted root")
    url = re.search(r"URI:(\S+)", openssl(["x509", "-noout", "-ext", "crlDistributionPoints"], cert))
    checked(url, "the certificate names no CRL")
    checked(re.fullmatch(crl_url, url.group(1)), "the certificate's CRL is not at an approved CRL endpoint")
    status, body = fetch(url.group(1), max_bytes)
    checked(status == 200, "the CRL did not download")
    checked(body is not None, "the CRL is larger than the fetch bound")
    crl_pem = directory / "crl.pem"
    crl_pem.write_text(openssl(["crl", "-inform", "DER", "-outform", "PEM"], body, text=False).decode())
    errors = verify(leaf_pem, bundle_pem, trust, now, crl_pem)
    if not errors:
        return "not revoked"
    checked(errors == [(CERT_REVOKED, "0")],
            "openssl verify -crl_check: " + ", ".join(f"error {e} at depth {d}" for e, d in errors))
    return "revoked"


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
        entry = found.setdefault(info["sha256"], dict(info, cert=cert, intermediates=certs[1:],
                                                      names=names(cert), versions=[], key=False))
        entry["versions"].append(version)
        entry["key"] |= key_matches(cert, doc.get("key", ""))
    return list(found.values())


def run(secrets, directory, check=False, now=None, **proof):
    """List, or with `check` prove each certificate revoked. Returns whether the run passed: a
    listing always does, a check only when there is at least one certificate and every one is
    proven revoked. `proof` passes crl_status's trust, crl_url and max_bytes (tests)."""
    certificates = collect(secrets)
    print(f"{len(certificates)} certificate(s) for names outside the fleet subzones in {LEGACY}")
    if check and not certificates:
        print(f"NO STATUS: {LEGACY} holds no certificate for a name outside the fleet subzones, so this run "
              "cannot prove the known certificates (#922 inventory) revoked")
        return False
    passed = True
    for c in certificates:
        label = (f"serial {c['serial']} expires {c['not_after']} names {' '.join(c['names'])} "
                 f"versions {len(c['versions'])} key {'stored' if c['key'] else 'MISSING'}")
        if not check:
            print(label)
            continue
        with tempfile.TemporaryDirectory(dir=directory) as d:
            try:
                state = crl_status(c["cert"], c["intermediates"], Path(d), now, **proof)
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
