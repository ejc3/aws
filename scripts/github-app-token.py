#!/usr/bin/env python3
"""Mint a GitHub installation token for the administration agent's GitHub App (github-admin-tokens.tf).

    scripts/github-app-token.py OWNER [--repos a,b]   # prints ONLY the token on stdout (expires in an hour)

OWNER is an account the app is installed on (ejc3, dolphin-labs-hq). --repos narrows the token to those repositories
of OWNER. The app's credentials come from Secrets Manager `github-app/aws-admin` (JSON: app_id, private_key,
installations {owner: id}), readable only by the jumpbox role. The JWT is signed by the system `openssl`, so nothing
beyond the standard library and boto3 is needed. The private key touches disk only as a 0600 file in a private
temporary directory that is removed before the token is requested. Transient GitHub errors (5xx, 429, network) are
retried with backoff; anything else fails with GitHub's message and no partial output.

Use it in a pipe, never on a command line:
    printf 'Authorization: Bearer %s\\n' "$(scripts/github-app-token.py dolphin-labs-hq)" | curl -H @- ...
    GH_TOKEN=$(scripts/github-app-token.py ejc3) gh api ...
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

SECRET_ID = "github-app/aws-admin"
REGION = "us-west-1"
API = "https://api.github.com"
ATTEMPTS = 4


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(app_id, private_key_pem, now=None, openssl="openssl"):
    """An RS256 JWT for the app: issued a minute in the past (clock skew) and valid for nine minutes, under GitHub's ten."""
    now = int(time.time() if now is None else now)
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    with tempfile.TemporaryDirectory() as tmp:
        os.chmod(tmp, 0o700)
        key = os.path.join(tmp, "key.pem")
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(private_key_pem)
        r = subprocess.run([openssl, "dgst", "-sha256", "-sign", key], input=signing_input, capture_output=True, check=False)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("openssl could not sign the app JWT (is private_key a PEM RSA key?)")
    return f"{header}.{payload}.{b64url(r.stdout)}"


def request(method, path, token, body=None, opener=urllib.request.urlopen, sleep=time.sleep):
    data = None if body is None else json.dumps(body).encode()
    last = None
    for attempt in range(ATTEMPTS):
        req = urllib.request.Request(API + path, data=data, method=method, headers={
            "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ejc3-aws-github-app-token"})
        try:
            with opener(req, timeout=20) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            msg = ""
            try:
                msg = json.loads(e.read().decode()).get("message", "")
            except Exception:
                pass
            if e.code < 500 and e.code != 429:
                raise RuntimeError(f"GitHub answered {e.code} to {method} {path}: {msg}") from None
            last = f"{e.code} {msg}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = str(getattr(e, "reason", e))
        if attempt + 1 < ATTEMPTS:
            sleep(2 ** attempt)
    raise RuntimeError(f"GitHub kept failing {method} {path}: {last}")


def installation_token(creds, owner, repos=None, opener=urllib.request.urlopen, sleep=time.sleep, now=None):
    installations = creds.get("installations") or {}
    if owner not in installations:
        raise RuntimeError(f"the app is not installed on {owner} (known: {', '.join(sorted(installations)) or 'none'})")
    jwt = make_jwt(creds["app_id"], creds["private_key"], now=now)
    body = {"repositories": [r.split("/")[-1] for r in repos]} if repos else None
    out = request("POST", f"/app/installations/{installations[owner]}/access_tokens", jwt, body, opener=opener, sleep=sleep)
    token = out.get("token", "")
    if not token.startswith("ghs_"):
        raise RuntimeError("GitHub returned no installation token")
    return token


def load_creds():
    import boto3
    value = boto3.client("secretsmanager", region_name=REGION).get_secret_value(SecretId=SECRET_ID)["SecretString"]
    creds = json.loads(value)
    for key in ("app_id", "private_key", "installations"):
        if not creds.get(key):
            raise RuntimeError(f"{SECRET_ID} has no {key}")
    return creds


def main(argv=None):
    ap = argparse.ArgumentParser(description="Print a GitHub installation token for the administration agent's app.")
    ap.add_argument("owner", help="an account the app is installed on, e.g. ejc3 or dolphin-labs-hq")
    ap.add_argument("--repos", default="", help="comma-separated repositories of OWNER to narrow the token to")
    args = ap.parse_args(argv)
    repos = [r.strip() for r in args.repos.split(",") if r.strip()]
    try:
        token = installation_token(load_creds(), args.owner, repos or None)
    except Exception as e:
        print(f"github-app-token: {e}", file=sys.stderr)
        return 1
    sys.stdout.write(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
