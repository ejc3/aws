#!/usr/bin/env python3
"""Revoke every certificate for a name outside the fleet's subzones held in the legacy
staging secret (ejc3/aws#922).

Before the staging subzone certificate (#923), the staging coordinator held certificates for
`*.fcvm.dev` and `fcvm.dev`, and copies of their keys reached staging hosts and tiles. Those
names include the production apex and API, so the certificates must stop being valid before
production is exposed. The coordinator used a new ACME account for every order and kept none,
so each certificate is revoked with its own private key (RFC 8555 section 7.6), taken from the
versions of `fcvm-fleet/tls-wildcard`.

Run with the staging fleet administration role. With no flag it lists and changes nothing.

Runbook (#922). Revocation cannot be undone and breaks anything still serving these certificates,
so it runs only after staging's cutover to its subzone certificate (#923) is verified and the
owner has approved the retirement:

1. List: `python3 scripts/fcvm-revoke-parent-wildcards.py`.
2. Revoke exactly the approved serials: `--revoke SERIAL [SERIAL ...]`. A serial that is not
   listed is refused before any request; an already revoked certificate counts as done.
3. Prove it: `python3 scripts/fcvm-parent-wildcard-audit.py --check --expect <the #922
   fingerprints>` exits 0 once Let's Encrypt's CRLs list every one (within hours).
"""

import argparse
import base64
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
DIRECTORY = "https://acme-v02.api.letsencrypt.org/directory"
# The fleet's own subzones: a certificate naming only these is not a parent-zone certificate.
SUBZONES = ("tiles-stage.fcvm.dev", "tiles.fcvm.dev")
PARENT = "fcvm.dev"
SUPERSEDED = 4
USER_AGENT = "ejc3-aws-fcvm-revoke/1"

# DER prefixes of the EC public keys this script signs with: (curve OID, coordinate bytes, alg).
EC_CURVES = {
    bytes.fromhex("06082a8648ce3d030107"): (32, "ES256", "P-256", "-sha256"),
    bytes.fromhex("06052b81040022"): (48, "ES384", "P-384", "-sha384"),
}


def checked(condition, message):
    if not condition:
        raise ValueError(message)


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


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


def der_ints(sig):
    # An ECDSA-Sig-Value: SEQUENCE { INTEGER r, INTEGER s }, short-form lengths at these sizes.
    checked(sig[0] == 0x30, "unexpected ECDSA signature")
    i = 2 if sig[1] < 0x80 else 2 + (sig[1] & 0x7F)
    out = []
    for _ in range(2):
        checked(sig[i] == 0x02, "unexpected ECDSA signature")
        n = sig[i + 1]
        out.append(sig[i + 2:i + 2 + n].lstrip(b"\0"))
        i += 2 + n
    return out


class Signer:
    """A JWS signer for one private key: its JWK and RFC 7518 signature, through openssl."""

    def __init__(self, key_pem, directory):
        self.key = directory / "key.pem"
        self.key.write_text(key_pem)
        spki = openssl(["pkey", "-in", str(self.key), "-pubout", "-outform", "DER"], None, text=False)
        self.public_der = spki
        curve = next(((oid, v) for oid, v in EC_CURVES.items() if oid in spki), None)
        if curve:
            size, self.alg, crv, self.digest = curve[1]
            point = spki[-(2 * size + 1):]
            checked(point[0] == 4, "compressed EC public keys are not supported")
            self.size = size
            self.jwk = {"crv": crv, "kty": "EC", "x": b64url(point[1:1 + size]), "y": b64url(point[1 + size:])}
        else:
            text = openssl(["pkey", "-in", str(self.key), "-noout", "-text_pub"], None)
            checked("Modulus:" in text, "the key is neither EC P-256/P-384 nor RSA")
            hexes = re.search(r"Modulus:\s*\n((?:\s+[0-9a-f:]+\n)+)", text).group(1)
            modulus = bytes.fromhex(re.sub(r"[\s:]", "", hexes)).lstrip(b"\0")
            exponent = int(re.search(r"Exponent:\s*(\d+)", text).group(1))
            self.alg, self.digest, self.size = "RS256", "-sha256", None
            self.jwk = {"e": b64url(exponent.to_bytes((exponent.bit_length() + 7) // 8, "big")),
                        "kty": "RSA", "n": b64url(modulus)}

    def sign(self, data):
        sig = openssl(["dgst", self.digest, "-sign", str(self.key)], data, text=False)
        if self.size is None:
            return sig
        r, s = der_ints(sig)
        return r.rjust(self.size, b"\0") + s.rjust(self.size, b"\0")


def request(url, method="GET", body=None):
    req = urllib.request.Request(url, data=body, method=method, headers={
        "User-Agent": USER_AGENT, **({"Content-Type": "application/jose+json"} if body else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def revoke(cert, key_pem, directory, acme=DIRECTORY):
    """Revoke `cert` signed by its own key. True when revoked now, False when it already was."""
    status, _, body = request(acme)
    checked(status == 200, "the ACME directory did not answer")
    urls = json.loads(body)
    status, headers, _ = request(urls["newNonce"], "HEAD")
    nonce = headers.get("Replay-Nonce") or headers.get("replay-nonce")
    checked(nonce, "the ACME server gave no nonce")
    signer = Signer(key_pem, directory)
    cert_key = openssl(["x509", "-noout", "-pubkey"], cert)
    checked(openssl(["pkey", "-pubin", "-outform", "DER"], cert_key.encode(), text=False) == signer.public_der,
            "the certificate's key is not the key in its secret version")
    der = openssl(["x509", "-outform", "DER"], cert.encode(), text=False)
    protected = b64url(json.dumps({"alg": signer.alg, "jwk": signer.jwk, "nonce": nonce,
                                   "url": urls["revokeCert"]}).encode())
    payload = b64url(json.dumps({"certificate": b64url(der), "reason": SUPERSEDED}).encode())
    signature = b64url(signer.sign(f"{protected}.{payload}".encode()))
    status, _, body = request(urls["revokeCert"], "POST",
                              json.dumps({"protected": protected, "payload": payload, "signature": signature}).encode())
    if status == 200:
        return True
    problem = json.loads(body or b"{}")
    if problem.get("type") == "urn:ietf:params:acme:error:alreadyRevoked":
        return False
    raise RuntimeError(f"revocation refused ({status}): {problem.get('type')}: {problem.get('detail')}")



def collect(secrets):
    """The distinct parent-zone certificates, with their keys, in every version of the legacy secret."""
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
        cert, issuer = certs[0], (certs[1] if len(certs) > 1 else None)
        info = fields(cert)
        if not any(outside_subzones(n) for n in names(cert)):
            continue
        entry = found.setdefault(info["sha256"], dict(info, cert=cert, issuer=issuer, key=doc["key"],
                                                          names=names(cert), versions=[]))
        entry["versions"].append(version)
    return list(found.values())


def run(secrets, directory, serials, acme=DIRECTORY):
    """List the parent-zone certificates; with `serials`, revoke exactly those (each must be listed)."""
    certificates = collect(secrets)
    print(f"{len(certificates)} certificate(s) for names outside the fleet subzones in {LEGACY}")
    named = {s.upper().replace(":", "") for s in serials}
    unknown = named - {c["serial"] for c in certificates}
    checked(not unknown, "not in the listing, nothing revoked: " + " ".join(sorted(unknown)))
    for c in certificates:
        label = f"serial {c['serial']} expires {c['not_after']} names {' '.join(c['names'])} versions {len(c['versions'])}"
        if c["serial"] not in named:
            print(("left (not named) " if named else "") + label)
            continue
        with tempfile.TemporaryDirectory(dir=directory) as d:
            print(("revoked " if revoke(c["cert"], c["key"], Path(d), acme) else "already revoked ") + label)
    return certificates


if __name__ == "__main__":
    import boto3

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--revoke", nargs="+", default=(), metavar="SERIAL", help="revoke exactly these listed serials")
    arguments = parser.parse_args()
    os.umask(0o077)
    with tempfile.TemporaryDirectory(dir=os.getenv("XDG_RUNTIME_DIR") or "/dev/shm") as workspace:
        run(boto3.client("secretsmanager", region_name=REGION), Path(workspace), arguments.revoke)
