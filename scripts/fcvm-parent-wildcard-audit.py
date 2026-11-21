#!/usr/bin/env python3
"""Audit the staging certificates for names in fcvm.dev outside the fleet's subzones (ejc3/aws#922).

Before the staging subzone certificate (#923), the staging coordinator held certificates for
`*.fcvm.dev` and `fcvm.dev`, and copies of their keys reached staging hosts and tiles. Those names
include the production apex and API, so #922 requires each one to be revoked, or shown unusable,
before production is exposed. This script only reads: it changes no secret, sends no ACME request
and revokes nothing. Revoking them is a separate, approved step under #922, after staging has
verifiably stopped serving them.

Run with the staging fleet administration role. After cutover and approval, a different ACME account
with valid authorizations for every identifier on an old certificate can request revocation even
when its original private key is missing. See the operator guidance in README.md.

- No flag: list every distinct certificate in the versions of `fcvm-fleet/tls-wildcard` that names
  something in fcvm.dev outside `tiles-stage.fcvm.dev` and `tiles.fcvm.dev`, with its serial,
  expiry, names, how many versions hold it, and whether the key stored with it is its key (a
  revocation signed with the certificate's own key, RFC 8555 section 7.6, needs it: the
  coordinator used a new ACME account for every order and kept none).
  With --expect, also say which expected certificates the secret does not hold.
- --check --expect SHA256 [SHA256 ...]: prove every expected certificate revoked. The expected set
  is the independent inventory, the SHA-256 fingerprints of the Certificate Transparency entries
  for these names (#922). Every held parent-zone leaf and every public supplement must be in that
  set, and every fingerprint in the set must have evidence. For a missing leaf, repeat
  --public-chain FILE once per leaf. Each local file contains only its leaf and intermediate PEM
  certificates, with the leaf first (256 KiB maximum). Omit leaves already supplied or held in the
  secret. Never derive --expect from the files submitted as proof. Each leaf must verify through
  the untrusted intermediates to a root in this host's trust store before fetching its Let's Encrypt CRL
  endpoint (`http://<issuer>.c.lencr.org/<n>.crl`); the fetch follows no redirect and reads at
  most 8 MiB. OpenSSL then verifies the certificate again with that CRL (`verify -crl_check`),
  which decides the CRL's issuer, signature, freshness and issuing-distribution-point scope.
  Only a run whose one error is "certificate revoked" at depth 0 proves revocation; a clean
  verify means not revoked, and any other error, a failed or timed-out fetch included, is no
  status for that certificate and the next one is checked. Exits non-zero unless every expected
  certificate is proven revoked and the inventory matches exactly.
"""

import argparse
import datetime as dt
import http.client
import json
import os
from pathlib import Path
import re
import stat
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
MAX_PUBLIC_CHAIN_BYTES = 256 << 10
PUBLIC_CERT = re.compile(r"-----BEGIN CERTIFICATE-----\r?\n(?:[a-zA-Z0-9+/=]+\r?\n)+-----END CERTIFICATE-----")
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
    # DNS names compare without case (RFC 5280 section 7.2).
    host = name.lower().removeprefix("*.")
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


def fetch(url, max_bytes, timeout=30):
    """GET `url` with no redirect followed and at most `max_bytes` read: (status, body or None).
    A transport failure (name resolution, connection, timeout) raises ValueError naming it."""
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(req, timeout=timeout) as response:
            body = response.read(max_bytes + 1)
            return response.status, (body if len(body) <= max_bytes else None)
    except urllib.error.HTTPError as error:
        return error.code, b""
    except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
        reason = getattr(error, "reason", error)
        raise ValueError(f"the CRL fetch failed ({type(reason).__name__})") from error


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
               max_bytes=MAX_CRL_BYTES, timeout=30):
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
    status, body = fetch(url.group(1), max_bytes, timeout)
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


def public_certificate(path):
    """Read one bounded, local, certificate-only PEM bundle. Its intermediates are never trust anchors."""
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
        checked(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "a public chain must be a regular file")
        raw = stream.read(MAX_PUBLIC_CHAIN_BYTES + 1)
    checked(len(raw) <= MAX_PUBLIC_CHAIN_BYTES, "public chain file is too large")
    try:
        pem = raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("public chain must contain ASCII PEM certificates") from error
    certs = PUBLIC_CERT.findall(pem)
    checked(len(certs) >= 2 and not PUBLIC_CERT.sub("", pem).strip(),
            "public chain requires only PEM certificates: leaf first, then intermediates")
    leaf, *intermediates = [c + "\n" for c in certs]
    try:
        info = fields(leaf)
        checked(not re.search(r"\bCA:TRUE\b", openssl(["x509", "-noout", "-ext", "basicConstraints"], leaf)),
                "a public chain must begin with an end-entity certificate")
        san = names(leaf)
        checked(any(outside_subzones(n) for n in san), "public certificate is outside the audit scope")
        for ca in intermediates:
            constraints = openssl(["x509", "-noout", "-ext", "basicConstraints"], ca)
            checked(re.search(r"\bCA:TRUE\b", constraints), "a public chain contains another leaf instead of an intermediate")
    except subprocess.CalledProcessError as error:
        raise ValueError("invalid X.509 certificate in public chain") from error
    return dict(info, cert=leaf, intermediates=intermediates, names=san, versions=[], key=False)


def fingerprint(text):
    value = text.replace(":", "").lower()
    checked(re.fullmatch(r"[0-9a-f]{64}", value), "an expected fingerprint is not a SHA-256 hex digest")
    return value


def run(secrets, directory, check=False, now=None, expect=(), public_chains=(), **proof):
    """List, or with `check` prove every expected certificate revoked. Returns whether the run passed:
    a valid listing always does; a check passes only if the secret and optional pinned public
    evidence cover precisely the expected certificates and every leaf is proven revoked. `proof` passes crl_status's trust,
    crl_url, max_bytes and timeout (tests)."""
    expected = {fingerprint(e) for e in expect}
    checked(expected or not check, "--check needs --expect: the SHA-256 fingerprints of the known certificates")
    certificates = collect(secrets)
    print(f"{len(certificates)} certificate(s) for names outside the fleet subzones in {LEGACY}")
    held = {c["sha256"] for c in certificates}
    checked(expected or not public_chains, "--public-chain needs an independent --expect inventory")
    for path in public_chains:
        extra = public_certificate(path)
        digest = extra["sha256"]
        checked(digest in expected, f"public certificate sha256 {digest} is not in the expected set")
        checked(digest not in held, f"duplicate certificate sha256 {digest} is already held or supplied")
        held.add(digest)
        certificates.append(extra)
    passed = True
    for missing in sorted(expected - held):
        print(f"NO STATUS: expected certificate sha256 {missing} is not in {LEGACY} or any supplied public chain")
        passed = False
    for c in certificates:
        if expected and c["sha256"] not in expected:
            print(f"NO STATUS: serial {c['serial']} sha256 {c['sha256']} is held but not in the expected set")
            passed = False
        label = (f"serial {c['serial']} sha256 {c['sha256']} expires {c['not_after']} names {' '.join(c['names'])} "
                 f"versions {len(c['versions'])} key {'stored' if c['key'] else 'MISSING'}")
        if not c["versions"]:
            label += " source public chain"
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
    return passed if check else True


if __name__ == "__main__":
    import boto3

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="prove every expected certificate revoked from its CRL")
    parser.add_argument("--expect", nargs="+", default=(), metavar="SHA256",
                        help="the known certificates' SHA-256 fingerprints (required with --check)")
    parser.add_argument("--public-chain", action="append", default=[], type=Path, metavar="FILE",
                        help="local leaf and intermediate PEMs for one expected certificate missing from the secret; repeatable")
    arguments = parser.parse_args()
    if arguments.public_chain and not arguments.expect:
        parser.error("--public-chain needs an independent --expect inventory")
    if arguments.check and not arguments.expect:
        parser.error("--check needs --expect: the SHA-256 fingerprints of the known certificates")
    os.umask(0o077)
    with tempfile.TemporaryDirectory(dir=os.getenv("XDG_RUNTIME_DIR") or "/dev/shm") as workspace:
        try:
            ok = run(boto3.client("secretsmanager", region_name=REGION), Path(workspace), arguments.check,
                     expect=arguments.expect, public_chains=arguments.public_chain)
        except (OSError, ValueError) as error:
            parser.exit(1, f"audit failed: {error}\n")
    raise SystemExit(0 if ok else 1)
